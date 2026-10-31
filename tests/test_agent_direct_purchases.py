"""Purchases made on willhaben directly, outside a purchase order, for the AI agent (docs/agent-api.md): a buyer chat
with PayLivery state in a deal stage is listed next to the orders (source "chat") and announces its stage changes with
a quiet purchase event; a chat with a Purchase row or in the seller role is neither listed nor announced."""
import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Message, Purchase, session, utcnow
from app.main import app
from app.platforms import fees
from app.services import notify as notify_mod
from app.services import paylivery, purchase


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _ago(**kw) -> str:
    return (utcnow() - timedelta(**kw)).isoformat()


def _chat(cid="cb1", stage="paid", role="buyer", history=None, ad_id="77", platform=None, ctx=None, **state):
    """A chat with PayLivery state as the notices leave it (role, stage, amounts, history)."""
    history = history if history is not None else [{"at": _ago(hours=5), "stage": stage, "text": "x", "notice": "n1"}]
    st = {"role": role, "stage": stage, "history": history, "applied": ["n1"], "rules": paylivery.RULES, **state}
    ctx = {"title": "Kinderbuch", "url": f"https://www.willhaben.at/iad/object?adId={ad_id}", "price": "€ 12",
           "category": "Baby/Kind > Bücher", "images": ["https://cache.willhaben.at/p.jpg"],
           "shipping_price": 4.9, "paylivery": True} if ctx is None else ctx
    with session() as db:
        db.add(Conversation(id=cid, ad_id=ad_id, ad_title="Kinderbuch", role=role, platform=platform, paylivery=st,
                            ad_context=ctx, last_message_at=utcnow()))
        db.commit()


def _order(**kw) -> int:
    base = dict(ad_id="2000000001", ad_title="Bosch Geschirrspüler", asking_price=250.0, max_price=280.0,
                mode="paylivery", status="negotiating")
    base.update(kw)
    with session() as db:
        row = Purchase(**base)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _list(client, **params):
    return client.get("/api/purchases", params=params).json()


# ------------------------------------------------------------------ the list
def test_a_buyer_chat_in_a_deal_stage_is_listed_with_its_amounts(client):
    _chat("cb1", "paid", offer=10.0, amount=10.0)
    (p,) = _list(client)
    fee = fees.rule("willhaben", ["Baby/Kind", "Bücher"]).fee(10.0)
    assert p == {
        "source": "chat", "id": None, "conversation_id": "cb1", "ad_id": "77", "platform": "willhaben",
        "ad_title": "Kinderbuch", "ad_url": "https://www.willhaben.at/iad/object?adId=77",
        "image_url": "https://cache.willhaben.at/p.jpg", "category": "Baby/Kind > Bücher", "status": "paid",
        "mode": "paylivery", "agreed_price": 10.0, "shipping": 4.9, "shipping_source": "ad", "fee": fee,
        "total": round(10.0 + 4.9 + fee, 2), "created_at": p["created_at"], "updated_at": p["updated_at"],
        "app_path": "/chats/cb1", "app_url": None}
    assert p["created_at"].endswith("+00:00") and p["updated_at"].endswith("+00:00")


def test_unknown_values_are_null(client):
    _chat("cb1", "offer_accepted", ctx={})                      # no stored ad, no amount yet
    (p,) = _list(client)
    assert p["status"] == "offer_accepted" and p["ad_title"] == "Kinderbuch" and p["ad_url"].endswith("adId=77")
    assert (p["category"], p["image_url"], p["agreed_price"], p["shipping"], p["fee"], p["total"]) == \
        (None, None, None, None, None, None)


def test_the_accepted_offer_is_the_price_before_the_amount_of_a_notice(client):
    _chat("cb1", "paid", offer=8.0, amount=15.0)
    assert _list(client)[0]["agreed_price"] == 8.0
    _chat("cb2", "paid", ad_id="78", amount=15.0)               # a buy-now purchase: no offer
    assert {p["conversation_id"]: p["agreed_price"] for p in _list(client)} == {"cb1": 8.0, "cb2": 15.0}


def test_every_deal_stage_counts_and_the_others_do_not(client):
    for i, stage in enumerate(purchase.CHAT_DEAL_STAGES):
        _chat(f"c-{stage}", stage, ad_id=str(i))
    for i, stage in enumerate(("offer_sent", "counter_received", "offer_declined", "offer_received", "paid_out")):
        _chat(f"x-{stage}", stage, ad_id=f"x{i}")
    _chat("x-none", None, history=[], ad_id="y1")
    assert {p["status"] for p in _list(client, limit=100)} == set(purchase.CHAT_DEAL_STAGES)
    assert purchase.CHAT_DEAL_STAGES == ("offer_accepted", "paid", "released", "label_ready", "shipped", "delivered",
                                         "received")


def test_a_problem_or_cancellation_counts_after_a_deal_stage_only(client):
    paid = [{"at": _ago(days=2), "stage": "paid", "text": "x"}, {"at": _ago(days=1), "stage": "problem", "text": "x"}]
    _chat("cb1", "problem", history=paid)
    _chat("cb2", "cancelled", ad_id="78", history=[{"at": _ago(days=1), "stage": "offer_sent", "text": "x"},
                                                  {"at": _ago(hours=1), "stage": "cancelled", "text": "x"}])
    _chat("cb3", "cancelled", ad_id="79", history=[{"at": _ago(days=2), "stage": "paid", "text": "x"},
                                                  {"at": _ago(hours=1), "stage": "cancelled", "text": "x"}])
    assert {p["conversation_id"]: p["status"] for p in _list(client)} == {"cb1": "problem", "cb3": "cancelled"}


def test_seller_chats_other_sites_and_chats_without_state_are_not_listed(client):
    _chat("seller", "paid", role="seller", ad_id="1")
    _chat("vinted", "paid", ad_id="2", platform="vinted")
    _chat("mixed", "paid", ad_id="3", role="seller")             # the chat says seller ...
    with session() as db:
        db.add(Conversation(id="plain", ad_id="4", role="buyer"))                  # ... no PayLivery at all
        c = db.get(Conversation, "mixed")
        c.role = "buyer"                                         # ... role flipped, state still the seller's
        db.add(c)
        db.commit()
    _chat("ok", "shipped", ad_id="5", platform="willhaben")
    assert [p["conversation_id"] for p in _list(client)] == ["ok"]


def test_a_chat_with_a_purchase_row_is_not_listed_twice(client):
    _chat("cb1", "paid", ad_id="77")
    _chat("cb2", "paid", ad_id="78")
    _chat("cb3", "shipped", ad_id="79")
    _order(ad_id="77", conversation_id="cb1", status="paid")
    _order(ad_id="78", conversation_id="cb2", status="done")      # a finished order counts as well
    rows = _list(client, limit=100)
    assert sorted((r["source"], r.get("conversation_id")) for r in rows) == \
        [("chat", "cb3"), ("order", "cb1"), ("order", "cb2")]
    assert all(r["id"] is not None for r in rows if r["source"] == "order")


def test_the_real_chat_of_an_auto_buy_order_is_the_orders(client):
    _chat("cb-real", "paid", ad_id="77")
    _order(ad_id="77", conversation_id="autobuy-77", status="pay_now")        # still on the placeholder chat
    assert [r["source"] for r in _list(client)] == ["order"]


def test_the_source_filter(client):
    _chat("cb1", "paid")
    pid = _order(status="paid")
    assert {r["source"] for r in _list(client)} == {"order", "chat"}
    assert [(r["source"], r["id"]) for r in _list(client, source="order")] == [("order", pid)]
    assert [(r["source"], r["conversation_id"]) for r in _list(client, source="chat")] == [("chat", "cb1")]
    assert client.get("/api/purchases", params={"source": "both"}).status_code == 400


def test_orders_carry_their_source_and_conversation(client):
    _chat("cb1", "paid")
    _order(conversation_id="conv-1")
    (o,) = _list(client, source="order")
    assert o["source"] == "order" and o["conversation_id"] == "conv-1"


def test_status_filter_picks_the_kind_it_belongs_to(client):
    _chat("cb1", "paid", ad_id="1")
    _chat("cb2", "shipped", ad_id="2")
    _chat("cb3", "label_ready", ad_id="3")
    order = _order(status="paid")
    _order(ad_id="2000000002", status="negotiating")
    assert {(r["source"], r["status"]) for r in _list(client, status="paid")} == {("order", "paid"), ("chat", "paid")}
    assert [(r["source"], r["status"]) for r in _list(client, status="label_ready")] == [("chat", "label_ready")]
    assert [(r["source"], r["id"]) for r in _list(client, status="negotiating")] == [("order", order + 1)]
    assert [r["conversation_id"] for r in _list(client, status="shipped", source="chat")] == ["cb2"]
    assert _list(client, status="shipped", source="order") == []
    assert client.get("/api/purchases", params={"status": "bogus"}).status_code == 400


def test_active_means_not_yet_delivered_or_finished(client):
    open_stages = ("offer_accepted", "paid", "released", "label_ready", "shipped")
    assert purchase.CHAT_OPEN_STAGES == (*open_stages, "problem")
    for i, stage in enumerate((*open_stages, "delivered", "received")):
        _chat(f"c-{stage}", stage, ad_id=str(i))
    _chat("c-problem", "problem", ad_id="p", history=[{"at": _ago(days=1), "stage": "paid", "text": "x"}])
    _chat("c-cancelled", "cancelled", ad_id="q", history=[{"at": _ago(days=1), "stage": "paid", "text": "x"}])
    _order(status="paid")
    _order(ad_id="2000000002", status="done")
    live = {(r["source"], r["status"]) for r in _list(client, active=1, limit=100)}
    assert live == {("chat", s) for s in (*open_stages, "problem")} | {("order", "paid")}
    rest = {(r["source"], r["status"]) for r in _list(client, active=0, limit=100)}
    assert rest == {("chat", "delivered"), ("chat", "received"), ("chat", "cancelled"), ("order", "done")}


def test_both_kinds_are_merged_by_the_last_change_and_limited_together(client):
    _chat("c-old", "paid", ad_id="1", history=[{"at": _ago(days=9), "stage": "paid", "text": "x"}])
    _chat("c-new", "shipped", ad_id="3", history=[{"at": _ago(days=9), "stage": "paid", "text": "x"},
                                                  {"at": _ago(minutes=5), "stage": "shipped", "text": "x"}])
    mid = _order(ad_id="2000000002", status="paid")
    with session() as db:
        row = db.get(Purchase, mid)
        row.updated_at = utcnow() - timedelta(days=2)
        db.add(row)
        db.commit()
    rows = _list(client)
    assert [(r["source"], r.get("conversation_id") if r["source"] == "chat" else r["id"]) for r in rows] == \
        [("chat", "c-new"), ("order", mid), ("chat", "c-old")]
    created = {r["conversation_id"]: r["created_at"] for r in rows if r["source"] == "chat"}
    assert created["c-new"][:13] == created["c-old"][:13]          # the first step of both is nine days old
    assert [r["conversation_id"] for r in _list(client, limit=1)] == ["c-new"]
    assert len(_list(client, limit=2)) == 2


def test_the_app_url_follows_the_web_address(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "public_url", "https://agent.example.org/")
    _chat("cb1", "paid")
    assert _list(client)[0]["app_url"] == "https://agent.example.org/chats/cb1"


# ------------------------------------------------------------------ the event
@pytest.fixture
def events(monkeypatch):
    """The purchase module's PayLivery hook alone; the purchase notifications sent."""
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    paylivery.on_stage(purchase.on_paylivery_stage)
    sent = []

    async def notify(kind, title, message, **kw):
        if kind == "purchase":                                   # PayLivery's own pushes are not the point here
            sent.append({"kind": kind, "title": title, "message": message, **kw})
        return True

    monkeypatch.setattr("app.services.notify.notify", notify)
    return sent


async def _settle():
    while paylivery._hook_tasks or notify_mod._later:
        await asyncio.gather(*list(paylivery._hook_tasks), *list(notify_mod._later), return_exceptions=True)


def _notice(ext_id, text, cid="cb1", minutes_ago=5):
    with session() as db:
        db.add(Message(conversation_id=cid, ext_id=ext_id, direction="unknown", text=text, kind="integration",
                       created_at=utcnow() - timedelta(minutes=minutes_ago)))
        db.commit()


def _buyer_chat(cid="cb1", role="buyer", **kw):
    with session() as db:
        db.add(Conversation(id=cid, ad_id="77", role=role, ad_title="Kinderbuch", partner_name="Max",
                            ad_context={"url": "https://www.willhaben.at/iad/object?adId=77", "category": "Bücher",
                                        "shipping_price": 4.9}, **kw))
        db.commit()


async def test_each_deal_stage_is_announced_once_with_the_documented_fields(events):
    _buyer_chat()
    _notice("n1", "Preisvorschlag von € 10,00 wurde angenommen", minutes_ago=30)
    await paylivery.scan("cb1")
    await _settle()
    _notice("n2", "Zahlung eingegangen: € 10,00 über PayLivery", minutes_ago=20)
    await paylivery.scan("cb1")
    await paylivery.scan("cb1")                                  # nothing new: nothing again
    await _settle()
    first, second = events
    assert first["kind"] == "purchase" and first["push"] is False and first["title"] == "Purchase: Offer accepted"
    assert first["message"] == "Kinderbuch" and first["url"].endswith("/chats/cb1")
    d = first["data"]
    assert d["stage"] == "status" and d["source"] == "chat" and d["status"] == "offer_accepted"
    assert "from" not in d                                       # nothing came before
    assert d["conversation_id"] == "cb1" and d["ad_id"] == "77" and d["platform"] == "willhaben"
    assert d["ad_title"] == "Kinderbuch" and d["ad_url"].endswith("adId=77") and d["category"] == "Bücher"
    assert d["agreed_price"] == 10.0 and d["shipping"] == 4.9
    assert d["total"] == round(10.0 + 4.9 + fees.rule("willhaben", ["Bücher"]).fee(10.0), 2)
    assert "purchase_id" not in d
    assert second["title"] == "Purchase: Paid"
    assert second["data"]["status"] == "paid" and second["data"]["from"] == "offer_accepted"


async def test_the_same_stage_twice_is_announced_once(events):
    _buyer_chat()
    _notice("n1", "Preisvorschlag wurde angenommen", minutes_ago=10)
    _notice("n2", "Preisvorschlag ist angenommen", minutes_ago=5)
    await paylivery.scan("cb1")
    await _settle()
    assert len(events) == 1


async def test_a_stage_that_is_no_deal_is_not_announced(events):
    _buyer_chat()
    _notice("n1", "Preisvorschlag wurde abgelehnt")
    await paylivery.scan("cb1")
    await _settle()
    assert events == []


async def test_the_seller_role_gets_no_chat_purchase_event(events):
    _buyer_chat("cs1", role="seller")
    _notice("n1", "Preisvorschlag wurde angenommen", cid="cs1")
    _notice("n2", "Zahlung eingegangen: € 10,00", cid="cs1", minutes_ago=2)
    await paylivery.scan("cs1")
    await _settle()
    assert events == []


async def test_a_chat_with_an_order_leaves_the_events_to_the_order(events):
    _buyer_chat()
    _order(ad_id="77", conversation_id="cb1", status="contacting")
    _notice("n1", "Preisvorschlag wurde angenommen")
    await paylivery.scan("cb1")
    await _settle()
    assert [e for e in events if (e.get("data") or {}).get("source") == "chat"] == []
    _order(ad_id="88", conversation_id="cb2", status="done")      # a finished order of another chat counts too
    _buyer_chat("cb2")
    _notice("m1", "Preisvorschlag wurde angenommen", cid="cb2")
    await paylivery.scan("cb2")
    await _settle()
    assert [e for e in events if (e.get("data") or {}).get("source") == "chat"] == []


async def test_a_stage_from_weeks_ago_is_listed_but_not_announced(events):
    _buyer_chat()
    _notice("n1", "Zahlung eingegangen: € 10,00", minutes_ago=60 * 24 * 10)
    await paylivery.scan("cb1")
    await _settle()
    assert events == []


async def test_an_own_action_announces_too(events):
    _buyer_chat()
    with session() as db:
        c = db.get(Conversation, "cb1")
        c.paylivery = {"role": "buyer", "stage": "offer_accepted", "offer": 10.0, "rules": paylivery.RULES,
                       "history": [{"at": _ago(minutes=30), "stage": "offer_accepted", "text": "x"}]}
        db.add(c)
        db.commit()
    paylivery._record("cb1", "buy", True, "bezahlt", 10.0)
    await _settle()
    (e,) = events
    assert e["data"]["status"] == "paid" and e["data"]["from"] == "offer_accepted" and e["data"]["agreed_price"] == 10.0


def test_the_stage_before_comes_from_the_history():
    st = {"history": [{"stage": "offer_sent"}, {"stage": "offer_accepted"}, {"stage": "paid"}, {"stage": "paid"}]}
    assert purchase._stage_before(st, "paid") == "offer_accepted"
    assert purchase._stage_before({"history": [{"stage": "paid"}]}, "paid") is None
    assert purchase._stage_before({}, "paid") is None
