"""Telegram bot: a second push channel next to Home Assistant, with buttons.

Long polling (getUpdates), so the server needs no public URL. The bot answers exactly one chat. Linking:
while no chat is linked, every chat that writes to the bot (any device, any Telegram client) is remembered as a
candidate for a few minutes and Settings shows a "link with <name>" button; the logged-in user confirms there.
Updates from any other chat are ignored.

Buttons carry a short random token (`t:<token>`, table TelegramAction) instead of the action itself:
  D_SEND / D_REPLY / D_REVISE / D_DISCARD  held-back answer -> ha_actions.handle (the one send path)
  D_ALT                                     revise along one of Claude's alternative decisions, one tap, no typing
  D_CAL / D_UNCAL                           agreed appointment -> Google Calendar / out again (ha_actions.handle)
  D_SHIP                                    address received -> fill the carrier's label form (ha_actions.handle)
  D_PAID / D_BUY / D_DECL / D_CANCEL        payment and approval of a parcel (ha_actions.handle)
  C_DRAFT / C_REPLY                         new chat message -> Claude draft / answer typed in Telegram
  F_UP / F_DOWN                             feedback on an automatic answer (👎 asks for a note)
  A_ON / A_OFF                              automatic answers for one own ad
  H_SAVE / H_IGNORE                         search hit: keep / ignore
  P_BUY                                     search hit: start a purchase order (when purchases are on in Settings)
  AB_STOP                                   auto-buy's countdown: cancel the purchase before it runs
Buttons that need text ("Direkte Antwort", "Mit Claude überarbeiten", 👎 note) send a ForceReply prompt that
quotes the notification; the user's reply to it is the text. Replying to a notification directly (swipe) works
without any button: the text goes to that chat (and sends the held-back draft when the notification shows one).

One notification, one message: every tagged message keeps its text (TelegramMessage), so progress ("Claude is
writing …") and results ("Sent") are edited into the same message instead of piling up new ones.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Any

import httpx
from sqlmodel import col, delete, select

from app.config import get_settings
from app.db import (
    Conversation, Draft, Hit, MyAd, TelegramAction, TelegramMessage, current_ad, get_setting, session, set_setting, utcnow,
)
from app.i18n import t

log = logging.getLogger(__name__)


class _HideBotToken(logging.Filter):
    """The Bot API has the token in the URL and httpx logs every request URL at INFO: drop those lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "api.telegram.org/bot" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideBotToken())

API = "https://api.telegram.org"
_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport
_tasks: set[asyncio.Task] = set()
_pending: dict[int, tuple[str, str, float, str | None]] = {}   # prompt message id -> (verb, ref, time, origin tag)
PENDING_TTL = 30 * 60
# chat id -> (display name, time, pairing code) while no chat is linked. The bot sends the code to that chat and
# Settings shows it next to the button: anyone can find the bot and pick the owner's name, the code tells them apart.
_candidates: dict[int, tuple[str, float, str]] = {}
CANDIDATE_TTL = 10 * 60
MAX_CANDIDATES = 20
# Last Bot API outcome that says something about the connection, read by app/services/health.py.
api_health: dict[str, Any] = {"ok_at": None, "error_at": None, "error": None}


def kinds() -> dict[str, str]:
    """Notification kinds that can go to Telegram (event kind -> label)."""
    return {
        "message": t("New chat messages"),
        "auto_reply_pending": t("Answers waiting for approval"),
        "auto_reply": t("Automatic answers sent"),
        "hit": t("New search hits"),
        "price_drop": t("Price drops"),
        "saved": t("Saved ads: price drops, sold or reserved"),
        "ad_expiring": t("Own ads about to expire"),
        "ad_expired": t("Own ads expired"),
        "ad_renewed": t("Own ads renewed automatically"),
        "meetup": t("Appointments for the calendar"),
        "shipping": t("Shipping addresses and labels"),
        "paylivery": t("PayLivery steps (offers, payment, delivery)"),
        "purchase": t("Purchases (negotiation, pay now)"),
    }


class TelegramError(RuntimeError):
    pass


# ------------------------------------------------------------------ config
def _token_file():
    return get_settings().data_dir / "telegram-token"


def token() -> str:
    try:
        tok = _token_file().read_text().strip()
    except OSError:
        tok = ""
    return tok or get_settings().telegram_bot_token


def token_source() -> str | None:
    if _token_file().exists():
        return "login"
    return "env" if get_settings().telegram_bot_token else None


def chat_id() -> int | None:
    v = get_setting("telegram_chat_id")
    return int(v) if v.lstrip("-").isdigit() else None


def enabled_kinds() -> set[str]:
    raw = get_setting("telegram_kinds")
    return set(kinds()) if not raw else {k for k in raw.split(",") if k in kinds()}   # "-" = none chosen


def set_kinds(selected: list[str]) -> None:
    set_setting("telegram_kinds", ",".join(k for k in selected if k in kinds()) or "-")


def active() -> bool:
    return bool(token() and chat_id() and get_setting("telegram_enabled") != "0")


def bot_url() -> str | None:
    user = get_setting("telegram_username")
    return f"https://t.me/{user}" if user else None


def bot_qr_svg() -> str:
    """The bot link as an inline SVG QR code: scan it with the phone when Settings is open on a desktop."""
    import segno

    url = bot_url()
    return segno.make(url, error="m").svg_inline(scale=4, border=1, dark="#000") if url else ""


def candidates() -> list[dict[str, Any]]:
    """Chats that wrote to the bot while nothing was linked, newest first (expired ones dropped)."""
    now = time.monotonic()
    for k in [k for k, v in _candidates.items() if now - v[1] > CANDIDATE_TTL]:
        _candidates.pop(k, None)
    return [{"chat_id": k, "name": v[0], "age_s": int(now - v[1]), "code": v[2]}
            for k, v in sorted(_candidates.items(), key=lambda kv: -kv[1][1])]


def status() -> dict[str, Any]:
    return {"configured": bool(token()), "source": token_source(), "username": get_setting("telegram_username"),
            "linked": chat_id() is not None, "chat_name": get_setting("telegram_chat_name"),
            "enabled": get_setting("telegram_enabled") != "0", "kinds": enabled_kinds(), "bot_url": bot_url(),
            "candidates": candidates() if chat_id() is None else []}


COMMANDS = [("drafts", "Open answer drafts"), ("auto", "Automatic answers per ad"),
            ("status", "Session and counters"), ("help", "What this bot can do")]


async def save_token(tok: str) -> str:
    """Check a BotFather token, store it (600) and forget any linked chat. Returns the bot's @username."""
    tok = tok.strip()
    me = await api("getMe", _token=tok)
    p = _token_file()
    p.write_text(tok)
    p.chmod(0o600)
    set_setting("telegram_username", me.get("username") or "")
    unlink()
    try:
        await api("deleteWebhook")          # getUpdates does not work while a webhook is set
        await api("setMyCommands", commands=[{"command": c, "description": t(d)} for c, d in COMMANDS])
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram setup calls failed: %s", e)
    return me.get("username") or ""


async def menu_button() -> bool:
    """The bot's menu button opens the Mini App (app/tg_web.py) for the linked chat; only with an https public URL,
    which is all Telegram opens. False when there is none or Telegram refused."""
    from app.tg_web import menu_url
    url, cid = menu_url(), chat_id()
    if not url or not cid or cid <= 0:
        return False
    try:
        await api("setChatMenuButton", chat_id=cid,
                  menu_button={"type": "web_app", "text": "SecondHandler", "web_app": {"url": url}})
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram menu button not set: %s", e)
        return False
    return True


def unlink() -> None:
    set_setting("telegram_chat_id", "")
    set_setting("telegram_chat_name", "")
    _candidates.clear()


async def link(cid: int) -> str | None:
    """Confirm one candidate chat (the button in Settings). Returns its name, or None if it is not a candidate."""
    found = {c["chat_id"]: c["name"] for c in candidates()}.get(cid)
    if found is None:
        return None
    set_setting("telegram_chat_id", str(cid))
    set_setting("telegram_chat_name", found)
    _candidates.clear()
    await menu_button()
    await _say("✅ " + t("Linked. Notifications from SecondHandler now arrive here.") + "\n\n" + _help())
    return found


def forget() -> None:
    _token_file().unlink(missing_ok=True)
    _candidates.clear()
    for k in ("telegram_chat_id", "telegram_chat_name", "telegram_username", "telegram_link_code"):   # last: pre-0.2 leftover
        set_setting(k, "")


# ------------------------------------------------------------------ Bot API
async def api(method: str, _token: str | None = None, _timeout: float = 20, **params: Any) -> Any:
    tok = _token or token()
    if not tok:
        raise TelegramError("no bot token")
    body = {k: v for k, v in params.items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=_timeout, transport=_transport) as c:
            r = await c.post(f"{API}/bot{tok}/{method}", json=body)
    except httpx.HTTPError as e:
        api_health.update(error_at=utcnow(), error=type(e).__name__)   # never str(e): it may hold the URL
        raise
    try:
        data = r.json()
    except ValueError as e:
        api_health.update(error_at=utcnow(), error=f"HTTP {r.status_code}")
        raise TelegramError(f"{method}: HTTP {r.status_code}") from e
    if not data.get("ok"):
        # token revoked / bot deleted / second poller / Telegram down; a 400 (e.g. "not modified") says nothing
        if r.status_code in (401, 403, 404, 409) or r.status_code >= 500:
            api_health.update(error_at=utcnow(), error=f"{r.status_code} {data.get('description') or ''}"[:200])
        raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
    api_health["ok_at"] = utcnow()
    return data.get("result")


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _button(text: str, verb: str, ref: str) -> dict[str, str]:
    tok = secrets.token_urlsafe(9)
    with session() as db:
        db.add(TelegramAction(token=tok, verb=verb, ref=str(ref)))
        db.commit()
    return {"text": text, "callback_data": f"t:{tok}"}


def open_button(text: str, url: str) -> dict[str, Any]:
    """A button that opens `url`. A page of the dashboard opens as a Mini App: in Telegram, full height, without a
    browser, logged in with Telegram's signed launch data (/login/telegram). Other links (willhaben, eBay) and any
    link in a group chat (Mini App buttons work in private chats only) stay ordinary links."""
    base = get_settings().public_url.rstrip("/")
    mine = base.startswith("https://") and (url == base or url.startswith(base + "/"))
    if mine and (chat_id() or 0) > 0:                 # in Telegram: the Mini App's page (app/tg_web.py)
        from app.tg_web import tg_link
        return {"text": text, "web_app": {"url": tg_link(url)}}
    return {"text": text, "url": url}


def _rows(buttons: list[dict[str, str]], per_row: int = 2) -> list[list[dict[str, str]]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def keyboard(kind: str, data: dict[str, Any], actions: list[dict[str, Any]], url: str | None) -> dict | None:
    """Telegram buttons for one notification (HA push actions are translated, others derived from data)."""
    from app.services import ha_actions

    buttons: list[dict[str, str]] = []
    for a in actions:
        parsed = ha_actions.parse_action(str(a.get("action") or ""))
        if parsed:
            verb, draft_id = parsed
            icon = {"SEND": "✅ ", "REPLY": "↩️ ", "REVISE": "✍️ ", "DISCARD": "🗑 ", "CAL": "📅 ", "UNCAL": "↩️ ",
                    "SHIP": "📦 ", "PAID": "💶 ", "BUY": "📦 ", "DECL": "✖️ ", "CANCEL": "⏹ "}.get(verb, "")
            buttons.append(_button(icon + str(a.get("title") or verb), f"D_{verb}", str(draft_id)))
    conv, ad_id = data.get("conversation_id"), data.get("ad_id")
    rows = _rows(buttons)
    if kind == "auto_reply_pending" and data.get("draft_id") and any(b["text"].startswith("✍️") for b in buttons):
        # one tap instead of "Revise" + typing: the other decisions Claude saw for this situation
        draft_id = int(data["draft_id"])
        with session() as db:
            draft = db.get(Draft, draft_id)
        alts = (draft.alternatives or []) if draft else []
        rows += _rows([_button(a["label"], "D_ALT", f"{draft_id}|{a['label']}|{a['instruction']}") for a in alts])
    buttons = []
    ref = data.get("ref") or ad_id                # hits of other sites: "<platform>:<id>"
    if kind == "message" and conv:
        buttons += [_button("✍️ " + t("Draft with Claude"), "C_DRAFT", conv), _button("↩️ " + t("Reply"), "C_REPLY", conv)]
    elif kind == "auto_reply" and conv:
        ref = str(data.get("draft_id") or f"conv:{conv}")
        buttons += [_button("👍", "F_UP", ref), _button("👎 " + t("Bad answer"), "F_DOWN", ref)]
        if ad_id:
            buttons.append(_button("⏸ " + t("Auto answers off for this ad"), "A_OFF", ad_id))
    elif kind == "purchase" and data.get("auto_buy") and data.get("purchase_id"):   # the countdown: stop it
        buttons.append(_button("⏹ " + t("Cancel the purchase"), "AB_STOP", str(data["purchase_id"])))
    elif kind in ("hit", "price_drop") and ad_id:
        buttons += [_button("⭐ " + t("Keep"), "H_SAVE", ref), _button("🚫 " + t("Ignore"), "H_IGNORE", ref)]
        if get_setting("purchase_enabled") == "1" and ":" not in str(ref):   # purchase orders: willhaben only
            buttons.append(_button("🛒 " + t("Start purchase order"), "P_BUY", ad_id))
    rows += _rows(buttons)
    if url and url.startswith("https://"):
        rows.append([open_button("🔗 " + t("Open"), url)])
    return {"inline_keyboard": rows} if rows else None


# ------------------------------------------------------------------ outgoing
async def send(kind: str, title: str, message: str, *, url: str | None = None, image: str | None = None,
               data: dict[str, Any] | None = None, actions: list[dict[str, Any]] | None = None,
               tag: str | None = None, fresh: bool = False) -> bool:
    """Post one notification to the linked chat. A known `tag` edits the earlier message instead (like HA)."""
    if not active() or (kind not in ("test", "health") and kind not in enabled_kinds()):   # health: watchdog
        return False
    text = f"<b>{_esc(title)}</b>\n{_esc(message)}"
    try:
        markup = keyboard(kind, data or {}, actions or [], url)
        prev = _message_for(tag) if tag and not fresh else None
        if prev is not None:
            try:
                await _edit(prev, text, markup)
                _remember(tag, {"chat": {"id": prev.chat_id}, "message_id": prev.message_id}, photo=prev.photo, text=text)
                return True
            except TelegramError as e:
                if "not modified" in str(e):
                    return True
                log.info("telegram edit failed, sending new: %s", e)
        if image and image.startswith("https://"):
            try:
                msg = await api("sendPhoto", chat_id=chat_id(), photo=image, caption=text[:1024], parse_mode="HTML",
                                reply_markup=markup)
            except TelegramError as e:      # image URL Telegram cannot fetch -> plain text
                log.info("telegram photo failed: %s", e)
                image, msg = None, None
            else:
                _remember(tag, msg, photo=True, text=text)
                return True
        msg = await api("sendMessage", chat_id=chat_id(), text=text[:4096], parse_mode="HTML",
                        reply_markup=markup, link_preview_options={"is_disabled": True})
        _remember(tag, msg, photo=False, text=text)
        return True
    except (TelegramError, httpx.HTTPError) as e:
        log.warning("telegram send failed: %s", e)
        return False


TYPING_EVERY_S = 4.5   # Telegram shows "typing…" for ~5 s per call


@asynccontextmanager
async def typing():
    """Show "typing…" in the linked chat while the block runs (Claude writing an answer)."""
    if not active():
        yield
        return

    async def keep_typing() -> None:
        while True:
            try:
                await api("sendChatAction", chat_id=chat_id(), action="typing")
            except (TelegramError, httpx.HTTPError) as e:
                log.debug("sendChatAction failed: %s", e)
            await asyncio.sleep(TYPING_EVERY_S)

    task = asyncio.get_running_loop().create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def clear(tag: str) -> None:
    """Take the buttons off a tagged message (it was handled somewhere)."""
    prev = _message_for(tag) if active() else None
    if prev is None:
        return
    try:
        await api("editMessageReplyMarkup", chat_id=prev.chat_id, message_id=prev.message_id,
                  reply_markup={"inline_keyboard": []})
    except (TelegramError, httpx.HTTPError) as e:
        if "not modified" not in str(e):        # buttons already gone (a progress line took them)
            log.info("telegram clear failed: %s", e)


async def progress(tag: str, line: str) -> None:
    """Progress into the tagged message ("✍️ Claude is writing …"): text + line, buttons gone while it runs."""
    prev = _message_for(tag) if active() else None
    if prev is None or not prev.text:
        return
    try:
        await _edit(prev, f"{prev.text}\n\n<i>{_esc(line)}</i>", None)
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram progress failed: %s", e)


async def settle(tag: str, line: str, url: str | None = None) -> bool:
    """The matter of a tagged message was handled in the app (dashboard or Mini App): the message says so in a last
    line ("✅ Sent in the app") and its buttons go – only "Open" stays when `url` is given – as if it had been tapped
    here (10-29: they stayed, and a later tap only said "Already done"). Nothing when there is no such message or
    it says so already. True when it was edited."""
    prev = _message_for(tag) if active() else None
    if prev is None:
        return False
    text = prev.text or ""
    if line in text:
        return False
    markup = {"inline_keyboard": [[open_button("🔗 " + t("Open"), url)]]} if url and url.startswith("https://") \
        else {"inline_keyboard": []}
    try:
        if text:
            new = f"{text}\n\n<b>{_esc(line)}</b>"
            await _edit(prev, new, markup)
            with session() as db:
                row = db.get(TelegramMessage, tag)
                if row is not None:
                    row.text = new
                    db.add(row)
                    db.commit()
        else:
            await api("editMessageReplyMarkup", chat_id=prev.chat_id, message_id=prev.message_id, reply_markup=markup)
    except (TelegramError, httpx.HTTPError) as e:
        if "not modified" not in str(e):
            log.info("telegram settle failed: %s", e)
        return False
    return True


_settle_tasks: set[asyncio.Task] = set()


def settle_later(tags: list[str], line: str, url: str | None = None) -> None:
    """settle() for each tag without waiting for Telegram (from a page's request)."""
    async def run() -> None:
        for tag in tags:
            await settle(tag, line, url)
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:                  # no loop (a sync caller in tests)
        return
    _settle_tasks.add(task)
    task.add_done_callback(_settle_tasks.discard)


def retag(old: str, new: str) -> bool:
    """The message sent for `old` is now the one for `new` (a chat message becomes its draft offer)."""
    if old == new:
        return True
    with session() as db:
        row = db.get(TelegramMessage, old)
        if row is None:
            return False
        if (dup := db.get(TelegramMessage, new)) is not None:
            db.delete(dup)
        db.delete(row)
        db.add(TelegramMessage(tag=new, chat_id=row.chat_id, message_id=row.message_id, photo=row.photo, text=row.text))
        db.commit()
    return True


def _message_for(tag: str) -> TelegramMessage | None:
    with session() as db:
        return db.get(TelegramMessage, tag)


def _tag_of(chat: int | None, message_id: int | None) -> str | None:
    """Which notification a message in the chat was (for replies to it)."""
    if not chat or not message_id:
        return None
    with session() as db:
        row = db.exec(select(TelegramMessage).where(TelegramMessage.chat_id == chat,
                                                    TelegramMessage.message_id == message_id)).first()
    return row.tag if row else None


def _target_of(tag: str) -> tuple[str, int | None] | None:
    """(conversation id, held-back draft id or None) behind a notification tag."""
    from app.db import Shipment

    kind, _, ref = tag.rpartition("-")
    with session() as db:
        if kind == "wh-msg":
            return (ref, None) if db.get(Conversation, ref) else None
        if kind in ("wh-draft", "wh-meetup") and ref.isdigit():
            d = db.get(Draft, int(ref))
            return (d.conversation_id, d.id if d.status == "draft" else None) if d else None
        if kind == "wh-ship" and ref.isdigit():
            s = db.get(Shipment, int(ref))
            return (s.conversation_id, None) if s else None
    return None


def _remember(tag: str | None, msg: dict[str, Any] | None, photo: bool, text: str | None = None) -> None:
    if not tag or not msg:
        return
    with session() as db:
        row = db.get(TelegramMessage, tag) or TelegramMessage(tag=tag, chat_id=0, message_id=0)
        row.chat_id, row.message_id, row.photo = msg["chat"]["id"], msg["message_id"], photo
        if text is not None:
            row.text = text
        db.add(row)
        db.commit()


async def _edit(prev: TelegramMessage, text: str, markup: dict | None) -> None:
    markup = markup or {"inline_keyboard": []}
    if prev.photo:
        await api("editMessageCaption", chat_id=prev.chat_id, message_id=prev.message_id, caption=text[:1024],
                  parse_mode="HTML", reply_markup=markup)
    else:
        await api("editMessageText", chat_id=prev.chat_id, message_id=prev.message_id, text=text[:4096],
                  parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})


async def _say(text: str, markup: dict | None = None, reply_to: int | None = None) -> dict | None:
    try:
        return await api("sendMessage", chat_id=chat_id(), text=text[:4096], parse_mode="HTML", reply_markup=markup,
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
                         link_preview_options={"is_disabled": True})
    except (TelegramError, httpx.HTTPError) as e:
        log.warning("telegram reply failed: %s", e)
        return None


async def _ask(prompt: str, verb: str, ref: str, placeholder: str, origin: dict[str, Any] | None = None) -> None:
    """ForceReply prompt quoting the notification it belongs to; the user's reply carries the text for verb/ref."""
    msg = await _say(prompt, {"force_reply": True, "input_field_placeholder": placeholder[:64]},
                     reply_to=(origin or {}).get("message_id"))
    if msg:
        _pending[msg["message_id"]] = (verb, ref, time.monotonic(), _tag_of(chat_id(), (origin or {}).get("message_id")))


# ------------------------------------------------------------------ incoming
async def handle_update(u: dict[str, Any]) -> None:
    from app.services import timeline

    timeline.set_actor("telegram")      # what the buttons and replies cause shows up as done via Telegram
    if cq := u.get("callback_query"):
        await _on_callback(cq)
    elif msg := u.get("message"):
        await _on_message(msg)


async def _on_message(msg: dict[str, Any]) -> None:
    chat = (msg.get("chat") or {})
    text = (msg.get("text") or "").strip()
    if chat_id() is None:
        await _on_candidate(chat)
        return
    if chat.get("id") != chat_id():
        return                                          # not our user: stay silent
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    pending = _pending.pop(reply_to, None) if reply_to else None
    if pending is None and reply_to and text and not text.startswith("/"):
        tag = _tag_of(chat.get("id"), reply_to)            # swipe-reply to a notification: answer that chat
        target = _target_of(tag) if tag else None
        if target:
            await _reply_in(target[0], target[1], text, msg.get("message_id"), tag)
            return
    if pending is None and text and not text.startswith("/"):
        open_prompts = _open_prompts()
        if len(open_prompts) > 1:
            await _say(t("Several questions are open – please reply (swipe) to the one you mean."),
                       reply_to=msg.get("message_id"))
            return
        pending = _pending.pop(open_prompts[0], None) if open_prompts else None
    if pending:
        verb, ref, _, origin = pending
        await _complete(verb, ref, text, msg.get("message_id"), origin)
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd in ("/drafts", "/entwuerfe"):
        await _cmd_drafts()
    elif cmd == "/auto":
        await _cmd_auto()
    elif cmd == "/status":
        await _cmd_status()
    else:
        await _say(_help())


async def _on_candidate(chat: dict[str, Any]) -> None:
    """Nothing linked yet: remember the chat and tell it once (per CANDIDATE_TTL) to confirm in Settings."""
    cid = chat.get("id")
    live = {c["chat_id"] for c in candidates()}          # drops expired entries first
    if not isinstance(cid, int) or cid in live or len(live) >= MAX_CANDIDATES:
        return
    name = " ".join(x for x in (chat.get("first_name") or chat.get("title"),
                                 f"@{chat['username']}" if chat.get("username") else None) if x) or str(cid)
    code = f"{secrets.randbelow(10000):04d}"
    _candidates[cid] = (name, time.monotonic(), code)
    with suppress(TelegramError, httpx.HTTPError):
        await api("sendMessage", chat_id=cid, text="👋 " + t(
            "Request received – confirm the link in the app under Settings → Telegram. Your code: {code}", code=code))


def _open_prompts() -> list[int]:
    """Prompt message ids still waiting for text, newest first (plain text without "reply" goes to a lone one)."""
    now = time.monotonic()
    for k in [k for k, v in _pending.items() if now - v[2] > PENDING_TTL]:
        _pending.pop(k, None)
    return sorted(_pending, key=lambda k: _pending[k][2], reverse=True)


def _help() -> str:
    return "\n".join([
        t("Buttons under each notification: send, answer, let Claude rewrite, feedback."),
        t("To answer a chat, reply (swipe) to its notification and type – no button needed."),
        "", "/drafts – " + t("Open answer drafts"), "/auto – " + t("Automatic answers per ad"),
        "/status – " + t("Session and counters"),
    ])


async def _on_callback(cq: dict[str, Any]) -> None:
    from app.services import ha_actions

    msg = cq.get("message") or {}
    cq_id = cq.get("id")
    if chat_id() is None or (msg.get("chat") or {}).get("id") != chat_id():
        await _answer(cq_id, t("Not allowed."))
        return
    raw = str(cq.get("data") or "")
    with session() as db:
        row = db.get(TelegramAction, raw[2:]) if raw.startswith("t:") else None
    if row is None:
        await _answer(cq_id, t("This button has expired."))
        return
    verb, ref = row.verb, row.ref
    if not verb.startswith("D_"):         # the D_ buttons are recorded by ha_actions.handle
        from app.services import timeline
        timeline.action(f"telegram.{verb.lower()}", t("Telegram button: {verb}", verb=verb), ref=ref)

    tag = _tag_of((msg.get("chat") or {}).get("id"), msg.get("message_id"))
    if verb in ("D_SEND", "D_DISCARD", "D_CAL", "D_UNCAL", "D_SHIP", "D_PAID", "D_BUY", "D_DECL", "D_CANCEL"):
        await _answer(cq_id, "…")
        if verb == "D_SHIP" and tag:
            await progress(tag, "📦 " + t("Preparing the label form …"))
        res = await ha_actions.handle({"action": ha_actions.action_id(verb[2:], int(ref))})
        if res is None:
            await _say("ℹ️ " + t("Already done."), reply_to=msg.get("message_id"))
            await _drop_buttons(msg)
        return
    if verb == "D_ALT":
        draft_id, label, instruction = ref.split("|", 2)
        await _answer(cq_id, "✍️ " + label)
        if tag:
            await progress(tag, "✍️ " + t("Claude is revising: {what} …", what=label))
        res = await ha_actions.handle({"action": ha_actions.action_id("REVISE", int(draft_id)), "reply_text": instruction,
                                       "chosen": True})
        if res is None:
            await _say("ℹ️ " + t("Already done."), reply_to=msg.get("message_id"))
            await _drop_buttons(msg)
        return
    if verb == "D_REPLY":
        await _answer(cq_id)
        await _ask(t("Type the answer – it is sent exactly like this:"), verb, ref, t("Your answer"), msg)
    elif verb == "D_REVISE":
        await _answer(cq_id)
        await _ask(t("How should Claude change the answer?"), verb, ref, t("e.g. friendlier, offer 18 €"), msg)
    elif verb == "C_REPLY":
        await _answer(cq_id)
        await _ask(t("Type your answer:"), verb, ref, t("Your answer"), msg)
    elif verb == "C_DRAFT":
        await _answer(cq_id, t("Claude is writing a draft …"))
        if tag:
            await progress(tag, "✍️ " + t("Claude is writing a draft …"))
        else:
            await _drop_buttons(msg)
        await _draft_for(ref, replaces=tag)
    elif verb == "F_UP":
        d = _resolve_draft(ref)
        if d:
            from app.services import feedback
            feedback.rate(d.id, "up")
        await _answer(cq_id, "👍 " + t("Thanks, noted."))
    elif verb == "F_DOWN":
        d = _resolve_draft(ref)
        if d:
            from app.services import feedback
            feedback.rate(d.id, "down")
            await _answer(cq_id)
            await _ask(t("What was wrong? Your note is used for future automatic answers."), "F_NOTE", str(d.id),
                       t("e.g. never promise shipping"), msg)
        else:
            await _answer(cq_id, t("Answer not found."))
    elif verb in ("A_ON", "A_OFF"):
        on = verb == "A_ON"
        name = _set_auto(ref, on)
        await _answer(cq_id, t("Automatic answers on: {ad}", ad=name) if on else t("Automatic answers off: {ad}", ad=name))
        # the tapped button flips in place (notification: "off" <-> "on again"; /auto list: the checkbox)
        in_list = msg.get("text", "").startswith(t("Automatic answers – tap to switch:"))
        if in_list:
            new = _button(("✅ " if on else "⬜ ") + name[:40], "A_OFF" if on else "A_ON", ref)
        else:
            new = _button("⏸ " + t("Auto answers off for this ad"), "A_OFF", ref) if on else \
                _button("▶️ " + t("Switch on again"), "A_ON", ref)
        await _swap_button(msg, raw, new)
    elif verb == "P_BUY":
        await _start_purchase(cq_id, msg, raw, ref)
    elif verb == "AB_STOP":                      # auto-buy's countdown: taken back before anything is bought
        from app.services import jobs
        stopped = jobs.cancel_key(f"purchase:{ref}")
        await _answer(cq_id, ("⏹ " + t("Purchase cancelled")) if stopped else t("Too late – the purchase is running already."))
        if stopped:
            await _drop_buttons(msg)
    elif verb in ("H_SAVE", "H_IGNORE"):
        n = _set_hit_state(ref, "saved" if verb == "H_SAVE" else "ignored")
        await _answer(cq_id, ("⭐ " + t("Kept")) if verb == "H_SAVE" else ("🚫 " + t("Ignored")) if n else t("Hit not found."))
        if n:
            await _drop_buttons(msg)
    else:
        await _answer(cq_id)


async def _complete(verb: str, ref: str, text: str, msg_id: int | None, origin: str | None = None) -> None:
    """The text for a ForceReply prompt arrived (`origin`: tag of the notification the prompt belonged to)."""
    from app.services import feedback, ha_actions

    if not text:
        return
    if verb == "D_REVISE":
        await progress(ha_actions.tag_for(int(ref)), "✍️ " + t("Claude is revising: {what} …", what=text[:60]))
    if verb in ("D_REPLY", "D_REVISE"):
        res = await ha_actions.handle({"action": ha_actions.action_id(verb[2:], int(ref)), "reply_text": text})
        if res is None:     # failures are reported by handle() itself (notify -> also Telegram)
            await _say("ℹ️ " + t("Already done."), reply_to=msg_id)
    elif verb == "C_REPLY":
        await _reply_in(ref, None, text, msg_id, origin)
    elif verb == "F_NOTE":
        feedback.rate(int(ref), "down", text)
        await _say("📝 " + t("Noted – future automatic answers will take it into account."), reply_to=msg_id)


async def _reply_in(conversation_id: str, draft_id: int | None, text: str, msg_id: int | None,
                    origin: str | None) -> None:
    """Text typed for a chat: sends the held-back draft with it, or a new message. Result edits the notification."""
    from app.services import chats, ha_actions

    if draft_id is not None:       # ha_actions.handle pushes "Sent" with the draft's tag -> edits that message
        res = await ha_actions.handle({"action": ha_actions.action_id("REPLY", draft_id), "reply_text": text})
        if res is not None:
            return
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            await _say(t("Chat not found."), reply_to=msg_id)
            return
        d = Draft(conversation_id=conversation_id, text=text, model="telegram")
        db.add(d)
        db.commit()
        db.refresh(d)
    try:
        await chats.send_draft(d.id, text)
    except Exception as e:  # noqa: BLE001 - report every failure back
        await _say("⚠️ " + t("Sending failed: {error}", error=_esc(str(e)[:200])), reply_to=msg_id)
        return
    prev = _message_for(origin) if origin else None
    if prev is not None and prev.text:
        try:
            await _edit(prev, f"{prev.text}\n\n✅ <b>{_esc(t('Sent'))}:</b> {_esc(text[:300])}", None)
            _remember(origin, {"chat": {"id": prev.chat_id}, "message_id": prev.message_id}, photo=prev.photo,
                      text=f"{prev.text}\n\n✅ <b>{_esc(t('Sent'))}:</b> {_esc(text[:300])}")
            return
        except (TelegramError, httpx.HTTPError) as e:
            log.info("telegram edit failed, replying instead: %s", e)
    await _say("✅ " + t("Sent."), reply_to=msg_id)


async def _swap_button(msg: dict[str, Any], pressed: str, new: dict[str, Any]) -> None:
    """Replace the tapped button in the message's keyboard (the message itself stays)."""
    rows = [[new if b.get("callback_data") == pressed else b for b in row]
            for row in ((msg.get("reply_markup") or {}).get("inline_keyboard") or [])]
    if not any(b is new for row in rows for b in row):
        rows.append([new])
    try:
        await api("editMessageReplyMarkup", chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
                  reply_markup={"inline_keyboard": rows})
    except (TelegramError, httpx.HTTPError) as e:
        log.info("telegram button swap failed: %s", e)


async def _draft_for(conversation_id: str, replaces: str | None = None) -> None:
    from app.services import chats

    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        await _say(t("Chat not found."))
        return
    try:
        d = await chats.draft_reply(conversation_id)
    except Exception as e:  # noqa: BLE001
        await _say("⚠️ " + t("Claude could not write a draft: {error}", error=_esc(str(e)[:200])))
        return
    await chats.offer_draft(conv, d, t("requested in Telegram"), replaces=replaces)


def _resolve_draft(ref: str) -> Draft | None:
    """Draft id, or "conv:<id>" for the newest automatically sent answer in that chat."""
    with session() as db:
        if ref.isdigit():
            return db.get(Draft, int(ref))
        if ref.startswith("conv:"):
            return db.exec(select(Draft).where(Draft.conversation_id == ref[5:], Draft.status == "sent")
                           .order_by(col(Draft.id).desc())).first()
    return None


def _set_auto(ad_id: str, on: bool) -> str:
    """Only the switch; the per-ad instructions stay untouched. A push about a renewed ad's chat names the old id:
    the switch is on the live copy (chats._auto_ad reads that one)."""
    with session() as db:
        ad = current_ad(db, ad_id)
        if ad is None:
            return ad_id
        ad.auto_reply = on
        db.add(ad)
        db.commit()
        return ad.heading or ad_id


def _set_hit_state(ref: str, state: str) -> int:
    """Every agent's hit of this ad (the ref names the site; a bare id is a willhaben id from an older push)."""
    from app.platforms import InvalidRef, split_ref

    try:
        platform, ad_id = split_ref(ref)
    except InvalidRef:
        return 0
    with session() as db:
        hits = db.exec(select(Hit).where(Hit.ad_id == ad_id, Hit.platform == platform)).all()
        for h in hits:
            h.state = state
            db.add(h)
        db.commit()
    return len(hits)


async def _start_purchase(cq_id: str | None, msg: dict[str, Any], pressed: str, ad_id: str) -> None:
    """One tap on a hit: a purchase order with the defaults of the buy dialog (mode from Settings, max price =
    asking price + shipping). The button becomes a link to the order; refusals come back as the toast."""
    from app.services import purchase
    from app.services.notify import push_url

    with session() as db:
        h = db.exec(select(Hit).where(Hit.ad_id == ad_id).order_by(col(Hit.id).desc())).first()
    mode = "paylivery" if get_setting(purchase.SETTING_PREFER_PAYLIVERY) == "1" else "pickup"
    try:
        pid = purchase.start(ad_id, mode, None, title=h.heading if h else None, url=h.url if h else None,
                             image_url=h.image_url if h else None, price=h.price if h else None)
    except purchase.PurchaseError as e:
        await _answer(cq_id, "⚠️ " + str(e))
        return
    await _answer(cq_id, "🛒 " + t("Purchase order #{id} started", id=pid))
    url = push_url("purchase", f"/purchases/{pid}")
    if url and url.startswith("https://"):
        await _swap_button(msg, pressed, open_button("🛒 " + t("Purchase order #{id}", id=pid), url))
    else:
        await _swap_button(msg, pressed, _button("🛒 " + t("Purchase order #{id}", id=pid), "P_NOOP", str(pid)))


async def _answer(cq_id: str | None, text: str = "") -> None:
    try:
        await api("answerCallbackQuery", callback_query_id=cq_id, text=text[:200] or None)
    except (TelegramError, httpx.HTTPError) as e:
        log.info("answerCallbackQuery failed: %s", e)


async def _drop_buttons(msg: dict[str, Any]) -> None:
    try:
        await api("editMessageReplyMarkup", chat_id=(msg.get("chat") or {}).get("id"), message_id=msg.get("message_id"),
                  reply_markup={"inline_keyboard": []})
    except (TelegramError, httpx.HTTPError):
        pass


# ------------------------------------------------------------------ commands
def can_send(d: Draft) -> bool:
    """Same rule as chats.offer_draft: no plain "send" while the text has [placeholders] or looks like a scam."""
    return not ("[" in d.text and "]" in d.text) and d.intent != "scam_suspect" and bool(d.text.strip())


async def _cmd_drafts() -> None:
    from app.services import ha_actions

    with session() as db:
        rows = db.exec(select(Draft).where(Draft.status == "draft").order_by(col(Draft.id).desc()).limit(5)).all()
        convs = {c.id: c for c in db.exec(select(Conversation).where(col(Conversation.id).in_([d.conversation_id for d in rows])))}
    if not rows:
        await _say(t("No open drafts."))
        return
    for d in reversed(rows):
        conv = convs.get(d.conversation_id)
        label = t("Draft")
        await send("auto_reply_pending", f"{label}: {conv.partner_name if conv else ''}".strip(),
                   f"„{d.text[:300]}“\n{(conv.ad_title if conv else '') or ''}",
                   data={"conversation_id": d.conversation_id, "draft_id": d.id},
                   actions=ha_actions.draft_actions(d.id, can_send(d)), tag=ha_actions.tag_for(d.id), fresh=True)


async def _cmd_auto() -> None:
    with session() as db:
        ads = db.exec(select(MyAd).where(col(MyAd.status).not_in(("expired", "deleted")), col(MyAd.replaced_by).is_(None))
                      .order_by(col(MyAd.heading))).all()
    if not ads:
        await _say(t("No own ads imported yet."))
        return
    rows = [[_button(("✅ " if ad.auto_reply else "⬜ ") + (ad.heading or ad.ad_id)[:40],
                     "A_OFF" if ad.auto_reply else "A_ON", ad.ad_id)] for ad in ads[:30]]
    await _say(t("Automatic answers – tap to switch:"), {"inline_keyboard": rows})


async def _cmd_status() -> None:
    from sqlalchemy import func

    from app.services import chats
    from app.willhaben.session import get_session

    st = get_session().status()
    with session() as db:
        new_hits = db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one()
        unread, drafts = chats.summary_counts(db)       # as the chat list counts them
    login = t("logged in") if st.get("logged_in") else t("NOT logged in")
    await _say("\n".join([f"willhaben: <b>{login}</b>", t("New hits: {n}", n=new_hits),
                          t("Unread messages: {n}", n=unread), t("Open drafts: {n}", n=drafts)]))


# ------------------------------------------------------------------ polling
def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe(u: dict[str, Any]) -> None:
    try:
        await handle_update(u)
    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
        log.exception("telegram update failed")


def _prune_actions() -> None:
    with session() as db:
        db.exec(delete(TelegramAction).where(col(TelegramAction.created_at) < utcnow() - timedelta(days=30)))
        db.commit()


async def listen_forever() -> None:
    """getUpdates long polling; each update runs as its own task (a Claude draft takes a minute)."""
    offset, delay, last_prune = None, 5, 0.0
    while True:
        if not token():
            await asyncio.sleep(10)
            continue
        if time.monotonic() - last_prune > 86400:
            _prune_actions()
            await menu_button()          # the Mini App's button, kept current (a new public URL, a new bot)
            last_prune = time.monotonic()
        try:
            updates = await api("getUpdates", _timeout=65, offset=offset, timeout=50,
                                allowed_updates=["message", "callback_query"])
            delay = 5
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network, token revoked, Telegram down
            log.info("telegram polling failed: %s (retry in %ss)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        for u in updates or []:
            offset = u["update_id"] + 1
            _spawn(_safe(u))
