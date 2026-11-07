import asyncio
import copy

import httpx
import pytest
from sqlmodel import select

from app import db as dbmod
from app.db import set_setting
from app.llm.base import LLMError, LLMResult
from app.services import screening
from app.services.search_agents import SETTING_REFILED, create_agent, refile_screened, run_agent
from tests.conftest import load
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.test_search_agents import URL, _set_attr, ha, responses  # noqa: F401  (pytest fixtures)


class FakeScreen:
    model = "fake-haiku"

    def __init__(self, results=None, fail=False):
        self.calls, self.results, self.fail = [], results, fail

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"prompt": prompt, "images": list(images or []), "schema": schema})
        if self.fail:
            raise LLMError("rate limited")
        return LLMResult(data={"results": self.results}, model=self.model)


@pytest.fixture
def images(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(screening, "_transport", httpx.MockTransport(handler))


async def _agent_with_two_new(queue, minimum="good"):
    base = load("search_fahrrad_rows3.json")
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen, a.screen_brief, a.screen_min = True, "Rennrad RH 56, kein E-Bike", minimum
        db.add(a)
        db.commit()
    queue.append(base)
    await run_agent(agent.id)                         # baseline: silent, nothing screened
    second = copy.deepcopy(base)
    for n, heading in ((1, "Rennrad Shimano 105 RH 56"), (2, "Fahrradschlauch 28 Zoll")):
        ad = copy.deepcopy(second["advertSummaryList"]["advertSummary"][0])
        ad["id"] = f"99900011{n}"
        _set_attr(ad, "ADID", ad["id"])
        _set_attr(ad, "HEADING", heading)
        second["advertSummaryList"]["advertSummary"].insert(0, ad)
    queue.append(second)
    return agent


def _pushes(sent):
    return [b for p, b in sent if p.startswith("/api/services/notify/")]


async def test_screening_limits_pushes_but_keeps_events(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    # newest first in the search -> nr 1 = the tube (inserted last), nr 2 = the road bike
    fake = FakeScreen([{"nr": 1, "relevant": False, "deal": "unknown", "reason": "Nur ein Schlauch"},
                       {"nr": 2, "relevant": True, "deal": "good", "reason": "30 % unter üblichem Preis"}])
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    r = await run_agent(agent.id)

    assert len(r.new) == 2 and len(fake.calls) == 1
    prompt = fake.calls[0]["prompt"]
    assert "Rennrad RH 56, kein E-Bike" in prompt and "[1] Fahrradschlauch" in prompt and "Foto: ./foto1.jpg" in prompt
    assert len(fake.calls[0]["images"]) == 2
    events = [b for p, b in ha if p == "/api/events/willhaben_hit"]
    assert len(events) == 2                                           # HA automations still see both
    rejected = next(e for e in events if e["ad_id"] == "999000112")
    assert rejected["screen_relevant"] is False and rejected["screen_reason"] == "Nur ein Schlauch"
    pushes = _pushes(ha)
    assert len(pushes) == 1 and "Rennrad Shimano 105" in pushes[0]["message"]
    assert "Claude: good price: 30 % unter üblichem Preis" in pushes[0]["message"]
    with dbmod.session() as db:
        tube = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "999000112")).one()
    assert (tube.screen_relevant, tube.screen_deal) == (False, "unknown")
    assert (tube.state, _hit("999000111").state) == ("filtered", "new")   # no push: not under New


def _hit(ad_id):
    with dbmod.session() as db:
        return db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == ad_id)).one()


async def test_filtered_hit_is_new_again_after_a_passing_price_drop(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    agent = await _agent_with_two_new(queue)
    third = copy.deepcopy(queue[-1])                 # third check: same ads
    queue.append(404)                                # the pushed hit's ad page (its shipping): gone
    queue.append(third)
    monkeypatch.setattr(screening, "get_backend", lambda *a: FakeScreen(
        [{"nr": 1, "relevant": False, "deal": "unknown", "reason": "Nur ein Schlauch"},
         {"nr": 2, "relevant": True, "deal": "good", "reason": "Passt"}]))
    await run_agent(agent.id)
    assert _hit("999000112").state == "filtered"
    with dbmod.session() as db:
        tube = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "999000112")).one()
        tube.price = 120                                # the third check sees 100 €: a price drop
        db.add(tube)
        db.commit()
    monkeypatch.setattr(screening, "get_backend", lambda *a: FakeScreen(
        [{"nr": 1, "relevant": True, "deal": "great", "reason": "Jetzt ein Schnäppchen"}]))
    r = await run_agent(agent.id)
    assert [h.ad_id for h in r.price_drops] == ["999000112"] and _hit("999000112").state == "new"


async def test_screening_fails_open(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "get_backend", lambda *a: FakeScreen(fail=True))
    await run_agent(agent.id)
    assert len(_pushes(ha)) == 2                                     # Claude down: every hit pushes as before
    assert {_hit("999000111").state, _hit("999000112").state} == {"new"}


async def test_every_new_hit_is_screened_in_several_calls(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "MAX_PER_CALL", 1)
    fake = FakeScreen([{"nr": 1, "relevant": True, "deal": "good", "reason": "Passt"}])
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    await run_agent(agent.id)
    assert len(fake.calls) == 2 and len(_pushes(ha)) == 2
    assert {_hit("999000111").screen_relevant, _hit("999000112").screen_relevant} == {True}


async def test_a_hit_beyond_the_screening_limit_gets_no_push(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "MAX_PER_CALL", 1)
    monkeypatch.setattr(screening, "MAX_PER_RUN", 1)
    fake = FakeScreen([{"nr": 1, "relevant": True, "deal": "good", "reason": "Passt"}])
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    await run_agent(agent.id)
    assert len(fake.calls) == 1 and len(_pushes(ha)) == 1          # before: the other one was pushed unjudged
    unjudged = next(h for h in (_hit("999000111"), _hit("999000112")) if h.screen_relevant is None)
    assert unjudged.state == "pending"                              # the next check judges it first
    assert len([b for p, b in ha if p == "/api/events/willhaben_hit"]) == 2      # HA still sees both


async def test_a_failed_call_ends_the_round_its_hits_fail_open(responses, ha, images, monkeypatch):  # noqa: F811
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "MAX_PER_CALL", 1)
    fake = FakeScreen(fail=True)
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    await run_agent(agent.id)
    assert len(fake.calls) == 1 and len(_pushes(ha)) == 1
    assert sorted([_hit("999000111").state, _hit("999000112").state]) == ["new", "pending"]


async def test_a_later_call_that_fails_leaves_its_hits_pending(responses, ha, images, monkeypatch):  # noqa: F811
    """11-06: the chunks after the first run side by side; one that timed out let its hits through unjudged."""
    queue, _ = responses
    set_setting("ha_notify_service", "mobile_app_phone")
    set_setting("push_channel", "ha")
    agent = await _agent_with_two_new(queue)
    monkeypatch.setattr(screening, "MAX_PER_CALL", 1)

    class FailsSecond(FakeScreen):
        async def complete(self, **kw):
            self.fail = len(self.calls) >= 1          # the first call answers, the second times out
            return await super().complete(**kw)

    fake = FailsSecond([{"nr": 1, "relevant": True, "deal": "good", "reason": "Passt"}])
    monkeypatch.setattr(screening, "get_backend", lambda *a: fake)
    await run_agent(agent.id)
    assert len(fake.calls) == 2 and len(_pushes(ha)) == 1           # the unjudged one gets no push
    assert sorted([_hit("999000111").state, _hit("999000112").state]) == ["new", "pending"]


async def test_screening_calls_wait_for_a_free_slot(monkeypatch):
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen = True
        db.add(a)
        hits = [dbmod.Hit(agent_id=agent.id, ad_id=str(n), heading=f"Rad {n}", url="u", body="x" * 100)
                for n in range(4)]
        db.add_all(hits)
        db.commit()
        for h in hits:
            db.refresh(h)
            db.expunge(h)
    monkeypatch.setattr(screening, "MAX_PER_CALL", 1)
    monkeypatch.setattr(screening, "SCREEN_CONCURRENCY", 1)
    running, peak = 0, 0

    async def ask(agent, hits, check, stats):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {1: {"nr": 1, "relevant": True, "deal": "good", "reason": "Passt"}}, {}

    monkeypatch.setattr(screening, "ask", ask)
    asked = await screening.screen_all(agent.id, hits)
    assert asked == {h.id for h in hits} and peak == 1               # unbounded, the last three ran at once


def test_refile_screened_moves_held_back_new_hits_once(client):  # noqa: F811
    agent = create_agent(URL)
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a.screen, a.screen_min = True, "good"
        db.add(a)
        for ad_id, state, relevant, deal in (("1", "new", False, "unknown"), ("2", "new", True, "great"),
                                             ("3", "new", None, None), ("4", "seen", False, "unknown")):
            db.add(dbmod.Hit(agent_id=agent.id, ad_id=ad_id, heading=f"Hit {ad_id}", url="https://x", state=state,
                             screen_relevant=relevant, screen_deal=deal))
        db.commit()
    set_setting(SETTING_REFILED, "")                  # the client's startup already ran it on the empty database
    refile_screened()
    assert [_hit(x).state for x in "1234"] == ["filtered", "new", "new", "seen"]
    page = client.get(f"/hits?state=filtered&agent_id={agent.id}").text
    assert "Filtered out" in page and "Hit 1" in page and "Hit 2" not in page
    assert "Hit 1" not in client.get(f"/hits?agent_id={agent.id}").text
    with dbmod.session() as db:                       # moved back by hand: a restart leaves it alone
        h = db.exec(select(dbmod.Hit).where(dbmod.Hit.ad_id == "1")).one()
        h.state = "new"
        db.add(h)
        db.commit()
    refile_screened()
    assert _hit("1").state == "new"


def test_passes_thresholds():
    agent = dbmod.SearchAgent(name="x", source_url="u", webapi_url="u", screen=True, screen_min="good")
    hit = dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u")
    assert screening.passes(agent, hit)                               # not screened yet
    hit.screen_relevant, hit.screen_deal = True, "fair"
    assert not screening.passes(agent, hit)
    hit.screen_deal = "great"
    assert screening.passes(agent, hit)
    hit.screen_relevant = False
    assert not screening.passes(agent, hit)
    agent.screen_min, hit.screen_relevant, hit.screen_deal = "fair", True, "unknown"
    assert screening.passes(agent, hit)                               # unclear price passes "fair", not "good"
    agent.screen = False
    hit.screen_relevant = False
    assert screening.passes(agent, hit)                               # screening off: everything notifies


def test_agent_form_settings_and_hit_badge(client):  # noqa: F811
    agent = create_agent(URL)
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rennrad", "interval_min": "5", "notify_on": "on",
                                                  "screen_on": "on", "screen_brief": "RH 56\r\nkein E-Bike",
                                                  "screen_min": "fair"})
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
        a_screen = (a.screen, a.screen_brief, a.screen_min)
        db.add(dbmod.Hit(agent_id=agent.id, ad_id="5", heading="Schlauch", url="https://x", state="new",
                         screen_relevant=False, screen_deal="unknown", screen_reason="Nur Zubehör"))
        db.commit()
    assert a_screen == (True, "RH 56\nkein E-Bike", "fair")
    page = client.get("/agents").text
    assert "Claude screening" in page and 'name="screen_brief"' in page and "only fair or better prices" in page
    assert 'name="model_screen"' in client.get("/settings/claude").text
    assert "wrong item" in client.get(f"/hits?agent_id={agent.id}").text
    client.post(f"/agents/{agent.id}/edit", data={"name": "Rennrad", "interval_min": "5", "screen_min": "bogus"})
    with dbmod.session() as db:
        a = db.get(dbmod.SearchAgent, agent.id)
    assert a.screen is False and a.screen_min == "match"


def test_the_prompt_rates_against_the_geizhals_new_price():
    """Claude gets the hit's Geizhals product, its share of the new price and the zone label the card shows, so its
    verdict does not rest on an outdated new price from memory ("neu 60–70 €" for a Pi 5 that costs 118,50 €)."""
    from app.llm.prompts import SCREEN_SYSTEM, screen_prompt

    gh = {"name": "Raspberry Pi 5 Modell B, 4GB RAM", "url": "https://geizhals.at/x-a1.html", "price_from": 118.5}
    hit = dbmod.Hit(agent_id=1, ad_id="1", heading="Raspberry PI 5 4 GB Ram", url="u", price=120.0,
                    item_condition="new", geizhals=gh)
    np = screening._new_price(hit)
    assert np == {"name": gh["name"], "price": 118.5, "pct": 101, "level": "teuer"}     # new items: 100–114 %
    prompt = screen_prompt(agent_name="Pi", brief="", stats=None, language="Deutsch", hits=[
        {"nr": 1, "title": hit.heading, "price": "120 €", "location": "Wien", "private": True, "description": None,
         "photo": None, "condition": "Neu", "new_price": np}])
    assert "Zustand laut Anzeige: Neu" in prompt
    assert ("Neupreis laut Geizhals: ab 118.5 € (Raspberry Pi 5 Modell B, 4GB RAM); der Preis ist 101 % davon, "
            "nach den Preiszonen der Person: teuer") in prompt
    assert "Steht bei einem Treffer ein Geizhals-Neupreis" in SCREEN_SYSTEM and "teuer oder sehr teuer → overpriced" in SCREEN_SYSTEM
    for g, price in (({}, 120.0), (None, 120.0), (gh, None), (gh, 0.0)):
        assert screening._new_price(dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u", price=price, geizhals=g)) is None


def test_hit_card_names_the_source_of_each_rating(client):  # noqa: F811
    """The zone badge and Claude's verdict in the same colours, each after its source, the reason below."""
    agent = create_agent(URL)
    gh = {"name": "Raspberry Pi 5 Modell B, 4GB RAM", "url": "https://geizhals.at/x-a1.html", "price_from": 118.5,
          "checked": True}
    with dbmod.session() as db:
        db.add(dbmod.Hit(agent_id=agent.id, ad_id="5", heading="Raspberry PI 5 4 GB Ram", url="https://x", state="new",
                         price=120.0, item_condition="new", geizhals=gh, screen_relevant=True, screen_deal="fair",
                         screen_reason="Marktüblich für einen neuen Pi 5."))
        db.commit()
    page = client.get(f"/hits?agent_id={agent.id}").text
    card = page[page.index("data-ratings"):]
    geizhals = card[:card.index('data-rating="claude"')]
    assert "bg-lvl-expensive" in geizhals and "Geizhals ✓ checked by Claude: Raspberry Pi 5" in geizhals   # its source
    assert ">expensive</span>" in geizhals and "101\xa0% of €\xa0118,50 new" in geizhals and 'title="checked by Claude"' in geizhals
    claude = card[card.index('data-rating="geizhals"'):]
    assert "bg-lvl-fair" in claude and "Claude&#39;s assessment" in claude and ">fair price</span>" in claude
    assert 'title="Marktüblich für einen neuen Pi 5.">Marktüblich für einen neuen Pi 5.</p>' in page


def test_push_line_uses_first_sentence_only():
    hit = dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u", screen_relevant=True, screen_deal="good",
                    screen_reason="Guter Preis für ein gepflegtes Rad. Neu kostet es 900 €, gebraucht meist 500–600 €.")
    assert screening.summary(hit) == "Claude: good price: Guter Preis für ein gepflegtes Rad."
    assert screening.short_reason("x" * 300).endswith("…") and len(screening.short_reason("x" * 300)) == 140
