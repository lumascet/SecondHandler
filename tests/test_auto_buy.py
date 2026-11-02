"""Auto-buy: a passing hit within the agent's maximum is bought with PayLivery's buy-now after a countdown that
Telegram (or the jobs list) can stop; the price is checked again right before buying."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Hit, SearchAgent, session
from app.platforms.types import AdDetail
from app.services import auto_buy, jobs, purchase


@pytest.fixture(autouse=True)
def purchases_on():
    from app.db import set_setting
    set_setting(purchase.SETTING_ENABLED, "1")
    yield
    set_setting(purchase.SETTING_ENABLED, "")


@pytest.fixture
def agent():
    with session() as db:
        a = SearchAgent(name="Homepod", source_url="https://www.willhaben.at/iad/x", webapi_url="x",
                        auto_buy=True, auto_buy_max=60.0)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a


def _hit(agent, ad_id="777", price=40.0, **kw):
    with session() as db:
        h = Hit(agent_id=agent.id, ad_id=ad_id, heading="HomePod mini", url=f"https://www.willhaben.at/iad/{ad_id}",
                state="new", price=price, shipping_price=3.49, **kw)
        db.add(h)
        db.commit()
        db.refresh(h)
        return h


def _detail(ad_id="777", price=40.0, buy_now=True):
    return AdDetail(ad_id=ad_id, heading="HomePod mini", description="", price=price, price_display=f"€ {price:g}",
                    status="active", url=f"https://www.willhaben.at/iad/{ad_id}", paylivery_enabled=True,
                    buy_now=buy_now, shipping_price=3.49, seller_name="Verkäufer", published_at=None,
                    first_published_at=None, end_date=None)


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def notify(kind, title, message, **kw):
        sent.append({"kind": kind, "title": title, **kw})
        return True
    monkeypatch.setattr("app.services.notify.notify", notify)
    monkeypatch.setattr(purchase, "_push", lambda *a, **k: asyncio.sleep(0))
    return sent


def test_which_hits_it_buys(agent):
    assert auto_buy.candidate(agent, _hit(agent, "1", 40.0))
    assert not auto_buy.candidate(agent, _hit(agent, "2", 80.0))                  # above the maximum
    assert not auto_buy.candidate(agent, _hit(agent, "3", 40.0, screen_relevant=False))
    assert not auto_buy.candidate(agent, _hit(agent, "4", 40.0, screen_issues=["scam_suspect"]))
    assert not auto_buy.candidate(agent, _hit(agent, "5", 40.0, platform="vinted"))   # not yet
    agent.auto_buy = False
    assert not auto_buy.candidate(agent, _hit(agent, "6", 40.0))


async def test_the_countdown_and_telegrams_cancel(agent, pushes, monkeypatch):
    """The order waits COUNTDOWN_S; Telegram's notice has a Cancel button; cancelling leaves nothing bought."""
    from app.services import telegram
    h = _hit(agent)
    pid = await auto_buy.schedule(agent, h)
    assert purchase.get(pid).status == "new" and purchase.get(pid).max_price == 60.0
    job = next(j for j in jobs._active if j.current == f"purchase:{pid}")
    assert job.status == "queued" and job.runs_at is not None and job.irreversible
    assert pushes[0]["kind"] == "purchase" and pushes[0]["data"]["auto_buy"]
    monkeypatch.setattr(telegram, "_button", lambda text, verb, ref: {"text": text, "verb": verb, "ref": ref})
    rows = telegram.keyboard("purchase", pushes[0]["data"], [], None)["inline_keyboard"]
    assert any(b["verb"] == "AB_STOP" and b["ref"] == str(pid) for r in rows for b in r)
    assert jobs.cancel_key(f"purchase:{pid}")
    assert purchase.get(pid).status == "cancelled"
    assert await auto_buy.schedule(agent, h) is None                    # cancelled: this ad is not bought again


async def test_it_buys_after_checking_the_ad_again(agent, pushes, monkeypatch):
    from app import platforms
    from app.services import paylivery
    calls = []

    async def detail(ad_id, url=None):
        return _detail(ad_id)

    async def run_action(cid, action, amount=None, claimed=False):
        calls.append((cid, action, amount))
        paylivery._record(cid, action, True, "ok", amount)     # as the real one: the chat's stage, the order later
        return "ok"
    monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
    monkeypatch.setattr(paylivery, "run_action", run_action)
    pid = await auto_buy.schedule(agent, _hit(agent))
    assert await auto_buy.buy(pid) == "Bought"
    assert calls == [("autobuy-777", "buy", 40.0)]
    with session() as db:
        conv = db.get(Conversation, "autobuy-777")
        assert conv.role == "buyer" and conv.removed and conv.ad_context["buy_now"]
    row = purchase.get(pid)
    assert row.status == "paid" and row.agreed_price == 40.0 and row.agreed_total <= 60.0
    assert any(p["title"].startswith("Bought") for p in pushes)
    assert not any(p["title"].startswith("Purchase paused") for p in pushes)
    with session() as db:
        assert db.get(SearchAgent, agent.id).auto_buy is False              # one purchase: the switch goes off


async def test_a_higher_price_or_no_buy_now_stops_it(agent, pushes, monkeypatch):
    from app import platforms
    from app.services import paylivery
    asked = []
    monkeypatch.setattr(paylivery, "run_action", lambda *a, **k: asked.append(a))
    for ad_id, d in (("771", _detail("771", price=70.0)), ("772", _detail("772", buy_now=False))):
        async def detail(ad_id, url=None, d=d):
            return d
        monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
        pid = await auto_buy.schedule(agent, _hit(agent, ad_id=ad_id))
        await auto_buy.buy(pid)
        assert purchase.get(pid).status == "failed"
    assert asked == []                                                   # nothing was bought


async def test_willhabens_real_chat_takes_the_order_over(agent, pushes):
    pid = await auto_buy.schedule(agent, _hit(agent))
    purchase._change(pid, lambda r: (setattr(r, "conversation_id", "autobuy-777"),
                                     purchase._move(r, "pay_now", "Buying")))
    with session() as db:
        db.add(Conversation(id="real-chat", role="buyer", ad_id="777", platform="willhaben"))
        db.commit()
        conv = db.get(Conversation, "real-chat")
    await purchase.on_paylivery_stage(conv, "paid", {})
    row = purchase.get(pid)
    assert row.conversation_id == "real-chat" and row.status == "paid"


def test_the_agent_form_saves_it_and_another_form_leaves_it(agent):
    from app.main import app
    form = {"name": "Homepod", "interval_min": "30"}
    with TestClient(app) as c:
        c.post(f"/agents/{agent.id}/edit", data={**form, "auto_buy_shown": "1", "auto_buy_on": "on", "auto_buy_max": "55,5"})
        with session() as db:
            a = db.get(SearchAgent, agent.id)
            assert a.auto_buy and a.auto_buy_max == 55.5
        c.post(f"/agents/{agent.id}/edit", data=form)                   # a form without the fields
        with session() as db:
            assert db.get(SearchAgent, agent.id).auto_buy
        r = c.post(f"/agents/{agent.id}/edit", data={**form, "auto_buy_shown": "1", "auto_buy_on": "on"},
                   follow_redirects=False)
        assert "error=" in r.headers["location"]                           # no maximum: refused
        c.post(f"/agents/{agent.id}/edit", data={**form, "auto_buy_shown": "1"})
        with session() as db:
            assert not db.get(SearchAgent, agent.id).auto_buy
        assert 'name="auto_buy_on"' in c.get("/agents").text and 'name="auto_buy_on"' in c.get(f"/tg/agents/{agent.id}").text


async def test_an_agent_run_hands_its_passing_hits_to_auto_buy(monkeypatch):
    from app import platforms
    from app.services import search_agents
    from tests.fake_platform import FakePlatform, listing
    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    seen = []

    async def consider(agent, hits):
        seen.append([h.ad_id for h in hits])
        return []

    async def notify(*a, **kw):
        return True
    monkeypatch.setattr(auto_buy, "consider", consider)
    monkeypatch.setattr(search_agents, "notify", notify)
    try:
        a = search_agents.create_agent("https://fake.example/s/homepod")
        fake.pages = [[]]
        await search_agents.run_agent(a.id)                               # baseline
        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.auto_buy, row.auto_buy_max = True, 50.0
            db.add(row)
            db.commit()
        fake.pages = [[listing("1", "HomePod mini", 40.0)]]
        await search_agents.run_agent(a.id)
        assert seen == [["1"]]
    finally:
        platforms.reset()


def test_no_money_on_a_guess(agent, monkeypatch):
    """With the screening on, a hit Claude did not judge (its call failed) is not bought; with a Geizhals limit, one
    without a checked new price neither; nothing at all while purchases are switched off."""
    from app.db import set_setting
    with session() as db:
        a = db.get(SearchAgent, agent.id)
        a.screen = True
        db.add(a)
        db.commit()
        db.refresh(a)
    assert not auto_buy.candidate(a, _hit(a, "11", 40.0))                           # not judged
    assert auto_buy.candidate(a, _hit(a, "12", 40.0, screen_relevant=True))
    a.geizhals_pct = 30
    assert not auto_buy.candidate(a, _hit(a, "13", 40.0, screen_relevant=True))    # no Geizhals price
    assert auto_buy.candidate(a, _hit(a, "14", 40.0, screen_relevant=True,
                                      geizhals={"name": "x", "url": "u", "price_from": 100.0}))
    set_setting(purchase.SETTING_ENABLED, "")
    assert auto_buy.why_not(a, _hit(a, "15", 40.0, screen_relevant=True)) == "auto-buy is off"


async def test_one_auto_buy_per_agent_and_a_gone_ad_stops(agent, pushes, monkeypatch):
    from app import platforms
    first = await auto_buy.schedule(agent, _hit(agent, "21"))
    assert first and await auto_buy.schedule(agent, _hit(agent, "22")) is None    # the first one runs still

    async def gone(ad_id, url=None):
        return None
    monkeypatch.setattr(platforms.get("willhaben"), "detail", gone)
    await auto_buy.buy(first)
    assert purchase.get(first).status == "failed"
    assert {a["name"] for a in purchase.actions(purchase.get(first))}.isdisjoint({"restart"})   # never a negotiation


async def test_a_doubtful_seller_account_stops_it(agent, pushes, monkeypatch):
    """A young account, or one without a PayLivery rating (from their profile), is not bought from on its own;
    Settings → Purchases can allow it."""
    from datetime import timedelta

    from app import platforms
    from app.db import set_setting, utcnow
    from app.services import paylivery
    from app.willhaben import public
    asked = []
    monkeypatch.setattr(paylivery, "run_action", lambda *a, **k: asked.append(a))
    ratings = {"n": 0}

    class Pub:
        async def member_profile(self, ad_uuid, user_uuid):
            return {"place": None, "since": None, "ratings": ratings["n"], "score": None, "reply_time": None}
    monkeypatch.setattr(public, "get_public", lambda: Pub())
    old, young = utcnow() - timedelta(days=900), utcnow() - timedelta(days=12)
    for ad_id, since, n in (("781", young, 5), ("782", old, 0)):
        d = _detail(ad_id)
        d.seller_since, d.ad_uuid, d.seller_uuid = since, "a" * 8, "u" * 8
        ratings["n"] = n

        async def detail(ad_id, url=None, d=d):
            return d
        monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
        pid = await auto_buy.schedule(agent, _hit(agent, ad_id=ad_id))
        await auto_buy.buy(pid)
        row = purchase.get(pid)
        assert row.status == "failed" and "account is doubtful" in str(row.log[-1]), row.log
    assert asked == []
    assert "Account only 12 days old" in str(purchase.get(pid - 1).log[-1])
    assert "No PayLivery ratings yet" in str(purchase.get(pid).log[-1])
    set_setting(auto_buy.SETTING_TRUST, "0")                             # allowed: no check
    assert await auto_buy.seller_doubt(d) is None


async def test_a_cancel_while_the_ad_is_read_stops_the_buy(agent, pushes, monkeypatch):
    """The owner cancels on the order page while the ad is being read again: the cancel wins, nothing is bought (the
    move to "pay now" used to overwrite it)."""
    from app import platforms
    from app.services import paylivery
    calls = []
    pid = None

    async def detail(ad_id, url=None):
        purchase.cancel(pid)                       # the web Cancel lands right now
        return _detail(ad_id)

    async def run_action(cid, action, amount=None, claimed=False):
        calls.append(action)
        return "ok"
    monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
    monkeypatch.setattr(paylivery, "run_action", run_action)
    pid = await auto_buy.schedule(agent, _hit(agent))
    assert await auto_buy.buy(pid) == "Cancelled"
    assert calls == [] and purchase.get(pid).status == "cancelled"
