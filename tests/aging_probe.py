"""Priority-aging probe (Fix #2 mitigation verification).

Directly exercises `PriorityAdmissionController` (no HTTP/LLM) to show that,
under sustained higher-priority pressure, LOW waiters still get admitted thanks
to aging — instead of starving as they would under strict priority.

Scenario:
  * capacity = 1, the slot initially held.
  * Enqueue 3 LOW waiters, then a continuous stream of URGENT waiters that keep
    arriving faster than the queue drains, so there is ALWAYS an urgent waiting.
  * Each admitted worker holds the slot briefly, then releases.

With strict priority (aging OFF), every fresh URGENT outranks every waiting LOW,
so the 3 LOW tasks are admitted dead last. With aging ON, a LOW waiter's
effective score improves as it waits and eventually overtakes a fresh URGENT, so
LOW is interleaved much earlier.

Run:  python -m tests.aging_probe
"""

import asyncio

from src.admission import PriorityAdmissionController
from src.models import Priority


async def _scenario(aging_interval: float, label: str):
    ctrl = PriorityAdmissionController(capacity=1, aging_interval=aging_interval)
    await ctrl.acquire(Priority.URGENT)          # occupy the only slot

    admitted_order: list[str] = []
    hold = 0.10                                  # slot hold time per worker

    async def worker(name: str, priority: Priority):
        await ctrl.acquire(priority)
        admitted_order.append(name)
        await asyncio.sleep(hold)
        await ctrl.release(priority)

    tasks = []
    # 3 LOW waiters enqueue up front.
    for i in range(3):
        tasks.append(asyncio.create_task(worker(f"low-{i}", Priority.LOW)))
    await asyncio.sleep(0.01)

    # Continuous URGENT pressure: a new urgent arrives every `hold` seconds, so
    # there is always at least one urgent competing whenever a slot frees.
    async def urgent_stream():
        for i in range(10):
            tasks.append(asyncio.create_task(worker(f"urgent-{i}", Priority.URGENT)))
            await asyncio.sleep(hold)
    stream = asyncio.create_task(urgent_stream())

    await asyncio.sleep(0.01)
    await ctrl.release(Priority.URGENT)          # free the initial slot

    await stream
    await asyncio.gather(*tasks)

    low_positions = [i for i, n in enumerate(admitted_order) if n.startswith("low")]
    mean_low = sum(low_positions) / len(low_positions)
    print(f"\n[{label}] aging_interval={aging_interval}s")
    print("  admission order:", " ".join(admitted_order))
    print(f"  LOW admitted at positions {low_positions} of {len(admitted_order)}")
    print(f"  mean LOW position: {mean_low:.1f}  (lower = earlier / less starved)")
    return mean_low, low_positions, len(admitted_order)


async def main():
    print("=== Priority aging (Fix #2 mitigation) ===")
    strict_mean, strict_pos, n1 = await _scenario(0.0, "STRICT priority (aging OFF)")
    aged_mean, aged_pos, n2 = await _scenario(0.25, "AGED priority (aging ON)")

    print("\nSummary:")
    print(f"  strict: LOW positions {strict_pos} (mean {strict_mean:.1f}) "
          f"→ starved to the back")
    print(f"  aged  : LOW positions {aged_pos} (mean {aged_mean:.1f}) "
          f"→ interleaved earlier")
    improved = aged_mean < strict_mean
    print(f"\n  RESULT: aging moved LOW earlier: {'PASS' if improved else 'NO CHANGE'}")


if __name__ == "__main__":
    asyncio.run(main())
