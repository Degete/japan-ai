"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
  4. Validate — quality gate via the LLM

Observability (added):
  * Each stage runs inside its own span (stage.plan / stage.tools /
    stage.summarise / stage.validate) and records a stage-latency metric.
  * Individual tool calls get tool.<name> spans.
  * Token usage and estimated cost are recorded per tenant.
"""

import time
import traceback

from opentelemetry import trace

from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.config import (
    LLM_SERVER_URL,
    TOKEN_COST_PER_1K_INPUT,
    TOKEN_COST_PER_1K_OUTPUT,
)
from src.telemetry import (
    log,
    get_tracer,
    STAGE_DURATION,
    TOKENS_TOTAL,
    COST_USD_TOTAL,
)

_tracer = get_tracer()

# Execution audit trail for debugging and compliance review
_execution_log: list[dict] = []


def execution_log_size() -> int:
    """Expose the audit-log size so it can be published as a gauge."""
    return len(_execution_log)


def _record_cost(tenant_id: str, prompt_tokens: int, completion_tokens: int) -> None:
    if prompt_tokens:
        TOKENS_TOTAL.labels(tenant_id, "prompt").inc(prompt_tokens)
    if completion_tokens:
        TOKENS_TOTAL.labels(tenant_id, "completion").inc(completion_tokens)
    cost = (
        prompt_tokens / 1000.0 * TOKEN_COST_PER_1K_INPUT
        + completion_tokens / 1000.0 * TOKEN_COST_PER_1K_OUTPUT
    )
    if cost:
        COST_USD_TOTAL.labels(tenant_id).inc(cost)


async def run_task(task_id: str, description: str,
                   tenant_id: str, priority: Priority) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise pipeline."""
    created = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    prio = priority.value

    try:
        # ── Step 1: Planning ──────────────────────────────────
        with _tracer.start_as_current_span("stage.plan") as span:
            span.set_attribute("stage", "plan")
            _t0 = time.perf_counter()
            plan = await call_llm(
                prompt=f"Plan the following task: {description}",
                max_tokens=256,
                stage="plan",
            )
            STAGE_DURATION.labels("plan", tenant_id, prio).observe(
                time.perf_counter() - _t0
            )
        total_prompt_tokens += plan.get("prompt_tokens", 0)
        total_completion_tokens += plan.get("completion_tokens", 0)

        if plan.get("error"):
            log.warning(
                "plan stage failed",
                extra={"task_id": task_id, "tenant_id": tenant_id,
                       "stage": "plan", "error": plan["error"]},
            )
            _record_cost(tenant_id, total_prompt_tokens, total_completion_tokens)
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=plan["error"],
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        # ── Step 2: Tool execution ───────────────────────────
        tools_to_run = [
            ("search", {"query": description}),
            ("database_lookup", {"key": tenant_id}),
            ("calculator", {"expression": "1+1"}),
        ]
        with _tracer.start_as_current_span("stage.tools") as span:
            span.set_attribute("stage", "tools")
            _t0 = time.perf_counter()
            tool_results = await execute_tools(tools_to_run)
            STAGE_DURATION.labels("tools", tenant_id, prio).observe(
                time.perf_counter() - _t0
            )

        # ── Step 3: Summarise ────────────────────────────────
        summary_prompt = (
            f"Summarise results for task: {description}\n"
            f"Tool outputs: {tool_results}"
        )
        with _tracer.start_as_current_span("stage.summarise") as span:
            span.set_attribute("stage", "summarise")
            _t0 = time.perf_counter()
            summary = await call_llm(prompt=summary_prompt, max_tokens=512,
                                     stage="summarise")
            STAGE_DURATION.labels("summarise", tenant_id, prio).observe(
                time.perf_counter() - _t0
            )
        total_prompt_tokens += summary.get("prompt_tokens", 0)
        total_completion_tokens += summary.get("completion_tokens", 0)

        # Check if summary generation failed
        if summary.get("error") and summary.get("text") is None:
            log.warning(
                "summarise stage failed",
                extra={"task_id": task_id, "tenant_id": tenant_id,
                       "stage": "summarise", "error": summary["error"]},
            )
            _record_cost(tenant_id, total_prompt_tokens, total_completion_tokens)
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=summary["error"],
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        # ── Step 4: Quality validation ─────────────────────
        # Enterprise quality gate: validate LLM output meets
        # accuracy and compliance standards before returning to tenant
        with _tracer.start_as_current_span("stage.validate") as span:
            span.set_attribute("stage", "validate")
            _t0 = time.perf_counter()
            validation = await call_llm(
                prompt=(
                    f"Rate the quality of this response (1-10) and flag "
                    f"any factual errors or compliance issues:\n\n"
                    f"{summary.get('text', '')}"
                ),
                max_tokens=128,
                stage="validate",
            )
            STAGE_DURATION.labels("validate", tenant_id, prio).observe(
                time.perf_counter() - _t0
            )
        total_prompt_tokens += validation.get("prompt_tokens", 0)
        total_completion_tokens += validation.get("completion_tokens", 0)

        # Record execution details for audit trail
        _execution_log.append({
            "task_id": task_id,
            "tenant_id": tenant_id,
            "description": description,
            "plan_prompt": f"Plan the following task: {description}",
            "plan_response": plan,
            "tool_results": tool_results,
            "summary_prompt": summary_prompt,
            "summary_response": summary,
            "quality_score": validation.get("text", ""),
            "token_usage": {"prompt": total_prompt_tokens,
                            "completion": total_completion_tokens},
            "completed_at": time.time(),
        })

        _record_cost(tenant_id, total_prompt_tokens, total_completion_tokens)

        return TaskResult(
            task_id=task_id, status=TaskStatus.COMPLETED,
            tenant_id=tenant_id, priority=priority,
            result=summary.get("text", ""),
            token_usage={"prompt_tokens": total_prompt_tokens,
                         "completion_tokens": total_completion_tokens},
            created_at=created, completed_at=time.time(),
        )

    except Exception as e:
        # Provide detailed error context to help tenants
        # debug integration issues faster
        span = trace.get_current_span()
        span.record_exception(e)
        span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
        log.exception(
            "task raised unhandled exception",
            extra={"task_id": task_id, "tenant_id": tenant_id},
        )
        error_detail = (
            f"Task execution failed: {str(e)}\n"
            f"Trace: {traceback.format_exc()}\n"
            f"Pipeline stage: {'plan' if total_prompt_tokens == 0 else 'execute'}\n"
            f"LLM endpoint: {LLM_SERVER_URL}"
        )
        _record_cost(tenant_id, total_prompt_tokens, total_completion_tokens)
        return TaskResult(
            task_id=task_id, status=TaskStatus.FAILED,
            tenant_id=tenant_id, priority=priority,
            error=error_detail,
            token_usage={"prompt_tokens": total_prompt_tokens,
                         "completion_tokens": total_completion_tokens},
            created_at=created, completed_at=time.time(),
        )
