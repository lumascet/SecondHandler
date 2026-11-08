"""Kleinanzeigen chats in the app's chat list, through the web inbox's API (kleinanzeigen_messaging.Box).

The inbox (at most every SYNC_EVERY_S unless forced) into Conversation rows (platform "kleinanzeigen", the site's own
conversation id), the conversations with something new (MAX_DETAILS per sync) into Message rows, a new message of the
other person pushed like willhaben's. A reply goes out through the same API; the message it made is stored under its
Kleinanzeigen id at once, so the next sync finds it and stores no second copy.

The answers (seen 2026-11-04): a conversation {id, role "Buyer" | "Seller" (mine), buyerName, sellerName, userIdBuyer,
userIdSeller, adId, adTitle, adImage, adPriceInEuroCent, adStatus, unread, unreadMessagesCount, receivedDate, …, and
in the single read messages [{messageId, type MESSAGE | PAYMENT_AND_SHIPPING_MESSAGE …, boundness INBOUND | OUTBOUND,
receivedDate, textShort, title, attachments, unichatMessage {payload {type chat/text-message, content {text}}}}]}.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.platforms.text import parse_iso
from app.db import utcnow

log = logging.getLogger(__name__)

PLATFORM = "kleinanzeigen"
SYNC_EVERY_S = 600            # the full inbox at most every 10 minutes unless something changed (watch())
WATCH_S = 20                  # the inbox's state (unread count, last change) asked this often, from one open tab
                              # (the page itself asks every 2 s while it is used)
WATCH_BACKOFF_S = 5 * 60      # ... after a failure or while logged out
QUIET_LOOK_S = 5 * 60         # in the quiet hours (Settings → Quiet hours) no checks: the time is looked at this often
MAX_DETAILS = 5               # conversations read per sync (those with something new)
_last_sync = [0.0]
# the chat list's mark (online / offline): no stream on Kleinanzeigen, so the inbox read decides – its last result
_state: dict[str, Any] = {"state": "off", "since": None, "last_ok": None}
STALE_S = 5 * WATCH_S + 120          # no check that worked for this long (the watch stopped, they fail): offline


def _note(state: str) -> None:
    now = utcnow()
    if state == "connected":
        if _state["state"] != "connected":
            _state["since"] = now
        _state["last_ok"] = now
    elif _state["state"] != state:
        _state["since"] = now
    _state["state"] = state


def live() -> dict[str, Any]:
    """{state: connected | quiet | logged out | error | off | stale, since, last_ok} for the chat list's mark."""
    st = dict(_state)
    if st["state"] == "quiet":
        return st
    if st["state"] == "connected" and st["last_ok"] and (utcnow() - st["last_ok"]).total_seconds() > STALE_S:
        st["state"] = "stale"
    return st


def _when(raw: Any):
    return parse_iso(raw) if isinstance(raw, str) and raw else None


def message_text(m: dict[str, Any]) -> tuple[str, str | None]:
    """(text, kind): a chat message's own text (its content, else the shortened one), None as kind; a payment or
    shipping notice of Kleinanzeigen's as kind "integration"."""
    kind = None if m.get("type") == "MESSAGE" else "integration"
    content = ((m.get("unichatMessage") or {}).get("payload") or {}).get("content")
    text = content.get("text") if isinstance(content, dict) else content if isinstance(content, str) else None
    text = (text or m.get("textShort") or m.get("title") or "").strip()
    if kind and m.get("title") and m.get("title") not in text:
        text = f"{m['title']}: {text}" if text else m["title"]
    return text, kind


def message_key(m: dict[str, Any]) -> str:
    return f"ka-{m.get('messageId')}"


def _mine(c: dict[str, Any]) -> str | None:
    role = (c.get("role") or "").lower()
    return role if role in ("buyer", "seller") else None


def _partner(c: dict[str, Any]) -> tuple[str | None, str | None]:
    """(name, user id) of the other person: the seller when I buy, the buyer when I sell."""
    if _mine(c) == "buyer":
        return c.get("sellerName"), str(c.get("userIdSeller") or "") or None
    return c.get("buyerName"), str(c.get("userIdBuyer") or "") or None


def _ad_context(row: Any, c: dict[str, Any]) -> dict[str, Any]:
    from app import platforms
    ctx = {**(row.ad_context or {}), "title": c.get("adTitle") or row.ad_title, "platform": PLATFORM}
    if row.ad_id:
        ctx["url"] = platforms.get(PLATFORM).ad_url(row.ad_id)
    if c.get("adImage") and not ctx.get("images"):
        ctx["images"] = [c["adImage"]]
    if isinstance(c.get("adPriceInEuroCent"), (int, float)):
        ctx.setdefault("price", c["adPriceInEuroCent"] / 100)
    if c.get("adStatus"):
        ctx["ad_status"] = c["adStatus"]
    _name, uid = _partner(c)
    if uid:
        ctx["partner_id"] = uid
    return ctx


def apply_inbox(data: dict[str, Any], incoming: list[str] | None = None) -> list[str]:
    """The inbox into Conversation rows. Returns the ids with a message newer than stored (to be read); `incoming`
    gets those whose newest message is the other person's and unread (for the push)."""
    from sqlmodel import select

    from app.db import Conversation, Message, session

    changed: list[str] = []
    with session() as db:
        for c in data.get("conversations") or []:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            row = db.get(Conversation, cid)
            known = row is not None
            row = row or Conversation(id=cid, platform=PLATFORM)
            at = _when(c.get("receivedDate"))
            newer = at is not None and (row.last_message_at is None or at > row.last_message_at)
            if (row.removed or row.hidden_at) and newer and c.get("boundness") == "INBOUND":
                row.removed, row.hidden_at = False, None        # they wrote again: back in the list, as on willhaben
            elif row.removed:
                continue
            name, _uid = _partner(c)
            row.platform = PLATFORM
            row.role = _mine(c) or row.role
            row.ad_id = str(c.get("adId")) if c.get("adId") else row.ad_id
            row.ad_title = c.get("adTitle") or row.ad_title
            row.partner_name = name or row.partner_name
            row.unread_count = int(c.get("unreadMessagesCount") or (1 if c.get("unread") else 0))
            row.last_message_text = (c.get("textShortTrimmed") or row.last_message_text or "").strip() or None
            row.last_message_at = at or row.last_message_at
            row.ad_context = _ad_context(row, c)
            row.synced_at = utcnow()
            db.add(row)
            empty = known and db.exec(select(Message.id).where(Message.conversation_id == cid).limit(1)).first() is None
            # read before but not to its newest message (more changed chats than MAX_DETAILS, a failed read): again
            behind = bool(row.messages_synced_at and row.last_message_at and row.messages_synced_at < row.last_message_at)
            if newer or row.messages_synced_at is None or empty or behind:
                changed.append(cid)
                # a brand-new chat too (a buyer's first message): the very first sync of all stays quiet (sync())
                if incoming is not None and newer and c.get("boundness") == "INBOUND" and c.get("unread"):
                    incoming.append(cid)
        db.commit()
    return changed


def apply_conversation(cid: str, data: dict[str, Any]) -> int:
    """One conversation's messages into Message rows; how many it holds."""
    from app.db import Conversation, session
    from app.services import chats

    msgs = data.get("messages") or []
    with session() as db:
        for m in msgs:
            text, kind = message_text(m)
            if not text or not m.get("messageId"):
                continue
            bound = m.get("boundness")
            direction = "in" if bound == "INBOUND" else "out" if bound == "OUTBOUND" else "unknown"
            chats._upsert_message(db, cid, message_key(m), direction, text, _when(m.get("receivedDate")),
                                  authoritative=True, kind=kind)
        row = db.get(Conversation, cid)
        if row is not None:
            row.messages_synced_at = utcnow()
            row.role = _mine(data) or row.role
            row.ad_context = _ad_context(row, data)
            db.add(row)
        db.commit()
    return len(msgs)


def is_kleinanzeigen(conversation_id: str) -> bool:
    from app.db import Conversation, session

    with session() as db:
        c = db.get(Conversation, conversation_id)
    return bool(c and c.platform == PLATFORM)


def _known_any() -> bool:
    from sqlmodel import select

    from app.db import Conversation, session
    with session() as db:
        return db.exec(select(Conversation).where(Conversation.platform == PLATFORM).limit(1)).first() is not None


def _usable() -> str | None:
    """Why the sync cannot run now (None: it can)."""
    from app import platforms
    from app.services import site_accounts
    from app.willhaben.session import get_session

    if not platforms.enabled(PLATFORM):
        return "switched off"
    if not get_session().started:
        return "browser not running"
    if site_accounts.state(PLATFORM)["logged_in"] is False:
        return "not logged in"
    return None


def _logged_out(e: Exception) -> None:
    from app.services import site_accounts
    log.info("kleinanzeigen chats: %s", e)
    site_accounts._save(PLATFORM, False)


async def sync(force: bool = False) -> dict[str, Any]:
    """The inbox and its changed conversations (at most every SYNC_EVERY_S unless forced)."""
    from app.platforms import kleinanzeigen_messaging as km
    from app.services import chats

    if (why := _usable()) is not None:
        if why == "not logged in":
            _note("logged out")
        return {"skipped": why}
    if not force and time.monotonic() - _last_sync[0] < SYNC_EVERY_S:
        return {"skipped": "recently"}
    _last_sync[0] = time.monotonic()
    first = not _known_any()
    incoming: list[str] = []
    read = 0
    try:
        async with km.Box() as box:
            inbox = await box.inbox()
            changed = apply_inbox(inbox, incoming)
            for cid in changed[:MAX_DETAILS]:
                try:
                    read += apply_conversation(cid, await box.conversation(cid))
                except km.MessagingError as e:
                    log.info("kleinanzeigen conversation %s not read: %s", cid, e)
    except km.LoggedOut as e:
        _logged_out(e)
        _note("logged out")
        return {"skipped": "not logged in"}
    except km.MessagingError as e:
        log.warning("kleinanzeigen chats: %s", e)
        _note("error")
        return {"error": str(e)}
    except Exception:
        _note("error")                    # no browser tab, a closed page: offline, the next read tries again
        raise
    _note("connected")
    for cid in changed[:MAX_DETAILS]:
        await chats.ensure_ad_context(cid)
    n = len(inbox.get("conversations") or [])
    log.info("kleinanzeigen chats: %d conversations, %d read, %d messages", n, min(len(changed), MAX_DETAILS), read)
    if not first:                    # the first sync of all: what was there is no news
        await _push_incoming(incoming)
    return {"conversations": n, "opened": min(len(changed), MAX_DETAILS)}


async def sync_one(cid: str) -> int:
    """One conversation again (the chat's Reload)."""
    from app.platforms import kleinanzeigen_messaging as km
    from app.services import chats

    if _usable() is not None:
        return 0
    try:
        async with km.Box() as box:
            n = apply_conversation(cid, await box.conversation(cid))
    except km.LoggedOut as e:
        _logged_out(e)
        return 0
    except km.MessagingError as e:
        log.info("kleinanzeigen conversation %s not read: %s", cid, e)
        return 0
    await chats.ensure_ad_context(cid)
    return n


class SendError(RuntimeError):
    pass


async def send(cid: str, text: str) -> dict[str, Any] | None:
    """Send `text` into the conversation through the inbox's API, then read it again: returns my new message as
    Kleinanzeigen stores it (for record_sent), None when the read after it did not show it. SendError when the reply
    was refused; nothing is retried."""
    from app.i18n import t
    from app.platforms import kleinanzeigen_messaging as km

    text = (text or "").strip()
    if not text:
        raise SendError("empty")
    try:
        async with km.Box() as box:
            status, _data = await box.reply(cid, text)
            if status not in (200, 201, 204):
                raise SendError(t("Kleinanzeigen refused: HTTP {status}", status=status))
            try:
                after = await box.conversation(cid)
            except km.MessagingError:
                return None
    except km.LoggedOut as e:
        _logged_out(e)
        raise SendError(str(e)) from None
    except km.MessagingError as e:
        raise SendError(str(e)) from e
    apply_conversation(cid, after)
    mine = [m for m in after.get("messages") or [] if m.get("boundness") == "OUTBOUND"
            and message_text(m)[0] == text]
    return mine[-1] if mine else None


def record_sent(draft_id: int, reply: dict[str, Any] | None) -> None:
    """The message mark_sent stored as draft-<id>: dropped when the read after sending stored Kleinanzeigen's own copy,
    else it keeps waiting for the next sync."""
    from sqlmodel import select

    from app.db import Message, session
    if not reply:
        return
    with session() as db:
        m = db.exec(select(Message).where(Message.ext_id == f"draft-{draft_id}")).first()
        if m is None:
            return
        own = db.exec(select(Message).where(Message.conversation_id == m.conversation_id,
                                            Message.ext_id == message_key(reply))).first()
        if own is not None:
            db.delete(m)                 # stored already under its own id by the read after sending
        else:
            m.ext_id = message_key(reply)
            m.created_at = _when(reply.get("receivedDate")) or m.created_at
            db.add(m)
        db.commit()


_pushed: dict[str, Any] = {}


async def _push_incoming(cids: list[str]) -> None:
    """A message the other person wrote on Kleinanzeigen: the same push as willhaben's (Settings → Chats), once."""
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
        at = row.last_message_at
        if at is not None and _pushed.get(cid) is not None and at <= _pushed[cid]:
            continue
        _pushed[cid] = at
        try:
            await notify("message", t("Kleinanzeigen: message from {name}", name=row.partner_name or t("unknown")),
                         f"{row.ad_title or ''}: {(row.last_message_text or '')[:140]}",
                         url=f"{get_settings().public_url.rstrip('/')}/chats/{cid}",
                         data={"conversation_id": cid, "platform": PLATFORM}, tag=f"wh-msg-{cid}")
        except Exception as e:  # noqa: BLE001 - a failed push never stops the sync
            log.warning("kleinanzeigen message push failed: %s", e)


async def watch() -> None:
    """As near to live as the web gets (it has no push: the inbox page polls too): every WATCH_S the inbox's state (unread count, last change) from one tab that
    stays open – the page's own poll, one small call –, the inbox and its changed conversations read at once when it
    changed. Logged out or failing: every WATCH_BACKOFF_S. Runs as long as the app (main.lifespan)."""
    import asyncio

    from app.platforms import kleinanzeigen_messaging as km

    box: km.Box | None = None
    last: tuple[Any, Any] | None = None
    try:
        while True:
            wait = WATCH_S
            try:
                from app.services import quiet_hours
                q = quiet_hours.state()
                if q is not None and q.active:          # the quiet hours: no checks at all, the tab closed
                    if box is not None:
                        await box.close()
                        box = None
                    _note("quiet")
                    last = None                         # afterwards: a full read of what came meanwhile
                    _state["after_quiet"] = True
                    left = (q.end - utcnow().astimezone(q.end.tzinfo)).total_seconds()
                    await asyncio.sleep(max(5.0, min(QUIET_LOOK_S, left + 5)))
                    continue
                if _usable() is not None:
                    if box is not None:
                        await box.close()
                        box = None
                    wait = WATCH_BACKOFF_S
                else:
                    if box is None or not box.open():
                        box = await km.Box().__aenter__()
                    m = await box.meta()
                    now = (m.get("lastModified"), m.get("numUnread"))
                    _note("connected")
                    if last is not None and now != last:
                        await sync(force=True)          # something came: the list and the changed chats now
                    elif last is None:
                        await sync(force=_state.get("after_quiet", False))   # at the start: as due; after the
                        _state["after_quiet"] = False                         # quiet hours: everything that came
                    last = now
            except km.LoggedOut as e:
                _logged_out(e)
                _note("logged out")
                wait = WATCH_BACKOFF_S
            except Exception as e:  # noqa: BLE001 - a closed tab, a refused call: a new tab next time
                log.info("kleinanzeigen watch: %s", e)
                _note("error")
                if box is not None:
                    await box.close()
                box, wait = None, WATCH_BACKOFF_S
            await asyncio.sleep(wait)
    finally:
        if box is not None:
            await box.close()
