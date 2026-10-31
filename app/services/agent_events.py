"""The AI agent channel: every notification as a structured event (docs/agent-api.md), for the feed (/api/events)
and pushed to the agent's webhook with an HMAC signature, retried with backoff. Independent of Home Assistant and
Telegram: notify() hands each event to dispatch(), the scheduler retries what did not arrive."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlmodel import col, select

from app.db import EventLog, WebhookDelivery, get_setting, session, set_setting, utcnow

log = logging.getLogger(__name__)

SETTING_TOKEN = "agent_token"
SETTING_WEBHOOK_URL = "agent_webhook_url"
SETTING_WEBHOOK_SECRET = "agent_webhook_secret"
SETTING_KINDS = "agent_kinds"

# every notification kind the app sends (notify(kind, ...)), in the order the settings page lists them
ALL_KINDS = ("purchase", "sale", "paylivery", "shipping", "message", "auto_reply", "auto_reply_pending", "meetup",
             "hit", "price_drop", "saved", "ad_expiring", "ad_expired", "ad_renewed", "digest", "health", "test")

_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport

TIMEOUT_S = 10
BACKOFF_S = (30, 120, 600, 1800)          # after the 1st..4th failed try; every later one waits BACKOFF_LATER_S
BACKOFF_LATER_S = 3600
GIVE_UP_AFTER = timedelta(hours=24)       # a delivery older than this is not tried again
KEEP_FINISHED = timedelta(days=7)         # delivered / given-up rows stay this long (Settings shows the failures)
PARALLEL = 5                              # deliveries in flight at once

_loop_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
_tasks: set[asyncio.Task] = set()         # fire-and-forget deliveries (the loop only keeps weak references)


# --- events ----------------------------------------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a zone: they are UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _split(e: EventLog) -> tuple[str, str]:
    """(title, message) of an event: older rows only have "title: message" in message."""
    if e.title:
        prefix = f"{e.title}: "
        return e.title, e.message[len(prefix):] if e.message.startswith(prefix) else e.message
    title, sep, message = e.message.partition(": ")
    return (title, message) if sep else (e.message, "")


def serialize(e: EventLog) -> dict[str, Any]:
    """The event as docs/agent-api.md shows it (feed, stream and webhook body)."""
    from app.services import notify      # notify imports this module

    data = e.data or {}
    title, message = _split(e)
    app_url = notify.app_link(e.kind, e.url, {**data, "ad_id": e.ad_id, "conversation_id": e.conversation_id}) \
        if notify._reachable() else None
    return {"id": e.id, "ts": _aware(e.ts).isoformat(), "kind": e.kind, "title": title, "message": message,
            "url": e.url, "app_url": app_url, "ad_id": e.ad_id, "conversation_id": e.conversation_id,
            "quiet": bool(e.quiet), "data": data}


def events_since(since: int | None = None, limit: int = 50, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """Events after id `since`, oldest first; without `since` the latest `limit`. Quiet ones are included."""
    limit = max(1, min(200, limit))
    q = select(EventLog)
    if kinds:
        q = q.where(col(EventLog.kind).in_(kinds))
    if since is not None:
        q = q.where(EventLog.id > since).order_by(col(EventLog.id)).limit(limit)
    else:
        q = q.order_by(col(EventLog.id).desc()).limit(limit)
    with session() as db:
        rows = list(db.exec(q))
    if since is None:
        rows.reverse()
    return [serialize(e) for e in rows]


def kinds() -> list[str]:
    """The kinds that go to the webhook (Settings → AI agent)."""
    return [k for k in (p.strip() for p in get_setting(SETTING_KINDS).split(",")) if k]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def ensure_secret() -> str:
    """The webhook's signing secret; created the first time something needs it."""
    secret = get_setting(SETTING_WEBHOOK_SECRET)
    if not secret:
        secret = new_secret()
        set_setting(SETTING_WEBHOOK_SECRET, secret)
    return secret


# --- webhook ---------------------------------------------------------------------------------------------------------

def dispatch(event_id: int | None, kind: str) -> None:
    """A new event: queue it for the webhook (when one is set and wants this kind) and try it right away. Never raises."""
    try:
        if not event_id or not get_setting(SETTING_WEBHOOK_URL) or kind not in kinds():
            return
        with session() as db:
            db.add(WebhookDelivery(event_id=event_id))
            db.commit()
        try:
            task = asyncio.get_running_loop().create_task(deliver_due())
        except RuntimeError:
            return                   # no event loop here: the scheduler job picks it up within 30 s
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as e:  # noqa: BLE001 - a notification must never fail because of the agent channel
        log.warning("agent webhook dispatch failed: %s", e)


def _lock() -> asyncio.Lock:
    """One lock per event loop (a lock is bound to the loop that first waits on it; tests run one loop each)."""
    global _loop_lock
    loop = asyncio.get_running_loop()
    if _loop_lock is None or _loop_lock[0] is not loop:
        _loop_lock = (loop, asyncio.Lock())
    return _loop_lock[1]


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=BACKOFF_S[attempts - 1] if attempts <= len(BACKOFF_S) else BACKOFF_LATER_S)


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any], delivery_id: int | str
                ) -> tuple[int | None, str | None]:
    """POST one signed event; (HTTP status or None without an answer, error text or None when it went through)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "SecondHandler",
               "X-SecondHandler-Event": str(payload["kind"]), "X-SecondHandler-Delivery": str(delivery_id),
               "X-SecondHandler-Timestamp": ts, "X-SecondHandler-Signature": _sign(ensure_secret(), ts, body)}
    try:
        r = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException:
        return None, f"no answer within {TIMEOUT_S} s"
    except httpx.HTTPError as e:
        return None, str(e) or type(e).__name__
    if r.is_success:
        return r.status_code, None
    return r.status_code, f"HTTP {r.status_code}: {r.text[:200]}".strip()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport)


def _due_ids() -> list[int]:
    with session() as db:
        q = (select(WebhookDelivery.id).where(col(WebhookDelivery.delivered_at).is_(None),
                                              col(WebhookDelivery.failed_at).is_(None),
                                              WebhookDelivery.next_at <= utcnow())
             .order_by(col(WebhookDelivery.id)))
        return list(db.exec(q))


def _prune() -> None:
    cutoff = utcnow() - KEEP_FINISHED
    with session() as db:
        for d in db.exec(select(WebhookDelivery).where(
                (col(WebhookDelivery.delivered_at) < cutoff) | (col(WebhookDelivery.failed_at) < cutoff))):
            db.delete(d)
        db.commit()


def _give_up(delivery_id: int, why: str) -> None:
    with session() as db:
        d = db.get(WebhookDelivery, delivery_id)
        if d:
            d.failed_at, d.last_error = utcnow(), why
            db.commit()


def _record(delivery_id: int, status: int | None, error: str | None) -> bool:
    """Store the outcome of a try: delivered, to be retried later, or given up. True if it was delivered."""
    now = utcnow()
    with session() as db:
        d = db.get(WebhookDelivery, delivery_id)
        if d is None:
            return False
        d.last_status, d.last_error = status, error
        if error is None:
            d.delivered_at = now
        else:
            d.attempts += 1
            if now - _aware(d.created_at) > GIVE_UP_AFTER:
                d.failed_at = now
            else:
                d.next_at = now + _retry_delay(d.attempts)
        db.commit()
        return error is None


async def _attempt(client: httpx.AsyncClient, delivery_id: int, url: str) -> bool:
    with session() as db:
        d = db.get(WebhookDelivery, delivery_id)
        if d is None or d.delivered_at or d.failed_at:
            return False
        e = db.get(EventLog, d.event_id)
        payload = serialize(e) if e else None
    if payload is None:
        _give_up(delivery_id, "event no longer exists")
        return False
    status, error = await _post(client, url, payload, delivery_id)
    if error:
        log.info("agent webhook delivery %s failed: %s", delivery_id, error)
    return _record(delivery_id, status, error)


async def deliver_due() -> int:
    """Send every delivery whose time has come (new ones, retries); returns how many arrived. One run at a time,
    so a delivery is never sent twice at once."""
    async with _lock():
        try:
            _prune()
            ids = _due_ids()
            if not ids:
                return 0
            url = get_setting(SETTING_WEBHOOK_URL)
            if not url:
                for i in ids:
                    _give_up(i, "no webhook URL set")
                return 0
            gate = asyncio.Semaphore(PARALLEL)

            async def one(delivery_id: int) -> bool:
                async with gate:
                    try:
                        return await _attempt(client, delivery_id, url)
                    except Exception as e:  # noqa: BLE001 - one broken delivery must not stop the others
                        log.warning("agent webhook delivery %s crashed: %s", delivery_id, e)
                        return False

            async with _client() as client:
                return sum(await asyncio.gather(*(one(i) for i in ids)))
        except Exception as e:  # noqa: BLE001
            log.warning("agent webhook run failed: %s", e)
            return 0


async def send_test() -> dict[str, Any]:
    """A signed test event to the current URL (Settings → AI agent): {"ok", "status", "error"}."""
    url = get_setting(SETTING_WEBHOOK_URL)
    if not url:
        return {"ok": False, "status": None, "error": "No webhook URL set."}
    payload = {"id": 0, "ts": utcnow().isoformat(), "kind": "test", "title": "Test", "message": "Webhook works.",
               "url": None, "app_url": None, "ad_id": None, "conversation_id": None, "quiet": True,
               "data": {"test": True}}
    async with _client() as client:
        status, error = await _post(client, url, payload, "test")
    return {"ok": error is None, "status": status, "error": error}


def webhook_status() -> dict[str, Any]:
    """For Settings → AI agent: how the webhook is doing. Given up = the last 7 days; the time of the last try is
    worked out from the retry delay (a failed try schedules the next one)."""
    now = utcnow()
    with session() as db:
        rows = list(db.exec(select(WebhookDelivery)))      # finished ones older than a week are pruned
    pending = [d for d in rows if not d.delivered_at and not d.failed_at]
    delivered = [_aware(d.delivered_at) for d in rows if d.delivered_at]
    tried = [(_aware(d.failed_at) if d.failed_at else _aware(d.next_at) - _retry_delay(d.attempts), d)
             for d in rows if d.attempts and not d.delivered_at]
    last_fail = max(tried, key=lambda t: t[0], default=None)
    last_ok = max(delivered, default=None)
    attempts = [t for t in (last_ok, last_fail[0] if last_fail else None) if t]
    return {"url": get_setting(SETTING_WEBHOOK_URL), "pending": len(pending),
            "failed": sum(1 for d in rows if d.failed_at and _aware(d.failed_at) > now - KEEP_FINISHED),
            "last_delivered_at": last_ok,
            "last_attempt_at": max(attempts, default=None),
            # an error only matters while nothing arrived after it
            "last_error": last_fail[1].last_error if last_fail and (not last_ok or last_fail[0] > last_ok) else None}
