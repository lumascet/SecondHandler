"""WireGuard for the anonymous requests: one or two connections the sites share, found by a search in the background.

Only the anonymous clients use it (search agents, screening, price checks, the ad viewer, the product search and
their photo downloads, Geizhals); the logged-in browser never does. Each site has its own switch (setting
`platform_<id>_vpn`; Geizhals `geizhals_vpn`, off by default).

The source is uploaded in Settings → VPN and kept in DATA_DIR/wireguard (folder 700, files 600):
  profile/<name>.conf    one dedicated profile, used as it is (tested, never swapped)
  collection/*.conf      many configs (e.g. Mullvad's zip of every server); searches pick among the EU ones
A new upload replaces the old source.

Connections: a server in use runs as a wireproxy process (userspace WireGuard: no root, no tun device) with an HTTP
CONNECT proxy on 127.0.0.1. Usually one server serves every site with its switch on; when no single server answers
them all, two do, each site on the one that answers it (`assign`; MAX_LIVE). The search (app/services/vpn_check.py)
tests servers on tunnels of its own (SEARCH_TUNNELS at most), beside the live ones, and switches over (`switch()`)
once it found better ones; a switch-over lets requests under way finish (`hold()`, DRAIN_S) before the old server
stops. Never two processes on one server: one key on one server twice knocks both off (Mullvad's zip has one key for
all servers), so nothing starts a server that has a process (live, the search's, or one still finishing requests).
One key on several *different* servers at once works (verified on 2026-10-15 with Mullvad: a second wireproxy with
the same key on another server ran beside the live one for 2 min 40 s, past WireGuard's 120 s rekey; 60 of 60
requests through the two proxies, alternating every 5 s, were answered, each through its own exit, and the live
tunnel was undisturbed). A new process must show a WireGuard handshake within HANDSHAKE_S (wireproxy's info address, /metrics) and reach the
site's host (CONNECT, no HTTP request), so a dead server fails in seconds. The clients get the proxy explicitly;
nothing sets HTTP(S)_PROXY, so Chromium never sees it. Host names are resolved inside the tunnel (the clients send
CONNECT host:port). `tunnel_for(pid)` is a site's view: the route of its server.

State (settings): `vpn_conn` holds the live servers, which site uses which, since when, the paused sites, the last
search and its automatic starts; `vpn_state` per site the servers it refused (marks, skipped until they expire,
MARK_TTL_S: a dead server only once it failed again on its own) and the ones it answered through (kept as long, they
steer the order of a search).

Failover (Tunnel.refused() → failed()): a site's refusal of its live server counts once it came CONFIRM times in a row
over at least CONFIRM_SPAN_S (network errors: CONFIRM_UNREACHABLE times, at once), with no answer the site accepted in
between; until then the request asks again through the same server. Sites refuse a server that works now and then:
eBay (Akamai) answers every other request with a 403 for half a minute at a time, Geizhals refuses a burst, both
right after a search found them answering there; a run of refusals within seconds says little about the server. A refusal that counts marks the server for the site; the site moves to the other live server when it has
not refused that one too (no pause, no search: untested there until a search asks it), else to a standby server: one
it answered through lately (the `ok` notes of the searches and of "Test again", standby()), started in seconds
(failover(): STANDBY_TRIES at most). Only when none of them starts does it wait (VpnWait) for a search
(vpn_check.trigger(): a site that refuses again gets one after a growing back-off, never only the regular one). The
same holds for a site whose only live server no longer starts (Tunnel.ensure()).

Fail closed: with the switch on and no working server, or for a site whose server refused it (it waits for the search:
VpnWait), `Tunnel.ensure()` raises; nothing falls back to the home IP.
"""
from __future__ import annotations

import asyncio
from collections import deque
import ctypes
import io
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import tempfile
import time
import zipfile
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from app.config import get_settings
from app.i18n import t
from app.platforms.base import PlatformError

log = logging.getLogger(__name__)

MODES = ("profile", "collection")
MARK_TTL_S = {"blocked": 24 * 3600, "rate": 3600, "botcheck": 3600,   # botcheck: a page in place of the one asked
              "unreachable": 3600,   # no answer through it (a timeout, a CONNECT that failed)
              "dead": 24 * 3600, "broken": 24 * 3600,   # no handshake / does not start, confirmed: stale configs
              "ok": 24 * 3600}       # answered through it: tried first when the site needs a server again
MAX_LIVE = 2                   # live connections at most (the search's tunnels come on top)
PARALLEL_KEY = "vpn_parallel"  # servers one site's parallel work (the product search) may use at once, its own one
MAX_PARALLEL = 8               # included (setting, Settings → VPN); 1 = only the site's own server, as before
HELPER_IDLE_S = 300            # a helper server no request used for this long stops
CONFIRM = 3                    # refusals in a row (403/429/bot check) before a site leaves its live server
CONFIRM_UNREACHABLE = 2        # ... network errors in a row (a single timeout is no dead server)
CONFIRM_WINDOW_S = 900         # older refusals no longer count
CONFIRM_SPAN_S = 90            # ... and refusals (403, bot checks) count only when the run lasts this long: eBay
                               # refuses every other request for half a minute at a time. Not a 429 ("rate"): the site
                               # says outright it wants fewer requests from that IP (Geizhals' limit per server)
STANDBY_TRIES = 3              # standby servers a site tries (start + reach its host) before it waits for a search
# Handshakes are what the provider counts. A search that started 66 tunnels in 20 min (11-02 20:20) was followed by no
# handshake anywhere for an hour, and the standby failovers and searches kept asking through it, a dozen new tunnels a
# minute. So every new tunnel (search, standby, live restart) passes one guard: at most HANDSHAKES_PER_WINDOW in
# HANDSHAKE_WINDOW_S, and once the search found no handshake anywhere (its breaker: not even the live server gets
# through) no new server is tried for a pause that grows (OUTAGE_PAUSES_S) – only the live one may start again, within
# the budget, so requests come back with the network; a handshake that works ends the series.
HANDSHAKES_PER_WINDOW = 15
HANDSHAKE_WINDOW_S = 600
OUTAGE_PAUSES_S = (300, 600, 1200, 1800, 3600)
PAUSED_RETRY_S = 120           # a paused site tries the standby servers again this often while its search waits
SEARCH_TUNNELS = 8             # servers the search starts at the same time at most (no site request to start one)
DRAIN_S = 15                   # a server switched away from stops once its requests in flight ended, or after this
HANDSHAKE_S = 5                # no WireGuard handshake within this: the server is dead (an answer takes < 1 s)
PROBE_TIMEOUT_S = 8            # the CONNECT to the site's host once the handshake is there
START_TIMEOUT_S = 5
MAX_UPLOAD = 20 << 20
MAX_MEMBERS = 2000
MAX_CONF = 16 << 10
# EU servers only: outside the EU sites show other prices (through a Swiss exit eBay.at adds Swiss VAT, +8.1 %, to
# every price, and later checks through EU exits saw price drops that were none). Never a fallback to other servers.
EU = frozenset({"at", "be", "bg", "cy", "cz", "de", "dk", "ee", "es", "fi", "fr", "gr", "hr", "hu", "ie", "it", "lt",
                "lu", "lv", "mt", "nl", "pl", "pt", "ro", "se", "si", "sk"})
NEAR = (("at",), ("de", "cz", "sk", "hu", "si", "it"), ("pl", "nl", "be", "lu", "dk", "fr", "hr"))  # home first
GEIZHALS = "geizhals"          # not a platform, but its lookups can go through the connection too
STATE_KEY = "vpn_state"        # {pid: {"marks": {server: [until, reason]}, "ok": {server: until}}}
CONN_KEY = "vpn_conn"
_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")       # a WireGuard key, base64 of 32 bytes
_ENDPOINT = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):\d{1,5}$")
_shuffled: Callable[[list[str]], list[str]] = lambda xs: random.sample(xs, len(xs))   # noqa: E731 - tests: sorted


# ------------------------------------------------------------------ the uploaded source
def root() -> Path:
    return get_settings().wireguard_dir


def source() -> tuple[str | None, list[str]]:
    """(mode, config names) of the uploaded source; (None, []) when there is none."""
    for mode in MODES:
        d = root() / mode
        names = sorted(p.stem for p in d.glob("*.conf")) if d.is_dir() else []
        if names:
            return mode, names
    return None, []


def config_path(name: str) -> Path:
    mode, _ = source()
    return root() / (mode or "collection") / f"{name}.conf"


def country(name: str) -> str:
    """"at-vie-wg-001" -> "at" (Mullvad's names); "" when the name does not start with a country code."""
    m = re.match(r"([a-z]{2})[-_]", name)
    return m.group(1) if m else ""


def check(text: str) -> str | None:
    """None when `text` is a usable wg-quick config, else what is wrong (in the UI language)."""
    section, found = None, set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in ("interface", "peer"):
                return t("unexpected section [{name}]", name=line[1:-1].strip()[:40])
            found.add(section)
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or section is None:
            return t("not a WireGuard config")
        if section == "interface" and key == "privatekey" and _KEY.match(value):
            found.add("privatekey")
        elif section == "interface" and key == "address" and value:
            found.add("address")
        elif section == "peer" and key == "publickey" and _KEY.match(value):
            found.add("publickey")
        elif section == "peer" and key == "endpoint" and _ENDPOINT.match(value):
            found.add("endpoint")
    missing = [k for k in ("interface", "privatekey", "address", "peer", "publickey", "endpoint") if k not in found]
    return t("missing or invalid: {fields}", fields=", ".join(missing)) if missing else None


def _safe_name(filename: str) -> str:
    """The base name as a file name wireproxy's config can point to: lower case, [a-z0-9._-] only."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[:-5] if base.lower().endswith(".conf") else base
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")[:80] or "profile"


def _decode(data: bytes) -> str:
    return data.decode("utf-8-sig", "replace")


def _from_zip(data: bytes) -> tuple[dict[str, str], list[str]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError(t("Not a valid zip file")) from None
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_MEMBERS:
        raise ValueError(t("The zip holds more than {n} files", n=MAX_MEMBERS))
    files: dict[str, str] = {}
    skipped: list[str] = []
    for info in infos:
        base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not base.lower().endswith(".conf") or base.startswith("._") or "__MACOSX/" in info.filename:
            continue
        try:
            with zf.open(info) as f:
                raw = f.read(MAX_CONF + 1)          # bounded: a lying header cannot unpack a bomb
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError, RuntimeError) as e:
            skipped.append(f"{base}: {e}")
            continue
        if len(raw) > MAX_CONF:
            skipped.append(f"{base}: {t('too large')}")
            continue
        text = _decode(raw)
        if err := check(text):
            skipped.append(f"{base}: {err}")
            continue
        name, n = _safe_name(base), 2
        while name in files:
            name, n = f"{_safe_name(base)}-{n}", n + 1
        files[name] = text
    return files, skipped


def import_upload(filename: str, data: bytes) -> tuple[str, int, list[str]]:
    """Replace the source with an uploaded .zip (collection) or .conf (dedicated profile).

    Returns (mode, number of configs, skipped files with the reason). Raises ValueError with a message for the user
    and keeps the old source when the upload holds nothing usable.
    """
    if len(data) > MAX_UPLOAD:
        raise ValueError(t("The file is larger than {n} MB", n=MAX_UPLOAD >> 20))
    lower = (filename or "").lower()
    if lower.endswith(".zip") or data[:4] == b"PK\x03\x04":
        mode = "collection"
        files, skipped = _from_zip(data)
    elif lower.endswith(".conf"):
        mode, skipped = "profile", []
        if len(data) > MAX_CONF:
            raise ValueError(t("{name}: too large", name=filename))
        if err := check(_decode(data)):
            raise ValueError(t("{name}: {error}", name=filename, error=err))
        files = {_safe_name(filename): _decode(data)}
    else:
        raise ValueError(t("Upload a .zip with WireGuard configs or one .conf file"))
    if not files:
        raise ValueError(t("No usable WireGuard config in {name}", name=filename or "upload")
                         + (f" ({skipped[0]})" if skipped else ""))
    _replace(mode, files)
    return mode, len(files), skipped


def _replace(mode: str, files: dict[str, str]) -> None:
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=r))      # mode 700
    for name, text in files.items():
        fd = os.open(tmp / f"{name}.conf", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    for m in MODES:
        shutil.rmtree(r / m, ignore_errors=True)
    tmp.rename(r / mode)


def remove_source() -> None:
    for m in MODES:
        shutil.rmtree(root() / m, ignore_errors=True)


def secure() -> None:
    """Folder 700, files 600 (after a backup restore, which writes with the default mode)."""
    r = root()
    if not r.is_dir():
        return
    for d, _dirs, names in os.walk(r):
        os.chmod(d, 0o700)
        for n in names:
            os.chmod(os.path.join(d, n), 0o600)


def order(exclude: Collection[str] = ()) -> list[str]:
    """The servers a search may try, in its order: the profile alone; in a collection the EU servers, nearest first
    (AT, then DE and the neighbours, then the rest of NEAR, then any EU country), random within each tier. Never one
    outside the EU, not even when none is left."""
    mode, names = source()
    if mode == "profile":
        return [n for n in names[:1] if n not in exclude]
    eu = [n for n in names if country(n) in EU and n not in exclude]
    out: list[str] = []
    seen: set[str] = set()
    for tier in (*NEAR, EU):
        group = [n for n in eu if country(n) in tier and n not in seen]
        seen.update(group)
        out += _shuffled(group)
    return out


def eu_count(names: list[str]) -> int:
    return sum(country(n) in EU for n in names)


def allowed(name: str) -> bool:
    """A server of the source that may be used: the profile, or an EU server of the collection."""
    mode, names = source()
    return name in names and (mode == "profile" or country(name) in EU)


# ------------------------------------------------------------------ switches and marks (setting vpn_state)
def _switch(pid: str) -> str:
    return "geizhals_vpn" if pid == GEIZHALS else f"platform_{pid}_vpn"


def enabled(pid: str) -> bool:
    from app.db import get_setting

    return get_setting(_switch(pid)) == "1"


def set_enabled(pid: str, on: bool) -> None:
    from app.db import set_setting

    set_setting(_switch(pid), "1" if on else "0")


def users() -> list[tuple[str, str]]:
    """(id, name) of every site with its VPN switch on, Geizhals last."""
    from app import platforms

    out = [(p.id, p.name) for p in platforms.all(enabled_only=False) if enabled(p.id)]
    return out + ([(GEIZHALS, "Geizhals")] if enabled(GEIZHALS) else [])


def _json_setting(key: str) -> dict[str, Any]:
    from app.db import get_setting

    try:
        d = json.loads(get_setting(key) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _set_json(key: str, d: dict[str, Any]) -> None:
    from app.db import set_setting

    set_setting(key, json.dumps(d, separators=(",", ":")))


def _load() -> dict[str, Any]:
    return _json_setting(STATE_KEY)


def _save(state: dict[str, Any]) -> None:
    _set_json(STATE_KEY, state)


def marks(pid: str) -> dict[str, tuple[float, str]]:
    """The servers this site refused: name -> (until, reason), expired ones left out."""
    now = time.time()
    raw = (_load().get(pid) or {}).get("marks") or {}
    return {n: (float(m[0]), str(m[1])) for n, m in raw.items()
            if isinstance(m, list) and len(m) == 2 and float(m[0]) > now}


def oks(pid: str) -> dict[str, float]:
    """The servers this site answered through lately: name -> until."""
    now = time.time()
    raw = (_load().get(pid) or {}).get("ok") or {}
    return {n: float(u) for n, u in raw.items() if isinstance(u, (int, float)) and u > now}


def mark(pid: str, name: str, reason: str, ttl: float | None = None) -> None:
    """Note what `name` did for this site: refused it (blocked, rate, unreachable, broken, botcheck) or answered it
    ("ok"), for MARK_TTL_S of the reason or `ttl` seconds. The newer note replaces the older one; expired ones go."""
    state = _load()
    site = state.setdefault(pid, {})
    now = time.time()
    refused = {n: m for n, m in (site.get("marks") or {}).items()
               if isinstance(m, list) and len(m) == 2 and m[0] > now and n != name}
    answered = {n: u for n, u in (site.get("ok") or {}).items() if isinstance(u, (int, float)) and u > now and n != name}
    if reason == "ok":
        answered[name] = now + (ttl or MARK_TTL_S["ok"])
    else:
        refused[name] = [now + (ttl or MARK_TTL_S.get(reason, 3600)), reason]
    site["marks"], site["ok"] = refused, answered
    _save(state)


def forget(pid: str | None = None) -> None:
    """Clear the refusals of every site ("Forget blocks"), or of `pid` only; those sites are no longer paused either."""
    state = _load()
    for key, site in state.items():
        if isinstance(site, dict) and pid in (None, key):
            site["marks"] = {}
    _save(state)
    unpause(pid)


def unmark(pid: str, name: str) -> None:
    """Take back a refusal mark (a search that found the network down or a key conflict wrote it)."""
    state = _load()
    site = state.get(pid)
    if isinstance(site, dict) and name in (site.get("marks") or {}):
        site["marks"] = {n: m for n, m in site["marks"].items() if n != name}
        _save(state)


# ------------------------------------------------------------------ the connections' state (setting vpn_conn)
def conn_state() -> dict[str, Any]:
    """servers (the live ones, the primary first; started again after a restart), assign {pid: server} (the server a
    search found each site answering through; a site without one uses the primary until a search asked it), since
    {server: ts}, paused {pid: [reason, detail]} (the site has no server now: it refused its one and waits for the
    search, or "none": the last search found none for it; its requests wait), odd (sites whose bot-check-like pages
    are the site's change, not the servers'), shuffled (end of the last finished search), auto (starts of the
    automatic searches of the last hour), triggered (sites that started one since the last regular search), pending
    (a search waiting for its turn), error (why there is no server at all). A state from before the search (one
    `server`) reads as one live server."""
    d = _json_setting(CONN_KEY)
    if "servers" not in d and d.get("server"):
        name, ts = d.pop("server"), d.get("since")
        d["servers"], d["since"] = [name], ({name: ts} if isinstance(ts, (int, float)) else {})
    return d


def update_conn(**fields: Any) -> dict[str, Any]:
    d = conn_state()
    d.update(fields)
    _set_json(CONN_KEY, d)
    return d


def live_servers(st: dict[str, Any] | None = None) -> list[str]:
    """The servers in use, the primary first (it serves most sites); never one the source no longer allows."""
    st = conn_state() if st is None else st
    return [s for s in st.get("servers") or [] if isinstance(s, str) and allowed(s)][:MAX_LIVE]


def server_for(pid: str, st: dict[str, Any] | None = None) -> str | None:
    """The server this site sends through: its own among the live ones, else the first live one it has not refused
    lately (a site switched on since the last search, until one asked it). None without such a server."""
    st = conn_state() if st is None else st
    live = live_servers(st)
    own = (st.get("assign") or {}).get(pid)
    if own in live:
        return own
    refused = marks(pid) if live else {}
    return next((s for s in live if s not in refused), None)


def waits_on(pid: str, st: dict[str, Any] | None = None) -> str | None:
    """The server a paused site refused: its own, else the live one it refused last (for the notes)."""
    st = conn_state() if st is None else st
    live = live_servers(st)
    own = (st.get("assign") or {}).get(pid)
    if own in live:
        return own
    refused = {n: until for n, (until, _r) in marks(pid).items() if n in live}
    return max(refused, key=refused.get) if refused else (live[0] if live else None)


def since(name: str, st: dict[str, Any] | None = None) -> float | None:
    d = (conn_state() if st is None else st).get("since")
    v = d.get(name) if isinstance(d, dict) else None
    return float(v) if isinstance(v, (int, float)) else None


def paused(pid: str) -> tuple[str, str] | None:
    """(reason, detail) while this site has no server (it refused its one, or "none" answers it); None when it may
    send."""
    p = (conn_state().get("paused") or {}).get(pid)
    return (str(p[0]), str(p[1])) if isinstance(p, list) and len(p) == 2 else None


def pause(pid: str, reason: str, detail: str = "") -> None:
    d = conn_state()
    d.setdefault("paused", {})[pid] = [reason, detail]
    _set_json(CONN_KEY, d)


def unpause(pid: str | None = None) -> None:
    d = conn_state()
    if d.get("paused"):
        d["paused"] = {} if pid is None else {k: v for k, v in d["paused"].items() if k != pid}
        _set_json(CONN_KEY, d)


# ------------------------------------------------------------------ the wireproxy processes
class Route(NamedTuple):
    proxy: str           # http://127.0.0.1:<port>
    gen: int             # which process start it belongs to; an older one changes nothing in failed()
    name: str = ""       # the server (config) behind it


class VpnWait(PlatformError):
    """The VPN holds this site's requests for now: the site refused its server and waits for the search to find it
    another one, or none answers it until the next search. Nothing is wrong with the request itself (a search agent
    does not count it as failed)."""


class Unreachable(Exception):
    """The server started, but no WireGuard handshake came (`handshake`: the server is down, the config stale), or the
    site's host cannot be reached through it."""

    def __init__(self, host: str, handshake: bool = False) -> None:
        super().__init__(t("no WireGuard handshake in {s} s", s=HANDSHAKE_S) if handshake else
                         t("no connection to {host}", host=host))
        self.host, self.handshake = host, handshake

    @property
    def detail(self) -> str:
        """For marks and the page (vpn.describe() translates it)."""
        return f"no handshake in {HANDSHAKE_S} s" if self.handshake else f"no connection to {self.host}"


@dataclass(eq=False)
class _Proc:
    name: str
    port: int
    handle: Any
    gens: set[int] = field(default_factory=set)     # its routes: the search's, then the live one once taken over

    @property
    def alive(self) -> bool:
        return self.handle.returncode is None


_libc: Any = None


def _die_with_parent() -> None:     # runs in the child between fork and exec: no imports, no locks
    if _libc is not None:
        _libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG: wireproxy never outlives a crashed app


class HandshakePause(PlatformError):
    """No new tunnel now: the handshake budget is used up, or the provider gave no handshake anywhere lately
    (HandshakeGuard). Nothing about a server: it is not marked."""

    def __init__(self, msg: str, left_s: float) -> None:
        super().__init__(msg)
        self.left_s = left_s


class HandshakeGuard:
    """The budget and the outage pause for new tunnels (the module's HANDSHAKE* and OUTAGE* settings)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.starts: deque[float] = deque()
        self.level = 0                     # outages in a row (no handshake after the last pause either)
        self.pause_until = 0.0

    def paused_s(self) -> float:
        return max(0.0, self.pause_until - time.monotonic())

    def _budget_wait(self, now: float) -> float:
        while self.starts and now - self.starts[0] >= HANDSHAKE_WINDOW_S:
            self.starts.popleft()
        return 0.0 if len(self.starts) < HANDSHAKES_PER_WINDOW else self.starts[0] + HANDSHAKE_WINDOW_S - now

    async def admit(self, max_wait_s: float, in_pause: bool = False) -> None:
        """Room for one new tunnel: waits up to `max_wait_s` for the budget, else HandshakePause; in a pause at once
        (unless `in_pause`: the live server's restart)."""
        while True:
            if not in_pause and (left := self.paused_s()) > 0:
                raise HandshakePause(t("VPN: the provider gave no handshake lately – no new server tried for {m} min",
                                       m=f"{left / 60:.0f}"), left)
            now = time.monotonic()
            wait = self._budget_wait(now)
            if wait <= 0:
                self.starts.append(now)
                return
            if wait > max_wait_s:
                raise HandshakePause(t("VPN: {n} new servers in {m} min already – the next in {s} s",
                                       n=HANDSHAKES_PER_WINDOW, m=HANDSHAKE_WINDOW_S // 60, s=f"{wait:.0f}"), wait)
            await asyncio.sleep(min(wait, 30))

    def result(self, handshake: bool) -> None:
        if handshake and self.level:
            log.info("VPN: handshakes work again")
            self.level = 0

    def outage(self) -> float:
        """No handshake anywhere (the search's breaker): no new tunnel for the next pause of the series; its length."""
        pause = OUTAGE_PAUSES_S[min(self.level, len(OUTAGE_PAUSES_S) - 1)]
        self.pause_until = time.monotonic() + pause
        self.level += 1
        log.warning("VPN: no handshake anywhere – no new tunnel for %d min (the provider is not asked meanwhile)",
                    pause // 60)
        return pause


guard = HandshakeGuard()


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Runner:
    """Starts and stops wireproxy and probes its proxy; tests swap in a fake (vpn.runner = ...)."""

    _info: dict[str, bool] = {}      # binary -> it has the info address (-i: /metrics with the last handshake)

    def binary(self) -> str | None:
        return shutil.which(get_settings().wireproxy_bin)

    async def _has_info(self, binary: str) -> bool:
        if binary not in self._info:
            try:
                p = await asyncio.create_subprocess_exec(binary, "--help", stdin=asyncio.subprocess.DEVNULL,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(p.communicate(), 5)
            except (OSError, asyncio.TimeoutError):
                out = b""
            self._info[binary] = b"--info" in out
        return self._info[binary]

    async def start(self, conf: Path, port: int) -> Any:
        global _libc
        if _libc is None:
            with suppress(OSError):
                _libc = ctypes.CDLL("libc.so.6", use_errno=True)
        binary = self.binary()
        if binary is None:
            raise OSError(t("wireproxy is not installed"))
        run = Path(tempfile.gettempdir()) / "willhaben-vpn"
        run.mkdir(mode=0o700, exist_ok=True)
        cfg, err_log = run / f"{conf.stem}.conf", run / f"{conf.stem}.log"
        cfg.write_text(f"WGConfig = {conf}\n\n[http]\nBindAddress = 127.0.0.1:{port}\n", encoding="utf-8")
        info = f"127.0.0.1:{_port()}" if await self._has_info(binary) else None
        with open(err_log, "wb") as err:     # a file, not a pipe: an unread pipe would stall wireproxy
            proc = await asyncio.create_subprocess_exec(binary, "-s", "-c", str(cfg), *(["-i", info] if info else []),
                                                        stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=err,
                                                        preexec_fn=_die_with_parent)
        proc.info = info
        try:
            end = time.monotonic() + START_TIMEOUT_S
            while time.monotonic() < end:
                if proc.returncode is not None:
                    tail = err_log.read_text("utf-8", "replace").strip().splitlines()[-1:] if err_log.exists() else []
                    raise OSError(t("wireproxy stopped: {error}", error=(tail[0] if tail else proc.returncode)))
                try:
                    _r, w = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.1)
                    continue
                w.close()
                return proc
            raise OSError(t("wireproxy did not open its port"))
        except BaseException:          # did not start, or cancelled meanwhile (VPN test, shutdown): no orphan
            await self.stop(proc)
            raise

    async def probe(self, handle: Any, port: int, host: str) -> str:
        """"" when `host` is reachable through the tunnel, else what failed: "handshake" (no WireGuard handshake
        within HANDSHAKE_S: a dead server fails in seconds, not after the CONNECT timeout) or "connect". The CONNECT
        host:443 makes wireproxy handshake and dial through the tunnel; it answers 200 only when the site is reachable
        that way. No HTTP request reaches the site."""
        connect = asyncio.ensure_future(self._connect(port, host))
        try:
            info = getattr(handle, "info", None)
            if info:
                end = time.monotonic() + HANDSHAKE_S
                while not connect.done() and not await self._handshake(info):
                    if time.monotonic() >= end:
                        return "handshake"
                    await asyncio.wait({connect}, timeout=0.25)
            return "" if await connect else "connect"
        finally:
            connect.cancel()
            with suppress(BaseException):
                await connect

    async def reach(self, port: int, host: str) -> bool:
        """A CONNECT host:443 through a running tunnel gets through (no HTTP request reaches the site)."""
        return await self._connect(port, host)

    async def _connect(self, port: int, host: str) -> bool:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            await writer.drain()
            parts = (await asyncio.wait_for(reader.readline(), HANDSHAKE_S + PROBE_TIMEOUT_S)).split()
            return len(parts) >= 2 and parts[0].startswith(b"HTTP/1.") and parts[1] == b"200"
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _handshake(self, info: str) -> bool:
        """wireproxy's /metrics (the info address) shows a completed handshake: last_handshake_time_sec > 0."""
        host, _, port = info.rpartition(":")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 1)
        except (OSError, asyncio.TimeoutError):
            return False
        try:
            writer.write(b"GET /metrics HTTP/1.0\r\nHost: wireproxy\r\n\r\n")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(), 1)
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            writer.close()
        m = re.search(rb"last_handshake_time_sec=(\d+)", body)
        return bool(m and int(m.group(1)) > 0)

    async def stop(self, handle: Any) -> None:
        if handle.returncode is not None:
            return
        with suppress(ProcessLookupError):
            handle.terminate()
        try:
            await asyncio.wait_for(handle.wait(), 3)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                handle.kill()
            await handle.wait()


runner = Runner()

_tunnels: dict[str, Tunnel] = {}
_locks_: tuple[Any, dict[str, asyncio.Lock]] | None = None


def _lock(kind: str) -> asyncio.Lock:
    """An asyncio.Lock of the running loop (tests run one loop per test): one for the live connections, one for the
    search's tunnels, so a slow start of a server under test never holds up a live one. Both: live first."""
    global _locks_
    loop = asyncio.get_running_loop()
    if _locks_ is None or _locks_[0] is not loop:
        _locks_ = (loop, {})
    return _locks_[1].setdefault(kind, asyncio.Lock())


_last_port = 0


def _free_port(taken: Collection[int] = ()) -> int:
    """A free local port: none a process or a start under way holds (several start at once), never the one handed
    out last (a client keeps its transport while the proxy URL stays the same; a new server needs new connections)."""
    global _last_port
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != _last_port and port not in taken:
            break
    else:
        raise OSError("no free local port")
    _last_port = port
    return port


class Connection:
    """The processes: one per live server (MAX_LIVE at most), the search's tunnels beside them, and the ones that
    stop once their requests ended."""

    def __init__(self) -> None:
        self.live: dict[str, tuple[_Proc, Route]] = {}
        self.probes: dict[str, tuple[_Proc, Route]] = {}   # the servers the search tests, beside the live ones
        self._starting: set[str] = set()                  # search tunnels starting right now
        self._ports: set[int] = set()                     # ports handed out to processes and starts under way
        self.gen = 0
        self.closing = False             # the app shuts down: no new process
        self._inflight: dict[int, int] = {}                # route gen -> requests in flight
        self._retiring: set[_Proc] = set()                 # switched away from, stopping once their requests ended
        self._tasks: set[asyncio.Task] = set()
        # extra servers for one site's parallel work (lanes()): never a live one, never one of the search's
        self.helpers: dict[str, tuple[_Proc, Route]] = {}
        self.helper_used: dict[str, float] = {}             # server -> monotonic time of its last lane

    # ---- reads (no lock)
    def route(self, name: str | None) -> Route | None:
        """The live route of server `name` while its process runs."""
        p = self.live.get(name) if name else None
        return p[1] if p is not None and p[0].alive else None

    def routes(self) -> list[Route]:
        return [r for p, r in self.live.values() if p.alive]

    def current(self) -> Route | None:
        """The primary's route."""
        servers = live_servers()
        return self.route(servers[0]) if servers else None

    def _procs(self) -> list[_Proc]:
        return [p for p, _r in [*self.live.values(), *self.probes.values(), *self.helpers.values()]] + list(self._retiring)

    def alive(self, route: Route) -> bool:
        """`route`'s process still runs: a live one, the search's tunnel (also once taken over), or one that stops
        once its requests ended."""
        return any(route.gen in p.gens and p.alive for p in self._procs())

    def is_live(self, route: Route) -> bool:
        """`route` is the route of a live server now."""
        p = self.live.get(route.name)
        return p is not None and p[1] == route and p[0].alive

    def processes(self) -> int:
        return sum(p.alive for p in self._procs())

    def busy(self) -> set[str]:
        """Servers with a process or a start now (the search starts none of them)."""
        return {*self.live, *self.probes, *self.helpers, *self._starting, *(p.name for p in self._retiring if p.alive)}

    def draining(self) -> int:
        return sum(p.alive for p in self._retiring)

    @contextmanager
    def hold(self, route: Route) -> Iterator[None]:
        """A request in flight on `route`: a switch-over lets it finish before its process stops (≤ DRAIN_S)."""
        self._inflight[route.gen] = self._inflight.get(route.gen, 0) + 1
        try:
            yield
        finally:
            n = self._inflight.pop(route.gen, 1) - 1
            if n > 0:
                self._inflight[route.gen] = n

    def _busy(self, proc: _Proc) -> bool:
        return any(self._inflight.get(g) for g in proc.gens)

    async def reach(self, name: str, host: str) -> bool:
        """Does live server `name` still get through to `host` (a CONNECT through its tunnel, no HTTP request)?"""
        p = self.live.get(name)
        return p is not None and p[0].alive and await runner.reach(p[0].port, host)

    # ---- starts and stops
    async def _start(self, name: str, host: str, need_host: bool = True, wait_s: float = 20,
                     in_pause: bool = False) -> tuple[_Proc, bool]:
        """Start `name`: a WireGuard handshake within HANDSHAKE_S, then a CONNECT to `host` (no HTTP request).
        Returns the process and whether `host` answered. OSError: it does not start; Unreachable: no handshake, or
        `host` unreachable when `need_host` (stopped again; a search tunnel stays: only that site cannot use it).
        Cancelled meanwhile: stopped."""
        if self.closing:
            raise PlatformError(t("The app is shutting down"))
        await guard.admit(wait_s, in_pause)   # the provider's handshake budget (HandshakePause: not now, no mark)
        port = _free_port(self._ports | {p.port for p in self._procs()})
        self._ports.add(port)
        try:
            handle = await runner.start(config_path(name), port)
            try:
                reached = await runner.probe(handle, port, host)
            except BaseException:
                await runner.stop(handle)
                raise
            guard.result(reached != "handshake")
            if reached == "handshake" or reached and need_host:
                await runner.stop(handle)
                raise Unreachable(host, handshake=reached == "handshake")
        except BaseException:
            self._ports.discard(port)
            raise
        return _Proc(name, port, handle), not reached

    def _route(self, proc: _Proc) -> Route:
        self.gen += 1
        proc.gens.add(self.gen)
        return Route(f"http://127.0.0.1:{proc.port}", self.gen, proc.name)

    async def _evict(self, name: str) -> None:
        """A process of `name` still finishing requests stops now: never two processes on one server."""
        for p in [p for p in self._retiring if p.name == name]:
            await self._stop(p)

    async def start_live(self, name: str, host: str) -> Route | None:
        """The process of live server `name` after a restart or a crash (Tunnel.ensure()); None when `name` is no
        longer live. Never beside another process on the same server (one of the search's or one still draining
        stops first)."""
        async with _lock("live"):
            if (r := self.route(name)) is not None:
                return r
            if name not in live_servers():
                return None
            dead = self.live.pop(name, None)
            if dead is not None:
                await self._stop(dead[0])
            if name in self._starting:
                raise OSError(t("{server} is being tested right now", server=name))
            async with _lock("search"):
                probe = self.probes.pop(name, None)
            if probe is not None:
                await self._stop(probe[0])
            await self._evict(name)
            proc, _ = await self._start(name, host, in_pause=True)    # the server in use: back with the network
            self.live[name] = (proc, self._route(proc))
            log.info("VPN: server %s started", name)
            return self.live[name][1]

    async def start_search(self, name: str, host: str) -> tuple[Route, bool]:
        """A tunnel of the search on `name`, beside the live ones and its other tunnels (several start at once);
        returns its route and whether `host` answered there. Never a server that has a process (live, the search's,
        or one still draining): one key on one server twice would knock both off."""
        async with _lock("search"):
            if self.closing:
                raise PlatformError(t("The app is shutting down"))
            if name in self.busy() or name in live_servers():
                raise RuntimeError(f"{name} runs already")
            self._starting.add(name)
        try:
            proc, reached = await self._start(name, host, need_host=False, wait_s=HANDSHAKE_WINDOW_S)   # waits its turn
        finally:
            self._starting.discard(name)
        if self.closing:
            await self._stop(proc)
            raise PlatformError(t("The app is shutting down"))
        self.probes[name] = (proc, self._route(proc))
        return self.probes[name][1], reached

    async def stop_search(self, name: str | None = None) -> None:
        """Stop the search's tunnel on `name`, or all of them; a server taken over meanwhile is live and stays."""
        async with _lock("search"):
            gone = [self.probes.pop(n) for n in ([name] if name else list(self.probes)) if n in self.probes]
            self._retiring.update(p for p, _r in gone)
        for proc, _r in gone:
            await self._stop(proc)

    async def helper(self, name: str, host: str) -> Route:
        """The route of helper server `name` for parallel work (started when it does not run; it must reach `host`).
        Never a server that has another process (live, the search's, one still draining). OSError / Unreachable /
        RuntimeError: not available."""
        async with _lock("search"):
            if self.closing:
                raise PlatformError(t("The app is shutting down"))
            p = self.helpers.get(name)
            if p is not None and p[0].alive:
                self.helper_used[name] = time.monotonic()
                return p[1]
            if p is not None:
                self.helpers.pop(name, None)
            if name in self.busy() or name in live_servers():
                raise RuntimeError(f"{name} runs already")
            self._starting.add(name)
        try:
            proc, _ = await self._start(name, host)
        finally:
            self._starting.discard(name)
        async with _lock("search"):
            self.helpers[name] = (proc, self._route(proc))
            self.helper_used[name] = time.monotonic()
        log.info("VPN: helper server %s started", name)
        return self.helpers[name][1]

    async def stop_helper(self, name: str) -> None:
        """Helper `name` stops once its requests in flight ended."""
        async with _lock("search"):
            p = self.helpers.pop(name, None)
            self.helper_used.pop(name, None)
            if p is not None:
                self._retiring.add(p[0])
        if p is not None:
            await self._stop_or_retire(p[0])

    async def prune_helpers(self, idle_s: float = HELPER_IDLE_S) -> None:
        now = time.monotonic()
        for name in [n for n, at in self.helper_used.items() if now - at >= idle_s]:
            await self.stop_helper(name)

    def release_helpers(self) -> None:
        """Parallel work ended: its helpers count as used now and stop once idle for HELPER_IDLE_S (unless the next
        parallel work takes them again)."""
        if not self.helpers:
            return
        now = time.monotonic()
        for name in self.helpers:
            self.helper_used[name] = now

        async def later() -> None:
            await asyncio.sleep(HELPER_IDLE_S + 1)
            await self.prune_helpers()

        task = asyncio.get_running_loop().create_task(later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def testing(self) -> list[str]:
        """The servers on the search's tunnels (starting or started)."""
        return sorted({*self.probes, *self._starting})

    async def switch(self, servers: list[str], hosts: dict[str, str], commit: Callable[[], None]) -> None:
        """Make `servers` the live ones: the search's tunnel is taken over when it runs one of them (no new
        handshake), a live one stays as it is, any other starts (it must reach `hosts[name]`). Then in one step with
        `commit()` (the new state) the routes change: a site on another server than before gets that server's route
        (a new transport, a new visitor). The servers left stop once their requests in flight ended (≤ DRAIN_S).
        OSError/Unreachable when a server does not start: nothing changed."""
        async with _lock("live"), _lock("search"):
            started: dict[str, _Proc] = {}
            probes = {n: p for n, p in self.probes.items() if p[0].alive}
            try:
                for name in servers:
                    if name in self._starting:
                        raise OSError(t("{server} is being tested right now", server=name))
                    if self.route(name) is None and name not in probes:
                        await self._evict(name)
                        started[name] = (await self._start(name, hosts.get(name) or "www.willhaben.at"))[0]
            except BaseException:
                for p in started.values():
                    await self._stop(p)
                raise
            new: dict[str, tuple[_Proc, Route]] = {}
            for name in servers:
                if name in started:
                    new[name] = (started[name], self._route(started[name]))
                elif name in probes:
                    proc = self.probes.pop(name)[0]
                    new[name] = (proc, self._route(proc))
                else:
                    new[name] = self.live[name]
            gone = [p for p, _r in self.live.values() if all(p is not q for q, _x in new.values())]
            fresh = [n for n in servers if new[n] is not self.live.get(n)]
            self.live = new
            self._retiring.update(gone)              # stopped below, also when cancelled meanwhile
            commit()
        if fresh:
            log.info("VPN: live now %s (new: %s)", " + ".join(servers), ", ".join(fresh))
        for p in gone:
            await self._stop_or_retire(p)

    async def _stop_or_retire(self, proc: _Proc) -> None:
        """Stop a process that left: at once when no request is in flight on it, else once they ended (≤ DRAIN_S),
        in the background."""
        if not self._busy(proc) or not proc.alive:
            await self._stop(proc)
            return
        self._retiring.add(proc)
        task = asyncio.get_running_loop().create_task(self._drain(proc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, proc: _Proc) -> None:
        try:
            end = time.monotonic() + DRAIN_S
            while self._busy(proc) and proc.alive and time.monotonic() < end:
                await asyncio.sleep(0.1)
        finally:
            await self._stop(proc)

    async def _stop(self, proc: _Proc) -> None:
        await runner.stop(proc.handle)           # cancelled meanwhile: it stays in _retiring, disconnect() stops it
        self._retiring.discard(proc)
        self._ports.discard(proc.port)

    async def drop(self, name: str) -> None:
        """Live server `name` leaves (it no longer starts, or no site uses it)."""
        async with _lock("live"):
            p = self.live.pop(name, None)
            if p is not None:
                self._retiring.add(p[0])
        if p is not None:
            await self._stop_or_retire(p[0])

    async def disconnect(self) -> None:
        """Every process stops, also the ones still finishing requests (the configs changed, the app shuts down)."""
        async with _lock("live"), _lock("search"):
            procs = [p for p, _r in [*self.live.values(), *self.probes.values(), *self.helpers.values()]]
            self.live, self.probes, self.helpers, self.helper_used = {}, {}, {}, {}
            self._retiring.update(procs)
        for task in list(self._tasks):
            task.cancel()
        for p in list(self._retiring):
            with suppress(Exception):
                await self._stop(p)


conn = Connection()


def _search(pid: str, reason: str, detail: str) -> bool:
    """Ask for a search (app/services/vpn_check.py tests the sites through their adapters, so it lives there); True
    when one runs or comes (now or after its guards and back-off), False when none can run."""
    from app.services import vpn_check

    return vpn_check.trigger(pid, reason, detail)


async def switch(servers: list[str], assign: dict[str, str], hosts: dict[str, str]) -> None:
    """The search's result goes live: `servers` (the primary first, MAX_LIVE at most), each site on its server in
    `assign` (see Connection.switch). A server that stays keeps its route and its `since`."""
    servers = servers[:MAX_LIVE]

    def commit() -> None:
        st = conn_state()
        was, now = live_servers(st), time.time()
        update_conn(servers=servers, assign={p: s for p, s in assign.items() if s in servers}, error="",
                    since={s: (since(s, st) if s in was and since(s, st) else now) for s in servers}, dropped=[])

    await conn.switch(servers, hosts, commit)


async def drop(name: str, error: str = "") -> None:
    """A live server that does not start again leaves: its sites use the other one, if any (untested there until a
    search asks them), else they wait for a search."""
    st = conn_state()
    servers = [s for s in live_servers(st) if s != name]
    update_conn(servers=servers, assign={p: s for p, s in (st.get("assign") or {}).items() if s != name},
                error="" if servers else error, dropped=[name])      # the search's reference for the network
    await conn.drop(name)


async def prune() -> None:
    """A live server no site with its switch on uses any more stops (after a switch went off, at the end of a
    search; the state stays when no site uses the VPN at all: switched on again, it starts the same server)."""
    on = {pid for pid, _ in users()}
    if not on:
        await conn.disconnect()
        return
    st = conn_state()
    servers = live_servers(st)
    assign = {p: s for p, s in (st.get("assign") or {}).items() if p in on and s in servers}
    rest = {server_for(p, st) for p in on - set(assign) if not paused(p)}      # untested sites use one too
    used = {s for p, s in assign.items() if not paused(p)} | rest             # a paused site uses none
    keep = [s for s in servers if s in used]
    if keep != servers or assign != (st.get("assign") or {}):
        update_conn(servers=keep, assign=assign)
    for s in servers:
        if s not in keep:
            await conn.drop(s)


def parallel() -> int:
    """How many servers one site's parallel work may use at once (its own included); 1 = its own only."""
    from app.db import get_setting
    try:
        n = int(get_setting(PARALLEL_KEY) or 1)
    except ValueError:
        n = 1
    return max(1, min(n, MAX_PARALLEL))


def set_parallel(n: int) -> int:
    from app.db import set_setting
    n = max(1, min(int(n), MAX_PARALLEL))
    set_setting(PARALLEL_KEY, str(n))
    return n


async def lanes(pid: str, want: int | None = None) -> list[Route]:
    """Routes for one site's parallel work: its own server's first, then helper servers – the ones it answered
    through lately (standby()), then other allowed servers in the search's order – each started here and reaching
    the site's host (a CONNECT, no request), up to `want` (default parallel()) in all. [] while the site's VPN is
    off (its requests go out directly: one connection, its limiter paces them); just its own route when no helper
    starts. A helper the site refuses later is marked for it (lane_refused) like any refused server."""
    tn = tunnel_for(pid)
    if tn is None:
        return []
    want = parallel() if want is None else max(1, min(want, MAX_PARALLEL))
    first = await tn.ensure()
    routes = [first]
    if want <= 1:
        return routes
    refused = marks(pid)
    tried = 0
    # helpers that run already first (no start), then the site's standby servers, then the rest of the source
    running = [n for n in conn.helpers if n not in refused and n != first.name]
    candidates = list(dict.fromkeys(running + standby(pid, exclude=[first.name]) + order(exclude=[first.name])))
    for name in candidates:
        if len(routes) >= want or tried >= 2 * want:
            break
        if name in refused or name in live_servers() or not allowed(name):
            continue
        tried += 1
        try:
            routes.append(await conn.helper(name, tn.host))
        except Unreachable as e:
            mark(pid, name, "unreachable")
            log.info("VPN %s: helper %s unreachable: %s", pid, name, e.detail)
        except (OSError, RuntimeError, PlatformError) as e:
            log.info("VPN %s: helper %s not started: %s", pid, name, e)
    return routes


def lane_refused(pid: str, route: Route, reason: str) -> None:
    """A site refused a helper's (or its own) server during parallel work: marked for the site, so neither the next
    lanes() nor a failover picks it; its own server goes the usual way (Tunnel.refused)."""
    mark(pid, route.name, reason)


def standby(pid: str, exclude: Collection[str] = ()) -> list[str]:
    """The servers this site answered through lately (oks(): a search's or "Test again"'s answer, kept MARK_TTL_S),
    newest first: the ones the source allows, the site has not refused since, and no process runs on (never two
    on one server). The quick failover's candidates."""
    ok, refused, busy = oks(pid), marks(pid), conn.busy() | set(live_servers())
    return [n for n in sorted(ok, key=ok.get, reverse=True)
            if n not in refused and n not in busy and n not in exclude and allowed(n)]


def working() -> list[dict[str, Any]]:
    """The servers that answered sites with the VPN on lately (oks(): the searches' and "Test again"'s answers, kept
    MARK_TTL_S), each with those sites (in users() order) and when the newest answer came: the most sites first,
    then the newest. For Settings → VPN; the same notes are the failover's standby list (standby())."""
    by: dict[str, dict[str, Any]] = {}
    for pid, _name in users():
        for n, until in oks(pid).items():
            if allowed(n):
                e = by.setdefault(n, {"name": n, "sites": [], "at": 0.0})
                e["sites"].append(pid)
                e["at"] = max(e["at"], until - MARK_TTL_S["ok"])
    return sorted(by.values(), key=lambda e: (-len(e["sites"]), -e["at"], e["name"]))


def _jobs_track(label: str) -> Any:
    from app.services import jobs

    return jobs.track("vpn-failover", label, lane="vpn", link="/settings/vpn", quiet=True)


async def failover(pid: str, host: str, label: str = "") -> str | None:
    """Move this site to a standby server (standby()) beside the live servers the other sites keep using: the first
    that starts and reaches `host` within STANDBY_TRIES. Returns its name, or None (no room beside two live servers
    other sites use, none on the list, none started). Also while a search runs: it may wait an hour for a site's
    budget, and its switch-over at the end takes the site along anyway. A server that does
    not start or reach the host is marked for the site. Shown as a quiet job (lane "vpn": an open settings page
    refreshes)."""
    if conn.closing or guard.paused_s() > 0:         # no handshake anywhere lately: no server to blame or try
        return None
    async with _lock("failover"):
        _retried[pid] = time.monotonic()
        st = conn_state()
        live, assign = live_servers(st), dict(st.get("assign") or {})
        if (own := server_for(pid, st)) is not None and own not in marks(pid) and conn.route(own) is not None:
            unpause(pid)
            return own                                # another request of the site moved it meanwhile
        on = {p for p in (p for p, _ in users()) if p != pid and not paused(p)}
        # the servers other sites are on stay; one a site without its own only borrows (server_for()'s fallback: one
        # switched on lately, or one no server answers) stays only while there is room beside the standby server
        firm = [s for s in live if s in {assign[p] for p in on if assign.get(p) in live}]
        if len(firm) >= MAX_LIVE:
            return None
        loose = [s for s in live if s not in firm and s in {server_for(p, st) for p in on}]
        keep = [s for s in live if s in firm or s in loose[:MAX_LIVE - 1 - len(firm)]]
        tried: list[str] = []
        for name in standby(pid)[:STANDBY_TRIES]:
            tried.append(name)
            servers = [*keep, name]
            new = {p: s for p, s in assign.items() if p != pid and s in keep} | {pid: name}
            try:
                async with _jobs_track(t("VPN: {site} moves to {server}", site=label or pid, server=name)) as job:
                    await switch(servers, new, {name: host})
                    job.result = t("{site} now through {server}", site=label or pid, server=name)
            except HandshakePause as e:              # the provider is refusing handshakes: no server is at fault
                log.info("VPN %s: no standby server tried now: %s", pid, e)
                return None
            except Unreachable as e:
                log.warning("VPN %s: standby server %s %s", pid, name, e)
                mark(pid, name, "unreachable")       # not "dead": that one a search confirms on its own
                continue
            except OSError as e:
                log.warning("VPN %s: standby server %s does not start: %s", pid, name, e)
                mark(pid, name, "broken")
                continue
            unpause(pid)
            log.warning("VPN %s: failover to standby server %s (live now %s)", pid, name, " + ".join(servers))
            return name
        if tried:
            log.warning("VPN %s: no standby server started (%s)", pid, ", ".join(tried))
        return None


# ------------------------------------------------------------------ one site's view of the connections
_strikes: dict[tuple[str, str], list[float]] = {}   # (site, server) -> its refusals in a row (Tunnel.refused())
_retried: dict[str, float] = {}                      # site -> its last failover try (a paused one retries: ensure())


class Tunnel:
    """The route a site sends through (its server's), and its report when the site refuses that server."""

    def __init__(self, pid: str, host: str, label: str | None = None) -> None:
        self.pid, self.host, self.label = pid, host, label or pid

    def current(self) -> Route | None:
        return conn.route(server_for(self.pid))

    def alive(self, route: Route) -> bool:
        """`route`'s process still runs (a test of the search may send through a server that is not the site's)."""
        return conn.alive(route)

    def hold(self, route: Route) -> Any:
        return conn.hold(route)

    def odd(self) -> bool:
        """A search found this site answering with bot-check-like pages through several servers: the site changed,
        not the servers, so its live bot checks refuse no server until the next regular search."""
        return self.pid in (conn_state().get("odd") or [])

    async def ensure(self) -> Route:
        """The route to send through: the process of this site's server, started again after a restart or a crash
        (a server that no longer starts leaves; the other one, if any, takes over). VpnWait while the site waits for
        a server (it refused its one, or none is there yet and a search runs or comes); PlatformError when none is
        there and none comes (fail closed: the caller must not send)."""
        error = ""
        for _ in range(MAX_LIVE + 1):
            if paused(self.pid) is not None and self._retry_due() and await failover(self.pid, self.host, self.label):
                continue                              # a standby server took the paused site in
            self._check()
            name = server_for(self.pid)
            if name is None and await failover(self.pid, self.host, self.label):
                continue                              # a standby server took over
            if name is None:
                break
            if (route := conn.route(name)) is not None:
                return route
            try:
                route = await conn.start_live(name, self.host)
            except (OSError, Unreachable) as e:
                reason, error = ("unreachable" if isinstance(e, Unreachable) else "broken"), str(e)
                log.warning("VPN %s: server %s %s again: %s", self.pid, name, reason, error)
                mark(self.pid, name, reason)
                await drop(name, t("{server} {reason}", server=name, reason=reason_label(reason)))
                _search(self.pid, reason, error)
                continue
            if route is not None:
                return route
        if _search(self.pid, "none", ""):
            raise VpnWait(t("The VPN is searching a server for {name}", name=self.label))
        why = conn_state().get("error") or error
        raise PlatformError(t("No working VPN server for {name} right now", name=self.label)
                            + (f" ({why})" if why else ""))

    def _retry_due(self) -> bool:
        """A paused site tries the standby servers again every PAUSED_RETRY_S: the search it waits for may be held
        back for an hour (its back-off), and the list changes meanwhile (a search's or "Test again"'s answers, a
        restart that brought a site paused under the old rules)."""
        return time.monotonic() - _retried.get(self.pid, -PAUSED_RETRY_S) >= PAUSED_RETRY_S

    def _check(self) -> None:
        if not source()[1]:
            raise PlatformError(t("The VPN is on for {name}, but no WireGuard config is uploaded (Settings → VPN)",
                                  name=self.label))
        if runner.binary() is None:
            raise PlatformError(t("The VPN is on for {name}, but wireproxy is not installed", name=self.label))
        if (p := paused(self.pid)) is not None:
            if p[0] == "none":
                raise VpnWait(t("No VPN server answers {name} right now; its requests wait for the next search",
                                name=self.label))
            raise VpnWait(t("{name} refused the VPN server {server} ({reason}); its requests wait until the search "
                            "finds another one", name=self.label, server=waits_on(self.pid) or "?",
                            reason=reason_label(p[0])))

    def _concerns(self, route: Route | None) -> bool:
        """`route` is this site's live server now (not an earlier process, not another server since a switch-over),
        and the site is not paused already."""
        return route is not None and conn.is_live(route) and route.name == server_for(self.pid) and not paused(self.pid)

    async def refused(self, route: Route | None, reason: str, detail: str = "") -> bool:
        """A refusal through the site's server, or no answer (`unreachable`): it counts (failed()) once it came CONFIRM
        times in a row over at least CONFIRM_SPAN_S (a 429: no span; CONFIRM_UNREACHABLE times for network errors, no
        span) within
        CONFIRM_WINDOW_S, with no answer the site accepted in between (answered()). Before that the caller asks again through the same server. True when it
        counted and the server was marked."""
        if not self._concerns(route) or (reason == "botcheck" and self.odd()):
            return False
        assert route is not None
        now, key = time.monotonic(), (self.pid, route.name)
        seen = [x for x in _strikes.get(key, []) if now - x < CONFIRM_WINDOW_S] + [now]
        need, span = ((CONFIRM_UNREACHABLE, 0) if reason == "unreachable" else (CONFIRM, 0) if reason == "rate"
                      else (CONFIRM, CONFIRM_SPAN_S))
        if len(seen) < need or seen[-1] - seen[0] < span:
            _strikes[key] = seen
            log.info("VPN %s: server %s %s (%s), %d of %d in a row: asking again", self.pid, route.name, reason,
                     detail or "-", len(seen), need)
            return False
        _strikes.pop(key, None)
        return await self.failed(route, reason, detail)

    def answered(self, route: Route | None) -> None:
        """The site accepted an answer through `route`: its refusals in a row start again from none."""
        if route is not None:
            _strikes.pop((self.pid, route.name), None)

    async def failed(self, route: Route | None, reason: str, detail: str = "") -> bool:
        """The site refused its server (`blocked`, `rate`, `botcheck`) or got no answer through it (`unreachable`):
        mark the server for the site. When the other live server is one the site has not refused, the site moves
        there (untested there until a search asks it; no pause, no search); else to a standby server it answered
        through lately (failover()); else it pauses (its requests wait) and
        asks for a search, whose guards may let it wait; the other sites go on. Nothing for a route that is no
        longer the site's (a switch-over, a dead process: ensure() starts it again), for a site paused already, or
        for the bot checks of a site a search found changed. `detail` ("HTTP 403", the network error) goes to the log
        and the page. True when the server was marked."""
        if not self._concerns(route) or (reason == "botcheck" and self.odd()):
            return False
        assert route is not None
        mark(self.pid, route.name, reason)
        if reason == "unreachable" and not await conn.reach(route.name, self.host):
            # the tunnel itself no longer gets through: the server is down for every site, not only this one.
            # It leaves (drop()), so its sites take the other live server or a standby one (ensure() → failover())
            # and its place is free for a standby server.
            log.warning("VPN %s: server %s does not get through any more (%s), it leaves", self.pid, route.name,
                        detail or "-")
            await drop(route.name, t("{server} {reason}", server=route.name, reason=reason_label(reason)))
            if server_for(self.pid) is not None or await failover(self.pid, self.host, self.label):
                return True
            pause(self.pid, reason, detail)
            _search(self.pid, reason, detail)
            return True
        st = conn_state()
        refused = marks(self.pid)
        if (other := next((s for s in live_servers(st) if s != route.name and s not in refused), None)) is not None:
            update_conn(assign={p: s for p, s in (st.get("assign") or {}).items() if p != self.pid})
            log.warning("VPN %s: server %s %s (%s), moved to %s", self.pid, route.name, reason, detail or "-", other)
            await prune()                        # the refused server stops once no site uses it
            return True
        if (spare := await failover(self.pid, self.host, self.label)) is not None:
            log.warning("VPN %s: server %s %s (%s), moved to standby %s", self.pid, route.name, reason, detail or "-",
                        spare)
            return True
        pause(self.pid, reason, detail)
        log.warning("VPN %s: server %s %s (%s), asking for a search", self.pid, route.name, reason, detail or "-")
        _search(self.pid, reason, detail)
        return True


def describe(detail: str) -> str:
    """A refusal's detail for the page: common network errors in the viewer's language, "HTTP 403" in one piece."""
    low = detail.lower()
    if m := re.fullmatch(r"(?:no connection to|keine Verbindung zu) (\S+)", detail):
        return t("no connection to {host}", host=m.group(1))
    if m := re.fullmatch(r"no handshake in (\d+) s", detail):
        return t("no WireGuard handshake in {s} s", s=m.group(1))
    if "timed out" in low or "timeout" in low:
        return t("no answer in time")
    if "refused" in low:
        return t("connection refused")
    if "reset" in low or "closed" in low:
        return t("connection dropped")
    if "ssl" in low or "tls" in low or "certificate" in low:
        return t("TLS error")
    return detail.replace("HTTP ", "HTTP ")


def reason_label(reason: str) -> str:
    return {"blocked": t("blocked by the site"), "rate": t("rate-limited by the site"),
            "unreachable": t("not reachable"), "broken": t("does not start"),
            "botcheck": t("bot check"), "none": t("no server answers it"),
            "dead": t("no WireGuard handshake")}.get(reason, reason)


def tunnel_for(pid: str) -> Tunnel | None:
    """The site's view of the connections while its VPN switch is on, else None (requests go out directly)."""
    if not enabled(pid):
        return None
    tn = _tunnels.get(pid)
    if tn is None:
        if pid == GEIZHALS:
            host, label = "geizhals.at", "Geizhals"
        else:
            from app import platforms

            try:
                p = platforms.get(pid)
                host, label = p.api_host or p.host, p.name
            except Exception:  # noqa: BLE001 - a client without an adapter (tests)
                host, label = pid, pid
        tn = _tunnels[pid] = Tunnel(pid, host, label)
    return tn


def host_of(pid: str) -> str:
    """The host a server must reach for this site (probed when it starts)."""
    tn = _tunnels.get(pid) or (tunnel_for(pid) if enabled(pid) else None)
    return tn.host if tn is not None else "www.willhaben.at"


async def site_off(pid: str) -> None:
    """A switch went off: that site is no longer paused, and a server no site uses any more stops."""
    unpause(pid)
    await prune()


async def source_changed() -> None:
    """After an upload or removal: the old servers, their marks and the connection state are gone."""
    await conn.disconnect()
    _save({})
    _set_json(CONN_KEY, {})


async def shutdown() -> None:
    """Stop every process, after a start under way (it holds a lock); none starts after this."""
    conn.closing = True
    with suppress(Exception):
        await conn.disconnect()


def reset() -> None:
    """Tests: forget the connections and the tunnels (fakes; nothing to stop)."""
    global _locks_, conn
    _tunnels.clear()
    _strikes.clear()
    _retried.clear()
    guard.reset()
    conn = Connection()
    _locks_ = None


# ------------------------------------------------------------------ status for Settings → VPN and the health check
def _searching() -> tuple[bool, bool]:
    """(a search runs, one comes: pending)."""
    from app.services import vpn_check

    return vpn_check.running(), vpn_check.pending() is not None


def pause_note(p: tuple[str, str], server: str | None = None) -> str:
    """Why a site waits, for its row and its settings page."""
    if p[0] == "none":
        n, _, total = p[1].partition("/")
        return t("Paused: no VPN server answers. The last search checked {n} of {total} EU servers; the next search "
                 "tries again.", n=n or "?", total=total or "?")
    running, coming = _searching()
    reason = reason_label(p[0]) + (f", {describe(p[1])}" if p[1] else "")
    if running or coming:
        return t("Waits for a server: refused {server} ({reason}). The search looks for another one; the other sites "
                 "go on.", server=server or "?", reason=reason)
    return t("Waits for a server: refused {server} ({reason}). Its requests wait for the next search.",
             server=server or "?", reason=reason)


def status() -> dict[str, Any]:
    from app import platforms

    mode, names = source()
    counts: dict[str, int] = {}
    for n in names:
        counts[country(n) or "?"] = counts.get(country(n) or "?", 0) + 1
    st = conn_state()
    servers, assign = live_servers(st), st.get("assign") or {}
    on = users()

    def row(pid: str, name: str) -> dict[str, Any]:
        p, m, is_on = paused(pid), marks(pid), enabled(pid)
        server = (waits_on(pid, st) if p else server_for(pid, st)) if is_on else None
        return {"id": pid, "name": name, "on": is_on, "server": server, "tested": assign.get(pid) in servers,
                "paused": {"kind": "none" if p[0] == "none" else "wait", "reason": reason_label(p[0]),
                           "detail": describe(p[1]) if p[0] != "none" else "",
                           "note": pause_note(p, server)} if p else None,
                "marks": sorted(((n, reason_label(r), until) for n, (until, r) in m.items()), key=lambda x: x[2],
                                reverse=True)}

    sites = [row(p.id, p.name) for p in platforms.all(enabled_only=False)]
    gz = row(GEIZHALS, "Geizhals")
    conns = [{"name": s, "since": since(s, st), "connected": conn.route(s) is not None, "primary": i == 0,
              "sites": [n for pid, n in on if server_for(pid, st) == s and not paused(pid)]}
             for i, s in enumerate(servers)]
    running, _coming = _searching()
    return {"mode": mode, "count": len(names), "eu": eu_count(names) if mode == "collection" else len(names),
            "profile": names[0] if mode == "profile" else None,
            "countries": sorted(counts.items(), key=lambda x: (-x[1], x[0])), "binary": runner.binary() is not None,
            "servers": conns, "server": servers[0] if servers else None, "connected": bool(conn.routes()),
            "searching": running, "error": st.get("error") or "", "sites": sites, "geizhals": gz,
            "any_on": any(s["on"] for s in sites) or gz["on"]}


def health() -> tuple[str, str]:
    """(state, reason) for the connection watchdog: off | ok | problem | unknown. Memory and settings only."""
    on = users()
    if not on:
        return "off", t("Not used")
    if not source()[1]:
        return "problem", t("No WireGuard config uploaded")
    if runner.binary() is None:
        return "problem", t("wireproxy is not installed")
    st = conn_state()
    running, coming = _searching()
    lost = [name for pid, name in on if (p := paused(pid)) and p[0] == "none"]
    waiting = [name for pid, name in on if (p := paused(pid)) and p[0] != "none"]
    if lost:
        return "problem", t("{sites} paused: no VPN server answers them", sites=", ".join(lost))
    if waiting and (running or coming):
        return "unknown", t("Searching a server for {sites}", sites=", ".join(waiting))
    if waiting:
        return "problem", t("{sites} paused: refused {server}", sites=", ".join(waiting),
                            server=" + ".join(live_servers(st)) or "?")
    servers = live_servers(st)
    if servers and conn.routes():
        return "ok", " + ".join(servers)
    if servers:
        return "ok", t("{server}, starts with the next request", server=" + ".join(servers))
    if running:
        return "unknown", t("Searching a server")
    if st.get("error"):
        return "problem", st["error"]
    return "unknown", t("Not used yet")
