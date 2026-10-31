"""The AI agent's JSON API (app/agent_api_routes.py, docs/agent-api.md): bearer token next to the dashboard login,
the event feed and stream, purchase orders and sales with their costs, categories and buyers, and the MCP tools on top."""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent_api_routes, auth
from app.config import get_settings
from app.db import (AdDraft, Conversation, EventLog, MyAd, Purchase, Sale, Shipment, session, set_setting)
from app.main import app
from app.platforms import fees
from app.services import agent_events, purchase

TOKEN = "t0ken-for-the-agent"
BEARER = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locked(monkeypatch, tmp_path):
    """The dashboard has a password and the agent an API token."""
    monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_setting(agent_events.SETTING_TOKEN, TOKEN)


def _order(**kw) -> int:
    base = dict(ad_id="2000000001", ad_title="Bosch Geschirrspüler", ad_url="https://www.willhaben.at/iad/x",
                image_url="https://cache.willhaben.at/x.jpg", asking_price=250.0, max_price=280.0, mode="paylivery",
                status="negotiating", log=[{"at": "2026-10-09T08:00:00", "status": "negotiating", "text": "Gegenangebot"}])
    base.update(kw)
    with session() as db:
        row = Purchase(**base)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def _event(kind="purchase", **kw) -> int:
    with session() as db:
        e = EventLog(kind=kind, message=kw.pop("message", f"{kind} happened"), **kw)
        db.add(e)
        db.commit()
        db.refresh(e)
        return e.id


# ------------------------------------------------------------------ authentication
def test_bearer_opens_the_api_only(client, locked):
    assert client.get("/api/purchases").status_code == 401
    assert client.get("/api/purchases", headers=BEARER).status_code == 200
    assert client.get("/api/events", headers=BEARER).status_code == 200
    assert client.get("/myads", headers={**BEARER, "accept": "text/html"}, follow_redirects=False).status_code == 303
    assert client.get("/myads", headers=BEARER).status_code == 401              # not even as a script


def test_wrong_or_empty_token_is_refused(client, locked):
    assert client.get("/api/purchases", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/purchases", headers={"authorization": "Bearer "}).status_code == 401
    assert client.get("/api/purchases", headers={"authorization": f"Basic {TOKEN}"}).status_code == 401
    set_setting(agent_events.SETTING_TOKEN, "")                                 # no token set: no bearer access
    assert client.get("/api/purchases", headers={"authorization": "Bearer "}).status_code == 401
    assert client.get("/api/purchases", headers=BEARER).status_code == 401


def test_basic_auth_keeps_working(client, locked):
    basic = base64.b64encode(b"admin:geheim").decode()
    assert client.get("/api/purchases", headers={"authorization": f"Basic {basic}"}).status_code == 200


def test_valid_bearer():
    set_setting(agent_events.SETTING_TOKEN, TOKEN)
    assert auth.valid_bearer(f"Bearer {TOKEN}") and auth.valid_bearer(f"bearer {TOKEN}")
    assert not auth.valid_bearer("Bearer") and not auth.valid_bearer(f"Bearer {TOKEN}x") and not auth.valid_bearer("")
    set_setting(agent_events.SETTING_TOKEN, "")
    assert not auth.valid_bearer("Bearer ") and not auth.valid_bearer("Bearer x")


# ------------------------------------------------------------------ events
def test_events_page_with_since_and_kinds(client):
    ids = [_event("purchase", message="p1", data={"purchase_id": 1}), _event("hit", message="h1"),
           _event("sale", message="s1"), _event("purchase", message="p2")]
    body = client.get("/api/events").json()
    assert [e["id"] for e in body["events"]] == ids and body["next_since"] == ids[-1]
    body = client.get("/api/events", params={"since": ids[0], "limit": 2}).json()
    assert [e["id"] for e in body["events"]] == ids[1:3] and body["next_since"] == ids[2]
    body = client.get("/api/events", params={"kind": "purchase,sale"}).json()
    assert [e["kind"] for e in body["events"]] == ["purchase", "sale", "purchase"]
    assert body["events"][0]["data"] == {"purchase_id": 1}
    body = client.get("/api/events", params={"since": ids[-1]}).json()
    assert body == {"events": [], "next_since": ids[-1]}                         # nothing new: the cursor stays
    assert client.get("/api/events", params={"limit": 2}).json()["events"][0]["id"] == ids[2]   # latest two


class _Request:
    """A request that stays connected for `alive` looks."""
    def __init__(self, alive):
        self.alive = alive

    async def is_disconnected(self):
        self.alive -= 1
        return self.alive < 0


def _frames(request, since, kinds=None):
    async def run():
        return [f async for f in agent_api_routes._event_stream(request, since, kinds)]
    return asyncio.run(run())


def test_stream_emits_events_pings_and_resumes(monkeypatch):
    monkeypatch.setattr(agent_api_routes, "POLL_S", 0.01)
    first = _event("purchase", message="p1")
    second = _event("sale", message="s1", data={"price": 99.0})
    frames = _frames(_Request(2), first - 1)
    assert frames[0].startswith(": connected")
    body = [f for f in frames if f.startswith("id:")]
    assert [f.split("\n")[0] for f in body] == [f"id: {first}", f"id: {second}"]
    assert body[1].split("\n")[1] == "event: sale"
    assert json.loads(body[1].split("\n")[2][len("data: "):])["data"] == {"price": 99.0}
    assert body[1].endswith("\n\n")
    assert [f for f in _frames(_Request(1), first) if f.startswith("id:")][0].startswith(f"id: {second}")   # resumes after the id
    assert not [f for f in _frames(_Request(1), None) if f.startswith("id:")]                  # no cursor: only new ones
    monkeypatch.setattr(agent_api_routes, "PING_S", 0.0)
    assert ": ping\n\n" in _frames(_Request(2), second)


def test_stream_filters_kinds_and_pages_through_a_backlog(monkeypatch):
    monkeypatch.setattr(agent_api_routes, "POLL_S", 0.01)
    monkeypatch.setattr(agent_api_routes, "STREAM_BATCH", 2)
    ids = [_event("hit"), _event("sale"), _event("hit"), _event("sale"), _event("sale")]
    frames = [f for f in _frames(_Request(3), 0, ["sale"]) if f.startswith("id:")]
    assert [int(f.split("\n")[0][4:]) for f in frames] == [ids[1], ids[3], ids[4]]


def test_stream_route_over_http(client, monkeypatch):
    """The real route, with an event that exists before the first look (Last-Event-ID resumes after the one before)."""
    monkeypatch.setattr(agent_api_routes, "POLL_S", 0.01)
    before = _event("purchase", message="old")
    wanted = _event("sale", message="s1")

    async def one_look(request, since, kinds):                      # the route's wiring: header, query, content type
        yield f"seen {since} {kinds}\n\n"
        yield agent_api_routes._sse(agent_events.events_since(since, 1, kinds)[0])
    monkeypatch.setattr(agent_api_routes, "_event_stream", one_look)
    with client.stream("GET", "/api/events/stream", params={"kind": "sale"}, headers={"last-event-id": str(before)}) as r:
        assert r.headers["content-type"].startswith("text/event-stream") and r.headers["cache-control"] == "no-cache"
        text = "".join(r.iter_text())
    assert f"seen {before} ['sale']" in text and f"id: {wanted}\nevent: sale\n" in text


# ------------------------------------------------------------------ purchases
def test_purchase_list_costs_category_and_filters(client):
    ad_rule = fees.RULES["willhaben"]
    first = _order(category="Elektronik > Audio > Kopfhörer", shipping_cost=7.9, shipping_source="ad",
                   status="paid", agreed_price=200.0, fee=ad_rule.as_dict())
    old = _order(ad_id="2000000002", ad_title="Alt", status="done", mode="pickup", asking_price=40.0, max_price=40.0,
                 fee={**ad_rule.as_dict(), "category": "Haushalt"})                  # before Purchase.category existed
    direct = _order(ad_id="2000000003", mode="direct", status="pay_now", asking_price=100.0, agreed_price=90.0,
                    shipping_cost=5.0, shipping_source="chat", agreed_total=96.0, wish_price=80.0)
    noprice = _order(ad_id="2000000004", asking_price=None, status="new")
    rows = client.get("/api/purchases").json()
    assert [r["id"] for r in rows] == [noprice, direct, old, first]              # newest change first
    by_id = {r["id"]: r for r in rows}

    p = by_id[first]
    assert p["category"] == "Elektronik > Audio > Kopfhörer" and p["platform"] == "willhaben"
    assert p["ad_title"] == "Bosch Geschirrspüler" and p["image_url"] and p["ad_url"]
    fee = ad_rule.fee(200.0)
    assert p["shipping"] == 7.9 and p["shipping_source"] == "ad" and p["fee"] == fee
    assert p["total"] == round(200.0 + 7.9 + fee, 2)
    assert p["total"] == purchase.costs_of(Purchase(mode="paylivery", shipping_cost=7.9, shipping_source="ad",
                                                    fee=ad_rule.as_dict()), 200.0)["total"]
    assert (p["asking_price"], p["max_price"], p["agreed_price"], p["mode"], p["status"]) == (250.0, 280.0, 200.0,
                                                                                            "paylivery", "paid")
    assert p["app_path"] == f"/purchases/{first}" and p["app_url"] is None        # no reachable web address
    assert p["created_at"].endswith("+00:00") and p["updated_at"].endswith("+00:00")

    assert by_id[old]["category"] == "Haushalt"                                  # the fee rule's category
    assert (by_id[old]["shipping"], by_id[old]["fee"], by_id[old]["total"]) == (0.0, 0.0, 40.0)   # pickup
    d = by_id[direct]
    assert (d["shipping"], d["fee"], d["total"], d["wish_price"], d["category"]) == (5.0, 0.0, 96.0, 80.0, None)
    assert (by_id[noprice]["fee"], by_id[noprice]["total"]) == (None, None)

    assert [r["id"] for r in client.get("/api/purchases", params={"status": "paid"}).json()] == [first]
    assert [r["id"] for r in client.get("/api/purchases", params={"active": 1}).json()] == [noprice, direct, first]
    assert [r["id"] for r in client.get("/api/purchases", params={"active": 0}).json()] == [old]
    assert len(client.get("/api/purchases", params={"limit": 2}).json()) == 2
    assert client.get("/api/purchases", params={"status": "bogus"}).status_code == 400


def test_purchase_app_url_with_reachable_web_address(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_url", "https://agent.example.org/")
    pid = _order()
    assert client.get("/api/purchases").json()[0]["app_url"] == f"https://agent.example.org/purchases/{pid}"


def test_purchase_platform_from_the_conversation(client):
    with session() as db:
        db.add(Conversation(id="c1", platform="vinted", ad_id="2000000001"))
        db.commit()
    _order(conversation_id="c1")
    assert client.get("/api/purchases").json()[0]["platform"] == "vinted"


def test_purchase_detail(client):
    pid = _order(note="Verkäufer fragt nach Abholung", conversation_id="abc-def", questions=[{"question": "Rechnung?", "answer": "ja"}],
                 pickup={"start": "2026-10-12T17:00", "minutes": 20, "location": "Graz"},
                 payment={"iban": "AT611904300234573201", "amount": 90.0}, market={"median": 220.0, "count": 9})
    body = client.get(f"/api/purchases/{pid}").json()
    assert body["id"] == pid and body["total"] is not None
    assert body["log"] == [{"at": "2026-10-09T08:00:00", "status": "negotiating", "text": "Gegenangebot"}]
    assert body["questions"] == [{"question": "Rechnung?", "answer": "ja"}] and body["pickup"]["location"] == "Graz"
    assert body["payment"]["amount"] == 90.0 and body["market"]["median"] == 220.0
    assert body["note"] == "Verkäufer fragt nach Abholung" and body["conversation_id"] == "abc-def"
    assert client.get("/api/purchases/9999").status_code == 404


# ------------------------------------------------------------------ sales
def _sale_world() -> dict[str, int]:
    """Ad A (4 pieces, renewed as B, published from a draft) with two sales, one taken back; a single item C sold
    outside willhaben."""
    now = datetime.now(timezone.utc)
    with session() as db:
        db.add(MyAd(ad_id="A", heading="Kopfhörer alt", url="https://www.willhaben.at/iad/A", image_url="a.jpg",
                    quantity=4, replaced_by="B"))
        db.add(MyAd(ad_id="B", heading="Kopfhörer", url="https://www.willhaben.at/iad/B", image_url="b.jpg", quantity=4))
        db.add(MyAd(ad_id="C", heading="Fahrrad", url="https://www.willhaben.at/iad/C"))
        db.add(AdDraft(input_text="Kopfhörer", status="published", wh_ad_id="B", category_hint="Elektronik > Audio",
                       wh_plan={"selected": 1, "options": [
                           {"path": [{"label": "Egal", "code": "1"}]},
                           {"path": [{"label": "Elektronik", "code": "2"}, {"label": "Audio", "code": "3"},
                                     {"label": "Kopfhörer", "code": "4"}]}]}))
        db.add(Conversation(id="conv-1", ad_id="A", partner_name="Maria K."))
        db.add(Shipment(conversation_id="conv-1", ad_id="A", status="labelled", paid_via="paylivery",
                        paid_at=now - timedelta(hours=1), tracking="1Z999"))
        db.add(Shipment(conversation_id="conv-1", ad_id="A", status="discarded"))      # not the parcel that counts
        s1 = Sale(ad_id="A", conversation_id="conv-1", pieces=1, price=60.0, note="paylivery",
                  created_at=now - timedelta(days=2))
        s2 = Sale(ad_id="B", pieces=2, price=100.0, created_at=now - timedelta(days=1), undone_at=now)
        s3 = Sale(ad_id="C", price=150.0, note="Abholung", created_at=now - timedelta(hours=3))
        db.add_all([s1, s2, s3])
        db.commit()
        return {"s1": s1.id, "s2": s2.id, "s3": s3.id}


def test_sales_with_buyer_shipment_category_and_stock(client):
    ids = _sale_world()
    rows = client.get("/api/sales").json()
    assert [r["id"] for r in rows] == [ids["s3"], ids["s1"]]                     # newest first, the undone one is out
    bike, phones = rows
    assert (bike["ad_title"], bike["category"], bike["buyer"], bike["shipment"], bike["pieces_left"]) == (
        "Fahrrad", None, None, None, None)
    assert bike["price"] == 150.0 and bike["note"] == "Abholung" and bike["conversation_id"] is None
    assert phones["ad_id"] == "A" and phones["ad_title"] == "Kopfhörer alt" and phones["image_url"] == "a.jpg"
    assert phones["category"] == "Elektronik > Audio > Kopfhörer"               # the draft of the renewed copy B
    assert (phones["pieces"], phones["price"], phones["buyer"], phones["conversation_id"]) == (1, 60.0, "Maria K.", "conv-1")
    assert phones["pieces_left"] == 3                                            # 4 - the one that counts, over the chain
    assert phones["shipment"]["status"] == "labelled" and phones["shipment"]["paid_via"] == "paylivery"
    assert phones["shipment"]["tracking"] == "1Z999" and phones["shipment"]["paid_at"].endswith("+00:00")
    assert phones["undone_at"] is None and phones["created_at"].endswith("+00:00")


def test_sales_include_undone_and_limit(client):
    ids = _sale_world()
    rows = client.get("/api/sales", params={"include_undone": 1}).json()
    assert [r["id"] for r in rows] == [ids["s3"], ids["s2"], ids["s1"]]
    undone = rows[1]
    assert undone["undone_at"] and undone["ad_title"] == "Kopfhörer" and undone["pieces"] == 2
    assert [r["id"] for r in client.get("/api/sales", params={"include_undone": 1, "limit": 1}).json()] == [ids["s3"]]


def test_sale_category_falls_back_to_the_hint(client):
    with session() as db:
        db.add(MyAd(ad_id="D", heading="Lampe"))
        db.add(AdDraft(input_text="Lampe", wh_ad_id="D", category_hint="Haushalt > Beleuchtung"))
        db.add(Sale(ad_id="D", price=10.0))
        db.commit()
    assert client.get("/api/sales").json()[0]["category"] == "Haushalt > Beleuchtung"


# ------------------------------------------------------------------ MCP tools
def test_mcp_tools_call_the_api(monkeypatch):
    from app import mcp_server
    calls = []

    async def fake(method, path, **kw):
        calls.append((method, path, kw.get("params")))
        return {"ok": True}
    monkeypatch.setattr(mcp_server, "_call", fake)

    async def run():
        await mcp_server.get_events(since=5, limit=10, kind="purchase,sale")
        await mcp_server.get_events()
        await mcp_server.list_purchases(status="paid", active=True, limit=3)
        await mcp_server.list_purchases(active=False)
        await mcp_server.get_purchase(7)
        await mcp_server.list_sales(limit=4)
    asyncio.run(run())
    assert calls == [
        ("GET", "/api/events", {"limit": 10, "since": 5, "kind": "purchase,sale"}),
        ("GET", "/api/events", {"limit": 50}),
        ("GET", "/api/purchases", {"limit": 3, "status": "paid", "active": 1}),
        ("GET", "/api/purchases", {"limit": 20, "active": 0}),
        ("GET", "/api/purchases/7", None),
        ("GET", "/api/sales", {"limit": 4}),
    ]


def test_mcp_tools_are_read_only_and_the_token_becomes_a_bearer_header(monkeypatch):
    import importlib

    from app import mcp_server
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("get_events", "list_purchases", "get_purchase", "list_sales"):
        assert tools[name].annotations.read_only_hint is True and tools[name].description
    monkeypatch.setenv("WH_AGENT_TOKEN", "abc")
    monkeypatch.setenv("WH_AGENT_PASSWORD", "pw")
    try:
        fresh = importlib.reload(mcp_server)
        assert fresh.HEADERS["authorization"] == "Bearer abc" and fresh._TOKEN == "abc"
    finally:
        monkeypatch.delenv("WH_AGENT_TOKEN")
        monkeypatch.delenv("WH_AGENT_PASSWORD")
        importlib.reload(mcp_server)
    assert "authorization" not in mcp_server.HEADERS


# ------------------------------------------------------------------ stopping a purchase
def test_cancel_stops_the_auto_buy_countdown_first(client, locked, monkeypatch):
    from app.services import jobs
    pid = _order(status="new")
    asked = []
    monkeypatch.setattr(jobs, "cancel_key", lambda key: asked.append(key) or True)
    r = client.post(f"/api/purchases/{pid}/cancel", headers=BEARER)
    assert r.status_code == 200 and r.json() == {"ok": True, "stopped": "countdown"}
    assert asked == [f"purchase:{pid}"]


def test_cancel_an_open_order_and_refuse_a_finished_one(client, locked):
    pid = _order()
    r = client.post(f"/api/purchases/{pid}/cancel", headers=BEARER)
    assert r.json() == {"ok": True, "stopped": "order"}
    with session() as db:
        assert db.get(Purchase, pid).status == "cancelled"
    assert client.post(f"/api/purchases/{pid}/cancel", headers=BEARER).status_code == 409
    assert client.post("/api/purchases/99999/cancel", headers=BEARER).status_code == 404
    assert client.post(f"/api/purchases/{pid}/cancel").status_code == 401           # no token, no login
