"""Sliding-window rate limiter for every OpenD request.

* One sliding window per endpoint (or per shared bucket, e.g. place_order and
  place_combo_order share one budget).
* A request that would exceed the limit is queued until capacity is available;
  it is never sent early.
* Waiters are served by priority (lower number first), then FIFO. Orders and
  cancels therefore overtake background snapshot polling.
* `reserve_high`: background work (priority >= Priority.POLL) may only use
  `max_calls - reserve_high` of a window, so an order that needs a fresh mid
  price can always get a snapshot quickly even while polling is busy.
* `consumer_caps`: a named consumer (e.g. the market overview page) may use at
  most N calls of the window. A consumer that is held back by its own cap never
  blocks other requests behind it.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable


class Priority(IntEnum):
    CRITICAL = 0     # emergency stop, cancellations
    ORDER = 1        # order placement and the price lookups that feed it
    NORMAL = 5       # user actions in the UI, subscription management, status
    POLL = 9         # background snapshot polling (stocks / options pages)
    BACKGROUND = 10  # lowest: market overview refresh, news enrichment


@dataclass
class Limit:
    max_calls: int | None = None      # None = no documented limit; calls are still counted
    window_sec: float = 30.0
    min_interval_sec: float = 0.0
    reserve_high: int = 0             # calls per window kept free for priority < POLL
    consumer_caps: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_calls is not None and self.max_calls < 1:
            raise ValueError("max_calls must be >= 1 or None")
        if self.window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        if self.max_calls is not None and not 0 <= self.reserve_high < self.max_calls:
            raise ValueError("reserve_high must be between 0 and max_calls - 1")
        for name, cap in self.consumer_caps.items():
            if cap < 1:
                raise ValueError(f"consumer cap for {name} must be >= 1")


class _Waiter:
    __slots__ = ("priority", "seq", "future", "consumer")

    def __init__(self, priority: int, seq: int, future: asyncio.Future, consumer: str | None):
        self.priority, self.seq, self.future, self.consumer = priority, seq, future, consumer


class _Bucket:
    def __init__(self, name: str, limit: Limit, margin: float):
        self.name = name
        self.limit = limit
        self.margin = margin
        self.sent: deque[float] = deque()
        self.sent_by: dict[str, deque[float]] = {}
        self.last: float | None = None
        self.total = 0
        self.waiters: list[_Waiter] = []
        self.timer: asyncio.TimerHandle | None = None
        self.timer_at = 0.0

    def purge(self, now: float) -> None:
        horizon = self.limit.window_sec + self.margin
        for dq in (self.sent, *self.sent_by.values()):
            while dq and now - dq[0] >= horizon:
                dq.popleft()

    def cap_for(self, priority: int) -> int | None:
        if self.limit.max_calls is None:
            return None
        if priority >= Priority.POLL:
            return self.limit.max_calls - self.limit.reserve_high
        return self.limit.max_calls

    def _until_fits(self, dq: deque[float], cap: int, now: float) -> float:
        if len(dq) < cap:
            return 0.0
        idx = len(dq) - cap  # this call must leave the window before we fit
        return max(0.0, dq[idx] + self.limit.window_sec + self.margin - now)

    def bucket_wait(self, now: float, priority: int) -> float:
        """Wait imposed by the shared window and min interval (not by consumer caps)."""
        self.purge(now)
        wait = 0.0
        cap = self.cap_for(priority)
        if cap is not None:
            wait = self._until_fits(self.sent, cap, now)
        if self.limit.min_interval_sec and self.last is not None:
            wait = max(wait, self.last + self.limit.min_interval_sec - now)
        return max(0.0, wait)

    def consumer_wait(self, now: float, consumer: str | None) -> float:
        if consumer is None or consumer not in self.limit.consumer_caps:
            return 0.0
        self.purge(now)
        return self._until_fits(self.sent_by.get(consumer, deque()), self.limit.consumer_caps[consumer], now)

    def wait_time(self, now: float, priority: int, consumer: str | None = None) -> float:
        return max(self.bucket_wait(now, priority), self.consumer_wait(now, consumer))

    def record(self, now: float, consumer: str | None = None) -> None:
        self.sent.append(now)
        if consumer is not None:
            self.sent_by.setdefault(consumer, deque()).append(now)
        self.last = now
        self.total += 1

    def used_in_window(self, now: float, consumer: str | None = None) -> int:
        dq = self.sent if consumer is None else self.sent_by.get(consumer, deque())
        return sum(1 for t in dq if now - t < self.limit.window_sec)

    def prune(self) -> None:
        self.waiters = [w for w in self.waiters if not w.future.done()]

    def queued(self) -> int:
        return sum(1 for w in self.waiters if not w.future.done())


class SlidingWindowLimiter:
    def __init__(self, limits: dict[str, Limit], aliases: dict[str, str] | None = None,
                 safety_margin_sec: float = 0.0, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._margin = float(safety_margin_sec)
        self._aliases = dict(aliases or {})
        self._buckets: dict[str, _Bucket] = {
            name: _Bucket(name, lim, self._margin) for name, lim in limits.items()}
        self._seq = itertools.count()

    # ---------------------------------------------------------------- config
    @classmethod
    def from_config(cls, rate_limits: dict, settings: dict | None = None,
                    clock: Callable[[], float] = time.monotonic) -> "SlidingWindowLimiter":
        limits: dict[str, Limit] = {}
        aliases: dict[str, str] = {}
        for endpoint, spec in (rate_limits or {}).items():
            spec = dict(spec or {})
            bucket = spec.get("bucket")
            if bucket and bucket != endpoint:
                aliases[endpoint] = bucket
                continue
            limits[endpoint] = Limit(
                max_calls=spec.get("max_calls"),
                window_sec=float(spec.get("window_sec", 30) or 30),
                min_interval_sec=float(spec.get("min_interval_sec", 0) or 0),
                reserve_high=int(spec.get("reserve_high", 0) or 0),
                consumer_caps={str(k): int(v) for k, v in (spec.get("consumer_caps") or {}).items()},
            )
        settings = settings or {}
        return cls(limits, aliases, float(settings.get("safety_margin_sec", 0.0) or 0.0), clock)

    def bucket_name(self, endpoint: str) -> str:
        seen = set()
        name = endpoint
        while name in self._aliases and name not in seen:
            seen.add(name)
            name = self._aliases[name]
        return name

    def _bucket(self, endpoint: str) -> _Bucket:
        name = self.bucket_name(endpoint)
        b = self._buckets.get(name)
        if b is None:  # undocumented endpoint: count it, do not limit it
            b = self._buckets[name] = _Bucket(name, Limit(None, 30.0), self._margin)
        return b

    # --------------------------------------------------------------- acquire
    async def acquire(self, endpoint: str, priority: int = Priority.NORMAL, consumer: str | None = None) -> None:
        """Wait until `endpoint` may be called, then count the call."""
        b = self._bucket(endpoint)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        b.waiters.append(_Waiter(int(priority), next(self._seq), fut, consumer))
        self._drain(b, loop)
        if fut.done():
            return
        try:
            await fut
        except asyncio.CancelledError:
            # Cancelled while queued: the entry is skipped later. If the slot had already
            # been granted the call is still counted (conservative).
            self._drain(b, loop)
            raise

    def _drain(self, b: _Bucket, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            b.prune()
            if not b.waiters:
                return
            now = self._clock()
            next_wait: float | None = None
            granted = False
            for w in sorted(b.waiters, key=lambda x: (x.priority, x.seq)):
                bw = b.bucket_wait(now, w.priority)
                if bw > 0:
                    # the shared window is full for this priority: nobody behind may overtake it
                    next_wait = bw if next_wait is None else min(next_wait, bw)
                    break
                cw = b.consumer_wait(now, w.consumer)
                if cw > 0:
                    # held back only by its own consumer cap: let the others through
                    next_wait = cw if next_wait is None else min(next_wait, cw)
                    continue
                b.waiters.remove(w)
                b.record(now, w.consumer)
                w.future.set_result(None)
                granted = True
                break
            if not granted:
                if next_wait is not None:
                    self._arm(b, loop, now, next_wait)
                return

    def _arm(self, b: _Bucket, loop: asyncio.AbstractEventLoop, now: float, wait: float) -> None:
        at = now + wait
        if b.timer is not None:
            if b.timer_at <= at + 1e-6:
                return
            b.timer.cancel()
        b.timer_at = at
        # +2 ms so a coarse monotonic clock (Windows ~15 ms) has really passed the boundary
        b.timer = loop.call_later(wait + 0.002, self._on_timer, b, loop)

    def _on_timer(self, b: _Bucket, loop: asyncio.AbstractEventLoop) -> None:
        b.timer = None
        self._drain(b, loop)

    # ----------------------------------------------------------------- usage
    def usage(self) -> dict[str, dict]:
        now = self._clock()
        out = {}
        for name, b in self._buckets.items():
            b.purge(now)
            out[name] = {
                "used": b.used_in_window(now),
                "max_calls": b.limit.max_calls,
                "window_sec": b.limit.window_sec,
                "queued": b.queued(),
                "total": b.total,
                "shared_by": sorted(e for e, t in self._aliases.items() if self.bucket_name(e) == name),
                "consumers": {c: {"used": b.used_in_window(now, c), "cap": cap}
                              for c, cap in b.limit.consumer_caps.items()},
            }
        return out
