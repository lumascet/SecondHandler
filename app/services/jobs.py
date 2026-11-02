"""Background jobs on the user's behalf (ad actions, publishing, chat messages), one after another per lane.

The page that starts a job returns at once; the header shows what is running right now, what is queued and the
last results (GET /jobs). Work that is not queued here shows up too: track() wraps a Claude call a page waits for
(price verdict, Geizhals search) or a task of its own (ad batch), and add_source() lists work that keeps its state
elsewhere (the product search, from its DB row). Lanes: "ads" (own ads: update, delete, publish, ...), "chat" (messages, sync) and "claude"
(answers and revisions Claude writes). Within a lane
jobs run strictly in order (an update must not overlap a delete run); the lanes run side by side, so a chat answer
never waits behind twenty deletes. The pacing towards willhaben stays the shared limiter's: requests of both lanes
still go out one by one. A site's used-up hourly budget pauses a job instead of failing it: it goes on by itself
when the budget has room again (the header says until when; the wait does not count towards JOB_TIMEOUT_S). Jobs
live in memory only: a restart drops the queue (callers persist their own markers and
clean them up at startup, see my_ads.recover, chats.recover_sending, ad_creator.recover_publishing).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.db import utcnow
from app.i18n import t

if TYPE_CHECKING:
    from app.platforms.ratelimit import BudgetWait

log = logging.getLogger(__name__)

KEEP_FINISHED = 8
SHOW_FINISHED_S = 15 * 60      # finished jobs stay visible in the header this long


@dataclass
class Job:
    id: int
    kind: str                  # e.g. "myads-update": a still queued job of the same kind is reused, not doubled
    label: str
    lane: str = "ads"
    status: str = "queued"     # queued | running | done | failed | cancelled (taken back while queued)
    step: str = ""             # what happens right now, e.g. "Delete „Fahrrad“"
    current: str | None = None  # the item being worked on right now, as a page marker key ("ad:123", "draft:5")
    done: int = 0
    total: int = 0
    result: str = ""
    problems: int = 0          # items that did not work out (job itself finished)
    link: str | None = None    # the page with the result (the finished-job notice opens it)
    quiet: bool = False        # no notice when it finishes: the page that started it shows the result itself
    keys: list[str] = field(default_factory=list)   # every marker key the job will touch (a page's cancel button finds it)
    runs_at: datetime | None = None   # grace period (submit(grace=True)): not before this; cancel it meanwhile
    created_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    on_cancel: Callable[[], None] | None = field(default=None, repr=False)   # clears the submitter's markers
    cancellable: bool = False   # may be cancelled while queued (has on_cancel, or no markers to clear)
    # irreversible work (submit(grace=True): send, publish, buy, delete): never waits for a used-up hourly budget
    # while running – it could not be taken back meanwhile and would fire long after the click; it fails instead
    irreversible: bool = False

    def progress(self, done: int, step: str = "", current: str | None = None) -> None:
        self.done, self.step, self.current = done, step, current
        if current:     # what the job records from here on belongs to this item (timeline)
            from app.services import timeline
            timeline.update_context(**timeline.marker_ids(current))

    def view(self) -> dict[str, Any]:
        wait = max((self.runs_at - utcnow()).total_seconds(), 0) if self.runs_at and self.status == "queued" else 0
        return {"id": self.id, "label": self.label, "lane": self.lane, "status": self.status, "step": self.step, "current": self.current,
                "done": self.done, "total": self.total, "result": self.result, "problems": self.problems,
                "link": self.link, "quiet": self.quiet, "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "keys": self.keys, "cancellable": self.status == "queued" and self.cancellable, "grace_s": round(wait, 1)}


LANES = ("ads", "chat", "claude", "sim", "agents")   # claude: drafts and revisions (no willhaben account request, can take a minute);
#   sim: the chat simulator's test chats (never in the way of real answers); agents: search-agent checks started by hand

_ids = itertools.count(1)
_locks: dict[str, asyncio.Lock] = {}
_active: list[Job] = []                                  # running first, then queued, in order
_finished: deque[Job] = deque(maxlen=KEEP_FINISHED)      # newest first


_sources: list[Callable[[], dict[str, list[dict[str, Any]]]]] = []


def reset() -> None:
    """Forget everything (tests: every test has its own event loop). A task still running on the previous test's loop
    is cancelled, so it does not wake up later on a closed loop ("Event loop is closed")."""
    for j in list(_active):
        task = getattr(j, "task", None)
        if task is not None and not task.done():
            try:
                task.cancel()
            except RuntimeError:          # its loop is closed already: nothing left to cancel
                pass
    _locks.clear()
    _active.clear()
    _finished.clear()


def add_source(fn: Callable[[], dict[str, list[dict[str, Any]]]]) -> None:
    """Work that keeps its own state (the product search): `fn()` returns {"active": [view], "recent": [view]} in
    the shape of Job.view(), with string ids ("ps-12") so they never clash with job numbers."""
    if fn not in _sources:
        _sources.append(fn)


@asynccontextmanager
async def track(kind: str, label: str, *, lane: str = "claude", link: str | None = None, quiet: bool = False,
                current: str | None = None) -> AsyncIterator[Job]:
    """Show work that runs right here in the header like a job: running while the block runs, then among the
    finished ones. Not queued: it starts at once and holds no lane. The block may set job.result, or job.status =
    "failed" for a failure it handled itself; an exception marks it failed and goes on."""
    job = Job(next(_ids), kind, label, lane=lane, status="running", current=current, link=link, quiet=quiet)
    _active.append(job)
    try:
        yield job
        if job.status == "running":
            job.status = "done"
    except BaseException as e:
        job.status, job.result = "failed", str(e) or type(e).__name__
        raise
    finally:
        job.finished_at, job.step, job.current = utcnow(), "", None
        if job in _active:
            _active.remove(job)
        _finished.appendleft(job)


def grace_s() -> float:
    """How long an irreversible job (delete, mark sold, send) waits in the queue before it runs, so that the click
    can still be taken back (the page's cancel button, the jobs popover) instead of a confirm dialog before it."""
    from app.config import get_settings
    return max(float(get_settings().job_grace_s), 0)


def submit(kind: str, label: str, run: Callable[[Job], Awaitable[str | None]], *, lane: str = "ads", total: int = 0,
           current: str | None = None, reuse_queued: bool = False, keys: list[str] | None = None,
           on_cancel: Callable[[], None] | None = None, grace: bool | float = False, quiet: bool = False,
           link: str | None = None) -> Job:
    """Queue `run(job)` in `lane`; it returns the result line shown in the header. Needs a running event loop.
    `link`: the page with the result (the finished-job notice opens it; no notice on that page itself).

    `keys`: the marker keys ("ad:123", "draft:5") of everything the job will touch, so the elements waiting for it
    can offer a cancel button while it is queued. `on_cancel` clears those markers when it is cancelled (cancel());
    a job with markers but no on_cancel cannot be cancelled. `grace`: irreversible work waits grace_s() seconds
    in the queue first (the window to take the click back); a number: that many seconds (auto-buy's countdown). `quiet`: no notice when it is done – the page that
    started it shows the result itself (a failure still gets one)."""
    if lane not in LANES:
        raise ValueError(lane)
    if reuse_queued:
        for j in _active:
            if j.kind == kind and j.status == "queued":
                return j
    keys = list(keys or ([current] if current else []))
    wait = grace_s() if grace is True else float(grace or 0)
    job = Job(next(_ids), kind, label, lane=lane, total=total, current=current, keys=keys, on_cancel=on_cancel,
              cancellable=on_cancel is not None or not keys, quiet=quiet, link=link,
              runs_at=utcnow() + timedelta(seconds=wait) if wait else None, irreversible=bool(grace))
    _active.append(job)
    job.task = asyncio.create_task(_run(job, run))
    return job


def cancel_key(key: str) -> bool:
    """Take back the queued job working on `key` ("purchase:12"); False when there is none (it runs already)."""
    job = next((j for j in _active if j.current == key and j.status == "queued"), None)
    return cancel(job.id) if job is not None else False


def cancel(job_id: int) -> bool:
    """Take a queued job back: it never runs, its markers are cleared (on_cancel) and it is listed as cancelled.
    False: unknown, already running or finished, or not cancellable."""
    job = next((j for j in _active if j.id == job_id), None)
    if job is None or job.status != "queued" or not job.cancellable:
        return False
    job.status, job.result = "cancelled", t("Taken back")
    if job.task is not None:
        job.task.cancel()
    if job.on_cancel is not None:
        try:
            job.on_cancel()
        except Exception as e:  # noqa: BLE001 - the markers are the submitter's business; the job is off either way
            log.warning("on_cancel of job %r failed: %s", job.label, e)
    _settle(job)
    return True


def _settle(job: Job) -> None:
    job.finished_at, job.step, job.current = utcnow(), "", None
    if job in _active:
        _active.remove(job)
    if job not in _finished:
        _finished.appendleft(job)


JOB_TIMEOUT_S = 15 * 60     # longest a job may run (a big clean-up or update fits); the lane is freed afterwards


async def _run(job: Job, run: Callable[[Job], Awaitable[str | None]]) -> None:
    from app.platforms import ratelimit
    from app.services import timeline

    try:
        if job.runs_at is not None:       # grace period: cancellable until it ends (task.cancel() lands here)
            await asyncio.sleep(max((job.runs_at - utcnow()).total_seconds(), 0))
        async with _locks.setdefault(job.lane, asyncio.Lock()):
            job.status = "running"
            # one timeline entry per job; Claude calls and willhaben requests inside it are its steps
            async with timeline.run("job", job.label, name=job.kind, **timeline.marker_ids(job.current)) as tl:
                try:
                    async with asyncio.timeout(JOB_TIMEOUT_S) as limit:
                        if job.irreversible:
                            job.result = await run(job) or ""
                        else:
                            with ratelimit.waiting(_budget_pause(job, limit)):
                                job.result = await run(job) or ""
                    job.status = "done"
                except TimeoutError:          # a stuck request (e.g. a willhaben page that never answers) frees the lane
                    log.warning("job %r timed out after %d s", job.label, JOB_TIMEOUT_S)
                    job.status, job.result = "failed", t("Took longer than {n} min – stopped", n=JOB_TIMEOUT_S // 60)
                except Exception as e:  # noqa: BLE001 - nobody awaits the task: the error goes to the header
                    log.warning("job %r failed: %s", job.label, e)
                    job.status, job.result = "failed", str(e)
                tl.finish(job.status == "done", job.result if job.status == "failed" else None,
                          payload={k: v for k, v in {"result": job.result, "problems": job.problems, "done": job.done,
                                                     "total": job.total, "lane": job.lane}.items() if v})
    except asyncio.CancelledError:        # cancel() while queued (or the loop shutting down): it never ran
        if job.status == "queued":
            job.status, job.result = "cancelled", t("Taken back")
    finally:
        _settle(job)


def budget_wait(job: Job) -> BudgetWait:
    """For ratelimit.waiting(): while a request of the job waits for a site's hourly budget, the header says until
    when (the step shown before comes back afterwards, unless the job moved on meanwhile)."""
    from app.platforms.ratelimit import BudgetWait

    before: str | None = None
    shown = ""

    def changed(bw: BudgetWait) -> None:
        nonlocal before, shown
        if not bw.sites:
            if before is not None and job.step == shown:
                job.step = before
            before = None
            return
        if before is None:
            before = job.step
        job.step = shown = "; ".join(t("{site}: hourly request limit reached – goes on at {time}", site=name,
                                       time=at.strftime("%H:%M")) for name, at in bw.free_at().items())

    return BudgetWait(changed)


def _budget_pause(job: Job, limit: asyncio.Timeout) -> BudgetWait:
    """budget_wait(job), and the job's deadline moves by the time waited (overlapping waits of parallel requests
    count once)."""
    bw = budget_wait(job)
    note = bw.on_change
    covered = 0.0           # loop time up to which waiting has already moved the deadline

    def changed(bw: BudgetWait) -> None:
        nonlocal covered
        if bw.sites:
            loop = asyncio.get_running_loop()
            until = loop.time() + max(bw.sites.values()) - time.monotonic()
            if until > covered and (when := limit.when()) is not None:
                limit.reschedule(when + until - max(covered, loop.time()))
                covered = until
        if note:
            note(bw)

    bw.on_change = changed
    return bw


def working() -> set[str]:
    """Marker keys of the items running jobs work on right now (pages show a spinner instead of an hourglass)."""
    return {j.current for j in _active if j.status == "running" and j.current}


def current_keys(prefix: str) -> list[str]:
    """Marker keys of queued or running jobs starting with `prefix` (pages wait for them like for DB markers)."""
    return [j.current for j in _active if j.current and j.current.startswith(prefix)]


def busy(kind_prefix: str = "") -> bool:
    return any(j.kind.startswith(kind_prefix) for j in _active)


def snapshot() -> dict[str, Any]:
    now = utcnow()
    recent = [j for j in _finished if j.finished_at and (now - j.finished_at).total_seconds() < SHOW_FINISHED_S]
    active, done = [j.view() for j in _active], [j.view() for j in recent]
    for fn in _sources:
        try:
            more = fn()
        except Exception as e:  # noqa: BLE001 - a broken source must not take the header down
            log.warning("job source %r failed: %s", getattr(fn, "__name__", fn), e)
            continue
        active += more.get("active") or []
        done += more.get("recent") or []
    done.sort(key=lambda v: v.get("finished_at") or "", reverse=True)
    return {"active": active, "recent": done[:KEEP_FINISHED]}
