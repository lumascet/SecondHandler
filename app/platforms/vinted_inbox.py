"""Vinted messages: reading the inbox and its conversations into the chat list (sync), and the read-only
exploration that found how (explore).

Vinted's web app reads them from api.vinted.at/messaging/main/ (seen 2026-10-21 after logging in with Chrome):
  GET /messaging/main/inbox                   {conversations: [{id (uuid), conversation_type, created_at,
      is_unread_by_current_user, data.inventory_items[{id, title, photos}], last_message {id, created_at, sender_id,
      message_type, data.content}, opposite_users[{type, data{id, login_name, photo}}]}], pagination_temp}
  GET /messaging/main/conversations/<uuid>    {allow_reply, data{order_id, escrow_order_id}, messages[{id, created_at,
      sender_id, message_type, data}], opposite_users}; message_type "legacy_action": an event (data.entity title,
      subtitle); written messages carry their text in data (content/body)
They need Vinted's cookie, CSRF token and anon id, which only its own web app has. So the app does not call them: a
hidden tab of the app's browser (Google Chrome, logged in) opens /inbox or /inbox/<uuid> as a person would, and the
JSON answers of those calls are read on the way. Nothing is sent or marked from here.

explore() (the session page's "Explore the Vinted inbox", pressed right after logging in) opens the inbox and its newest
conversation in a hidden tab of the app's browser that is strictly read-only (session.read_hidden blocks every request
but GET/HEAD/OPTIONS, so nothing can be sent or changed), and keeps Vinted's own data calls in a capture file in
data/captures (local, like every capture: it holds chat content). The log names only the calls and their status.
Reading the chats into the chat list is built on what this finds.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timedelta, timezone
from typing import Any

from app.db import utcnow

log = logging.getLogger(__name__)

ORIGIN = "https://www.vinted.at"
_CONV_ID = re.compile(r"/inbox/(\d{5,})")
_ID_IN_JSON = re.compile(r'"(?:conversation_id|id)"\s*:\s*(\d{6,})')


def _vinted_data(url: str) -> bool:
    return "vinted." in url and "/api/" in url


# What the read-only tab lets through besides GET (seen 2026-10-21):
# - the token refresh before the inbox loads (/web/api/auth/refresh): blocked, Vinted calls expire-cookies and the
#   browser is logged out
# - the bot protection's report (dd.vinted.lt/js), metrics, logs and tracking: blocked, the browser looks like a bot
#   and Vinted blocks the connection for a while ("Deine Sitzung wurde blockiert")
# Never through: writes to Vinted's own data API (/api/v2/…: messages, offers, purchases) and anything else.
_REFRESH = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/refresh(?:\?|$)")
_EXPIRE = re.compile(r"^https://www\.vinted\.[a-z.]+/web/api/auth/expire-cookies")
_TELEMETRY = re.compile(r"^https://(?:dd\.vinted\.lt/|metrics\.vinted\.lt/|logs-ingress\.svc\.vinted\.com/|"
                        r"api\.vinted\.[a-z.]+/(?:j3r4zw/|tracking-events/))")


def _allow(method: str, url: str) -> bool:
    return method == "POST" and bool(_REFRESH.match(url) or _TELEMETRY.match(url))


def _deny(method: str, url: str) -> bool:
    return bool(_EXPIRE.match(url))


async def explore(delay_s: float = 0) -> dict[str, Any] | None:
    from app.willhaben.session import get_session

    if delay_s:
        await asyncio.sleep(delay_s)
    sess = get_session()
    if not sess.started:
        return None
    inbox = await sess.read_hidden(f"{ORIGIN}/inbox", r"Posteingang|Nachrichten|Inbox",
                                   timeout_ms=30000, keep=_vinted_data,
                                   allow=_allow, deny=_deny)
    conv_id = next(iter(_CONV_ID.findall(inbox["text"] or "")), None)
    if conv_id is None:
        for url, body in inbox["bodies"].items():
            if "inbox" in url or "conversation" in url:
                conv_id = next(iter(_ID_IN_JSON.findall(body)), None)
                if conv_id:
                    break
    conv = None
    if conv_id:
        conv = await sess.read_hidden(f"{ORIGIN}/inbox/{conv_id}", r"Nachricht|Artikel|Bestellung",
                                      timeout_ms=30000, keep=_vinted_data,
                                   allow=_allow, deny=_deny)
    out = {"label": "vinted-inbox-explore", "started_at": utcnow().isoformat(), "inbox": inbox, "conversation": conv,
           "conversation_id": conv_id}
    path = sess.settings.captures_dir / f"{utcnow():%Y%m%d-%H%M%S}-vinted-inbox-explore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    for name, res in (("inbox", inbox), ("conversation", conv)):
        if res is None:
            continue
        calls = [f"{r.get('method')} {re.sub(r'[?].*', '', r['url'])} {r.get('status', 'blocked' if r.get('blocked') else '')}"
                 for r in res["requests"] if _vinted_data(r["url"])]
        log.info("vinted %s: found=%s, %d data calls: %s", name, res["found"], len(calls),
                 "; ".join(re.sub(r"\d{5,}", "N", c) for c in calls[:25]))
    log.info("vinted inbox exploration saved as %s", path.name)
    return out


def summary(res: dict[str, Any] | None) -> str:
    """One line for the page: logged in or not, the inbox and the conversation read."""
    from app.i18n import t
    if res is None:
        return t("Browser session is not running")
    inbox = res["inbox"]
    if any("/member/" in r["url"] or "signup" in r["url"] for r in inbox["requests"] if r.get("type") == "document"):
        from app.services import site_accounts
        site_accounts._save("vinted", False)          # the stored state follows what the inbox showed
        return t("Vinted: not logged in in the browser window – log in there, then press again right away")
    calls = sum(1 for r in inbox["requests"] if _vinted_data(r["url"]) and not r.get("blocked"))
    conv = t("conversation read") if res.get("conversation") else t("no conversation found")
    return t("Vinted inbox read ({n} data calls), {conv}", n=calls, conv=conv)


# ------------------------------------------------------------------ sync into the chat list
SYNC_EVERY_S = 600            # a hidden tab of Vinted's inbox at most every 10 minutes (bot protection, not a poller)
MAX_DETAILS = 5               # conversations opened per sync (those with something new)
ORDER_RECHECK_S = 30 * 60     # an order's chat is read again this often: its shipping updates do not change the
ORDER_DAYS = 21               # inbox's last message (10-27 missed); for orders active within these days
_last_sync = [0.0]


def _messaging(url: str) -> bool:
    return "/messaging/main/" in url


def _parse(body: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def _when(raw: Any):
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def message_text(m: dict[str, Any]) -> tuple[str, str | None]:
    """(text, kind) of one Vinted message: a written one (kind None, like willhaben's), or an event of the order
    ("integration": shown as a notice)."""
    d = m.get("data") or {}
    e = d.get("entity") or {}
    if d.get("entity_type") == "message" or m.get("message_type") in ("legacy_reply", "reply_plain"):
        # a written message: legacy_reply carries it in data.entity.body (entity_type "message", seen 2026-10-21),
        # reply_plain (the answer to a send) in data.content. Vinted may hand its own machine translation in the
        # body (10-27 "Paket wurde versendet" for an Italian seller): the original, where it gives one, wins
        text = _original(e) or _original(d) or e.get("body") or d.get("content") or d.get("body") or ""
        return (text.strip() if isinstance(text, str) else ""), None
    if d.get("entity_type") in ("offer_request_message", "offer_message") \
            or m.get("message_type") in ("legacy_offer_request", "legacy_offer"):
        return offer_text(e), "integration"
    if m.get("message_type") == "legacy_action" or "entity" in d:
        text = " – ".join(x for x in (e.get("title"), e.get("subtitle")) if x) or str(d.get("event_type") or "")
        return text, "integration"
    for key in ("body", "content", "text", "message"):
        if isinstance(d.get(key), str) and d[key].strip():
            return d[key].strip(), None
    return "", None


OFFER_PENDING, OFFER_ACCEPTED, OFFER_REJECTED, OFFER_CANCELLED = 10, 20, 30, 40   # Vinted's offer statuses (its web
#   app's enum, 10-29); the web app offers accept / decline only while an offer request is pending


def _money_of(v: Any) -> float | None:
    try:
        return float((v or {}).get("amount")) if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


def offer_state(data: dict[str, Any], partner_id: str) -> dict[str, Any] | None:
    """The chat's transaction and its latest offer: {transaction_id, price (the price agreed so far), offer: {kind
    "request" (the buyer's) | "offer" (the seller's), id, price, original, status, status_title, from_me, at}}."""
    dd = data.get("data") or {}
    out: dict[str, Any] = {}
    if dd.get("transaction_id"):
        out["transaction_id"] = str(dd["transaction_id"])
    if (now := _money_of(dd.get("inquiry_value"))) is not None:
        out["price"] = now
    latest = None
    for m in data.get("messages") or []:
        d = m.get("data") or {}
        if d.get("entity_type") in ("offer_request_message", "offer_message") and \
                (latest is None or str(m.get("created_at") or "") >= str(latest[0].get("created_at") or "")):
            latest = (m, d, d.get("entity") or {})
    if latest:
        m, d, e = latest
        out.setdefault("transaction_id", str(e.get("transaction_id") or "") or None)
        out["offer"] = {
            "kind": "request" if d.get("entity_type") == "offer_request_message" else "offer",
            "id": str(e.get("offer_request_id") or d.get("id") or ""), "price": _money_of(e.get("price")),
            "original": _money_of(e.get("original_price")), "status": e.get("status"),
            "status_title": e.get("status_title"), "at": m.get("created_at"),
            "from_me": str(e.get("user_id") or m.get("sender_id") or "") != partner_id}
    return out or None


def offer_text(e: dict[str, Any]) -> str:
    """An offer in a Vinted chat as one line (10-29: the owner's counter-offer had no title and was dropped, the
    buyer's showed only "… hat dir ein neues Angebot gemacht:" without the amount):
      offer_request_message {title, price_label, original_price_label, status_title}  – the other side asks
      offer_message         {price_label, original_price_label}                          – an offer sent"""
    from app.i18n import t
    price = e.get("price_label") or (f"{(e.get('price') or {}).get('amount')} €" if (e.get("price") or {}).get("amount")
                                     else "")
    orig = e.get("original_price_label")
    head = (e.get("title") or "").strip().rstrip(":") or t("Offer")
    line = f"{head}: {price}".strip().rstrip(":")
    if orig and orig != price:
        line += " " + t("(was {price})", price=orig)
    if e.get("status_title"):
        line += f" · {e['status_title']}"
    return line


_ORIGINAL_KEYS = ("original_body", "body_original", "original_text", "original_content", "untranslated_body",
                  "source_body", "original")
_logged_shapes: set[str] = set()


def _original(d: dict[str, Any]) -> str | None:
    """The untranslated text of a message, when Vinted carries it beside its translation (field names guessed:
    log_shape names what really comes)."""
    for k in _ORIGINAL_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict) and isinstance(v.get("body"), str) and v["body"].strip():
            return v["body"]
    return None


def log_shape(m: dict[str, Any]) -> None:
    """Once per kind of message: the field names Vinted sends (names only, no content) – where a translation and
    its original live."""
    d = m.get("data") or {}
    e = d.get("entity") or {}
    shape = f"{m.get('message_type')}: {sorted(m)} data={sorted(d)} entity={sorted(e) if isinstance(e, dict) else '-'}"
    if shape not in _logged_shapes and len(_logged_shapes) < 30:
        _logged_shapes.add(shape)
        log.info("vinted message fields: %s", shape)


def message_key(m: dict[str, Any]) -> str:
    """A stable id of one Vinted message: Vinted gives its order events a new id on every load (seen 2026-10-21:
    the same "Gekauft!" twice), so an event is keyed by its kind, title and time; a written message by its number
    (data.entity.id of legacy_reply, data.id of the reply_plain a send answers with – the same number)."""
    import hashlib
    d = m.get("data") or {}
    e = d.get("entity") or {}
    if d.get("entity_type") == "message" and e.get("id"):
        return f"vinted-msg-{e['id']}"
    if m.get("message_type") == "reply_plain" and d.get("id"):
        return f"vinted-msg-{d['id']}"
    if m.get("message_type") == "legacy_action" or "entity" in d:
        raw = "|".join(str(x or "") for x in (d.get("event_type"), e.get("title"), e.get("subtitle"), m.get("created_at")))
        return "vinted-ev-" + hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"vinted-msg-{d.get('id') or m.get('id')}"      # another kind of written message


def _partner(c: dict[str, Any]) -> dict[str, Any]:
    users = c.get("opposite_users") or [{}]
    return (users[0] or {}).get("data") or {}


def apply_inbox(data: dict[str, Any], incoming: list[str] | None = None) -> list[str]:
    """The inbox list into Conversation rows (platform vinted). Returns the ids whose last message is new here;
    `incoming` gets those where the other person wrote it (unread), for the push."""
    from sqlmodel import col, select

    from app.db import Conversation, Message, session

    changed = []
    with session() as db:
        for c in data.get("conversations") or []:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            row = db.get(Conversation, cid)
            known = row is not None                    # a chat seen before: its new message is news
            row = row or Conversation(id=cid, platform="vinted")
            if row.removed:
                continue
            items = (c.get("data") or {}).get("inventory_items") or [{}]
            item = items[0] or {}
            last = c.get("last_message") or {}
            text, _ = message_text(last)
            at = _when(last.get("created_at") or c.get("created_at"))
            newer = at is not None and (row.last_message_at is None or at > row.last_message_at)
            partner = _partner(c)
            row.platform = "vinted"
            row.ad_id = str(item.get("id")) if item.get("id") else row.ad_id
            row.ad_title = item.get("title") or row.ad_title
            row.partner_name = partner.get("login_name") or row.partner_name
            row.unread_count = 1 if c.get("is_unread_by_current_user") else 0
            row.last_message_text = text or row.last_message_text
            row.last_message_at = at or row.last_message_at
            if row.ad_id:
                photos = [p.get("url") for p in item.get("photos") or [] if isinstance(p, dict) and p.get("url")]
                ctx = {**(row.ad_context or {}), "title": row.ad_title, "url": f"{ORIGIN}/items/{row.ad_id}",
                       "platform": "vinted"}
                if partner.get("id"):                # their profile tells their country: the answers' language
                    ctx["partner_id"] = str(partner["id"])
                if photos and not ctx.get("images"):        # the inbox's photo: a sold item's page has none left
                    ctx["images"] = photos[:1]
                row.ad_context = ctx
            row.synced_at = utcnow()
            db.add(row)
            empty = known and db.exec(select(Message.id).where(Message.conversation_id == cid).limit(1)).first() is None
            stale = known and db.exec(select(Message.id).where(        # stored under Vinted's unstable ids
                Message.conversation_id == cid, col(Message.ext_id).like("vinted-%"),
                ~col(Message.ext_id).like("vinted-msg-%"), ~col(Message.ext_id).like("vinted-ev-%")).limit(1)).first() is not None
            no_side = (row.role is None and c.get("conversation_type") == "order"     # its order says the side;
                       and not (row.ad_context or {}).get("side_tried"))                    # tried once
            if newer or row.messages_synced_at is None or empty or no_side or stale:
                changed.append(cid)
                partner_wrote = str(last.get("sender_id") or "") == str(partner.get("id") or "-")
                if incoming is not None and known and partner_wrote and c.get("is_unread_by_current_user"):
                    incoming.append(cid)
        db.commit()
    return changed


def apply_conversation(cid: str, data: dict[str, Any], escrow: dict[str, Any] | None = None) -> int:
    """One conversation's messages into Message rows; returns how many it holds. The side (buyer/seller) comes from
    an item conversation's current_user_side or, for an order, the order's (`escrow`)."""
    from app.db import Conversation, session
    from app.services import chats

    from sqlmodel import col, select

    from app.db import Message
    partner_id = str(_partner(data).get("id") or "")
    msgs = data.get("messages") or []
    with session() as db:
        # rows stored under Vinted's own (unstable) ids before the stable keys: dropped, read again below
        for old in db.exec(select(Message).where(Message.conversation_id == cid, col(Message.ext_id).like("vinted-%"),
                                                 ~col(Message.ext_id).like("vinted-msg-%"),
                                                 ~col(Message.ext_id).like("vinted-ev-%"))):
            db.delete(old)
        db.flush()
        for m in msgs:
            log_shape(m)
            text, kind = message_text(m)
            if not text:
                continue
            sender = str(m.get("sender_id") or "")
            direction = "unknown" if kind == "integration" else ("in" if sender and sender == partner_id else "out")
            chats._upsert_message(db, cid, message_key(m), direction, text, _when(m.get("created_at")),
                                  authoritative=True, kind=kind)
        row = db.get(Conversation, cid)
        if row is not None:
            row.messages_synced_at = utcnow()
            side = (data.get("data") or {}).get("current_user_side") or \
                ((escrow or {}).get("escrow_order") or {}).get("current_user_side")   # buyer | seller
            if side in ("buyer", "seller"):
                row.role = side
            elif row.role is None:
                row.ad_context = {**(row.ad_context or {}), "side_tried": True}
            offers = offer_state(data, partner_id)       # the price talk's state: what the offer steps act on
            if offers:
                row.vinted_offer = offers
            order = (data.get("data") or {}).get("order_id")
            if order:
                row.ad_context = {**(row.ad_context or {}), "order_id": str(order)}
            db.add(row)
        db.commit()
    return len(msgs)


_ESCROW = re.compile(r"/escrow-orders/api/v2/escrow-orders/\d+(?:\?|$)")


async def _catch(url: str, part: str, timeout_ms: int = 30000, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Open `url` (a page of Vinted's web app) in a short-lived tab and return the JSON answer of its own call whose
    address contains `part` – waiting for that answer, not for the page's text. None when it did not come.
    `extra`: gets the page's order call too ("escrow": escrow_order with current_user_side), when it makes one."""
    from app.willhaben.session import get_session

    sess = get_session()
    sess._require()
    ui = sess.ui_page
    sess._creating_internal = True
    try:
        page = await sess.context.new_page()
    finally:
        sess._creating_internal = False
        sess.ui_page = ui
    seen: list[Any] = []
    if extra is not None:
        page.on("response", lambda r: seen.append(r) if _ESCROW.search(r.url) and r.status == 200 else None)
    try:
        async with page.expect_response(lambda r: part in r.url and r.request.method == "GET" and r.status == 200,
                                        timeout=timeout_ms) as got:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        body = _parse(await (await got.value).text())
        if extra is not None:
            if not seen:
                await page.wait_for_timeout(2500)       # the order is asked right after the conversation
            for r in seen[:1]:
                try:
                    extra["escrow"] = _parse(await r.text())
                except Exception:  # noqa: BLE001 - closed meanwhile
                    pass
        return body
    except Exception as e:  # noqa: BLE001 - logged out (a redirect), slow: nothing caught
        log.info("vinted: no %s answer on %s (%s)", part, url, type(e).__name__)
        if "/member/" in page.url:
            from app.services import site_accounts
            site_accounts._save("vinted", False)
        return None
    finally:
        await page.close()


def _orders_due(inbox: dict[str, Any]) -> list[str]:
    """The inbox's order chats active within ORDER_DAYS whose messages were read more than ORDER_RECHECK_S ago."""
    from app.db import Conversation, session

    now, out = utcnow(), []
    with session() as db:
        for c in inbox.get("conversations") or []:
            cid = str(c.get("id") or "")
            row = db.get(Conversation, cid) if cid else None
            if row is None or c.get("conversation_type") != "order" or row.removed:
                continue
            active = row.last_message_at or row.synced_at
            synced = row.messages_synced_at
            if active is not None and now - _aware(active) < timedelta(days=ORDER_DAYS) and (
                    synced is None or (now - _aware(synced)).total_seconds() > ORDER_RECHECK_S):
                out.append(cid)
    return out


def _aware(at: Any) -> Any:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


async def _load(cid: str, push: bool = True) -> int:
    """One conversation's page in a short-lived tab (a second try after a few seconds when its answer does not come:
    10-27 several in a row did not), its messages stored; with `push`, a new message of the other person and a new
    order event (shipped, delivered …) pushed, each once. How many messages it holds."""
    extra: dict[str, Any] = {}
    before_in, before_ev = _newest_in(cid), _newest_event(cid)
    conv = await _catch(f"{ORIGIN}/inbox/{cid}", f"/messaging/main/conversations/{cid}", extra=extra)
    if conv is None:
        await asyncio.sleep(LOAD_RETRY_S)
        conv = await _catch(f"{ORIGIN}/inbox/{cid}", f"/messaging/main/conversations/{cid}", extra=extra)
    if conv is None:
        return 0
    n = apply_conversation(cid, conv, extra.get("escrow"))
    if push:
        after_in, after_ev = _newest_in(cid), _newest_event(cid)
        if after_in is not None and (before_in is None or after_in > before_in):
            await _push_incoming([cid])
        if after_ev is not None and (before_ev is None or after_ev > before_ev):
            await _push_events(cid, before_ev)
    return n


LOAD_RETRY_S = 5


async def sync_one(cid: str, push: bool = True) -> int:
    """One conversation again (the chat's Reload, a live event): its page in a short-lived tab, new messages and
    order events pushed."""
    from app.willhaben.session import get_session

    if not get_session().started:
        return 0
    n = await _load(cid, push=push and _known_any())
    from app.services import chats
    await chats.ensure_ad_context(cid)            # the item's card (photo, price) like a willhaben ad's
    return n


SEND_TIMEOUT_MS = 20000


class SendError(RuntimeError):
    pass


async def send(cid: str, text: str) -> dict[str, Any]:
    """Send `text` into a Vinted conversation the way a person does: its page in a tab of the logged-in browser, the
    text into the message field, the send button. Vinted's own web app makes the call
    (POST api.vinted.at/messaging/main/conversations/<uuid>/replies {content, is_personal_data_sharing_check_skipped:
    false, photo_temp_uuids: null} -> 201 {id, message_type "reply_plain", data.content}, seen 2026-10-21) with the
    CSRF token, anon id and siid only it has. Sent only when that answer came (201); else SendError, and nothing is
    retried. Vinted's warning about personal data (a phone number, an address) is never clicked away."""
    from app.willhaben.session import get_session

    sess = get_session()
    sess._require()
    text = (text or "").strip()
    if not text:
        raise SendError("empty")
    ui = sess.ui_page
    sess._creating_internal = True
    try:
        page = await sess.context.new_page()
    finally:
        sess._creating_internal = False
        sess.ui_page = ui
    try:
        await page.goto(f"{ORIGIN}/inbox/{cid}", wait_until="domcontentloaded", timeout=30000)
        if "/member/" in page.url:
            from app.services import site_accounts
            site_accounts._save("vinted", False)
            raise SendError("not logged in to Vinted")
        box = page.locator("textarea").first
        await box.wait_for(state="visible", timeout=SEND_TIMEOUT_MS)
        await box.click()
        await box.fill(text)
        await page.wait_for_timeout(400)
        is_reply = lambda r: (r.request.method == "POST" and f"/conversations/{cid}/replies" in r.url)  # noqa: E731
        async with page.expect_response(is_reply, timeout=SEND_TIMEOUT_MS) as got:
            button = box.locator("xpath=ancestor::form[1]").locator("button[type=submit]")
            if await button.count():
                await button.first.click()
            else:
                await box.press("Enter")
        resp = await got.value
        body = _parse(await resp.text())
        if resp.status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
            raise SendError(f"Vinted answered HTTP {resp.status}")
        return body
    except SendError:
        raise
    except Exception as e:  # noqa: BLE001 - no field, no answer in time: not sent as far as we know
        raise SendError(str(e) or type(e).__name__) from e
    finally:
        await page.close()


def record_sent(draft_id: int, reply: dict[str, Any]) -> None:
    """The message mark_sent stored as draft-<id> takes Vinted's id and time (the reply's answer)."""
    from sqlmodel import select

    from app.db import Message, session
    with session() as db:
        m = db.exec(select(Message).where(Message.ext_id == f"draft-{draft_id}")).first()
        if m is None:
            return
        m.ext_id = message_key(reply)
        m.created_at = _when(reply.get("created_at")) or m.created_at
        db.add(m)
        db.commit()


def _known_any() -> bool:
    from sqlmodel import select

    from app.db import Conversation, session
    with session() as db:
        return db.exec(select(Conversation).where(Conversation.platform == "vinted").limit(1)).first() is not None


_pushed: dict[str, Any] = {}     # conversation id -> the time of the message last pushed (the live and the inbox way)


def _newest_in(cid: str) -> Any:
    """The time of the newest stored message the other person wrote in this chat, or None."""
    from sqlalchemy import func
    from sqlmodel import col, select

    from app.db import Message, session
    with session() as db:
        return db.exec(select(func.max(Message.created_at)).where(Message.conversation_id == cid,
                                                                  Message.direction == "in",
                                                                  col(Message.kind).is_(None))).one()


def _newest_event(cid: str) -> Any:
    """The time of the newest stored order event (a notice: shipped, delivered …) of this chat, or None."""
    from sqlalchemy import func
    from sqlmodel import select

    from app.db import Message, session
    with session() as db:
        return db.exec(select(func.max(Message.created_at)).where(Message.conversation_id == cid,
                                                                  Message.kind == "integration")).one()


_pushed_events: dict[str, Any] = {}    # conversation id -> the time of the order event last pushed


async def _push_events(cid: str, since: Any) -> None:
    """Vinted's order events newer than `since` (shipped, delivered, money released …): one push each, once
    (Settings → Chats, like new messages)."""
    from sqlmodel import select

    from app.config import get_settings
    from app.db import Conversation, Message, get_setting, session
    from app.i18n import t
    from app.services.notify import notify

    if get_setting("notify_messages") != "1":
        return
    with session() as db:
        row = db.get(Conversation, cid)
        q = select(Message).where(Message.conversation_id == cid, Message.kind == "integration")
        if since is not None:
            q = q.where(Message.created_at > since)
        events = list(db.exec(q.order_by(Message.created_at)))
    for ev in events:
        if cid in _pushed_events and ev.created_at <= _pushed_events[cid]:
            continue
        _pushed_events[cid] = ev.created_at
        try:
            await notify("message", t("Vinted · {title}", title=(row.ad_title if row else None) or t("order")),
                         ev.text[:200], url=f"{get_settings().public_url.rstrip('/')}/chats/{cid}",
                         data={"conversation_id": cid, "platform": "vinted"}, tag=f"wh-vev-{cid}")
        except Exception as e:  # noqa: BLE001 - a failed push never stops the sync
            log.warning("vinted order event push failed: %s", e)


async def _push_incoming(cids: list[str]) -> None:
    """A message the other person wrote on Vinted: the same push as willhaben's new messages (Settings → Chats).
    Once per message: the live event and the inbox both find it (10-27 only the inbox pushed, and the live event,
    which stores the message first, left it nothing new – no Vinted push at all)."""
    from app.config import get_settings
    from app.db import Conversation, get_setting, session
    from app.i18n import t
    from app.services.notify import notify

    if get_setting("notify_messages") != "1":
        return
    for cid in cids:
        with session() as db:
            row = db.get(Conversation, cid)
        if row is None:
            continue
        at = _newest_in(cid) or row.last_message_at
        if at is not None and cid in _pushed and at <= _pushed[cid]:
            continue                                   # this message was pushed already
        _pushed[cid] = at
        try:
            await notify("message", t("Vinted: message from {name}", name=row.partner_name or t("unknown")),
                         f"{row.ad_title or ''}: {(row.last_message_text or '')[:140]}",
                         url=f"{get_settings().public_url.rstrip('/')}/chats/{cid}",
                         data={"conversation_id": cid, "platform": "vinted"}, tag=f"wh-msg-{cid}")
        except Exception as e:  # noqa: BLE001 - a failed push never stops the sync
            log.warning("vinted message push failed: %s", e)


# ------------------------------------------------------------------ live: Vinted's own push stream
# Vinted's web app keeps a server-sent events stream open while a page of it is open (seen 2026-10-21 in its
# messaging bundle): fetch("https://sse.vinted.<tld>/messaging/real-time/subscribe", credentials include, headers
# Platform, X-Next-App, X-Anon-Id, Locale, Accept text/event-stream); comments are heartbeats (12 s, then it
# reconnects), a 401 makes it renew the token; events are JSON {message_type: "refresh_inbox" |
# "refresh_conversation", data: {conversation_id, user_msg_thread_id}}. The app keeps Vinted's inbox open in a tab
# of its own: Vinted's web app holds the stream (login, token, reconnects); a wrapper around that tab's fetch passes a
# copy of the stream's events to the app, and the inbox answers the page loads on them are read like a sync. No
# request of the app's own, no polling.
_LIVE_JS = r"""
(() => {
  if (window.__whVintedWrapped) return;
  window.__whVintedWrapped = true;
  // the app's binding arrives a moment after the page's first scripts (patchright): events wait in a queue
  const queue = [];
  const flush = () => { while (queue.length && typeof window.__whVintedLiveApp === 'function') window.__whVintedLiveApp(queue.shift()); };
  window.__whVintedLive = (x) => { queue.push(x); flush(); };
  setInterval(flush, 500);
  const real = window.fetch;
  window.fetch = async function (input, init) {
    const resp = await real.apply(this, arguments);
    try {
      const url = String((input && (input.url || input.href)) || input || '');   // a string, Request or URL
      if (url.includes('/messaging/real-time/subscribe') && resp.body) {
        window.__whVintedLive(JSON.stringify({_state: 'open', status: resp.status}));
        const reader = resp.clone().body.getReader(), dec = new TextDecoder();
        let buf = '';
        (async () => {
          for (;;) {
            const {done, value} = await reader.read();
            if (done) { window.__whVintedLive(JSON.stringify({_state: 'closed'})); break; }
            buf += dec.decode(value, {stream: true});
            let i;
            while ((i = buf.indexOf('\n\n')) >= 0) {
              const block = buf.slice(0, i); buf = buf.slice(i + 2);
              const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n');
              window.__whVintedLive(data || JSON.stringify({_state: 'beat'}));
            }
          }
        })().catch(() => window.__whVintedLive(JSON.stringify({_state: 'closed'})));
      }
    } catch (e) {}
    return resp;
  };
})();
"""
live = {"state": "off", "since": None, "last_event": None, "events": 0, "page": None}
_live_tasks: set = set()


def _live_spawn(coro) -> None:
    task = asyncio.ensure_future(coro)
    _live_tasks.add(task)
    task.add_done_callback(_live_tasks.discard)


async def _on_live(payload: str) -> None:
    try:
        ev = json.loads(payload)
    except ValueError:
        return
    if "_state" in ev:
        if ev["_state"] == "fetch":
            log.info("vinted live: the page calls %s", ev.get("url"))
        # refused (401: the page's login ran out and it never renews it, 10-28/29: every 2 s for 18 hours): no sign
        # of life; the tab is reloaded, and if that does not help it is closed and tried again later
        if ev["_state"] == "open" and ev.get("status") in (401, 403):
            live["refused"] = live.get("refused", 0) + 1
            if live.get("state") != "refused":
                live.update(state=f"open {ev.get('status')}", since=live.get("since") or utcnow())
            if live["refused"] == LIVE_REFUSED_MAX and not live.get("renewing"):
                _live_spawn(_renew_live())
            return
        if ev["_state"] == "closed" and live.get("refused"):
            return
        if ev["_state"] == "open" and ev.get("status") == 200:
            live["refused"], live["renewals"] = 0, 0
        if ev["_state"] in ("open", "closed"):
            state = "connected" if ev["_state"] == "open" and ev.get("status") == 200 else \
                f"{ev['_state']} {ev.get('status') or ''}".strip()
            if state != live.get("state"):
                log.info("vinted live: %s", state)
            live.update(state=state, since=utcnow())
        live["last_event"] = utcnow()
        return
    live["last_event"], live["events"] = utcnow(), live["events"] + 1
    if ev.get("message_type") == "refresh_conversation":
        cid = str((ev.get("data") or {}).get("conversation_id") or "")
        if cid:          # its messages and events, new ones pushed here; the inbox the page reloads brings the list
            await sync_one(cid)


LIVE_REFUSED_MAX = 3          # refused connections in a row before the tab is reloaded (once) or closed
LIVE_BACKOFF_S = 15 * 60      # a tab still refused after its reload: closed, opened again after this


async def _renew_live() -> None:
    """The stream is refused (401): reload the tab once – Vinted's page renews its login when it loads –; refused
    again after that, close it and leave Vinted alone for LIVE_BACKOFF_S (the watchdog opens it again)."""
    live["renewing"] = True
    try:
        page = live.get("page")
        if live.get("renewals", 0) >= 1 or page is None or page.is_closed():
            log.warning("vinted live: still refused after a reload – closed, next try in %d min", LIVE_BACKOFF_S // 60)
            live.update(state="refused", since=utcnow(), refused=0,
                        retry_at=utcnow() + timedelta(seconds=LIVE_BACKOFF_S))
            if page is not None and not page.is_closed():
                await page.close()
            return
        live["renewals"], live["refused"] = live.get("renewals", 0) + 1, 0
        log.info("vinted live: refused (%s) – reloading the tab to renew its login", live.get("state"))
        await page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa: BLE001 - the watchdog takes it from here
        log.info("vinted live: reload failed: %s", e)
    finally:
        live["renewing"] = False


async def _on_live_response(resp) -> None:
    """The inbox the live tab reloads on an event: read like a sync (with the push for a new message)."""
    url = resp.url
    if resp.request.method != "GET" or resp.status != 200 or "/messaging/main/inbox" not in url:
        return
    try:
        data = _parse(await resp.text())
    except Exception:  # noqa: BLE001 - a page that navigated away meanwhile
        return
    if isinstance(data, dict):
        first = not _known_any()
        incoming: list[str] = []
        apply_inbox(data, incoming)
        if not first:
            await _push_incoming(incoming)


# ad and tracking hosts the live tab never needs (10-27: amazon-adsystem and criteo frames, each a renderer process of
# its own); Vinted's bot protection (DataDome) is not among them
_AD_HOSTS = re.compile(r"^https?://([a-z0-9-]+\.)*(amazon-adsystem\.com|criteo\.(com|net)|doubleclick\.net|"
                       r"googlesyndication\.com|googletagservices\.com|googleadservices\.com|adnxs\.com|"
                       r"adsrvr\.org|rubiconproject\.com|pubmatic\.com|casalemedia\.com|taboola\.com|"
                       r"outbrain\.com|smartadserver\.com|teads\.tv|yieldlab\.net|3lift\.com|openx\.net|"
                       r"bidswitch\.net|adform\.net)(/|:|$)", re.I)


async def start_live() -> bool:
    """Open (or reopen) the live tab: Vinted's inbox with the stream wrapper. False when the browser does not run or
    Vinted is logged out."""
    from app.services import site_accounts
    from app.willhaben.session import get_session

    sess = get_session()
    if not sess.started or site_accounts.state("vinted")["logged_in"] is False:
        return False
    old = live.get("page")
    if old is not None and not old.is_closed():
        sess.working_pages.discard(old)
        await old.close()
    ui = sess.ui_page
    sess._creating_internal = True
    try:
        page = await sess.context.new_page()
    finally:
        sess._creating_internal = False
        sess.ui_page = ui
    sess.working_pages.add(page)                   # a working page: the idle parking keeps it
    page.on("close", lambda p: sess.working_pages.discard(p))
    await page.route(_AD_HOSTS, lambda route: route.abort())   # its ads: a renderer each, nothing to listen to
    await page.expose_function("__whVintedLiveApp", lambda payload: _live_spawn(_on_live(payload)))
    await page.add_init_script(_LIVE_JS)
    page.on("response", lambda r: _live_spawn(_on_live_response(r)))
    live.update(page=page, state="connecting", since=utcnow(), refused=0, renewals=0, retry_at=None)
    await page.goto(f"{ORIGIN}/inbox", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(8000)
    try:
        wrapped = await page.evaluate("Boolean(window.__whVintedWrapped)", isolated_context=False)
    except Exception:  # noqa: BLE001 - navigated meanwhile
        wrapped = None
    log.info("vinted live tab: %s, wrapper %s, state %s", page.url, wrapped, live.get("state"))
    if "/member/" in page.url:                     # logged out: nothing to listen to
        site_accounts._save("vinted", False)
        live.update(state="logged out", since=utcnow())
        await page.close()
        return False
    return True


async def live_watchdog() -> None:
    """Every minute, locally: the live tab still open and its stream alive (an event or heartbeat within 3 min)?
    Else it is opened again. No request to Vinted while it runs."""
    from app import platforms
    while True:
        await asyncio.sleep(60)
        try:
            if not platforms.enabled("vinted"):
                continue
            page, last = live.get("page"), live.get("last_event") or live.get("since")
            dead = page is None or page.is_closed() or last is None or (utcnow() - last).total_seconds() > 180
            if live.get("state") == "refused" and utcnow() < (live.get("retry_at") or utcnow()):
                continue                               # refused: Vinted is left alone until retry_at
            from app.services import site_accounts
            logged_in_again = live.get("state") == "logged out" and site_accounts.state("vinted")["logged_in"]
            if dead and (live.get("state") != "logged out" or logged_in_again):
                log.info("vinted live: reopening (%s)", live.get("state"))
                await start_live()
        except Exception as e:  # noqa: BLE001 - next minute again
            log.info("vinted live watchdog: %s", e)


def is_vinted(conversation_id: str) -> bool:
    from app.db import Conversation, session

    with session() as db:
        c = db.get(Conversation, conversation_id)
    return bool(c and c.platform == "vinted")


async def sync(force: bool = False) -> dict[str, Any]:
    """The inbox and its changed conversations, through hidden tabs of the logged-in browser (at most every
    SYNC_EVERY_S unless forced). Skipped when the browser is not running or Vinted is known to be logged out."""
    from app.services import site_accounts
    from app.willhaben.session import get_session

    sess = get_session()
    if not sess.started or site_accounts.state("vinted")["logged_in"] is False:
        return {"skipped": "not logged in"}
    if not force and time.monotonic() - _last_sync[0] < SYNC_EVERY_S:
        return {"skipped": "recently"}
    _last_sync[0] = time.monotonic()
    inbox = await _catch(f"{ORIGIN}/inbox", "/messaging/main/inbox")
    if inbox is None:
        if site_accounts.state("vinted")["logged_in"] is False:
            return {"skipped": "not logged in"}
        return {"error": "no inbox answer"}
    first = not _known_any()
    incoming: list[str] = []
    changed = apply_inbox(inbox, incoming)
    changed += [cid for cid in _orders_due(inbox) if cid not in changed]
    read = 0
    from app.services import chats
    for cid in changed[:MAX_DETAILS]:
        read += await _load(cid, push=not first)

    for c in (inbox.get("conversations") or [])[:MAX_DETAILS * 2]:   # the item cards (photo, price); kept fresh a while
        if c.get("id"):
            await chats.ensure_ad_context(str(c["id"]))
    log.info("vinted chats: %d conversations, %d opened, %d messages", len(inbox.get("conversations") or []),
             min(len(changed), MAX_DETAILS), read)
    if not first:                                  # the first sync of all: what was there is no news
        await _push_incoming(incoming)
    return {"conversations": len(inbox.get("conversations") or []), "opened": min(len(changed), MAX_DETAILS)}

