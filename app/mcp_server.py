"""MCP server (stdio) that exposes the running SecondHandler to Claude Desktop / Claude Code.

It is a thin client of the agent's JSON API, so it can run on any machine:

  WH_AGENT_URL=http://homeserver:8010 WH_AGENT_PASSWORD=... python -m app.mcp_server

With the API token of Settings → AI agent (docs/agent-api.md) instead of the dashboard password, WH_AGENT_TOKEN=... is
sent as `Authorization: Bearer` (it opens the JSON API only, which is all this server uses).

The same tools are also served by the running app itself over HTTP at /mcp (app/mcp_http.py), no local process needed:

  claude mcp add --transport http secondhandler https://<host>/mcp --header "Authorization: Bearer <token>"

Claude Code:   claude mcp add willhaben -e WH_AGENT_URL=http://homeserver:8010 -- uv --directory <repo> run python -m app.mcp_server
Claude Desktop: see README (claude_desktop_config.json).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app import mcp_http

# WH_HELPER_* are the pre-rename names; still accepted so existing MCP configs keep working.
BASE = (os.environ.get("WH_AGENT_URL") or os.environ.get("WH_HELPER_URL") or "http://127.0.0.1:8000").rstrip("/")
_PW = os.environ.get("WH_AGENT_PASSWORD") or os.environ.get("WH_HELPER_PASSWORD")
AUTH = (os.environ.get("WH_AGENT_USER") or os.environ.get("WH_HELPER_USER") or "admin", _PW) if _PW else None
_TOKEN = os.environ.get("WH_AGENT_TOKEN")
HEADERS = {"x-wh-client-kind": "mcp", **({"authorization": f"Bearer {_TOKEN}"} if _TOKEN else {})}   # timeline: actor "mcp"

mcp = MCPServer(
    "secondhandler",
    instructions=(
        "Access to your SecondHandler: search on willhaben.at and the other second-hand sites it knows "
        "(list_platforms), your search agents and their hits, willhaben chats with reply drafts, your own ads. "
        "Messages are only sent with send_reply; always ask for confirmation of the exact text first. "
        "To follow what happens: the notifications of the app are events (purchases, sales, messages, hits, …); "
        "call get_events with the `since` of the last call's next_since to see only what is new. Purchases and sales "
        "come with item, category and price: list_purchases / get_purchase for what is bought through a purchase "
        "order (and those made directly in a buyer chat with PayLivery), list_sales for your own items sold. "
        "get_settings / update_settings read and change the app's settings; ask the user before changing one unless "
        "they told you to."
    ),
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


async def _call(method: str, path: str, **kw: Any) -> Any:
    """One request to the JSON API. Served from the app (/mcp) it goes straight into the app in-process, with the
    credentials the /mcp request brought; as stdio server it is an HTTP client of WH_AGENT_URL."""
    local = mcp_http.current.get()
    if local:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=local.app), base_url="http://app", timeout=300,
                                   headers={**local.headers, "x-wh-client-kind": "mcp"})   # timeline: actor "mcp"
    else:
        client = httpx.AsyncClient(base_url=BASE, auth=None if _TOKEN else AUTH, timeout=300, headers=HEADERS)
    async with client as c:
        r = await c.request(method, path, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if r.status_code >= 400:
        return {"error": r.status_code, "detail": body}
    return body


@mcp.tool(annotations=READ)
async def status() -> dict:
    """Login status of the willhaben session, unread messages and new hits."""
    return await _call("GET", "/api/status")


@mcp.tool(annotations=READ)
async def search_willhaben(keyword: str, price_from: float | None = None, price_to: float | None = None,
                           rows: int = 20, sort: int = 1) -> dict:
    """Live search on the willhaben marketplace. sort: 1 newest, 3 price ascending, 4 price descending, 7 relevance."""
    params = {"keyword": keyword, "rows": rows, "sort": sort}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    return await _call("GET", "/api/search", params=params)


@mcp.tool(annotations=READ)
async def list_platforms() -> list:
    """The second-hand sites the agent knows (willhaben, kleinanzeigen, …), whether each is switched on and what it can do."""
    return await _call("GET", "/api/platforms")


@mcp.tool(annotations=READ)
async def search_listings(platform: str, keyword: str, price_from: float | None = None, price_to: float | None = None,
                          rows: int = 20, sort: int = 1) -> dict:
    """Live keyword search on one site (platform id from list_platforms). sort: 1 newest, 3 price ascending,
    4 price descending, 7 relevance."""
    params = {"platform": platform, "keyword": keyword, "rows": rows, "sort": sort}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    return await _call("GET", "/api/search", params=params)


@mcp.tool(annotations=READ)
async def get_ad(ad_id: str, platform: str = "willhaben") -> dict:
    """Details of an ad (title, price, description, location, seller); `platform` for sites other than willhaben."""
    return await _call("GET", f"/api/ads/{platform}/{ad_id}")


@mcp.tool(annotations=READ)
async def list_search_agents() -> list:
    """All of your search agents with interval and last run."""
    return await _call("GET", "/api/agents")


@mcp.tool()
async def create_search_agent(url: str | None = None, name: str | None = None, interval_minutes: int = 5,
                              platform: str | None = None, keyword: str | None = None, price_from: float | None = None,
                              price_to: float | None = None, willhaben_url: str | None = None,
                              sites: list[str] | None = None) -> dict:
    """Create a search agent from a result-list URL of any supported site (set the filters in the browser first),
    or from a keyword with an optional price range on one platform (list_platforms). `sites`: one agent watching
    several platforms (the URL, if given, is the main search; the other sites get its keyword)."""
    return await _call("POST", "/api/agents",
                       json={"url": url or willhaben_url, "platform": platform or "willhaben", "keyword": keyword,
                             "price_from": price_from, "price_to": price_to, "name": name,
                             "interval_s": interval_minutes * 60, "sites": sites})


@mcp.tool(annotations=READ)
async def list_hits(agent_id: int | None = None, state: str = "new", limit: int = 20) -> list:
    """Hits of the search agents. state: new, saved, seen, ignored, filtered (held back by the screening) or all."""
    params: dict[str, Any] = {"state": state, "limit": limit}
    if agent_id:
        params["agent_id"] = agent_id
    return await _call("GET", "/api/hits", params=params)


@mcp.tool(annotations=READ)
async def list_conversations() -> list:
    """willhaben chats (as last synced) with partner, ad, last message and unread count."""
    return await _call("GET", "/api/chats")


@mcp.tool(annotations=READ)
async def get_conversation(conversation_id: str) -> dict:
    """One conversation with ad context, message history and existing drafts."""
    return await _call("GET", f"/api/chats/{conversation_id}")


@mcp.tool()
async def draft_reply(conversation_id: str, hint: str | None = None) -> dict:
    """Have the agent write a reply draft (uses your rules from the settings). Sends nothing."""
    return await _call("POST", f"/api/chats/{conversation_id}/draft", json={"hint": hint})


@mcp.tool()
async def save_reply_draft(conversation_id: str, text: str) -> dict:
    """Store a reply text you wrote yourself as a draft in the dashboard. Sends nothing."""
    return await _call("POST", f"/api/chats/{conversation_id}/drafts", json={"text": text})


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
async def send_reply(draft_id: int, text: str) -> dict:
    """Sends a draft as a willhaben message. Only call this after the text has been explicitly confirmed."""
    return await _call("POST", f"/api/drafts/{draft_id}/send", json={"text": text})


@mcp.tool(annotations=READ)
async def list_my_ads() -> list:
    """Your own monitored ads with status, expiry date and auto-reactivation; `stock` holds quantity, sold and
    remaining pieces of an ad with several pieces (None for a single item)."""
    return await _call("GET", "/api/myads")


@mcp.tool(annotations=READ)
async def get_events(since: int | None = None, limit: int = 50, kind: str | None = None) -> dict:
    """The app's notifications as structured events, oldest first: {"events": [...], "next_since": id}. Each event has
    id, ts, kind, title, message, url, app_url, ad_id, conversation_id and `data` with the kind's fields. Pass the
    previous call's next_since as `since` to get only what is new; without `since` you get the latest `limit`.
    `kind` filters, comma-separated: purchase (purchase orders: stage status = every status change incl. paid/
    shipped/done, announced = auto-buy countdown, pay_now, bought, failed, update; item, category, prices), sale (own items sold: price, pieces, buyer, category), paylivery, shipping, message, hit,
    price_drop, meetup, ad_expiring, … Purchases made directly on a site outside a purchase order are not events."""
    params: dict[str, Any] = {"limit": limit}
    if since is not None:
        params["since"] = since
    if kind:
        params["kind"] = kind
    return await _call("GET", "/api/events", params=params)


@mcp.tool(annotations=READ)
async def list_purchases(status: str | None = None, active: bool | None = None, limit: int = 20,
                         source: str | None = None) -> list:
    """Your purchase orders (Kaufauftrag) of this app, the latest change first: what was bought or is being bought,
    with ad title and link, category, status, mode (paylivery, direct, pickup), asking/maximum/agreed price, shipping,
    fee and total in euro. status: new, contacting, negotiating, agreed, meetup, pay_now, paid, shipped, done, paused,
    failed, cancelled. active=true: only open orders, false: only finished ones. Also included are
    purchases you made directly on willhaben with PayLivery in a buyer chat (source "chat", id null, status = the
    PayLivery stage); purchases without PayLivery are not. source: "order" lists only purchase orders, "chat" only
    those from a chat."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if active is not None:
        params["active"] = int(active)
    if source:
        params["source"] = source
    return await _call("GET", "/api/purchases", params=params)


@mcp.tool(annotations=READ)
async def get_purchase(purchase_id: int) -> dict:
    """One purchase order with everything list_purchases has plus its log (what happened when, oldest first), the
    questions for the seller and their answers, the pickup appointment, payment details (direct mode), the market
    prices at the start and the conversation id (get_conversation)."""
    return await _call("GET", f"/api/purchases/{purchase_id}")


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
async def cancel_purchase(purchase_id: int) -> dict:
    """Stop a purchase order: during auto-buy's countdown (event stage "announced") nothing is bought at all, an open
    order is cancelled and the agent stops negotiating. Ask the user first unless they told you to."""
    return await _call("POST", f"/api/purchases/{purchase_id}/cancel")


@mcp.tool(annotations=READ)
async def list_sales(limit: int = 20) -> list:
    """Your own items sold (the sales ledger of this app), newest first: ad title and link, category (only for ads
    created with this app, else null), pieces, price in euro, buyer name, conversation id, the parcel's status and
    payment (shipment) and the pieces still for sale. Sales you took back are left out."""
    return await _call("GET", "/api/sales", params={"limit": limit})


@mcp.tool(annotations=READ)
async def get_settings(section: str | None = None) -> Any:
    """Every setting of the app that the API exposes, with key, section, type, value, default, description and
    choices / min / max where they apply. `section` narrows it to one section of the settings page."""
    params = {"section": section} if section else None
    return await _call("GET", "/api/settings", params=params)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=False))
async def update_settings(values: dict[str, Any]) -> Any:
    """Change settings: {key: new value} with the keys of get_settings. All values are validated first; if any is
    wrong nothing at all is stored and the errors say which. Ask the user before changing settings unless they told
    you to."""
    return await _call("PATCH", "/api/settings", json={"values": values})


@mcp.tool()
async def track_my_ad(ad_id_or_url: str, auto_renew: bool = False) -> dict:
    """Add one of your own ads to expiry monitoring."""
    return await _call("POST", "/api/myads", json={"ref": ad_id_or_url, "auto_renew": auto_renew})


if __name__ == "__main__":
    mcp.run("stdio")
