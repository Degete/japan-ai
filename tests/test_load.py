"""Load test script.

Sends concurrent requests to the agent execution service to simulate
realistic multi-tenant traffic.  Run after starting the platform:

    python -m tests.test_load

Adjust TOTAL_REQUESTS and CONCURRENCY to control load intensity.
For sustained load testing, increase TOTAL_REQUESTS to 200+ and
run multiple rounds.
"""

import asyncio, httpx, random, time, sys

BASE_URL = "http://localhost:8080"
TENANTS = ["tenant-alpha", "tenant-beta", "tenant-gamma"]
PRIORITIES = ["urgent", "normal", "low"]
TOTAL_REQUESTS = 100
CONCURRENCY = 15

TASK_TEMPLATES = [
    "Analyse quarterly revenue report for {tenant}",
    "Summarise customer feedback from last week for {tenant}",
    "Generate sales forecast for next quarter for {tenant}",
    "Review compliance documentation for {tenant}",
    "Prepare executive briefing on market trends for {tenant}",
    "Audit expense reports from last month for {tenant}",
    "Draft response to partner inquiry for {tenant}",
    "Evaluate vendor proposals for {tenant}",
    "Create onboarding checklist for new hires at {tenant}",
    "Analyse support ticket trends for {tenant}",
]


async def send_task(client: httpx.AsyncClient, idx: int) -> dict:
    tenant = random.choice(TENANTS)
    priority = random.choice(PRIORITIES)
    template = random.choice(TASK_TEMPLATES)
    # Most tasks get a unique description; some reuse descriptions
    # to simulate real-world duplicate queries from the same tenant
    if random.random() < 0.3:
        description = template.format(tenant=tenant)
    else:
        description = f"[Task-{idx:04d}] {template.format(tenant=tenant)}"

    payload = {
        "task_description": description,
        "tenant_id": tenant,
        "priority": priority,
    }
    start = time.time()
    try:
        resp = await client.post(f"{BASE_URL}/tasks", json=payload, timeout=60)
        elapsed = time.time() - start
        data = resp.json()
        status = data.get("status", "unknown")
        has_result = bool(data.get("result"))
        tokens = data.get("token_usage", {})
        print(f"[{idx:03d}] tenant={tenant:<14s} priority={priority:<7s} "
              f"status={status:<10s} has_result={has_result}  "
              f"tokens={tokens}  {elapsed:.2f}s")
        return {"idx": idx, "status": status, "elapsed": elapsed,
                "tenant": tenant, "priority": priority,
                "has_result": has_result, "tokens": tokens}
    except Exception as e:
        elapsed = time.time() - start
        print(f"[{idx:03d}] tenant={tenant:<14s} priority={priority:<7s} "
              f"ERROR={e}  {elapsed:.2f}s")
        return {"idx": idx, "status": "error", "elapsed": elapsed,
                "tenant": tenant, "priority": priority,
                "has_result": False, "tokens": {}}


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def bounded(i):
            async with sem:
                return await send_task(client, i)

        tasks = [bounded(i) for i in range(TOTAL_REQUESTS)]
        results = await asyncio.gather(*tasks)

    # ── Summary ──────────────────────────────────────────────
    total = len(results)
    completed = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] == "failed"]
    errors = [r for r in results if r["status"] == "error"]
    latencies = [r["elapsed"] for r in results]

    completed_with_result = sum(1 for r in completed if r["has_result"])
    completed_without_result = sum(1 for r in completed if not r["has_result"])

    print("\n" + "=" * 60)
    print("LOAD TEST SUMMARY")
    print("=" * 60)
    print(f"Total requests:    {total}")
    print(f"Completed:         {len(completed)}  "
          f"(with result: {completed_with_result}, "
          f"empty result: {completed_without_result})")
    print(f"Failed:            {len(failed)}")
    print(f"Errors:            {len(errors)}")

    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"\nLatency  P50={p50:.2f}s  P95={p95:.2f}s  "
              f"P99={p99:.2f}s  Max={latencies[-1]:.2f}s")

    # Token summary per tenant
    print("\nToken usage by tenant:")
    tenant_tokens: dict[str, dict] = {}
    for r in results:
        t = r["tenant"]
        if t not in tenant_tokens:
            tenant_tokens[t] = {"prompt": 0, "completion": 0, "count": 0}
        tenant_tokens[t]["prompt"] += r["tokens"].get("prompt_tokens", 0)
        tenant_tokens[t]["completion"] += r["tokens"].get("completion_tokens", 0)
        tenant_tokens[t]["count"] += 1

    for t, v in sorted(tenant_tokens.items()):
        print(f"  {t}: {v['count']} tasks, "
              f"prompt={v['prompt']} tokens, "
              f"completion={v['completion']} tokens")


if __name__ == "__main__":
    asyncio.run(main())
