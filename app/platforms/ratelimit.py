"""One rate limiter per platform: a minimum gap with jitter, an hourly budget and a cool-down after blocks."""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.i18n import t
from app.platforms.base import Blocked

VIENNA = ZoneInfo("Europe/Vienna")
WAKE_S = 60.0       # a waiting caller looks again at least this often (the budget may have been raised in Settings)


class BudgetFull(Blocked):
    """The hourly budget is used up and the caller does not wait for it: nothing was sent. `free_in_s`: until the
    next request is allowed again."""

    def __init__(self, msg: str, free_in_s: float) -> None:
        super().__init__(msg)
        self.free_in_s = free_in_s


class BudgetWait:
    """Background work that waits when a site's hourly budget is used up instead of giving up (jobs, the product
    search): while `waiting(bw)` is active, the limiter sleeps outside its lock until a request is free again, so
    page requests keep failing fast meanwhile. `sites` maps the limiters waited for right now to the monotonic time
    they are free again; `on_change(bw)` runs whenever it changes (a page shows it, a job moves its timeout)."""

    def __init__(self, on_change: Callable[[BudgetWait], None] | None = None) -> None:
        self.sites: dict[str, float] = {}
        self.on_change = on_change

    def free_at(self) -> dict[str, datetime]:
        """The sites waited for and when their budget lets the next request through (Vienna time, for the page)."""
        now, wall = time.monotonic(), datetime.now(VIENNA)
        return {name: wall + timedelta(seconds=max(0.0, until - now)) for name, until in self.sites.items()}

    async def sleep(self, name: str, seconds: float) -> None:
        self.sites[name] = time.monotonic() + seconds
        self._changed()
        try:
            await asyncio.sleep(min(seconds, WAKE_S) + random.uniform(0, 2))
        finally:
            self.sites.pop(name, None)
            self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self)


_patience: ContextVar[BudgetWait | None] = ContextVar("budget_wait", default=None)


@contextmanager
def waiting(bw: BudgetWait | None = None) -> Iterator[BudgetWait]:
    """Requests made inside (and in tasks started inside) wait for a used-up hourly budget instead of failing."""
    bw = bw or BudgetWait()
    token = _patience.set(bw)
    try:
        yield bw
    finally:
        _patience.reset(token)


def waits() -> bool:
    """True inside waiting(): a used-up budget delays the work instead of stopping it."""
    return _patience.get() is not None


_urgent: ContextVar[bool] = ContextVar("urgent_request", default=False)


@contextmanager
def urgent() -> Iterator[None]:
    """Requests made inside go first in each site's queue: someone waits for them on a page (the price check of the
    ad viewer), the search agents' requests wait behind them."""
    token = _urgent.set(True)
    try:
        yield
    finally:
        _urgent.reset(token)


def is_urgent() -> bool:
    return _urgent.get()


class RateLimiter:
    """Serialises requests with a minimum gap (plus jitter), an hourly budget and a global cool-down.

    Waits that would block a caller for longer than `max_wait_s` (cool-down after 403/429 or an
    exhausted budget) raise Blocked instead, so dashboard requests fail fast and scheduled jobs
    simply retry on their next run. Background work inside waiting() waits for an exhausted budget
    instead (outside the lock, so nobody else hangs behind it). `name` is the platform shown in
    those messages.
    """

    def __init__(self, min_gap_s: float, per_hour: int = 0, max_wait_s: float = 45.0, *, name: str = "willhaben",
                 persist: str | None = None) -> None:
        """`persist`: a site's own limiter keeps its cool-down after a block across a restart (setting
        `limiter_pause_<persist>`), so an update right after a 403 does not hit the site again at once."""
        self.min_gap_s = min_gap_s
        self.per_hour = per_hour
        self.max_wait_s = max_wait_s
        self.name = name
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cooldown_until = 0.0
        self._strikes = 0
        self._recent: deque[float] = deque()   # monotonic start times of the last hour
        self._urgent_waiting = 0               # urgent() requests waiting for the lock: the others let them go first
        self.persist = persist
        self._restored = persist is None

    def _restore(self) -> None:
        """Once, on first use: the cool-down a block set before the restart (wall clock -> this process's clock)."""
        self._restored = True
        try:
            import json

            from app.db import get_setting
            data = json.loads(get_setting(f"limiter_pause_{self.persist}") or "{}")
            left = float(data.get("until", 0)) - time.time()
            if left > 0:
                self._cooldown_until = max(self._cooldown_until, time.monotonic() + left)
                self._strikes = max(self._strikes, int(data.get("strikes") or 0))
        except Exception:  # noqa: BLE001 - no database yet, a broken value: start without a pause
            pass

    def _save_pause(self) -> None:
        if self.persist is None:
            return
        try:
            import json

            from app.db import set_setting
            left = max(0.0, self._cooldown_until - time.monotonic())
            set_setting(f"limiter_pause_{self.persist}",
                        json.dumps({"until": time.time() + left, "strikes": self._strikes}) if left else "")
        except Exception:  # noqa: BLE001 - the pause still holds in memory
            pass

    def _budget_wait(self, now: float) -> float:
        while self._recent and now - self._recent[0] >= 3600:
            self._recent.popleft()
        if not self.per_hour or len(self._recent) < self.per_hour:
            return 0.0
        return self._recent[0] + 3600 - now

    async def __aenter__(self) -> None:
        if not self._restored:
            self._restore()
        urgent = _urgent.get()
        while True:
            if urgent:
                self._urgent_waiting += 1
            try:
                await self._lock.acquire()
            finally:
                if urgent:
                    self._urgent_waiting -= 1
            if not urgent and self._urgent_waiting:     # someone waits on a page: they go first
                self._lock.release()
                await asyncio.sleep(0.05)
                continue
            try:
                now = time.monotonic()
                if self._cooldown_until - now > self.max_wait_s:
                    raise Blocked(t("{name} pause after a block, {s} s left", name=self.name,
                                    s=f"{self._cooldown_until - now:.0f}"))
                budget = self._budget_wait(now)
                patient = _patience.get()
                if budget > self.max_wait_s and patient is None:
                    raise BudgetFull(t("{name}: request limit ({n}/h) reached, free again in {m} min", name=self.name,
                                       n=self.per_hour, m=f"{budget / 60:.0f}"), budget)
                if budget <= self.max_wait_s:
                    wait = max(self._cooldown_until - now, self._last + self.min_gap_s - now, budget, 0.0)
                    if wait > 0:
                        await asyncio.sleep(wait + random.uniform(0, self.min_gap_s / 2))
                    self._recent.append(time.monotonic())
                    return                                  # the lock stays held until __aexit__
            except BaseException:
                self._lock.release()
                raise
            self._lock.release()                            # wait for the budget without holding up anyone else
            assert patient is not None
            await patient.sleep(self.name, budget)

    async def __aexit__(self, *exc: Any) -> None:
        self._last = time.monotonic()
        self._lock.release()

    def penalize(self) -> float:
        """Exponential back-off after 403/429; returns the cool-down in seconds."""
        self._strikes += 1
        delay = min(60 * 2 ** (self._strikes - 1), 3600)
        self._cooldown_until = time.monotonic() + delay
        self._save_pause()
        return delay

    def reward(self) -> None:
        if self._strikes and self.persist is not None:
            self._strikes = 0
            self._save_pause()
        self._strikes = 0

    def refund(self) -> None:
        """The request just let through was not sent after all (the VPN test's exit moved): it costs no budget."""
        if self._recent:
            self._recent.pop()

    def free_in(self, k: int) -> float:
        """Seconds until the hour's `k` oldest requests have left it (0 for k <= 0)."""
        now = time.monotonic()
        self._budget_wait(now)
        if k <= 0 or not self._recent:
            return 0.0
        return max(0.0, self._recent[min(k, len(self._recent)) - 1] + 3600 - now)

    def reset(self) -> None:
        """Forget the pause, the pacing and the hour's requests (tests: each one starts like a fresh site)."""
        self._lock = asyncio.Lock()
        self._last = self._cooldown_until = 0.0
        self._strikes = 0
        self._recent.clear()
        self._restored = self.persist is None or self._restored

    @property
    def cooling_down_s(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def last_hour(self) -> int:
        self._budget_wait(time.monotonic())
        return len(self._recent)
