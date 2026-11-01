"""Vinted messaging over the API, with no browser tab (option A).

Everything the old live/sync browser tabs did is done here with curl_cffi and the logged-in session's cookies:

  * reads   – GET /messaging/main/inbox and /messaging/main/conversations/<id> on api.vinted.at (through the Vinted
              client's limiter and recorder; the Cookie header is redacted in recordings).
  * push    – a long-lived GET on /messaging/real-time/subscribe (Server-Sent Events) that the web app opens; its
              events ({message_type: refresh_inbox | refresh_conversation, data:{conversation_id}}) drive the reads.
  * refresh – POST www.vinted.at/web/api/auth/refresh to renew the access token, since no tab keeps it alive.

The cookies are an owned jar: seeded from the browser's Playwright context (the Vinted login lives there), kept fresh
by the refresh call, and PERSISTED to disk (data/vinted-native.json, like willhaben-native.json) so the rotated
refresh token survives a restart — on start the jar is loaded from disk, not re-seeded from the (by then stale)
browser cookie. A successful call re-asserts the login flag; a 401 that neither refresh nor a browser re-seed can fix
falls back to the browser tab and leaves re-login to site_accounts. The browser is touched only for login.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, Callable

log = logging.getLogger(__name__)

AUTH_COOKIES = ("access_token_web", "_vinted_fr_session", "v_sid")   # any one marks a logged-in web session
WEB = "https://www.vinted.at"
REFRESH_URL = f"{WEB}/web/api/auth/refresh"
STATE_FILE = "vinted-native.json"         # the owned jar persisted to disk (mode 600), like willhaben-native.json
CONNECT_TIMEOUT_S = 20                     # bound on getting the first response when opening the SSE stream
READ_IDLE_S = 40                           # heartbeats arrive ~every 12s; no data this long = a dead stream, reconnect

_jar: dict[str, str] = {}                 # the owned cookie jar (name -> value), the single source of truth
_anon: str | None = None
_loaded = False                           # whether the on-disk jar has been read this process


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
            json.dump({"jar": _jar, "anon": _anon}, f)
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
    _jar.clear()
    _jar.update(jar)
    _anon = jar.get("anon_id")
    _persist()
    return True


async def _ensure(sess: Any) -> bool:
    """A usable jar: in memory, else the one persisted last run (its rotated token is current), else the browser."""
    global _anon, _loaded
    if _logged_in():
        return True
    if not _loaded:
        _loaded = True
        jar = _load_persisted().get("jar")
        if isinstance(jar, dict) and any(k in jar for k in AUTH_COOKIES):
            _jar.update(jar)
            _anon = _load_persisted().get("anon")
            return True
    return await seed_from_browser(sess)


async def _refresh(sess: Any) -> bool:
    """Renew the access token over HTTP and merge the new cookies into the jar. False when it cannot be renewed."""
    if not _jar:
        return False
    from curl_cffi.requests import AsyncSession

    headers = {"Accept": "application/json", "Cookie": _cookie_header()}
    if _anon:
        headers["X-Anon-Id"] = _anon
    try:
        async with AsyncSession(impersonate="chrome") as s:
            r = await s.post(REFRESH_URL, headers=headers, timeout=30)
    except Exception as e:  # noqa: BLE001
        log.info("vinted api: refresh failed (%s)", type(e).__name__)
        return False
    if r.status_code // 100 != 2:
        log.info("vinted api: refresh -> HTTP %s", r.status_code)
        return False
    got = {c.name: c.value for c in r.cookies.jar}
    if got:
        _jar.update(got)
        _persist()                                     # keep the rotated token across restarts
        log.info("vinted api: token refreshed (%d cookies)", len(got))
    return True


async def _mark_logged_out(sess: Any) -> None:
    _jar.clear()
    try:
        from app.services import site_accounts

        site_accounts._save("vinted", False)          # the normal browser re-login takes over and re-seeds the jar
    except Exception:  # noqa: BLE001
        pass


async def _get(sess: Any, path: str) -> dict[str, Any] | None:
    """A messaging GET with the owned jar; one refresh + one re-seed retry on 401, then None (caller falls back)."""
    if not await _ensure(sess):
        return None
    from app.platforms import vinted

    for attempt in range(3):
        try:
            r = await vinted.get_client().messaging_get(path, _cookie_header(), _anon)
        except Exception as e:  # noqa: BLE001 - network/transport
            log.info("vinted api %s failed (%s)", path, type(e).__name__)
            return None
        if r.status_code == 200:
            _assert_logged_in()
            try:
                data = json.loads(r.text)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
        if r.status_code in (401, 403) and attempt == 0 and await _refresh(sess):
            continue                                   # refreshed the token: try again
        if r.status_code in (401, 403) and attempt <= 1 and await seed_from_browser(sess):
            continue                                   # re-seed from the browser (it may have re-logged in)
        log.info("vinted api %s -> HTTP %s", path, r.status_code)
        if r.status_code in (401, 403):
            await _mark_logged_out(sess)
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
    from curl_cffi.requests import AsyncSession

    from app.platforms.vinted import MSG_API

    if not await _ensure(sess):
        return 401
    url = f"{MSG_API}/messaging/real-time/subscribe"
    for attempt in range(2):                           # a refused stream renews the token once, then reconnects
        headers = {"Accept": "text/event-stream", "Cookie": _cookie_header()}
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
                    if r.status_code in (401, 403) and attempt == 0 and await _refresh(sess):
                        continue                       # token renewed: reconnect with the fresh cookies
                    if open_cb:
                        await _maybe_await(open_cb(r.status_code))
                    if r.status_code != 200:
                        return r.status_code
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
