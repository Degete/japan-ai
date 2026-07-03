"""Priority-aware admission control (with aging).

FIX (Issue #2): the original service accepted a `priority` (urgent/normal/low)
but never scheduled on it — admission was plain FIFO, so an `urgent` request
could sit behind a batch of `low` ones (priority inversion).

`asyncio.Semaphore` alone cannot fix this: when a slot frees, it wakes waiters
in roughly arrival order, ignoring priority. This controller replaces the plain
global semaphore with a priority queue in front of a fixed number of slots:

  * Up to `capacity` tasks run concurrently (same global budget as before).
  * When full, the waiter admitted next is the one with the best *effective
    score*.

AGING (mitigation for the strict-priority starvation trade-off):
  Strict priority (urgent always before low) can starve `low` under sustained
  load. To bound that, a waiter's effective score improves the longer it waits:

      score = base_rank - (wait_seconds / aging_interval)

  Lower score wins. A LOW task (rank 2) overtakes a freshly-arrived URGENT task
  (rank 0) once it has aged past a 2-rank gap — i.e. after ~2·aging_interval
  seconds. This keeps urgent fast in the common case while guaranteeing low
  makes forward progress. Ties break by submission order (FIFO). Set the aging
  interval to 0 to fall back to strict priority.

The controller publishes queue depth per priority so Grafana can show both that
urgent normally jumps the queue and that aged low tasks eventually get admitted.
"""

from __future__ import annotations

import asyncio
import itertools
import time

from src.models import Priority
from src.config import PRIORITY_AGING_INTERVAL_SECONDS
from src.telemetry import ADMISSION_QUEUE_DEPTH

# Lower rank = admitted sooner.
_PRIORITY_RANK: dict[Priority, int] = {
    Priority.URGENT: 0,
    Priority.NORMAL: 1,
    Priority.LOW: 2,
}


class _Waiter:
    __slots__ = ("priority", "rank", "seq", "enqueued_at", "future")

    def __init__(self, priority: Priority, rank: int, seq: int,
                 future: asyncio.Future):
        self.priority = priority
        self.rank = rank
        self.seq = seq
        self.enqueued_at = time.monotonic()
        self.future = future

    def effective_score(self, now: float, aging_interval: float) -> float:
        """Lower is better. Base rank improved by how long we've waited."""
        if aging_interval > 0:
            aged = (now - self.enqueued_at) / aging_interval
        else:
            aged = 0.0
        return self.rank - aged


class PriorityAdmissionController:
    """A capacity-bounded gate that admits waiters by aged priority order."""

    def __init__(self, capacity: int,
                 aging_interval: float = PRIORITY_AGING_INTERVAL_SECONDS):
        self._capacity = capacity
        self._aging_interval = aging_interval
        self._in_use = 0
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()
        self._depth: dict[Priority, int] = {p: 0 for p in Priority}

    def _publish_depth(self) -> None:
        for p, n in self._depth.items():
            ADMISSION_QUEUE_DEPTH.labels(p.value).set(n)

    def _pop_best(self) -> _Waiter | None:
        """Remove and return the waiter with the best (lowest) effective score.

        The backlog is bounded by the concurrency overflow, so a linear scan is
        cheap and lets us apply the time-dependent aging score at selection
        time (a static heap key cannot age)."""
        if not self._waiters:
            return None
        now = time.monotonic()
        best_i = 0
        best_key = (
            self._waiters[0].effective_score(now, self._aging_interval),
            self._waiters[0].seq,
        )
        for i in range(1, len(self._waiters)):
            w = self._waiters[i]
            key = (w.effective_score(now, self._aging_interval), w.seq)
            if key < best_key:
                best_key = key
                best_i = i
        return self._waiters.pop(best_i)

    async def acquire(self, priority: Priority) -> None:
        """Block until a slot is free, honouring aged priority ordering."""
        async with self._lock:
            if self._in_use < self._capacity:
                # Fast path: capacity available, admit immediately.
                self._in_use += 1
                return
            # Slow path: enqueue and wait to be woken.
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            rank = _PRIORITY_RANK.get(priority, 1)
            self._waiters.append(_Waiter(priority, rank, next(self._seq), fut))
            self._depth[priority] += 1
            self._publish_depth()

        # Wait OUTSIDE the lock so other tasks can release/enqueue.
        await fut

    async def release(self, priority: Priority) -> None:
        """Return a slot; wake the best (aged-priority) waiter, if any."""
        async with self._lock:
            waiter = self._pop_best()
            if waiter is not None:
                self._depth[waiter.priority] -= 1
                self._publish_depth()
                # Slot stays "in use": it transfers to the woken waiter.
                if not waiter.future.done():
                    waiter.future.set_result(None)
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
