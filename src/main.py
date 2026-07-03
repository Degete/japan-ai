"""FastAPI application — Agent Execution Service.

Provides the HTTP API for submitting and querying agent tasks.
"""

import uuid
import time
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from typing import Optional

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task, execution_log_size
from cachetools import TTLCache

from src.config import (
    MAX_CONCURRENT_TASKS,
    MAX_CONCURRENT_TASKS_PER_TENANT,
    TASK_TIMEOUT_SECONDS,
    TASK_STORE_MAX_ENTRIES,
    TASK_STORE_TTL_SECONDS,
    RESPONSE_CACHE_MAX_ENTRIES,
    RESPONSE_CACHE_TTL_SECONDS,
)
from src import telemetry
from src.admission import PriorityAdmissionController
from src.telemetry import (
    log,
    get_tracer,
    TASKS_TOTAL,
    TASK_DURATION,
    TASK_QUEUE_WAIT,
    TASKS_IN_PROGRESS,
    CACHE_EVENTS,
    STORE_ENTRIES,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry.init_telemetry()
    # Instrument httpx (used by llm_client) so outbound LLM calls appear as
    # child CLIENT spans within each task trace.
    HTTPXClientInstrumentor().instrument()
    log.info("agent-service starting up")
    yield
    log.info("agent-service shutting down")


app = FastAPI(title="Agent Execution Service", lifespan=lifespan)

# Auto-instrument incoming HTTP requests. Exclude /metrics and /health from
# tracing to avoid noise from scrapers/health-checks.
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")

_tracer = get_tracer()

# Task storage.
task_store: TTLCache[str, TaskResult] = TTLCache(
    maxsize=TASK_STORE_MAX_ENTRIES, ttl=TASK_STORE_TTL_SECONDS,
)

# Response cache for repeated queries — avoids redundant LLM calls.
_response_cache: TTLCache[str, dict] = TTLCache(
    maxsize=RESPONSE_CACHE_MAX_ENTRIES, ttl=RESPONSE_CACHE_TTL_SECONDS,
)

# Global ceiling on concurrent task executions (protects downstream LLM
# service and bounds total in-flight cost across the whole service).
_admission = PriorityAdmissionController(capacity=MAX_CONCURRENT_TASKS)

# Per-tenant concurrency limiter.
_tenant_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_tenant_semaphore(tenant_id: str) -> asyncio.Semaphore:
    sem = _tenant_semaphores.get(tenant_id)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS_PER_TENANT)
        _tenant_semaphores[tenant_id] = sem
    return sem


def _update_store_gauges() -> None:
    """Publish current in-memory store sizes so unbounded growth is visible."""
    STORE_ENTRIES.labels(store="task_store").set(len(task_store))
    STORE_ENTRIES.labels(store="response_cache").set(len(_response_cache))
    STORE_ENTRIES.labels(store="tenant_semaphores").set(len(_tenant_semaphores))
    STORE_ENTRIES.labels(store="execution_log").set(execution_log_size())


class CreateTaskBody(BaseModel):
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody):
    task_id = str(uuid.uuid4())

    with _tracer.start_as_current_span("agent.task") as task_span:
        task_span.set_attribute("agent.task_id", task_id)
        task_span.set_attribute("tenant.id", body.tenant_id)
        task_span.set_attribute("task.priority", body.priority.value)
        task_span.set_attribute("task.description", body.task_description)

        # Cache key: tenant + description (priority excluded because
        # task results are priority-independent in the current design)
        cache_key = f"{body.tenant_id}:{body.task_description}"
        if cache_key in _response_cache:
            task_span.set_attribute("cache.hit", True)
            CACHE_EVENTS.labels(result="hit").inc()
            cached = _response_cache[cache_key]
            result = TaskResult(
                task_id=task_id, status=TaskStatus.COMPLETED,
                tenant_id=body.tenant_id, priority=body.priority,
                result=cached.get("result"),
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(), completed_at=time.time(),
            )
            task_store[task_id] = result
            TASKS_TOTAL.labels(body.tenant_id, body.priority.value, "completed").inc()
            _update_store_gauges()
            log.info(
                "task served from cache",
                extra={"task_id": task_id, "tenant_id": body.tenant_id,
                       "priority": body.priority.value},
            )
            return _to_response(result)

        task_span.set_attribute("cache.hit", False)
        CACHE_EVENTS.labels(result="miss").inc()

        # Execute the task (bounded by concurrency limit)
        task_store[task_id] = TaskResult(
            task_id=task_id, status=TaskStatus.PENDING,
            tenant_id=body.tenant_id, priority=body.priority,
        )

        async def _guarded_execute():
            # Measure time spent queued (waiting on the priority admission gate
            # + per-tenant limiter) separately from actual execution — this is
            # the head-of-line-blocking signal.
            queue_start = time.perf_counter()
            tenant_sem = _get_tenant_semaphore(body.tenant_id)
            with _tracer.start_as_current_span("agent.queue_wait") as qspan:
                qspan.set_attribute("tenant.id", body.tenant_id)
                qspan.set_attribute("task.priority", body.priority.value)
                async with _admission.slot(body.priority):
                    async with tenant_sem:
                        wait_s = time.perf_counter() - queue_start
                        qspan.set_attribute("queue.wait_seconds", wait_s)
                        TASK_QUEUE_WAIT.labels(
                            body.tenant_id, body.priority.value
                        ).observe(wait_s)
                        TASKS_IN_PROGRESS.inc()
                        try:
                            return await run_task(
                                task_id=task_id,
                                description=body.task_description,
                                tenant_id=body.tenant_id,
                                priority=body.priority,
                            )
                        finally:
                            TASKS_IN_PROGRESS.dec()

        # Enforce task-level deadline: clients should not wait indefinitely
        exec_start = time.perf_counter()
        timed_out = False
        try:
            result = await asyncio.wait_for(
                _guarded_execute(), timeout=TASK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            timed_out = True
            result = TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=body.tenant_id, priority=body.priority,
                error="Task execution exceeded time limit",
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(), completed_at=time.time(),
            )
        exec_elapsed = time.perf_counter() - exec_start

        task_store[task_id] = result
        task_span.set_attribute("task.status", result.status.value)
        task_span.set_attribute("task.timed_out", timed_out)

        TASKS_TOTAL.labels(
            body.tenant_id, body.priority.value, result.status.value
        ).inc()
        TASK_DURATION.labels(
            body.tenant_id, body.priority.value, result.status.value
        ).observe(exec_elapsed)

        if timed_out:
            task_span.set_status(trace.Status(trace.StatusCode.ERROR,
                                              "task timed out"))
            log.warning(
                "task timed out",
                extra={"task_id": task_id, "tenant_id": body.tenant_id,
                       "priority": body.priority.value,
                       "elapsed_s": round(exec_elapsed, 3)},
            )
        elif result.status == TaskStatus.FAILED:
            log.error(
                "task failed",
                extra={"task_id": task_id, "tenant_id": body.tenant_id,
                       "priority": body.priority.value,
                       "error": (result.error or "")[:200]},
            )
        else:
            log.info(
                "task completed",
                extra={"task_id": task_id, "tenant_id": body.tenant_id,
                       "priority": body.priority.value,
                       "elapsed_s": round(exec_elapsed, 3)},
            )

        # Cache successful responses for future identical requests
        if result.status == TaskStatus.COMPLETED:
            _response_cache[cache_key] = {"result": result.result}

        _update_store_gauges()
        return _to_response(result)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    if task_id not in task_store:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task_store[task_id])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _to_response(r: TaskResult) -> TaskResponse:
    return TaskResponse(
        task_id=r.task_id, status=r.status,
        tenant_id=r.tenant_id, priority=r.priority,
        result=r.result, error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at, completed_at=r.completed_at,
    )
