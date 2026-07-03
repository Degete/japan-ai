"""Priority-aware admission control.

FIX (Issue #2): the original service accepted a `priority` (urgent/normal/low)
but never scheduled on it — admission was plain FIFO, so an `urgent` request
could sit behind a batch of `low` ones (priority inversion).

`asyncio.Semaphore` alone cannot fix this: when a slot frees, it wakes waiters
in roughly arrival order, ignoring priority. This controller replaces the plain
global semaphore with a **priority queue** in front of a fixed number of slots:

  * Up to `capacity` tasks run concurrently (same global budget as before).
  * When full, waiters are ordered by (priority_rank, submission_seq):
      - higher priority is admitted first;
      - within the same priority it stays FIFO (no reordering ⇒ no starvation
        of same-priority tasks, and fairness is preserved).

The controller also publishes queue depth per priority so Grafana can show that
urgent work is genuinely jumping the queue.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools

from src.models import Priority
from src.telemetry import ADMISSION_QUEUE_DEPTH

# Lower rank = admitted sooner.
_PRIORITY_RANK: dict[Priority, int] = {
    Priority.URGENT: 0,
    Priority.NORMAL: 1,
    Priority.LOW: 2,
}


class PriorityAdmissionController:
    """A capacity-bounded gate that admits waiters in priority order."""

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._in_use = 0
        # Heap of (rank, seq, future, priority). `seq` breaks ties in FIFO
        # order and keeps heap items unique/comparable.
        self._waiters: list[tuple[int, int, asyncio.Future, Priority]] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()
        self._depth: dict[Priority, int] = {p: 0 for p in Priority}

    def _publish_depth(self) -> None:
        for p, n in self._depth.items():
            ADMISSION_QUEUE_DEPTH.labels(p.value).set(n)

    async def acquire(self, priority: Priority) -> None:
        """Block until a slot is free, honouring priority ordering."""
        async with self._lock:
            if self._in_use < self._capacity:
                # Fast path: capacity available, admit immediately.
                self._in_use += 1
                return
            # Slow path: enqueue and wait to be woken in priority order.
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            rank = _PRIORITY_RANK.get(priority, 1)
            heapq.heappush(
                self._waiters, (rank, next(self._seq), fut, priority)
            )
            self._depth[priority] += 1
            self._publish_depth()

        # Wait OUTSIDE the lock so other tasks can release/enqueue.
        await fut

    async def release(self, priority: Priority) -> None:
        """Return a slot; wake the highest-priority waiter, if any."""
        async with self._lock:
            if self._waiters:
                _, _, fut, wp = heapq.heappop(self._waiters)
                self._depth[wp] -= 1
                self._publish_depth()
                # Slot stays "in use": it transfers to the woken waiter.
                if not fut.done():
                    fut.set_result(None)
                else:
                    # Waiter was cancelled; reclaim the slot.
                    self._in_use -= 1
            else:
                self._in_use -= 1

    class _Slot:
        def __init__(self, ctrl: "PriorityAdmissionController", priority: Priority):
            self._ctrl = ctrl
            self._priority = priority

        async def __aenter__(self):
            await self._ctrl.acquire(self._priority)
            return self

        async def __aexit__(self, *exc):
            await self._ctrl.release(self._priority)

    def slot(self, priority: Priority) -> "PriorityAdmissionController._Slot":
        """`async with controller.slot(priority): ...` admission guard."""
        return self._Slot(self, priority)
