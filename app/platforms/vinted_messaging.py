"""Vinted messaging over the API, with no browser tab (option A).

Everything the old live/sync browser tabs did is done here with curl_cffi and the logged-in session's cookies:

  * reads   – GET /messaging/main/inbox and /messaging/main/conversations/<id> on api.vinted.at (through the Vinted
              client's limiter and recorder; the Cookie header is redacted in recordings).
  * push    – a long-lived GET on /messaging/real-time/subscribe (Server-Sent Events) that the web app opens; its
              events ({message_type: refresh_inbox | refresh_conversation, data:{conversation_id}}) drive the reads.
  * refresh – POST www.vinted.at/web/api/auth/refresh to renew the access token, since no tab keeps it alive.

The login has ONE holder at a time: this jar. Vinted rotates the refresh token on every refresh and refuses (HTTP 400)
one that was used already – the session is then gone for everybody. While the browser's Vinted pages refreshed the
same token on their own, whoever came second killed the login (every login lasted a few hours, 11-01/02). So the jar
owns it: it alone refreshes, it is PERSISTED to disk (data/vinted-native.json) so the rotated token survives a
restart, and the browser only borrows it: every page the app opens on Vinted (sending a reply, a listing, an offer,
the login check, a fallback read) runs inside browser_turn(), which gives the browser the jar's current cookies
first, takes back what the page left (its web app may have refreshed them meanwhile) and keeps the jar from
refreshing during the turn. The jar is seeded from the browser only when it has no login (right after the owner
logged in there). A 401 the refresh cannot fix is a logout (pushed by the watchdog); the owner logs in again.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextvars import ContextVar
from typing import Any, AsyncIterator, Callable

log = logging.getLogger(__name__)

AUTH_COOKIES = ("access_token_web", "_vinted_fr_session", "v_sid")   # any one marks a logged-in web session
WEB = "https://www.vinted.at"
REFRESH_URL = f"{WEB}/web/api/auth/refresh"
SSE_URL = "https://sse.vinted.at/messaging/real-time/subscribe"   # the web real-time push host (not api.vinted.at)
STATE_FILE = "vinted-native.json"         # the owned jar persisted to disk (mode 600), like willhaben-native.json
CONNECT_TIMEOUT_S = 20                     # bound on getting the first response when opening the SSE stream
READ_IDLE_S = 40                           # heartbeats arrive ~every 12s; no data this long = a dead stream, reconnect

_jar: dict[str, str] = {}                 # the owned cookie jar (name -> value), the single source of truth
_anon: str | None = None
_loaded = False                           # whether the on-disk jar has been read this process
_owner: asyncio.Lock | None = None         # held by a refresh and by a browser turn: never both at once
_alive = False                            # the jar's login worked lately (an answer, a refresh): only then it is
                                          # handed to the browser – a dead one would overwrite a new login there
_dead: set[str] = set()                   # token values Vinted refused: never taken back from the browser's old cookies
_turn: ContextVar[bool] = ContextVar("vinted_browser_turn", default=False)


def _lock() -> asyncio.Lock:
    global _owner
    if _owner is None:
        _owner = asyncio.Lock()
    return _owner


@contextlib.asynccontextmanager
async def browser_turn(sess: Any) -> AsyncIterator[None]:
    """The browser's turn with the Vinted login: the jar's cookies go into the browser before the page loads, the
    page's cookies come back into the jar after (the web app may have refreshed the token), and no refresh of the jar
    runs in between. Every Vinted page the app opens goes through here; nested turns share the outer one."""
    if _turn.get() or getattr(sess, "context", None) is None:
        yield
        return
    async with _lock():
        tok = _turn.set(True)
        try:
            await _push_to_browser(sess)
            yield
        finally:
            try:
                await seed_from_browser(sess)       # what the page left: a refreshed token, a new datadome cookie
            finally:
                _turn.reset(tok)


async def _push_to_browser(sess: Any) -> None:
    """The jar's values into the browser's Vinted cookies (only when the jar holds a login: an empty jar must not
    overwrite a login the owner just made in the browser)."""
    from app.platforms.vinted import MSG_API

    _load_once()
    if not _logged_in() or not _alive:            # nothing (or a login that stopped working): the browser's stays
        return
    try:
        have = await sess.context.cookies([MSG_API, WEB])
        names = {c["name"] for c in have}
        put = [{**c, "value": _jar[c["name"]]} for c in have if c["name"] in _jar and c.get("value") != _jar[c["name"]]]
        put += [{"name": k, "value": v, "domain": ".vinted.at", "path": "/", "secure": True}
                for k, v in _jar.items() if k in AUTH_COOKIES and k not in names]
        if put:
            await sess.context.add_cookies(put)
    except Exception as e:  # noqa: BLE001 - no browser: the page loads with what it has
        log.info("vinted: cookies not handed to the browser (%s)", type(e).__name__)


def _cookie_header() -> str:
    return "; ".join(f"{k}={v}" for k, v in _jar.items() if v is not None)


def _logged_in() -> bool:
    return any(k in _jar for k in AUTH_COOKIES)


def _state_path():
    from app.config import get_settings

    return get_settings().data_dir / STATE_FILE


def _persist() -> None:
    """Write the jar + anon id to disk (atomic, mode 600) so the rotated refresh token survives a restart — the bug
    that made the browser-free chat fail before was re-seeding the stale browser cookie instead of this."""
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"jar": _jar, "anon": _anon, "dead": sorted(_dead)[-20:]}, f)
        os.replace(tmp, p)
    except OSError as e:  # noqa: BLE001
        log.info("vinted api: could not persist the jar (%s)", type(e).__name__)


def _load_persisted() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def has_auth() -> bool:
    """We hold (or have persisted) a logged-in cookie — even if the login flag wrongly says otherwise."""
    if _logged_in():
        return True
    jar = _load_persisted().get("jar") or {}
    return any(k in jar for k in AUTH_COOKIES)


def _assert_logged_in() -> None:
    """A working API call proves we are logged in: correct the flag if a past failure wrongly cleared it."""
    try:
        from app.services import site_accounts

        if site_accounts.state("vinted").get("logged_in") is not True:
            site_accounts._save("vinted", True)
    except Exception:  # noqa: BLE001
        pass


async def seed_from_browser(sess: Any) -> bool:
    """Fill the jar from the logged-in session's context (the browser holds the Vinted login). False when logged out."""
    global _anon
    from app.platforms.vinted import MSG_API

    ctx = getattr(sess, "context", None)
    if ctx is None:
        return False
    try:
        cookies = await ctx.cookies([MSG_API, WEB])
    except Exception as e:  # noqa: BLE001 - no browser / context gone
        log.info("vinted api: cannot read cookies (%s)", type(e).__name__)
        return False
    jar = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value") is not None}
    if not any(k in jar for k in AUTH_COOKIES):
        return False
    if _dead & {jar.get("access_token_web"), jar.get("refresh_token_web")} - {None}:
        return False                                   # the browser still has the refused login: not taken again
    _jar.clear()
    _jar.update(jar)
    _anon = jar.get("anon_id")
    _persist()
    return True


def _load_once() -> None:
    """The jar persisted last run (its rotated token is the current one), once per process."""
    global _anon, _loaded
    if _loaded:
        return
    _loaded = True
    st = _load_persisted()
    _dead.update(x for x in st.get("dead") or [] if isinstance(x, str))
    if _logged_in():
        return
    jar = st.get("jar")
    if isinstance(jar, dict) and any(k in jar for k in AUTH_COOKIES):
        _jar.update(jar)
        _anon = st.get("anon")


async def _ensure(sess: Any) -> bool:
    """A usable jar: in memory, else the one persisted last run, else (no login held yet: the owner just logged in)
    the browser's."""
    _load_once()
    if _logged_in():
        return True
    return await seed_from_browser(sess)


async def _refresh(sess: Any) -> bool | None:
    """Renew the access token over HTTP and merge the new cookies into the jar. Tri-state: True = refreshed; False =
    the refresh token was genuinely rejected (4xx: the caller may re-seed from the browser); None = a transient
    failure (exception or 5xx) — keep the (still-good) persisted token, do NOT re-seed."""
    if not _jar:
        return False
    if _turn.get():
        return None                                    # the browser holds the login right now: not a second refresh
    async with _lock():                                # never while a browser page has the login
        return await _refresh_locked()


async def _refresh_locked() -> bool | None:
    if not _jar:
        return False
    from curl_cffi.requests import AsyncSession

    headers = {"Accept": "application/json", "Cookie": _cookie_header()}
    if _anon:
        headers["X-Anon-Id"] = _anon
    try:
        async with AsyncSession(impersonate="chrome") as s:
            r = await s.post(REFRESH_URL, headers=headers, timeout=30)
    except Exception as e:  # noqa: BLE001 - transient network error
        log.info("vinted api: refresh failed (%s)", type(e).__name__)
        return None
    if r.status_code // 100 == 5:
        log.info("vinted api: refresh -> HTTP %s (transient)", r.status_code)
        return None
    if r.status_code // 100 != 2:
        log.info("vinted api: refresh -> HTTP %s (rejected)", r.status_code)
        _bury()
        return False
    global _alive
    _alive = True
    got = {c.name: c.value for c in r.cookies.jar}
    if got:
        _jar.update(got)
        _persist()                                     # keep the rotated token across restarts
        log.info("vinted api: token refreshed (%d cookies)", len(got))
    return True


def _bury() -> None:
    """The jar's login was refused for good: its tokens are remembered as dead (the browser keeps the same old cookies
    until the owner logs in again), and it is no longer handed to the browser."""
    global _alive
    _alive = False
    _dead.update(v for v in (_jar.get("access_token_web"), _jar.get("refresh_token_web")) if v)
    _persist()


async def _renew(sess: Any) -> bool | None:
    """A 401: the jar refreshes its token (it is the only holder). Rejected: the jar's login is dead – only a login the
    owner made in the browser since (another token) can replace it. True = renewed, None = a transient refresh
    failure (keep the token), False = logged out."""
    before = _jar.get("access_token_web")
    rr = await _refresh(sess)
    if rr is not False:
        return rr
    _jar.clear()                                       # dead: not handed to the browser, not taken back
    if await seed_from_browser(sess) and _jar.get("access_token_web") not in (None, before):
        return True
    return False


async def _fresh_datadome(sess: Any) -> None:
    """A 403 is the bot protection's cookie: a Vinted page in the browser (a turn: it gets the login and gives the
    new datadome cookie back)."""
    if getattr(sess, "context", None) is None:
        return
    from app.services import site_accounts
    await site_accounts.check("vinted")


async def _mark_logged_out(sess: Any) -> None:
    _bury()
    _jar.clear()
    _persist()                                        # on disk too: the live stream then waits for a new login
    try:
        from app.services import site_accounts

        site_accounts._save("vinted", False)          # the normal browser re-login takes over and re-seeds the jar
    except Exception:  # noqa: BLE001
        pass


async def _get(sess: Any, path: str) -> dict[str, Any] | None:
    """A messaging GET with the owned jar; a 401 renews the token (_renew) and retries, a 403 re-seeds the datadome
    cookie from the browser for the next attempt; else None (the caller falls back)."""
    global _alive
    if not await _ensure(sess):
        return None
    from app.platforms import vinted

    from app.platforms.base import Blocked

    for attempt in range(3):
        try:
            r = await vinted.get_client().messaging_get(path, _cookie_header(), _anon)
        except Blocked:
            # a 403/429 bot-protection refusal: the limiter now pauses Vinted, so an in-call retry cannot go out.
            # A fresh datadome cookie from a browser page (home IP) for the NEXT attempt; the SSE stream, which does
            # not use this limiter, reconnects at once with it.
            await _fresh_datadome(sess)
            return None
        except Exception as e:  # noqa: BLE001 - network/transport
            log.info("vinted api %s failed (%s)", path, type(e).__name__)
            return None
        if r.status_code == 200:
            _alive = True
            _assert_logged_in()
            try:
                data = json.loads(r.text)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
        if r.status_code == 401 and attempt < 2:       # the access token expired (a 401 does not pause the limiter)
            renewed = await _renew(sess)
            if renewed is True:
                continue
            if renewed is None:
                return None                            # transient refresh failure: keep the token, just fail this call
        log.info("vinted api %s -> HTTP %s", path, r.status_code)
        if r.status_code == 401:
            await _mark_logged_out(sess)               # a token the browser could not renew: genuinely logged out
        return None
    return None


async def inbox(sess: Any) -> dict[str, Any] | None:
    return await _get(sess, "/messaging/main/inbox")


async def conversation(sess: Any, cid: str) -> dict[str, Any] | None:
    return await _get(sess, f"/messaging/main/conversations/{cid}")


async def escrow(sess: Any, order_id: str) -> dict[str, Any] | None:
    return await _get(sess, f"/escrow-orders/api/v2/escrow-orders/{order_id}")


# ------------------------------------------------------------------ the SSE push stream
def sse_events(chunk: str, buf: list[str]) -> list[str]:
    """Pure SSE framing: append `chunk` to the pending buffer (`buf` holds one string), return the `data` payloads of
    every complete event (blocks split by a blank line; the `data:` lines joined). Leaves the partial tail in `buf`."""
    buf[0] = (buf[0] if buf else "") + chunk
    out: list[str] = []
    while "\n\n" in buf[0]:
        block, buf[0] = buf[0].split("\n\n", 1)
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            out.append(data)
    return out


async def subscribe(sess: Any, on_event: Callable[[str], Any], *, open_cb: Callable[[int], Any] | None = None) -> int:
    """Open the SSE stream once and dispatch each event's data to `on_event`. Returns the HTTP status (200 while it
    streamed, or the refusal). Raises nothing: network errors return 0."""
    global _alive
    from curl_cffi.requests import AsyncSession

    if not await _ensure(sess):
        return 401
    url = SSE_URL
    for attempt in range(2):                           # a refused stream renews the token once, then reconnects
        headers = {"Accept": "text/event-stream", "Cookie": _cookie_header(), "Origin": WEB,
                   "Referer": f"{WEB}/", "Locale": "de-AT", "Platform": "web", "X-Next-App": "marketplace-web"}
        if _anon:
            headers["X-Anon-Id"] = _anon
        buf = [""]
        try:
            async with AsyncSession(impersonate="chrome") as s:
                cm = s.stream("GET", url, headers=headers, timeout=None)   # no total timeout: the stream is long-lived
                try:
                    r = await asyncio.wait_for(cm.__aenter__(), CONNECT_TIMEOUT_S)   # but bound the connect itself
                except Exception as e:  # noqa: BLE001 - timeout or connection error: reconnect
                    log.info("vinted sse: connect failed (%s)", type(e).__name__)
                    return 0
                try:
                    if r.status_code == 401 and attempt == 0 and await _renew(sess) is True:
                        continue                       # an expired token: refreshed (or a new one from the browser)
                    if r.status_code == 403 and attempt == 0:
                        # 403 is the bot-protection cookie (datadome), not a dead token: a fresh one from a browser
                        # page (home IP), else the token refresh
                        before = _jar.get("datadome")
                        await _fresh_datadome(sess)
                        if _jar.get("datadome") != before or await _refresh(sess) is True:
                            continue                   # a new datadome cookie or token: reconnect
                        # the browser is logged out and the refresh was transient: back off and retry later
                    if open_cb:
                        await _maybe_await(open_cb(r.status_code))
                    if r.status_code != 200:
                        return r.status_code
                    _alive = True                  # the stream opened with the jar: its login works
                    it = r.aiter_content()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(it.__anext__(), READ_IDLE_S)   # heartbeats keep it alive
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            log.info("vinted sse: no data within %ds, reconnecting", READ_IDLE_S)
                            break
                        text = chunk.decode("utf-8", "replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                        for data in sse_events(text, buf):
                            await _maybe_await(on_event(data))
                finally:
                    with contextlib.suppress(Exception):
                        await cm.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001 - the stream dropped; the caller reconnects
            log.info("vinted sse: stream ended (%s)", type(e).__name__)
            return 0
        return 200
    return 401


async def _maybe_await(v: Any) -> None:
    if hasattr(v, "__await__"):
        await v
