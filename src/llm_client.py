"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.
"""

import httpx
import asyncio
import random
import time
from src.config import (
    LLM_SERVER_URL,
    TASK_TIMEOUT_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
)

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


async def call_llm(prompt: str, max_tokens: int = 512) -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    client = _get_client()
    last_error = None
    last_status = None
    accumulated_tokens = 0

    # Unified retry policy: all transient errors (500, 429, timeout)
    # use the same exponential backoff strategy for simplicity
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            await _rate_limiter.acquire()
            response = await client.post(
                f"{LLM_SERVER_URL}/v1/inference",
                json={"prompt": prompt, "max_tokens": max_tokens},
            )

            if response.status_code == 200:
                data = response.json()
                # Include any token overhead from failed attempts
                data["prompt_tokens"] = data.get("prompt_tokens", 0) + accumulated_tokens
                return data

            last_status = response.status_code
            last_error = f"LLM returned {response.status_code}"

            # Track estimated tokens for failed attempts that were
            # partially processed by the LLM before failing
            if response.status_code == 500:
                accumulated_tokens += max(1, len(prompt.split()))

        except httpx.TimeoutException:
            last_error = "LLM request timed out"
            last_status = 408
        except Exception as e:
            last_error = str(e)
            last_status = 0

        # Exponential backoff with jitter before next retry
        if attempt < RETRY_MAX_ATTEMPTS - 1:
            delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
            jitter = random.uniform(0, delay * 0.3)
            await asyncio.sleep(delay + jitter)

    return {
        "error": last_error,
        "text": "",
        "prompt_tokens": accumulated_tokens,
        "completion_tokens": 0,
        "status_code": last_status,
    }
