"""Vinted messaging over the API, with no browser tab (option A).

Everything the old live/sync browser tabs did is done here with curl_cffi and the logged-in session's cookies:

  * reads   – GET /messaging/main/inbox and /messaging/main/conversations/<id> on api.vinted.at (through the Vinted
              client's limiter and recorder; the Cookie header is redacted in recordings).
  * push    – a long-lived GET on /messaging/real-time/subscribe (Server-Sent Events) that the web app opens; its
              events ({message_type: refresh_inbox | refresh_conversation, data:{conversation_id}}) drive the reads.
  * refresh – POST www.vinted.at/web/api/auth/refresh to renew the access token, since no tab keeps it alive.

The cookies are an owned jar: seeded once from the browser's Playwright context (the Vinted login lives there), then
kept fresh by the refresh call. A 401 that refresh cannot fix marks Vinted logged out, so the normal browser re-login
(site_accounts) recovers it and re-seeds the jar — the browser is touched only for login, never for everyday chat.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

AUTH_COOKIES = ("access_token_web", "_vinted_fr_session", "v_sid")   # any one marks a logged-in web session
WEB = "https://www.vinted.at"
REFRESH_URL = f"{WEB}/web/api/auth/refresh"

_jar: dict[str, str] = {}                 # the owned cookie jar (name -> value), the single source of truth
_anon: str | None = None


def _cookie_header() -> str:
    return "; ".join(f"{k}={v}" for k, v in _jar.items() if v is not None)


def _logged_in() -> bool:
    return any(k in _jar for k in AUTH_COOKIES)


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
    return True


async def _ensure(sess: Any) -> bool:
    return True if _logged_in() else await seed_from_browser(sess)


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
                async with s.stream("GET", url, headers=headers, timeout=None) as r:
                    if r.status_code in (401, 403) and attempt == 0 and await _refresh(sess):
                        continue                       # token renewed: reconnect with the fresh cookies
                    if open_cb:
                        await _maybe_await(open_cb(r.status_code))
                    if r.status_code != 200:
                        return r.status_code
                    async for chunk in r.aiter_content():
                        text = chunk.decode("utf-8", "replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                        for data in sse_events(text, buf):
                            await _maybe_await(on_event(data))
        except Exception as e:  # noqa: BLE001 - the stream dropped; the caller reconnects
            log.info("vinted sse: stream ended (%s)", type(e).__name__)
            return 0
        return 200
    return 401


async def _maybe_await(v: Any) -> None:
    if hasattr(v, "__await__"):
        await v
