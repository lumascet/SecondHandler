"""willhaben chats: sync conversations, draft replies with Claude, send after approval or automatically.

Automatic answers are opt-in per own ad (MyAd.auto_reply), only in conversations where the user is
the seller, only after one manual send has been confirmed (setting send_verified), within rate limits,
and only when both Claude (auto_send) and the code checks below agree.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case
from sqlmodel import col, func, select

from app.db import Conversation, Draft, Message, MyAd, current_ad, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import (
    AUTO_REPLY_SCHEMA, MAX_OTHERS, MAX_STYLE_EXAMPLES, PURCHASE_SCHEMA, REPLY_SCHEMA, REPLY_SYSTEM,
    auto_rules, language_rule, platform_rule,
    price_floor, reply_prompt,
)
from app.services import feedback, gcal, humanize, jobs, meetups, min_price, paylivery, purchase, shipping, telegram, topic
from app.services import deals, my_ads, paylivery_advice, selling, stock, style, system_prompts
from app.services.notify import clear_push, notify
from app.willhaben.account import AccountClient, EndpointUnknown, SendFailed, _when
from app.willhaben.public import WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session
from app.services import timeline

log = logging.getLogger(__name__)


def _account() -> AccountClient:
    return AccountClient(get_session())


FINISHED_STAYS_H = 24        # a finished chat stays in the list this long after its last message


def sync_window() -> tuple[int, datetime | None]:
    """Settings chat_sync_limit (N latest conversations, 1-50) and chat_sync_days (conversations active in the last N
    days: the cutoff is local midnight N days ago, so it moves along every day; 0 = today only, empty = no cutoff)."""
    try:
        limit = min(max(int(get_setting("chat_sync_limit") or 10), 1), 50)
    except ValueError:
        limit = 10
    since = None
    raw = (get_setting("chat_sync_days") or "").strip()
    if raw.isdigit():
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        since = (today - timedelta(days=int(raw))).replace(tzinfo=None).astimezone()   # local midnight, DST-safe
    return limit, since


async def sync(force: bool = False) -> dict[str, Any]:
    sess = get_session()
    if not sess.started:
        return {"skipped": t("Browser session is not running")}
    if not sess.logged_in and not await sess.check_login():
        return {"skipped": t("not logged in")}
    acct = _account()
    limit, since = sync_window()
    try:
        convs = await acct.conversations(limit=limit)
    except NotLoggedIn:
        return {"skipped": t("Session expired")}
    convs = sorted(convs, key=lambda c: _aware(c.last_at) if c.last_at else datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:limit]
    if since is not None:
        convs = [c for c in convs if c.last_at is None or _aware(c.last_at) >= since]

    changed: list[str] = []
    incoming: list[Conversation] = []
    known_until: dict[str, datetime | None] = {}     # newest message time before this sync
    with session() as db:
        first_sync = db.exec(select(Conversation).limit(1)).first() is None
        for c in convs:
            row = db.get(Conversation, c.id)
            is_new = row is None
            if row is None:
                row = Conversation(id=c.id)
            known_until[c.id] = row.last_message_at
            newer = c.last_at is not None and (row.last_message_at is None or c.last_at > row.last_message_at)
            more_unread = c.unread_count > (row.unread_count or 0)
            if row.hidden_at is not None:
                if not (newer and not c.last_is_mine):
                    if row.removed:
                        continue     # removed here: not synced again until the other person writes
                else:
                    row.hidden_at, row.removed = None, None    # they wrote again: back in the list
            row.ad_id = c.ad_id or row.ad_id
            row.ad_title = c.ad_title or row.ad_title
            row.partner_name = c.partner_name or row.partner_name
            row.partner_uuid = c.partner_uuid or row.partner_uuid
            row.ad_uuid = c.ad_uuid or row.ad_uuid
            row.role = c.role or row.role
            row.unread_count = c.unread_count
            row.last_message_text = c.last_text or row.last_message_text
            row.last_message_at = c.last_at or row.last_message_at
            row.synced_at = utcnow()
            db.add(row)
            if c.last_is_mine and c.last_read and c.last_at is not None:
                mark_seen(db, c.id, until=c.last_at)    # they read my newest message: all mine before it too
            if is_new or newer or force:
                changed.append(c.id)
            if not first_sync and c.unread_count > 0 and (newer or more_unread) and not c.last_is_mine:
                incoming.append(row)
        db.commit()

    for cid in changed:
        await sync_messages(cid)
    incoming = [r for r in incoming if _partner_wrote(r, known_until.get(r.id))]

    notify_on = get_setting("notify_messages") == "1"
    for row in incoming:
        try:
            await shipping.on_incoming(row)    # buyer sent an address? -> parcel + label offer (background)
        except Exception as e:  # noqa: BLE001 - never block the chat sync
            log.warning("shipping check failed for %s: %s", row.id, e)
        if _auto_mode(row) is not None:
            continue   # auto reply on: process_pending_auto pushes "answered" or "approve?" instead of the raw message
        auto_draft = get_setting("auto_draft") == "1"
        tag = message_tag(row.id)
        if notify_on:
            await notify("message", t("Message from {name}", name=row.partner_name or t("unknown")),
                         f"{row.ad_title or ''}: {(row.last_message_text or '')[:140]}",
                         url="https://www.willhaben.at/iad/myprofile/chat",
                         data={"conversation_id": row.id, "ad_id": row.ad_id}, tag=tag)
            if auto_draft:
                await telegram.progress(tag, "✍️ " + t("Claude is writing a draft …"))
        if auto_draft:
            try:
                d = await draft_reply(row.id)
            except (LLMError, WillhabenError) as e:
                log.warning("auto draft failed for %s: %s", row.id, e)
                continue
            # the message push turns into the draft offer: one message, one tap to send; with message pushes off,
            # the offer is the one push for this message
            await offer_draft(row, d, t("drafted automatically"), replaces=tag if notify_on else None)
    last_sync_at[0] = utcnow()
    answered = await process_pending_auto()
    return {"conversations": len(convs), "changed": len(changed), "incoming": len(incoming), "auto": answered}


HISTORY_SLACK = timedelta(seconds=2)     # the list's last-message time vs. that message's created_at (same on 10-13)


def _history_reaches(msgs: list[Message], conv: Conversation) -> bool:
    """The stored history holds the list's newest message: one at its time whose text starts like its preview (the
    list cuts it at 80 characters)."""
    if conv.last_message_at is None:
        return bool(msgs)
    at, head = _aware(conv.last_message_at), " ".join((conv.last_message_text or "").split())[:40].rstrip("…").strip()
    return any(m.created_at and abs(_aware(m.created_at) - at) <= HISTORY_SLACK and " ".join(m.text.split()).startswith(head)
               for m in msgs)


def _partner_wrote(conv: Conversation, since: datetime | None) -> bool:
    """Did the other person write since the last sync? The list only says that something new and unread came: a
    willhaben notice (PayLivery, shipping, status – the list marks it is_mine false) or our own answer counts there
    too. The stored history tells when it holds the list's newest message – fetched in this sync, or earlier when only
    the unread count went up (willhaben counts a notice as unread again: a shipping notice was pushed twice as their
    message); when it does not reach it (endpoint unknown) the list has to do. On True, `conv.last_message_text`
    becomes their newest message: the push and the address check quote it, not a notice."""
    msgs = messages_of(conv.id)
    if not _history_reaches(msgs, conv):
        return True
    theirs = [m for m in msgs if m.direction == "in" and not m.kind
              and (since is None or m.created_at is None or _aware(m.created_at) > _aware(since))]
    if not theirs:
        return False
    conv.last_message_text = theirs[-1].text
    return True


def _is_fallback_id(ext_id: str) -> bool:
    """Ids we made up: last-message previews, content hashes from before message_id was parsed,
    and `draft-<id>` rows written when we sent a message (before willhaben's history has it)."""
    return ext_id.startswith(("last-", "draft-")) or (len(ext_id) == 16 and all(c in "0123456789abcdef" for c in ext_id))


SENT_MATCH_WINDOW = timedelta(minutes=10)   # our clock at send time vs. willhaben's created_at


def _drop_sent_placeholders(db, conversation_id: str) -> None:
    """Remove `draft-<id>` rows once willhaben's history holds the same sent message under its real id."""
    rows = list(db.exec(select(Message).where(Message.conversation_id == conversation_id)))
    real = [r for r in rows if r.direction == "out" and not _is_fallback_id(r.ext_id)]
    for p in (r for r in rows if r.ext_id.startswith("draft-")):
        if any(r.text == p.text and r.created_at and p.created_at
               and abs(_aware(r.created_at) - _aware(p.created_at)) <= SENT_MATCH_WINDOW for r in real):
            db.delete(p)
    db.flush()


SEEN_SLACK = timedelta(seconds=10)   # a `draft-<id>` row holds our clock after sending, willhaben's is a bit earlier


def mark_seen(db, conversation_id: str, until: datetime | None = None, at: datetime | None = None) -> int:
    """The other person has seen my messages in this chat up to `until` (all of them without): mark them read,
    with `at` as the time they did, when known. Local database only; returns how many changed."""
    n = 0
    for m in db.exec(select(Message).where(Message.conversation_id == conversation_id, Message.direction == "out")):
        if until is not None and m.created_at is not None and _aware(m.created_at) > _aware(until) + SEEN_SLACK:
            continue
        if m.read and (m.read_at is not None or at is None):
            continue
        m.read, m.read_at = True, m.read_at or at
        db.add(m)
        n += 1
    return n


SEEN_EVENTS = ("mark_message_as_read", "mark_inbox_as_read")


def _seen_event(conversation_id: str, frame: dict[str, Any]) -> bool:
    """A read event of the live socket from the other person (`incoming` true; false = I read theirs): my message
    (`message_id`, and everything before it) or the whole chat (`mark_inbox_as_read`) has been seen. Stored here,
    nothing new to fetch from willhaben. Seen in recorded frames 2026-10-15."""
    kind = frame.get("type") or frame.get("req_type")
    if kind not in SEEN_EVENTS or frame.get("incoming") is not True:
        return False
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    at = _when(frame.get("created_at"))
    with session() as db:
        if kind == "mark_inbox_as_read":
            n = mark_seen(db, conversation_id, until=at or utcnow(), at=at)
        else:
            mine = db.exec(select(Message).where(Message.conversation_id == conversation_id, Message.direction == "out",
                                                 Message.ext_id == str(payload.get("message_id") or ""))).first()
            # their copies of willhaben's notices carry ids I do not have: nothing of mine was read
            n = mark_seen(db, conversation_id, until=mine.created_at or at, at=at) if mine else 0
        db.commit()
    if n:
        log.info("%d message(s) seen in %s", n, conversation_id)
    return True


def _same_message(row: Message, text: str, created_at) -> bool:
    if row.text != text or not _is_fallback_id(row.ext_id):
        return False
    if row.created_at == created_at:
        return True
    if row.ext_id.startswith("draft-") and row.created_at and created_at:
        return abs(_aware(row.created_at) - _aware(created_at)) <= SENT_MATCH_WINDOW
    return False


def _upsert_message(db, conversation_id: str, ext_id: str, direction: str, text: str, created_at,
                    authoritative: bool = False, kind: str | None = None, params: dict | None = None,
                    read: bool | None = None) -> None:
    """Insert or update one message. `authoritative` = direction comes from the full history, not a guess.

    A row with the same text and time but another ext_id is the same message stored earlier under a
    fallback id (last-message preview, or before the real message_id was parsed): it is re-keyed.
    `kind` marks willhaben notices (system | integration); `params` is their structured `parameters`.
    `read` is willhaben's flag; it is kept for my own messages only (the other person has seen it) and never
    goes back from seen to unseen.
    """
    exists = db.exec(select(Message).where(Message.conversation_id == conversation_id,
                                           Message.ext_id == ext_id)).first()
    if exists is None:
        exists = next((m for m in db.exec(select(Message).where(
            Message.conversation_id == conversation_id, Message.text == text))
            if _same_message(m, text, created_at)), None)
        if exists:
            exists.ext_id = ext_id
            db.add(exists)
    if exists:
        if direction != exists.direction and (authoritative or exists.direction == "unknown"):
            exists.direction = direction
            db.add(exists)
        if authoritative and text and exists.text != text:
            # the full history says it differently (Vinted's original instead of its machine translation, 10-27):
            # its text, and its language and translation asked again
            exists.text, exists.lang, exists.translation = text, None, None
            db.add(exists)
        if kind is not None and exists.kind != kind:
            exists.kind = kind
            db.add(exists)
        if params is not None and exists.params != params:
            exists.params = params
            db.add(exists)
        if exists.direction == "out" and exists.read is not True and read is not None and exists.read != read:
            exists.read = read
            db.add(exists)
        return
    db.add(Message(conversation_id=conversation_id, ext_id=ext_id, direction=direction,
                   text=text, created_at=created_at, kind=kind, params=params,
                   read=read if direction == "out" else None))


def chat_for_ad(ad_id: str | None) -> str | None:
    """The willhaben chat in which I write to the seller of this ad (role buyer), or None."""
    if not ad_id:
        return None
    with session() as db:
        conv = db.exec(select(Conversation).where(Conversation.ad_id == str(ad_id), Conversation.platform.is_(None),
                                                  Conversation.role == "buyer")
                       .order_by(col(Conversation.last_message_at).desc())).first()
    return conv.id if conv else None


def is_own_ad(ad_id: str | None) -> bool:
    if not ad_id:
        return False
    with session() as db:
        return db.get(MyAd, str(ad_id)) is not None


async def contact_seller(ad_id: str, text: str) -> str:
    """Write to the seller of an ad (the ad pane's "Message the seller"): willhaben's contact form opens the chat
    (AccountClient.create_conversation), which is kept as my chat as a buyer with the opener, then synced. Returns the
    conversation id. SendFailed / ValueError: nothing went out; OpenerUnconfirmed: it went out, the chat did not show
    up yet – never send it again."""
    from app.willhaben.public import get_public

    text = (text or "").strip()
    if not text:
        raise ValueError(t("Empty message"))
    try:
        detail = await get_public().get_detail(ad_id)
    except Exception as e:  # noqa: BLE001 - title and seller are niceties: the chat brings them later
        log.info("contact %s: ad detail not read (%s)", ad_id, e)
        detail = None
    sent_at = utcnow()
    cid = await _account().create_conversation(ad_id, text)
    with session() as db:
        conv = db.get(Conversation, cid) or Conversation(id=cid)
        conv.ad_id, conv.role = str(ad_id), "buyer"
        if detail is not None:
            conv.ad_title = conv.ad_title or detail.heading
            conv.partner_name = conv.partner_name or detail.seller_name
            conv.ad_context, conv.ad_context_at = detail.as_context(), utcnow()
        db.add(conv)
        ext_id = f"draft-contact-{cid}"
        if db.exec(select(Message).where(Message.conversation_id == cid, Message.ext_id == ext_id)).first() is None:
            # stamped with the send time, so the sync can match it with willhaben's copy (SENT_MATCH_WINDOW)
            db.add(Message(conversation_id=cid, ext_id=ext_id, direction="out", text=text, created_at=sent_at))
        db.commit()
    try:
        await sync_messages(cid)
        await ensure_ad_context(cid)
    except Exception as e:  # noqa: BLE001 - the chat exists; the history follows at the next sync
        log.info("first sync of chat %s failed: %s", cid, e)
    return cid


async def sync_messages(conversation_id: str) -> str:
    """Fetch the full history; falls back to the list's last message while the endpoint is unverified."""
    from app.platforms import vinted_inbox
    if vinted_inbox.is_vinted(conversation_id):   # Vinted: its conversation page in a hidden tab
        await vinted_inbox.sync_one(conversation_id)
        return "full"
    acct = _account()
    try:
        msgs = await acct.messages(conversation_id)
    except EndpointUnknown:
        msgs = None
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if msgs is not None:
            for m in msgs:
                _upsert_message(db, conversation_id, m.ext_id, m.direction, m.text, m.created_at,
                                authoritative=True, kind=m.kind, params=m.params, read=m.read)
            _drop_sent_placeholders(db, conversation_id)
            mode = "full"
        else:
            if conv and conv.last_message_text:
                ext = "last-" + hashlib.sha1(f"{conv.last_message_at}|{conv.last_message_text}".encode()).hexdigest()[:12]
                direction = "in" if conv.unread_count > 0 else "unknown"
                _upsert_message(db, conversation_id, ext, direction, conv.last_message_text, conv.last_message_at)
            mode = "last-only"
        if conv:
            conv.messages_synced_at = utcnow()
            db.add(conv)
        db.commit()
        has_role = conv is not None and conv.role in ("seller", "buyer")
    if has_role:
        try:
            await paylivery.scan(conversation_id, fresh=msgs)   # PayLivery notices: state for both roles, payment/label for sellers
        except Exception as e:  # noqa: BLE001 - never block the chat sync
            log.warning("PayLivery scan failed for %s: %s", conversation_id, e)
    return mode


AD_CONTEXT_MAX_AGE = timedelta(minutes=30)   # a price change, reservation or sale on willhaben reaches Claude within this


def _ad_context_fresh(conv: Conversation) -> bool:
    """The stored ad context still serves a draft: the public page's data (it carries the PayLivery flags) younger
    than AD_CONTEXT_MAX_AGE, or the ad is gone - that only changes when the owner reloads the chat. The editor
    fallback (_own_ad_context) and contexts stored before they carried a time are fetched again."""
    ctx = conv.ad_context or {}
    if ctx.get("status") == "nicht mehr online":
        return True
    if "paylivery" not in ctx or conv.ad_context_at is None:
        return False
    return utcnow() - _aware(conv.ad_context_at) < AD_CONTEXT_MAX_AGE


async def ensure_ad_context(conversation_id: str, force: bool = False) -> Conversation | None:
    """The ad as Claude sees it (title, price, description, status, PayLivery flags) from the public ad page, kept
    for AD_CONTEXT_MAX_AGE so a draft sees today's price and status; `force` (the chat's Reload) fetches it again
    at once, also for an ad stored as gone."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None or not conv.ad_id or (not force and _ad_context_fresh(conv)):
        return conv
    if conv.platform == "vinted":         # its item page on Vinted (anonymous, like a hit's), not willhaben's
        return await _vinted_ad_context(conv)
    try:
        detail = await get_public().get_detail(conv.ad_id)
    except WillhabenError as e:
        log.info("ad context for %s failed: %s", conv.ad_id, e)
        if "paylivery" in (conv.ad_context or {}):
            return conv                   # a refresh that failed: the page data from before stays, tried again next time
        return await _own_ad_context(conv)
    me = get_session().user or {}
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if detail is None:
            # German on purpose: ad_context is part of the prompt Claude answers from
            conv.ad_context = {"title": conv.ad_title, "status": "nicht mehr online"}
        else:
            conv.ad_context = detail.as_context()
            mine = {str(v) for v in (me.get("id"), me.get("uuid")) if v}
            if mine:
                conv.role = "seller" if {detail.seller_login_id, detail.seller_uuid} & mine else "buyer"
        conv.ad_context_at = utcnow()
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


async def _vinted_ad_context(conv: Conversation) -> Conversation:
    """A Vinted chat's item from its public item page (title, price, photos, condition, description, status), kept
    like a willhaben ad's; what the inbox said (order) stays. A failure keeps what was there."""
    from app import platforms
    from app.platforms import PlatformError
    url = (conv.ad_context or {}).get("url")
    try:
        detail = await platforms.get("vinted").detail(conv.ad_id, url=url)
    except (PlatformError, Exception) as e:  # noqa: BLE001 - the chat stays usable without it
        log.info("vinted item %s not loaded: %s", conv.ad_id, e)
        return conv
    with session() as db:
        row = db.get(Conversation, conv.id)
        if detail is not None:
            old = row.ad_context or {}
            ctx = {**detail.as_context(), **{k: v for k, v in old.items()
                                             if k in ("order_id", "side_tried", "partner_id", "partner_country",
                                                      "partner_country_tried", "partner_country_title", "partner_city")},
                   "platform": "vinted"}
            if not ctx.get("images") and old.get("images"):   # a sold item's page has no photos: the inbox's stays
                ctx["images"] = old["images"]
            row.ad_context = ctx
        row.ad_context_at = utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def partner_language(conversation_id: str) -> str | None:
    """The language the other person writes in: the most frequent among their last messages whose language is known
    (services/translate), None when none is known yet."""
    from collections import Counter
    with session() as db:
        langs = list(db.exec(select(Message.lang).where(
            Message.conversation_id == conversation_id, Message.direction == "in", col(Message.kind).is_(None),
            col(Message.lang).is_not(None), Message.lang != "-").order_by(col(Message.created_at).desc()).limit(5)))
    return Counter(langs).most_common(1)[0][0] if langs else None


async def _vinted_partner_country(conv: Conversation) -> str | None:
    """The other person's country on Vinted (their public profile, read once and kept with the chat): which language
    the answers are in. None when unknown or not loadable (then the conversation decides)."""
    ctx = conv.ad_context or {}
    if ctx.get("partner_country") or not ctx.get("partner_id") or ctx.get("partner_country_tried"):
        return ctx.get("partner_country")
    from app.platforms import PlatformError, vinted
    try:
        m = await vinted.get_client().member(ctx["partner_id"])
    except (PlatformError, Exception) as e:  # noqa: BLE001 - the answer is written all the same
        log.info("vinted member %s: country not loaded: %s", ctx["partner_id"], e)
        return None
    _keep_place(conv, m)
    return m.get("country")


def _keep_place(conv: Conversation, member: dict[str, Any]) -> None:
    """Where the other person on Vinted is from (their profile), kept with the chat: the language of the answers
    and the place shown in the chat's header."""
    with session() as db:
        row = db.get(Conversation, conv.id)
        if row is not None:
            row.ad_context = {**(row.ad_context or {}), "partner_country": member.get("country"),
                              "partner_country_title": member.get("country_title"),
                              "partner_city": member.get("city"), "partner_country_tried": True}
            db.add(row)
            db.commit()
            conv.ad_context = row.ad_context


def partner_place(conv: Conversation) -> str:
    """"🇮🇹 Italien", "Milano, 🇮🇹 Italien" (Vinted), "8530 Deutschlandsberg" (willhaben) or "" – what the other
    person's profile says."""
    if conv.platform != "vinted":
        return conv.partner_place or ""
    ctx = conv.ad_context or {}
    cc = (ctx.get("partner_country") or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    flag = "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in cc)
    land = f"{flag} {ctx.get('partner_country_title') or cc}"
    return f"{ctx['partner_city']}, {land}" if ctx.get("partner_city") else land


async def _own_ad_context(conv: Conversation) -> Conversation:
    """The public ad page is unreachable (VPN server blocked, willhaben down): for my own ad, title, price and
    description come from the ad editor over the logged-in session instead, so Claude at least knows the asking
    price. Stored without the PayLivery flags, so the next draft tries the public page again."""
    from app.willhaben import insert
    with session() as db:
        mine = db.get(MyAd, conv.ad_id) is not None
    if not mine and conv.role != "seller":
        return conv
    try:
        ad = await insert.get_own_ad(get_session(), conv.ad_id)
    except (NotLoggedIn, SessionUnavailable, insert.InsertError, RuntimeError, ValueError, KeyError) as e:
        log.info("own-ad context for %s failed too: %s", conv.ad_id, e)
        return conv
    try:
        price = f"€ {float(ad.get('price')):g}"
    except (TypeError, ValueError):
        price = None
    # German on purpose: ad_context is part of the prompt Claude answers from
    ctx = {"ad_id": conv.ad_id, "title": ad.get("heading") or conv.ad_title, "price": price,
           "description": insert.description_text(ad.get("description")),
           "status": "aktiv (eigene Anzeige, Daten aus dem Anzeigen-Editor)", "source": "own_ad"}
    with session() as db:
        conv = db.get(Conversation, conv.id)
        conv.ad_context, conv.ad_context_at = ctx, utcnow()
        conv.role = conv.role or "seller"
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


def messages_of(conversation_id: str) -> list[Message]:
    with session() as db:
        return list(db.exec(select(Message).where(Message.conversation_id == conversation_id)
                            .order_by(col(Message.created_at).asc().nulls_first(), Message.id)))


# a last message that only thanks, agrees or says goodbye ends the conversation: no answer is due ("Danke, passt!",
# "Ist angekommen, danke 👍", "Thanks, bye"). A question mark, or any other word, keeps it open.
_CLOSING_PHRASES = sorted([
    "vielen dank", "danke schön", "danke sehr", "dankeschön", "danke", "merci", "passt", "ok", "okay", "oki", "alles klar",
    "super", "perfekt", "top", "prima", "sehr gut", "gut", "gerne", "gern", "bis dann", "bis morgen", "bis später",
    "bis bald", "schönen abend", "schönen tag", "schönes wochenende", "schönen sonntag", "dir auch", "ihnen auch",
    "ebenso", "gleichfalls", "liebe grüße", "lg", "glg", "mfg", "lg lukas", "thanks", "thank you", "thx", "great",
    "perfect", "cheers", "bye", "have a nice day", "you too", "tschüss", "baba", "ciao", "servus"], key=len, reverse=True)
_CLOSING_FILLER = {"und", "auch", "nochmal", "nochmals", "vielmals", "echt", "sehr", "ja", "jo", "alles", "bestens",
                   "ist", "gut", "angekommen", "erhalten", "bekommen", "hat", "geklappt", "dir", "euch", "so", "dann",
                   "a", "lot", "so much", "much", "nice", "all", "good", "arrived", "received", "it", "has", "the",
                   "package", "paket", "artikel", "wieder", "freut", "mich"}
_NOT_WORD = re.compile(r"[^\w\s]", re.U)


# a site's notice that the item arrived or the deal is complete: the chat is over unless they write again
DELIVERED_RE = re.compile(r"zugestellt|wurde (?:erfolgreich )?(?:geliefert|abgeholt)|Bestellung (?:ist )?abgeschlossen|"
                          r"Transaktion (?:ist )?abgeschlossen|Kauf (?:ist )?abgeschlossen|hat (?:den Artikel|das Paket) "
                          r"erhalten|Geld wurde (?:dir )?(?:überwiesen|ausgezahlt)|delivered|order (?:is )?completed",
                          re.I)


def closes_chat(text: str | None) -> bool:
    """The other person's message ends the conversation (thanks, agreed, goodbye – nothing to answer)."""
    s = (text or "").strip().lower()
    if not s or "?" in s or len(s) > 120:
        return False
    s = " " + " ".join(_NOT_WORD.sub(" ", s).split()) + " "
    hit = False
    for phrase in _CLOSING_PHRASES:
        if f" {phrase} " in s:
            s, hit = s.replace(f" {phrase} ", " "), True
    return hit and all(w in _CLOSING_FILLER for w in s.split())


# ------------------------------------------------------------------ the other person's online state
PRESENCE_VINTED_CACHE_S = 300       # Vinted: their profile page, at most every 5 minutes per person
_vinted_presence: dict[str, dict[str, Any]] = {}    # conversation id -> {online, last_seen}


def _when_iso(raw: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))) if raw else None
    except ValueError:
        return None


PROFILE_REFRESH = timedelta(days=7)


def partner_trust(conv: Conversation) -> dict[str, Any]:
    """{flags, facts} of the other person's account (services/trust), as their profile said."""
    from app.services import trust
    return {"flags": trust.flags(conv.partner_since, conv.partner_ratings, conv.platform),
            "facts": trust.facts(conv.partner_since, conv.partner_ratings, conv.partner_reply_time)}


def presence_of(conv: Conversation) -> dict[str, Any] | None:
    """{online: bool, last_seen: datetime | None} of the other person, as their site last said; None when unknown."""
    if conv.platform == "vinted":
        p = _vinted_presence.get(conv.id)
    else:
        p = get_session().presence.get(conv.partner_uuid or "") if conv.partner_uuid else None
    if not p or (p.get("online") is None and not p.get("last_seen")):
        return None
    return {"online": bool(p.get("online")), "last_seen": _when_iso(p.get("last_seen"))}


async def ask_presence(conv: Conversation) -> None:
    """Ask the site for the other person's online state (the open chat shows it): willhaben over the live chat
    socket (the answer arrives as a frame), Vinted from their public profile page."""
    if conv.platform == "vinted":
        partner = (conv.ad_context or {}).get("partner_id")
        if not partner:
            return
        from app.platforms import PlatformError, vinted
        try:
            m = await vinted.get_client().member(partner, cache_s=PRESENCE_VINTED_CACHE_S)
        except (PlatformError, Exception) as e:  # noqa: BLE001 - a nicety
            log.info("vinted presence of %s not loaded: %s", partner, e)
            return
        _vinted_presence[conv.id] = {"online": m.get("online"), "last_seen": m.get("last_seen")}
        if not (conv.ad_context or {}).get("partner_country"):
            _keep_place(conv, m)
    elif conv.partner_uuid:
        await get_session().track_presence(conv.partner_uuid)
        # their place and account (age, PayLivery ratings): from their public profile, again after a week
        stale = conv.partner_trust_at is None or utcnow() - _aware(conv.partner_trust_at) > PROFILE_REFRESH
        if conv.ad_uuid and stale:
            from app.willhaben.public import WillhabenError, get_public
            try:
                prof = await get_public().member_profile(conv.ad_uuid, conv.partner_uuid)
            except (WillhabenError, Exception) as e:  # noqa: BLE001 - a nicety
                log.info("willhaben profile of %s not loaded: %s", conv.id, e)
                return
            with session() as db:
                row = db.get(Conversation, conv.id)
                if row is not None:
                    row.partner_place = prof["place"] or row.partner_place
                    row.partner_place_tried = True
                    row.partner_since, row.partner_ratings = prof["since"], prof["ratings"]
                    row.partner_reply_time, row.partner_trust_at = prof["reply_time"], utcnow()
                    db.add(row)
                    db.commit()


def mark_unread(conversation_id: str) -> None:
    """Unread again here (the blue badge, at least 1) until I open the chat; the site is not told."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is not None:
            conv.marked_unread = True
            db.add(conv)
            db.commit()


def set_finished(conversation_id: str, on: bool = True) -> None:
    """Mark as finished: no answer due (the yellow dot and the sidebar's count leave it out) until they write again."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is not None:
            conv.finished_at = utcnow() if on else None
            conv.finished_by = None                  # mine now, not Claude's
            db.add(conv)
            db.commit()


def _seen_baseline() -> datetime:
    """Chats never opened in the app count as read up to the moment this was first asked (no badge for history)."""
    raw = get_setting("chats_app_seen_since")
    if not raw:
        raw = utcnow().isoformat()
        set_setting("chats_app_seen_since", raw)
    return _aware(datetime.fromisoformat(raw))


def overview(db) -> dict[str, Any]:
    """The chat list's state: the chats it shows (not hidden, not older than the sync window), those where the other
    person wrote last and it is not finished ("waiting"; "finished": marked so, or their last message only thanks or
    says goodbye), those with an answer drafted, and per chat the messages from the other person that
    came after it was last opened in the app ("unread"). The sidebar badges count the same."""
    convs = list(db.exec(select(Conversation).where(col(Conversation.removed).is_not(True))
                         .order_by(col(Conversation.last_message_at).desc().nulls_last())))
    open_drafts = set(db.exec(select(Draft.conversation_id).where(Draft.status == "draft")).all())
    last_dir: dict[str, str] = {}       # each chat's newest real message (not a site notice): in = they wrote last
    incoming: dict[str, list[datetime]] = {}
    answered: dict[str, datetime] = {}  # my newest own message: whoever answers has read what came before –
    # not one the app sent by itself (an automatic answer): nobody looked at the chat for that
    auto_sent = set(db.exec(select(Draft.conversation_id, Draft.text).where(Draft.auto == True,  # noqa: E712
                                                                             Draft.status == "sent")).all())
    last_in: dict[str, tuple[datetime | None, str | None]] = {}
    for cid, direction, at, text in db.exec(select(Message.conversation_id, Message.direction, Message.created_at,
                                                   Message.text).where(col(Message.kind).is_(None))
                                            .order_by(col(Message.created_at).asc().nulls_first(), col(Message.id).asc())):
        last_dir[cid] = direction
        if direction == "in":
            last_in[cid] = (_aware(at) if at is not None else None, text)
        if direction == "in" and at is not None:
            incoming.setdefault(cid, []).append(_aware(at))
        elif direction == "out" and at is not None and (cid, text) not in auto_sent:
            answered[cid] = _aware(at)
    base = _seen_baseline()
    unread = {}
    # the newest stored entry of each chat, notices included: does the history reach the list's newest one?
    newest = {cid: _aware(at) for cid, at in db.exec(select(Message.conversation_id, func.max(Message.created_at))
                                                    .group_by(Message.conversation_id)) if at is not None}
    for c in convs:
        seen = max([base] + [x for x in (_aware(c.app_seen_at), answered.get(c.id)) if x is not None])
        n = sum(1 for at in incoming.get(c.id, []) if at > seen)
        # the site's own count too, for messages not fetched yet – unless the chat was opened here since, or the
        # stored history holds the list's newest entry and their last real message was seen or answered: willhaben
        # counts its own notice ("item shipped") unread again, the "1" came back after every opening (10-26)
        theirs = (last_in.get(c.id) or (None, None))[0]
        fetched = c.last_message_at is not None and c.id in newest and \
            newest[c.id] >= _aware(c.last_message_at) - HISTORY_SLACK
        if c.unread_count and not (c.app_seen_at and c.last_message_at
                                   and _aware(c.app_seen_at) >= _aware(c.last_message_at)) \
                and not (fetched and (theirs is None or seen >= theirs)):
            n = max(n, c.unread_count)
        if c.marked_unread:                 # Mark as unread
            n = max(n, 1)
        if n:
            unread[c.id] = n
    since = sync_window()[1]
    old = {c.id for c in convs if since is not None and c.last_message_at is not None and _aware(c.last_message_at) < since}
    shown = [c for c in convs if not c.hidden_at and c.id not in old]
    by_id = {c.id: c for c in convs}

    # the newest notice of each chat (Vinted's order events, willhaben's PayLivery ones): "delivered" ends a sale
    notices: dict[str, tuple[datetime | None, str]] = {}
    for cid, at, text in db.exec(select(Message.conversation_id, Message.created_at, Message.text).where(
            col(Message.kind).is_not(None)).order_by(col(Message.created_at).asc().nulls_first())):
        notices[cid] = (_aware(at) if at is not None else None, text or "")
    why: dict[str, str] = {}

    def finished(cid: str) -> bool:      # marked finished since their last message, or that message ends it
        at, text = last_in.get(cid, (None, None))
        mark = by_id[cid].finished_at if cid in by_id else None
        if mark and (at is None or _aware(mark) >= at):
            why[cid] = "marked"
            return True
        if closes_chat(text):
            why[cid] = "closing"
            return True
        n_at, n_text = notices.get(cid, (None, ""))
        if DELIVERED_RE.search(n_text) and n_at is not None and (at is None or n_at >= at):
            why[cid] = "delivered"          # the package arrived and they have not written since
            return True
        return False
    done = {cid for cid in by_id if finished(cid)}
    # finished, but within the last day: still in the list (with its tag) – 10-30 a buyer's "ok, vielen dank" took
    # the newest chat out of the list minutes after it came, and it looked lost
    recent = datetime.now(timezone.utc) - timedelta(hours=FINISHED_STAYS_H)
    fresh = {cid for cid in done if by_id[cid].last_message_at is not None and _aware(by_id[cid].last_message_at) >= recent}
    return {"convs": convs, "shown": shown, "old": old, "open_drafts": open_drafts, "finished": done, "finished_why": why,
            "finished_fresh": fresh,
            "thumbs": _thumbs(db, convs),
            "waiting": {cid for cid, d in last_dir.items() if d == "in" and cid not in done}, "unread": unread}


def _thumbs(db, convs: list[Conversation]) -> dict[str, str]:
    """Each chat's item picture for the list: the item page's photo, else my own ad's, the purchase's or a hit's."""
    from app.db import Hit, Purchase
    out = {c.id: ((c.ad_context or {}).get("images") or [None])[0] for c in convs}
    missing = [c for c in convs if not out.get(c.id) and c.ad_id]
    if missing:
        ad_ids = {c.ad_id for c in missing}
        mine = dict(db.exec(select(MyAd.ad_id, MyAd.image_url).where(col(MyAd.ad_id).in_(ad_ids))).all())
        bought = dict(db.exec(select(Purchase.conversation_id, Purchase.image_url).where(
            col(Purchase.conversation_id).in_([c.id for c in missing]))).all())
        hits = dict(db.exec(select(Hit.ad_id, Hit.image_url).where(col(Hit.ad_id).in_(ad_ids))).all())
        for c in missing:
            out[c.id] = mine.get(c.ad_id) or bought.get(c.id) or hits.get(c.ad_id)
    return {k: v for k, v in out.items() if v}


def badge_counts(db) -> dict[str, int]:
    """Sidebar: unread messages (as above) and chats that need me: the other person wrote last, or a draft waits."""
    o = overview(db)
    ids = {c.id for c in o["shown"]}
    return {"chats": sum(n for cid, n in o["unread"].items() if cid in ids),
            "chats_action": len(ids & (o["waiting"] | o["open_drafts"]))}


def summary_counts(db) -> tuple[int, int]:
    """Unread messages and chats with a draft (Home, the bot's /status), as the chat list counts them: shown chats
    only, several drafts of one chat once (the list's "Draft" filter)."""
    o = overview(db)
    ids = {c.id for c in o["shown"]}
    return sum(n for cid, n in o["unread"].items() if cid in ids), len(ids & o["open_drafts"])


_clear_tasks: set[asyncio.Task] = set()


def seen_in_app(conversation_id: str) -> None:
    """The owner looked at the chat in the app: what came so far is read here."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            return
        newest = db.exec(select(func.max(Message.created_at)).where(Message.conversation_id == conversation_id,
                                                                    Message.direction == "in")).one()
        if conv.app_seen_at is not None and not conv.marked_unread and (
                newest is None or _aware(newest) <= _aware(conv.app_seen_at)):
            return                          # nothing new since: no write
        conv.app_seen_at, conv.marked_unread = utcnow(), None
        db.add(conv)
        db.commit()
    try:                                    # its "new message" push goes off the phone
        task = asyncio.get_running_loop().create_task(clear_push(message_tag(conversation_id)))
        _clear_tasks.add(task)
        task.add_done_callback(_clear_tasks.discard)
    except RuntimeError:                    # no loop (a sync caller in tests)
        pass


def live_state(conversation_id: str | None = None) -> dict[str, str]:
    """Short fingerprints of what the chat pages show: "list" and, for an open conversation, "messages", "composer"
    and "context". The pages poll them (local database only, never willhaben) and swap just the part that changed."""
    from app.db import Purchase, Shipment

    def fp(*parts: Any) -> str:
        return hashlib.sha1(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()[:12]

    with session() as db:
        convs = db.exec(select(Conversation.id, Conversation.last_message_at, Conversation.unread_count,
                               Conversation.last_message_text, Conversation.hidden_at, Conversation.removed,
                               Conversation.app_seen_at, Conversation.finished_at, Conversation.marked_unread)).all()
        open_drafts = sorted(set(db.exec(select(Draft.conversation_id).where(Draft.status == "draft")).all()))
        out = {"list": fp([list(r) for r in convs], open_drafts, get_session().chat_live.get("state"))}
        conv = db.get(Conversation, conversation_id) if conversation_id else None
        if conv is not None:
            msgs = db.exec(select(func.count(), func.max(Message.id), func.sum(case((col(Message.read).is_(True), 1), else_=0)),
                                  func.max(Message.read_at), func.count(Message.translation))
                           .where(Message.conversation_id == conv.id)).one()   # seen ticks and translations too
            drafts = db.exec(select(Draft.id, Draft.status, Draft.text, Draft.error, Draft.intent, Draft.translation)
                             .where(Draft.conversation_id == conv.id)).all()
            ad = current_ad(db, conv.ad_id) if conv.ad_id else None      # the sale status lives on the live copy
            ships = [r.model_dump(mode="json") for r in db.exec(select(Shipment).where(Shipment.conversation_id == conv.id))]
            buys = [r.model_dump(mode="json") for r in db.exec(select(Purchase).where(Purchase.conversation_id == conv.id))]
            out["messages"] = fp(list(msgs))
            out["presence"] = fp(presence_of(conv), partner_place(conv), conv.partner_since, conv.partner_ratings,
                                 conv.partner_reply_time)
            out["composer"] = fp([list(d) for d in drafts], conv.drafting, conv.draft_error)
            out["context"] = fp(conv.ad_context, conv.meetup, conv.paylivery, conv.hidden_at,
                                ad and [ad.sale_status, ad.sale_conversation_id, ad.status, stock.numbers(ad),
                                        [s.id for s in stock.sales_to(conv.id)]], ships, buys)
    return out


def drafts_of(conversation_id: str) -> list[Draft]:
    with session() as db:
        return list(db.exec(select(Draft).where(Draft.conversation_id == conversation_id)
                            .order_by(Draft.created_at.desc())))


def _style_examples(limit: int = MAX_STYLE_EXAMPLES) -> list[str]:
    """The owner's own voice: messages they typed themselves on willhaben (outgoing, not sent from a Claude draft),
    newest first; only when there are too few, sent drafts rated 👍 in Telegram fill up. Never the partner's messages."""
    with session() as db:
        sent = {style._norm(d) for d in db.exec(select(Draft.text).where(          # as compared below; typed by me
            Draft.status == "sent", (col(Draft.model).is_(None)) | (Draft.model != "manual"))).all()}  # ("Send directly"): my voice
        rows = db.exec(select(Message).where(Message.direction == "out", Message.kind.is_(None))  # type: ignore[union-attr]
                       .order_by(Message.created_at.desc()).limit(200))
        out: list[str] = []
        for m in rows:
            text = " ".join(m.text.split())
            # an address, phone number or IBAN I sent one buyer is no style example for the next one's prompt
            if len(text) < 8 or text in sent or text in out or style.has_contact_data(text):
                continue
            out.append(text)
            if len(out) == limit:
                return out
        if len(out) < limit:
            liked = db.exec(select(Draft).where(Draft.status == "sent", Draft.feedback == "up")
                            .order_by(Draft.sent_at.desc()).limit(limit - len(out)))
            out += [r.text for r in liked if r.text not in out and not style.has_contact_data(r.text)]
        return out


OTHERS_MAX_AGE = timedelta(days=30)     # what I told other buyers longer ago is no fact about the item any more


def _others_said(conversation_id: str, ad_id: str | None, limit: int = MAX_OTHERS) -> list[str]:
    """What I wrote to the other buyers of the same ad (every copy of a renewed ad, my seller chats only) in the last
    OTHERS_MAX_AGE, newest first: facts about the item ("funktioniert", "hab nur die eine") that the next buyer asks
    again. No memory is needed for that; the prompt tells Claude to keep prices, commitments and appointments with
    others to itself. Left out: the sold/reserved notices (app/services/sold.py; availability comes from the ad and
    the stock, a lifted reservation must not live on here), what Claude wrote (sent drafts) and lines with contact data
    (an address I sent one buyer)."""
    from app.services import sold

    if not ad_id:
        return []
    ids = stock.ad_chain(ad_id)
    with session() as db:
        convs = db.exec(select(Conversation.id).where(col(Conversation.ad_id).in_(ids), Conversation.role == "seller",
                                                      Conversation.id != conversation_id)).all()
        if not convs:
            return []
        notices = {style._norm(x) for x in db.exec(select(Draft.text).where(
            col(Draft.intent).in_(list(sold.NOTICE_INTENTS.values())), col(Draft.conversation_id).in_(convs)))}
        # what Claude wrote there (a sent draft, automatic or approved) is no fact I stated: a claim it made up in
        # one chat must not become "what the seller said" in the next
        claude = {style._norm(x) for x in db.exec(select(Draft.text).where(
            Draft.status == "sent", col(Draft.conversation_id).in_(convs))) if x}
        rows = db.exec(select(Message).where(col(Message.conversation_id).in_(convs), Message.direction == "out",
                                             col(Message.kind).is_(None),
                                             col(Message.created_at) >= utcnow() - OTHERS_MAX_AGE)
                       .order_by(col(Message.created_at).desc(), col(Message.id).desc()).limit(60))
        out: list[str] = []
        for m in rows:
            text = style._norm(m.text)
            if len(text) < 8 or text in out or text in notices or text in claude or style.has_contact_data(text):
                continue
            out.append(text)
            if len(out) == limit:
                break
        return out


MAX_ALTERNATIVES = 3


def clean_alternatives(raw: Any) -> list[dict[str, str]] | None:
    """Claude's other decisions for the situation (one-tap buttons under a held-back answer), None when there are none."""
    out: list[dict[str, str]] = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        # "|" separates the parts of a Telegram button ref (draft_id|label|instruction)
        label = " ".join(str(a.get("label") or "").replace("|", "/").split())[:30].strip()
        instruction = str(a.get("instruction") or "").strip()[:200].strip()
        if label and instruction:
            out.append({"label": label, "instruction": instruction})
        if len(out) == MAX_ALTERNATIVES:
            break
    return out or None


def ad_status_label(status: str | None) -> str:
    """The ad's status from a chat's ad context, for the page (Jinja global): stored in German or as the site's word
    because Claude reads it; shown in the UI language, and "" while the ad is simply online."""
    low = (status or "").strip().lower()
    if not low or low.startswith(("active", "aktiv")):
        return ""
    if "nicht mehr online" in low or low in ("gone", "deleted"):
        return t("no longer online")
    for key in ("sold", "reserved", "expired"):
        if low.startswith(key) or low.startswith({"sold": "verkauft", "reserved": "reserviert", "expired": "abgelaufen"}[key]):
            return t(key)
    return status or ""


def intent_label(intent: str | None) -> str:
    """A draft's intent in the UI language (Jinja global, the draft's header on the chat page): REPLY_SCHEMA's enum
    and sold.NOTICE_INTENTS. "" for none and for "other" (says nothing); an unknown value (a draft saved over the API)
    shows as written, underscores as spaces."""
    if not intent or intent == "other":
        return ""
    labels = {"availability": t("Availability"), "price_negotiation": t("Price negotiation"),
              "logistics": t("Pickup & shipping"), "question": t("Question"), "agreement": t("Agreement"),
              "scam_suspect": t("Suspected scam"), "sold_notice": t("Sold notice"), "reserved_notice": t("Reserved notice")}
    return labels.get(intent) or intent.replace("_", " ")


@timeline.scoped(conversation_id="conversation_id")
async def draft_reply(conversation_id: str, hint: str | None = None, auto: bool = False) -> Draft:
    conv = await ensure_ad_context(conversation_id)
    if conv is None:
        raise LLMError(t("Conversation not found"))
    msgs = messages_of(conversation_id)
    if not msgs and conv.last_message_text:
        msgs_t = [("in" if conv.unread_count else "unknown", conv.last_message_at, conv.last_message_text)]
    else:
        msgs_t = [(m.direction, m.created_at, m.text) for m in msgs]
    buy = purchase.for_conversation(conversation_id) if conv.role == "buyer" else None
    timeline.update_context(ad_id=conv.ad_id, purchase_id=buy.id if buy else None, mine=conv.role == "seller" or None)
    if auto and conv.role == "buyer" and buy is None:
        raise LLMError(t("No open purchase for this chat"))
    ad = None
    if buy is None:
        with session() as db:
            ad = current_ad(db, conv.ad_id) if conv.ad_id else None
    req = await compose(conv, msgs_t, auto=auto, ad=ad, buy=buy, hint=hint)
    log.info("draft for %s: hint=%r, ad price=%s", conversation_id, hint, (req["ad_ctx"] or {}).get("price"))
    async with telegram.typing():                 # "typing…" in Telegram while Claude writes
        result = await get_backend(req["backend"]).complete(system=req["system"], prompt=req["prompt"],
                                                            schema=req["schema"])
    draft = draft_from(result, conversation_id, auto=auto, buyer=buy is not None)
    pl_args = paylivery_advice.for_draft(conv, ad)
    draft.paylivery = paylivery_advice.clean((result.data or {}).get("paylivery_action"), *pl_args,
                                             before=paylivery_advice.previous_amount(
                                                 conversation_id, (pl_args[0] or {}).get("offer")))
    with session() as db:
        db.add(draft)
        db.commit()
        db.refresh(draft)
    from app.services import translate
    translate.schedule_draft(draft.id)          # in the other person's language: its translation for me
    return draft


async def compose(conv: Conversation, msgs_t: list[tuple[str, datetime | None, str]], *, auto: bool,
                  ad: MyAd | None, buy: Any = None, hint: str | None = None,
                  overrides: dict[str, str] | None = None, platform: str | None = None) -> dict[str, Any]:
    """What Claude gets for one answer: {system, schema, prompt, backend, ad_ctx}. Reads settings, the calendar and
    the owner's own messages (style), writes nothing: the chat simulator (app/services/chat_sim.py) builds its
    answers here too, with unsaved rows. The system prompts of automatic answers are the owner's edited ones, if any
    (app/services/system_prompts.py). `overrides` replaces them or the owner's rules for a test run:
    "auto_reply_system", "purchase_system", "auto_reply_rules", "purchase_rules", "ad_instructions"; `platform`
    names a site other than willhaben (default: the chat's own; real drafts of a Vinted chat got neither the Vinted
    rule nor the language rule before it was taken from there)."""
    overrides = overrides or {}
    platform = platform or conv.platform
    cal = gcal.config()
    if buy is not None:                           # buyer: the purchase's rules replace the seller's rules
        availability = await gcal.availability_text() if (buy.mode == "pickup" and gcal.connected()) else None
        rules = purchase.rules_block(buy, conv, availability=availability, user_rules=overrides.get("purchase_rules"))
        if auto and (lessons := feedback.prompt_block()):
            rules = f"{rules}\n\n{lessons}"
        availability = None
        system, schema = ((overrides.get("purchase_system") or system_prompts.get("purchase"), PURCHASE_SCHEMA)
                          if auto else (REPLY_SYSTEM, REPLY_SCHEMA))
        examples = style.examples()                       # buying sounds like me too
        stock_now, others = None, []
    else:
        rules = get_setting("reply_rules")
        if conv.role == "seller":                     # Settings → Selling preferences, before the per-answer blocks
            rules = selling.append_to(rules)
        floor = ad.min_price if ad and conv.role == "seller" else None
        numbers = stock.numbers(ad) if conv.role == "seller" else None    # several pieces: Claude knows how many are left
        stock_now = {"quantity": numbers["quantity"], "remaining": numbers["remaining"]} if numbers else None
        asking = (conv.ad_context or {}).get("price")
        if auto:
            general = overrides.get("auto_reply_rules", get_setting("auto_reply_rules"))
            instructions = overrides.get("ad_instructions", ad.auto_reply_instructions if ad else None)
            rules = f"{rules}\n\n{auto_rules(general, instructions, floor, asking)}"
            if lessons := feedback.prompt_block():     # the owner's 👎 notes on earlier automatic answers
                rules = f"{rules}\n\n{lessons}"
        elif floor is not None:                       # manual drafts of my own ad: same negotiation range
            rules = f"{rules}\n\n{price_floor(floor, asking)}"
        availability = await gcal.availability_text() if (cal["in_auto"] if auto else cal["in_drafts"]) else None
        system, schema = ((overrides.get("auto_reply_system") or system_prompts.get("auto_reply"), AUTO_REPLY_SCHEMA)
                          if auto else (REPLY_SYSTEM, REPLY_SCHEMA))
        examples = style.examples()                       # the profile's characteristic messages, then the newest
        others = _others_said(conv.id, conv.ad_id) if conv.role == "seller" else []
    if line := platform_rule(platform):
        rules = f"{rules}\n\n{line}"
    if conv.role == "buyer" and buy is None and conv.ad_id:      # the price check's rating, if there was one
        from app.platforms import ad_ref
        if advice := deals.buyer_advice(ad_ref(platform, conv.ad_id)):
            rules = f"{rules}\n\n{advice}"
    country = await _vinted_partner_country(conv) if platform == "vinted" else None
    if line := language_rule(country, partner_language(conv.id) if conv.id else None):
        rules = f"{rules}\n\n{line}"
    pl, pl_asking, pl_floor = paylivery_advice.for_draft(conv, ad)
    if pl:                                        # an open PayLivery offer: the draft suggests the step on willhaben too
        before = paylivery_advice.previous_amount(conv.id, pl["offer"]) if conv.id else None
        rules = f"{rules}\n\n{paylivery_advice.prompt_block(pl, pl_asking, pl_floor, before)}"
        schema = paylivery_advice.schema(schema)
    ad_ctx = conv.ad_context or ({"title": conv.ad_title} if conv.ad_title else None)   # title at least, price unknown
    prompt = reply_prompt(role=conv.role, ad=ad_ctx, partner=conv.partner_name, messages=msgs_t,
                          rules=rules, examples=examples, hint=hint, availability=availability, stock=stock_now,
                          style=style.profile() or None, others=others)
    return {"system": system, "schema": schema, "prompt": prompt, "ad_ctx": ad_ctx,
            "backend": "purchase" if buy is not None else "chat"}   # chats of a purchase order: Settings → "Purchases"


def draft_from(result: Any, conversation_id: str, *, auto: bool, buyer: bool) -> Draft:
    """Claude's answer as an (unsaved) Draft."""
    d = result.data
    note = (d.get("note") or "").strip()
    # "Hinweis:" marks Claude's note among needs_info (stored in the DB, matched below and in chat.html)
    buyer_auto = auto and buyer
    return Draft(conversation_id=conversation_id, text=d.get("reply", "").strip(), intent=d.get("intent"),
                 needs_info=(d.get("needs_info") or []) + ([f"Hinweis: {note}"] if note else []),
                 suggested_price=d.get("suggested_price"), model=result.model, meetup=meetups.clean(d.get("meetup")),
                 alternatives=clean_alternatives(d.get("alternatives")),
                 auto=True if auto else None,
                 auto_reason=(("✓ " if d.get("auto_send") else "✗ ") + (d.get("auto_reason") or "").strip()) if auto else None,
                 purchase=purchase.clean_state(d.get("purchase_state")) if buyer_auto else None)


async def mark_read(conversation_id: str) -> int:
    """Read receipts for the buyer's unread messages (best effort: never blocks answering)."""
    from app.platforms import vinted_inbox
    if vinted_inbox.is_vinted(conversation_id):  # opening it on Vinted marks it read; nothing to send from here
        return 0
    try:
        n = await _account().mark_read(conversation_id)
    except Exception as e:  # noqa: BLE001 - read receipts are cosmetic, answering must go on
        log.info("mark read failed for %s: %s", conversation_id, e)
        return 0
    if n:
        log.info("marked %d message(s) read in %s", n, conversation_id)
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv and conv.unread_count:
            conv.unread_count = 0
            db.add(conv)
            db.commit()
    return n


class AlreadySent(ValueError):
    """The draft went out already or is going out right now (a second tap on a push, Telegram and Home Assistant
    together, a double click)."""


_in_flight: set[int] = set()   # drafts between the claim and mark_sent


async def send_draft(draft_id: int, text: str) -> Draft:
    """Send an approved draft, once: the claim (check + mark in flight) runs without an await in between, so two
    callers in the event loop cannot both pass it. Raises EndpointUnknown until the send endpoint has been captured."""
    with session() as db:
        draft = db.get(Draft, draft_id)
    if draft is None:
        raise ValueError(t("Draft not found"))
    if draft.status in ("sent", "discarded"):
        raise AlreadySent(t("This answer was already sent") if draft.status == "sent" else t("This draft was discarded"))
    if draft_id in _in_flight:
        raise AlreadySent(t("This answer is being sent right now"))
    _in_flight.add(draft_id)
    try:
        return await _send_claimed(draft, text)
    finally:
        _in_flight.discard(draft_id)


async def _send_claimed(draft: Draft, text: str) -> Draft:
    from app.platforms import vinted_inbox
    draft_id = draft.id
    text = text.strip()
    vinted = vinted_inbox.is_vinted(draft.conversation_id)
    sent_on_vinted = None
    if not vinted:
        await mark_read(draft.conversation_id)   # nobody answers without having read the message
    try:
        if vinted:                               # its page in a tab, typed and sent there (vinted_inbox.send)
            try:
                sent_on_vinted = await vinted_inbox.send(draft.conversation_id, text)
            except vinted_inbox.SendError as e:
                raise SendFailed(t("Vinted did not confirm the message: {error}", error=e)) from e
        else:
            await _account().send_message(draft.conversation_id, text)
    except (EndpointUnknown, SendFailed, NotLoggedIn, SessionUnavailable, ValueError) as e:   # keep the edited text
        with session() as db:
            draft = db.get(Draft, draft_id)
            draft.text, draft.error = text, str(e)
            db.add(draft)
            db.commit()
        raise
    if not vinted and get_setting("send_verified") != "1":
        set_setting("send_verified", "1")     # sending works on this account: auto replies may now send
        log.info("first send confirmed via history; auto replies enabled")
    sent = mark_sent(draft_id, text)
    if sent_on_vinted:                            # the local copy under Vinted's id: the next sync finds it, no double
        vinted_inbox.record_sent(draft_id, sent_on_vinted)
    try:
        await meetups.on_sent(sent)           # agreed appointment -> "add to calendar?" push
    except Exception as e:  # noqa: BLE001 - the message is out; the calendar offer is a bonus
        log.warning("calendar offer for draft %s failed: %s", draft_id, e)
    return sent


def enqueue_send(draft_id: int, text: str, now: bool = False) -> bool:
    """The answer is marked "sending" right away and goes out as a background job (chat lane, never behind ad jobs);
    it waits the grace period to be taken back, unless `now` (Send now).

    The state lives in the database, so a restart cannot lose it silently (recover_sending). False: nothing queued
    (draft gone, not open any more, or empty text).
    """
    text = text.strip()
    with session() as db:
        d = db.get(Draft, draft_id)
        if d is None or d.status != "draft" or not text:
            return False
        d.text, d.status, d.error = text, "sending", None
        db.add(d)
        db.commit()
        conv = db.get(Conversation, d.conversation_id)
    who = (conv.partner_name if conv else None) or t("unknown")

    def cancelled() -> None:          # taken back while queued: an open draft again, text as edited
        with session() as db:
            row = db.get(Draft, draft_id)
            if row and row.status == "sending":
                row.status = "draft"
                db.add(row)
                db.commit()

    # a sent message cannot be taken back: it waits the grace period in the queue (cancel instead of a confirm)
    jobs.submit("chat-send", t("Answer to {name}", name=who), lambda job: _send_job(draft_id), lane="chat",
                current=f"draft:{draft_id}", on_cancel=cancelled, grace=not now, quiet=True)
    return True


async def _send_job(draft_id: int) -> str:
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None or d.status != "sending":
        return t("Not sent: the draft changed meanwhile")
    try:
        await send_draft(draft_id, d.text)
    except BaseException as e:  # back to an open draft with the reason; the text stays as it was edited
        cut = not isinstance(e, Exception)      # the job's time ran out (cancelled): it may have gone out
        with session() as db:
            row = db.get(Draft, draft_id)
            if row and row.status == "sending":
                row.status = "draft"
                row.error = t("Interrupted – it is unclear whether it went out: reload the history before sending again") \
                    if cut else str(e)
                db.add(row)
                db.commit()
        raise
    return t("Sent – confirmed in the willhaben history")


def recover_sending() -> None:
    """After a restart the queue is gone. Never sent again automatically: the message may have gone out just before.
    A draft Claude was still writing is simply not there: the chat offers "Prepare answer" again."""
    with session() as db:
        for d in db.exec(select(Draft).where(Draft.status == "sending")):
            d.status = "draft"
            d.error = t("Interrupted by a restart – it is unclear whether it went out: reload the history before sending again")
            db.add(d)
        for c in db.exec(select(Conversation).where(Conversation.drafting == True)):  # noqa: E712
            c.drafting = None
            db.add(c)
        db.commit()


def _set_drafting(conversation_id: str, on: bool, error: str | None = None) -> None:
    with session() as db:
        c = db.get(Conversation, conversation_id)
        if c is not None:
            c.drafting, c.draft_error = (True if on else None), error
            db.add(c)
            db.commit()


def enqueue_draft(conversation_id: str, hint: str | None = None) -> bool:
    """"Prepare answer" as a background job (lane claude, 10–60 s): the chat shows "Claude is writing…" and the new
    draft appears by itself (live view). False: this chat already has one on the way."""
    with session() as db:
        c = db.get(Conversation, conversation_id)
        if c is None or c.drafting:
            return False
    _set_drafting(conversation_id, True)

    async def run(job: jobs.Job) -> str:
        try:
            await draft_reply(conversation_id, hint)
        except Exception as e:               # the reason shows in the chat, next to "Prepare answer"
            _set_drafting(conversation_id, False, t("Draft failed: {error}", error=e))
            raise
        except BaseException:                # the job's time ran out: no "Claude is writing…" forever
            _set_drafting(conversation_id, False, t("Draft interrupted"))
            raise
        _set_drafting(conversation_id, False)
        return t("Draft ready")

    who = (c.partner_name if c else None) or t("unknown")
    jobs.submit("chat-draft", t("Claude writes an answer to {name}", name=who), run, lane="claude",
                current=f"drafting:{conversation_id}", on_cancel=lambda: _set_drafting(conversation_id, False), quiet=True)
    return True


def enqueue_sync() -> jobs.Job:
    """"Sync" as a background job (lane chat): every conversation in the window is read again (one request per
    chat); the list updates itself (live view). A sync already waiting is reused."""
    async def run(job: jobs.Job) -> str:
        from app.platforms import vinted_inbox
        res = await sync(force=True)
        from app import platforms
        if platforms.enabled("vinted"):
            try:
                await vinted_inbox.sync(force=True)       # Vinted's chats with it (hidden tab of its inbox)
            except Exception as e:  # noqa: BLE001 - willhaben's result counts
                log.warning("vinted chat sync failed: %s", e)
        if res.get("skipped"):           # not logged in / browser off: shown with a warning sign
            job.problems = 1
            return res["skipped"]
        return t("{n} conversations, {changed} updated", n=res["conversations"], changed=res["changed"])

    return jobs.submit("chat-sync", t("Sync chats with willhaben"), run, lane="chat", reuse_queued=True)


def enqueue_reload(conversation_id: str) -> jobs.Job:
    """"Reload" of one chat as a background job (lane chat); the thread updates itself (live view). The ad's data
    (price, status) is fetched again too, whatever its age."""
    async def run(job: jobs.Job) -> str:
        mode = await sync_messages(conversation_id)
        await ensure_ad_context(conversation_id, force=True)
        return "" if mode == "full" else t("Only the last message is available (messages endpoint not verified yet).")

    with session() as db:
        c = db.get(Conversation, conversation_id)
    return jobs.submit(f"chat-reload-{conversation_id}", t("Reload the chat with {name}", name=(c.partner_name if c else None) or t("unknown")),
                       run, lane="chat", reuse_queued=True)


# ------------------------------------------------------------------ automatic answers
AUTO_PER_CONVERSATION_PER_DAY = 3
AUTO_PER_HOUR = 10


def _auto_ad(conv: Conversation) -> MyAd | None:
    """The own ad whose buyers get automatic answers, if this conversation qualifies."""
    if conv.role != "seller" or not conv.ad_id:
        return None
    with session() as db:
        ad = current_ad(db, conv.ad_id)
    if ad and (my_ads.is_sold(ad) or ad.status == "deleted"):
        return None   # sold out / given away / gone: no automatic answers (the sold workflow offers a polite draft)
    return ad if ad and ad.auto_reply else None


def _auto_blockers(conv_id: str, d: Draft, msgs: list[Message]) -> list[str]:
    """Hard checks in code, independent of what the model says (seller and buyer chats)."""
    why = []
    with session() as db:
        conv = db.get(Conversation, conv_id)
    buyer = conv is not None and conv.role == "buyer"
    if get_setting("send_verified") != "1":
        why.append(t("Sending not confirmed yet – send once manually from the dashboard"))
    p = purchase.for_conversation(conv_id) if buyer else None
    if buyer and p is None:
        why.append(t("No open purchase for this chat"))
    ad = None
    if not buyer:
        with session() as db:
            ad = current_ad(db, conv.ad_id) if conv and conv.ad_id else None
    why += answer_blockers(conv, d, msgs, ad=ad, buy=p, buyer=buyer)
    last = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
    if last is None or last.direction != "in":
        why.append(t("last message is not from the other person"))
    now = utcnow()
    with session() as db:
        sent = list(db.exec(select(Draft).where(Draft.auto == True, Draft.status == "sent",  # noqa: E712
                                                Draft.sent_at >= now - timedelta(days=1))))
    per_day = purchase.PER_CONVERSATION_PER_DAY if buyer else AUTO_PER_CONVERSATION_PER_DAY
    if sum(1 for x in sent if x.conversation_id == conv_id) >= per_day:
        why.append(t("already {n} automatic answers today in this chat", n=per_day))
    if sum(1 for x in sent if x.sent_at and x.sent_at >= now - timedelta(hours=1)) >= AUTO_PER_HOUR:
        why.append(t("Limit of {n} automatic answers per hour reached", n=AUTO_PER_HOUR))
    return why


def answer_blockers(conv: Conversation | None, d: Draft, msgs: list[Message], *, ad: MyAd | None, buy: Any,
                    buyer: bool) -> list[str]:
    """The checks of one answer's content (no limits, no send state): what Claude says, the price floor or the
    purchase's maximum, stock, contact data, staying on topic. Shared with the chat simulator."""
    why = []
    if not d.auto_reason or not d.auto_reason.startswith("✓"):
        why.append(t("Claude does not recommend an automatic answer"))
    if d.intent == "scam_suspect":
        why.append(t("Suspected scam"))
    if any(not x.startswith("Hinweis:") for x in d.needs_info or []):
        why.append(t("Claude needs info: {items}", items=", ".join(x for x in d.needs_info if not x.startswith("Hinweis:"))))
    if "[" in d.text and "]" in d.text:
        why.append(t("Answer contains placeholders"))
    if not d.text.strip():
        why.append(t("empty answer"))
    if d.paylivery:                       # a step on willhaben goes with it: the owner sends both
        why.append(t("A PayLivery step goes with this answer"))
    if buyer:                             # purchase guards: maximum price, bank details, address, needs_user
        if buy is not None:
            why += purchase.blockers(buy, d, msgs)
    else:                                 # price floor of the own ad, checked in code (app/services/min_price.py)
        why += min_price.blockers(ad.min_price if ad else None, d.suggested_price, d.text,
                                  asking=min_price.asking_value((conv.ad_context or {}).get("price") if conv else None),
                                  intent=d.intent)
        why += stock.blockers(ad, d.text)   # never more pieces than are left (app/services/stock.py)
        if selling.contact_hits(d.text):
            why.append(t("Answer contains an e-mail address or link – willhaben censors them"))
        why += topic.blockers(d.text)     # staying on topic: no instruction words or AI talk, no essays
    return why


AUTO_MAX_AGE = timedelta(hours=48)


def _auto_mode(conv: Conversation) -> str | None:
    """"seller" (auto replies to buyers of my ad), "buyer" (an open purchase of this chat), or None."""
    if conv.role == "buyer":
        return "buyer" if purchase.auto_mode(conv) else None
    return "seller" if _auto_ad(conv) is not None else None


async def _purchase_after(conversation_id: str, d: Draft, sent: bool) -> None:
    """Buyer chats: the purchase follows the answer (status, pushes, PayLivery offer). Never blocks the answer."""
    p = purchase.for_conversation(conversation_id)
    if p is None:
        return
    try:
        await purchase.after_reply(p, d, sent)
    except Exception as e:  # noqa: BLE001 - the answer is already out or blocked; the purchase is secondary
        log.warning("purchase update for %s failed: %s", conversation_id, e)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _waiting_for_answer(conv_id: str) -> bool:
    """Buyer's last message is recent and nobody (me or the app) has drafted or sent anything since. A draft the
    owner discarded counts too: they decided not to answer that message, so no new answer is written and offered at
    every sync (the next message from the buyer is answered again)."""
    msgs = messages_of(conv_id)
    last = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
    if last is None or last.direction != "in" or last.created_at is None:
        return False
    since = _aware(last.created_at)
    if utcnow() - since > AUTO_MAX_AGE:
        return False
    with session() as db:
        later = db.exec(select(Draft).where(Draft.conversation_id == conv_id,
                                            (Draft.status != "discarded") | (Draft.dismissed == True))).all()  # noqa: E712
    alive = conv_id in _humanized
    # "pending" = a human-like answer still "typing"; after a restart its task is gone and it must not block
    return not any(_aware(d.created_at) >= since for d in later if d.status != "pending" or alive)


# ------------------------------------------------------------------ live chat events
LIVE_MIN_GAP_S = 15          # at most one sync per 15 s, whatever the socket pushes (willhaben budget)
_live = {"pending": False, "task": None, "last": 0.0}
last_sync_at: list = [None]  # set by sync(); the poll job skips while live is healthy and this is fresh


async def on_live_event(conversation_id: str, frame: dict[str, Any]) -> None:
    """Called for every chat frame the live WebSocket pushes; coalesces bursts into one sync. The other person
    reading my messages is stored right away and needs no sync."""
    if _seen_event(conversation_id, frame):
        return
    _live["pending"] = True
    if _live["task"] is None or _live["task"].done():
        _live["task"] = asyncio.create_task(_live_sync())


async def _live_sync() -> None:
    loop = asyncio.get_running_loop()
    while _live["pending"]:
        wait = LIVE_MIN_GAP_S - (loop.time() - _live["last"])
        await asyncio.sleep(max(wait, 1.5))       # also lets a burst of frames settle
        _live["pending"] = False
        _live["last"] = loop.time()
        try:
            res = await sync()
            log.info("live chat sync: %s", res)
        except Exception as e:  # noqa: BLE001 - keep listening
            log.warning("live chat sync failed: %s", e)


_auto_lock = asyncio.Lock()   # scheduled sync, manual sync and the toggle may overlap: never answer twice


async def process_pending_auto(ad_id: str | None = None) -> int:
    """Answer every waiting seller conversation of ads with auto reply on (optionally just one ad)."""
    async with _auto_lock:
        return await _process_pending_auto(ad_id)


async def _process_pending_auto(ad_id: str | None) -> int:
    with session() as db:
        q = select(Conversation).where(Conversation.role.in_(("seller", "buyer")))
        if ad_id:
            q = q.where(Conversation.ad_id == ad_id)
        convs = list(db.exec(q))
    done = 0
    for conv in convs:
        if _auto_mode(conv) is None or not _waiting_for_answer(conv.id) or _auto_given_up(conv.id):
            continue
        if humanize.enabled():
            _schedule_humanized(conv.id)   # answers later in the background, sync goes on
            continue
        try:
            d = await auto_reply(conv.id)
        except Exception as e:  # noqa: BLE001 - the owner is told (push, chat page); the other chats are still answered
            await _auto_failed(conv.id, e)
            continue
        done += bool(d and d.status == "sent")
    return done


AUTO_MAX_ATTEMPTS = 3   # tries per buyer message when writing or sending the automatic answer fails (Claude down, …)
_auto_failures: dict[str, tuple[str, int]] = {}   # conversation id -> (buyer message ext_id, failed attempts)


def _auto_given_up(conversation_id: str) -> bool:
    """AUTO_MAX_ATTEMPTS automatic answers to the buyer's current message failed: the owner knows (_auto_failed),
    the sync leaves the chat alone until the buyer writes again."""
    ext, attempts = _auto_failures.get(conversation_id, ("", 0))
    if attempts < AUTO_MAX_ATTEMPTS:
        return False
    last = _last_incoming_msg(conversation_id)
    return last is not None and last.ext_id == ext


async def _auto_failed(conversation_id: str, error: Exception) -> None:
    """An automatic answer could not be written or sent (Claude or willhaben down, a bug): the chat page shows the
    reason next to "Prepare answer" (Conversation.draft_error), and the owner gets one push per buyer message - the
    question with the reason, so they answer themselves - instead of a silent retry at every sync."""
    if isinstance(error, (LLMError, WillhabenError, NotLoggedIn, SessionUnavailable, EndpointUnknown, SendFailed)):
        log.warning("auto reply failed for %s: %s", conversation_id, error)
    else:
        log.error("auto reply failed for %s: %s", conversation_id, error, exc_info=error)
    last = _last_incoming_msg(conversation_id)
    ext = last.ext_id if last else ""
    prev_ext, attempts = _auto_failures.get(conversation_id, ("", 0))
    attempts = attempts + 1 if prev_ext == ext else 1
    _auto_failures[conversation_id] = (ext, attempts)
    reason = t("Automatic answer failed: {error}", error=error)
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            return
        conv.draft_error = reason
        db.add(conv)
        db.commit()
    if attempts > 1:
        return                        # pushed for this message already
    from app.config import get_settings
    q = last.text if last else conv.last_message_text
    await notify("auto_reply_pending", t("Not answered automatically: {name}", name=conv.partner_name or "").strip(),
                 (t("Question: “{text}”", text=q[:120]) + "\n" if q else "") + f"{conv.ad_title or ''} · {reason}",
                 url=f"{get_settings().public_url.rstrip('/')}/chats/{conv.id}",
                 data={"conversation_id": conv.id, "ad_id": conv.ad_id}, tag=message_tag(conv.id))


def _auto_recovered(conversation_id: str) -> None:
    """Claude wrote the automatic answer: an earlier failure's note on the chat page goes."""
    _auto_failures.pop(conversation_id, None)
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is not None and conv.draft_error:
            conv.draft_error = None
            db.add(conv)
            db.commit()


def reset() -> None:
    """Tests: forget the failed answers of the previous test's conversations."""
    _auto_failures.clear()


# ------------------------------------------------------------------ human-like timing
_humanized: dict[str, asyncio.Task] = {}   # conversation id -> pending delayed answer


def _schedule_humanized(conversation_id: str) -> None:
    task = _humanized.get(conversation_id)
    if task is not None and not task.done():
        return
    _humanized[conversation_id] = asyncio.create_task(_humanized_auto(conversation_id))


def _last_incoming_msg(conversation_id: str) -> Message | None:
    msgs = [m for m in messages_of(conversation_id) if m.direction == "in"]
    return msgs[-1] if msgs else None


async def _sleep_until(when) -> None:
    wait = (_aware(when) - utcnow()).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)


async def _humanized_auto(conversation_id: str) -> Draft | None:
    """Notice the message after a while, read it (receipt), think, type, then send - re-checking in between."""
    with session() as db:
        for orphan in db.exec(select(Draft).where(Draft.conversation_id == conversation_id, Draft.status == "pending")):
            orphan.status = "discarded"
            db.add(orphan)
        db.commit()
    try:
        for _ in range(3):   # a new buyer message while "typing" means: read again and answer that
            last = _last_incoming_msg(conversation_id)
            if last is None:
                return None
            warm = any(m.direction == "out" for m in messages_of(conversation_id))   # I answered before
            plan = humanize.plan(_aware(last.created_at), warm=warm)
            await _sleep_until(plan.read_at)
            await sync_messages(conversation_id)
            if not _waiting_for_answer(conversation_id):
                return None            # answered meanwhile (manually, on the phone, ...)
            newest = _last_incoming_msg(conversation_id)
            if newest is not None and newest.ext_id != last.ext_id:
                continue               # more came in before we "looked": plan from the newest one
            await mark_read(conversation_id)
            outcome: dict[str, Any] = {}

            async def before_send(d: Draft, plan=plan, last=last, outcome=outcome, warm=warm) -> bool:
                typing_done = max(_aware(plan.send_after), utcnow())
                await _sleep_until(typing_done + timedelta(seconds=humanize.typing_seconds(d.text, warm=warm)))
                await sync_messages(conversation_id)
                newest = _last_incoming_msg(conversation_id)
                if newest is not None and newest.ext_id != last.ext_id:
                    outcome["redo"] = True       # buyer wrote again while we were typing
                    return False
                msgs = messages_of(conversation_id)
                last_any = next((m for m in reversed(msgs) if m.direction in ("in", "out")), None)
                if last_any is None or last_any.direction != "in":
                    return False               # answered by hand / on willhaben meanwhile
                with session() as db:
                    others = db.exec(select(Draft).where(Draft.conversation_id == conversation_id,
                                                         Draft.id != d.id, Draft.status != "discarded")).all()
                since = _aware(newest.created_at) if newest and newest.created_at else None
                # another draft or answer (push button, Telegram, dashboard) since the buyer's message: stay out
                return not (since and any(_aware(o.created_at) >= since for o in others))

            d = await auto_reply(conversation_id, before_send=before_send)
            if outcome.get("redo"):
                continue
            return d
        return None
    except Exception as e:  # noqa: BLE001 - a background answer must never take the app down
        await _auto_failed(conversation_id, e)
        return None
    finally:
        _humanized.pop(conversation_id, None)


async def auto_reply(conversation_id: str, before_send=None) -> Draft | None:
    """Draft and (if nothing blocks it) send an automatic answer.

    `before_send(draft)` runs right before sending (human-like typing delay + re-check); False drops the draft.
    """
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None or _auto_mode(conv) is None:
        return None
    d = await draft_reply(conversation_id, auto=True)
    _auto_recovered(conversation_id)
    blockers = _auto_blockers(conversation_id, d, messages_of(conversation_id))
    title = conv.ad_title or ""
    sent: Draft | None = None
    if not blockers and before_send is not None:
        _set_status(d.id, "pending")
        go = await before_send(d)
        _set_status(d.id, "draft")
        if not go:
            discard_draft(d.id)
            return None
        async with _send_lock:   # limits and checks count at the moment of sending, one send at a time
            if not _still_auto(conversation_id):   # auto answers turned off / ad sold while "typing"
                discard_draft(d.id)
                return None
            blockers = _auto_blockers(conversation_id, d, messages_of(conversation_id))
            if not blockers:
                sent = await _send_auto(conv, d, conversation_id, title)
    elif not blockers:
        if not _still_auto(conversation_id):   # changed while Claude was drafting
            discard_draft(d.id)
            return None
        sent = await _send_auto(conv, d, conversation_id, title)
    if sent is not None:
        if sent.status == "sent":
            return sent
        blockers = [sent.error or t("Sending failed")]   # the draft keeps its text: offered below with the reason
    with session() as db:
        row = db.get(Draft, d.id)
        row.auto_reason = f"{row.auto_reason or ''} · {t('not sent: {reasons}', reasons='; '.join(blockers))}".strip(" ·")
        db.add(row)
        db.commit()
        db.refresh(row)
    await _purchase_after(conversation_id, row, sent=False)
    await offer_draft(conv, row, t("not sent automatically: {reason}", reason=blockers[0]))
    return row


_send_lock = asyncio.Lock()


def _still_auto(conversation_id: str) -> bool:
    """Re-check right before an automatic send: the owner may have switched auto answers off or sold the ad."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    return conv is not None and _auto_mode(conv) is not None


def _set_status(draft_id: int, status: str) -> None:
    with session() as db:
        row = db.get(Draft, draft_id)
        if row is not None and row.status in ("draft", "pending"):
            row.status = status
            db.add(row)
            db.commit()


async def _send_auto(conv: Conversation, d: Draft, conversation_id: str, title: str) -> Draft:
    """Send an automatic answer and push "answered"; on failure the draft keeps the error (status draft). A used-up
    hourly willhaben budget fails it at once instead of waiting: the checks before the send (still automatic, the
    buyer's newest message, the floor) ran just now, an answer an hour later could go out after the owner answered
    themselves or switched the automatic answers off."""
    from app.platforms.ratelimit import Blocked

    try:
        d = await send_draft(d.id, d.text)
    except (EndpointUnknown, SendFailed, NotLoggedIn, SessionUnavailable, Blocked) as e:
        with session() as db:
            row = db.get(Draft, d.id)
            row.error = t("Sending failed: {error}", error=e)
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
    await _purchase_after(conversation_id, d, sent=True)
    q = _last_incoming(conversation_id)
    await notify("auto_reply", t("Answered automatically: {name}", name=conv.partner_name or "").strip(),
                 (t("“{text}” → ", text=q[:120]) if q else "") + f"{d.text[:160]}\n{title}",
                 url="https://www.willhaben.at/iad/myprofile/chat",
                 data={"conversation_id": conversation_id, "ad_id": conv.ad_id, "draft_id": d.id},
                 tag=f"wh-draft-{d.id}")     # = ha_actions.tag_for (not imported here: circular)
    return d


def message_tag(conversation_id: str) -> str:
    """Tag of the "new message" push for a chat (a later draft offer takes the same Telegram message over)."""
    return f"wh-msg-{conversation_id}"


def _last_incoming(conversation_id: str) -> str | None:
    msgs = [m for m in messages_of(conversation_id) if m.direction == "in"]
    return msgs[-1].text if msgs else None


async def offer_draft(conv: Conversation, d: Draft, reason: str, replaces: str | None = None) -> None:
    """Push the draft to the phone with buttons: send, direct answer, let Claude revise, discard.

    `replaces`: tag of the push this offer supersedes (the chat message, or the draft it revises). In Telegram
    that message is edited in place; the Home Assistant push with the old tag is cleared.
    """
    from app.config import get_settings
    from app.services import ha_actions
    from app.services.notify import clear_push

    if replaces and replaces != ha_actions.tag_for(d.id):
        telegram.retag(replaces, ha_actions.tag_for(d.id))
        await clear_push(replaces)
    can_send = not ("[" in d.text and "]" in d.text) and d.intent != "scam_suspect" and bool(d.text.strip())
    q = _last_incoming(conv.id)
    await notify("auto_reply_pending", t("Approve answer? {name}", name=conv.partner_name or "").strip(),
                 (t("Question: “{text}”", text=q[:120]) + "\n" if q else "")
                 + t("Suggestion: “{text}”", text=d.text[:300]) + f"\n{conv.ad_title or ''} · {reason}",
                 url=f"{get_settings().public_url.rstrip('/')}/chats/{conv.id}",
                 data={"conversation_id": conv.id, "ad_id": conv.ad_id, "draft_id": d.id},
                 actions=ha_actions.draft_actions(d.id, can_send), tag=ha_actions.tag_for(d.id))


async def revise(draft_id: int, instruction: str, *, chosen: bool = False) -> Draft | None:
    """New draft from an instruction about the old one; the old draft is dropped. None when it is not an open draft
    any more. `chosen`: the instruction is one of Claude's own alternatives the owner tapped (Telegram button, button
    on the chat page), so it names the decision only: the wording follows the rules, and nothing the alternative or
    the old draft added unasked (a question the partner already answered, "keine Garantie") survives. An
    instruction the owner typed ("Mit Claude überarbeiten") stays binding as it stands."""
    with session() as db:
        old = db.get(Draft, draft_id)
        conv = db.get(Conversation, old.conversation_id) if old else None
    if old is None or conv is None or old.status != "draft":
        return None
    # German on purpose: both texts go into the prompt
    instruction = instruction.strip() or "Formuliere die Antwort neu."
    if chosen:
        hint = (f"Meine Entscheidung für diese Antwort (einer deiner eigenen Vorschläge, den ich angetippt habe): "
                f"{instruction}\nMein bisheriger Entwurf war: „{old.text}“ – nur zum Vergleich. Setz die Entscheidung um "
                "und formuliere sie nach den Regeln oben; übernimm aus dem Vorschlag und dem alten Entwurf nichts, was "
                "der Partner nicht verlangt hat (keine Rückfrage zu Gesagtem, kein Hinweis auf Garantie, Rücknahme oder "
                "Privatverkauf).")
    else:
        hint = f"Mein bisheriger Entwurf war: „{old.text}“. Überarbeite ihn so: {instruction}"
    new = await draft_reply(conv.id, hint=hint, auto=bool(old.auto))
    with session() as db:                  # Claude took its time: the old draft may have been sent meanwhile
        still_open = (db.get(Draft, draft_id) or Draft(conversation_id="", text="", status="gone")).status == "draft"
    if not still_open:
        discard_draft(new.id)              # answered already: no second answer on offer
        return None
    discard_draft(draft_id)
    with session() as db:
        row = db.get(Draft, new.id)
        row.auto_reason = t("revised as asked: “{instruction}”", instruction=instruction[:80])
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


async def revise_and_offer(draft_id: int, instruction: str, *, chosen: bool = False) -> Draft | None:
    """"Revise with Claude" (typed) or an alternative button (chosen) from the push: new draft from the instruction,
    old one dropped, same push updated."""
    from app.services import ha_actions

    row = await revise(draft_id, instruction, chosen=chosen)
    if row is None:
        return None
    with session() as db:
        conv = db.get(Conversation, row.conversation_id)
    await offer_draft(conv, row, t("revised: {instruction}", instruction=instruction.strip()[:80]),
                      replaces=ha_actions.tag_for(draft_id))
    return row


def enqueue_revise(draft_id: int, instruction: str) -> bool:
    """One of Claude's alternatives tapped on the chat page: revise in the background (lane claude), the composer
    shows "Claude is writing…" and the new draft appears by itself. False: no open draft / already drafting."""
    with session() as db:
        d = db.get(Draft, draft_id)
        c = db.get(Conversation, d.conversation_id) if d else None
    if d is None or c is None or d.status != "draft" or c.drafting:
        return False
    conversation_id = c.id
    _set_drafting(conversation_id, True)

    async def run(job: jobs.Job) -> str:
        try:
            await revise(draft_id, instruction, chosen=True)
        except Exception as e:               # the reason shows in the chat, next to the draft
            _set_drafting(conversation_id, False, t("Draft failed: {error}", error=e))
            raise
        except BaseException:
            _set_drafting(conversation_id, False, t("Draft interrupted"))
            raise
        _set_drafting(conversation_id, False)
        return t("Draft ready")

    jobs.submit("chat-draft", t("Claude revises the answer to {name}", name=c.partner_name or t("unknown")), run,
                lane="claude", current=f"drafting:{conversation_id}", on_cancel=lambda: _set_drafting(conversation_id, False), quiet=True)
    return True


def mark_sent(draft_id: int, text: str | None = None) -> Draft:
    """Mark a draft as sent (also used when the user sent it manually on willhaben)."""
    with session() as db:
        draft = db.get(Draft, draft_id)
        if text:
            draft.text = text.strip()
        draft.status, draft.sent_at, draft.error = "sent", utcnow(), None
        db.add(draft)
        ext = f"draft-{draft.id}"
        _upsert_message(db, draft.conversation_id, ext, "out", draft.text, draft.sent_at)
        conv = db.get(Conversation, draft.conversation_id)
        if conv:
            conv.unread_count = 0
            db.add(conv)
        db.commit()
        db.refresh(draft)
    if draft.meetup:                           # the answer fixed an appointment: the chat page offers it
        meetups.remember(draft.conversation_id, draft.meetup)
    return draft


def discard_draft(draft_id: int, by_owner: bool = False) -> None:
    """A draft goes. `by_owner`: the owner discarded it (chat page, push or Telegram button), so the buyer's message
    counts as handled and no automatic answer is written for it again; the app's own discards (a new draft replaces
    it, the buyer wrote again while "typing") leave the message waiting."""
    with session() as db:
        draft = db.get(Draft, draft_id)
        if draft:
            draft.status = "discarded"
            if by_owner:
                draft.dismissed = True
            db.add(draft)
            db.commit()


def hide(conversation_id: str, on: bool = True) -> None:
    """Hide a chat from the list (willhaben is not touched); it comes back when the other person writes."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv:
            conv.hidden_at, conv.removed = (utcnow(), conv.removed) if on else (None, None)
            db.add(conv)
            db.commit()


async def delete(ids: list[str]) -> tuple[list[str], list[str]]:
    """Delete chats on their site and then here (remove). willhaben deletes them for good (one request for all);
    a chat of a site without a known delete (Vinted) is not touched there and comes back as `kept` – it is not
    removed here either, so nothing looks deleted that is not. Returns (deleted, kept). Raises when willhaben
    refuses: then nothing is removed."""
    from app.platforms import vinted_inbox
    wh = [i for i in ids if not vinted_inbox.is_vinted(i)]
    kept = [i for i in ids if vinted_inbox.is_vinted(i)]
    if wh:
        await _account().delete_conversations(wh)
        log.info("deleted %d chat(s) on willhaben", len(wh))
    for cid in wh:
        remove(cid)
    return wh, kept


def remove(conversation_id: str) -> None:
    """Delete a chat's messages and drafts here (not on willhaben) and skip it in future syncs, until the
    other person writes again. Shipments and purchases stay: they are records of labels and money."""
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            return
        for model in (Message, Draft):
            for r in db.exec(select(model).where(model.conversation_id == conversation_id)):
                db.delete(r)
        conv.hidden_at, conv.removed, conv.unread_count = utcnow(), True, 0
        db.add(conv)
        db.commit()
