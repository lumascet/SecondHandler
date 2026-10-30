"""The price steps of a Vinted chat, like the PayLivery steps of a willhaben chat: accept or decline the buyer's
offer, answer with a counter-offer, make an offer (as seller or as buyer), reserve the item for this buyer.

The calls are those of Vinted's own web app (read from its code 10-29, the offer statuses with them):
  PUT  /api/v2/transactions/<t>/offer_requests/<id>/accept     the buyer's offer accepted (no body)
  PUT  /api/v2/transactions/<t>/offer_requests/<id>/reject     declined
  POST /api/v2/transactions/<t>/offers          {offer: {price, currency}}           the seller's offer
  POST /api/v2/transactions/<t>/offer_requests  {offer_request: {price, currency}}   the buyer's offer
  PUT  /api/v2/transactions/<t>/reservation     {reserved: true|false}
  PUT  /api/v2/transactions/<t>/complete        "Alles in Ordnung": a bought item that arrived is accepted (10-30;
                                                 an order's transaction is its order id)
They run in a tab of the logged-in browser on the chat's page (platforms/vinted_listing.Upload: the page's own token),
then the chat is read again. Nothing runs without the owner's click; accepting asks first. Not yet tried against a
real offer – the first one is worth watching.
"""
from __future__ import annotations

import logging
from typing import Any

from app.db import Conversation, session
from app.i18n import t
from app.platforms import vinted_inbox as vi

log = logging.getLogger(__name__)

API = "https://www.vinted.at/api/v2"
STEPS = ("accept", "decline", "counter", "offer", "reserve", "unreserve", "complete")


class OfferError(RuntimeError):
    pass


def _eur(v: float | None) -> str:
    return f"{v:.2f} €".replace(".", ",") if v is not None else ""


def card(conv: Conversation | None) -> dict[str, Any] | None:
    """What the chat page shows of the price talk: {line, actions: [{name, label, input, confirm}], ask}; None for a
    chat that is not on Vinted or has no transaction yet. `ask`: Vinted's question that waits for the owner (the
    order arrived: all good, or a problem?) with its buttons – the one Vinted's page runs itself as a step, the
    others lead to the chat on Vinted. A bought or sold item (an order) has no price talk left."""
    if conv is None or conv.platform != "vinted":
        return None
    st = conv.vinted_offer or {}
    if not st.get("transaction_id"):
        return None
    ask = _ask(conv, st.get("ask"))
    if st.get("order_id"):
        return {"line": None, "actions": [], "open_request": False, "ask": ask, "order": True} if ask else None
    o, seller = st.get("offer") or {}, conv.role == "seller"
    open_request = o.get("kind") == "request" and o.get("status") == vi.OFFER_PENDING and not o.get("from_me")
    if o:
        line = t("Your latest offer: {price}", price=_eur(o.get("price"))) if o.get("from_me") else \
            t("Latest offer from {who}: {price}", who=conv.partner_name or t("the other side"), price=_eur(o.get("price")))
        if o.get("status_title"):
            line += f" · {o['status_title']}"
    else:
        line = t("No offer yet")
    if st.get("price") is not None:
        line += " · " + t("price now {price}", price=_eur(st["price"]))
    actions: list[dict[str, Any]] = []
    if seller and open_request:
        actions += [{"name": "accept", "label": t("Accept {price}", price=_eur(o.get("price"))),
                     "confirm": t("Accept the offer of {price}? The buyer can then buy at this price.",
                                  price=_eur(o.get("price")))},
                    {"name": "decline", "label": t("Decline")},
                    {"name": "counter", "label": t("Counter-offer"), "input": True}]
    elif seller:
        actions.append({"name": "offer", "label": t("Make an offer"), "input": True})
    else:
        actions.append({"name": "offer", "label": t("Make an offer"), "input": True})
    if seller:
        actions.append({"name": "reserve", "label": t("Reserve for them"),
                        "confirm": t("Reserve the item for this buyer on Vinted?")})
    return {"line": line, "actions": actions, "open_request": open_request, "ask": ask}


def _ask(conv: Conversation, ask: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ask:
        return None
    buttons = []
    for a in ask.get("actions") or []:
        if str(a.get("action")).startswith("all_is_good"):
            buttons.append({"name": "complete", "label": a.get("title") or t("All is good"), "primary": True,
                            "confirm": t("Confirm on Vinted that the order is all right? The money then goes to the "
                                         "seller; a problem cannot be reported afterwards.")})
        else:
            buttons.append({"label": a.get("title"), "url": f"{vi.ORIGIN}/inbox/{conv.id}"})
    return {"title": ask.get("title"), "text": ask.get("text"), "buttons": buttons}


def _price(amount: str | float | None) -> str:
    try:
        v = float(str(amount or "").replace(",", ".").replace("€", "").strip())
    except ValueError:
        raise OfferError(t("Enter an amount in euros")) from None
    if not 0 < v < 100000:
        raise OfferError(t("Enter an amount in euros"))
    return f"{v:.2f}"


def plan(conv: Conversation, step: str, amount: str | None = None) -> tuple[str, str, dict[str, Any] | None]:
    """(method, url, body) of a step for this chat; OfferError when it does not fit its state."""
    st = conv.vinted_offer or {}
    tx, o = st.get("transaction_id"), st.get("offer") or {}
    if step not in STEPS:
        raise OfferError(t("Unknown step"))
    if not tx:
        raise OfferError(t("This chat has no Vinted transaction yet"))
    base = f"{API}/transactions/{tx}"
    if step in ("accept", "decline"):
        if not (o.get("kind") == "request" and o.get("status") == vi.OFFER_PENDING and o.get("id")):
            raise OfferError(t("There is no open offer to answer"))
        return "PUT", f"{base}/offer_requests/{o['id']}/{'accept' if step == 'accept' else 'reject'}", None
    if step in ("counter", "offer"):
        price = _price(amount)
        if conv.role == "seller":
            return "POST", f"{base}/offers", {"offer": {"price": price, "currency": "EUR"}}
        return "POST", f"{base}/offer_requests", {"offer_request": {"price": price, "currency": "EUR"}}
    if step == "complete":
        if not any(str(a.get("action")).startswith("all_is_good") for a in (st.get("ask") or {}).get("actions") or []):
            raise OfferError(t("Vinted asks nothing to confirm here"))
        return "PUT", f"{base}/complete", None
    return "PUT", f"{base}/reservation", {"reserved": step == "reserve"}


async def run(conversation_id: str, step: str, amount: str | None = None) -> str:
    """Do the step on Vinted (from the chat's own page) and read the chat again; the result line, or OfferError."""
    from app.platforms import vinted_listing as vl

    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None or conv.platform != "vinted":
        raise OfferError(t("Not a Vinted chat"))
    method, url, body = plan(conv, step, amount)
    try:
        async with vl.Upload(url=f"{vi.ORIGIN}/inbox/{conversation_id}") as page:
            status, _, text = await page.call(method, url, body)
    except vl.ListingError as e:
        raise OfferError(str(e)) from None
    if status not in (200, 201, 204):
        raise OfferError(t("Vinted refused: {error}", error=vl.errors_of(text) or status))
    log.info("vinted offer step %s in %s: HTTP %s", step, conversation_id, status)
    try:
        await vi.sync_one(conversation_id, push=False)
    except Exception as e:  # noqa: BLE001 - the step is done; the chat comes with the next sync
        log.info("vinted chat %s not read again: %s", conversation_id, e)
    return {"accept": t("Offer accepted on Vinted"), "decline": t("Offer declined on Vinted"),
            "counter": t("Counter-offer sent on Vinted"), "offer": t("Offer sent on Vinted"),
            "reserve": t("Reserved on Vinted"), "unreserve": t("Reservation removed on Vinted"),
            "complete": t("Confirmed on Vinted: all is good")}[step]
