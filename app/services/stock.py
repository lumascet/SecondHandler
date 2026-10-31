"""Stock per own ad (MyAd.quantity) and the sales ledger (Sale): how many pieces an ad still has.

Assumptions (the owner could not be asked): stock is local to the app, willhaben has no quantity field we know of,
so the ad text says how many pieces there are and the owner edits it there when that changes. The price is per
piece (MyAd.min_price is a per-piece floor) and a reservation stays ad-wide. A renewed ad gets a new willhaben id
(MyAd.replaced_by) while its chats keep the old one: sales recorded on any copy count for the whole chain.
A piece paid with PayLivery is recorded by itself (sold.on_paylivery_stage). willhaben takes the ad offline after such
a sale (seen by the owner; which status it shows is not recorded yet): with pieces left the app puts it online again,
as a new ad when willhaben offers no way back (my_ads.relist), and that copy joins the chain.
"""
from __future__ import annotations

import re

from sqlmodel import Session, col, select

from app.db import AdDraft, MyAd, Sale, current_ad, session
from app.i18n import t


def _chain(db: Session, ad_id: str) -> list[str]:
    ids, todo = [ad_id], [ad_id]
    while todo:
        cur = todo.pop()
        row = db.get(MyAd, cur)
        linked = list(db.exec(select(MyAd.ad_id).where(MyAd.replaced_by == cur)))
        if row is not None and row.replaced_by:
            linked.append(row.replaced_by)
        for x in linked:
            if x not in ids:
                ids.append(x)
                todo.append(x)
    return ids


def ad_chain(ad_id: str) -> list[str]:
    """Every id of an ad renewed under new ids (MyAd.replaced_by, both directions): its old copies and the newest."""
    with session() as db:
        return _chain(db, ad_id)


def category_of(db: Session, ad_id: str) -> str | None:
    """The category of an own ad (any copy of its chain), from the ad draft it was published from: the category
    chosen for willhaben, else Claude's hint. Ads not created here, or whose draft link was cleared, have none:
    willhaben's ad list does not name the category, only a request to the ad itself would."""
    drafts = db.exec(select(AdDraft).where(col(AdDraft.wh_ad_id).in_(_chain(db, ad_id))).order_by(col(AdDraft.id).desc()))
    for d in drafts:
        plan = d.wh_plan or {}
        options = plan.get("options") or []
        picked = options[plan["selected"]] if isinstance(plan.get("selected"), int) \
            and 0 <= plan["selected"] < len(options) else None
        labels = [p.get("label") for p in (picked or {}).get("path") or [] if p.get("label")]
        if labels or d.category_hint:
            return " > ".join(labels) if labels else d.category_hint
    return None


def active_sales(db: Session, ad_id: str) -> list[Sale]:
    """The ad's sales that count (not undone), over its chain, newest first."""
    return list(db.exec(select(Sale).where(col(Sale.ad_id).in_(_chain(db, ad_id)), col(Sale.undone_at).is_(None))
                        .order_by(col(Sale.created_at).desc(), col(Sale.id).desc())))


def sales_of(ad_id: str) -> list[Sale]:
    with session() as db:
        return active_sales(db, ad_id)


def pieces_sold(ad_id: str) -> int:
    return sum(s.pieces for s in sales_of(ad_id))


def numbers(ad: MyAd | None) -> dict[str, int] | None:
    """{"quantity", "sold", "remaining"} of an ad with a quantity; None for a single item (quantity None)."""
    if ad is None or ad.quantity is None:
        return None
    sold = pieces_sold(ad.ad_id)
    return {"quantity": ad.quantity, "sold": sold, "remaining": max(ad.quantity - sold, 0)}


def remaining(ad_id: str) -> int | None:
    """Pieces still available; None for a single item."""
    with session() as db:
        ad = current_ad(db, ad_id)
    n = numbers(ad)
    return n["remaining"] if n else None


def stock_text(ad: MyAd | None) -> str:
    """"2 sold · 1 available" (My ads); "" for a single item."""
    n = numbers(ad)
    return t("{sold} sold · {left} available", sold=n["sold"], left=n["remaining"]) if n else ""


def sales_to(conversation_id: str) -> list[Sale]:
    """The sales that count recorded for a buyer's chat (a chat belongs to one ad, whichever copy it was recorded on)."""
    with session() as db:
        return list(db.exec(select(Sale).where(Sale.conversation_id == conversation_id, col(Sale.undone_at).is_(None))
                            .order_by(col(Sale.created_at).desc(), col(Sale.id).desc())))


def sold_to(conversation_id: str) -> Sale | None:
    """The newest sale that counts recorded for this chat."""
    return next(iter(sales_to(conversation_id)), None)


# ------------------------------------------------------------------ guard for automatic answers
# German on purpose: the answers to buyers are German. Number words from two on (one piece is always fine).
# Counts without a piece word ("3x", "noch 3 verfügbar") are not parsed: the prompt rule covers them.
_WORDS = {"zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
          "elf": 11, "zwölf": 12}
_PIECES = re.compile(rf"(?<![\w.,])(\d{{1,4}}|{'|'.join(_WORDS)})\s*(?:stück(?:e|en)?|stk|teile?|exemplare?)(?!\w)", re.I)


def piece_counts(text: str) -> list[int]:
    """Piece counts an answer names: "2 Stück", "drei Stk.", "4 Teile", "zwei Exemplare"."""
    return [int(w) if w.isdigit() else _WORDS[w.lower()] for w in (m.group(1) for m in _PIECES.finditer(text or ""))]


def blockers(ad: MyAd | None, text: str) -> list[str]:
    """An automatic answer that promises more pieces than are left is held back (checked in code, whatever Claude
    says). False positives only cost one tap on the push."""
    n = numbers(ad)
    over = [c for c in piece_counts(text) if n is not None and c > n["remaining"]]
    return [t("Answer offers {n} pieces, only {left} available", n=max(over), left=n["remaining"])] if over else []
