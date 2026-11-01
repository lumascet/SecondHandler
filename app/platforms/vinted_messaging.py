"""Vinted messaging over the API: the inbox and its conversations fetched straight from api.vinted.at (curl_cffi,
through the Vinted client's limiter and recorder), using the cookies of the logged-in browser session — instead of
reading them off a hidden browser tab.

The browser session still holds the Vinted login; here we only borrow its cookies (exported from the Playwright
context). No tab is opened. `auth()` returns None when Vinted is not logged in there, and every call returns None on
any failure, so the caller (vinted_inbox) can fall back to the browser read and chat never depends on this alone.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# the session cookies that mark a logged-in Vinted web session (any one is enough to try the API)
AUTH_COOKIES = ("access_token_web", "_vinted_fr_session", "v_sid")


async def auth(sess: Any) -> tuple[str, str | None] | None:
    """(Cookie header, anon_id) from the logged-in session's context, or None when Vinted is not logged in there."""
    from app.platforms.vinted import MSG_API

    ctx = getattr(sess, "context", None)
    if ctx is None:
        return None
    try:
        cookies = await ctx.cookies(MSG_API)
    except Exception as e:  # noqa: BLE001 - no browser / context gone
        log.info("vinted api: cannot read cookies (%s)", type(e).__name__)
        return None
    if not any(c.get("name") in AUTH_COOKIES for c in cookies):
        return None                                   # logged out: let the caller fall back
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if c.get("name") and c.get("value") is not None)
    anon = next((c["value"] for c in cookies if c.get("name") == "anon_id"), None)
    return jar, anon


async def _get(sess: Any, path: str) -> dict[str, Any] | None:
    creds = await auth(sess)
    if creds is None:
        return None
    jar, anon = creds
    from app.platforms import vinted

    try:
        r = await vinted.get_client().messaging_get(path, jar, anon)
    except Exception as e:  # noqa: BLE001 - network / transport: fall back to the browser
        log.info("vinted api %s failed (%s)", path, type(e).__name__)
        return None
    if r.status_code != 200:
        log.info("vinted api %s -> HTTP %s", path, r.status_code)
        return None
    try:
        data = json.loads(r.text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def inbox(sess: Any) -> dict[str, Any] | None:
    """The messaging inbox as the web app's /messaging/main/inbox call returns it, or None to fall back."""
    return await _get(sess, "/messaging/main/inbox")


async def conversation(sess: Any, cid: str) -> dict[str, Any] | None:
    """One conversation (/messaging/main/conversations/<id>), or None to fall back."""
    return await _get(sess, f"/messaging/main/conversations/{cid}")
