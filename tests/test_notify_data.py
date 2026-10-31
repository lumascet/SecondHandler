"""The structured `data` of the notifications for the AI agent channel (docs/agent-api.md): purchase, auto-buy,
PayLivery, shipping and the new sale event carry the documented keys next to the ones Telegram and Home Assistant use."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.db import Conversation, Message, MyAd, Purchase, Sale, Shipment, session, utcnow
from app.services import auto_buy, paylivery, purchase, shipping, sold, stock, telegram
from app.services import notify as notify_mod
from tests.test_auto_buy import _detail, _hit, agent, purchases_on, pushes  # noqa: F401 - fixtures
from tests.test_purchase import detail, fx, open_order, row, set_row  # noqa: F401 - fixtures


def _json_safe(data):
    import json
    json.dumps(data)


# ------------------------------------------------------------------ purchase
def test_event_data_has_the_documented_keys_and_leaves_unknown_ones_out():
    p = Purchase(id=7, ad_id="555", ad_title="Maus", ad_url="https://w/555", mode="paylivery", asking_price=100.0,
                 max_price=110.0, agreed_price=80.0, conversation_id="c1", status="agreed", category="Computer > Maus",
                 agent_id=3, shipping_cost=5.0, shipping_source="ad")
    d = purchase.event_data(p, "update")
    costs = purchase.costs_of(p, 80.0)
    assert d == {"stage": "update", "purchase_id": 7, "status": "agreed", "conversation_id": "c1", "ad_id": "555",
                 "platform": "willhaben", "ad_title": "Maus", "ad_url": "https://w/555", "category": "Computer > Maus",
                 "mode": "paylivery", "asking_price": 100.0, "max_price": 110.0, "agreed_price": 80.0,
                 "shipping": 5.0, "fee": costs["fee"], "total": costs["total"], "agent_id": 3}
    assert costs["fee"] > 0 and d["total"] == round(80.0 + 5.0 + costs["fee"], 2)
    bare = purchase.event_data(Purchase(ad_id="9", mode="pickup", status="new"))
    assert bare["stage"] == "update" and "purchase_id" not in bare and "ad_title" not in bare and "category" not in bare
    assert "total" not in bare and "agent_id" not in bare                  # nothing known, nothing sent
    done = purchase.event_data(Purchase(id=1, ad_id="9", mode="pickup", status="done", agreed_price=20.0))
    assert done["stage"] == "bought" and done["shipping"] == 0.0 and done["fee"] == 0.0 and done["total"] == 20.0
    assert purchase.event_data(Purchase(ad_id="9", status="failed"))["stage"] == "failed"
    assert purchase.event_data(Purchase(ad_id="9", status="pay_now"))["stage"] == "pay_now"
    _json_safe(d)


async def test_category_is_stored_when_the_order_starts_and_pushes_carry_the_fields(fx):  # noqa: F811
    fx.ad = detail(category_path=["Elektronik", "Audio", "Kopfhörer"])
    pid = await open_order(fx)
    assert row(pid).category == "Elektronik > Audio > Kopfhörer"
    await purchase.pause(pid, "Please decide")
    kind, title, _msg, kw = fx.notes[-1]
    assert kind == "purchase" and title == "Purchase paused"
    data = kw["data"]
    assert data["stage"] == "update" and data["status"] == "paused" and data["purchase_id"] == pid
    assert data["conversation_id"] == "conv-buy-1" and data["ad_id"] == "555" and data["platform"] == "willhaben"
    assert data["category"] == "Elektronik > Audio > Kopfhörer" and data["mode"] == "paylivery"
    assert data["ad_title"] == "Logitech MX Master 3" and data["asking_price"] == 300.0 and data["max_price"] > 300
    assert data["shipping"] >= 0 and "fee" in data and data["total"] > 300
    telegram.keyboard("purchase", data, [], None)                          # Telegram copes with the extra keys


async def test_category_without_a_path_stays_empty(fx):  # noqa: F811
    fx.ad = detail(category_path=[])
    pid = await open_order(fx)
    assert row(pid).category is None
    await purchase._fail(pid, "boom")
    data = fx.notes[-1][3]["data"]
    assert data["stage"] == "failed" and "category" not in data


async def test_pay_now_push_is_the_pay_now_stage(fx):  # noqa: F811
    pid = await open_order(fx)
    set_row(pid, agreed_price=250.0)
    r = purchase.get(pid)
    r = purchase._change(pid, lambda x: purchase._move(x, "pay_now", "agreed"))
    await purchase._push_paylivery_pay(r)
    data = fx.notes[-1][3]["data"]
    assert data["stage"] == "pay_now" and data["agreed_price"] == 250.0 and data["total"] > 250


# ------------------------------------------------------------------ auto-buy
async def test_auto_buy_countdown_is_announced_with_cancel_by_and_url(agent, pushes):  # noqa: F811
    before = utcnow()
    pid = await auto_buy.schedule(agent, _hit(agent))
    note = pushes[0]
    d = note["data"]
    assert note["kind"] == "purchase" and d["stage"] == "announced" and d["status"] == "new"
    assert d["purchase_id"] == pid and d["auto_buy"] is True and d["price"] == 40.0     # what Telegram's Cancel uses
    assert d["ad_id"] == "777" and d["ad_title"] == "HomePod mini" and d["mode"] == "paylivery"
    assert d["asking_price"] == 40.0 and d["max_price"] == 60.0 and d["agent_id"] == agent.id and d["platform"] == "willhaben"
    assert d["shipping"] == 3.49 and d["total"] == purchase.costs_of(purchase.get(pid), 40.0)["total"]
    by = datetime.fromisoformat(d["cancel_by"])
    assert by.tzinfo is not None and timedelta(seconds=auto_buy.COUNTDOWN_S - 5) < by - before < timedelta(seconds=auto_buy.COUNTDOWN_S + 5)
    assert d["cancel_url"].endswith(f"/purchases/{pid}")
    assert "conversation_id" not in d
    _json_safe(d)


async def test_auto_buy_bought_push_carries_the_order(agent, pushes, monkeypatch):  # noqa: F811
    from app import platforms
    ad = _detail()
    ad.category_path = ["Audio", "Lautsprecher"]

    async def get_detail(ad_id, url=None):
        return ad

    async def run_action(cid, action, amount=None, claimed=False):
        paylivery._record(cid, action, True, "ok", amount)
        return "ok"
    monkeypatch.setattr(platforms.get("willhaben"), "detail", get_detail)
    monkeypatch.setattr(paylivery, "run_action", run_action)
    pid = await auto_buy.schedule(agent, _hit(agent))
    assert await auto_buy.buy(pid) == "Bought"
    assert purchase.get(pid).category == "Audio > Lautsprecher"
    note = next(p for p in pushes if p["title"].startswith("Bought"))
    d = note["data"]
    assert d["stage"] == "bought" and d["status"] == "paid" and d["purchase_id"] == pid and d["agreed_price"] == 40.0
    assert d["category"] == "Audio > Lautsprecher" and d["total"] <= 60.0 and d["agent_id"] == agent.id
    assert "auto_buy" not in d and "conversation_id" not in d        # no Cancel button, no hidden placeholder chat


# ------------------------------------------------------------------ PayLivery and shipping
@pytest.fixture
def captured(monkeypatch):
    sent = []

    async def notify(kind, title, message, **kw):
        sent.append({"kind": kind, "title": title, "message": message, **kw})
        return True
    monkeypatch.setattr("app.services.notify.notify", notify)
    return sent


async def test_paylivery_push_data(captured):
    with session() as db:
        db.add(Conversation(id="c9", ad_id="77", role="seller", partner_name="Max"))
        conv = db.get(Conversation, "c9")
        msg = Message(conversation_id="c9", ext_id="m1", direction="in", text="x", created_at=utcnow())
    await paylivery._push(conv, {"stage": "offer_sent", "role": "seller", "offer": 55.0}, msg)
    (n,) = captured
    assert n["kind"] == "paylivery"
    assert n["data"] == {"stage": "offer_sent", "role": "seller", "ad_id": "77", "conversation_id": "c9", "amount": 55.0}


async def test_shipping_push_data(captured):
    with session() as db:
        db.add(Conversation(id="c8", ad_id="88", role="seller"))
        s = Shipment(conversation_id="c8", ad_id="88", status="labelled", price=4.9, paid_price=5.2, tracking="AB123")
        db.add(s)
        db.commit()
        db.refresh(s)
    await shipping._notify(s, "Label bought", "x")
    d = captured[0]["data"]
    assert captured[0]["kind"] == "shipping"
    assert d == {"shipment_id": s.id, "status": "labelled", "ad_id": "88", "conversation_id": "c8", "price": 5.2,
                 "tracking": "AB123"}
    with session() as db:
        s2 = Shipment(conversation_id="c8", status="draft")
        db.add(s2)
        db.commit()
        db.refresh(s2)
    assert shipping.event_data(s2) == {"shipment_id": s2.id, "status": "draft", "conversation_id": "c8"}


# ------------------------------------------------------------------ the sale event
@pytest.fixture
def stock_ad():
    with session() as db:
        db.add(MyAd(ad_id="A1", heading="USB-Kabel", status="active", quantity=3, url="https://w/A1"))
        db.add(MyAd(ad_id="A2", heading="Fahrrad", status="active", url="https://w/A2"))
        db.add(Conversation(id="cs1", ad_id="A1", role="seller", partner_name="Max"))
        db.add(Conversation(id="cs2", ad_id="A2", role="seller", partner_name="Eva"))
        db.add(Conversation(id="cs3", ad_id="A2", role="seller", partner_name="Tom"))
        db.commit()


async def _settle():
    while notify_mod._later:
        await asyncio.gather(*list(notify_mod._later))


async def test_a_sale_is_announced_without_a_push(stock_ad, captured):
    sold.finish("A1", "sold", "cs1", pieces=2, price=30.0)
    await _settle()
    (n,) = captured
    assert n["kind"] == "sale" and n["push"] is False and n["title"] == "Sold: USB-Kabel"
    with session() as db:
        sale = db.exec(select(Sale)).one()
    d = n["data"]
    assert d == {"sale_id": sale.id, "ad_id": "A1", "ad_title": "USB-Kabel", "ad_url": "https://w/A1", "pieces": 2,
                 "price": 30.0, "pieces_left": 1, "conversation_id": "cs1", "buyer": "Max", "via": "manual"}
    assert "2 pieces sold" in n["message"] and "30 €" in n["message"]
    _json_safe(d)


async def test_no_event_for_a_reservation_an_undo_or_the_same_sale_again(stock_ad, captured):
    sold.finish("A2", "reserved", "cs2")
    await _settle()
    assert captured == []
    sold.finish("A2", "sold", "cs2", price=90.0)                           # single item: pieces_left 0
    await _settle()
    assert len(captured) == 1 and captured[0]["data"]["pieces_left"] == 0 and captured[0]["data"]["pieces"] == 1
    sold.finish("A2", "sold", "cs2", price=90.0)                           # entered again for the same buyer
    await _settle()
    assert len(captured) == 1
    sold.finish("A2", "sold", "cs3", price=85.0)                           # another buyer replaces it: a new sale
    await _settle()
    assert len(captured) == 2 and captured[1]["data"]["buyer"] == "Tom"
    with session() as db:
        sale = db.get(Sale, captured[1]["data"]["sale_id"])
    assert sold.undo_sale("A2", sale.id)
    await _settle()
    assert len(captured) == 2                                              # an undo needs no event


async def test_a_paylivery_sale_fires_once(stock_ad, captured, monkeypatch):
    from app.services import my_ads
    monkeypatch.setattr(my_ads, "enqueue_relist", lambda ad_id: True)
    with session() as db:
        conv = db.get(Conversation, "cs1")
    await sold.on_paylivery_stage(conv, "paid", {"stage": "paid", "amount": 12.5})
    await sold.on_paylivery_stage(conv, "released", {"stage": "released", "amount": 12.5})   # next stage: no 2nd sale
    await _settle()
    (n,) = captured
    assert n["data"]["via"] == "paylivery" and n["data"]["price"] == 12.5 and n["data"]["pieces_left"] == 2
    assert stock.remaining("A1") == 2


async def test_a_failing_notification_never_breaks_the_sale(stock_ad, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr("app.services.notify.notify", boom)
    sold.finish("A1", "sold", "cs1")
    await _settle()
    assert stock.remaining("A1") == 2


def test_a_sale_recorded_outside_a_loop_is_announced_too(stock_ad, captured):
    sold.finish("A1", "sold", "cs1", price=9.0)
    assert len(captured) == 1 and captured[0]["data"]["price"] == 9.0


# ------------------------------------------------------------------ status changes and sale categories
async def test_every_purchase_status_change_is_a_quiet_status_event(fx, captured):  # noqa: F811
    pid = await open_order(fx)
    await _settle()
    captured.clear()
    purchase._change(pid, lambda x: purchase._move(x, "paid", "paid by the user"))
    purchase._change(pid, lambda x: purchase._line(x, "a log line, same status"))     # no change, no event
    await _settle()
    (n,) = captured
    assert n["kind"] == "purchase" and n["push"] is False and n["title"] == "Purchase: Paid"
    assert n["data"]["stage"] == "status" and n["data"]["status"] == "paid" and n["data"]["purchase_id"] == pid
    assert n["data"]["from"] == row(pid).log[-2]["from"] == "contacting" and n["data"]["ad_title"] == "Logitech MX Master 3"


async def test_a_sale_names_the_category_of_the_draft_the_ad_came_from(stock_ad, captured):
    from app.db import AdDraft
    with session() as db:
        db.add(AdDraft(input_text="x", title="USB-Kabel", wh_ad_id="A1", wh_plan={"selected": 0, "options": [
            {"path": [{"label": "Computer"}, {"label": "Kabel"}]}]}))
        db.add(AdDraft(input_text="x", title="Fahrrad", wh_ad_id="A2", category_hint="Fahrräder"))
        db.commit()
    sold.finish("A1", "sold", "cs1", price=10.0)
    sold.finish("A2", "sold", "cs2", price=90.0)
    await _settle()
    assert [n["data"]["category"] for n in captured] == ["Computer > Kabel", "Fahrräder"]


async def test_a_restart_that_fails_an_order_tells_the_agent_too(captured):
    with session() as db:
        p = Purchase(ad_id="77", ad_title="Lampe", status="new", mode="pickup")
        db.add(p)
        db.commit()
        db.refresh(p)
    purchase.recover()
    await _settle()
    (n,) = captured
    assert n["data"]["stage"] == "status" and n["data"]["status"] == "failed" and n["data"]["from"] == "new"
