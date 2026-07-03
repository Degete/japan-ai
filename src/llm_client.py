"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.

Observability (added):
  * Each HTTP attempt runs inside an `llm.request` span tagged with stage,
    attempt number and resulting status code.
  * Metrics record call outcomes, per-attempt latency, retries (by reason),
    time blocked in the client-side rate limiter, and cumulative retry backoff.
"""

import httpx
import asyncio
import random
import time

from opentelemetry import trace

from src.config import (
    LLM_SERVER_URL,
    TASK_TIMEOUT_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    RETRY_MAX_ATTEMPTS_RATE_LIMIT,
    RETRY_RATE_LIMIT_BASE_DELAY,
    RETRY_TOTAL_BACKOFF_BUDGET,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
)
from src.telemetry import (
    log,
    get_tracer,
    LLM_CALLS_TOTAL,
    LLM_REQUEST_DURATION,
    LLM_RETRIES_TOTAL,
    LLM_RATE_LIMIT_WAIT,
    LLM_RETRY_BACKOFF_WAIT,
    WASTED_TOKENS_TOTAL,
)

_tracer = get_tracer()

# Shared HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=TASK_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


class _TokenBucket:
    """Rate limiter to protect the downstream LLM service from overload
    and prevent runaway inference costs during traffic spikes."""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._rate)
            self._last_refill = now
            while self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._rate)
                self._last_refill = now
            self._tokens -= 1


# Global rate limiter for LLM calls
_rate_limiter = _TokenBucket(rate=LLM_RATE_LIMIT_RPS, capacity=LLM_RATE_LIMIT_BURST)


def _parse_retry_after(response: "httpx.Response") -> float | None:
    """Return the Retry-After delay in seconds, if the header is present and
    parseable (supports the delta-seconds form). Returns None otherwise."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        # HTTP-date form is possible but the mock never sends it; ignore.
        return None


def _backoff_delay(reason: str, attempt: int, retry_after: float | None) -> float:
    """Backoff delay for the *next* retry, differentiated by failure class.

    * 429 (rate limited): honour Retry-After if given, else a longer
      exponential backoff — the downstream is already overloaded, so we must
      not hammer it.
    * everything else (500 / timeout / connection): standard exponential
      backoff with jitter — transient faults, safe to retry promptly.
    """
    if reason == "429":
        if retry_after is not None:
            base = retry_after
        else:
            base = RETRY_RATE_LIMIT_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
    else:
        base = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
    jitter = random.uniform(0, base * 0.3)
    return base + jitter


async def call_llm(prompt: str, max_tokens: int = 512, stage: str = "unknown") -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    client = _get_client()
    last_error = None
    last_status = None
    wasted_tokens = 0
    total_backoff = 0.0            # cumulative sleep across retries (budget)
    attempt = 0

    # Class-differentiated retry policy (Issue #5):
    #   * 500 / timeout / connection → fast exponential backoff (transient).
    #   * 429 → back off harder (Retry-After if present), fewer attempts, so we
    #     don't amplify load on an already-overloaded LLM.
    # A single call also gets fewer allowed attempts once it has seen a 429, and
    # the total time spent sleeping is capped by RETRY_TOTAL_BACKOFF_BUDGET.
    while True:
        reason = None
        retry_after = None
        try:
            # Time spent blocked in the client-side token-bucket limiter.
            _rl0 = time.perf_counter()
            await _rate_limiter.acquire()
            LLM_RATE_LIMIT_WAIT.observe(time.perf_counter() - _rl0)

            with _tracer.start_as_current_span("llm.request") as span:
                span.set_attribute("llm.stage", stage)
                span.set_attribute("llm.attempt", attempt)
                span.set_attribute("llm.max_tokens", max_tokens)
                _req0 = time.perf_counter()
                response = await client.post(
                    f"{LLM_SERVER_URL}/v1/inference",
                    json={"prompt": prompt, "max_tokens": max_tokens},
                )
                _elapsed = time.perf_counter() - _req0
                span.set_attribute("http.status_code", response.status_code)
                LLM_REQUEST_DURATION.labels(
                    stage, str(response.status_code)
                ).observe(_elapsed)

                if response.status_code == 200:
                    data = response.json()
                    if wasted_tokens:
                        WASTED_TOKENS_TOTAL.labels(stage).inc(wasted_tokens)
                    LLM_CALLS_TOTAL.labels(stage, "success").inc()
                    return data

                last_status = response.status_code
                last_error = f"LLM returned {response.status_code}"
                reason = str(response.status_code)
                span.set_status(trace.Status(trace.StatusCode.ERROR, last_error))
                LLM_RETRIES_TOTAL.labels(stage, reason).inc()

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response)
                    if retry_after is not None:
                        span.set_attribute("llm.retry_after_seconds", retry_after)

                # Estimate tokens burned by a 500 that partially processed the
                # prompt before failing. Counted as waste, not billable.
                if response.status_code == 500:
                    wasted_tokens += max(1, len(prompt.split()))

        except httpx.TimeoutException:
            last_error = "LLM request timed out"
            last_status = 408
            reason = "timeout"
            LLM_REQUEST_DURATION.labels(stage, "timeout").observe(
                TASK_TIMEOUT_SECONDS
            )
            LLM_RETRIES_TOTAL.labels(stage, "timeout").inc()
        except Exception as e:
            last_error = str(e)
            last_status = 0
            reason = "exception"
            LLM_RETRIES_TOTAL.labels(stage, "exception").inc()

        # Decide whether another attempt is allowed. 429s get a smaller budget.
        attempt += 1
        max_attempts = (
            RETRY_MAX_ATTEMPTS_RATE_LIMIT if reason == "429" else RETRY_MAX_ATTEMPTS
        )
        if attempt >= max_attempts:
            break

        # Class-differentiated backoff, capped by the total backoff budget.
        delay = _backoff_delay(reason, attempt - 1, retry_after)
        remaining = RETRY_TOTAL_BACKOFF_BUDGET - total_backoff
        if remaining <= 0:
            break  # spent the backoff budget; fail fast instead of stalling
        delay = min(delay, remaining)
        total_backoff += delay
        LLM_RETRY_BACKOFF_WAIT.labels(stage, reason or "unknown").observe(delay)
        await asyncio.sleep(delay)

    LLM_CALLS_TOTAL.labels(stage, "error").inc()
    if wasted_tokens:
        WASTED_TOKENS_TOTAL.labels(stage).inc(wasted_tokens)
    log.warning(
        "llm call exhausted retries",
        extra={"stage": stage, "last_status": last_status,
               "last_error": last_error, "attempts": attempt,
               "total_backoff_s": round(total_backoff, 2),
               "wasted_tokens": wasted_tokens},
    )
    return {
        "error": last_error,
        "text": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "status_code": last_status,
    }
