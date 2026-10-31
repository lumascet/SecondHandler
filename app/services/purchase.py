"""Kaufauftrag: the buyer side of SecondHandler. The app starts a purchase for one ad, opens a chat with the
seller, negotiates as buyer (chats.auto_reply with the buyer prompt) and stops before any money moves.

Modes:
- paylivery: after the agreement the app sends a PayLivery price offer (paylivery.run_action "offer"). When the seller
  accepts (stage offer_accepted, or a buy-now ad at its asking price) the status is pay_now: the user pays with
  willhaben or with the "Buy now & pay" button in the PayLivery card (3-D Secure is approved on the phone).
- direct: after the agreement the own shipping address goes out with the next answer and the seller's bank details
  come from purchase_state.payment. Valid IBAN that the seller really wrote -> pay_now with a push; the user transfers
  and presses "Paid". Nothing is paid by the app.
- pickup: the appointment is agreed in the chat (meetup, calendar), cash on pickup, no address of ours, no payment.

Status flow: new -> contacting -> negotiating -> agreed -> pay_now -> paid -> shipped -> done (direct/PayLivery),
agreed -> meetup -> done (pickup). paused: asks the user (see pause reasons); failed / cancelled are final.

Code guards (blockers) run on every automatic answer, independent of what Claude wrote; pushes and status changes
happen only in after_reply and the hooks, never in the guards.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Coroutine

from sqlmodel import col, select

from app.config import get_settings
from app.db import Conversation, Draft, Message, Purchase, get_setting, session, utcnow
from app.i18n import t
from app.llm.base import get_backend
from app.llm.prompts import PURCHASE_OPENER_SCHEMA, PURCHASE_OPENER_SYSTEM, purchase_opener_prompt, purchase_rules
from app.platforms import fees
from app.services import deals, meetups, paylivery, shipping, topic
from app.services.notify import notify
from app.willhaben.account import OpenerUnconfirmed, SendFailed
from app.willhaben.public import WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable
from app.services import style, timeline

log = logging.getLogger(__name__)

MODES = ("paylivery", "direct", "pickup")
STAGES = ("negotiating", "agreed", "meetup_agreed", "declined", "unavailable", "needs_user")
STATUSES = ("new", "contacting", "negotiating", "agreed", "meetup", "pay_now", "paid", "shipped", "done",
            "paused", "failed", "cancelled")
ACTIVE = ("new", "contacting", "negotiating", "agreed", "meetup", "pay_now", "paid", "shipped")
AUTO = ("contacting", "negotiating", "agreed", "meetup", "pay_now", "paid", "shipped")   # answered automatically
OPEN = (*ACTIVE, "paused")          # one open purchase per ad; the chat card shows these
TERMINAL = ("done", "failed", "cancelled")
AFTER_AGREEMENT = ("agreed", "meetup", "pay_now", "paid", "shipped", "done")  # own address allowed (direct only)
PER_CONVERSATION_PER_DAY = 6        # own limit (a negotiation needs more than the 3 of a seller chat)
DEFAULT_DISCOUNT_PCT = 10           # suggested wish price (and the target without one): 10 % below the asking price
WISH_SLACK = 0.01                   # an opening offer may be 1 % (at least 1 €) below the wish price: Claude rounds
# Shipping in euro while nothing better is known (no PayLivery price of the ad, no price named by the seller)
SHIPPING_ESTIMATE = {"paylivery": 6.0, "direct": 6.0, "pickup": 0.0}   # € when the ad names no price (Post M is 7.90)
# The buyer protection fee of PayLivery (paid by the buyer): app/platforms/fees.py, by the ad's category; the rule
# is stored with the order at its start (Purchase.fee).
SETTING_ENABLED = "purchase_enabled"      # "1": purchase orders allowed (Settings)
SETTING_RULES = "purchase_rules"          # free-text rules of the user for purchases
SETTING_DISCOUNT = "purchase_discount_pct"
SETTING_PREFER_PAYLIVERY = "purchase_prefer_paylivery"   # "1": PayLivery preselected in the buy dialog
OVER = 1.5                          # asking price above 1.5 x the maximum: still started, with a note
LOG_CAP = 50
MAX_QUESTIONS = 8                   # the user's questions for the seller (buy dialog, last step)
QUESTION_LEN = 300
ANSWER_LEN = 500
WH_CHAT = "https://www.willhaben.at/iad/myprofile/chat/{cid}"
DEFAULT_RULES = (
    "- Nur kaufen, wenn der Preis passt. Keine Vorkasse an Dritte.\n"
    "- Du-Form, freundlich, kurz, keine Emojis.\n"
    "- Bei Abholung nur Termine in meinem Zeitfenster vorschlagen."
)
_TASKS: set[asyncio.Task] = set()


class PurchaseError(RuntimeError):
    """A user-facing refusal of a purchase action."""


# ------------------------------------------------------------------ numbers, IBAN, text guards
_NUMBER = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+,\d{1,2}|\d+\.\d{1,2}|\d+"
_CURRENCY = r"(?:€|\beur(?:o|os)?\b)"
# "€ 120", "120 €", "EUR 120", "120 Euro", and the dash forms "120,-" / "120.-" / "120,–"
_AMOUNT = re.compile(rf"{_CURRENCY}\s*({_NUMBER})|(?<![\w.,])({_NUMBER})(?:\s*{_CURRENCY}|\s?[,.][-–](?![\d-]))", re.I)
# an amount right after these words is a total (incl. shipping and fee): compared with the maximum itself
_TOTAL_BEFORE = re.compile(r"(?:inkl\.?|inklusive|samt|mit\s+versand|gesamt|insgesamt|zusammen|total)\W*(?:\w+\W+){0,3}$", re.I)
_IBAN_TEXT = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,8}\b", re.I)
_CARD_TEXT = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}\d(?!\d)")
# Words of the agent's instructions or AI self-talk (topic.INTERNAL) plus the purchase's own: never to the seller
_INTERNAL_TEXT = re.compile(rf"\b(?:höchstpreis\w*|obergrenze\w*|regelblock\w*|{topic.INTERNAL})", re.I)


def _to_float(num: str) -> float:
    if "," in num:                                   # 1.234,50 or 12,34
        return float(num.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", num):   # 1.234
        return float(num.replace(".", ""))
    return float(num)


def amounts(text: str | None) -> list[float]:
    """Every euro amount in a text: "€ 12,50", "12,50 €", "EUR 300", "120 Euro", "120,-"."""
    return [a for a, _ in _amounts_in(text)]


def _amounts_in(text: str | None) -> list[tuple[float, bool]]:
    """(amount, is_total) for every euro amount; is_total: "inkl. Versand 125 €", "insgesamt 125 €"."""
    text, out = text or "", []
    for m in _AMOUNT.finditer(text):
        try:
            value = _to_float(m.group(1) or m.group(2))
        except ValueError:
            continue
        before, after = text[max(0, m.start() - 40):m.start()], text[m.end():m.end() + 30]
        out.append((value, bool(_TOTAL_BEFORE.search(before)
                                or re.match(r"\W*(?:inkl|inklusive|samt|mit\s+versand|gesamt)", after, re.I))))
    return out


def normalize_iban(s: str | None) -> str:
    return re.sub(r"[\s-]", "", s or "").upper()


def valid_iban(s: str | None) -> bool:
    """ISO 13616 check: shape plus mod-97 checksum (spaces and case ignored)."""
    iban = normalize_iban(s)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return False
    digits = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


def group_iban(s: str | None) -> str:
    iban = normalize_iban(s)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().replace("ß", "ss")).strip()


def sender_address() -> dict[str, str]:
    return shipping.sender()


def _address_line(s: dict[str, str]) -> str:
    street = " ".join(x for x in (s.get("street"), s.get("house_no")) if x)
    city = " ".join(x for x in (s.get("zip"), s.get("city")) if x)
    parts = [s.get("name"), street, s.get("extra"), city, s.get("phone"), s.get("email")]
    return ", ".join(p for p in parts if p)


def _address_hits(text: str) -> list[str]:
    """Own address, phone or e-mail that our own message would give away."""
    s = sender_address()
    t_, digits, hits = _norm(text), re.sub(r"\D", "", text or ""), []
    if len(_norm(s.get("street"))) >= 4 and _norm(s["street"]) in t_:
        hits.append("street")
    if s.get("zip") and s.get("city") and _norm(s["zip"]) in t_ and _norm(s["city"]) in t_:
        hits.append("city")
    phone = re.sub(r"\D", "", s.get("phone") or "")
    if len(phone) >= 7 and phone[-7:] in digits:
        hits.append("phone")
    if s.get("email") and _norm(s["email"]) in t_:
        hits.append("email")
    return hits


def text_problems(text: str | None, *, max_price: float | None, address_ok: bool,
                  item_max: float | None = None) -> list[str]:
    """Reasons why one of our own messages must not go out (empty list = fine). An amount is an item price unless
    the words around it make it a total: an item price may reach `item_max` (what the maximum leaves after shipping and
    the fee), a total the maximum itself."""
    why = []
    if max_price:
        for a, is_total in _amounts_in(text):
            cap = max_price if is_total or item_max is None else min(item_max, max_price)
            if a > cap + 0.005:
                why.append(t("Amount {amount} is above your maximum {max}", amount=_money(a), max=_money(cap)))
                break
    if _IBAN_TEXT.search(text or ""):
        why.append(t("Bank details (IBAN) in our own message"))
    if _CARD_TEXT.search(text or ""):
        why.append(t("Card or account number in our own message"))
    if not address_ok and _address_hits(text or ""):
        why.append(t("Own address or contact details in a message before they are allowed"))
    why += topic.internal_problem(text, _INTERNAL_TEXT)
    return why


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _money(v: float | None) -> str:
    if v is None:
        return ""
    s = f"{v:,.2f}".translate(str.maketrans({",": ".", ".": ","}))
    return (s[:-3] if s.endswith(",00") else s) + " €"


def _discount() -> float:
    try:
        return min(max(float(get_setting(SETTING_DISCOUNT) or DEFAULT_DISCOUNT_PCT), 0.0), 90.0)
    except ValueError:
        return float(DEFAULT_DISCOUNT_PCT)


def shipping_of(row: Purchase) -> tuple[float, str]:
    """(cost, source) of the shipping that applies: "ad" (PayLivery price of the ad), "chat" (named by the seller,
    direct only), "estimate" or "none" (pickup)."""
    if row.mode == "pickup":
        return 0.0, "none"
    if row.shipping_cost is not None:
        if row.shipping_source == "ad" and row.mode == "paylivery":
            return float(row.shipping_cost), "ad"
        if row.shipping_source == "chat" and row.mode == "direct":
            return float(row.shipping_cost), "chat"
    return float(SHIPPING_ESTIMATE.get(row.mode or "", 0.0)), "estimate"


def shipping_label(source: str | None) -> str:
    return {"ad": t("according to the ad"), "chat": t("named by the seller"), "estimate": t("estimate"),
            "none": t("no shipping")}.get(source or "", "")


def _cents(v: float) -> int:
    return int(round(float(v) * 100))


def rule_of(row: Purchase | None = None) -> fees.FeeRule:
    """The fee rule of this order: the one stored at its start (the ad's category, app/platforms/fees.py), else
    willhaben's general PayLivery rule."""
    return fees.from_dict(getattr(row, "fee", None), "willhaben") or fees.RULES["willhaben"]


async def stated_fee(ad_id: str, price: float) -> float | None:
    """willhaben's fee for the ad at this price, read from its PayLivery window (app/willhaben/paylivery_fee.py,
    read-only); None when it could not be read."""
    from app.willhaben import paylivery_fee
    from app.willhaben.session import get_session
    try:
        return (await paylivery_fee.fetch(get_session(), ad_id, price))["fee"]
    except Exception as e:  # noqa: BLE001 - without it the category's rule applies
        log.info("PayLivery fee of %s not read: %s", ad_id, e)
        return None


def paylivery_fee(item_price: float | None, rule: fees.FeeRule | None = None) -> float:
    """Buyer protection fee of PayLivery for an item price in euro (0 for a price that is not positive)."""
    return (rule or rule_of()).fee(item_price)


def fee_rule(rule: fees.FeeRule | None = None) -> dict[str, Any]:
    """The fee rule for the browser (data-fee of the buy dialog) and the prompts."""
    return (rule or rule_of()).as_dict()


def fee_of(row: Purchase, item_price: float | None) -> float:
    """The buyer protection fee that applies to this order: PayLivery only (a bank transfer or pickup has none)."""
    return rule_of(row).fee(item_price) if row.mode == "paylivery" else 0.0


def costs_of(row: Purchase, item_price: float) -> dict[str, Any]:
    """What an item price costs in this order: shipping (and where it comes from), the buyer protection fee and the
    total the buyer pays."""
    ship, source = shipping_of(row)
    fee = fee_of(row, item_price)
    return {"shipping": ship, "shipping_source": source, "fee": fee, "total": round(float(item_price) + ship + fee, 2)}


def item_for_total(row: Purchase, total: float) -> float:
    """The item price a total incl. shipping (and, with PayLivery, the buyer protection fee) leaves: total minus the
    shipping that applies and the fee of the item price (item + fee(item) <= total - shipping). Never below 1 euro."""
    ship = shipping_of(row)[0]
    if row.mode == "paylivery":
        return max(1.0, rule_of(row).item_for_cents(_cents(total) - _cents(ship)) / 100)
    return max(1.0, round(total - ship, 2))


def item_ceiling(row: Purchase) -> float | None:
    """The most the item may cost: the item price the maximum leaves (item_for_total)."""
    return item_for_total(row, row.max_price) if row.max_price else None


def pct_below(asking: float | None, value: float | None) -> int | None:
    """Percent the value lies below the asking price (negative if above); None without both numbers."""
    if not asking or value is None:
        return None
    return round((asking - value) / asking * 100)


def _in_text(value: float, text: str) -> bool:
    """A euro amount written the German or the English way appears in the text (4,90 / 4.90 / 5)."""
    forms = {f"{value:g}", f"{value:.2f}"}
    forms |= {f.replace(".", ",") for f in forms}
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?!\d|[.,]\d)", text or "") for f in forms)


def _named_shipping(row: Purchase, ps: dict[str, Any], seller_text: str) -> float | None:
    """Shipping the seller named in this answer: only without PayLivery, and only if the amount is in his text."""
    cost = ps.get("shipping_cost")
    if row.mode != "direct" or not cost:
        return None
    return float(cost) if _in_text(float(cost), seller_text) else None


def _shipping_now(row: Purchase, ps: dict[str, Any], seller_text: str) -> tuple[float, str]:
    named = _named_shipping(row, ps, seller_text)
    return (named, "chat") if named is not None else shipping_of(row)


def _total_now(row: Purchase, ps: dict[str, Any], seller_text: str, price: float | None = None) -> float | None:
    """What the buyer pays for this answer: the total the seller named, else item price + shipping (+ the PayLivery buyer
    protection fee of the item price)."""
    if ps.get("total_price"):
        return float(ps["total_price"])
    price = price if price is not None else ps.get("agreed_price")
    if not price:
        return None
    return round(float(price) + _shipping_now(row, ps, seller_text)[0] + fee_of(row, float(price)), 2)


def _price_line(row: Purchase) -> str:
    """Item + shipping (+ buyer protection with PayLivery) = total, where shipping applies (pickup: just the cash amount).
    With PayLivery every part comes from costs_of; otherwise the total is the agreed total when there is one, so the
    shipping is what it adds to the item price."""
    item = row.agreed_price or row.asking_price
    cost, src = shipping_of(row)
    if item is None:
        return ""
    if src == "none":
        return _money(item)
    if row.mode == "paylivery":
        c = costs_of(row, item)
        return t("Item {item} + shipping {shipping} ({source}) + buyer protection {fee} = {total}", item=_money(item),
                 shipping=_money(c["shipping"]), source=shipping_label(src), fee=_money(c["fee"]), total=_money(c["total"]))
    total = row.agreed_total if row.agreed_total is not None and row.agreed_price is not None else item + cost
    shipping = max(0.0, total - item)
    return t("Item {item} + shipping {shipping} ({source}) = {total}", item=_money(item), shipping=_money(shipping),
             source=shipping_label(src), total=_money(item + shipping))


def target_price(row: Purchase) -> float | None:
    """The first offer for the item: the item price of the user's wish price (incl. shipping and fee, as the maximum),
    without one the discount below the asking price. Never above the asking price or the item ceiling. Comparable ads
    play no part: a keyword search finds accessories or other products often enough."""
    if row.wish_price:
        target = item_for_total(row, row.wish_price)
        target = float(math.floor(target)) if target >= 10 else target     # a chat names whole euros: 568, not 568.42
    elif row.asking_price:
        target = row.asking_price * (1 - _discount() / 100)
        target = float(math.floor(target)) if target >= 1 else round(target, 2)
    else:
        return None
    if row.asking_price:
        target = min(target, row.asking_price)
    ceiling = item_ceiling(row)
    if ceiling:
        target = min(target, ceiling)
    return target


def clean_state(raw: Any) -> dict[str, Any]:
    """Claude's purchase_state, cleaned: known stage, positive numbers, sentinels turned into None."""
    raw = raw if isinstance(raw, dict) else {}

    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

    pay = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    payment = {"iban": str(pay.get("iban") or "").strip()[:40], "holder": str(pay.get("holder") or "").strip()[:100],
               "amount": num(pay.get("amount")), "reference": str(pay.get("reference") or "").strip()[:140]}
    pk = raw.get("pickup") if isinstance(raw.get("pickup"), dict) else {}
    pickup = None
    if str(pk.get("start") or "").strip():
        pickup = {"start": str(pk["start"]).strip()[:16], "minutes": int(pk.get("minutes") or 30),
                  "location": str(pk.get("location") or "").strip()[:200]}
    answers = []
    for a in raw.get("answers") if isinstance(raw.get("answers"), list) else []:
        if isinstance(a, dict) and str(a.get("question") or "").strip() and str(a.get("answer") or "").strip():
            answers.append({"question": " ".join(str(a["question"]).split())[:QUESTION_LEN],   # one line each: no
                            "answer": " ".join(str(a["answer"]).split())[:ANSWER_LEN]})       # injected rule lines
    return {"stage": raw.get("stage") if raw.get("stage") in STAGES else "negotiating",
            "agreed_price": num(raw.get("agreed_price")),
            "shipping_cost": num(raw.get("shipping_cost")),
            "total_price": num(raw.get("total_price")),
            "payment": payment if any(payment.values()) else None,
            "pickup": pickup,
            "answers": answers[:MAX_QUESTIONS],
            "reason": str(raw.get("reason") or "").strip()[:300]}


# ------------------------------------------------------------------ the user's questions for the seller
def clean_questions(raw: str | list[str] | None) -> list[str]:
    """The questions of the buy dialog: one per line (or a list), trimmed, without duplicates, at most MAX_QUESTIONS."""
    items = raw.splitlines() if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for q in items:
        q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", str(q or "")).strip()[:QUESTION_LEN]
        if q and _norm(q) not in {_norm(x) for x in out}:
            out.append(q)
    return out[:MAX_QUESTIONS]


def _q_key(s: str | None) -> str:
    return re.sub(r"[^\w]+", " ", _norm(s)).strip()


def _news(old: str | None, new: str | None) -> bool:
    """A first answer, a correction or a fuller answer – not a reworded or shortened copy of the stored one."""
    o, n = _q_key(old), _q_key(new)
    if not n or o == n:
        return False
    if not o:
        return True
    return n not in o and difflib.SequenceMatcher(None, o, n).ratio() < 0.5


def merge_answers(row: Purchase, answers: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Claude's answers (purchase_state.answers) onto the row's questions, matched by the question text. Returns the
    questions that got a new or changed answer; questions Claude made up are ignored."""
    qs = [dict(q) for q in (row.questions or [])]
    changed = []
    for a in answers or []:
        key = _q_key(a.get("question"))
        q = next((q for q in qs if _q_key(q.get("question")) == key), None)
        if q is None or not key or not _news(q.get("answer"), a["answer"]):
            continue
        q["answer"] = a["answer"]
        changed.append(q)
    if changed:
        row.questions = qs
    return changed


def questions_text(questions: list[dict[str, Any]] | None) -> str:
    """Questions and answers as push text: one block per question, open ones marked."""
    return "\n".join(t("Q: {question}\nA: {answer}", question=q.get("question"),
                       answer=q.get("answer") or t("(no answer yet)")) for q in questions or [])


async def notify_agreement(row: Purchase) -> None:
    """The agreement push with the answers to the user's questions, once per purchase (the first agreement or pickup
    appointment). Without questions there is nothing to send: the mode's own push says what follows."""
    if not row.questions or row.questions_sent_at is not None:
        return
    row = _change(row.id, lambda r: setattr(r, "questions_sent_at", utcnow()))
    await _push(row, t("The seller agreed"), t("{title}, {price}", title=row.ad_title or row.ad_id, price=_price_line(row))
                + "\n\n" + questions_text(row.questions))


async def _apply_answers(row: Purchase, answers: list[dict[str, str]] | None) -> Purchase:
    """Store new answers; an answer that comes after the agreement push is sent on by itself."""
    changed: list[dict[str, str]] = []

    def apply(r: Purchase) -> None:
        changed.extend(merge_answers(r, answers))
        if changed:
            _line(r, t("Seller answered: {questions}", questions="; ".join(q["question"] for q in changed)[:200]))

    row = _change(row.id, apply) or row
    if changed and row.questions_sent_at is not None:
        await _push(row, t("Answer from the seller"), questions_text(changed))
    return row


# ------------------------------------------------------------------ rows
def get(pid: int) -> Purchase | None:
    with session() as db:
        return db.get(Purchase, pid)


def recent(limit: int = 20) -> list[Purchase]:
    with session() as db:
        return list(db.exec(select(Purchase).order_by(col(Purchase.updated_at).desc(), col(Purchase.id).desc())
                            .limit(limit)))


def active() -> list[Purchase]:
    with session() as db:
        return list(db.exec(select(Purchase).where(col(Purchase.status).in_(OPEN))
                            .order_by(col(Purchase.updated_at).desc())))


def for_conversation(conversation_id: str) -> Purchase | None:
    """The open purchase of this chat (active or paused), newest first; None if there is none."""
    with session() as db:
        return db.exec(select(Purchase).where(Purchase.conversation_id == conversation_id,
                                              col(Purchase.status).in_(OPEN))
                       .order_by(col(Purchase.updated_at).desc())).first()


def _latest_for_conversation(conversation_id: str) -> Purchase | None:
    with session() as db:
        return db.exec(select(Purchase).where(Purchase.conversation_id == conversation_id)
                       .order_by(col(Purchase.updated_at).desc())).first()


def auto_mode(conv: Conversation) -> bool:
    """Automatic answers for this buyer chat: an open purchase whose status is answered automatically."""
    p = for_conversation(conv.id)
    return p is not None and p.status in AUTO


def _change(pid: int, fn) -> Purchase | None:
    with session() as db:
        row = db.get(Purchase, pid)
        if row is None:
            return None
        prev = row.status
        fn(row)
        row.updated_at = utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)
    if row.status != prev:
        _status_event(row, prev)
    return row


def _status_event(row: Purchase, prev: str) -> None:
    """Every status change as a quiet event for the AI agent channel (stage "status", docs/agent-api.md): the feed
    and the webhook see paid / shipped / done too, which push nothing to the phone."""
    from app.services.notify import notify_later

    notify_later("purchase", t("Purchase: {status}", status=status_label(row.status)), row.ad_title or row.ad_id,
                 url=_chat_url(row), data=event_data(row, "status", **{"from": prev}), push=False)


def _move(row: Purchase, status: str, text: str = "", note: Any = ...) -> None:
    """Set the status and append to the log ("from" = the status before, used by resume)."""
    prev = row.status
    row.status = status
    if note is not ...:
        row.note = note
    entry = {"at": utcnow().isoformat(), "status": status, "text": (text or "")[:300], "from": prev}
    row.log = (list(row.log or []) + [entry])[-LOG_CAP:]


def _line(row: Purchase, text: str) -> None:
    """Log line without a status change."""
    row.log = (list(row.log or []) + [{"at": utcnow().isoformat(), "status": row.status, "text": text[:300]}])[-LOG_CAP:]


def _base() -> str:
    return get_settings().public_url.rstrip("/")


def _chat_url(row: Purchase) -> str:
    return f"{_base()}/chats/{row.conversation_id}#purchase" if row.conversation_id else f"{_base()}/purchases/{row.id}"


# stage of a purchase event (docs/agent-api.md) by the order's status, where the push does not name one itself
STAGE_OF_STATUS = {"pay_now": "pay_now", "done": "bought", "failed": "failed", "cancelled": "cancelled"}


def category_text(path: Any) -> str | None:
    """The site's category path as one readable string ("Elektronik > Audio"), None when unknown."""
    text = " > ".join(str(c) for c in path if c) if isinstance(path, (list, tuple)) else str(path or "").strip()
    return text or None


def event_data(row: Purchase, stage: str | None = None, **extra: Any) -> dict[str, Any]:
    """The structured fields of a purchase notification (docs/agent-api.md, data of kind "purchase"): the order, the
    ad, the prices in euro (shipping, fee and total as the order's own sums, costs_of) and the stage. Keys that are not
    known are left out. `extra` wins over the computed values."""
    from app.services.auto_buy import BUY_CHAT_PREFIX     # lazy: auto_buy imports this module
    item = row.agreed_price or row.asking_price
    costs = costs_of(row, item) if item else {}
    cid = row.conversation_id if not (row.conversation_id or "").startswith(BUY_CHAT_PREFIX) else None   # hidden placeholder chat
    total = row.agreed_total if row.agreed_total is not None and row.agreed_price is not None else costs.get("total")
    data: dict[str, Any] = {
        "stage": stage or STAGE_OF_STATUS.get(row.status, "update"), "purchase_id": row.id, "status": row.status,
        "conversation_id": cid, "ad_id": row.ad_id, "platform": "willhaben", "ad_title": row.ad_title,
        "ad_url": row.ad_url, "category": row.category, "mode": row.mode, "asking_price": row.asking_price,
        "max_price": row.max_price, "agreed_price": row.agreed_price, "shipping": costs.get("shipping"),
        "fee": costs.get("fee"), "total": round(float(total), 2) if total is not None else None,
        "agent_id": row.agent_id}
    data.update(extra)
    return {k: v for k, v in data.items() if v is not None}


async def _push(row: Purchase, title: str, message: str, stage: str | None = None) -> None:
    try:
        await notify("purchase", title, message, url=_chat_url(row), data=event_data(row, stage))
    except Exception as e:  # noqa: BLE001 - a failed push never breaks the purchase
        log.warning("purchase push failed: %s", e)


def _spawn(coro: Coroutine[Any, Any, Any]) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ------------------------------------------------------------------ start
def start(ad_id: str, mode: str, max_price: float | None = None, *, search_id: int | None = None,
          query: str | None = None, title: str | None = None, url: str | None = None,
          image_url: str | None = None, price: float | None = None, pickup_window: str | None = None,
          questions: str | list[str] | None = None, wish_price: float | None = None) -> int:
    """Store a purchase order and start it in the background. Raises PurchaseError on a refusal.

    `wish_price`: the price the user wants to pay, on the basis of the maximum (incl. shipping and fee). The agent
    offers it first and may go up to the maximum; None = the discount below the asking price.
    `questions`: the user's questions for the seller (text, one per line, or a list). The agent asks them in the chat
    and the answers go to the user with the agreement push (notify_agreement)."""
    if get_setting(SETTING_ENABLED) != "1":
        raise PurchaseError(t("Purchase orders are switched off. Turn them on in Settings."))
    ad_id = (ad_id or "").strip()
    if not ad_id:
        raise PurchaseError(t("No ad given."))
    if mode not in MODES:
        raise PurchaseError(t("Unknown purchase mode."))
    if max_price is not None and max_price <= 0:
        raise PurchaseError(t("Enter the maximum price in euros."))
    if wish_price is not None and wish_price <= 0:
        raise PurchaseError(t("Enter the wish price in euros."))
    if wish_price is not None and max_price is not None and wish_price > max_price + 0.005:
        raise PurchaseError(t("The wish price is above your maximum."))
    if mode == "direct" and not shipping.sender_complete():
        raise PurchaseError(t("Fill in your own shipping address first (Settings → Shipping)."))
    with session() as db:
        dup = db.exec(select(Purchase).where(Purchase.ad_id == ad_id, col(Purchase.status).in_(OPEN))).first()
        if dup is not None:
            raise PurchaseError(t("There is already an open purchase for this ad (#{id}).", id=dup.id))
        row = Purchase(ad_id=ad_id, ad_title=title, ad_url=url, image_url=image_url, asking_price=price,
                       max_price=max_price, mode=mode, status="new",   # None: asking price + shipping, set in _begin
                       wish_price=wish_price,
                       search_id=search_id, query=query,
                       pickup_window=((pickup_window or "").strip()[:200] or None) if mode == "pickup" else None,
                       questions=[{"question": q, "answer": ""} for q in clean_questions(questions)] or None,
                       log=[])
        _line(row, t("Purchase started ({mode})", mode=mode_label(mode)))
        db.add(row)
        db.commit()
        db.refresh(row)
        pid = row.id
    _queue_start(pid, title or ad_id)
    return pid


def _queue_start(pid: int, title: str) -> None:
    """The start (read the ad, Claude's opener, the first message to the seller) as a background job (lane chat)
    with a grace period: the click can be taken back (take_back) before anything reaches the seller. The purchase
    card waits on "purchase:<id>" meanwhile and refreshes itself."""
    from app.services import jobs

    begin = _begin                    # bound now (tests swap it out around start())

    async def run(job) -> str:
        await begin(pid)
        row = get(pid)
        return status_label(row.status) if row is not None else ""

    jobs.submit("purchase-start", t("Purchase „{title}“", title=title[:60]), run, lane="chat", current=f"purchase:{pid}",
                on_cancel=lambda: take_back(pid), grace=True, link=f"/purchases/{pid}")


def take_back(pid: int) -> None:
    """The start was taken back while it waited in the queue: nothing was sent, the order is cancelled."""
    row = get(pid)
    if row is None or row.status != "new":
        return
    _change(pid, lambda r: _move(r, "cancelled", t("Taken back before the first message")))


def pending_start(pid: int) -> bool:
    """The order's start still waits in the queue or runs right now (the card shows it)."""
    from app.services import jobs
    return f"purchase:{pid}" in jobs.current_keys("purchase:")


async def _fail(pid: int, reason: str) -> None:
    row = _change(pid, lambda r: _move(r, "failed", reason, note=reason))
    if row is not None:
        await _push(row, t("Purchase failed"), reason)


async def _begin(pid: int) -> None:
    """Contact the seller: read the ad, price check, opening message by Claude, the chat itself."""
    try:
        await _begin_inner(pid)
    except Exception as e:  # noqa: BLE001 - a background task must leave a readable state behind
        log.exception("purchase %s failed to start", pid)
        await _fail(pid, t("Could not start the purchase: {error}", error=e))


@timeline.scoped(purchase_id="pid")
async def _begin_inner(pid: int) -> None:
    row = get(pid)
    if row is None:
        return
    timeline.update_context(ad_id=row.ad_id, search_id=row.search_id)
    try:
        detail = await get_public().get_detail(row.ad_id)
    except WillhabenError as e:
        return await _fail(pid, t("Could not read the ad: {error}", error=e))
    if detail is None:
        return await _fail(pid, t("The ad is no longer online."))
    if row.mode == "paylivery" and not detail.paylivery_enabled:
        return await _fail(pid, t("This ad has no willhaben PayLivery."))
    median, market = None, None
    try:
        res = await deals.analyze(detail)
        stats = (res.get("comparables") or {}).get("stats") or {}
        if stats.get("median") and (stats.get("count") or 0) >= deals.MIN_COMPARABLES:
            median = float(stats["median"])
            market = {"median": median, "min": _num(stats.get("min")), "p25": _num(stats.get("p25")),
                      "count": int(stats["count"])}
    except Exception as e:  # noqa: BLE001 - the price check is a bonus, the purchase goes on without it
        log.info("price check for %s failed: %s", row.ad_id, e)
    ad_ship = detail.shipping_price if row.mode == "paylivery" else None     # PayLivery: the buyer's delivery price
    rule = fees.rule("willhaben", detail.category_path) or rule_of()      # the fee rule of the ad's category
    if row.mode == "paylivery" and detail.price:   # willhaben's exact fee at the asking price (its PayLivery window)
        stated = await stated_fee(row.ad_id, detail.price)
        if stated is not None:
            rule = rule.stated(detail.price, stated)
            row = _change(pid, lambda r: _line(r, t("Buyer protection fee from willhaben: {fee} at the asking price",
                                                    fee=_money(stated))))
        else:
            row = _change(pid, lambda r: _line(r, t("willhaben did not show the buyer protection fee – it is estimated "
                                                    "from the category")))
    default_max, ship, fee = None, 0.0, 0.0
    if not row.max_price and detail.price:        # no maximum typed: the asking price plus shipping (and PayLivery's fee)
        ship = ad_ship if ad_ship is not None else SHIPPING_ESTIMATE.get(row.mode or "", 0.0)
        fee = rule.fee(detail.price) if row.mode == "paylivery" else 0.0
        default_max = round(detail.price + ship + fee, 2)
    max_price = row.max_price or default_max
    far = bool(detail.price and max_price and detail.price > max_price * OVER)
    info = t("The asking price is far above your maximum – the agent starts anyway.") if far else None
    if default_max and fee:
        info = t("Maximum set to {max}: asking price plus shipping {ship} and buyer protection {fee}",
                 max=_money(default_max), ship=_money(ship), fee=_money(fee))
    elif default_max:
        info = t("Maximum set to {max}: asking price plus shipping {ship}", max=_money(default_max), ship=_money(ship))
    row = _change(pid, lambda r: (setattr(r, "ad_title", detail.heading), setattr(r, "ad_url", detail.url),
                                  setattr(r, "image_url", detail.images[0] if detail.images else r.image_url),
                                  setattr(r, "asking_price", detail.price), setattr(r, "median_price", median),
                                  setattr(r, "max_price", max_price), setattr(r, "market", market),
                                  setattr(r, "shipping_cost", ad_ship),
                                  setattr(r, "shipping_source", "ad" if ad_ship is not None else None),
                                  setattr(r, "fee", rule.as_dict()), setattr(r, "category", category_text(detail.category_path)),
                                  info and _line(r, info)))
    target = target_price(row)
    cost, src = shipping_of(row)
    refused: tuple[str, list[str]] | None = None
    for _attempt in range(2):      # a refused opener gets one retry with the reason, then the order fails
        prompt = purchase_opener_prompt(ad_context=detail.as_context(), target=target, max_price=row.max_price,
                                        mode=row.mode, rules=get_setting(SETTING_RULES) or DEFAULT_RULES,
                                        seller_name=detail.seller_name, shipping_cost=cost, shipping_source=src,
                                        item_ceiling=item_ceiling(row),
                                        fee=fee_rule(rule_of(row)) if row.mode == "paylivery" else None,
                                        questions=[q["question"] for q in row.questions or []],
                                        asking=detail.price, refused=refused,
                                        style=style.profile() or None, examples=style.examples())
        out = await get_backend("purchase").complete(system=PURCHASE_OPENER_SYSTEM, prompt=prompt,
                                                 schema=PURCHASE_OPENER_SCHEMA)
        text = str((out.data or {}).get("message") or "").strip()
        if not text:
            return await _fail(pid, t("Claude wrote no opening message."))
        problems = text_problems(text, max_price=row.max_price, address_ok=False, item_max=item_ceiling(row))
        offer = _num((out.data or {}).get("opening_offer"))   # direct: incl. shipping, so never below the item target
        if offer and target and offer < target - max(1.0, target * WISH_SLACK):
            problems.append(t("Opening offer {amount} is below your wish price {wish}", amount=_money(offer),
                              wish=_money(target)))
        if not problems:
            break
        log.info("purchase %s: opening message refused (%s): %s", pid, "; ".join(problems), text)
        refused = (text, problems)
    else:
        return await _fail(pid, t("The opening message was refused: {why}. Claude's text: {text}",
                                  why="; ".join(problems), text=text[:300]))
    from app.services import chats  # lazy: chats imports this module

    # stored before the POST: after a restart in between, recover() looks for the chat instead of writing again
    _change(pid, lambda r: (setattr(r, "opener_text", text), setattr(r, "opener_sent_at", utcnow())))
    try:
        cid = await chats._account().create_conversation(row.ad_id, text)
    except (SendFailed, NotLoggedIn, SessionUnavailable, ValueError) as e:   # refused: nothing went out
        _change(pid, lambda r: (setattr(r, "opener_text", None), setattr(r, "opener_sent_at", None)))
        return await _fail(pid, t("willhaben did not open the chat: {error}", error=e))
    except OpenerUnconfirmed:     # the message went out, the chat is not visible yet: never send a second opener
        return await _pause_unconfirmed(pid, t("The opening message was sent, but the chat did not show up yet – check on willhaben. Resume looks for the chat again; it does not write to the seller a second time."))  # noqa: E501
    except Exception as e:  # noqa: BLE001 - broken off while sending: it may have gone out, so never send it again
        log.warning("opener of purchase %s broke off: %s", pid, e)
        return await _pause_unconfirmed(pid, t("Sending the opening message broke off ({error}) – it may have reached the seller. Resume looks for the chat; it does not write to the seller a second time.", error=e))  # noqa: E501
    await _attach_chat(pid, cid, text, heading=detail.heading, seller=detail.seller_name, context=detail.as_context())


_LOOKING: set[int] = set()     # purchases whose unconfirmed opener's chat is being looked for (one lookup each)


def _unconfirmed_opener(row: Purchase) -> str | None:
    """The text of an opener that went out (or may have) without a chat to follow, or None."""
    return row.opener_text if row.conversation_id is None else None


async def _pause_unconfirmed(pid: int, reason: str, *, push: bool = True) -> None:
    row = _change(pid, lambda r: _move(r, "paused", reason, note=reason) if r.status != "paused"
                  else (setattr(r, "note", reason), _line(r, reason)))
    if row is not None and push:
        await _push(row, t("Purchase paused"), reason)


async def _attach_chat(pid: int, cid: str, text: str, *, heading: str | None, seller: str | None,
                       context: dict[str, Any] | None) -> None:
    """The opener is in a chat: store the conversation, move on to contacting, sync the history. A purchase that was
    cancelled meanwhile only gets its chat noted; it stays cancelled."""
    from app.services import chats  # lazy: chats imports this module

    with session() as db:
        conv = db.get(Conversation, cid) or Conversation(id=cid)
        row = db.get(Purchase, pid)
        conv.ad_id, conv.ad_title, conv.role = row.ad_id if row else conv.ad_id, heading or conv.ad_title, "buyer"
        conv.partner_name = conv.partner_name or seller
        if context:
            conv.ad_context, conv.ad_context_at = context, utcnow()
        db.add(conv)
        ext_id = f"draft-purchase-{pid}"
        if db.exec(select(Message).where(Message.conversation_id == cid, Message.ext_id == ext_id)).first() is None:
            # stamped with the send time, so the sync can match it with willhaben's copy (SENT_MATCH_WINDOW)
            db.add(Message(conversation_id=cid, ext_id=ext_id, direction="out", text=text,
                           created_at=(row.opener_sent_at if row else None) or utcnow()))
        db.commit()
    _change(pid, lambda r: (setattr(r, "conversation_id", cid),
                            r.status in ("new", "paused")
                            and _move(r, "contacting", t("Opening message sent: {text}", text=text[:200]), note=None)))
    try:
        await chats.sync_messages(cid)
        await chats.ensure_ad_context(cid)
    except Exception as e:  # noqa: BLE001 - the chat exists; the history follows at the next sync
        log.info("first sync of purchase chat %s failed: %s", cid, e)


def _look_for_chat(pid: int, text: str) -> None:
    """Start one lookup for the chat of an unconfirmed opener (resume / start again / after a restart)."""
    if pid in _LOOKING:
        raise PurchaseError(t("Already looking for the chat – wait a moment."))
    _LOOKING.add(pid)
    _spawn(_find_unconfirmed_chat(pid, text))


async def _find_unconfirmed_chat(pid: int, text: str) -> None:
    """Look for the chat of an unconfirmed opener; never send the opener again."""
    from app.services import chats  # lazy: chats imports this module

    try:
        row = get(pid)
        if row is None:
            return
        try:
            cid = await chats._account().find_conversation(row.ad_id, text)
        except Exception as e:  # noqa: BLE001 - stays paused, the user can try again
            cid = None
            log.info("looking for the chat of purchase %s failed: %s", pid, e)
        row = get(pid)
        if row is None or row.conversation_id is not None:
            return
        if cid:
            await _attach_chat(pid, cid, text, heading=row.ad_title, seller=None, context=None)
        elif row.status == "paused":     # cancelled meanwhile: stays cancelled
            await _pause_unconfirmed(pid, t("The chat of the sent opening message is still not visible – check on willhaben, then resume again."),  # noqa: E501
                                     push=False)
    finally:
        _LOOKING.discard(pid)


# ------------------------------------------------------------------ prompts, guards
def rules_block(p: Purchase, conv: Conversation | None = None, availability: str | None = None,
                user_rules: str | None = None) -> str:
    """Prompt block of one purchase: prices, mode, what may be disclosed, the user's rules (`user_rules`: other
    rules for a test run in the chat simulator)."""
    sender = None
    if p.mode == "direct" and p.status in AFTER_AGREEMENT:
        s = sender_address()
        sender = _address_line(s) if shipping.sender_complete() and s else None
    seller = None
    if conv is not None:
        seller = conv.partner_name or (conv.ad_context or {}).get("seller_name")
    cost, src = shipping_of(p)
    return purchase_rules(asking=p.asking_price, target=target_price(p), max_price=p.max_price,
                          mode=p.mode, sender_address=sender, stage=p.status,
                          user_rules=user_rules if user_rules is not None else (get_setting(SETTING_RULES) or DEFAULT_RULES),
                          note=p.note,
                          pickup_window=p.pickup_window, availability=availability, seller_name=seller,
                          shipping_cost=cost, shipping_source=src, item_ceiling=item_ceiling(p),
                          fee=fee_rule(rule_of(p)) if p.mode == "paylivery" else None, questions=p.questions)


def blockers(p: Purchase, d: Draft, msgs: list[Message]) -> list[str]:
    """Hard checks in code for one buyer draft (pure: no status change, no push)."""
    ps = d.purchase or {}
    why = []
    if ps.get("stage") == "needs_user":
        why.append(t("Claude needs your decision: {reason}", reason=ps.get("reason") or "-"))
    why += text_problems(d.text, max_price=p.max_price, address_ok=p.mode == "direct" and p.status in AFTER_AGREEMENT,
                         item_max=item_ceiling(p))
    why += topic.too_long(d.text)                     # the instruction words are in text_problems already
    if p.max_price:
        # PayLivery: the offer is the item price, the buyer pays shipping and buyer protection on willhaben (the ceiling
        # is the maximum minus both)
        cap = (item_ceiling(p) or p.max_price) if p.mode == "paylivery" else p.max_price
        if d.suggested_price and d.suggested_price > cap:
            why.append(t("Offer {amount} is above your maximum {max}", amount=_money(d.suggested_price), max=_money(cap))
                       if cap == p.max_price else
                       t("Offer {amount} is above the item ceiling {max} (maximum minus shipping and buyer protection)",
                         amount=_money(d.suggested_price), max=_money(cap)))
        total = _total_now(p, ps, _seller_text_of(msgs))
        if total and total > p.max_price + 0.005:
            if p.mode == "paylivery":
                why.append(t("Agreed price incl. shipping and buyer protection {amount} is above your maximum {max}",
                             amount=_money(total), max=_money(p.max_price)))
            else:
                why.append(t("Agreed price incl. shipping {amount} is above your maximum {max}", amount=_money(total),
                             max=_money(p.max_price)))
    return why


def _seller_text_of(msgs: list[Message]) -> str:
    return " ".join(m.text or "" for m in msgs if m.direction == "in")


async def after_reply(p: Purchase, d: Draft, sent: bool) -> None:
    """Apply Claude's purchase_state of one draft: status changes, pushes, the PayLivery offer. Called after a send
    (sent=True) and after a blocked draft (sent=False: only pauses and refusals are acted on)."""
    row = get(p.id)
    if row is None or row.status in TERMINAL:
        return
    ps = d.purchase or {}
    stage = ps.get("stage")
    reason = (ps.get("reason") or "").strip()
    if d.intent == "scam_suspect" or stage == "needs_user":
        return await pause(row.id, reason or t("Claude asked for a decision"), push=True)
    if ps.get("answers") and row.questions:
        row = await _apply_answers(row, ps.get("answers"))
    if not sent:
        return
    if stage in ("unavailable", "declined"):
        return await _fail(row.id, reason or t("The seller ended the purchase."))
    seller = _seller_text(row.conversation_id)
    named = _named_shipping(row, ps, seller)
    if named is not None and (row.shipping_source != "chat" or row.shipping_cost != named):
        row = _change(row.id, lambda r: (setattr(r, "shipping_cost", named), setattr(r, "shipping_source", "chat"),
                                         _line(r, t("Shipping named by the seller: {amount}", amount=_money(named)))))
    total = _total_now(row, ps, seller)
    if total and row.max_price and total > row.max_price + 0.005:
        if row.mode == "paylivery":
            return await pause(row.id, t("Agreed price incl. shipping and buyer protection is above your maximum."), push=True)
        return await pause(row.id, t("Agreed price incl. shipping is above your maximum."), push=True)
    prev = row.status
    if stage == "negotiating" and prev == "contacting":
        row = _change(row.id, lambda r: _move(r, "negotiating", reason))
    if stage == "agreed" and prev in ("contacting", "negotiating"):
        price = ps.get("agreed_price") or row.agreed_price
        total = _total_now(row, ps, seller, price=price)
        row = _change(row.id, lambda r: (setattr(r, "agreed_price", price), setattr(r, "agreed_total", total),
                                         _move(r, "agreed", reason)))
        await notify_agreement(row)
        if row.mode == "paylivery":
            await _paylivery_next(row, prev)
        elif row.mode == "direct":
            await _direct_payment(row, ps.get("payment"))
    elif stage == "agreed" and row.status == "agreed" and row.mode == "direct":
        await _direct_payment(row, ps.get("payment"))
    if stage == "meetup_agreed" and row.mode == "pickup" and row.status in ("contacting", "negotiating", "agreed"):
        m = meetups.clean(ps.get("pickup"))
        if m:
            await _meetup_agreed(row, m, reason)


async def _paylivery_next(row: Purchase, prev: str) -> None:
    """After an agreement with PayLivery: buy-now at the asking price, else the PayLivery offer in the background."""
    if prev == "agreed":
        return
    amount = row.agreed_price
    with session() as db:
        conv = db.get(Conversation, row.conversation_id) if row.conversation_id else None
    buy_now = bool((conv.ad_context or {}).get("buy_now")) if conv else False
    if amount and row.max_price:                  # item + shipping + buyer protection must fit the maximum
        total = costs_of(row, amount)["total"]
        if total > row.max_price + 0.005:
            return await pause(row.id, t("Agreed price incl. shipping and buyer protection is above your maximum."), push=True)
    if buy_now and amount and row.asking_price and abs(amount - row.asking_price) < 0.005:
        row = _change(row.id, lambda r: _move(r, "pay_now", t("Buy-now price agreed")))
        await _push_paylivery_pay(row)
        return
    _spawn(_send_offer(row.id, row.conversation_id, amount))


async def _send_offer(pid: int, conversation_id: str | None, amount: float | None) -> None:
    if not conversation_id or not amount:
        return
    try:
        result = await paylivery.run_action(conversation_id, "offer", amount)
    except Exception as e:  # noqa: BLE001 - the offer failed: the user decides
        return await pause(pid, t("PayLivery offer failed: {error}", error=e), push=True)
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    stage = ((conv.paylivery or {}) if conv else {}).get("stage")
    if stage not in ("offer_sent", "offer_accepted"):
        await pause(pid, t("PayLivery offer not sent: {text}", text=result[:200]), push=True)


async def _direct_payment(row: Purchase, payment: dict[str, Any] | None) -> None:
    """Bank details from the seller's answer: pay_now only for a valid IBAN that the seller really wrote."""
    if row.status != "agreed" or not payment:
        return
    raw = payment.get("iban") or ""
    if not raw:
        return
    seller_text = normalize_iban(_seller_text(row.conversation_id))
    if not valid_iban(raw) or normalize_iban(raw) not in seller_text:
        await _push(row, t("Check IBAN"), t("The seller wrote: {text}", text=f"{raw} · {payment.get('holder') or '-'}"))
        return
    amount = payment.get("amount") or row.agreed_total or row.agreed_price
    if amount is None:
        return
    if row.agreed_total and amount > row.agreed_total + 0.005:
        return await pause(row.id, t("The amount {amount} is above the agreed total {total}",
                                     amount=_money(amount), total=_money(row.agreed_total)), push=True)
    if row.max_price and amount > row.max_price + 0.005:
        return await pause(row.id, t("The amount {amount} is above your maximum {max}",
                                     amount=_money(amount), max=_money(row.max_price)), push=True)
    data = {"iban": normalize_iban(raw), "holder": payment.get("holder") or "",
            "amount": amount, "reference": (payment.get("reference") or "")
            or f"{row.ad_title or ''} willhaben"}
    row = _change(row.id, lambda r: (setattr(r, "payment", data),        # agreed_price stays the item price
                                     _move(r, "pay_now", t("Bank details received"))))
    await _push_direct_pay(row)


def _seller_text(conversation_id: str | None) -> str:
    if not conversation_id:
        return ""
    with session() as db:
        rows = db.exec(select(Message).where(Message.conversation_id == conversation_id,
                                             Message.direction == "in")).all()
    return " ".join(m.text or "" for m in rows)


async def _meetup_agreed(row: Purchase, m: dict[str, Any], reason: str) -> None:
    row = _change(row.id, lambda r: (setattr(r, "pickup", m), _move(r, "meetup", reason)))
    await notify_agreement(row)
    when = meetups.format_when(m)
    where = m.get("location") or "-"
    await _push(row, t("Pickup appointment"),
                t("{when}, {where}, {price} cash", when=when, where=where, price=_price_line(row)))


async def _push_paylivery_pay(row: Purchase) -> None:
    amount = _price_line(row)
    await _push(row, t("Pay now!"),
                t("{title}, {amount}. Pay in the willhaben app ({link}) or with Buy now & pay in the PayLivery card "
                  "here – the app fills in your saved card, you approve 3-D Secure on your phone, then it continues by itself.",
                  title=row.ad_title or row.ad_id, amount=amount, link=WH_CHAT.format(cid=row.conversation_id)))


async def _push_direct_pay(row: Purchase) -> None:
    p = row.payment or {}
    text = t("Recipient: {holder}\nIBAN: {iban}\nAmount: {amount}\nReference: {reference}",
             holder=p.get("holder") or "-", iban=group_iban(p.get("iban")), amount=_money(p.get("amount")),
             reference=p.get("reference") or "-")
    if shipping_of(row)[1] != "none":
        text += "\n" + t("Price: {price}", price=_price_line(row))
    await _push(row, t("Pay now!"), text)


# ------------------------------------------------------------------ pauses, failures and user buttons
async def pause(pid: int, note: str | None = None, *, push: bool = True) -> None:
    """Ask the user: the purchase waits until it is resumed (the previous status is kept in the log)."""
    # a finished, cancelled or failed order stays so (a buy step that ended after the user cancelled it)
    row = _change(pid, lambda r: (_move(r, "paused", note or "", note=note or r.note)
                                  if r.status not in ("paused", "paid", "shipped", "received", "cancelled", "failed") else None))
    if row is not None and push:
        await _push(row, t("Purchase paused"), note or t("Your decision is needed."))


def card(conv: Conversation | None) -> dict[str, Any] | None:
    """Chat card of a purchase (template context of _purchase.html); None hides it. Shows the newest purchase of the
    chat, also a finished or failed one."""
    if conv is None:
        return None
    row = _latest_for_conversation(conv.id)
    return card_for(row) if row is not None else None


def card_for(row: Purchase, log_limit: int | None = 8) -> dict[str, Any]:
    """The same card for one purchase row (order detail page); log_limit None keeps the whole log."""
    payment = row.payment or {}
    entries = list(row.log or [])
    if log_limit:
        entries = entries[-log_limit:]
    ship_cost, ship_source = shipping_of(row)
    item = row.agreed_price or row.asking_price
    costs = costs_of(row, item) if item else None
    if costs is not None and row.mode != "paylivery" and row.agreed_total is not None and row.agreed_price is not None:
        total = row.agreed_total                    # without PayLivery the seller may have named the total
    else:
        total = costs["total"] if costs is not None else None
    return {
        "id": row.id, "ad_title": row.ad_title, "status": row.status, "status_label": status_label(row.status),
        "mode": row.mode, "mode_label": mode_label(row.mode), "asking": row.asking_price, "median": row.median_price,
        "target": target_price(row), "wish_price": row.wish_price, "max_price": row.max_price, "agreed": row.agreed_price,
        "payment": ({**payment, "iban_group": group_iban(payment.get("iban"))} if payment else None),
        "pickup": row.pickup, "pickup_window": row.pickup_window, "note": row.note,
        "questions": [dict(q) for q in row.questions or []],
        "shipping_cost": ship_cost, "shipping_source": ship_source,
        "shipping_label": shipping_label(ship_source), "agreed_total": row.agreed_total,
        "fee": costs["fee"] if costs is not None else 0.0, "total": total,
        "item_ceiling": item_ceiling(row), "market": dict(row.market) if row.market else None,
        "pct_below": pct_below(row.asking_price, row.max_price),
        "log": list(reversed(entries)), "next": next_text(row), "pay_now": row.status == "pay_now",
        "wh_url": WH_CHAT.format(cid=row.conversation_id) if row.conversation_id else None,
        "actions": actions(row), "pending": pending_start(row.id),
    }


def actions(row: Purchase) -> list[dict[str, Any]]:
    s, out = row.status, []
    if s == "paused" and row.id not in _LOOKING:
        out.append({"name": "resume", "label": t("Resume")})
    if s in ("contacting", "negotiating", "agreed", "meetup", "pay_now"):
        out.append({"name": "pause", "label": t("Pause")})
    from app.services import paylivery
    paying = s == "pay_now" and row.conversation_id in paylivery._busy      # the buy runs in the browser right now
    if s in ("contacting", "negotiating", "agreed", "meetup", "pay_now", "paused") and not paying:
        out.append({"name": "cancel", "label": t("Cancel purchase"), "confirm": True})
    if s in ("contacting", "negotiating", "agreed", "pay_now", "paused"):
        out.append({"name": "max_price", "label": t("Maximum price"), "input": True})
    if row.mode == "direct" and s == "pay_now":
        out.append({"name": "paid", "label": t("Paid"), "confirm": True})
    if s in ("paid", "shipped"):
        out.append({"name": "received", "label": t("Received"), "confirm": True})
    if row.mode == "pickup" and s == "meetup":
        out.append({"name": "collected", "label": t("Collected & paid"), "confirm": True})
    if s == "failed" and not is_auto_buy(row):
        out.append({"name": "restart", "label": t("Try again")})      # an auto-buy is never restarted as a negotiation
    return out


def is_auto_buy(row: Purchase) -> bool:
    """Started by a search agent's auto-buy: no seller talks, buy-now only (also an order from before agent_id)."""
    return row.agent_id is not None or (row.conversation_id or "").startswith("autobuy-")


def set_max_price(pid: int, value: float) -> None:
    if not value or value <= 0:
        raise PurchaseError(t("Enter the maximum price in euros."))
    row = get(pid)
    if row is None or row.status not in OPEN:
        raise PurchaseError(t("This purchase is not open."))
    _change(pid, lambda r: (setattr(r, "max_price", float(value)), _line(r, t("Maximum set to {max}", max=_money(value)))))


async def resume(pid: int) -> None:
    row = get(pid)
    if row is None or row.status != "paused":
        raise PurchaseError(t("This purchase is not paused."))
    back = next((e.get("from") for e in reversed(row.log or []) if e.get("status") == "paused" and e.get("from")), None)
    if row.conversation_id is None:
        sent = _unconfirmed_opener(row)
        if sent:                           # the opener already went out: find its chat, never send it again
            _look_for_chat(pid, sent)
            _change(pid, lambda r: _line(r, t("Looking for the chat of the sent opening message")))
            return
        row = _change(pid, lambda r: _move(r, "new", t("Resumed")))
        _queue_start(pid, row.ad_title or row.ad_id)      # the opener again waits its grace period
        return
    row = _change(pid, lambda r: _move(r, back if back in OPEN and back != "paused" else "negotiating", t("Resumed"), note=None))


async def restart(pid: int) -> None:
    row = get(pid)
    if row is None or row.status != "failed":
        raise PurchaseError(t("Only a failed purchase can be started again."))
    if row.conversation_id is None:
        sent = _unconfirmed_opener(row)
        if sent:                           # the opener went out (or may have): find its chat, never send it again
            _look_for_chat(pid, sent)
            _change(pid, lambda r: _move(r, "paused", t("Looking for the chat of the sent opening message")))
            return
        _change(pid, lambda r: _move(r, "new", t("Started again"), note=None))
        _queue_start(pid, row.ad_title or row.ad_id)      # the opener again waits its grace period
    else:
        _change(pid, lambda r: _move(r, "negotiating", t("Started again"), note=None))


def cancel(pid: int) -> None:
    row = get(pid)
    if row is None or row.status not in OPEN:
        raise PurchaseError(t("This purchase is not open."))
    _change(pid, lambda r: _move(r, "cancelled", t("Cancelled by you")))


def mark_received(pid: int) -> None:
    row = get(pid)
    if row is None or row.status not in ("paid", "shipped"):
        raise PurchaseError(t("Only after payment can the receipt be confirmed."))
    _change(pid, lambda r: _move(r, "done", t("Received")))


def mark_collected(pid: int) -> None:
    row = get(pid)
    if row is None or row.status != "meetup" or row.mode != "pickup":
        raise PurchaseError(t("Only a confirmed pickup can be marked as collected."))
    _change(pid, lambda r: _move(r, "done", t("Collected and paid in cash")))


def mark_paid(pid: int) -> None:
    """The user transferred the money (direct mode): status paid, and the seller is told in the chat."""
    row = get(pid)
    if row is None or row.status != "pay_now" or row.mode != "direct":
        raise PurchaseError(t("Nothing to pay for this purchase right now."))
    _change(pid, lambda r: _move(r, "paid", t("Transfer reported as done by you")))
    _spawn(_tell_seller_paid(pid))


async def _tell_seller_paid(pid: int) -> None:
    from app.services import chats  # lazy: chats imports this module

    row = get(pid)
    if row is None or not row.conversation_id:
        return
    p = row.payment or {}
    text = (f"Hallo, ich habe {_money(p.get('amount'))} überwiesen"
            + (f" (Verwendungszweck: {p['reference']})" if p.get("reference") else "")
            + ". Gib mir bitte kurz Bescheid, wenn der Betrag angekommen ist. Danke!")
    with session() as db:
        draft = Draft(conversation_id=row.conversation_id, text=text, status="draft")
        db.add(draft)
        db.commit()
        db.refresh(draft)
    try:
        await chats.send_draft(draft.id, text)
    except Exception as e:  # noqa: BLE001 - the payment is recorded; the message can be sent by hand
        log.warning("paid message for purchase %s failed: %s", pid, e)
        failed = t("The message to the seller failed: {error}", error=e)
        _change(pid, lambda r: _line(r, failed))


# ------------------------------------------------------------------ purchases made in a chat (no order)
# A buyer chat of willhaben whose PayLivery state says the deal is on, without a Purchase row (the user bought on
# willhaben directly): the AI agent interface lists it next to the orders (/api/purchases, source "chat") and gets a
# quiet purchase event per stage. Without PayLivery (cash, bank transfer) there is no reliable "bought" signal.
CHAT_DEAL_STAGES = ("offer_accepted", "paid", "released", "label_ready", "shipped", "delivered", "received")
CHAT_AFTER_DEAL = ("problem", "cancelled")                   # count once a deal stage was reached before
CHAT_OPEN_STAGES = ("offer_accepted", "paid", "released", "label_ready", "shipped", "problem")   # not yet delivered
CHAT_EVENT_RECENT = paylivery.PUSH_RECENT                    # older stages (a first sync) are listed, not announced


def chat_purchase_stage(state: dict[str, Any] | None) -> str | None:
    """The PayLivery stage of a buyer chat that counts as a purchase made there, else None: the offer was accepted,
    paid, released, label ready, shipped, delivered or confirmed (CHAT_DEAL_STAGES); a reported problem or a
    cancellation only when such a stage was reached before (the history)."""
    st = state or {}
    if st.get("role") not in (None, "buyer"):
        return None
    stage = st.get("stage")
    if stage in CHAT_DEAL_STAGES:
        return stage
    if stage in CHAT_AFTER_DEAL and any(h.get("stage") in CHAT_DEAL_STAGES for h in st.get("history") or []):
        return stage
    return None


def orders_of_chats(db: Any) -> tuple[set[str], set[str]]:
    """(conversation ids that have a Purchase row of any status, ad ids of open orders still on auto-buy's
    placeholder chat: willhaben's real chat of such an ad is theirs, see _take_over_auto_buy)."""
    from app.services.auto_buy import BUY_CHAT_PREFIX
    rows = db.exec(select(Purchase.conversation_id, Purchase.ad_id, Purchase.status)).all()
    chats = {cid for cid, _ad, _st in rows if cid}
    ads = {ad for cid, ad, st in rows if ad and st in OPEN and (cid or "").startswith(BUY_CHAT_PREFIX)}
    return chats, ads


def chat_has_order(conv: Conversation, chats: set[str], ads: set[str]) -> bool:
    return conv.id in chats or bool(conv.ad_id and conv.ad_id in ads)


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def chat_facts(conv: Conversation, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """What is known about a purchase made in a chat, from the chat row alone (no network): the ad (title, link, first
    picture, category path from the stored ad context) and the prices in euro. agreed_price is the accepted offer, else
    the amount of the PayLivery notice; shipping is the PayLivery delivery price of the ad as stored in the ad context
    (with shipping_source "ad"); fee is the buyer protection fee of that price by the category's rule; total is the sum
    when the price and the shipping are known. Unknown values are None."""
    st = state if state is not None else (conv.paylivery or {})
    ctx = conv.ad_context or {}
    category = category_text(ctx.get("category"))
    price = _number(st.get("offer")) or _number(st.get("amount"))
    shipping = _number(ctx.get("shipping_price"))
    rule = fees.rule("willhaben", category.split(" > ") if category else None)
    fee = round(rule.fee(price), 2) if rule and price else None
    images = ctx.get("images") or []
    return {
        "ad_title": conv.ad_title or ctx.get("title"), "ad_url": ctx.get("url") or (
            f"https://www.willhaben.at/iad/object?adId={conv.ad_id}" if conv.ad_id else None),
        "image_url": images[0] if images else None, "category": category, "agreed_price": price,
        "shipping": shipping, "shipping_source": "ad" if shipping is not None else None, "fee": fee,
        "total": round(price + shipping + fee, 2) if price and shipping is not None and fee is not None else None}


def _stage_before(state: dict[str, Any], stage: str) -> str | None:
    """The stage the chat had before `stage` (newest history entry with another stage), None when unknown."""
    return next((h["stage"] for h in reversed(state.get("history") or []) if h.get("stage") not in (None, stage)), None)


def _stage_recent(state: dict[str, Any], stage: str) -> bool:
    at = next((h.get("at") for h in reversed(state.get("history") or []) if h.get("stage") == stage), None)
    try:
        when = datetime.fromisoformat(at) if at else None
    except ValueError:
        when = None
    return when is None or (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) >= utcnow() - CHAT_EVENT_RECENT


def _chat_purchase_event(conv: Conversation, stage: str, state: dict[str, Any]) -> None:
    """A quiet purchase event (stage "status", source "chat") for a stage change of a buyer chat that has no order: the
    agent sees purchases made on willhaben directly. Only a real, recent stage change of a willhaben buyer chat counts
    (never a recheck's correction, never a chat with a Purchase row: its order sends the events)."""
    from app.services.notify import notify_later

    state = {**state, "role": state.get("role") or conv.role}
    if (conv.platform or "willhaben") != "willhaben" or conv.role != "buyer" or state.get("corrected_from"):
        return
    if chat_purchase_stage(state) != stage or not _stage_recent(state, stage):
        return
    with session() as db:
        chats, ads = orders_of_chats(db)
    if chat_has_order(conv, chats, ads):
        return
    facts = chat_facts(conv, state)
    data = {"stage": "status", "source": "chat", "status": stage, "from": _stage_before(state, stage),
            "conversation_id": conv.id, "ad_id": conv.ad_id, "platform": "willhaben", "ad_title": facts["ad_title"],
            "ad_url": facts["ad_url"], "category": facts["category"], "agreed_price": facts["agreed_price"],
            "shipping": facts["shipping"], "total": facts["total"]}
    notify_later("purchase", t("Purchase: {status}", status=paylivery.stage_label(stage)),
                 facts["ad_title"] or conv.ad_id or "", url=f"{_base()}/chats/{conv.id}",
                 data={k: v for k, v in data.items() if v is not None}, push=False)


# ------------------------------------------------------------------ hooks: PayLivery (not registered here)
async def on_paylivery_stage(conv: Conversation, stage: str, state: dict[str, Any]) -> None:
    """PayLivery stage of a buyer chat changed (registered by app/main.py)."""
    row = for_conversation(conv.id) or _take_over_auto_buy(conv)
    if row is None:
        try:                                     # no order of ours: a purchase made on willhaben directly
            _chat_purchase_event(conv, stage, state)
        except Exception as e:  # noqa: BLE001 - the agent channel never breaks the stage hooks
            log.warning("chat purchase event for %s failed: %s", conv.id, e)
        return
    if state.get("corrected_from"):              # a recheck lowered the stage
        row = _paylivery_corrected(row, stage)
        if state.get("seen"):                    # handled when the chat had it before: nothing to catch up
            return
    if stage == "offer_accepted":
        amount = state.get("offer") or row.agreed_price or row.asking_price
        if row.status in ("contacting", "negotiating", "agreed", "meetup"):
            row = _change(row.id, lambda r: (setattr(r, "agreed_price", amount),
                                             setattr(r, "agreed_total", costs_of(r, amount)["total"] if amount else r.agreed_total),
                                             _move(r, "pay_now", t("The seller accepted the PayLivery offer"))))
            await notify_agreement(row)
            await _push_paylivery_pay(row)
    elif stage == "counter_received":              # the seller answered with an amount of his own: the owner decides
        amount = state.get("offer")
        if amount and row.status in OPEN:
            fits = not row.max_price or costs_of(row, float(amount))["total"] <= row.max_price + 0.005
            await _push(row, t("Counter offer {amount}", amount=_money(float(amount))),
                        t("The seller made a PayLivery counter offer. It fits your maximum – accept it in the chat.")
                        if fits else t("The seller made a PayLivery counter offer above your maximum."))
    elif stage == "offer_declined":
        if row.status == "agreed":
            _change(row.id, lambda r: _move(r, "negotiating", t("The seller declined the PayLivery offer"),
                                            note=t("The seller declined our offer – negotiate further.")))
    elif stage in ("paid", "released"):            # released: the seller accepted the paid purchase
        if row.status in ("agreed", "pay_now"):
            _change(row.id, lambda r: _move(r, "paid", t("Paid with PayLivery")))
    elif stage == "shipped":
        if row.status == "paid":
            _change(row.id, lambda r: _move(r, "shipped", t("Shipped")))
    elif stage == "delivered":
        if row.status in ("paid", "shipped"):
            await _push(row, t("Confirm receipt?"), t("The parcel was delivered – confirm the receipt in the chat."))
    elif stage == "received":
        if row.status in ("paid", "shipped"):
            _change(row.id, lambda r: _move(r, "done", t("Receipt confirmed")))
    elif stage in ("cancelled", "problem"):
        if row.status in OPEN and row.status != "paused":
            await pause(row.id, t("PayLivery: {stage}", stage=stage), push=True)


def _take_over_auto_buy(conv: Conversation) -> Purchase | None:
    """willhaben's real chat of an ad bought by auto-buy (its order sat on a placeholder chat, auto_buy.py) takes the
    order over: shipping, delivery and receipt follow there."""
    from app.services.auto_buy import BUY_CHAT_PREFIX
    if not conv.ad_id or (conv.id or "").startswith(BUY_CHAT_PREFIX):
        return None
    with session() as db:
        row = db.exec(select(Purchase).where(Purchase.ad_id == conv.ad_id, col(Purchase.status).in_(OPEN),
                                             col(Purchase.conversation_id).startswith(BUY_CHAT_PREFIX))).first()
    if row is None:
        return None
    return _change(row.id, lambda r: setattr(r, "conversation_id", conv.id))


def _paylivery_corrected(row: Purchase, stage: str) -> Purchase:
    """A recheck lowered the chat's PayLivery stage that older notice rules had misread (paylivery.recheck). An open
    order the misread moved to shipped goes back to paid while the parcel is not out. A finished order is not reopened
    (the hooks only see open orders)."""
    if row.status == "shipped" and stage in ("paid", "released", "label_ready"):
        text = t("PayLivery stage corrected: {stage}", stage=paylivery.stage_label(stage))
        return _change(row.id, lambda r: _move(r, "paid", text)) or row
    return row


async def on_paylivery_action(conv: Conversation, action: str, ok: bool, text: str) -> None:
    """A PayLivery browser action of this chat ended (registered by app/main.py). The 3-D Secure wait and its push
    live in paylivery itself; here a failed offer or purchase gets a note on the order and a push, so the user can
    finish it on willhaben or restart it."""
    row = for_conversation(conv.id)
    if row is None or action not in ("offer", "buy") or ok:
        return
    what = t("PayLivery offer") if action == "offer" else t("PayLivery purchase")
    note = t("{what} did not finish: {text}", what=what, text=(text or "").strip()[:300])
    row = _change(row.id, lambda r: setattr(r, "note", note))
    if row is not None:
        await _push(row, t("Purchase needs you"), note)


# ------------------------------------------------------------------ startup
def recover() -> None:
    """After a restart no purchase task is running any more: unfinished starts are marked failed, or paused when the
    opener may already have gone out (Resume then looks for its chat instead of writing again)."""
    moved: list[tuple[Purchase, str]] = []
    with session() as db:
        for row in db.exec(select(Purchase).where(col(Purchase.status).in_(("new", "contacting")),
                                                  Purchase.conversation_id.is_(None))):
            moved.append((row, row.status))
            if row.opener_text:
                note = t("Interrupted by a restart while the opening message was sent – Resume looks for the chat; it does not write to the seller a second time.")  # noqa: E501
                _move(row, "paused", note, note=note)
            else:
                _move(row, "failed", t("Interrupted by a restart."), note=t("Interrupted by a restart."))
            row.updated_at = utcnow()
            db.add(row)
        db.commit()
        for row, prev in moved:
            db.refresh(row)
            _status_event(row, prev)


# ------------------------------------------------------------------ texts
def status_label(status: str | None) -> str:
    return {
        "new": t("Starting"), "contacting": t("Contacting the seller"), "negotiating": t("Negotiating"),
        "agreed": t("Price agreed"), "meetup": t("Pickup agreed"), "pay_now": t("Pay now"), "paid": t("Paid"),
        "shipped": t("Shipped"), "done": t("Done"), "paused": t("Paused"), "failed": t("Failed"),
        "cancelled": t("Cancelled"),
    }.get(status or "", status or "")


def mode_label(mode: str | None) -> str:
    return {"paylivery": t("With PayLivery"), "direct": t("Bank transfer"), "pickup": t("Pickup (cash)")}.get(mode or "", mode or "")


def next_text(row: Purchase) -> str:
    s, m = row.status, row.mode
    if s == "new":
        return t("The agent opens the chat with the seller.")
    if s == "contacting":
        return t("The agent waits for the seller's answer and negotiates.")
    if s == "negotiating":
        return t("The agent negotiates within your maximum price.")
    if s == "agreed":
        if m == "paylivery":
            return t("The PayLivery offer goes to the seller; the price is set once the seller accepts.")
        if m == "pickup":
            return t("The agent agrees a pickup time within your window.")
        return t("Your address goes to the seller with the next answer; the agent waits for the bank details.")
    if s == "meetup":
        return t("Pick up the item at the agreed time and pay cash, then press Collected & paid.")
    if s == "pay_now":
        if m == "paylivery":
            return t("Pay in the PayLivery card below (or in the willhaben app).")
        return t("Transfer the amount to the account below, then press Paid.")
    if s == "paid":
        return t("Paid – waiting for the item; confirm the receipt afterwards.")
    if s == "shipped":
        return t("Shipped – confirm the receipt when it arrives.")
    if s == "done":
        return t("Finished.")
    if s == "paused":
        return t("Paused – your decision is needed.")
    if s == "failed":
        return t("The purchase failed.")
    return t("Cancelled.")
