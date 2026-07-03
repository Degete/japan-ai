"""Token-accounting probe (Issue #6 verification).

Proves that estimated tokens from failed 500 attempts are NO LONGER folded into
the billable prompt_tokens of a later successful call.

We monkey-patch the shared httpx client to return a fixed sequence
(500, 500, 200) and assert the returned prompt_tokens equals exactly what the
"server" reported on the successful attempt — not that value plus the
per-failure estimate.

Run:  python -m tests.token_accounting_probe
"""

import asyncio

from src import llm_client


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


class _FakeClient:
    """Returns a preset sequence of responses, ignoring the request."""

    def __init__(self, sequence):
        self._seq = list(sequence)
        self.calls = 0

    async def post(self, *a, **k):
        self.calls += 1
        return self._seq.pop(0)


async def run_case(sequence, prompt):
    fake = _FakeClient(sequence)
    llm_client._http_client = fake                      # inject
    # Neutralise real sleeps/rate-limiter so the probe is instant.
    llm_client._rate_limiter.acquire = lambda: asyncio.sleep(0)
    orig_sleep = asyncio.sleep
    async def _no_sleep(*_a, **_k):
        return None
    asyncio.sleep = _no_sleep
    try:
        return await llm_client.call_llm(prompt=prompt, max_tokens=128, stage="probe")
    finally:
        asyncio.sleep = orig_sleep
        llm_client._http_client = None


async def main():
    prompt = "one two three four five"           # 5 words
    server_prompt_tokens = 42                     # what the LLM actually returns

    print("=== Token accounting (Issue #6) ===\n")

    # Case A: two 500s then a 200. Old code would report 42 + 5 + 5 = 52.
    res = await run_case(
        [
            _Resp(500),
            _Resp(500),
            _Resp(200, {"text": "ok", "prompt_tokens": server_prompt_tokens,
                        "completion_tokens": 10}),
        ],
        prompt,
    )
    print("Sequence: 500, 500, 200")
    print(f"  server-reported prompt_tokens : {server_prompt_tokens}")
    print(f"  returned  prompt_tokens       : {res['prompt_tokens']}")
    old_would_be = server_prompt_tokens + 2 * max(1, len(prompt.split()))
    print(f"  (old buggy code would report  : {old_would_be})")
    ok_a = res["prompt_tokens"] == server_prompt_tokens
    print(f"  billable NOT inflated by retries: {'PASS' if ok_a else 'FAIL'}\n")

    # Case B: all failures. Old code returned prompt_tokens = accumulated est.
    res2 = await run_case([_Resp(500) for _ in range(5)], prompt)
    print("Sequence: 500 x5 (total failure)")
    print(f"  returned prompt_tokens        : {res2['prompt_tokens']} (expected 0)")
    ok_b = res2["prompt_tokens"] == 0
    print(f"  failed call bills 0 tokens     : {'PASS' if ok_b else 'FAIL'}\n")

    print("RESULT:", "ALL PASS" if (ok_a and ok_b) else "FAILURES PRESENT")


if __name__ == "__main__":
    asyncio.run(main())
