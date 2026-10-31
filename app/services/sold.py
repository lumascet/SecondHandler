""""Sold" / "Reserved" workflow for an own ad (page /sold/{ad_id}, reached from the chat page and My ads).

Three independent steps, each one optional and each one only on an explicit click (+ confirm in the browser):
  1. willhaben: mark the ad as sold or reserved (app/willhaben/sales.py; needs live verification), undo "reserved"
  2. the other buyers: polite "already sold" / "reserved" reply drafts for the other conversations about the ad,
     sent only via chats.send_draft after "Send selected" (+ confirm)
  3. here: automatic answers off for the ad (instructions kept) and the local sale status set
     (MyAd.sale_status; a sold ad never gets automatic answers again, see chats._auto_ad)

Every sale is a row in the ledger (db.Sale, app/services/stock.py). An ad with a quantity sells piece by piece: it
stays online and keeps answering while pieces are left, and only the last piece runs the steps above.
A renewed ad keeps its chats on the old id: the workflow acts on the live copy (db.current_ad) and finds the
chats of every copy (stock.ad_chain).

A purchase paid with PayLivery is recorded by itself for an ad with a quantity (on_paylivery_stage): willhaben takes
the ad offline after it, and with pieces left the app puts it online again (my_ads.relist).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from app.db import Conversation, Draft, Message, MyAd, Sale, Shipment, current_ad, session, utcnow
from app.i18n import t
from app.services import chats, stock
from app.willhaben import sales
from app.willhaben.session import get_session

log = logging.getLogger(__name__)

KINDS = ("sold", "reserved")
NOTICE_INTENTS = {"sold": "sold_notice", "reserved": "reserved_notice"}

# German on purpose: these texts go to buyers (editable before sending)
NOTICE_TEXT = {
    "sold": "Hallo{name}, danke für dein Interesse! Der Artikel ist leider schon verkauft. Liebe Grüße",
    "reserved": ("Hallo{name}, danke für dein Interesse! Der Artikel ist gerade reserviert. "
                 "Falls es nicht klappt, melde ich mich bei dir. Liebe Grüße"),
}
# German on purpose: hint for Claude (chats.draft_reply) when the owner wants a written-out answer
NOTICE_HINT = {
    "sold": ("Der Artikel ist bereits an jemand anderen verkauft. Sag kurz und freundlich ab und bedank dich für das "
             "Interesse. Keine Fragen stellen, nichts anderes anbieten."),
    "reserved": ("Der Artikel ist gerade für jemand anderen reserviert. Sag das kurz und freundlich und biete an, "
                 "dich zu melden, falls es nicht klappt. Keine Fragen stellen."),
}


def kind_of(value: str | None) -> str:
    return value if value in KINDS else "sold"


def notice_text(kind: str, partner: str | None) -> str:
    name = (partner or "").strip()
    return NOTICE_TEXT[kind_of(kind)].format(name=f" {name}" if name else "")


def ad_row(ad_id: str) -> MyAd | None:
    """The live copy of the ad (a renewed ad's chats keep the old id)."""
    with session() as db:
        return current_ad(db, ad_id)


def current_id(ad_id: str) -> str:
    row = ad_row(ad_id)
    return row.ad_id if row else ad_id


def seller_conversations(ad_id: str) -> list[Conversation]:
    """The buyers' chats about the ad, on every copy of it."""
    ids = stock.ad_chain(ad_id)
    with session() as db:
        return list(db.exec(select(Conversation).where(col(Conversation.ad_id).in_(ids), Conversation.role == "seller")
                            .order_by(col(Conversation.last_message_at).desc().nulls_last())))


def title_of(ad_id: str) -> str:
    ad = ad_row(ad_id)
    if ad and ad.heading:
        return ad.heading
    conv = next(iter(seller_conversations(ad_id)), None)
    return (conv.ad_title if conv else None) or ad_id


@dataclass
class Other:
    conv: Conversation
    last_in: str | None          # the buyer's latest message
    informed: bool               # a sold/reserved notice was already sent in this chat
    draft: Draft | None          # open notice draft, if any
    sending: Draft | None = None # notice queued / being sent right now


def others(ad_id: str, buyer: str | None) -> list[Other]:
    """The other buyers' conversations about this ad (everyone except the buyer it was sold/reserved to and, for an
    ad with a quantity, everyone who bought a piece)."""
    ad = ad_row(ad_id)
    bought = {s.conversation_id for s in stock.sales_of(ad_id)} if ad and ad.quantity is not None else set()
    out = []
    with session() as db:
        for conv in seller_conversations(ad_id):
            if conv.id == buyer or conv.id in bought:
                continue
            msgs = list(db.exec(select(Message).where(Message.conversation_id == conv.id, Message.direction == "in")
                                .order_by(col(Message.created_at).desc().nulls_last()).limit(1)))
            notices = list(db.exec(select(Draft).where(Draft.conversation_id == conv.id,
                                                       col(Draft.intent).in_(list(NOTICE_INTENTS.values())))
                                   .order_by(col(Draft.created_at).desc())))
            out.append(Other(conv=conv, last_in=msgs[0].text if msgs else conv.last_message_text,
                             informed=any(d.status == "sent" for d in notices),
                             draft=next((d for d in notices if d.status == "draft"), None),
                             sending=next((d for d in notices if d.status == "sending"), None)))
    return out


# ------------------------------------------------------------------ step 1: willhaben (writes, after confirm)
def pieces_left(ad_id: str) -> bool:
    """An ad with a quantity that still has pieces: willhaben is marked sold only with the last one."""
    return (stock.remaining(ad_id) or 0) > 0


async def on_willhaben(ad_id: str, kind: str, *, undo: bool = False, reason_id: str | None = None) -> str:
    """Mark sold / reserved (or remove "reserved") on willhaben. Raises sales.ReasonRequired, NotOffered, ...
    An ad with pieces left is never marked sold (a stale page, e.g. from before a sale was undone, can still post
    the form): only its last recorded piece leads there."""
    ad_id = current_id(ad_id)       # the live copy: an old one is passive on willhaben
    if kind_of(kind) == "sold" and not undo and pieces_left(ad_id):
        raise sales.NotOffered(t("Pieces left: record the sale instead – willhaben is marked sold with the last piece"))
    s = get_session()
    if kind_of(kind) == "sold":
        if undo:
            raise sales.NotOffered(t('willhaben cannot undo "sold".'))
        await sales.mark_sold(s, ad_id, reason_id)
        status, msg = "sold", t("Marked as sold on willhaben")
    else:
        await sales.mark_reserved(s, ad_id, on=not undo)
        status, msg = ("active", t("Reservation removed on willhaben")) if undo else ("reserved", t("Marked as reserved on willhaben"))
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        row.status, row.last_action, row.last_error = status, msg, None
        if status == "sold":
            row.auto_renew = False    # sold: auto-renew must never reactivate or republish it
        db.add(row)
        db.commit()
    return msg


def enqueue_willhaben(ad_id: str, kind: str, *, undo: bool = False, reason_id: str | None = None) -> bool:
    """Step 1 as a background job (lane ads). When willhaben asks for a sold reason first, nothing is written: the
    reasons wait on the ad (pending_payload) and the page offers them; the next click queues it again.
    False: the ad already waits for another action."""
    from app.services import jobs

    kind, ad_id = kind_of(kind), current_id(ad_id)
    action = "sold" if kind == "sold" else ("unreserve" if undo else "reserved")
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        if row.pending_action:
            return False
        unsaved = (row.pending_payload or {}).get("unsaved")      # an edit text waiting to be saved again stays
        row.pending_action, row.pending_payload, row.last_error = action, ({"unsaved": unsaved} if unsaved else None), None
        db.add(row)
        db.commit()

    def unmark(error: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with session() as db:
            r = db.get(MyAd, ad_id)
            unsaved = (r.pending_payload or {}).get("unsaved")
            r.pending_action, r.pending_payload = None, ({**(payload or {}), "unsaved": unsaved} if unsaved else payload)
            if error is not None:
                r.last_error = error
            db.add(r)
            db.commit()

    async def run(job: jobs.Job) -> str:
        try:
            msg = await on_willhaben(ad_id, kind, undo=undo, reason_id=reason_id)
        except sales.ReasonRequired as e:     # nothing written: the user picks willhaben's reason, then confirms again
            unmark(t("willhaben asks why the ad is sold. Pick a reason."),
                   {"reasons": [{"id": r.id, "label": r.label} for r in e.reasons]})
            return t("willhaben asks why the ad is sold. Pick a reason.")
        except Exception as e:
            unmark(t("Not changed on willhaben: {error}", error=e))
            raise
        unmark()
        from app.services.my_ads import _vinted_follows
        _vinted_follows([ad_id])            # reserved / sold on willhaben: the Vinted item is hidden
        return msg

    label = {"sold": t("Mark „{title}“ as sold on willhaben", title=title_of(ad_id)[:60]),
             "reserved": t("Mark „{title}“ as reserved on willhaben", title=title_of(ad_id)[:60]),
             "unreserve": t("Remove the reservation of „{title}“ on willhaben", title=title_of(ad_id)[:60])}[action]
    # "sold" cannot be undone on willhaben: it waits the grace period in the queue (cancel instead of a confirm)
    jobs.submit(f"sold-{action}", label, run, current=f"ad:{ad_id}", on_cancel=unmark, grace=action == "sold", quiet=True)
    return True


# ------------------------------------------------------------------ one click: here and on willhaben together
def reserve(ad_id: str, buyer: str | None) -> tuple[MyAd, bool]:
    """"Reserve": the ad is reserved here at once (automatic answers off, the buyer noted) and, as a background job,
    marked reserved on willhaben – unless willhaben shows it reserved already. Returns the row and whether the
    willhaben step was queued (False: already reserved there, or another job still works on the ad; the page shows
    the willhaben state and offers the step again). Raises ValueError when an ad with a quantity is sold out."""
    row = finish(ad_id, "reserved", buyer, auto_off=True)
    queued = row.status != "reserved" and enqueue_willhaben(row.ad_id, "reserved")
    return row, queued


def unreserve(ad_id: str) -> tuple[MyAd | None, bool]:
    """"Remove reservation": gone here at once and, when willhaben shows the ad reserved, there too (background job).
    The automatic answers go back to what they were before the reservation (finish() noted them)."""
    row = ad_row(ad_id)
    reset(ad_id, restore_auto=True)
    queued = row is not None and row.status == "reserved" and enqueue_willhaben(row.ad_id, "reserved", undo=True)
    return ad_row(ad_id), queued


def sell(ad_id: str, buyer: str | None, *, pieces: int = 1, price: float | None = None, auto_off: bool = True,
         reason_id: str | None = None) -> tuple[MyAd, bool]:
    """"Sold": the sale is recorded here at once (finish) and, when that sells the ad out (a single item, the last
    piece), the ad is marked sold on willhaben as a background job – unless willhaben shows it sold already.
    Returns the row and whether the willhaben step was queued. Raises ValueError like finish()."""
    row = finish(ad_id, "sold", buyer, auto_off=auto_off, pieces=pieces, price=price)
    queued = row.sale_status == "sold" and row.status not in ("sold", "deleted") \
        and enqueue_willhaben(row.ad_id, "sold", reason_id=reason_id)
    return row, queued


def willhaben_state(ad: MyAd | None) -> dict[str, Any]:
    """What willhaben shows for the ad, for the sale panel: status (sold / reserved / active / …), the job waiting or
    running for it (pending_action), its last error and the sold reasons willhaben asked for."""
    if ad is None:
        return {"status": None, "pending": None, "error": None, "reasons": None}
    raw = ((ad.pending_payload or {}).get("reasons")) or None
    return {"status": ad.status, "pending": ad.pending_action if ad.pending_action in ("sold", "reserved", "unreserve") else None,
            "error": ad.last_error, "reasons": [sales.SoldReason(id=str(r["id"]), label=str(r["label"])) for r in raw] if raw else None}


def pending_reasons(ad_id: str) -> list[sales.SoldReason] | None:
    """Sold reasons a background step left behind (willhaben asked for one), for the reason picker."""
    row = ad_row(ad_id)
    raw = ((row.pending_payload or {}) if row else {}).get("reasons")
    return [sales.SoldReason(id=str(r["id"]), label=str(r["label"])) for r in raw] if raw else None


# ------------------------------------------------------------------ step 2: notices to the other buyers
async def prepare_notices(ad_id: str, kind: str, conversation_ids: list[str], *, buyer: str | None = None,
                          use_claude: bool = False) -> tuple[int, list[str]]:
    """One notice draft per chosen conversation (replacing an unsent one). Nothing is sent here."""
    kind = kind_of(kind)
    allowed = {o.conv.id: o for o in others(ad_id, buyer)}
    made, errors = 0, []
    for cid in conversation_ids:
        o = allowed.get(cid)
        if o is None:
            continue                     # not a conversation about this ad (or the buyer's own)
        if o.draft is not None:
            chats.discard_draft(o.draft.id)
        if use_claude:
            try:
                d = await chats.draft_reply(cid, hint=NOTICE_HINT[kind])
            except Exception as e:  # noqa: BLE001 - one failed draft must not stop the others
                errors.append(f"{o.conv.partner_name or cid}: {e}")
                continue
            with session() as db:
                row = db.get(Draft, d.id)
                row.intent = NOTICE_INTENTS[kind]
                db.add(row)
                db.commit()
        else:
            with session() as db:
                db.add(Draft(conversation_id=cid, text=notice_text(kind, o.conv.partner_name),
                             intent=NOTICE_INTENTS[kind], model="template"))
                db.commit()
        made += 1
    return made, errors


def enqueue_notice_drafts(ad_id: str, kind: str, conversation_ids: list[str], *, buyer: str | None = None) -> int:
    """"Prepare drafts with Claude" as a background job (lane claude, 10–60 s per chat): the chats show "Claude is
    writing…" until their draft is there. Nothing is sent. Returns how many chats were queued."""
    from app.services import jobs

    allowed = {o.conv.id for o in others(ad_id, buyer)}
    picked = [c for c in dict.fromkeys(conversation_ids) if c in allowed]
    with session() as db:
        picked = [c for c in picked if not (db.get(Conversation, c) or Conversation(id=c)).drafting]
    for cid in picked:
        chats._set_drafting(cid, True)
    if not picked:
        return 0

    async def run(job: jobs.Job) -> str:
        made, errors = 0, []
        try:
            for i, cid in enumerate(picked):
                job.progress(i, t("Draft {i} of {n}", i=i + 1, n=len(picked)), f"drafting:{cid}")
                m, err = await prepare_notices(ad_id, kind, [cid], buyer=buyer, use_claude=True)
                made += m
                errors += err
                chats._set_drafting(cid, False, err[0] if err else None)
        finally:
            for cid in picked:
                with session() as db:
                    c = db.get(Conversation, cid)
                    still = bool(c and c.drafting)
                if still:
                    chats._set_drafting(cid, False)
        job.problems = len(errors)
        return t("{n} drafts prepared – nothing sent yet", n=made)

    def cancelled() -> None:
        for cid in picked:
            chats._set_drafting(cid, False)

    jobs.submit("notice-drafts", t("Claude writes {n} notices", n=len(picked)), run, lane="claude", total=len(picked),
                keys=[f"drafting:{c}" for c in picked], on_cancel=cancelled, quiet=True)
    return len(picked)


def send_notices(ad_id: str, items: list[tuple[int, str]]) -> int:
    """Queue the chosen notice drafts on the normal send path (chats.enqueue_send): one background job per chat,
    sent one after another; a failure goes back to that draft with its reason. Returns how many were queued."""
    convs = {c.id for c in seller_conversations(ad_id)}
    queued = 0
    for draft_id, text in items:
        with session() as db:
            d = db.get(Draft, draft_id)
        if d is None or d.status != "draft" or d.conversation_id not in convs or d.intent not in NOTICE_INTENTS.values():
            continue
        queued += chats.enqueue_send(draft_id, text)
    return queued


def discard_notices(ad_id: str, draft_ids: list[int]) -> int:
    convs = {c.id for c in seller_conversations(ad_id)}
    n = 0
    for draft_id in draft_ids:
        with session() as db:
            d = db.get(Draft, draft_id)
        if d and d.status == "draft" and d.conversation_id in convs and d.intent in NOTICE_INTENTS.values():
            chats.discard_draft(draft_id, by_owner=True)
            n += 1
    return n


# ------------------------------------------------------------------ step 3: local state
def finish(ad_id: str, kind: str, buyer: str | None, *, auto_off: bool = True, pieces: int = 1,
           price: float | None = None, note: str | None = None) -> MyAd:
    """"sold" records the sale in the ledger (`price`: its total, optional). An ad with a quantity stays as it is
    while pieces are left: online, automatic answers and auto-renew unchanged. The last piece, a single item and
    "reserved" (ad-wide) set the sale status: automatic answers off (instructions kept), sold never auto-renewed.
    A single item has one state: an earlier sale of it is replaced. Raises ValueError (message for the page) when
    more pieces are asked for than are left, and when an ad with a quantity is sold out (nothing left to sell or
    reserve)."""
    kind = kind_of(kind)
    announce: dict[str, Any] | None = None
    with session() as db:
        row = current_ad(db, ad_id) or MyAd(ad_id=ad_id, heading=title_of(ad_id))
        left = None
        replaced: list[Sale] = []
        if row.quantity is None:
            pieces = 1
            replaced = stock.active_sales(db, row.ad_id)
            _undo(db, replaced)
        else:
            available = max(row.quantity - sum(s.pieces for s in stock.active_sales(db, row.ad_id)), 0)
            if available == 0:
                raise ValueError(t("No pieces left – undo a sale first"))
            if kind == "sold":
                left = available
                if not 1 <= pieces <= left:
                    raise ValueError(t("Pieces: a number from 1 to {max}", max=left))
        sale = None
        if kind == "sold":
            sale = Sale(ad_id=row.ad_id, conversation_id=buyer or None, pieces=pieces, price=price, note=note)
            db.add(sale)
        if left is not None and left > pieces:
            row.last_action = (t("{n} piece sold ({left} left)", n=pieces, left=left - pieces) if pieces == 1 else
                               t("{n} pieces sold ({left} left)", n=pieces, left=left - pieces))
        else:
            if not row.sale_status:       # entering the sale status: remember the answers as they were (reset restores)
                row.auto_reply_before_sale = row.auto_reply
            if auto_off:
                row.auto_reply = False
            if kind == "sold" and row.sale_status != "sold":
                row.auto_renew_before_sale = row.auto_renew
            row.sale_status, row.sale_conversation_id, row.sale_at = kind, buyer or None, utcnow()
            if kind == "sold":
                row.auto_renew = False    # sold: auto-renew must never reactivate or republish it
            row.last_action = t("Sold") if kind == "sold" else t("Reserved")
        db.add(row)
        db.commit()
        db.refresh(row)
        # the same sale entered again (a single item re-sold to the same buyer) is no new event
        if sale is not None and not any(r.conversation_id == sale.conversation_id for r in replaced):
            db.refresh(sale)
            announce = _sale_event(db, row, sale, 0 if left is None else left - pieces)
    if announce is not None:
        _announce_sale(*announce)
    return row


def _sale_event(db: Session, ad: MyAd, sale: Sale, pieces_left: int) -> tuple[str, str, dict[str, Any]]:
    """Title, message and structured fields of the "sale" notification (docs/agent-api.md). Keys not known are left out."""
    conv = db.get(Conversation, sale.conversation_id) if sale.conversation_id else None
    title = ad.heading or ad.ad_id
    message = " · ".join(p for p in (ad.last_action, f"{sale.price:g} €" if sale.price else "") if p)
    data = {"sale_id": sale.id, "ad_id": ad.ad_id, "ad_title": ad.heading, "ad_url": ad.url, "pieces": sale.pieces,
            "price": sale.price, "pieces_left": pieces_left, "conversation_id": sale.conversation_id,
            "buyer": conv.partner_name if conv else None, "category": stock.category_of(db, ad.ad_id),
            "via": "paylivery" if sale.note == PAYLIVERY_NOTE else "manual"}
    return t("Sold: {title}", title=title), message, {k: v for k, v in data.items() if v is not None}


def _announce_sale(title: str, message: str, data: dict[str, Any]) -> None:
    """Tell the AI agent channel about a sale: it lands in the event log and the feed and pushes nothing to the phone
    (push=False). The ledger is written by now, so a failure here never touches the sale."""
    from app.services.notify import notify_later

    notify_later("sale", title, message, url=data.get("ad_url"), data=data, push=False)


def _undo(db: Session, sales: list[Sale]) -> None:
    for s in sales:
        s.undone_at = utcnow()
        db.add(s)


def reset(ad_id: str, *, restore_auto: bool = False) -> None:
    """The deal fell through: forget the local sale status (willhaben is not touched). A single item's sale is
    undone with it. An ad with a quantity keeps its sales (each one has its own undo, undo_sale): only a
    reservation is lifted here. `restore_auto` ("Remove reservation"): the automatic answers go back to what
    they were before the sale status switched them off; otherwise ("not sold after all") they stay off."""
    with session() as db:
        row = current_ad(db, ad_id)
        if row is None or (row.quantity is not None and row.sale_status == "sold"):
            return
        if row.quantity is None:
            _undo(db, stock.active_sales(db, row.ad_id))
        row.sale_status = row.sale_conversation_id = row.sale_at = None
        if restore_auto and row.auto_reply_before_sale is not None:
            row.auto_reply = row.auto_reply_before_sale
        row.auto_reply_before_sale = row.auto_renew_before_sale = None
        db.add(row)
        db.commit()


def reopen(row: MyAd) -> None:
    """The ad sells again (pieces left after all): no sale status here any more, automatic answers and auto-renew
    as they were before the sale switched them off (unchanged where that is not known). The caller commits."""
    row.sale_status = row.sale_conversation_id = row.sale_at = None
    if row.auto_reply_before_sale is not None:
        row.auto_reply = row.auto_reply_before_sale
    if row.auto_renew_before_sale is not None:
        row.auto_renew = row.auto_renew_before_sale
    row.auto_reply_before_sale = row.auto_renew_before_sale = None


def undo_sale(ad_id: str, sale_id: int) -> bool:
    """"Not sold after all" for one sale of the ad: the row stays (undone_at), its pieces are available again. An ad
    it had sold out is no longer sold here (willhaben is not touched: a "sold" there stays); automatic answers and
    auto-renew stay as the sell-out left them. False: not a sale of this ad, or undone already."""
    with session() as db:
        sale = db.get(Sale, sale_id)
        if sale is None or sale.undone_at is not None or sale.ad_id not in stock.ad_chain(ad_id):
            return False
        row = current_ad(db, sale.ad_id)       # reads and texts before writing
        note = (t("Sale undone ({n} piece)", n=sale.pieces) if sale.pieces == 1 else
                t("Sale undone ({n} pieces)", n=sale.pieces))
        _undo(db, [sale])
        if row is not None:
            if row.sale_status == "sold":
                row.sale_status = row.sale_conversation_id = row.sale_at = None
            row.last_action = note
            db.add(row)
        db.commit()
    return True


def agreed_price(conversation_id: str | None) -> float | None:
    """The price agreed in the buyer's chat (its shipment), to prefill the sale."""
    if not conversation_id:
        return None
    with session() as db:
        return db.exec(select(Shipment.agreed_price).where(Shipment.conversation_id == conversation_id,
                                                           col(Shipment.agreed_price).is_not(None))
                       .order_by(col(Shipment.created_at).desc())).first()


def state_for(conv: Conversation) -> dict[str, Any] | None:
    """Sale info for the chat page: kind (sold / reserved, None while an ad with a quantity has pieces left), the
    buyer of that status, whether this chat bought (a sale in the ledger, or the older sale status), the pieces sold
    to it and the stock (quantity/remaining, None for a single item). None when there is nothing to show."""
    if not conv.ad_id:
        return None
    ad = ad_row(conv.ad_id)
    if ad is None:
        return None
    kind = ad.sale_status or (ad.status if ad.status in KINDS else None)
    pieces = sum(s.pieces for s in stock.sales_to(conv.id))
    n = stock.numbers(ad)
    if kind is None and not pieces and n is None:
        return None
    return {"kind": kind, "buyer": ad.sale_conversation_id,
            "is_buyer": bool(pieces) or (kind is not None and ad.sale_conversation_id == conv.id),
            "pieces": pieces, "quantity": n["quantity"] if n else None, "remaining": n["remaining"] if n else None}


def others_to_tell(ad_id: str, buyer: str | None) -> int:
    """How many other buyers' chats have not heard yet (no notice sent): the "Tell the other buyers" button."""
    return sum(1 for o in others(ad_id, buyer) if not o.informed)


def panel(ad_id: str, buyer_id: str | None = None) -> dict[str, Any]:
    """What the sale panel (_sale_actions.html) shows for an ad: its live copy, the buyer's chat (if any), what
    willhaben shows, how many other buyers are still to be told, the stock and the price agreed in the chat."""
    ad_id = current_id(ad_id)
    ad = ad_row(ad_id)
    with session() as db:
        buyer = db.get(Conversation, buyer_id) if buyer_id else None
    n = stock.numbers(ad)
    return {"ad": ad, "buyer": buyer, "wh": willhaben_state(ad), "stock": n,
            "others": others_to_tell(ad_id, buyer.id if buyer else None) if ad is not None else 0,
            "agreed": agreed_price(buyer.id if buyer else None) if n else None}


# ------------------------------------------------------------------ PayLivery: a buyer paid for a piece
PAID_STAGES = ("paid", "released", "label_ready", "shipped", "delivered", "received", "paid_out")
PAYLIVERY_NOTE = "paylivery"          # Sale.note of a sale this hook recorded (a cancellation undoes only those)


async def on_paylivery_stage(conv: Conversation, stage: str, state: dict[str, Any]) -> None:
    """PayLivery stage of a seller chat changed (registered by app/main.py). For an ad with a quantity, the paid
    purchase is one piece sold here, once per chat (a sale already recorded for the chat, e.g. by hand, counts). With
    pieces left the ad is marked to go online again, which willhaben prevents after a PayLivery sale (my_ads.relist,
    a background job); the last piece sells the ad out here as by hand. A cancelled purchase gives its piece back.
    A single item is left to the account import: it follows willhaben's own "sold"."""
    if conv.role != "seller" or not conv.ad_id:
        return
    ad = ad_row(conv.ad_id)
    if ad is None or ad.quantity is None:
        return
    if stage == "cancelled":
        for s in stock.sales_to(conv.id):
            if s.note == PAYLIVERY_NOTE and undo_sale(ad.ad_id, s.id):
                log.info("PayLivery purchase in chat %s cancelled: sale %s of ad %s undone", conv.id, s.id, ad.ad_id)
        return
    if stage not in PAID_STAGES or state.get("corrected_from") or not _recent(state, stage):
        return
    if stock.sales_to(conv.id):           # recorded already (a former stage, or by hand)
        row = ad
    else:
        amount = state.get("amount")
        try:
            row = finish(ad.ad_id, "sold", conv.id, pieces=1, price=float(amount) if amount else None,
                         note=PAYLIVERY_NOTE)
        except ValueError as e:           # sold out here already
            log.info("PayLivery purchase in chat %s not recorded for ad %s: %s", conv.id, ad.ad_id, e)
            return
        if row.sale_status == "sold" and (stock.remaining(row.ad_id) or 0) > 0:
            row = _reopen_after_import(row.ad_id)
        log.info("PayLivery purchase in chat %s: 1 piece of ad %s sold (%s)", conv.id, row.ad_id,
                 row.sale_status or "pieces left")
    # every later stage arms it again: willhaben may take the ad offline only when the seller accepts the purchase
    if row.sale_status != "sold" and (stock.remaining(row.ad_id) or 0) > 0:
        from app.services import my_ads

        my_ads.mark_relist(row.ad_id)
        my_ads.enqueue_relist(row.ad_id)


def _reopen_after_import(ad_id: str) -> MyAd:
    """Sold here with pieces left right after the purchase was recorded: the account import saw willhaben's "sold" for
    this purchase before its paid notice came (finish and my_ads.set_quantity sell out only with the last piece).
    The ad sells on and goes online again. Only for a sale just recorded: a later stage of an older purchase must not
    open an ad the owner marked sold on willhaben since."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        reopen(row)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _recent(state: dict[str, Any], stage: str) -> bool:
    """The stage was reached lately: a first sync or a recheck of an old chat applies weeks-old notices too, which
    must not record a sale or put an ad online again (like the shipment, paylivery.MAX_AGE)."""
    from app.services.paylivery import MAX_AGE

    at = next((h.get("at") for h in reversed(state.get("history") or []) if h.get("stage") == stage), None)
    try:
        when = datetime.fromisoformat(at) if at else None
    except ValueError:
        when = None
    return when is None or (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) >= utcnow() - MAX_AGE
