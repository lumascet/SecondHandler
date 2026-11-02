"""The VPN search (app/platforms/vpn.py): finds, in the background, the server(s) every site with its VPN switch on
answers through, and switches over once it found better ones.

Goal: one EU server every site answers (each site with its VPN switch on, and Geizhals when its switch is on); when
none does, two that share the sites, each site on one that answers it (vpn.MAX_LIVE); when not even two cover them
all, the pair that covers the most, and the rest pause with a note until the next search (_retry()).

Triggers: the schedule (every `vpn_shuffle_h` hours after the last search, default 6), "Search now" and new configs
are regular searches: they look for a server other than the one in use, so the server changes now and then. A switch
turned on (is the new site answered by the server in use?) and a site that refuses its server or gets no answer
(Tunnel.failed(), or a request that finds no server) start one too; the automatic ones are guarded against storms:
AUTO_GAP_S after the last search and AUTO_PER_HOUR an hour (one that may not run yet waits: `pending`, started by
tick(), as is one a restart cut off), and a site that started one since the last regular search waits RETRY_S (growing
each time) before its next one. A site still waiting at the end (no server answered it) gets the next search the same
way (tick(), _retry()), not only the regular one hours later. One search at a time; a site switched on or off
meanwhile joins or leaves the running one.

In the background: the search tests servers on a tunnel of its own (never a live server: one key twice on one server
knocks both off) while the live connections keep serving every site that has a server. A site whose server refused
it waits (VpnWait, no agent error) until the search finds it one; the others go on. Each better result goes live at
once (vpn.switch): a server found for the waiting sites becomes the second connection (the search's process is taken
over), and the search goes on for a single server; once it finds one, every site moves to it.

What it knows (the Board): per server the sites that answered through it in this search, or that use it live, and
the ones that refused it (in this search, or a mark that has not expired). The best plan covers the most sites; then
it has fewer servers; in a regular search it is a single server other than the one(s) in use (the change); then it
keeps servers in use; then nearer and earlier ones. A plan never takes the server away from a site that has one.

Fail fast: a site is asked through a server only while its answer could still make the best plan better (every site
not asked there answering, against none answering): the sites without a server first, then the ones that refused most
in this search and lately. A server the pickiest site refuses costs one request; one marked for a site whose answer
is needed costs none (it is skipped without starting it). Live servers are asked first, without a new process: a site
switched on is tested on the server in use before another server starts.

Order: the EU servers, nearest first, random within a tier (vpn.order()), the ones a waiting site answered through
lately first; never one outside the EU. All of them, no cap: the search ends with one server for every site, or when
no server is left that could make the result better.

In parallel: several servers start at once (vpn.SEARCH_TUNNELS; no site request, a dead one fails within seconds by
its missing handshake and is marked for a day) and ASK_PARALLEL of them are asked at once, each site through one
server at a time (its limiter paces it anyway, and the live requests never queue behind several tests). A test
request gives up after TRIAL_TIMEOUT_S. Once a server can no longer help (Board.could: neither as the one server nor
as the partner a site without a server needs), its slow asks stop (not sent yet, or eBay's warm-up) and its tunnel
goes; a single request under way finishes and counts, so a server one site refused still records the others' answers
and can become the partner of a pair.

Careful with failures (_Walk): what fails on the search's tunnels stays with the search; a dead server (no handshake,
does not start) is marked for a day only at the end, once it failed again with no other search tunnel up and only
while the network works (confirm_dead(), at most MAX_DEAD_MARKS a search), and a site's host that a tunnel cannot
reach counts only for that site. The breaker: three tunnels in a row (or half of the last ones) without a handshake.
When tunnels came up lately the network works, and a live server that does not get through is dead (its sites move);
when none did and the live server does not get through either (or the one in use before, when a request dropped
it), the network is down (_Offline: nothing marked, again after OFFLINE_RETRY_S). Otherwise serial mode: one tunnel
at a time, the failed ones asked again. A walk that ends with a run of tunnels without a handshake pauses no site
for good: the search runs again later. Servers marked only for not answering are tried when nothing else is left
(pick()'s old fallback). An error (nothing sent) is asked once more, not taken as an answer. One key on several
servers at once works (see app/platforms/vpn.py).

Pacing: every test request goes through the site's own client and limiter, so it counts towards the site's hourly
budget. The search sends a site at most a quarter of that budget an hour (NO_BUDGET_PER_HOUR without one), never past
BUDGET_FULL of it with the agents' requests, and nothing during a pause after a block: it waits for the site instead of
skipping it, and the page says for which site and why. Only a site refused GIVE_UP_REFUSALS times in this search whose
wait would be longer than GIVE_UP_WAIT_S leaves it (`out`; one that blocks the VPN's servers would otherwise hold the
search, and the other sites' searches, for hours): it waits for the next search. The sites' own failover
(vpn.failover()) goes on while a search runs. eBay's warm-up (vpn_test_requests: at most 3 requests per
server), Kleinanzeigen's credential errors (NotTheExit) and a site's own errors (5xx, a JSON error) are answers, not
refusals; bot-check pages through BOTCHECK_STREAK servers in a row are the site's change, not the servers' (`odd`).

A dedicated profile is never swapped: it is only tested, every site through it; the refusing ones pause.

State in memory while it runs (the page polls it) and in the setting `vpn_test` (the last search) for reloads and
restarts; the connections themselves in vpn's `vpn_conn`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, NamedTuple

from app import platforms
from app.db import get_setting, set_setting
from app.i18n import lang, t
from app.platforms import vpn
from app.platforms.base import Blocked, NotTheExit, Platform, PlatformError
from app.platforms.httpclient import ExitMoved, Trial, trial
from app.platforms.ratelimit import RateLimiter
from app.services import jobs
from app.willhaben import replay

log = logging.getLogger(__name__)

KEYWORD = "fahrrad"       # every site answers it; an empty result list counts as an answer too
SHARE = 4                 # the search sends a site at most 1/SHARE of its hourly budget an hour
NO_BUDGET_PER_HOUR = 60   # ... and a site without a budget (Geizhals) at most this many
BUDGET_FULL = 0.9         # no test request that could take a site past this share of its hourly budget
WAIT_STEP_S = 30          # a wait for a site's budget looks again this often (the agents' requests change it)
GIVE_UP_WAIT_S = 300      # a site refused GIVE_UP_REFUSALS times in this search whose budget makes it wait longer
GIVE_UP_REFUSALS = 5      # ... leaves it (refused nearly everywhere, e.g. Facebook): the others are not held up
BOTCHECK_STREAK = 3       # bot-check pages through this many servers in a row: the site changed, not the servers
AUTO_GAP_S = 300          # an automatic search starts at least this long after the end of the last one
AUTO_PER_HOUR = 3         # and at most this many an hour
RETRY_S = (900, 1800, 3600, 7200)   # a site's next automatic search after its last one: the 2nd, 3rd, 4th, ... time
TRIAL_BLOCKED_TTL_S = 6 * 3600      # a 403 in a search's test marks the server for the site this long (eBay refuses
                                    # at random; a live refusal that counted (vpn.CONFIRM) keeps vpn.MARK_TTL_S)
INTERVAL_H = 6            # default of the setting vpn_shuffle_h
INTERVAL_KEY = "vpn_shuffle_h"
STOP_WAIT_S = 10          # cancel(): longest wait for the search to end
STATE_KEY = "vpn_test"
RETEST_KEY = "vpn_retest"   # the last "Test again": per site, the server it used and what it answered
REGULAR = ("schedule", "manual", "new")         # they look for another server: the change now and then
ANSWERED = ("ok", "site_error", "odd_page")    # an "error" (nothing sent) says nothing: asked once more
REFUSED = ("blocked", "rate", "unreachable", "botcheck")
SHOW_SERVERS = 6          # servers the live panel lists (the newest)
ASK_PARALLEL = 3          # servers asked at the same time (each site asks through one at a time)
MAX_DEAD_MARKS = 10       # dead servers one search marks at most (each confirmed on its own first)
OUTAGE_WINDOW = 8         # the last tunnel starts the breaker looks at: one came up, so the network works
OFFLINE_AFTER = 20        # without a live server: this many tunnels without a handshake and none up: no network
OFFLINE_RETRY_S = 300     # the network seemed down: the search runs again this much later

_clock: Callable[[], float] = time.monotonic          # tests move the hour on
_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep


@dataclass
class Answer:
    """One site through one server."""
    verdict: str = "waiting"   # waiting | asking | ANSWERED | REFUSED | error | off | skipped | moved
    detail: str = ""           # "HTTP 403", the network error, the site's message
    requests: int = 0
    secs: float | None = None
    params: dict[str, Any] = field(default_factory=dict)   # site_error: fix; odd_page: n


@dataclass
class Server:
    name: str
    status: str = "starting"   # starting | testing | done
    live: bool = False         # asked through a live connection (no process of its own)
    problem: str = ""          # dead (no handshake) | broken (does not start): no site was asked
    detail: str = ""
    answers: dict[str, Answer] = field(default_factory=dict)
    secs: float | None = None
    errors: dict[str, int] = field(default_factory=dict)   # asks of a site that sent nothing (asked once more)
    confirmed: bool = False    # its problem showed again on its own: marked for a day

    def refused(self) -> list[str]:
        return [pid for pid, a in self.answers.items() if a.verdict in REFUSED]

    def answered(self) -> list[str]:
        return [pid for pid, a in self.answers.items() if a.verdict in ANSWERED]


@dataclass
class Site:
    pid: str
    name: str
    requests: int = 0          # sent by this search


@dataclass
class Search:
    trigger: str               # schedule | manual | new | switch | auto
    sites: list[Site]
    by: str = ""               # auto, switch: the site that refused its server, found none or was switched on
    why: str = ""              # blocked | rate | unreachable | broken | botcheck | none | on
    detail: str = ""
    profile: bool = False      # a dedicated profile: tested, never swapped
    before: list[str] = field(default_factory=list)       # the live servers at the start
    total: int = 0             # EU servers of the collection
    skipped: int = 0           # servers not started: marks say they cannot make the result better
    servers: list[Server] = field(default_factory=list)
    best: dict[str, Any] = field(default_factory=dict)    # the best plan so far: {servers, assign}
    plan: dict[str, Any] = field(default_factory=dict)    # what it left live: {servers, assign, uncovered}
    switched: int = 0          # switch-overs it made
    odd: list[str] = field(default_factory=list)          # sites whose bot-check pages are the site's change
    mode: str = "parallel"     # parallel | serial (tunnels without a handshake: one at a time)
    status: str = "running"    # running | done | failed | stopped | interrupted
    reason: str = ""           # failed: no_eu | none | profile | offline; stopped: stopped | uploaded | removed | ...
    started: float = field(default_factory=time.time)
    finished: float | None = None
    wait: dict[str, Any] = field(default_factory=dict, compare=False)   # what it waits for now (not saved)
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    discarded: bool = field(default=False, repr=False, compare=False)    # its configs are gone: never saved again
    streak: dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    targets: dict[str, Target] = field(default_factory=dict, repr=False, compare=False)
    closing: bool = field(default=False, repr=False, compare=False)    # past its loop: takes in no new refusal
    out: list[str] = field(default_factory=list)          # sites that left it: their budget would hold it up

    def site(self, pid: str) -> Site:
        return next(s for s in self.sites if s.pid == pid)

    def checked(self) -> int:
        """Servers it is done with: asked (or found dead) on its tunnels, or skipped."""
        return sum(not s.live and s.status == "done" for s in self.servers) + self.skipped


@dataclass
class Target:
    """What a search asks: a site's search, or Geizhals' (not a platform, its own switch)."""
    pid: str
    name: str
    host: str                  # probed when a server starts
    limiter: RateLimiter
    cost: int                  # requests one ask may send (eBay: its warm-up)
    ask: Callable[[], Awaitable[Any]]

    def on(self) -> bool:
        return vpn.enabled(self.pid) and (self.pid == vpn.GEIZHALS or platforms.enabled(self.pid))


_run: Search | None = None
_spent: dict[str, deque[float]] = {}     # per site the search's requests of the last hour (_clock), across searches


# ------------------------------------------------------------------ who, when, guards
def targets() -> list[Target]:
    """The sites switched on under Platforms (willhaben always is) with their VPN switch on, then Geizhals."""
    out = [Target(p.id, p.name, p.api_host or p.host, p.limiter, p.vpn_test_requests, _searcher(p))
           for p in platforms.all() if vpn.enabled(p.id)]
    if vpn.enabled(vpn.GEIZHALS):
        from app.services import geizhals

        out.append(Target(vpn.GEIZHALS, "Geizhals", "geizhals.at", geizhals._limiter, 1,
                          lambda: geizhals.search(KEYWORD, limit=1)))
    return out


def _searcher(p: Platform) -> Callable[[], Awaitable[Any]]:
    return lambda: p.search(p.keyword_search(KEYWORD), rows=1)


def blocker() -> str | None:
    """Why no search can start now; None when one can."""
    if replay.enabled():
        return t("Not available in the offline replay: the test needs real requests")
    if not vpn.source()[1]:
        return t("Upload WireGuard configs first")
    if vpn.runner.binary() is None:
        return t("wireproxy is not installed")
    if not targets():
        return t("Switch the VPN on for a site first")
    return None


def interval_h() -> int:
    try:
        h = int(get_setting(INTERVAL_KEY) or INTERVAL_H)
    except ValueError:
        return INTERVAL_H
    return min(max(h, 1), 168)


def set_interval(h: int) -> None:
    set_setting(INTERVAL_KEY, str(min(max(int(h), 1), 168)))


def pending(st: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A search waiting for its turn: {at, trigger, by, why, detail} (an older state: [at, pid, reason, detail])."""
    p = (vpn.conn_state() if st is None else st).get("pending")
    if isinstance(p, list) and len(p) == 4:
        return {"at": float(p[0]), "trigger": "auto", "by": str(p[1]), "why": str(p[2]), "detail": str(p[3])}
    if isinstance(p, dict) and isinstance(p.get("at"), (int, float)):
        return {"at": float(p["at"]), "trigger": str(p.get("trigger") or "auto"), "by": str(p.get("by") or ""),
                "why": str(p.get("why") or ""), "detail": str(p.get("detail") or "")}
    return None


def next_at() -> float | None:
    """When the next search starts: a pending one, else a waiting site's (_retry) or the regular one, whichever comes
    first; None when none is due yet (never searched: the next tick)."""
    st = vpn.conn_state()
    if p := pending(st):
        return p["at"]
    regular = float(st["shuffled"]) + interval_h() * 3600 if st.get("shuffled") else None
    if (r := _retry(st, time.time())) is not None and (regular is None or r[0] < regular):
        return r[0]
    return regular


def _triggered(st: dict[str, Any]) -> dict[str, tuple[float, int]]:
    """The sites that started an automatic search since the last regular one: {pid: (when, how many times)} (an older
    state holds only the time)."""
    out: dict[str, tuple[float, int]] = {}
    for pid, v in (st.get("triggered") or {}).items():
        if isinstance(v, (int, float)):
            out[pid] = (float(v), 1)
        elif isinstance(v, list) and len(v) == 2 and isinstance(v[0], (int, float)) and isinstance(v[1], int):
            out[pid] = (float(v[0]), max(v[1], 1))
    return out


def _again_at(st: dict[str, Any], pid: str) -> float:
    """When this site may start an automatic search again: at once the first time, then RETRY_S after its last one."""
    if (last := _triggered(st).get(pid)) is None:
        return 0.0
    when, n = last
    return when + RETRY_S[min(n, len(RETRY_S)) - 1]


def _retry(st: dict[str, Any], now: float) -> tuple[float, str, str, str] | None:
    """The next search for a site that still waits (it refused its server, or no server answered it): (when, pid,
    why, detail) of the earliest one, at its back-off (_again_at) and the storm guards (_auto_at)."""
    on = {pid for pid, _ in vpn.users()}
    out = None
    for pid, p in (st.get("paused") or {}).items():
        if pid not in on or not isinstance(p, list) or len(p) != 2:
            continue
        at = max(_again_at(st, pid), _auto_at(st, now))
        if out is None or at < out[0]:
            out = (at, pid, str(p[0]), str(p[1]))
    return out


def _auto_at(st: dict[str, Any], now: float) -> float:
    """The earliest time an automatic search may start: AUTO_GAP_S after the last one ended, and no more than
    AUTO_PER_HOUR in an hour."""
    at = float(st.get("shuffled") or 0) + AUTO_GAP_S
    recent = sorted(float(x) for x in st.get("auto") or [] if now - float(x) < 3600)
    if len(recent) >= AUTO_PER_HOUR:
        at = max(at, recent[-AUTO_PER_HOUR] + 3600)
    return at


def running() -> bool:
    return _run is not None and _run.task is not None and not _run.task.done()


def trigger(pid: str, reason: str, detail: str = "") -> bool:
    """A site refused its server (reason blocked | rate | unreachable | broken | botcheck) or found none (none): a
    search now, or once the guards allow it (pending, started by tick()): AUTO_GAP_S, AUTO_PER_HOUR, and for a site
    that started one since the last regular search its back-off (RETRY_S). True when one runs or comes; False when
    none can run."""
    if running() and not _run.closing:
        return True                              # the running search takes it in
    if blocker():
        return False
    st, now = vpn.conn_state(), time.time()
    at = _auto_at(st, now)
    if reason != "none" and (again := _again_at(st, pid)) > now:
        log.info("VPN: %s %s again; its next search in %.0f s", pid, reason, again - now)
        at = max(at, again)
    if running():                                # one is finishing and takes in nothing new: the next one comes
        at = max(at, now + AUTO_GAP_S)
    if at > now:
        p = pending(st)
        if p is None or at < p["at"]:            # the earlier one: a search takes in every waiting site
            vpn.update_conn(pending={"at": at, "trigger": "auto", "by": pid, "why": reason, "detail": detail})
            log.info("VPN: %s %s, the next search may start in %.0f s", pid, reason, at - now)
        return True
    return start("auto", by=pid, why=reason, detail=detail) is None


def site_on(pid: str) -> None:
    """A VPN switch went on: a running search takes the site in; else one starts, which asks it through the server
    in use first."""
    if not running() and not blocker():
        start("switch", by=pid, why="on")


def _cut_off() -> dict[str, Any] | None:
    """The stored search when the app stopped hard while it ran (no shutdown to note it as pending)."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("status") == "running" and isinstance(d.get("trigger"), str) else None


async def tick() -> None:
    """Scheduler, every minute: a pending search whose turn came, one the app's end cut off, else the regular one
    when it is due."""
    if running() or blocker():
        return
    st, now = vpn.conn_state(), time.time()
    if (p := pending(st)) is not None:
        if p["at"] <= now:
            start(p["trigger"], by=p["by"], why=p["why"], detail=p["detail"])
        return
    if (d := _cut_off()) is not None:
        start(d["trigger"], by=str(d.get("by") or ""), why=str(d.get("why") or ""), detail=str(d.get("detail") or ""))
        return
    if now >= float(st.get("shuffled") or 0) + interval_h() * 3600:
        start("schedule")
        return
    if (r := _retry(st, now)) is not None and r[0] <= now:     # a site still waits: its next search came
        start("auto", by=r[1], why=r[2], detail=r[3])


# ------------------------------------------------------------------ starting and stopping
def start(trigger: str = "manual", *, by: str = "", why: str = "", detail: str = "") -> str | None:
    """Search in the background (the header lists the job); None when it started, else why not. No await before the
    task exists: two clicks, two tabs or two refusals cannot start two."""
    global _run
    if running():
        return t("A VPN search is running")
    if reason := blocker():
        return reason
    tgs = targets()
    st, now = vpn.conn_state(), time.time()
    before = vpn.live_servers(st) or [s for s in st.get("dropped") or [] if isinstance(s, str) and vpn.allowed(s)]
    auto = [x for x in st.get("auto") or [] if now - float(x) < 3600] + ([now] if trigger == "auto" else [])
    triggered = {} if trigger in REGULAR else dict(st.get("triggered") or {})
    if trigger == "auto" and by:
        triggered[by] = [now, (_triggered(st).get(by) or (0.0, 0))[1] + 1]
    vpn.update_conn(pending=None, auto=auto, triggered=triggered)
    _run = Search(trigger, [Site(tg.pid, tg.name) for tg in tgs], by=by, why=why, detail=detail,
                  profile=vpn.source()[0] == "profile", before=before)
    _save(_run)
    _run.task = asyncio.create_task(_main(_run))
    return None


async def cancel(reason: str) -> None:
    """Stop, new or removed configs, app shutdown: cancel now and wait for the search to end (≤ STOP_WAIT_S). The
    live connections stay as the search left them."""
    if not running():
        return
    run = _run
    run.reason = reason
    run.task.cancel()
    await asyncio.wait({run.task}, timeout=STOP_WAIT_S)


async def source_changed(reason: str) -> None:
    """Before vpn.source_changed(): cancel a running search (its servers are about to go) and forget the last one
    (it names servers of the old configs), also when the cancel fails."""
    try:
        await cancel(reason)
    finally:
        clear()


async def shutdown() -> None:
    await cancel("interrupted")


def clear() -> None:
    """Forget the last search. One still winding down after cancel() saves nothing when it ends."""
    global _run
    if _run is not None:
        _run.discarded = True
        if not running():
            _run = None
    set_setting(STATE_KEY, "")


def reset() -> None:
    """Tests: every test has its own event loop and database."""
    global _run, _retest
    _run = _retest = None
    _spent.clear()


# ------------------------------------------------------------------ "Test again": the connections in use, nothing else
_retest: asyncio.Task | None = None


def retesting() -> bool:
    return _retest is not None and not _retest.done()


def retest() -> str | None:
    """"Test again": every site with its VPN on (Geizhals too) asks once through the server it uses now, as its own
    visitor and judged like a search's ask; no other server starts, nothing switches, no refusal is marked (an
    answer is: it keeps the server on the site's standby list, vpn.standby()). In the
    background (the header lists the job); None when it started, else why not."""
    global _retest
    if running():
        return t("A VPN search is running")
    if retesting():
        return t("A test is running")
    if reason := blocker():
        return reason
    if not vpn.live_servers():
        return t("No server yet: the first search finds one.")
    _retest = asyncio.create_task(_retest_all())
    return None


async def _retest_all() -> None:
    tgs = [tg for tg in targets() if tg.on()]
    sh = Search("retest", [Site(tg.pid, tg.name) for tg in tgs])    # only the bookkeeping _ask keeps
    results: dict[str, dict[str, Any]] = {}
    async with jobs.track("vpn-retest", t("VPN test"), lane="vpn", link="/settings/vpn", quiet=True) as job:
        job.total = len(tgs)

        async def one(tg: Target) -> None:
            name = vpn.server_for(tg.pid)
            if name is None:                             # it refused its server and waits for the next search
                results[tg.pid] = {"server": vpn.waits_on(tg.pid) or "", "verdict": "waits", "detail": ""}
                return
            route = vpn.conn.route(name)
            if route is None:                            # its process is not up (a restart): this server only
                try:
                    route = await vpn.conn.start_live(name, tg.host)
                except (OSError, vpn.Unreachable) as e:
                    results[tg.pid] = {"server": name, "verdict": "unreachable", "detail": _short(e)}
                    return
            if route is None:
                results[tg.pid] = {"server": name, "verdict": "unreachable", "detail": ""}
                return
            a = await _ask(sh, route, tg)
            if a.verdict in ("ok", "site_error"):        # it answers there: kept on the site's standby list
                vpn.mark(tg.pid, route.name, "ok")
            results[tg.pid] = {"server": route.name, "verdict": a.verdict, "detail": a.detail, "secs": a.secs}
            job.progress(len(results), t("{site} through {server}", site=tg.name, server=route.name))

        await asyncio.gather(*(one(tg) for tg in tgs))
        ok = sum(r["verdict"] in ANSWERED for r in results.values())
        job.result = t("{ok} of {n} sites answer through their server", ok=ok, n=len(results))
        if ok < len(results):
            job.quiet = False
    set_setting(RETEST_KEY, json.dumps({"at": time.time(), "results": results}, separators=(",", ":")))


def _retest_view(names: dict[str, str]) -> dict[str, Any] | None:
    """The last "Test again" for the page: when, and per site its server and answer (a chip, the reason below)."""
    try:
        d = json.loads(get_setting(RETEST_KEY) or "null")
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("results"), dict):
        return None
    rows = []
    for pid, r in d["results"].items():
        v = str(r.get("verdict") or "")
        label, tone = (t("Waits"), "warn") if v == "waits" else _pill(v)
        rows.append({"name": names.get(pid, pid), "server": r.get("server") or "", "label": label, "tone": tone,
                     "detail": vpn.describe(r.get("detail") or "") if v not in ANSWERED else ""})
    return {"at": _dt(float(d.get("at") or 0)), "rows": rows}


# ------------------------------------------------------------------ what the search knows, and the best plan
class Plan(NamedTuple):
    servers: tuple[str, ...]   # primary first
    covered: int               # bitmask of the sites
    key: tuple[int, ...]       # (sites covered, -servers, the change, servers kept, -rank): larger is better


SOFT = ("unreachable", "dead", "broken")    # marks of servers that did not answer at all (not a site's refusal)


class Board:
    """What a search knows now, as bitmasks over the sites (see the module docstring)."""

    def __init__(self, sh: Search, tgs: list[Target]) -> None:
        st = vpn.conn_state()
        self.pids = [tg.pid for tg in tgs]
        self.cost = {tg.pid: tg.cost for tg in tgs}
        self.bit = {pid: 1 << i for i, pid in enumerate(self.pids)}
        self.full = (1 << len(self.pids)) - 1
        self.live = vpn.live_servers(st)
        self.most = vpn.MAX_LIVE                   # servers a plan may have
        self.before = set(sh.before)
        self.regular = sh.trigger in REGULAR
        self.odd = self.mask(sh.odd)
        self.next_rank = 10 + len(sh.servers)
        self.rank = {s: i for i, s in enumerate(self.live)}
        self.ok: dict[str, int] = {}
        self.no: dict[str, int] = {}
        self.soft: dict[str, int] = {}             # refusals that are only "did not answer" marks (the fallback)
        self.asked: dict[str, int] = {}
        assign, paused = st.get("assign") or {}, st.get("paused") or {}
        self.using = {s: self.mask(p for p in self.pids if assign.get(p) == s and p not in paused) for s in self.live}
        for s in self.live:          # a site on a live server without trouble answers through it
            self.ok[s] = self.using[s]
        self.refusals = dict.fromkeys(self.pids, 0)
        for i, srv in enumerate(sh.servers):
            self.rank.setdefault(srv.name, 10 + i)
            if srv.problem:
                self.no[srv.name] = self.full
                continue
            for pid, a in srv.answers.items():
                if pid not in self.bit:
                    continue
                if a.verdict in ANSWERED:
                    self._add(self.ok, srv.name, pid)
                elif a.verdict in REFUSED:
                    self._add(self.no, srv.name, pid)
                    self.refusals[pid] += 1
                elif not (a.verdict == "error" and srv.errors.get(pid, 0) >= 2):
                    continue                       # not asked to the end: unknown (an error is asked once more)
                self._add(self.asked, srv.name, pid)
        self.marks = {pid: vpn.marks(pid) for pid in self.pids}
        for pid, m in self.marks.items():
            for name, (_until, reason) in m.items():
                self._add(self.no, name, pid)
                if reason in SOFT:
                    self._add(self.soft, name, pid)
        for s, m in self.ok.items():                   # an answer of this search beats an older mark
            self.no[s] = self.no.get(s, 0) & ~m
        self._memo: dict[tuple[int, int, bool, bool, int], bool] = {}
        self.must = self._best({s: self.ok.get(s, 0) for s in self.live}, 0).covered   # never taken away
        self._plan_now: Plan | None = None

    def mask(self, pids: Any) -> int:
        m = 0
        for pid in pids:
            m |= self.bit.get(pid, 0)
        return m

    def _add(self, d: dict[str, int], s: str, pid: str) -> None:
        d[s] = d.get(s, 0) | self.bit[pid]

    def names(self, m: int) -> list[str]:
        return [pid for pid in self.pids if m & self.bit[pid]]

    def unknown(self, s: str, soft: bool = False) -> int:
        """Sites not asked through `s` whose answer there is not known either; `soft`: as if no "did not answer"
        mark existed (vpn.order()'s fallback when nothing else is left)."""
        no = self.no.get(s, 0) & ~(self.soft.get(s, 0) if soft else 0)
        return self.full & ~(self.ok.get(s, 0) | no | self.asked.get(s, 0) | self.odd)

    def _best(self, ok: dict[str, int], must: int) -> Plan:
        """The best plan over `ok` (server -> sites answering) that covers `must`: one server or two."""
        reps: dict[tuple[int, bool, bool], str] = {}
        for s, m in ok.items():
            m = (m | self.odd) & self.full
            if not m:
                continue
            key = (m, s in self.live, s in self.before)       # one of each kind is enough
            if key not in reps or self.rank.get(s, self.next_rank) < self.rank.get(reps[key], self.next_rank):
                reps[key] = s
        best = Plan((), 0, (0, 0, 0, 0, 0))
        found = not must
        servers = list(reps.values())
        cover = {s: (ok[s] | self.odd) & self.full for s in servers}
        for i, a in enumerate(servers):
            for b in [None, *(servers[i + 1:] if self.most > 1 else [])]:
                cov = cover[a] | (cover[b] if b else 0)
                if cov & must != must:
                    continue
                plan = self._plan(a, b, cover, cov)
                if not found or plan.key > best.key:
                    best, found = plan, True
        return best

    def _plan(self, a: str, b: str | None, cover: dict[str, int], cov: int) -> Plan:
        servers = [a] if b is None else sorted(
            (a, b), key=lambda s: (-bin(cover[s]).count("1"), s not in self.live, self.rank.get(s, self.next_rank)))
        change = int(self.regular and b is None and cov == self.full and a not in self.before)
        kept = sum(s in self.live for s in servers)
        rank = sum(self.rank.get(s, self.next_rank) for s in servers)
        return Plan(tuple(servers), cov, (bin(cov).count("1"), -len(servers), change, kept, -rank))

    def current(self) -> Plan:
        """The connections as they are: the live servers and the sites that use them now."""
        cov = 0
        for m in self.using.values():
            cov |= m
        if not self.live:
            return Plan((), 0, (0, 0, 0, 0, 0))
        return self._plan(self.live[0], self.live[1] if len(self.live) > 1 else None,
                          {s: self.using[s] | self.odd & self.full for s in self.live}, cov | (self.odd & self.full))

    def best(self, only_live: bool = False) -> Plan:
        if only_live:
            return self._best({s: self.ok.get(s, 0) for s in self.live}, 0)
        if self._plan_now is None:
            self._plan_now = self._best(self.ok, self.must)
        return self._plan_now

    def could(self, s: str, soft: bool = False) -> bool:
        """Could answers through `s` still make the best plan better (every site not asked there answering)?"""
        unk = self.unknown(s, soft)
        if not unk:
            return False
        memo = (self.ok.get(s, 0), unk, s in self.before, s in self.live, self.rank.get(s, self.next_rank))
        if memo not in self._memo:
            pess = self._best(self.ok, self.must) if s in self.ok else self._best({**self.ok, s: 0}, self.must)
            opt = self._best({**self.ok, s: self.ok.get(s, 0) | unk}, self.must)
            self._memo[memo] = opt.key > pess.key
        return self._memo[memo]

    def done(self) -> bool:
        """One server for every site (in a regular search one that was not in use: the change)."""
        p = self.best()
        return p.covered == self.full and len(p.servers) == 1 and (not self.regular or p.servers[0] not in self.before)

    def pickiest(self, m: int) -> list[str]:
        """The sites of `m`, the one that refused most first (in this search, then lately), a slow one (eBay's
        warm-up) last among equals: fail fast."""
        return sorted(self.names(m), key=lambda pid: (-self.refusals[pid], -len(self.marks[pid]), self.cost[pid],
                                                      self.pids.index(pid)))

    def assign(self, plan: Plan, current: dict[str, str]) -> dict[str, str]:
        """Each covered site on a server of `plan` that answers it: the one it uses when that is one, else the
        primary first."""
        out = {}
        for pid in self.pids:
            can = [s for s in plan.servers if (self.ok.get(s, 0) | self.odd) & self.bit[pid]]
            if can:
                out[pid] = current[pid] if current.get(pid) in can else can[0]
        return out


def _refresh(sh: Search) -> list[Target]:
    """The sites now: one switched on meanwhile joins the search, one switched off leaves it."""
    tgs = [tg for tg in targets() if tg.pid not in sh.out]
    sh.targets = {tg.pid: tg for tg in tgs}
    for tg in tgs:
        if all(s.pid != tg.pid for s in sh.sites):
            sh.sites.append(Site(tg.pid, tg.name))
    return tgs


def _note_best(sh: Search, b: Board) -> None:
    p = b.best()
    sh.best = {"servers": list(p.servers), "assign": b.assign(p, vpn.conn_state().get("assign") or {})}


# ------------------------------------------------------------------ the search
async def _main(sh: Search) -> None:
    global _run
    label = t("VPN test") if sh.profile else t("VPN server search")
    try:
        async with jobs.track("vpn-search", label, lane="vpn", link="/settings/vpn",
                              quiet=sh.trigger != "manual") as job:
            try:
                await (_test_profile(sh, job) if sh.profile else _search(sh, job))
            except asyncio.CancelledError:      # Stop, new configs, shutdown
                sh.status = "interrupted" if sh.reason == "interrupted" else "stopped"
            finally:
                with suppress(Exception):
                    await vpn.conn.stop_search()   # the servers under test; the live ones stay
            job.result = _summary(sh)
            job.problems = len(sh.plan.get("uncovered") or []) + (sh.status == "failed")
            if job.problems:
                job.quiet = False
    except Exception as e:  # noqa: BLE001 - the state must be noted whatever happened
        log.warning("VPN search: %s", e)
        sh.status, sh.reason = "failed", _short(e)
        with suppress(Exception):
            await vpn.conn.stop_search()
    finally:
        sh.finished, sh.wait, sh.closing = time.time(), {}, True
        if sh.status == "running":
            sh.status = "interrupted"
        if not sh.discarded:
            if sh.status in ("done", "failed", "stopped"):
                vpn.update_conn(shuffled=sh.finished)
            elif sh.status == "interrupted":    # a restart cut it off: the first tick runs it again
                vpn.update_conn(pending={"at": sh.finished, "trigger": sh.trigger, "by": sh.by, "why": sh.why,
                                         "detail": sh.detail})
            _save(sh)
        if sh.discarded and _run is sh:
            _run = None


class _Offline(Exception):
    """No tunnel gets a handshake and the live server does not get through either: the home network is down."""


@dataclass(eq=False)
class _Slot:
    """A server the search asks through: one of its tunnels, or a live server (no process of its own)."""
    srv: Server
    route: vpn.Route | None = None
    asks: dict[str, asyncio.Task] = field(default_factory=dict)    # site -> its ask under way
    trials: dict[str, Trial] = field(default_factory=dict)        # site -> its Trial (what it sent so far)
    t0: float = field(default_factory=time.monotonic)
    single: bool = False       # started with no other search tunnel allowed (serial mode)
    soft: bool = False         # tried although marked for not answering lately (nothing else was left)


async def _search(sh: Search, job: jobs.Job) -> None:
    """Until one server serves every site or none is left that could help (the module docstring); each better
    result goes live at once. Without network: stops, writes nothing, tries again later."""
    order = vpn.order()
    sh.total = len(order)
    if not order:
        sh.status, sh.reason = "failed", "no_eu"
        vpn.update_conn(servers=[], assign={}, error=_failed_text(sh))
        await vpn.conn.disconnect()
        return
    w = _Walk(sh, job, order)
    offline = False
    try:
        await w.loop()
    except _Offline:
        offline = True
    finally:
        await w.close()
    if offline:
        _offline(sh, w.gone)
        return
    sh.closing = True                                    # a refusal from now on waits for the next search
    await w.confirm_dead()
    await _finish(sh, retry=w.suspect())


class _Walk:
    """One search's servers under way: the tunnels starting and the servers being asked, and its guards: the breaker
    (tunnels without a handshake: the network, the live server or stale configs?) and the confirmation of dead
    servers before they are marked."""

    def __init__(self, sh: Search, job: jobs.Job, order: list[str]) -> None:
        self.sh, self.job, self.order = sh, job, order
        self.slots: dict[str, _Slot] = {}                    # servers being asked through (or ready for it)
        self.starts: dict[asyncio.Task, _Slot] = {}          # tunnels starting
        self.asks: dict[asyncio.Task, tuple[_Slot, str]] = {}  # asks under way
        self.recent: deque[bool] = deque(maxlen=OUTAGE_WINDOW)   # the last tunnel starts: came up?
        self.streak = 0                  # tunnel starts in a row without a handshake
        self.failed = 0                  # tunnel starts without a handshake in this search
        self.failures: list[tuple[Server, bool]] = []   # (server, failed alone): marked only by confirm_dead()
        self.written: list[tuple[str, str]] = []        # (site, server): the dead marks confirm_dead() wrote
        self.stored = 0
        self.gone: set[str] = set()      # live servers dead_live() found dead: no reference for the network

    # ---- the loop
    async def loop(self) -> None:
        sh = self.sh
        while True:
            if vpn.guard.paused_s() > 0:              # no handshake anywhere lately: stop asking the provider
                log.warning("VPN search: handshakes paused (%d min) – the search stops and runs again after",
                            vpn.guard.paused_s() // 60)
                raise _Offline
            tgs = _refresh(sh)
            if not tgs:
                return
            b = Board(sh, tgs)
            _note_best(sh, b)
            if b.done():
                return
            await self.retire(b)
            self.dispatch(b)
            if self.start_more(b) and not self.starts and not self.asks:
                await asyncio.sleep(0.2)              # only processes still draining fill the room: wait for them
                continue
            if not self.starts and not self.asks:
                return
            self.job.step, self.job.done, self.job.total = _progress(sh), sh.checked(), sh.total
            done, _ = await asyncio.wait({*self.starts, *self.asks}, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=lambda x: x in self.asks):     # tunnels first: their slots in order
                if task in self.starts:
                    await self.started(self.starts.pop(task), task)
                elif task in self.asks:
                    _asked(sh, *self.asks.pop(task), task)
            await _improve(sh)

    async def close(self) -> None:
        """Nothing keeps running: the asks and starts under way stop, the tunnels go."""
        tasks = [*self.starts, *self.asks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task, (slot, pid) in list(self.asks.items()):
            _asked(self.sh, slot, pid, task)
        for slot in [*self.slots.values(), *self.starts.values()]:
            if slot.srv.status != "done":
                slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)
        self.slots, self.starts, self.asks = {}, {}, {}
        self.sh.servers = [s for s in self.sh.servers if s.answers or s.problem]   # up ahead, never needed
        await vpn.conn.stop_search()

    def suspect(self) -> bool:
        """The walk ended with a run of tunnels without a handshake: maybe the network, not the servers."""
        return self.streak >= 3 or len(self.recent) >= 3 and not any(self.recent)

    # ---- asks
    def dispatch(self, b: Board) -> None:
        """Start the asks that may run now: live servers first (no new process), then the search's tunnels in the
        order they started, ASK_PARALLEL of them at a time (one in serial mode); a site asks through one server at a
        time."""
        sh = self.sh
        busy = {pid for _s, pid in self.asks.values()}
        blocked = {pid for pid, tg in sh.targets.items() if _budget(tg)[0]}
        limit = ASK_PARALLEL if sh.mode == "parallel" else 1
        asking = sum(1 for n, s in self.slots.items() if s.asks and n not in b.live and not s.srv.live)
        rank = {s.name: i for i, s in enumerate(sh.servers)}       # the search's order, not the order tunnels came up
        names = [*b.live, *sorted((n for n, s in self.slots.items() if s.route is not None and n not in b.live),
                                  key=lambda n: rank.get(n, 0))]
        for name in names:
            slot = self.slots.get(name)
            if name not in b.live and not slot.asks and asking >= limit:
                continue
            inflight = set(slot.asks) if slot else set()
            waiting = {p for p in inflight if slot.srv.answers.get(p, Answer()).verdict == "waiting"}
            wave = [pid for pid in _wave(b, name, inflight, waiting, blocked, bool(slot and slot.soft))
                    if pid not in busy]
            if not wave:
                continue
            if slot is None:
                srv = Server(name, status="testing", live=True)
                sh.servers.append(srv)
                slot = self.slots[name] = _Slot(srv)
            elif name not in b.live and not slot.asks:
                asking += 1
            for pid in wave:
                task = asyncio.create_task(_ask_slot(sh, slot, sh.targets[pid], self.job))
                self.asks[task], slot.asks[pid] = (slot, pid), task
                busy.add(pid)

    async def retire(self, b: Board) -> None:
        """A server that can no longer help (neither as the one server nor as the partner a site without a server
        needs: Board.could): its slow asks stop (not sent yet, or eBay's warm-up; a single request under way ends
        within TRIAL_TIMEOUT_S and is kept), and its tunnel stops once nothing runs there. One the best plan needs
        stays."""
        keep = set(b.best().servers)
        for name, slot in list(self.slots.items()):
            if slot.route is None and not slot.srv.live or name in keep or b.could(name, slot.soft):
                continue
            for pid, task in slot.asks.items():
                tr = slot.trials.get(pid)
                if tr is None or not tr.wire or tr.limit > 1:
                    task.cancel()
            if slot.asks:
                continue
            del self.slots[name]
            if slot.srv.status != "done":
                slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)
            if not slot.srv.live:
                await vpn.conn.stop_search(name)       # no-op for one that went live meanwhile
                log.info("VPN search: %s (%s)", name,
                         ", ".join(f"{p}: {a.verdict}" for p, a in slot.srv.answers.items()) or "-")

    # ---- tunnels
    def start_more(self, b: Board) -> bool:
        """More servers on the search's tunnels while there is room (vpn.SEARCH_TUNNELS in parallel mode, one in
        serial mode, the processes still draining counted) and few started ones wait to be asked. True when only
        draining processes kept a server that could help from starting."""
        sh = self.sh
        if any(s.srv.live for s, _p in self.asks.values()):
            return False                         # a live server may settle it: no tunnel before its answer
        room = vpn.SEARCH_TUNNELS if sh.mode == "parallel" else 1
        ready = sum(1 for n, s in self.slots.items() if s.route is not None and not s.asks and n not in b.live
                    and not b.asked.get(n, 0))
        while ready + len(self.starts) < 2 * ASK_PARALLEL:
            used = len(vpn.conn.testing()) + len(self.starts) - self._reserved()
            if used + vpn.conn.draining() >= room:
                return used < room and _candidate(sh, b, self.order)[0] is not None
            name, soft = _candidate(sh, b, self.order)
            if name is None:
                return False
            pid = (_wave(b, name, set(), set(), set(), soft) or b.pickiest(b.unknown(name, soft)) or [None])[0]
            slot = _Slot(Server(name), single=sh.mode != "parallel", soft=soft)
            sh.servers.append(slot.srv)
            host = sh.targets[pid].host if pid else "www.willhaben.at"
            self.starts[asyncio.create_task(_start_slot(slot, host, pid))] = slot
        return False

    def _reserved(self) -> int:
        """Starts already counted by vpn.conn.testing() (they reserved their server)."""
        testing = set(vpn.conn.testing())
        return sum(1 for s in self.starts.values() if s.srv.name in testing)

    async def started(self, slot: _Slot, task: asyncio.Task) -> None:
        """A tunnel came up, or did not: the breaker counts."""
        sh, srv = self.sh, slot.srv
        if task.cancelled():
            return
        if (e := task.exception()) is not None:
            srv.problem, srv.detail, srv.status = "broken", _short(e), "done"
            log.warning("VPN search: %s did not start: %s", srv.name, e)
            return
        if slot.route is not None:
            self.slots[srv.name] = slot
            self.streak = 0
            self.recent.append(True)
            return
        if srv.problem not in ("dead", "broken"):
            return                                    # skipped (a process runs there)
        if not slot.single and sh.mode != "parallel":
            sh.servers.remove(srv)                    # started beside others before the breaker: asked again alone
            return
        self.streak, self.failed = self.streak + 1, self.failed + 1
        self.recent.append(False)
        self.failures.append((srv, slot.single))
        half = len(self.recent) == self.recent.maxlen and 2 * self.recent.count(False) >= len(self.recent)
        if self.streak >= 3 or half and self.streak == 1:     # three in a row, or half of the last ones
            await self.breaker()

    async def breaker(self) -> None:
        """Tunnels without a handshake, three in a row or half of the last ones: the network, the live server or
        stale configs? Tunnels came up lately: the network works, so a live server that does not get through is dead
        (dead_live()). None came up: the live server decides (the one in use when the search started, when a request
        dropped it meanwhile): it does not get through either, so the network is down (_Offline); it does, so the
        configs are stale. No server to ask at all: down only after OFFLINE_AFTER failures. Not down: one tunnel at a
        time from now on, the ones that failed beside others asked again. The window keeps the last OUTAGE_WINDOW
        starts: a network that drops after a tunnel came up shows once none of them came up any more."""
        sh = self.sh
        self.streak = 0
        if True in self.recent:
            await self.dead_live()
        else:
            state = await self.live_state()
            if state == "down" or state == "none" and self.failed >= OFFLINE_AFTER:
                log.warning("VPN search: no handshake anywhere and the live server does not get through either: "
                            "the network seems down")
                raise _Offline
            if state == "none":
                return                                # nothing to compare with yet: stale configs or no network
        if sh.mode == "parallel":
            sh.mode = "serial"
            log.info("VPN search: tunnels without a handshake; one at a time from now on")
            again = {s.name for s, alone in self.failures if not alone}
            sh.servers = [s for s in sh.servers if s.name not in again]   # asked again, one at a time
            self.failures = [(s, alone) for s, alone in self.failures if alone]

    async def dead_live(self) -> None:
        """Tunnels come up, so the network works: a live server that does not get through now (asked twice) is dead.
        Its sites are marked (no answer through it, an hour) and lose it: the search finds them a server."""
        st = vpn.conn_state()
        for name in vpn.live_servers(st):
            host = self.host_for(name)
            if vpn.conn.route(name) is None or await vpn.conn.reach(name, host) or await vpn.conn.reach(name, host):
                continue
            log.warning("VPN search: %s in use no longer gets through; its sites move", name)
            for pid in self.sh.targets:
                if vpn.server_for(pid, st) == name:
                    vpn.mark(pid, name, "unreachable")
            self.gone.add(name)
            await vpn.drop(name)

    async def live_state(self) -> str:
        """The servers in use (when a request dropped them meanwhile, the ones in use when the search started, on a
        tunnel of the search): "ok" when one gets through, "down" when none does, "none" without one."""
        servers = [s for s in vpn.live_servers() if s not in self.gone]
        before = [s for s in self.sh.before if s not in servers and s not in self.gone and vpn.allowed(s)] \
            if not servers else []
        if not servers and not before:
            return "none"
        for name in servers:
            host = self.host_for(name)
            if await vpn.conn.reach(name, host):
                return "ok"
            if vpn.conn.route(name) is None:
                with suppress(OSError, vpn.Unreachable, PlatformError):
                    if await vpn.conn.start_live(name, host) is not None:
                        return "ok"
        for name in before:
            if name in vpn.conn.busy():
                continue
            try:
                await vpn.conn.start_search(name, self.host_for(name))
            except (OSError, vpn.Unreachable, RuntimeError, PlatformError):
                continue
            await vpn.conn.stop_search(name)
            return "ok"
        return "down"

    def host_for(self, server: str) -> str:
        st = vpn.conn_state()
        pids = [p for p in self.sh.targets if vpn.server_for(p, st) == server] or list(self.sh.targets)
        return self.sh.targets[pids[0]].host if pids else "www.willhaben.at"

    # ---- dead servers
    async def confirm_dead(self) -> None:
        """The servers without a handshake (or that did not start) are marked dead for a day, MAX_DEAD_MARKS a
        search, once they fail again on their own now (no other search tunnel up; a short blip of the network during
        the walk marks nothing). Only while the network works: checked first and after every mark again; when it
        went down, the marks written here go again."""
        if not self.failures or await self.live_state() != "ok":
            return
        for srv, _alone in self.failures:
            if self.stored >= MAX_DEAD_MARKS:
                break
            if srv.confirmed or srv.name in vpn.conn.busy() or srv.name in vpn.live_servers():
                continue
            if srv.name in self.sh.before:            # in use until it went down (maintenance): its sites' marks
                continue                              # ("unreachable", an hour) are enough, not a day
            try:
                await vpn.conn.start_search(srv.name, self.host_for(srv.name))
            except (OSError, vpn.Unreachable):
                pass                                  # dead again, on its own
            except (RuntimeError, PlatformError):
                continue
            else:
                await vpn.conn.stop_search(srv.name)
                continue                              # it answers now: not dead
            self.store(srv)
            if await self.live_state() != "ok":       # the network went meanwhile: none of this was the servers
                self.unstore()
                return

    def store(self, srv: Server) -> None:
        srv.confirmed, self.stored = True, self.stored + 1
        for pid in self.sh.targets:
            vpn.mark(pid, srv.name, srv.problem)
            self.written.append((pid, srv.name))

    def unstore(self) -> None:
        for pid, name in self.written:
            vpn.unmark(pid, name)
        for srv, _alone in self.failures:
            srv.confirmed = False
        self.written.clear()
        self.stored = 0


def _wave(b: Board, s: str, inflight: set[str], waiting: set[str], blocked: set[str], soft: bool = False) -> list[str]:
    """The sites to ask through `s` now (fail fast): the ones without a server, all at once; else, while nothing is
    known through `s`, the pickiest alone (one whose budget makes it wait gives way to the next meanwhile); once
    one answered, the rest at once (retire() cancels a slow one when `s` can no longer help)."""
    unk = [pid for pid in b.pickiest(b.unknown(s, soft)) if pid not in inflight]
    if not unk or not b.could(s, soft):
        return []
    without = b.full & ~b.best().covered
    if first := [pid for pid in unk if without & b.bit[pid]]:
        return first
    if not b.asked.get(s, 0) and not b.ok.get(s, 0):
        if inflight - waiting:
            return []                             # the first one asks: its answer decides
        free = [pid for pid in unk if pid not in blocked]
        return free[:1] if free else ([] if inflight else unk[:1])
    return unk


COUNTRY_REFUSALS = 2


def _candidate(sh: Search, b: Board, order: list[str]) -> tuple[str | None, bool]:
    """The next server to start: in vpn.order(), the ones a site without a server answered through lately first;
    none already asked, with a process, or whose answers could not help (sh.skipped counts those). When every one
    left is out only because it did not answer lately (unreachable, dead, broken marks), those are tried anyway
    (soft=True), as the pick always did."""
    seen = {s.name for s in sh.servers} | set(b.live) | vpn.conn.busy()
    without = b.full & ~b.best().covered
    liked = {n for pid in b.names(without) for n in vpn.oks(pid)}
    # a country where a site without a server was already refused on COUNTRY_REFUSALS servers goes to the back: a
    # site that refuses whole providers or regions (Facebook) gets to other countries instead of the next server of
    # the same city each time its marks expire
    spent: dict[str, int] = {}
    for name, bits in b.no.items():
        if bits & without & ~b.soft.get(name, 0):
            spent[vpn.country(name)] = spent.get(vpn.country(name), 0) + 1
    ordered = sorted(order, key=lambda n: (n not in liked, spent.get(vpn.country(n), 0) >= COUNTRY_REFUSALS))
    pick, skipped = None, 0
    for name in ordered:
        if name in seen:
            continue
        if not b.could(name):
            skipped += 1
        elif pick is None:
            pick = name
    sh.skipped = skipped
    if pick is None:
        for name in ordered:
            if name not in seen and b.could(name, soft=True):
                return name, True
    return pick, False


async def _start_slot(slot: _Slot, host: str, pid: str | None) -> None:
    """A tunnel of the search: started, the handshake there within seconds, `host` reachable through it (only that
    site cannot use it when not). Failures stay with this search (the breaker and confirm_dead() decide about
    marks)."""
    srv = slot.srv
    try:
        slot.route, reached = await vpn.conn.start_search(srv.name, host)
    except vpn.HandshakePause:                     # the provider refuses handshakes: not this server's fault
        srv.status = "done"
        return
    except vpn.Unreachable as e:
        srv.problem, srv.detail = "dead", e.detail
    except OSError as e:
        srv.problem, srv.detail = "broken", _short(e)
    except RuntimeError:                           # a process runs there (one still draining): not now
        srv.status = "done"
        return
    else:
        srv.status = "testing"
        if not reached and pid:                   # this site's host is not reachable there: the others may be
            srv.answers[pid] = Answer("unreachable", f"no connection to {host}")
        return
    srv.status, srv.secs = "done", round(time.monotonic() - slot.t0, 1)
    log.info("VPN search: %s %s: %s", srv.name, srv.problem, srv.detail)


def _asked(sh: Search, slot: _Slot, pid: str, task: asyncio.Task) -> None:
    """An ask ended: cancelled (its server could no longer help, or the search ends) leaves "skipped"."""
    slot.asks.pop(pid, None)
    tr = slot.trials.pop(pid, None)
    a = slot.srv.answers.get(pid)
    if task.cancelled() and (a is None or a.verdict in ("waiting", "asking")):
        slot.srv.answers[pid] = Answer("skipped", requests=tr.sent if tr else 0)
    elif not task.cancelled() and (e := task.exception()) is not None:
        slot.srv.answers[pid] = Answer("error", _short(e))
        slot.srv.errors[pid] = slot.srv.errors.get(pid, 0) + 1
    if slot.srv.live and not slot.asks:
        slot.srv.status, slot.srv.secs = "done", round(time.monotonic() - slot.t0, 1)


async def _ask_slot(sh: Search, slot: _Slot, tg: Target, job: jobs.Job) -> None:
    """One site through the slot's server: its tunnel, or a live server's route (started again when it stopped)."""
    srv, route = slot.srv, slot.route
    if srv.live:
        route = vpn.conn.route(srv.name)
        if route is None:
            try:
                route = await vpn.conn.start_live(srv.name, tg.host)
            except (OSError, vpn.Unreachable) as e:
                log.warning("VPN search: %s in use does not start: %s", srv.name, e)
                srv.answers[tg.pid] = Answer("unreachable", e.detail if isinstance(e, vpn.Unreachable) else _short(e))
                return
            if route is None:                 # no longer live: the next round sees it
                srv.answers[tg.pid] = Answer("skipped")
                return
    await _ask_one(sh, srv, route, tg, job, slot.trials)
    a = srv.answers.get(tg.pid)
    if a is not None and a.verdict == "moved" and not srv.live and not vpn.conn.alive(route):
        srv.problem, srv.detail = "broken", "wireproxy stopped"      # its tunnel died: not asked again


async def _ask_one(sh: Search, srv: Server, route: vpn.Route, tg: Target, job: jobs.Job,
                   trials: dict[str, Trial] | None = None) -> None:
    """One site through `route`, once its budget allows it. A site's refusal is marked (an answer through a server
    is real, a missing one stays with this search) and, when the site's own server refused it, the site waits from
    now on. An error (nothing sent) is asked once more."""
    while True:
        srv.answers[tg.pid] = Answer("waiting")
        if not await _pace(sh, tg, job):
            srv.answers[tg.pid] = Answer("off")
            return
        srv.answers[tg.pid] = Answer("asking")
        try:
            a = await _ask(sh, route, tg, trials)
        except Exception as e:  # noqa: BLE001 - one site's surprise must not end the search
            log.warning("VPN search: %s through %s: %s", tg.pid, srv.name, e)
            a = Answer("error", _short(e))
        if a.verdict != "paused":
            break
        sh.wait = {"pid": tg.pid, "kind": "pause", "until": time.time() + WAIT_STEP_S}   # its limiter said no
        await _sleep(WAIT_STEP_S)
    srv.answers[tg.pid] = a
    if a.verdict == "error":
        srv.errors[tg.pid] = srv.errors.get(tg.pid, 0) + 1
    elif a.verdict in ("blocked", "rate", "botcheck"):         # the site's own answer: real, kept
        vpn.mark(tg.pid, srv.name, a.verdict, ttl=TRIAL_BLOCKED_TTL_S if a.verdict == "blocked" else None)
        if vpn.server_for(tg.pid) == srv.name and not vpn.paused(tg.pid):
            vpn.pause(tg.pid, a.verdict, a.detail)
    elif a.verdict in ("ok", "site_error"):
        vpn.mark(tg.pid, srv.name, "ok")


async def _ask(sh: Search, route: vpn.Route, tg: Target, trials: dict[str, Trial] | None = None) -> Answer:
    """The site's search through `route` under a Trial (its own visitor), and what it says about the server."""
    tr, t1, exc = Trial(route, limit=tg.cost), time.monotonic(), None
    if trials is not None:
        trials[tg.pid] = tr
    try:
        with trial(tr):
            try:
                await tg.ask()
            except Exception as e:  # noqa: BLE001 - PlatformError, GeizhalsError: judged below
                exc = e
    finally:
        tr.close()
        sh.site(tg.pid).requests += tr.sent
        _spend(tg.pid, tr.sent)
    secs = round(time.monotonic() - t1, 1)
    v = _verdict(tg, tr, exc)
    if v == "botcheck":
        n = sh.streak[tg.pid] = sh.streak.get(tg.pid, 0) + 1
        if n >= BOTCHECK_STREAK:              # through several servers: the site changed, not the servers
            if tg.pid not in sh.odd:
                sh.odd.append(tg.pid)
                vpn.update_conn(odd=sorted({*(vpn.conn_state().get("odd") or []), tg.pid}))
            return Answer("odd_page", _short(exc), tr.sent, secs, {"n": n})
    elif v != "paused":
        sh.streak[tg.pid] = 0
    if v in ("blocked", "rate", "unreachable"):
        detail = tr.detail
    elif v in ("site_error", "botcheck", "paused", "error"):
        detail = _short(exc) or (t("answered without a request (cache or replay)") if v == "error" else "")
    else:
        detail = ""
    return Answer(v, detail, tr.sent, secs, {"fix": "settings"} if isinstance(exc, NotTheExit) else {})


def _verdict(tg: Target, tr: Trial, exc: BaseException | None) -> str:
    """What one try says about the server, checked in this order."""
    if not tg.on():
        return "off"                    # also the Trial's "VPN is off": nothing went out directly
    if isinstance(exc, ExitMoved):
        return "moved"                  # its server stopped before the request went out: says nothing about it
    if tr.refused:
        return tr.refused               # blocked | rate | unreachable
    if isinstance(exc, Blocked) and not exc.sent:
        return "paused"                 # the site's own limiter: a pause after a block, or the hour's budget
    if not tr.sent:
        return "error"                  # nothing went out: proves nothing about the server
    if exc is None:
        return "ok" if vpn.conn.alive(tr.route) else "unreachable"
    if isinstance(exc, NotTheExit):
        return "site_error"             # e.g. rotated credentials: every server would get the same
    if tr.challenge or (tr.status == 200 and "json" not in tr.ctype):
        return "botcheck"               # Cloudflare's challenge (any status) or a page in place of the result list
    return "site_error"                 # the site answered through the server with an error (5xx, a JSON error)


async def _improve(sh: Search) -> None:
    """Switch over when what the search found beats the connections in use."""
    async with vpn._lock("failover"):       # never against a site's failover (vpn.failover()) under way
        await _improve_now(sh)


async def _improve_now(sh: Search) -> None:
    b = Board(sh, _refresh(sh))
    plan, now = b.best(), b.current()        # what is known against what is live (not what is known of it)
    if not plan.servers or plan.key <= now.key:
        return
    assign = b.assign(plan, vpn.conn_state().get("assign") or {})
    hosts = {s: next((sh.targets[p].host for p, x in assign.items() if x == s), "www.willhaben.at")
             for s in plan.servers}
    try:
        await vpn.switch(list(plan.servers), assign, hosts)
    except (OSError, vpn.Unreachable) as e:   # a server found earlier does not start again: not again in this one
        log.warning("VPN search: switching to %s failed: %s", " + ".join(plan.servers), e)
        running_now = {*b.live, *vpn.conn.testing()}
        for srv in sh.servers:
            if srv.name in plan.servers and srv.name not in running_now and not srv.live:
                srv.problem, srv.detail = "broken", _short(e)
        return
    for pid in assign:
        vpn.unpause(pid)
    sh.plan, sh.switched = {"servers": list(plan.servers), "assign": assign, "uncovered": []}, sh.switched + 1
    _save(sh)
    log.info("VPN search: live now %s (%s)", " + ".join(plan.servers),
             ", ".join(f"{p}: {s}" for p, s in assign.items()))


async def _finish(sh: Search, retry: bool = False) -> None:
    """The end: the sites no connection serves pause with a note until the next search (_retry); none served at all:
    no server, their requests fail closed. `retry`: the walk ended with a run of tunnels without a handshake (maybe
    the network): no site pauses for good, the search runs again OFFLINE_RETRY_S later."""
    await vpn.prune()                                # a live server no site uses stops
    b = Board(sh, _refresh(sh))
    plan = b.current()
    uncovered = b.names(b.full & ~plan.covered)
    uncovered += [p for p in sh.out if p not in uncovered and vpn.enabled(p) and vpn.server_for(p) is None]
    if uncovered and retry:
        vpn.update_conn(pending={"at": time.time() + OFFLINE_RETRY_S, "trigger": sh.trigger, "by": sh.by,
                                 "why": sh.why, "detail": sh.detail})
        sh.reason = "retry"
    else:
        for pid in uncovered:
            vpn.pause(pid, "none", f"{sh.checked()}/{sh.total}")
    st = vpn.conn_state()
    odd = sorted(set(sh.odd) | (set() if sh.trigger in REGULAR else set(st.get("odd") or [])))
    vpn.update_conn(odd=odd)
    sh.plan = {"servers": list(plan.servers), "uncovered": uncovered,
               "assign": {p: x for p, x in (st.get("assign") or {}).items() if x in plan.servers and p in b.bit}}
    if plan.servers and plan.covered:
        sh.status = "done"
        return
    sh.status, sh.reason = "failed", "retry" if retry else "none" if sh.servers else "no_eu"
    vpn.update_conn(servers=[], assign={}, error=_failed_text(sh))
    await vpn.conn.disconnect()


def _offline(sh: Search, gone: set[str] = frozenset()) -> None:
    """The home network seems down: the search marks nothing, the sites that lost their server to it (no answer
    through it; dropped by a request, or by dead_live() as it went down) use it again, and the search runs again
    OFFLINE_RETRY_S later."""
    st = vpn.conn_state()
    for pid, _name in vpn.users():
        p = vpn.paused(pid)
        if p is not None and p[0] == "unreachable":
            server = (st.get("assign") or {}).get(pid)
            if server:
                vpn.unmark(pid, server)
            vpn.unpause(pid)
    if sh.before and (gone or not vpn.live_servers(st)):   # dropped for the outage: no fault of the server
        vpn.update_conn(servers=[x for x in sh.before if vpn.allowed(x)], error="")
        for pid, _name in vpn.users():
            for server in sh.before:
                if vpn.marks(pid).get(server, (0, ""))[1] == "unreachable":
                    vpn.unmark(pid, server)
        st = vpn.conn_state()
    sh.status, sh.reason = "failed", "offline"
    sh.plan = {"servers": vpn.live_servers(st), "assign": dict(st.get("assign") or {}), "uncovered": []}
    again = max(OFFLINE_RETRY_S, vpn.guard.paused_s() or vpn.guard.outage())   # no tunnel before the pause ends
    vpn.update_conn(pending={"at": time.time() + again, "trigger": sh.trigger, "by": sh.by,
                             "why": sh.why, "detail": sh.detail})
    log.warning("VPN search: no handshake (the network or the provider), nothing marked, again in %d s", again)


async def _test_profile(sh: Search, job: jobs.Job) -> None:
    """A dedicated profile: every site through it; the refusing ones pause until the next test, never another server."""
    tgs = _refresh(sh)
    name = vpn.source()[1][0]
    sh.total = 1
    srv = Server(name, live=True)
    sh.servers.append(srv)
    t0 = time.monotonic()
    try:
        if name not in vpn.live_servers():
            await vpn.switch([name], {}, {name: tgs[0].host})
        route = vpn.conn.route(name) or await vpn.conn.start_live(name, tgs[0].host)
        if route is None:
            raise OSError(t("wireproxy did not open its port"))
    except (OSError, vpn.Unreachable) as e:
        srv.problem = "dead" if isinstance(e, vpn.Unreachable) and e.handshake else \
            "unreachable" if isinstance(e, vpn.Unreachable) else "broken"
        srv.detail = e.detail if isinstance(e, vpn.Unreachable) else _short(e)
        srv.status, srv.secs = "done", round(time.monotonic() - t0, 1)
        for tg in tgs:
            vpn.pause(tg.pid, srv.problem, srv.detail)
        sh.status, sh.reason = "failed", "profile"
        sh.plan = {"servers": [], "assign": {}, "uncovered": [tg.pid for tg in tgs]}
        vpn.update_conn(error=_failed_text(sh))
        return
    srv.status = "testing"
    await asyncio.gather(*(_ask_one(sh, srv, route, tg, job) for tg in tgs if tg.on()))   # each its own limiter
    assign, refused = {}, []
    for tg in tgs:
        a = srv.answers.get(tg.pid)
        if a is not None and a.verdict in ANSWERED:
            vpn.unpause(tg.pid)
            assign[tg.pid] = name
        elif a is not None and a.verdict in REFUSED:
            vpn.pause(tg.pid, a.verdict, a.detail)
            refused.append(tg.pid)
    vpn.update_conn(servers=[name], assign=assign, error="")
    srv.status, srv.secs = "done", round(time.monotonic() - t0, 1)
    sh.plan = {"servers": [name], "assign": assign, "uncovered": refused}
    sh.status = "done" if assign else "failed"
    sh.reason = "" if assign else "profile"


# ------------------------------------------------------------------ budgets and pauses
def _spend(pid: str, n: int) -> None:
    q = _spent.setdefault(pid, deque())
    q.extend([_clock()] * n)


def _spent_hour(pid: str) -> deque[float]:
    q = _spent.setdefault(pid, deque())
    while q and _clock() - q[0] >= 3600:
        q.popleft()
    return q


def _cap(lim: RateLimiter, cost: int) -> int:
    """Requests the search may send a site an hour: a quarter of its hourly budget (at least one test's worth);
    NO_BUDGET_PER_HOUR without a budget."""
    return max(cost, lim.per_hour // SHARE if lim.per_hour else NO_BUDGET_PER_HOUR)


def _budget(tg: Target) -> tuple[str, float]:
    """("", 0) when the search may ask the site now, else (why, seconds to wait): pause (after a block), quarter (its
    share of the hour is used), full (the agents' requests and the search's are near the budget)."""
    lim, cost = tg.limiter, tg.cost
    if lim.cooling_down_s > lim.max_wait_s:
        return "pause", lim.cooling_down_s - lim.max_wait_s
    spent, cap = _spent_hour(tg.pid), _cap(lim, cost)
    if len(spent) + cost > cap:
        return "quarter", spent[min(len(spent) + cost - cap, len(spent)) - 1] + 3600 - _clock()
    if lim.per_hour and lim.last_hour + cost > lim.per_hour * BUDGET_FULL:
        return "full", lim.free_in(lim.last_hour + cost - int(lim.per_hour * BUDGET_FULL))
    return "", 0.0


async def _pace(sh: Search, tg: Target, job: jobs.Job) -> bool:
    """Wait until the site may be asked (module docstring); False when it was switched off meanwhile."""
    while tg.on():
        why, secs = _budget(tg)
        if not why:
            sh.wait = {}
            return True
        refused = sum(a.verdict in REFUSED for srv in sh.servers if (a := srv.answers.get(tg.pid)) is not None)
        if secs > GIVE_UP_WAIT_S and refused >= GIVE_UP_REFUSALS:     # its hour used up on refusals: go on without it
            if tg.pid not in sh.out:
                sh.out.append(tg.pid)
                log.warning("VPN search: %s leaves this search, its budget would hold it up %.0f min (%s)", tg.pid,
                            secs / 60, why)
            sh.wait = {}
            return False
        lim = tg.limiter
        sh.wait = {"pid": tg.pid, "kind": why, "until": time.time() + secs, "per_hour": lim.per_hour,
                   "cap": _cap(lim, tg.cost), "used": lim.last_hour}
        job.step = _wait_text(sh)
        await _sleep(min(max(secs, 1.0), WAIT_STEP_S))
    sh.wait = {}
    return False


def _short(exc: BaseException | None) -> str:
    return (str(exc) or type(exc).__name__)[:200] if exc is not None else ""


# ------------------------------------------------------------------ persistence
def _save(sh: Search) -> None:
    if sh.discarded:
        return
    skip = ("task", "discarded", "streak", "targets", "wait", "closing")
    d = {f.name: getattr(sh, f.name) for f in fields(sh) if f.name not in skip}
    d.update(sites=[asdict(s) for s in sh.sites], servers=[asdict(s) for s in sh.servers])
    set_setting(STATE_KEY, json.dumps(d, separators=(",", ":")))


def _stored() -> Search | None:
    """The last search from the setting (after a restart); one of the shuffles before it reads as far as it can."""
    try:
        d = json.loads(get_setting(STATE_KEY) or "null")
        if not isinstance(d, dict) or "servers" not in d:
            return None
        kept = d.pop("kept", None)
        if not isinstance(d.get("before"), list):
            d["before"] = [d["before"]] if isinstance(d.get("before"), str) else []
        if kept and "plan" not in d:
            d["plan"] = {"servers": [kept], "assign": {}, "uncovered": []}
        d["sites"] = [Site(**x) for x in d.get("sites") or []]
        d["servers"] = [Server(**{**x, "answers": {p: Answer(**a) for p, a in (x.get("answers") or {}).items()}})
                        for x in d.get("servers") or []]
        sh = Search(**d)
    except (ValueError, TypeError):
        return None
    if sh.status == "running" and not (sh is _run and running()):
        sh.status, sh.reason = "interrupted", "interrupted"     # stored while it ran, then the app restarted
    return sh


# ------------------------------------------------------------------ the view (Jinja global vpn_test)
def view() -> dict[str, Any]:
    """For settings/_vpn_test.html, in the viewer's language: the live connections, the schedule and the last search
    (this process's, else the stored one): its progress while it runs, then its result per site and per server."""
    sh, live = _run or _stored(), running()
    st, mode = vpn.conn_state(), vpn.source()[0]
    pend, nxt = pending(st), next_at()
    names = {pid: name for pid, name in vpn.users()}
    if sh is not None:
        names = {**{s.pid: s.name for s in sh.sites}, **names}
    servers = vpn.live_servers(st)
    on = [pid for pid, _ in vpn.users()]
    conns = [{"name": s, "since": _dt(vpn.since(s, st)), "connected": vpn.conn.route(s) is not None,
              "sites": ", ".join(names.get(p, p) for p in on if vpn.server_for(p, st) == s and not vpn.paused(p))}
             for s in servers]
    waiting = [{"name": names.get(p, p), "note": vpn.pause_note(pz, vpn.waits_on(p, st)),
                "tone": "bad" if pz[0] == "none" else "warn"}
               for p in on if (pz := vpn.paused(p)) is not None]
    out: dict[str, Any] = {
        "live": live, "blocked": None if live else blocker(), "profile": mode == "profile",
        "connections": conns, "two": len(conns) > 1, "waiting": waiting, "connected": bool(vpn.conn.routes()),
        "error": st.get("error") or "", "interval": interval_h(), "next_at": _dt(nxt), "parallel": vpn.parallel(),
        "parallel_ok": vpn.source()[0] == "collection",
        "next_due": nxt is not None and nxt <= time.time(), "pending": _pending_text(pend) if pend else "",
        "run": sh is not None, "progress": "", "now": "", "best": "", "wait": "", "clock": "", "summary": "",
        "tone": "", "trigger": "", "finished_at": None, "rows": [], "servers": [], "more": 0,
        "retesting": retesting(), "retest": _retest_view(names),
        "working": [{"name": w["name"], "sites": ", ".join(names.get(p, p) for p in w["sites"]),
                     "all": len(w["sites"]) == len(on), "live": w["name"] in servers, "at": _dt(w["at"])}
                    for w in vpn.working()] if mode != "profile" else []}
    if sh is None:
        return out
    testing = [s for s in sh.servers if s.status != "done"] if live else []
    current = next((s for s in reversed(testing) if s.answers), testing[-1] if testing else None)   # the newest asked
    listed = [s for s in sh.servers if s.answers or s.problem or s.status != "done"]    # not the unneeded ones
    shown = listed[-SHOW_SERVERS:] if live else listed
    out.update(progress=_progress(sh) if live else "", now=current.name if current else "",
               best=_best_text(sh, names) if live else "", wait=_wait_text(sh) if live else "",
               clock=_clock_text(int(time.time() - sh.started)), summary="" if live else _summary(sh), tone=_tone(sh),
               trigger=_trigger_text(sh, names), finished_at=_dt(sh.finished or sh.started),
               rows=[_row(sh, s, current, live) for s in sh.sites
                     if not live or s.pid in on and current is not None and s.pid in current.answers],
               servers=[_server_row(sh, srv, names, live) for srv in shown],
               more=len(listed) - len(shown))
    return out


def _row(sh: Search, s: Site, srv: Server | None, live: bool) -> dict[str, Any]:
    """One site: live, its answer on the server under test; after the search, where it ended up. Without a message
    the page shows it as a chip."""
    row = {"pid": s.pid, "name": s.name, "label": "", "tone": "", "message": "", "spinner": False, "action": None}
    if live:
        a = srv.answers.get(s.pid) if srv is not None else None
        if a is None:
            row["label"] = t("not asked") if srv is not None and srv.status == "testing" else ""
        elif a.verdict == "asking":
            row["spinner"] = True
        elif a.verdict == "waiting":
            row["label"] = t("waiting")
        else:
            row["label"], row["tone"] = _pill(a.verdict)
            row["message"] = _message(s, a, srv.name, live) if a.verdict not in REFUSED else ""
        return row
    plan = sh.plan or {}
    if sh.status in ("stopped", "interrupted") and not plan:
        return row                                           # the summary says it
    if s.pid in sh.out and s.pid in (plan.get("uncovered") or []):
        row.update(label=t("No server"), tone="bad",
                   message=t("It used up the requests the search may send it this hour, so the search went on "
                             "without it; the next search asks it again."))
        return row
    if s.pid in (plan.get("uncovered") or []):
        said = [srv.answers[s.pid] for srv in sh.servers if s.pid in srv.answers]
        refused = sum(a.verdict in REFUSED for a in said)
        row.update(label=t("No server"), tone="bad",
                   message=t("Refused every server it was asked through ({n}); its requests wait for the next "
                             "search.", n=refused) if refused else
                   t("No server got as far as asking {site}; its requests wait for the next search.",
                     site=s.name))
        return row
    server = (plan.get("assign") or {}).get(s.pid)
    a = next((x.answers[s.pid] for x in reversed(sh.servers) if x.name == server and s.pid in x.answers), None)
    if server is None:
        return row
    row["label"], row["tone"] = _pill(a.verdict) if a is not None and a.verdict in ANSWERED else (t("Works"), "ok")
    if a is not None and a.verdict in ("site_error", "odd_page", "error"):
        row["message"] = _message(s, a, server, live)
        if a.params.get("fix") == "settings" and s.pid != vpn.GEIZHALS:
            row["action"] = {"label": t("Open {site} settings", site=s.name), "href": f"/settings/{s.pid}"}
    return row


def _pill(v: str) -> tuple[str, str]:
    if v == "ok":
        return t("Works"), "ok"
    if v in REFUSED:
        return t("Refused"), "bad"
    if v in ("site_error", "odd_page"):
        return t("Site error"), "warn"
    if v == "off":
        return t("Off"), ""
    if v == "error":
        return t("Error"), "warn"
    if v in ("skipped", "moved"):
        return t("not needed"), ""
    return t("Not tested"), ""


def _message(s: Site, a: Answer, server: str, live: bool) -> str:
    site, v, p = s.name, a.verdict, a.params
    if v in REFUSED:
        msg = vpn.reason_label(v) + (f" ({vpn.describe(a.detail)})" if a.detail else "")
        return msg[:1].upper() + msg[1:]
    if v == "site_error":
        return t("{site} answers through {server}, but with an error: {error}. That is not the server's fault.",
                 site=site, server=server, error=a.detail)
    if v == "odd_page":
        return t("{site} answered through {n} servers in a row with a page the app does not know. That points to a "
                 "change on the site, not to the VPN.", site=site, n=p.get("n", BOTCHECK_STREAK))
    if v == "off":
        return t("The VPN or the site was switched off meanwhile.")
    if v == "error":
        return t("The test failed: {error}", error=a.detail)
    return ""


def _server_row(sh: Search, srv: Server, names: dict[str, str], live: bool) -> dict[str, Any]:
    ok, no, total = len(srv.answered()), len(srv.refused()), len(sh.targets) or len(sh.sites)
    if srv.problem:
        label, tone = vpn.reason_label(srv.problem), "bad"
    elif srv.status != "done":
        label, tone = (t("starting …") if srv.status == "starting" else t("asking the sites …")), "brand"
    elif not ok + no:
        label, tone = t("not tested"), ""
    elif no:
        label, tone = t("Refused"), "warn"
    else:
        label, tone = (t("all answer"), "ok") if ok >= total else (t("{ok} of {n} answer", ok=ok, n=total), "ok")
    parts = [vpn.describe(srv.detail)] if srv.problem and srv.detail else []
    parts += [t("{site}: {reason}", site=names.get(pid, pid), reason=vpn.reason_label(a.verdict))
              + (f" ({vpn.describe(a.detail)})" if a.detail else "") for pid, a in srv.answers.items()
              if a.verdict in REFUSED]
    if no and ok:                                       # a pair's partner: who answered through it
        parts.append(t("answered: {sites}", sites=", ".join(names.get(p, p) for p in srv.answered())))
    return {"name": srv.name, "label": label, "tone": tone, "live": srv.live, "parts": parts,
            "kept": srv.name in vpn.live_servers() and (not live or srv.live),
            "secs": t("{n} s", n=_num(srv.secs)) if srv.secs is not None else ""}


def _progress(sh: Search) -> str:
    """The header line while a search runs."""
    if sh.profile:
        return t("Testing the profile {server}", server=sh.servers[-1].name) if sh.servers else t("Starting …")
    if not sh.total:
        return t("Starting …")
    text = t("Checked {n} of {total} EU servers", n=sh.checked(), total=sh.total)
    if sh.skipped:
        text += " · " + t("{k} of them skipped: refused lately", k=sh.skipped)
    if sh.mode != "parallel":
        text += " · " + t("one server at a time")
    return text


def _plan_text(plan: dict[str, Any], names: dict[str, str], total: int) -> str:
    servers, assign = plan.get("servers") or [], plan.get("assign") or {}
    if not servers:
        return t("none yet")
    if len(servers) == 1:
        n = sum(1 for s in assign.values() if s == servers[0])
        return t("{server} for all {n} sites", server=servers[0], n=n) if n == total and n > 1 else \
            t("{server} for {sites}", server=servers[0], sites=", ".join(names.get(p, p) for p, s in assign.items()))
    return " · ".join(t("{server} for {sites}", server=s,
                        sites=", ".join(names.get(p, p) for p, x in assign.items() if x == s)) for s in servers)


def _best_text(sh: Search, names: dict[str, str]) -> str:
    return _plan_text(sh.best, names, len(sh.targets) or len(sh.sites))


def _wait_text(sh: Search) -> str:
    w = sh.wait
    if not w:
        return ""
    site = sh.targets[w["pid"]].name if w.get("pid") in sh.targets else w.get("pid", "?")
    m = max(1, round((float(w.get("until") or 0) - time.time()) / 60))
    if w.get("kind") == "pause":
        return t("Waits for {site}: it pauses after a block, about {m} min left.", site=site, m=m)
    if w.get("kind") == "full":
        return t("Waits for {site}: {used} of its {per_hour} requests this hour are used; the last tenth stays for "
                 "the search agents. Free again in about {m} min.", site=site, used=w.get("used", 0),
                 per_hour=w.get("per_hour", 0), m=m)
    if w.get("per_hour"):
        return t("Waits for {site}: the search sends it at most a quarter of its {per_hour} requests an hour "
                 "({cap}). Free again in about {m} min.", site=site, per_hour=w["per_hour"], cap=w.get("cap", 0), m=m)
    return t("Waits for {site}: the search sends it at most {cap} requests an hour. Free again in about {m} min.",
             site=site, cap=w.get("cap", 0), m=m)


def _summary(sh: Search) -> str:
    if sh.status in ("stopped", "interrupted"):
        return _reason_text(sh.reason)
    if sh.status == "failed":
        return _failed_text(sh)
    plan = sh.plan or {}
    servers, uncovered = plan.get("servers") or [], plan.get("uncovered") or []
    names = {s.pid: s.name for s in sh.sites}
    if sh.profile and servers:
        n = len(plan.get("assign") or {}) + len(uncovered)
        return t("{server}: {ok} of {n} sites answer", server=servers[0], ok=n - len(uncovered), n=n)
    if not servers:
        return ""
    text = _plan_text(plan, names, len(plan.get("assign") or {}) + len(uncovered))
    if len(servers) == 2:
        text = t("Two servers: {plan}", plan=text)
    elif sh.trigger in REGULAR and servers[0] in sh.before and not uncovered:
        text = t("No other server suits every site; {server} stays", server=servers[0])
    if uncovered:
        text = t("No server for {sites} after {n} of {total} EU servers", sites=", ".join(names.get(p, p)
                                                                                           for p in uncovered),
                 n=sh.checked(), total=sh.total) + " · " + text
    return text


def _tone(sh: Search) -> str:
    if sh.status == "failed":
        return "bad"
    return "warn" if (sh.plan or {}).get("uncovered") else ""


def _failed_text(sh: Search) -> str:
    if sh.reason == "no_eu":
        return t("No server works: the collection has no EU server left to try")
    if sh.reason == "profile":
        return t("The profile does not work for any site")
    if sh.reason == "none":
        return t("No server works: none of the {n} checked answers any site", n=sh.checked())
    if sh.reason == "retry":
        return t("No server answers yet: the last servers tried gave no handshake; the search runs again in {m} min",
                 m=OFFLINE_RETRY_S // 60)
    if sh.reason == "offline":
        return t("Stopped: the internet connection seems down (no server and not the one in use answer); "
                 "nothing was marked, it runs again in {m} min", m=OFFLINE_RETRY_S // 60)
    return t("The search failed: {error}", error=sh.reason)


def _trigger_text(sh: Search, names: dict[str, str]) -> str:
    if sh.trigger == "schedule":
        return t("Scheduled")
    if sh.trigger == "manual":
        return t("Started by you")
    if sh.trigger == "new":
        return t("New configs")
    site = names.get(sh.by) or ("Geizhals" if sh.by == vpn.GEIZHALS else sh.by)
    if sh.trigger == "switch":
        return t("{site} switched on", site=site)
    if sh.why == "none":
        return t("no server yet ({site} needed one)", site=site)
    return t("{site}: {server} {reason}", site=site, server=" + ".join(sh.before) or "?",
             reason=vpn.reason_label(sh.why)) + (f" ({vpn.describe(sh.detail)})" if sh.detail else "")


def _pending_text(p: dict[str, Any]) -> str:
    """Why the next search comes early: an automatic one waits for its turn, or one a restart cut off."""
    if p["trigger"] != "auto":
        return t("A search a restart cut off runs again.")
    site = dict(vpn.users()).get(p["by"], p["by"])
    if p["why"] == "none":
        return t("Brought forward: {site} found no server.", site=site)
    return t("Brought forward: {site} refused the server ({reason}).", site=site, reason=vpn.reason_label(p["why"]))


def _reason_text(reason: str) -> str:
    return {"uploaded": t("Stopped: new configs were uploaded"), "removed": t("Stopped: the configs were removed"),
            "interrupted": t("Interrupted by a restart of the app"),
            "stopped": t("Stopped by you; the servers found so far stay")}.get(reason, t("Stopped"))


def _clock_text(secs: int) -> str:
    """How long the search runs: "42 s", then "2:41 min"."""
    return t("{n} s", n=secs) if secs < 60 else t("{t} min", t=f"{secs // 60}:{secs % 60:02d}")


def _num(x: float) -> str:
    """2.9 → "2.9", in German "2,9"."""
    s = f"{x:g}"
    return s.replace(".", ",") if lang() == "de" else s


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None
