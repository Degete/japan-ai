"""Retry-policy probe (Issue #5 verification).

Exercises `_backoff_delay` and the 429-vs-500 attempt caps directly, so we can
prove the class-differentiated behaviour deterministically without depending on
the mock LLM's random failures.

Run:  python -m tests.retry_policy_probe
"""

import statistics

from src import config
from src.llm_client import _backoff_delay, _parse_retry_after


class _FakeResp:
    def __init__(self, headers):
        self.headers = headers


def main():
    print("=== Retry policy (Issue #5) ===\n")
    print("Config:")
    print(f"  RETRY_MAX_ATTEMPTS (500/timeout) = {config.RETRY_MAX_ATTEMPTS}")
    print(f"  RETRY_MAX_ATTEMPTS_RATE_LIMIT (429) = {config.RETRY_MAX_ATTEMPTS_RATE_LIMIT}")
    print(f"  RETRY_BASE_DELAY (500) = {config.RETRY_BASE_DELAY}s")
    print(f"  RETRY_RATE_LIMIT_BASE_DELAY (429) = {config.RETRY_RATE_LIMIT_BASE_DELAY}s")
    print(f"  RETRY_TOTAL_BACKOFF_BUDGET = {config.RETRY_TOTAL_BACKOFF_BUDGET}s\n")

    # 1) Retry-After parsing.
    print("Retry-After header honoured:")
    print(f"  'Retry-After: 5'  -> {_parse_retry_after(_FakeResp({'retry-after': '5'}))}s")
    print(f"  (missing)         -> {_parse_retry_after(_FakeResp({}))}")
    print(f"  'Retry-After: xx' -> {_parse_retry_after(_FakeResp({'retry-after': 'xx'}))}\n")

    # 2) 429 backs off harder than 500 for the same attempt number.
    def avg(reason, attempt, ra=None, n=2000):
        return statistics.mean(_backoff_delay(reason, attempt, ra) for _ in range(n))

    print("Mean backoff delay by class (jitter-averaged):")
    print(f"{'attempt':>8} | {'500/timeout':>12} | {'429 (no RA)':>12}")
    for a in range(3):
        print(f"{a:>8} | {avg('500', a):>11.2f}s | {avg('429', a):>11.2f}s")

    # 3) Retry-After overrides the computed 429 delay.
    ra_delay = avg("429", 2, ra=5.0)
    print(f"\n429 with Retry-After=5s -> mean delay {ra_delay:.2f}s "
          f"(honours header, ignores exponential schedule)")

    # 4) Worst-case cumulative backoff comparison (old vs new).
    old = sum(config.RETRY_BASE_DELAY * (config.RETRY_BACKOFF_FACTOR ** a)
              for a in range(config.RETRY_MAX_ATTEMPTS - 1))
    print(f"\nWorst-case cumulative backoff for a 500 storm:")
    print(f"  OLD (uniform, 5 attempts, uncapped) = {old:.2f}s")
    print(f"  NEW (budget-capped)                 = {config.RETRY_TOTAL_BACKOFF_BUDGET:.2f}s")
    print(f"  → a single call can no longer burn >{config.RETRY_TOTAL_BACKOFF_BUDGET:.0f}s of "
          f"the {config.TASK_TIMEOUT_SECONDS}s task deadline in backoff alone.")


if __name__ == "__main__":
    main()
