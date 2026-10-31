"""The AI agent's JSON API (docs/agent-api.md): the notification feed (polled or as a Server-Sent Events stream),
purchase orders (and purchases made on willhaben directly, seen in their PayLivery chat) and sales of own items;
stopping a purchase is the one action. Opened by the bearer token
(auth.valid_bearer) or the dashboard login like every /api/ route."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlmodel import col, select

from app.config import get_settings
from app.db import Conversation, MyAd, Purchase, Sale, Shipment, current_ad, session
from app.services import agent_events, purchase, stock

router = APIRouter()

POLL_S = 2.0              # how often the stream looks for new events (tests shorten it)
PING_S = 15.0             # a comment line this often keeps the connection open through proxies
STREAM_BATCH = 50         # events fetched per look; a bigger backlog is drained look after look without waiting


# ------------------------------------------------------------------ small helpers
def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _iso(dt: datetime | None) -> str | None:
    """A database time (UTC, stored without a zone) as ISO 8601 with +00:00, like the events' ts."""
    if dt is None:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


def _app_url(path: str) -> str | None:
    """Absolute link to a page of this app; None when Settings → Web address is not reachable from outside."""
    from app.services import notify
    return get_settings().public_url.rstrip("/") + path if notify._reachable() else None


def _kinds(kind: str | None) -> list[str] | None:
    return [k for k in (p.strip() for p in (kind or "").split(",")) if k] or None


# ------------------------------------------------------------------ events
@router.get("/api/events")
async def api_events(since: int | None = None, limit: int = 50, kind: str | None = None):
    """Events after `since` (oldest first); without it the latest `limit`. `kind`: comma-separated kinds.
    `next_since` is the id to pass next time (unchanged when nothing new came)."""
    events = agent_events.events_since(since, _clamp(limit, 1, 200), _kinds(kind))
    return {"events": events, "next_since": events[-1]["id"] if events else (since or 0)}


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _event_stream(request: Request, since: int | None, kinds: list[str] | None) -> AsyncIterator[str]:
    """SSE frames for every event after `since` (without it: only new ones from now on), until the client leaves."""
    if since is None:
        latest = agent_events.events_since(None, 1)
        since = latest[-1]["id"] if latest else 0
    last_write = time.monotonic()
    yield ": connected\n\n"
    while not await request.is_disconnected():
        events = agent_events.events_since(since, STREAM_BATCH, kinds)
        for e in events:
            yield _sse(e)
            since = e["id"]
        if events:
            last_write = time.monotonic()
            if len(events) >= STREAM_BATCH:
                continue
        elif time.monotonic() - last_write >= PING_S:
            yield ": ping\n\n"
            last_write = time.monotonic()
        await asyncio.sleep(POLL_S)


@router.get("/api/events/stream")
async def api_events_stream(request: Request, since: int | None = None, kind: str | None = None):
    """Server-Sent Events: one frame per event. A reconnect resumes after the browser's Last-Event-ID header."""
    last = request.headers.get("last-event-id", "").strip()
    cursor = int(last) if last.isdigit() else since
    return StreamingResponse(_event_stream(request, cursor, _kinds(kind)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ purchases
def _platform_of(db: Any, row: Purchase) -> str:
    conv = db.get(Conversation, row.conversation_id) if row.conversation_id else None
    return (conv.platform if conv is not None else None) or "willhaben"


def _costs(row: Purchase) -> dict[str, Any]:
    """Shipping, fee and total the way the order page shows them (purchase.card_for): the item price is the agreed
    one, else the asking price; without PayLivery the seller may have named the total."""
    ship, source = purchase.shipping_of(row)
    item = row.agreed_price or row.asking_price
    if not item:
        return {"shipping": ship, "shipping_source": source, "fee": None, "total": None}
    costs = purchase.costs_of(row, item)
    total = row.agreed_total if row.mode != "paylivery" and row.agreed_total is not None \
        and row.agreed_price is not None else costs["total"]
    return {"shipping": ship, "shipping_source": source, "fee": costs["fee"], "total": total}


def _purchase_item(db: Any, row: Purchase) -> dict[str, Any]:
    return {
        "id": row.id, "ad_id": row.ad_id, "platform": _platform_of(db, row), "ad_title": row.ad_title,
        "ad_url": row.ad_url, "image_url": row.image_url,
        "category": row.category or purchase.rule_of(row).category,    # orders from before the column: the fee rule's
        "status": row.status, "mode": row.mode, "asking_price": row.asking_price, "max_price": row.max_price,
        "wish_price": row.wish_price, "agreed_price": row.agreed_price, **_costs(row),
        "conversation_id": row.conversation_id,
        "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at), "note": row.note,
        "app_path": f"/purchases/{row.id}", "app_url": _app_url(f"/purchases/{row.id}"),
    }


def _chat_purchase_item(conv: Conversation, stage: str) -> dict[str, Any]:
    """A purchase made on willhaben directly, seen in its PayLivery chat (purchase.chat_facts for the values)."""
    st = conv.paylivery or {}
    facts = purchase.chat_facts(conv, st)
    steps = [d for h in st.get("history") or [] if (d := _parse_iso(h.get("at")))]   # when the notices / own actions came
    first = min(steps, default=None)
    last = max(steps, default=None) or _parse_iso(st.get("updated_at"))
    path = f"/chats/{conv.id}"
    return {
        "source": "chat", "id": None, "conversation_id": conv.id, "ad_id": conv.ad_id, "platform": "willhaben",
        "ad_title": facts["ad_title"], "ad_url": facts["ad_url"], "image_url": facts["image_url"],
        "category": facts["category"], "status": stage, "mode": "paylivery", "agreed_price": facts["agreed_price"],
        "shipping": facts["shipping"], "shipping_source": facts["shipping_source"], "fee": facts["fee"],
        "total": facts["total"], "created_at": _iso(first or conv.last_message_at),
        "updated_at": _iso(last or conv.last_message_at), "app_path": path, "app_url": _app_url(path)}


def _parse_iso(value: Any) -> datetime | None:
    """A time stored in the PayLivery state (ISO text, with or without a zone) as a naive UTC database time."""
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


def _chat_purchases(db: Any) -> list[dict[str, Any]]:
    """Purchases made on willhaben directly: buyer chats whose PayLivery stage counts as a purchase
    (purchase.chat_purchase_stage) and that have no Purchase row (nor wait as the real chat of an auto-buy order)."""
    chats, ads = purchase.orders_of_chats(db)
    q = select(Conversation).where(Conversation.role == "buyer",
                                   col(Conversation.platform).is_(None) | (Conversation.platform == "willhaben"))
    items = []
    for conv in db.exec(q):
        stage = purchase.chat_purchase_stage(conv.paylivery)
        if stage and not purchase.chat_has_order(conv, chats, ads):
            items.append(_chat_purchase_item(conv, stage))
    return items


@router.get("/api/purchases")
async def api_purchases(status: str | None = None, active: int | None = None, limit: int = 20,
                        source: str | None = None):
    """Your purchases, newest change first, of two kinds (`source` on every item): "order" = purchase orders
    (Kaufauftrag) of this app, "chat" = purchases made on willhaben directly, seen in their PayLivery chat: a buyer chat
    whose PayLivery stage is offer_accepted, paid, released, label_ready, shipped, delivered or received (a reported
    problem or a cancellation only after one of these) and that has no order. Without PayLivery (cash, bank transfer)
    nothing shows up. `source=order|chat` lists one kind only (default both). `status` is one of purchase.STATUSES
    for orders and one of those PayLivery stages (or problem / cancelled) for chat purchases; with it only the kind it
    belongs to is listed. `active=1`: open orders (not done, failed or cancelled) and chat purchases that are not yet
    delivered (offer_accepted, paid, released, label_ready, shipped, problem), `active=0`: the rest. `limit` applies
    to the merged list. Chat items have `id` null and are opened with `conversation_id` (`GET /api/chats/{id}`); their
    amounts come from the PayLivery state and the stored ad, unknown ones are null."""
    if source not in (None, "order", "chat"):
        raise HTTPException(400, "source is order or chat")
    if status and status not in purchase.STATUSES and status not in purchase.CHAT_DEAL_STAGES \
            and status not in purchase.CHAT_AFTER_DEAL:
        raise HTTPException(400, f"unknown status {status!r}; one of {', '.join(purchase.STATUSES)} (orders) or "
                                 f"{', '.join((*purchase.CHAT_DEAL_STAGES, *purchase.CHAT_AFTER_DEAL))} (chats)")
    n = _clamp(limit, 1, 100)
    items: list[dict[str, Any]] = []
    with session() as db:
        if source != "chat" and (not status or status in purchase.STATUSES):
            q = select(Purchase).order_by(col(Purchase.updated_at).desc(), col(Purchase.id).desc())
            if status:
                q = q.where(Purchase.status == status)
            if active == 1:
                q = q.where(col(Purchase.status).in_(purchase.OPEN))
            elif active == 0:
                q = q.where(col(Purchase.status).notin_(purchase.OPEN))
            items += [{"source": "order", **_purchase_item(db, r)} for r in db.exec(q.limit(n))]
        if source != "order" and (not status or status in (*purchase.CHAT_DEAL_STAGES, *purchase.CHAT_AFTER_DEAL)):
            chat = [i for i in _chat_purchases(db) if not status or i["status"] == status]
            if active is not None:
                chat = [i for i in chat if (i["status"] in purchase.CHAT_OPEN_STAGES) == (active == 1)]
            items += chat
    items.sort(key=lambda i: (i["updated_at"] or "", i["id"] or 0), reverse=True)
    return items[:n]


@router.get("/api/purchases/{purchase_id}")
async def api_purchase(purchase_id: int):
    """One purchase order with its log (oldest entry first), the seller questions and answers, pickup appointment,
    payment details (direct mode), market prices at the start and the chat."""
    with session() as db:
        row = db.get(Purchase, purchase_id)
        if row is None:
            raise HTTPException(404, "no such purchase")
        return {**_purchase_item(db, row), "log": list(row.log or []), "questions": list(row.questions or []),
                "pickup": row.pickup, "payment": row.payment, "note": row.note, "market": row.market,
                "conversation_id": row.conversation_id}


@router.post("/api/purchases/{purchase_id}/cancel")
async def api_purchase_cancel(purchase_id: int):
    """Stop a purchase: during auto-buy's countdown it never starts (like the push's Cancel button), an open order is
    cancelled (like the order page). 409 when it is finished already."""
    from app.services import jobs
    if purchase.get(purchase_id) is None:
        raise HTTPException(404, "no such purchase")
    if jobs.cancel_key(f"purchase:{purchase_id}"):
        return {"ok": True, "stopped": "countdown"}
    try:
        purchase.cancel(purchase_id)
    except purchase.PurchaseError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "stopped": "order"}


# ------------------------------------------------------------------ sales
def _shipment_of(db: Any, conversation_id: str | None) -> dict[str, Any] | None:
    ship = db.exec(select(Shipment).where(Shipment.conversation_id == conversation_id, Shipment.status != "discarded")
                   .order_by(col(Shipment.id).desc())).first() if conversation_id else None
    if ship is None:
        return None
    return {"status": ship.status, "paid_at": _iso(ship.paid_at), "paid_via": ship.paid_via, "tracking": ship.tracking}


def _sale_item(db: Any, s: Sale) -> dict[str, Any]:
    ad = db.get(MyAd, s.ad_id) or current_ad(db, s.ad_id)
    newest = current_ad(db, s.ad_id)
    conv = db.get(Conversation, s.conversation_id) if s.conversation_id else None
    left = stock.numbers(newest)
    return {
        "id": s.id, "ad_id": s.ad_id, "ad_title": ad.heading if ad else None, "ad_url": ad.url if ad else None,
        "image_url": ad.image_url if ad else None, "category": stock.category_of(db, s.ad_id),
        "pieces": s.pieces, "price": s.price, "note": s.note, "created_at": _iso(s.created_at),
        "undone_at": _iso(s.undone_at), "conversation_id": s.conversation_id,
        "buyer": conv.partner_name if conv is not None else None, "shipment": _shipment_of(db, s.conversation_id),
        "pieces_left": left["remaining"] if left else None,
    }


@router.get("/api/sales")
async def api_sales(limit: int = 20, include_undone: int = 0):
    """Your own items sold (the sales ledger), newest first. A sale taken back ("not sold after all") is left out
    unless `include_undone=1` (then its undone_at is set). `category` comes from the ad draft the ad was published
    from and is None for other ads; `pieces_left` is None for a single item."""
    q = select(Sale).order_by(col(Sale.created_at).desc(), col(Sale.id).desc())
    if not include_undone:
        q = q.where(col(Sale.undone_at).is_(None))
    with session() as db:
        return [_sale_item(db, s) for s in db.exec(q.limit(_clamp(limit, 1, 100)))]
