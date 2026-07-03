"""Observability bootstrap for the Agent Execution Service.

This module is the single place where the three pillars are wired up:

  * Tracing  — OpenTelemetry SDK exporting OTLP/HTTP to the Collector.
               FastAPI and httpx are auto-instrumented; the orchestrator and
               llm_client add manual spans for each pipeline stage, tool call
               and LLM request.
  * Metrics  — prometheus_client. Custom instruments sliced by the dimensions
               that matter operationally (tenant, priority, stage, status).
               Exposed on GET /metrics.
  * Logging  — python-json-logger emitting structured JSON to stdout, with the
               active trace_id / span_id injected into every record so logs can
               be correlated to traces (shipped to Loki by Grafana Alloy).

Keeping this isolated means application code only ever touches thin helpers
(`get_tracer`, the metric objects, `log`), never the SDK wiring.
"""

from __future__ import annotations

import logging
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from pythonjsonlogger import json as jsonlogger
from prometheus_client import Counter, Gauge, Histogram

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "agent-service")

# ─────────────────────────────────────────────────────────────────────────
# Prometheus metrics
#
# Naming follows Prometheus conventions (unit suffixes, _total for counters).
# Label sets are intentionally low-to-moderate cardinality: tenant (a handful),
# priority (3), stage (4), status (4). This is what lets us answer "which
# tenant is burning cost?" or "which stage is slow for urgent tasks?".
# ─────────────────────────────────────────────────────────────────────────

# Task-level outcomes.
TASKS_TOTAL = Counter(
    "agent_tasks_total",
    "Total agent tasks processed, by tenant/priority/status.",
    ["tenant", "priority", "status"],
)

TASK_DURATION = Histogram(
    "agent_task_duration_seconds",
    "End-to-end task execution latency (excludes queue wait).",
    ["tenant", "priority", "status"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 30, 60),
)

# Time a task spent waiting for the tenant lock + concurrency semaphore before
# real work started. This is the key signal for head-of-line blocking.
TASK_QUEUE_WAIT = Histogram(
    "agent_task_queue_wait_seconds",
    "Time a task waited (tenant lock + concurrency semaphore) before executing.",
    ["tenant", "priority"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)

CACHE_EVENTS = Counter(
    "agent_cache_events_total",
    "Response-cache lookups, by result (hit/miss).",
    ["result"],
)

TASKS_IN_PROGRESS = Gauge(
    "agent_tasks_in_progress",
    "Tasks currently executing (past the queue, doing real work).",
)

# Stage-level (plan / tools / summarise / validate).
STAGE_DURATION = Histogram(
    "agent_stage_duration_seconds",
    "Per-pipeline-stage latency.",
    ["stage", "tenant", "priority"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 30),
)

# LLM call instrumentation.
LLM_CALLS_TOTAL = Counter(
    "agent_llm_calls_total",
    "LLM inference calls, by pipeline stage and outcome.",
    ["stage", "outcome"],  # outcome: success | error
)

LLM_REQUEST_DURATION = Histogram(
    "agent_llm_request_seconds",
    "Latency of a single LLM HTTP attempt (per attempt, not per call).",
    ["stage", "status_code"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 10),
)

LLM_RETRIES_TOTAL = Counter(
    "agent_llm_retries_total",
    "LLM call retry attempts, by stage and reason (HTTP status / timeout).",
    ["stage", "reason"],
)

LLM_RATE_LIMIT_WAIT = Histogram(
    "agent_llm_rate_limit_wait_seconds",
    "Time blocked inside the client-side token-bucket rate limiter.",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

# Token usage & cost — the FinOps view, sliced by tenant.
TOKENS_TOTAL = Counter(
    "agent_tokens_total",
    "LLM tokens consumed, by tenant and kind (prompt/completion).",
    ["tenant", "kind"],
)

COST_USD_TOTAL = Counter(
    "agent_cost_usd_total",
    "Estimated LLM spend in USD, by tenant.",
    ["tenant"],
)

# In-memory store growth — surfaces unbounded-growth / leak issues.
STORE_ENTRIES = Gauge(
    "agent_store_entries",
    "Current number of entries held in each in-memory store.",
    ["store"],
)


# ─────────────────────────────────────────────────────────────────────────
# Structured logging with trace correlation
# ─────────────────────────────────────────────────────────────────────────

class _TraceContextFilter(logging.Filter):
    """Inject the active OTel trace_id / span_id into every log record so log
    lines can be joined to traces in Grafana (Loki -> Tempo)."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = ""
            record.span_id = ""
        return True


def _configure_logging() -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s "
            "%(trace_id)s %(span_id)s",
            rename_fields={"levelname": "level", "asctime": "timestamp"},
        )
    )
    handler.addFilter(_TraceContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Quiet noisy access logs but keep them structured.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger(SERVICE_NAME)


def _configure_tracing() -> None:
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.namespace": "agent-platform",
            "deployment.environment": os.getenv("DEPLOY_ENV", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    # OTLP endpoint points at the Collector; exporter defaults to /v1/traces.
    exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


# Public handles.
log: logging.Logger = _configure_logging()


def init_telemetry() -> None:
    """Initialise tracing. Called once at application startup."""
    _configure_tracing()
    log.info("telemetry initialised", extra={"service": SERVICE_NAME})


def get_tracer(name: str = SERVICE_NAME):
    return trace.get_tracer(name)
