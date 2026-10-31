"""Settings → AI agent (docs/agent-api.md): the API token, the webhook with its signing secret and the event kinds it
gets, a test button and the delivery status. Every action answers with the re-rendered page of the section; htmx
swaps just the card it came from (settings/agent.html), without htmx the browser is sent back to the page."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth
from app.config import get_settings
from app.db import get_setting, set_setting
from app.i18n import t
from app.services import agent_events

log = logging.getLogger(__name__)
router = APIRouter()


def kind_labels() -> dict[str, str]:
    """Event kind -> label, in the order of agent_events.ALL_KINDS (the strings shared with Telegram's list are the same)."""
    labels = {
        "purchase": t("Purchases (negotiation, pay now)"),
        "sale": t("Sales of your own items"),
        "paylivery": t("PayLivery steps (offers, payment, delivery)"),
        "shipping": t("Shipping addresses and labels"),
        "message": t("New chat messages"),
        "auto_reply": t("Automatic answers sent"),
        "auto_reply_pending": t("Answers waiting for approval"),
        "meetup": t("Appointments for the calendar"),
        "hit": t("New search hits"),
        "price_drop": t("Price drops"),
        "saved": t("Saved ads: price drops, sold or reserved"),
        "ad_expiring": t("Own ads about to expire"),
        "ad_expired": t("Own ads expired"),
        "ad_renewed": t("Own ads renewed automatically"),
        "digest": t("Weekly digest"),
        "health": t("Connection problems"),
        "test": t("Test events"),
    }
    return {k: labels.get(k, k) for k in agent_events.ALL_KINDS}


def panel(test: dict[str, Any] | None = None) -> dict[str, Any]:
    """Template context of the section (settings/agent.html). `test`: the result of the last "Send test" button."""
    status = agent_events.webhook_status()
    return {"agent": {
        "token": get_setting(agent_events.SETTING_TOKEN),
        "url": get_setting(agent_events.SETTING_WEBHOOK_URL),
        "secret": get_setting(agent_events.SETTING_WEBHOOK_SECRET),
        "kinds": agent_events.kinds(),
        "kind_labels": kind_labels(),
        "status": status,
        "test": test,
        "base": get_settings().public_url.rstrip("/"),
        "password": auth.enabled(),        # without one the API needs no token at all (auth.login_needed)
    }}


def attention() -> bool:
    """The settings navigation flags the section: the agent channel is set up but there is no dashboard password (the
    API is open to anyone who reaches the app), or deliveries were given up (the webhook does not answer)."""
    try:
        in_use = get_setting(agent_events.SETTING_TOKEN) or get_setting(agent_events.SETTING_WEBHOOK_URL)
        return bool(in_use and not auth.enabled()) or bool(agent_events.webhook_status().get("failed"))
    except Exception:  # noqa: BLE001 - a flag never breaks the settings page
        return False


def _answer(request: Request, test: dict[str, Any] | None = None):
    """The section's page for htmx (the swap picks the card out of it), a redirect for a plain form post."""
    if not request.headers.get("hx-request"):
        return RedirectResponse("/settings/agent", status_code=303)
    from app.main import templates     # lazy: main imports this module
    return templates.TemplateResponse(request, "settings/agent.html", panel(test))


def _save_webhook(url: str, kinds: list[str]) -> None:
    """The webhook URL and the kinds it gets. The signing secret is created with the first URL."""
    url = url.strip()
    set_setting(agent_events.SETTING_WEBHOOK_URL, url)
    known = set(agent_events.ALL_KINDS)
    set_setting(agent_events.SETTING_KINDS, ",".join(k for k in agent_events.ALL_KINDS if k in kinds and k in known))
    if url and not get_setting(agent_events.SETTING_WEBHOOK_SECRET):
        set_setting(agent_events.SETTING_WEBHOOK_SECRET, agent_events.new_secret())


@router.post("/settings/agent/token", response_class=HTMLResponse)
async def agent_token_create(request: Request):
    """Create the API token, or replace it with a new one (the old one stops working at once)."""
    set_setting(agent_events.SETTING_TOKEN, agent_events.new_token())
    return _answer(request)


@router.post("/settings/agent/token/remove", response_class=HTMLResponse)
async def agent_token_remove(request: Request):
    set_setting(agent_events.SETTING_TOKEN, "")
    return _answer(request)


@router.post("/settings/agent/webhook", response_class=HTMLResponse)
async def agent_webhook_save(request: Request, url: str = Form(""), kind: list[str] = Form(default=[])):
    _save_webhook(url, kind)
    return _answer(request)


@router.post("/settings/agent/webhook/secret", response_class=HTMLResponse)
async def agent_secret_new(request: Request, url: str = Form(""), kind: list[str] = Form(default=[])):
    """A new signing secret (the receiver has to learn it); what is typed in the form is saved with it."""
    _save_webhook(url, kind)
    set_setting(agent_events.SETTING_WEBHOOK_SECRET, agent_events.new_secret())
    return _answer(request)


@router.post("/settings/agent/webhook/test", response_class=HTMLResponse)
async def agent_webhook_test(request: Request, url: str = Form(""), kind: list[str] = Form(default=[])):
    """Save the form, then send a signed test event to the URL and show the answer in the card."""
    _save_webhook(url, kind)
    try:
        result = await agent_events.send_test()
    except Exception as e:  # noqa: BLE001 - the card shows what went wrong
        log.warning("agent webhook test failed: %s", e)
        result = {"ok": False, "status": None, "error": str(e) or type(e).__name__}
    return _answer(request, result)
