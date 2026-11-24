"""A search agent's shipping options: its price rules compare item + shipping, and a limit on the shipping price."""
from sqlmodel import select

import pytest

from app import platforms
from app.db import Hit, SearchAgent, session
from app.services import deals, search_agents
from app.services.search_agents import create_agent, geizhals_passes, passes, run_agent, shipping_passes
from tests.fake_platform import FakePlatform, listing

GH = {"name": "Apple iPhone 13 128GB", "url": "https://geizhals.at/apple-iphone-13-a2610000.html", "price_from": 500.0}


def _hit(price=340.0, shipping_price=None, shipping=None):
    return Hit(agent_id=1, ad_id="1", heading="iPhone", url="x", price=price, geizhals=dict(GH),
               shipping_price=shipping_price, shipping=shipping)


def _agent(**kw):
    return SearchAgent(name="x", source_url="x", webapi_url="x", **kw)


def test_the_price_rules_add_the_shipping_when_asked():
    pct = _agent(geizhals_pct=30)                       # 30 % below 500 €: at most 350 €
    assert geizhals_passes(pct, _hit(340.0, 15.0, "ship"))
    with_ship = _agent(geizhals_pct=30, price_with_shipping=True)
    assert not geizhals_passes(with_ship, _hit(340.0, 15.0, "ship"))           # 355 €
    assert geizhals_passes(with_ship, _hit(340.0, 10.0, "ship"))               # 350 €: exactly the limit
    assert geizhals_passes(with_ship, _hit(340.0, None, "pickup"))             # pickup: the item alone
    assert geizhals_passes(with_ship, _hit(340.0, None, None))                 # shipping unknown: the item alone


def test_a_price_zone_judges_item_plus_shipping(monkeypatch):
    zone = _agent(geizhals_level="cheap", price_with_shipping=True)
    seen = []

    def level(h, zones=None):
        seen.append(h.price)
        return "cheap"
    monkeypatch.setattr(deals, "hit_level", level)
    assert geizhals_passes(zone, _hit(300.0, 20.0, "ship")) and seen == [320.0]


def test_the_shipping_limit():
    a = _agent(max_shipping=5.0)
    assert shipping_passes(a, _hit(shipping_price=4.99, shipping="ship"))
    assert shipping_passes(a, _hit(shipping_price=5.0, shipping="ship"))
    assert not shipping_passes(a, _hit(shipping_price=6.9, shipping="ship"))
    assert shipping_passes(a, _hit(shipping="pickup")) and shipping_passes(a, _hit())   # pickup, unknown
    assert not shipping_passes(_agent(max_shipping=0.0), _hit(shipping_price=3.0, shipping="ship"))   # free only
    assert shipping_passes(_agent(), _hit(shipping_price=30.0, shipping="ship"))                       # no limit
    assert not passes(a, _hit(shipping_price=6.9, shipping="ship"))
    assert search_agents.held_back_reasons(a, _hit(shipping_price=6.9, shipping="ship")) == ["shipping over 5 €"]


@pytest.fixture
def fake():
    p = FakePlatform()
    platforms.set_platform("fake", p)
    yield p
    platforms.reset()


async def test_a_hit_whose_shipping_shows_up_on_its_page_is_held_back(fake, monkeypatch):
    """The list names no shipping; the ad page, read for the push, says 9 €: over the limit, no push, Filtered."""
    sent = []

    async def notify(kind, title, text="", **kw):
        sent.append(((kw.get("data") or {}).get("ad_id"), kw.get("push", True)))

    async def pages(hits):
        for h in hits:
            h.shipping_price, h.shipping = (9.0, "ship") if h.ad_id == "1" else (3.0, "ship")
    monkeypatch.setattr(search_agents, "notify", notify)
    monkeypatch.setattr(search_agents, "_shipping_pages", pages)
    a = create_agent("https://fake.example/s/rad")
    fake.pages = [[]]
    await run_agent(a.id)
    with session() as db:
        row = db.get(SearchAgent, a.id)
        row.max_shipping = 5.0
        db.add(row)
        db.commit()
    fake.pages = [[listing("1", "Rad", 100.0), listing("2", "Rad", 120.0)]]
    await run_agent(a.id)
    assert ("1", True) not in sent and ("2", True) in sent
    with session() as db:
        states = {h.ad_id: h.state for h in db.exec(select(Hit).where(Hit.agent_id == a.id))}
    assert states == {"1": "filtered", "2": "new"}


def test_both_forms_save_it_and_another_form_leaves_it(fake):
    from fastapi.testclient import TestClient
    from app.main import app

    a = create_agent("https://fake.example/s/rad")
    form = {"name": "Rad", "interval_min": "30"}
    with TestClient(app) as c:
        c.post(f"/agents/{a.id}/edit", data={**form, "ship_shown": "1", "gh_ship": "on", "ship_on": "on",
                                             "max_shipping": "4,90"})
        with session() as db:
            row = db.get(SearchAgent, a.id)
            assert row.price_with_shipping and row.max_shipping == 4.9
        c.post(f"/agents/{a.id}/edit", data=form)                            # a form without the fields
        with session() as db:
            assert db.get(SearchAgent, a.id).max_shipping == 4.9
        c.post(f"/agents/{a.id}/edit", data={**form, "ship_shown": "1"})    # both switched off
        with session() as db:
            row = db.get(SearchAgent, a.id)
            assert not row.price_with_shipping and row.max_shipping is None
        assert 'name="max_shipping"' in c.get("/agents").text and 'name="max_shipping"' in c.get(f"/tg/agents/{a.id}").text
