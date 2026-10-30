"""The Mini App's actions are the dashboard's: a /tg page posts its forms to the dashboard's own endpoints (one place for
the logic) with the header X-TG-Back (the /tg page it came from, set by templates/tg/_base.html). This ASGI middleware
lets the endpoint answer as to a plain form (htmx headers removed: no fragment, the message in the redirect) and turns
the answer into a redirect to the Mini App:

- a redirect to a dashboard page goes to its /tg page (tg_path: a new chat, ad or draft opens there), else back to the
  page it came from; the dashboard's message (?msg=) comes along, the Mini App shows it at the top;
- a page or fragment (200) goes back to the page it came from;
- an error goes back with its text as the message.

A redirect to the login page passes as it is.

Some endpoints answer with a page instead of a redirect (Claude's proposal on the edit page, a search result). For those
the dashboard template has a twin under templates/tg/ (TWINS): a request from the Mini App renders the twin with the
same context, and the bridge passes that page through (its address set to the /tg page it came from). A GET from the
Mini App to a dashboard fragment (loaded into a /tg page) gets the fragment's twin the same way.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HEADER = b"x-tg-back"
# dashboard template -> its Mini App twin (rendered for requests from the Mini App, see the module doc)
TWINS: dict[str, str] = {
    "myad_edit.html": "tg/myad_edit.html",
    "_myad_description.html": "tg/_myad_description.html",
    # an ad text's translation, loaded into the Mini App's ad page: the fragment itself (inline styles), never a
    # way back – that page came back into its own description, again and again (10-30)
    "_ad_translation.html": "_ad_translation.html",
}


def install(app: Any, templates: Any) -> None:
    """The middleware, and the twin templates for requests from the Mini App (TemplateResponse of `templates`)."""
    original = templates.TemplateResponse

    def response(request, name, context=None, *args, **kwargs):
        twin = TWINS.get(name)
        if twin and request.scope.get("tg_back") is not None:
            request.scope["tg_twin"] = True
            name, context = twin, {**(context or {}), "embed": True}
        return original(request, name, context, *args, **kwargs)

    templates.TemplateResponse = response
    app.add_middleware(TgBridge)
_DROP = {b"hx-request", b"hx-boosted", b"hx-target", b"hx-trigger", b"hx-trigger-name", b"hx-current-url",
         b"hx-prompt"}


def tg_path(url: str) -> str | None:
    """The Mini App page for a dashboard address (path with query), None when it has none."""
    parts = urlsplit(url)
    path, q = parts.path.rstrip("/") or "/", parse_qs(parts.query)
    one = {k: v[0] for k, v in q.items() if k != "msg"}
    if path.startswith("/tg"):
        return url
    if m := re.fullmatch(r"/chats/([^/]+)", path):
        return f"/tg/chats/{m.group(1)}"
    if path == "/chats":
        return "/tg/chats"
    if path == "/hits":                     # the view stays (tab, agent, site)
        if one.get("view"):
            return f"/tg/ad/{one['view']}"
        keep = {k: one[k] for k in ("state", "agent_id", "platform", "sort", "ship", "level") if one.get(k)}
        if keep.get("state") not in (None, "new", "saved", "filtered", "seen", "ignored"):
            keep.pop("state")
        return "/tg/hits" + (f"?{urlencode(keep)}" if keep else "")
    if path == "/myads/cleanup":
        return "/tg/myads/cleanup"
    if m := re.fullmatch(r"/myads/([^/]+)/edit", path):
        return f"/tg/myads/{m.group(1)}"
    if path == "/myads":
        return f"/tg/myads/{one['ad']}" if one.get("ad") else "/tg/myads"
    if m := re.fullmatch(r"/sold/([^/]+)", path):
        keep = {k: one[k] for k in ("kind", "buyer") if one.get(k)}
        return f"/tg/sold/{m.group(1)}" + (f"?{urlencode(keep)}" if keep else "")
    if m := re.fullmatch(r"/purchases/(\d+)", path):
        return f"/tg/purchases/{m.group(1)}"
    if path == "/purchases":
        return "/tg/purchases"
    if m := re.fullmatch(r"/purchases/new/([^/]+)", path):
        return f"/tg/purchases/new/{m.group(1)}"
    if path in ("/agents", "/agents/new"):
        return "/tg/agents"
    if m := re.fullmatch(r"/new-ad/(\d+)", path):
        return f"/tg/new-ad/{m.group(1)}"
    if m := re.fullmatch(r"/new-ad/batch/(\d+)", path):
        return f"/tg/new-ad/batch/{m.group(1)}"
    if path == "/new-ad":
        return "/tg/new-ad"
    if m := re.fullmatch(r"/deals/search/(\d+)", path):
        return f"/tg/search/{m.group(1)}"
    if path in ("/deals", "/search"):
        return "/tg/search" + (f"?{urlencode(one)}" if one else "")
    if m := re.fullmatch(r"/(?:deals|hits)/ad/(?:([a-z_]+)/)?([^/]+)", path):
        return f"/tg/check/{m.group(1) or 'willhaben'}/{m.group(2)}"
    if path == "/":
        return "/tg"
    return None


def web_url(tg: str) -> str:
    """The dashboard page a /tg page stands for, as the Referer the dashboard's endpoints read (My ads keeps the open
    ad from it); the /tg address itself when there is none."""
    path = urlsplit(tg).path
    if m := re.fullmatch(r"/tg/myads/([^/]+)(?:/edit)?", path):
        return f"/myads?ad={m.group(1)}"
    if m := re.fullmatch(r"/tg/chats/([^/]+)(?:/info)?", path):
        return f"/chats/{m.group(1)}"
    return tg


def target(location: str, back: str, msg: str = "", path: str = "") -> str:
    """Where the Mini App goes after an action (POSTed to `path`) that answered with `location` (or nothing): its /tg
    page or `back`, with the message. An agent's page stays open after its own actions (the dashboard goes back to
    its list), except after deleting it."""
    parts = urlsplit(location) if location else None
    if parts and not msg:       # the dashboard's message; agents report a refusal as ?error=
        q = parse_qs(parts.query)
        msg = (q.get("msg") or q.get("error") or [""])[0]
        if not msg and q.get("pn"):         # a purchase order's notice, kept on the server under a token
            from app.purchase_routes import _notice_text
            msg = _notice_text(q["pn"][0]) or ""
    page = (tg_path(location) if location else None) or back
    if "?" not in page and back.split("?")[0] == page:          # the same list: its filters stay
        page = back
    if back.split("?")[0] == page.split("?")[0] + "/info":     # a chat's details stay open after their actions
        page = back
    if page == "/tg/agents" and re.fullmatch(r"/tg/agents/\d+", back.split("?")[0]) and not path.endswith("/delete"):
        page = back
    if not msg:
        return page
    base, _, anchor = page.partition("#")
    return f"{base}{'&' if '?' in base else '?'}msg={quote(msg, safe='')}" + (f"#{anchor}" if anchor else "")


def _error_text(body: bytes, status: int) -> str:
    try:
        detail = json.loads(body).get("detail")
        if isinstance(detail, str) and detail:
            return detail
    except (ValueError, AttributeError):
        pass
    from app.i18n import t
    return t("That did not work (error {code})", code=status)


class TgBridge:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        back = headers.get(HEADER, b"").decode("latin-1")
        if not back.startswith("/tg") or scope["path"].startswith("/tg"):
            return await self.app(scope, receive, send)
        if scope["method"] != "POST":           # a fragment for a /tg page: its twin, as it is
            return await self.app({**scope, "tg_back": back}, receive, send)
        scope = {**scope, "tg_back": back,
                 "headers": [*((k, v) for k, v in scope["headers"] if k not in _DROP and k not in (HEADER, b"referer")),
                             (b"referer", web_url(back).encode("latin-1", "replace"))]}
        state: dict[str, Any] = {"status": 0, "location": "", "body": b"", "pass": False}

        async def capture(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["location"] = dict(message.get("headers") or []).get(b"location", b"").decode("latin-1")
                if scope.get("tg_twin") and message["status"] < 300:      # the twin page: shown as it is
                    state["pass"] = True
                    await send({**message, "headers": [*message.get("headers", []),
                                                       (b"hx-replace-url", back.encode("latin-1", "replace"))]})
            elif message["type"] == "http.response.body":
                if state["pass"]:
                    await send(message)
                elif state["status"] >= 400 and len(state["body"]) < 20000:
                    state["body"] += message.get("body", b"")

        await self.app(scope, receive, capture)
        if state["pass"]:
            return
        status, location = state["status"], state["location"]
        if 300 <= status < 400 and urlsplit(location).path.startswith("/login"):
            to = location
        elif status >= 400:
            to = target("", back, _error_text(state["body"], status))
        else:
            to = target(location if 300 <= status < 400 else "", back, path=scope["path"])
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", to.encode("latin-1", "replace")), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
