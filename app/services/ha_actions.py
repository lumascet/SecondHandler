"""Buttons on Home Assistant pushes: approve, rewrite or discard a held-back automatic answer.

The HA companion app fires `mobile_app_notification_action` when a button is tapped. The app keeps a
websocket subscription to that event (long-lived token from ha_auth) and acts on our own action ids:

  WH_SEND_<draft>_<sig>      send the draft as it is
  WH_REPLY_<draft>_<sig>     "Direct answer": send the text typed into the push (behavior: textInput)
  WH_REVISE_<draft>_<sig>    "Revise with Claude": typed text is an instruction, a corrected draft comes as new push
  WH_DISCARD_<draft>_<sig>   drop the draft
  WH_CAL_<draft>_<sig>       add the appointment the (sent) answer agreed to Google Calendar
  WH_UNCAL_<draft>_<sig>     take it out again (undo for appointments added automatically)
  WH_SHIP_<shipment>_<sig>   open the carrier's label form and fill it (the user pays in the remote view)
  WH_PAID_<shipment>_<sig>   the buyer has paid (marked by hand) -> ask for the go, or buy by the automatic rules
  WH_BUY_<shipment>_<sig>    approve: buy the label (printed when automatic printing is on)
  WH_DECL_ / WH_CANCEL_      decline the purchase / stop the automatic countdown (shipment ids)

<sig> is an HMAC over verb+draft with a secret in DATA_DIR, so no other event source can make the app
send. A draft is only acted on while it is still a draft, so a second tap does nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any

from app.config import get_settings
from app.i18n import t

log = logging.getLogger(__name__)

EVENT = "mobile_app_notification_action"
# Websocket listener state, read by the connection watchdog (app/services/health.py).
listener: dict[str, Any] = {"running": False, "connected": False, "error": None}
_ACTION_RE = re.compile(r"WH_(SEND|REPLY|REVISE|DISCARD|CAL|UNCAL|SHIP|PAID|BUY|DECL|CANCEL)_(\d+)_([0-9a-f]{12})")
SHIPMENT_VERBS = ("PAID", "BUY", "DECL", "CANCEL")


def _secret() -> bytes:
    p = get_settings().data_dir / "action-secret"
    try:
        return p.read_bytes()
    except OSError:
        key = secrets.token_bytes(32)
        p.write_bytes(key)
        p.chmod(0o600)
        return key


def _sig(verb: str, draft_id: int) -> str:
    return hmac.new(_secret(), f"{verb}:{draft_id}".encode(), hashlib.sha256).hexdigest()[:12]


def action_id(verb: str, draft_id: int) -> str:
    return f"WH_{verb}_{draft_id}_{_sig(verb, draft_id)}"


def tag_for(draft_id: int) -> str:
    return f"wh-draft-{draft_id}"


def draft_actions(draft_id: int, can_send: bool) -> list[dict[str, Any]]:
    """Buttons for a held-back answer. No plain "send" when the text still has [placeholders].

    Titles follow the ui_lang setting (pushes are built outside requests); the action ids never change.
    """
    actions = []
    if can_send:
        actions.append({"action": action_id("SEND", draft_id), "title": t("Send")})
    actions.append({"action": action_id("REPLY", draft_id), "title": t("Direct answer"), "behavior": "textInput",
                    "textInputButtonTitle": t("Send"), "textInputPlaceholder": t("Sent exactly as typed")})
    actions.append({"action": action_id("REVISE", draft_id), "title": t("Revise with Claude"), "behavior": "textInput",
                    "textInputButtonTitle": t("Revise"), "textInputPlaceholder": t("e.g. friendlier, offer €18")})
    actions.append({"action": action_id("DISCARD", draft_id), "title": t("Discard"), "destructive": True})
    return actions


def ship_tag(shipment_id: int) -> str:
    return f"wh-ship-{shipment_id}"


def ship_actions(shipment_id: int) -> list[dict[str, Any]]:
    return [{"action": action_id("SHIP", shipment_id), "title": t("Create label")}]


def _ship_button(verb: str, shipment_id: int, title: str) -> dict[str, Any]:
    return {"action": action_id(verb, shipment_id), "title": title}


def offer_actions(shipment_id: int, label: bool) -> list[dict[str, Any]]:
    """"Address received – not paid yet": the payment mark, and the manual label form when it can be filled."""
    acts = [_ship_button("PAID", shipment_id, t("Paid"))]
    return acts + (ship_actions(shipment_id) if label else [])


def claim_actions(shipment_id: int) -> list[dict[str, Any]]:
    """The buyer wrote that they paid: once the money is there, one tap marks it and asks for the label."""
    return [_ship_button("PAID", shipment_id, t("Paid, buy label"))]


def approval_actions(shipment_id: int) -> list[dict[str, Any]]:
    from app.services import shipping
    return [_ship_button("BUY", shipment_id, shipping.buy_title(shipping.get(shipment_id))),
            _ship_button("DECL", shipment_id, t("Decline"))]


def cancel_actions(shipment_id: int) -> list[dict[str, Any]]:
    return [_ship_button("CANCEL", shipment_id, t("Cancel"))]


def meetup_tag(draft_id: int) -> str:
    return f"wh-meetup-{draft_id}"


def meetup_actions(draft_id: int) -> list[dict[str, Any]]:
    return [{"action": action_id("CAL", draft_id), "title": t("Add to calendar")}]


def meetup_undo_actions(draft_id: int) -> list[dict[str, Any]]:
    return [{"action": action_id("UNCAL", draft_id), "title": t("Remove from calendar"), "destructive": True}]


def parse_action(action: str) -> tuple[str, int] | None:
    m = _ACTION_RE.fullmatch(action or "")
    if not m:
        return None
    verb, draft_id, sig = m.group(1), int(m.group(2)), m.group(3)
    if not hmac.compare_digest(sig, _sig(verb, draft_id)):
        log.warning("ignored push action with bad signature: %s", action)
        return None
    return verb, draft_id


async def handle(event_data: dict[str, Any]) -> str | None:
    """Act on one tapped button (Home Assistant push or Telegram); returns a short result text for the log.
    Each handled tap is a timeline action."""
    from app.services import timeline

    parsed = parse_action(str(event_data.get("action") or ""))
    actor = timeline.current().get("actor") if timeline.current().get("actor") == "telegram" else "ha"
    ids: dict[str, Any] = {}
    if parsed:
        ids = {"shipment_id": parsed[1]} if parsed[0] == "SHIP" or parsed[0] in SHIPMENT_VERBS else {"reply_draft": parsed[1]}
        ids = timeline.resolve_ids(ids)
    with timeline.context(actor=actor, **ids):
        res = await _handle(event_data)
        if parsed and res is not None:
            failed = res.startswith("error")
            timeline.action(f"button.{parsed[0].lower()}", t("Push button: {verb}", verb=parsed[0]), ok=not failed,
                            error=res[7:] if failed else None, payload={"result": res})
    return res


async def _handle(event_data: dict[str, Any]) -> str | None:
    from app.db import Draft, session
    from app.services import chats
    from app.services.notify import clear_push, notify

    parsed = parse_action(str(event_data.get("action") or ""))
    if not parsed:
        return None
    verb, draft_id = parsed
    if verb == "SHIP":
        return await _start_label(draft_id)      # the id is a shipment id here
    if verb in SHIPMENT_VERBS:
        return await _shipment_action(verb, draft_id)
    if verb == "CAL":
        return await _add_meetup(draft_id)
    if verb == "UNCAL":
        return await _remove_meetup(draft_id)
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None or d.status != "draft":
        return None            # already handled (second tap, or done in the dashboard)
    await clear_push(tag_for(draft_id))
    if verb == "DISCARD":
        chats.discard_draft(draft_id, by_owner=True)
        return "discarded"
    if verb == "REVISE":        # typed instruction, or one of Claude's alternatives tapped ("chosen")
        new = await chats.revise_and_offer(draft_id, str(event_data.get("reply_text") or ""),
                                           chosen=bool(event_data.get("chosen")))
        return f"revised -> draft {new.id}" if new else None
    text = d.text if verb == "SEND" else str(event_data.get("reply_text") or "").strip()
    if not text:
        return None
    try:
        await chats.send_draft(draft_id, text)
    except chats.AlreadySent:
        return None            # the other tap (or the dashboard) is sending it: nothing to report twice
    except Exception as e:  # noqa: BLE001 - report every failure back to the phone
        await notify("auto_reply", t("Sending failed"), str(e)[:200],
                     url=f"{get_settings().public_url.rstrip('/')}/chats/{d.conversation_id}")
        return f"error: {e}"
    await notify("auto_reply", t("Sent"), text[:200], tag=tag_for(draft_id))
    return "sent"


async def _start_label(shipment_id: int) -> str | None:
    from app.services import label_fill, shipping
    from app.services.notify import clear_push, notify

    s = shipping.get(shipment_id)
    if s is None or s.status != "draft":
        return None
    await clear_push(ship_tag(shipment_id))
    url = f"{get_settings().public_url.rstrip('/')}/session"
    try:
        msg = await label_fill.start(shipment_id)
    except Exception as e:  # noqa: BLE001 - report every failure back to the phone
        await notify("shipping", t("Label form failed"), str(e)[:200],
                     url=f"{get_settings().public_url.rstrip('/')}/chats/{s.conversation_id}#shipping",
                     data=shipping.event_data(shipping.get(shipment_id) or s))
        return f"error: {e}"
    await notify("shipping", t("Label form is ready – check and pay"), msg[:200], url=url, tag=ship_tag(shipment_id),
                 data=shipping.event_data(shipping.get(shipment_id) or s))
    return "label form"


async def _shipment_action(verb: str, shipment_id: int) -> str | None:
    """Payment and approval buttons of a shipment push (the id is a shipment id)."""
    from app.services import shipping
    from app.services.notify import clear_push, notify

    s = shipping.get(shipment_id)
    if s is None or s.status not in ("draft", "awaiting"):
        return None                              # already handled
    await clear_push(ship_tag(shipment_id))
    done = {"PAID": s.paid_at is not None, "BUY": s.approval in ("approved", "buy_only"), "DECL": s.approval == "declined",
            "CANCEL": s.approval != "auto"}
    if done.get(verb):
        return None
    try:
        if verb == "PAID":
            shipping.mark_paid(shipment_id, "manual")
            await shipping.after_paid(shipment_id)
        elif verb == "BUY":
            shipping.approve(shipment_id)
        elif verb == "DECL":
            shipping.decline(shipment_id)
        else:
            await shipping.cancel_auto(shipment_id)
    except shipping.ShippingError as e:
        await notify("shipping", t("Not possible"), str(e)[:200],
                     url=f"{get_settings().public_url.rstrip('/')}/chats/{s.conversation_id}#shipping",
                     data=shipping.event_data(shipping.get(shipment_id) or s))
        return f"error: {e}"
    return verb.lower()


async def _add_meetup(draft_id: int) -> str | None:
    from app.services import gcal, meetups
    from app.services.notify import clear_push, notify

    await clear_push(meetup_tag(draft_id))
    try:
        m = await meetups.add_from_draft(draft_id)
    except gcal.CalendarError as e:
        await notify("meetup", t("Calendar entry failed"), str(e)[:200])
        return f"error: {e}"
    if m is None:
        return None            # already added (second tap) or nothing to add
    await notify("meetup", t("In your calendar"), f"{meetups.format_when(m)} · {m['summary']}", url=m.get("event_link"),
                 tag=meetup_tag(draft_id))
    return "calendar"


async def _remove_meetup(draft_id: int) -> str | None:
    from app.services import gcal, meetups
    from app.services.notify import clear_push, notify

    await clear_push(meetup_tag(draft_id))
    try:
        m = await meetups.remove_from_draft(draft_id)
    except gcal.CalendarError as e:
        await notify("meetup", t("Calendar entry failed"), str(e)[:200])
        return f"error: {e}"
    if m is None:
        return None            # not in the calendar (second tap)
    await notify("meetup", t("Removed from calendar"), f"{meetups.format_when(m)} · {m['summary']}",
                 data={"draft_id": draft_id}, actions=meetup_actions(draft_id), tag=meetup_tag(draft_id))
    return "calendar removed"


async def listen_forever() -> None:
    """Keep a websocket subscription to HA push-button events; reconnect with backoff."""
    import websockets

    from app.services.ha_auth import ha_token, ha_url

    delay = 5
    listener["running"] = True
    while True:
        base, token = ha_url(), ha_token()
        if not (base and token):
            await asyncio.sleep(60)
            continue
        ws_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
        try:
            async with websockets.connect(ws_url, open_timeout=15, ping_interval=30) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    log.warning("HA push-button listener: auth failed")
                    listener.update(connected=False, error="auth failed")
                    await asyncio.sleep(300)
                    continue
                await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": EVENT}))
                log.info("HA push-button listener connected")
                listener.update(connected=True, error=None)
                delay = 5
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    data = (msg.get("event") or {}).get("data") or {}
                    try:
                        result = await handle(data)
                        if result:
                            log.info("push action %s: %s", str(data.get("action"))[:20], result)
                    except Exception:  # noqa: BLE001 - one bad event must not kill the listener
                        log.exception("push action failed")
        except asyncio.CancelledError:
            listener.update(running=False, connected=False)
            raise
        except Exception as e:  # noqa: BLE001 - network, HA restart, token revoked
            log.info("HA push-button listener disconnected: %s (retry in %ss)", e, delay)
            listener["error"] = str(e)[:200]
        listener["connected"] = False
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)
