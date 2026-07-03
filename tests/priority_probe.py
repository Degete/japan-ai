"""Priority-scheduling probe (Issue #2 verification).

The standard load test randomises priority, which cannot cleanly show priority
ordering. This probe does a controlled experiment:

  1. Saturate the service with filler tasks so the global admission gate is full
     and a real queue forms.
  2. Immediately submit an interleaved burst of LOW then URGENT tasks for the
     SAME tenant (so the per-tenant cap is identical) with unique descriptions
     (cache miss). Because low is submitted first, a FIFO scheduler would finish
     low first; a priority scheduler should finish URGENT first.
  3. Record completion order and per-priority latency.

Run against the running service:  python -m tests.priority_probe
"""

import asyncio
import time
import httpx

BASE_URL = "http://localhost:8080"
TENANT = "tenant-probe"          # isolated tenant → clean per-tenant cap
FILLER = 20                      # saturate the global gate
N_PER_PRIORITY = 8               # urgent vs low to compare


async def submit(client, desc, priority, tag, results):
    t0 = time.time()
    r = await client.post(
        f"{BASE_URL}/tasks",
        json={"task_description": desc, "tenant_id": TENANT, "priority": priority},
        timeout=90,
    )
    dt = time.time() - t0
    data = r.json()
    results.append({
        "tag": tag, "priority": priority, "elapsed": dt,
        "finished_at": time.time(), "status": data.get("status"),
    })


async def main():
    async with httpx.AsyncClient() as client:
        results: list[dict] = []

        # 1) Saturate with filler (different tenants so they don't consume the
        #    probe tenant's per-tenant slots, but still fill the GLOBAL gate).
        filler_tasks = [asyncio.create_task(_saturate(client, i))
                        for i in range(FILLER)]
        await asyncio.sleep(0.5)  # let the gate fill

        # 2) Interleave LOW (submitted first) then URGENT, same tenant.
        probe = []
        for i in range(N_PER_PRIORITY):
            probe.append(submit(client, f"low-{i}-{time.time()}", "low",
                                 f"low-{i}", results))
        for i in range(N_PER_PRIORITY):
            probe.append(submit(client, f"urgent-{i}-{time.time()}", "urgent",
                                 f"urgent-{i}", results))

        await asyncio.gather(*probe)
        for t in filler_tasks:
            t.cancel()

        # 3) Report.
        results.sort(key=lambda r: r["finished_at"])
        print("\nCompletion order (first finished → last):")
        for i, r in enumerate(results):
            print(f"  {i+1:2d}. {r['priority']:<7s} {r['tag']:<10s} "
                  f"elapsed={r['elapsed']:.2f}s status={r['status']}")

        def avg(p):
            xs = [r["elapsed"] for r in results if r["priority"] == p]
            return sum(xs) / len(xs) if xs else float("nan")

        print("\nAverage latency by priority (same tenant, urgent submitted "
              "AFTER low):")
        print(f"  urgent avg = {avg('urgent'):.2f}s")
        print(f"  low    avg = {avg('low'):.2f}s")

        urgent_ranks = [i for i, r in enumerate(results)
                        if r["priority"] == "urgent"]
        low_ranks = [i for i, r in enumerate(results)
                     if r["priority"] == "low"]
        print(f"\nMean completion rank  urgent={sum(urgent_ranks)/len(urgent_ranks):.1f} "
              f"low={sum(low_ranks)/len(low_ranks):.1f}  "
              f"(lower = finished earlier)")


async def _saturate(client, i):
    """Long-ish filler stream on a separate tenant to keep the gate full."""
    try:
        while True:
            await client.post(
                f"{BASE_URL}/tasks",
                json={"task_description": f"filler-{i}-{time.time()}",
                      "tenant_id": f"filler-{i % 4}", "priority": "normal"},
                timeout=90,
            )
    except (asyncio.CancelledError, Exception):
        return


if __name__ == "__main__":
    asyncio.run(main())
