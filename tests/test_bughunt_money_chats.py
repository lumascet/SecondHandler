"""Bug hunt (11-08): a deactivated ad is not for sale on the other sites; a fresh ad goes on Kleinanzeigen once
willhaben has it online; a paid order never turns "failed"; chats that come back and are read again."""
from datetime import timedelta

from app.db import MyAd, Purchase, session, utcnow
from app.services import crosspost, crosspost_kleinanzeigen as ck, purchase


def test_a_deactivated_ad_is_not_for_sale_but_one_in_review_is():
    old = MyAd(ad_id="1", heading="x", status="Passiv", published_at=utcnow() - timedelta(days=3))
    fresh = MyAd(ad_id="2", heading="x", status="Passiv", published_at=utcnow() - timedelta(hours=2))
    assert crosspost.not_for_sale(old) == "deactivated" and crosspost.not_for_sale(fresh) is None


def test_also_on_kleinanzeigen_waits_for_the_review(monkeypatch):
    queued = []
    monkeypatch.setattr(ck, "enqueue", lambda i: queued.append(i) or True)
    with session() as db:
        db.add(MyAd(ad_id="7001", heading="Rad", status="active"))
        db.commit()
    ck.after_review("7001")
    assert ck._ka(ck._row("7001")).get("wish") == "after_review"
    assert ck.review_done(["7001"]) == 1 and queued == ["7001"]
    assert ck._ka(ck._row("7001")).get("wish") is None


async def test_a_paid_order_never_becomes_failed(monkeypatch):
    pushed = []

    async def push(row, title, text, **kw):
        pushed.append(title)
    monkeypatch.setattr(purchase, "_push", push)
    with session() as db:
        p = Purchase(ad_id="9", status="paid", mode="paylivery", log=[])
        q = Purchase(ad_id="10", status="pay_now", mode="paylivery", log=[])
        db.add(p)
        db.add(q)
        db.commit()
        db.refresh(p)
        db.refresh(q)
    await purchase._fail(p.id, "lost push")
    await purchase._fail(q.id, "refused")
    assert purchase.get(p.id).status == "paid" and purchase.get(q.id).status == "failed" and pushed == ["Purchase failed"]
