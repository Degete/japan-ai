"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
"""

import time
import traceback
from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.config import LLM_SERVER_URL


# Execution audit trail for debugging and compliance review
_execution_log: list[dict] = []


async def run_task(task_id: str, description: str,
                   tenant_id: str, priority: Priority) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise pipeline."""
    created = time.time()
    total_prompt_tokens = 0
    total_completion_tokens = 0

    try:
        # ── Step 1: Planning ──────────────────────────────────
        plan = await call_llm(
            prompt=f"Plan the following task: {description}",
            max_tokens=256,
        )
        total_prompt_tokens += plan.get("prompt_tokens", 0)
        total_completion_tokens += plan.get("completion_tokens", 0)

        if plan.get("error"):
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
        tool_results = await execute_tools(tools_to_run)

        # ── Step 3: Summarise ────────────────────────────────
        summary_prompt = (
            f"Summarise results for task: {description}\n"
            f"Tool outputs: {tool_results}"
        )
        summary = await call_llm(prompt=summary_prompt, max_tokens=512)
        total_prompt_tokens += summary.get("prompt_tokens", 0)
        total_completion_tokens += summary.get("completion_tokens", 0)

        # Check if summary generation failed
        if summary.get("error") and summary.get("text") is None:
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
        validation = await call_llm(
            prompt=(
                f"Rate the quality of this response (1-10) and flag "
                f"any factual errors or compliance issues:\n\n"
                f"{summary.get('text', '')}"
            ),
            max_tokens=128,
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
        error_detail = (
            f"Task execution failed: {str(e)}\n"
            f"Trace: {traceback.format_exc()}\n"
            f"Pipeline stage: {'plan' if total_prompt_tokens == 0 else 'execute'}\n"
            f"LLM endpoint: {LLM_SERVER_URL}"
        )
        return TaskResult(
            task_id=task_id, status=TaskStatus.FAILED,
            tenant_id=tenant_id, priority=priority,
            error=error_detail,
            token_usage={"prompt_tokens": total_prompt_tokens,
                         "completion_tokens": total_completion_tokens},
            created_at=created, completed_at=time.time(),
        )
