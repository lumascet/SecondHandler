import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from sqlmodel import select

from app.db import EventLog, WebhookDelivery, get_setting, log_event, session, set_setting, utcnow
from app.services import agent_events as ae, notify as notify_mod

URL = "https://agent.example.org/hook"


def valid(secret: str, headers, body: bytes) -> bool:      # the snippet from docs/agent-api.md
    msg = headers["X-SecondHandler-Timestamp"].encode() + b"." + body
    sig = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, headers["X-SecondHandler-Signature"])


@pytest.fixture
def hook(monkeypatch):
    """The agent's webhook: every request lands in `calls`; `answer` is the status (or an exception) it gives."""
    calls: list[httpx.Request] = []
    state = {"answer": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return httpx.Response(state["answer"], text="x")

    monkeypatch.setattr(ae, "_transport", httpx.MockTransport(handler))
    set_setting(ae.SETTING_WEBHOOK_URL, URL)
    hook_ = type("Hook", (), {})()
    hook_.calls, hook_.state = calls, state
    return hook_


def deliveries() -> list[WebhookDelivery]:
    with session() as db:
        return list(db.exec(select(WebhookDelivery).order_by(WebhookDelivery.id)))


def age(delivery_id: int, **ago) -> None:
    """Make a delivery older / due again."""
    with session() as db:
        d = db.get(WebhookDelivery, delivery_id)
        d.created_at = utcnow() - timedelta(**ago) if ago else d.created_at
        d.next_at = utcnow() - timedelta(seconds=1)
        db.commit()


# --- serialize / events_since ----------------------------------------------------------------------------------------

def test_serialize_uses_title_and_data():
    eid = log_event("purchase", "Bought: Sony: 180 €", "https://willhaben.at/x", ad_id="1", title="Bought: Sony",
                    data={"stage": "bought", "total": 180.0})
    with session() as db:
        e = db.get(EventLog, eid)
    out = ae.serialize(e)
    assert out["title"] == "Bought: Sony" and out["message"] == "180 €" and out["kind"] == "purchase"
    assert out["data"] == {"stage": "bought", "total": 180.0} and out["quiet"] is False and out["ad_id"] == "1"
    assert out["ts"].endswith("+00:00") and out["id"] == eid and out["conversation_id"] is None


def test_serialize_old_row_splits_message():
    eid = log_event("hit", "New hit: Pi 4", None)
    with session() as db:
        out = ae.serialize(db.get(EventLog, eid))
    assert (out["title"], out["message"], out["data"], out["quiet"]) == ("New hit", "Pi 4", {}, False)


def test_serialize_app_url_only_when_reachable(monkeypatch):
    monkeypatch.setattr(notify_mod, "app_link", lambda kind, url, data=None: f"https://app/{kind}/{data['ad_id']}")
    eid = log_event("hit", "T: m", None, ad_id="42", quiet=True)
    with session() as db:
        e = db.get(EventLog, eid)
    monkeypatch.setattr(notify_mod, "_reachable", lambda: False)
    assert ae.serialize(e)["app_url"] is None
    monkeypatch.setattr(notify_mod, "_reachable", lambda: True)
    out = ae.serialize(e)
    assert out["app_url"] == "https://app/hit/42" and out["quiet"] is True


def test_events_since_paging_and_kinds():
    ids = [log_event("hit" if i % 2 else "sale", f"T{i}: m", None, quiet=i == 3) for i in range(1, 7)]
    assert [e["id"] for e in ae.events_since(limit=3)] == ids[-3:]            # latest 3, oldest first
    assert [e["id"] for e in ae.events_since(ids[1], limit=2)] == ids[2:4]    # after, oldest first, includes quiet
    assert [e["id"] for e in ae.events_since(ids[-1])] == []
    assert [e["id"] for e in ae.events_since(0, kinds=["sale"])] == [ids[1], ids[3], ids[5]]
    assert len(ae.events_since(limit=0)) == 1 and len(ae.events_since(limit=10_000)) == 6


def test_kinds_setting():
    assert ae.kinds() == ["purchase", "sale", "paylivery", "shipping", "message"]
    set_setting(ae.SETTING_KINDS, " hit, ,sale ,")
    assert ae.kinds() == ["hit", "sale"]
    assert ae.new_token() != ae.new_token() and len(ae.new_secret()) >= 40


# --- dispatch ----------------------------------------------------------------------------------------------------------

def test_dispatch_filters_by_kind_and_url(hook):
    eid = log_event("hit", "T: m", None)
    ae.dispatch(eid, "hit")                               # not a selected kind
    assert deliveries() == []
    set_setting(ae.SETTING_WEBHOOK_URL, "")
    ae.dispatch(eid, "sale")                              # no URL
    assert deliveries() == []
    set_setting(ae.SETTING_WEBHOOK_URL, URL)
    ae.dispatch(None, "sale")
    assert deliveries() == []
    ae.dispatch(eid, "sale")                              # no event loop in a sync test: the scheduler picks it up
    assert [d.event_id for d in deliveries()] == [eid] and hook.calls == []


async def test_dispatch_sends_right_away(hook):
    eid = log_event("sale", "Sold: Pi", None, title="Sold", data={"sale_id": 3})
    ae.dispatch(eid, "sale")
    await asyncio.gather(*ae._tasks)
    assert len(hook.calls) == 1 and deliveries()[0].delivered_at is not None


# --- delivery ----------------------------------------------------------------------------------------------------------

async def test_delivery_is_signed(hook):
    eid = log_event("purchase", "Bought: Sony: ünïcode €", "https://x", title="Bought: Sony", data={"stage": "bought"})
    ae.dispatch(eid, "purchase")
    assert await ae.deliver_due() == 1
    req = hook.calls[0]
    body = req.content
    assert req.method == "POST" and str(req.url) == URL
    assert req.headers["X-SecondHandler-Event"] == "purchase"
    assert req.headers["X-SecondHandler-Delivery"] == str(deliveries()[0].id)
    assert valid(get_setting(ae.SETTING_WEBHOOK_SECRET), req.headers, body)
    assert not valid("other secret", req.headers, body) and not valid(get_setting(ae.SETTING_WEBHOOK_SECRET),
                                                                      req.headers, body + b" ")
    sent = json.loads(body)
    assert sent["id"] == eid and sent["message"] == "ünïcode €" and sent["data"] == {"stage": "bought"}
    d = deliveries()[0]
    assert d.delivered_at and d.attempts == 0 and d.last_error is None
    assert await ae.deliver_due() == 0 and len(hook.calls) == 1              # delivered ones are not sent again


async def test_secret_is_created_once(hook):
    assert get_setting(ae.SETTING_WEBHOOK_SECRET) == ""
    first = ae.ensure_secret()
    assert first and ae.ensure_secret() == first


@pytest.mark.parametrize("answer, status, error", [
    (500, 500, "HTTP 500"),
    (httpx.ReadTimeout("slow"), None, "no answer"),
    (httpx.ConnectError("refused"), None, "refused"),
])
async def test_failures_back_off(hook, answer, status, error):
    hook.state["answer"] = answer
    ae.dispatch(log_event("sale", "T: m", None), "sale")
    schedule = [30, 120, 600, 1800, 3600, 3600]
    for n, delay in enumerate(schedule, 1):
        before = utcnow()
        assert await ae.deliver_due() == 0
        d = deliveries()[0]
        assert d.attempts == n and d.last_status == status and error in d.last_error and d.failed_at is None
        wait = (d.next_at.replace(tzinfo=before.tzinfo) - before).total_seconds()
        assert delay - 1 <= wait <= delay + 5
        assert await ae.deliver_due() == 0 and len(hook.calls) == n          # not due yet: nothing sent
        age(d.id)
    hook.state["answer"] = 200
    assert await ae.deliver_due() == 1                                       # the same delivery id on retries
    assert {r.headers["X-SecondHandler-Delivery"] for r in hook.calls} == {str(d.id)}
    assert deliveries()[0].delivered_at


async def test_gives_up_after_24_hours(hook):
    hook.state["answer"] = 500
    ae.dispatch(log_event("sale", "T: m", None), "sale")
    age(deliveries()[0].id, hours=23)
    await ae.deliver_due()
    assert deliveries()[0].failed_at is None
    age(deliveries()[0].id, hours=25)
    await ae.deliver_due()
    d = deliveries()[0]
    assert d.failed_at and d.attempts == 2
    n = len(hook.calls)
    age(d.id)
    await ae.deliver_due()
    assert len(hook.calls) == n                                              # given up: not tried again


async def test_event_gone_and_url_removed(hook):
    eid = log_event("sale", "T: m", None)
    ae.dispatch(eid, "sale")
    with session() as db:
        db.delete(db.get(EventLog, eid))
        db.commit()
    assert await ae.deliver_due() == 0
    assert deliveries()[0].failed_at and "no longer" in deliveries()[0].last_error and hook.calls == []
    ae.dispatch(log_event("sale", "T2: m", None), "sale")
    set_setting(ae.SETTING_WEBHOOK_URL, "")
    assert await ae.deliver_due() == 0 and deliveries()[1].failed_at and hook.calls == []


async def test_concurrent_runs_send_once(hook, monkeypatch):
    with session() as db:                       # queued, but no task of dispatch() is racing the runs below
        for i in range(3):
            db.add(WebhookDelivery(event_id=log_event("sale", f"T{i}: m", None)))
        db.commit()
    gate = asyncio.Event()
    orig = ae._post

    async def slow(*a, **kw):
        await gate.wait()
        return await orig(*a, **kw)

    monkeypatch.setattr(ae, "_post", slow)
    runs = [asyncio.create_task(ae.deliver_due()) for _ in range(4)]
    await asyncio.sleep(0.05)
    gate.set()
    assert sorted(await asyncio.gather(*runs)) == [0, 0, 0, 3]
    assert len(hook.calls) == 3


async def test_old_finished_rows_are_pruned(hook):
    ae.dispatch(log_event("sale", "T: m", None), "sale")
    await ae.deliver_due()
    with session() as db:
        d = db.get(WebhookDelivery, deliveries()[0].id)
        d.delivered_at = utcnow() - timedelta(days=8)
        db.commit()
    await ae.deliver_due()
    assert deliveries() == []


# --- test button / status ----------------------------------------------------------------------------------------------

async def test_send_test(hook):
    assert await ae.send_test() == {"ok": True, "status": 200, "error": None}
    req = hook.calls[0]
    sent = json.loads(req.content)
    assert (sent["id"], sent["kind"], sent["title"], sent["data"]) == (0, "test", "Test", {"test": True})
    assert req.headers["X-SecondHandler-Event"] == "test"
    assert valid(get_setting(ae.SETTING_WEBHOOK_SECRET), req.headers, req.content)
    hook.state["answer"] = 404
    out = await ae.send_test()
    assert out["ok"] is False and out["status"] == 404 and "404" in out["error"]
    hook.state["answer"] = httpx.ConnectTimeout("t")
    assert (await ae.send_test())["status"] is None
    set_setting(ae.SETTING_WEBHOOK_URL, "")
    out = await ae.send_test()
    assert out["ok"] is False and out["error"] and len(hook.calls) == 3


async def test_webhook_status(hook):
    assert ae.webhook_status() == {"url": URL, "pending": 0, "failed": 0, "last_delivered_at": None,
                                   "last_attempt_at": None, "last_error": None}
    hook.state["answer"] = 500
    ae.dispatch(log_event("sale", "A: m", None), "sale")
    await ae.deliver_due()
    st = ae.webhook_status()
    assert st["pending"] == 1 and st["failed"] == 0 and st["last_delivered_at"] is None
    assert "500" in st["last_error"] and st["last_attempt_at"] is not None
    age(deliveries()[0].id)
    hook.state["answer"] = 200
    await ae.deliver_due()
    st = ae.webhook_status()
    assert st["pending"] == 0 and st["last_delivered_at"] and st["last_error"] is None
    hook.state["answer"] = 500
    ae.dispatch(log_event("sale", "B: m", None), "sale")
    age(deliveries()[1].id, hours=30)
    await ae.deliver_due()
    st = ae.webhook_status()
    assert st["failed"] == 1 and st["pending"] == 0 and "500" in st["last_error"]


# --- notify() -----------------------------------------------------------------------------------------------------------

async def test_notify_dispatches_without_ha_or_telegram(hook):
    assert await notify_mod.notify("sale", "Sold", "Pi for 50 €", data={"sale_id": 1, "price": 50}) is False
    await asyncio.gather(*ae._tasks)
    with session() as db:
        e = db.exec(select(EventLog)).one()
    assert (e.title, e.data) == ("Sold", {"sale_id": 1, "price": 50}) and e.message == "Sold: Pi for 50 €"
    assert len(hook.calls) == 1 and json.loads(hook.calls[0].content)["data"] == {"sale_id": 1, "price": 50}


async def test_notify_quiet_events_are_dispatched_too(hook):
    await notify_mod.notify("sale", "Sold", "by hand", push=False)
    await asyncio.gather(*ae._tasks)
    assert json.loads(hook.calls[0].content)["quiet"] is True


async def test_notify_survives_dispatch_failure(hook, monkeypatch):
    def boom(*a):
        raise RuntimeError("broken")

    monkeypatch.setattr(ae, "dispatch", boom)
    await notify_mod.notify("sale", "Sold", "x")
    with session() as db:
        assert db.exec(select(EventLog)).one().kind == "sale"
    assert hook.calls == []
