"""Kleinanzeigen messages through the web inbox's own API, called from inside a hidden tab of the logged-in browser.

The inbox page (m-nachrichten.html, its bundle messagebox-web) works on two calls, seen 2026-11-04:

  GET  www.kleinanzeigen.de/m-access-token.json      the login cookies -> a short-lived token in the answer's
                                                     `authorization` header, {"expiration": <ms>} as its body
  GET  www.kleinanzeigen.de/messagebox-api/view      {"user": {"id", ...}}: the account's user id
  GET  gateway.kleinanzeigen.de/messagebox/api/users/<uid>/conversations?page=0&size=N   the inbox
  GET  .../conversations/<cid>?contentWarnings=true  one conversation with its messages
  POST .../conversations/<cid>  {"message": text}    a reply (the bundle's MessageboxApi.sendReply)

with the token as Authorization and X-ECG-USER-AGENT: messagebox-1. The calls run as the page's own fetch() in a tab
of the app's browser (Box): Kleinanzeigen's bot protection lets the real browser through, the login stays in the
browser alone (one holder, unlike Vinted's jar there is no copy to fall out of step) and nothing new is sent that the
web app would not send itself. The browser is the source of the login: logged out there (the token call is refused
or ends on the login page) is logged out here (site_accounts).
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

ORIGIN = "https://www.kleinanzeigen.de"
GATEWAY = "https://gateway.kleinanzeigen.de"
LANDING = f"{ORIGIN}/m-access-token.json"     # a small page of the site: the tab's origin for the page's own fetch()
INBOX_SIZE = 50

_JS_GATEWAY = """async ([method, path, body]) => {
  try {
    if (!window.__kaTok || !window.__kaTok.token || window.__kaTok.exp < Date.now() + 30000) {
      const r = await fetch('/m-access-token.json', {credentials: 'include', headers: {Accept: 'application/json'}});
      if (!r.ok) return {status: r.status, text: '', where: 'token'};
      const j = await r.json().catch(() => ({}));
      window.__kaTok = {token: r.headers.get('authorization'), exp: Number(j.expiration) || 0};
      if (!window.__kaTok.token) return {status: 401, text: '', where: 'token'};
    }
    const h = {Accept: 'application/json', Authorization: window.__kaTok.token, 'X-ECG-USER-AGENT': 'messagebox-1'};
    if (body !== null) h['Content-Type'] = 'application/json';
    const r = await fetch('""" + GATEWAY + """' + path, {method, headers: h, mode: 'cors', credentials: 'include',
                                                        body: body === null ? undefined : JSON.stringify(body)});
    return {status: r.status, text: await r.text(), where: 'gateway'};
  } catch (e) { return {status: 0, text: String(e), where: 'error'}; }
}"""

_JS_SAME_ORIGIN = """async (path) => {
  try {
    const r = await fetch(path, {credentials: 'include', headers: {Accept: 'application/json'}});
    return {status: r.status, text: await r.text()};
  } catch (e) { return {status: 0, text: String(e)}; }
}"""


class MessagingError(RuntimeError):
    pass


class LoggedOut(MessagingError):
    pass


class Box:
    """A hidden tab of the logged-in browser on kleinanzeigen.de; its calls are the page's own (async with)."""

    def __init__(self) -> None:
        self.page = None
        self.user_id: str | None = None

    async def __aenter__(self) -> Box:
        from app.willhaben.session import get_session

        sess = get_session()
        sess._require()
        ui = sess.ui_page
        sess._creating_internal = True
        try:
            self.page = await sess.context.new_page()
        finally:
            sess._creating_internal = False
            sess.ui_page = ui
        sess.working_pages.add(self.page)
        try:
            await self.page.goto(LANDING, wait_until="domcontentloaded", timeout=30000)
            if _login_page(self.page.url):
                raise LoggedOut("not logged in to Kleinanzeigen")
            self.user_id = await self._user_id()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.page is not None:
            from app.willhaben.session import get_session
            get_session().working_pages.discard(self.page)
            try:
                await self.page.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            self.page = None

    async def _user_id(self) -> str:
        res = await self.page.evaluate(_JS_SAME_ORIGIN, "/messagebox-api/view")
        if res.get("status") in (401, 403):
            raise LoggedOut("not logged in to Kleinanzeigen")
        try:
            uid = (json.loads(res.get("text") or "{}").get("user") or {}).get("id")
        except ValueError:
            uid = None
        if not uid:
            raise LoggedOut("Kleinanzeigen did not name the account (HTTP %s)" % res.get("status"))
        return str(uid)

    async def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """One call of the messagebox API from the page; (status, the answer's JSON or None). LoggedOut when the token
        is refused."""
        res = await self.page.evaluate(_JS_GATEWAY, [method, path, body])
        status = int(res.get("status") or 0)
        if res.get("where") == "token" and status in (0, 401, 403):
            raise LoggedOut(f"Kleinanzeigen refused the token (HTTP {status})")
        if status in (401, 403):
            raise LoggedOut(f"Kleinanzeigen refused the call (HTTP {status})")
        text = res.get("text") or ""
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        return status, data

    async def inbox(self, size: int = INBOX_SIZE) -> dict[str, Any]:
        status, data = await self.call("GET", f"/messagebox/api/users/{self.user_id}/conversations?page=0&size={size}")
        if status != 200 or not isinstance(data, dict):
            raise MessagingError(f"Kleinanzeigen inbox: HTTP {status}")
        return data

    async def conversation(self, cid: str) -> dict[str, Any]:
        status, data = await self.call("GET", f"/messagebox/api/users/{self.user_id}/conversations/{cid}"
                                              "?contentWarnings=true")
        if status != 200 or not isinstance(data, dict):
            raise MessagingError(f"Kleinanzeigen conversation: HTTP {status}")
        return data

    async def reply(self, cid: str, text: str) -> tuple[int, Any]:
        return await self.call("POST", f"/messagebox/api/users/{self.user_id}/conversations/{cid}", {"message": text})


def _login_page(url: str) -> bool:
    return any(p in (url or "") for p in ("m-einloggen", "/login", "login.kleinanzeigen.de"))
