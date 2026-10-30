"""Claude checks the Geizhals product of a screening agent's new hits and looks for the right one (switch "Check the
Geizhals product")."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Hit, SearchAgent, session
from app.llm.base import LLMError, LLMResult
from app.services import deals, geizhals, geizhals_check
from app.services.geizhals import Hit as GHit

AUTO_PRO_24 = {"name": "Ubiquiti UniFiSwitch Pro 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP+, 400W PoE++",
               "url": "https://geizhals.at/ubiquiti-unifiswitch-pro-24-usw-pro-24-poe-a2194138.html", "price_from": 739.88}
RIGHT_24 = GHit(name="Ubiquiti UniFiSwitch 24 Rackmount Gigabit Managed Switch, 24x RJ-45, 2x SFP, 95W PoE+",
                url="https://geizhals.at/ubiquiti-unifiswitch-24-usw-24-poe-a2189707.html", price=406.87)


class Script:
    model = "fake-haiku"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return LLMResult(data=a, model=self.model)


@pytest.fixture
def agent():
    with session() as db:
        a = SearchAgent(name="Unifi", source_url="https://www.willhaben.at/iad/x", webapi_url="x", screen=True,
                        geizhals_check=True)
        db.add(a)
        db.commit()
        db.refresh(a)
        return a


def _hit(agent, n, heading, gh, price=200.0):
    with session() as db:
        h = Hit(agent_id=agent.id, ad_id=str(n), heading=heading, url=f"u{n}", price=price, geizhals=gh)
        db.add(h)
        db.commit()
        db.refresh(h)
        return h


def _stored(h):
    with session() as db:
        return db.get(Hit, h.id).geizhals


async def test_right_product_is_kept_wrong_one_replaced(agent, monkeypatch):
    ok = _hit(agent, 1, "Raspberry Pi 4 4GB", {"name": "Raspberry Pi 4 Modell B, 4GB RAM", "url": "https://geizhals.at/p-a1.html",
                                               "price_from": 114.95})
    wrong = _hit(agent, 2, "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", dict(AUTO_PRO_24))
    searched = []

    async def candidates(queries):
        searched.append(queries)
        return [GHit(name=AUTO_PRO_24["name"], url=AUTO_PRO_24["url"], price=739.88), RIGHT_24]

    monkeypatch.setattr(geizhals_check, "_candidates", candidates)
    fake = Script({"results": [{"nr": 1, "ok": True, "product": "", "queries": []},
                               {"nr": 2, "ok": False, "product": "Ubiquiti UniFi Switch 24 PoE (USW-24-POE)",
                                "queries": ["USW-24-POE", "UniFi Switch 24 PoE"]}]},
                  {"results": [{"nr": 2, "pick": 2}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    assert await geizhals_check.check(agent, [ok, wrong]) == 2
    assert "Geizhals: Ubiquiti UniFiSwitch Pro 24" in fake.calls[0]["prompt"] and "(neu ab 739.88 €)" in fake.calls[0]["prompt"]
    assert searched == [["USW-24-POE", "UniFi Switch 24 PoE"]]
    assert "2. Ubiquiti UniFiSwitch 24 Rackmount" in fake.calls[1]["prompt"] and "ab 406.87 €" in fake.calls[1]["prompt"]
    assert _stored(ok) == {"name": "Raspberry Pi 4 Modell B, 4GB RAM", "url": "https://geizhals.at/p-a1.html",
                           "price_from": 114.95, "checked": True}
    assert _stored(wrong) == {"name": RIGHT_24.name, "url": RIGHT_24.url, "price_from": 406.87, "by": "claude",
                              "checked": True}
    assert wrong.geizhals["price_from"] == 406.87                     # the caller's object too (the rule uses it)

    assert await geizhals_check.check(agent, [ok, wrong]) == 0        # checked once: a price drop keeps the product
    assert len(fake.calls) == 2


async def test_no_product_no_price_and_failures_change_nothing(agent, monkeypatch):
    junk = _hit(agent, 3, "Konvolut Netzwerkkabel", {})                      # nothing found, nothing to find
    wrong = _hit(agent, 4, "UniFi Switch 8 150W", {"name": "Ubiquiti UniFiSwitch Lite 8", "url": "https://geizhals.at/l-a2.html",
                                                    "price_from": 120.0})
    failed = _hit(agent, 5, "Raspberry Pi 5", None)                          # lookup failed: asked again later

    async def nothing(queries):
        return []

    monkeypatch.setattr(geizhals_check, "_candidates", nothing)
    fake = Script({"results": [{"nr": 1, "ok": False, "product": "", "queries": []},
                               {"nr": 2, "ok": False, "product": "Ubiquiti UniFi Switch 8 150W", "queries": ["US-8-150W"]}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    await geizhals_check.check(agent, [junk, wrong, failed])
    assert _stored(junk) == {"checked": True}                                # final: no specific product
    assert _stored(wrong) == {} and len(fake.calls) == 1                     # no results: wrong price gone, asked again
    assert _stored(failed) is None                                           # (and no pick call)

    other = _hit(agent, 6, "iPhone 16 Pro", {"name": "Apple iPhone 16 128GB", "url": "https://geizhals.at/i-a3.html",
                                             "price_from": 842.0})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: Script(LLMError("limit")))
    assert await geizhals_check.check(agent, [other]) == 0
    assert _stored(other) == {"name": "Apple iPhone 16 128GB", "url": "https://geizhals.at/i-a3.html", "price_from": 842.0}


async def test_searches_are_capped_and_stop_when_geizhals_blocks(agent, monkeypatch):
    hits = [_hit(agent, 10 + i, f"Produkt {i}", {}) for i in range(5)]
    calls = []

    async def blocked(queries):
        calls.append(queries)
        raise geizhals.GeizhalsError("Geizhals answered HTTP 429")

    monkeypatch.setattr(geizhals_check, "_candidates", blocked)
    verdicts = [{"nr": i + 1, "ok": False, "product": f"Produkt {i}", "queries": [f"Produkt {i}"]} for i in range(5)]
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: Script({"results": verdicts}))
    await geizhals_check.check(agent, hits)
    assert calls == [["Produkt 0"]]                                          # stopped at the first block
    assert all(_stored(h) == {} for h in hits)                               # not final: a later check asks again
    assert geizhals_check.MAX_SEARCHED == 3


async def test_later_checks_take_older_unchecked_hits_and_stuck_ones_once(agent, monkeypatch):
    """Hits marked "no product" by an aborted check (version 1) and products from before the switch get checked by
    later runs too: up to MAX_BACKLOG per check, product only."""
    from app.db import get_setting, set_setting

    set_setting("geizhals_check_version", "1")
    stuck = _hit(agent, 20, "Ubiquiti Unifi UAP-AC-pro + adapteur Poe", {"checked": True})
    final = _hit(agent, 21, "Konvolut Kabel", {"checked": True, "name": "x"})       # a real answer stays
    old = _hit(agent, 22, "Ubiquiti Unifi switch USW Lite 8 PoE",
               {"name": "Ubiquiti UniFiSwitch Flex Desktop 2.5G", "url": "https://geizhals.at/f-a9.html", "price_from": 209.0})
    fake = Script({"results": [{"nr": 1, "ok": False, "product": "Ubiquiti UniFi Switch Lite 8 PoE",   # most recent first
                                "queries": ["USW-Lite-8-PoE"]},
                               {"nr": 2, "ok": False, "product": "Ubiquiti UniFi AP AC Pro", "queries": ["UAP-AC-PRO"]}]},
                  {"results": [{"nr": 1, "pick": 1}, {"nr": 2, "pick": 1}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)

    async def candidates(queries):
        return [GHit(name="Ubiquiti UniFi AP AC Pro", url="https://geizhals.at/ap-a1325749.html", price=146.12)] \
            if queries == ["UAP-AC-PRO"] else [GHit(name="Ubiquiti UniFiSwitch Lite 8 PoE", url="https://geizhals.at/l8-a1.html",
                                                     price=99.0)]

    monkeypatch.setattr(geizhals_check, "_candidates", candidates)
    assert await geizhals_check.check(agent, []) == 2                        # no new hit, only the backlog
    assert _stored(stuck)["price_from"] == 146.12 and _stored(old)["price_from"] == 99.0
    assert _stored(final) == {"checked": True, "name": "x"} and get_setting("geizhals_check_version") == "2"
    prompt = fake.calls[0]["prompt"]
    assert prompt.index("[1] Ubiquiti Unifi switch USW Lite 8 PoE") < prompt.index("[2] Ubiquiti Unifi UAP-AC-pro")
    assert "Geizhals: Ubiquiti UniFiSwitch Flex Desktop 2.5G (neu ab 209 €)" in prompt and "Konvolut" not in prompt
    assert await geizhals_check.check(agent, []) == 0 and len(fake.calls) == 2   # nothing left: no Claude call


async def test_candidates_stop_at_the_first_search_that_finds_something(monkeypatch):
    asked = []

    async def search(q, limit=5):
        asked.append(q)
        return [] if q == "a" else [GHit(name=q, url=f"https://geizhals.at/{q}-a1.html", price=1.0)]

    monkeypatch.setattr(geizhals, "search", search)
    assert [c.name for c in await geizhals_check._candidates(["a", "b", "c"])] == ["b"] and asked == ["a", "b"]


def test_drawer_switch_saves_only_with_screening(agent):
    from app.main import app

    with TestClient(app) as c:
        page = c.get("/agents").text
        assert 'name="gh_check"' in page and "Check the Geizhals product" in page and "checks the Geizhals product" in page
        form = {"name": "Unifi", "interval_min": "10", "screen_on": "1", "screen_min": "match"}
        c.post(f"/agents/{agent.id}/edit", data={**form, "gh_check": "on"}, follow_redirects=False)
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is True
        c.post(f"/agents/{agent.id}/edit", data=form, follow_redirects=False)              # switched off
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is False
        c.post(f"/agents/{agent.id}/edit", data={"name": "Unifi", "interval_min": "10", "gh_check": "1"},
               follow_redirects=False)                                                       # no screening: no check
        with session() as db:
            assert db.get(SearchAgent, agent.id).geizhals_check is False


async def test_agent_run_checks_before_the_rule(monkeypatch):
    """A screening agent with the switch and the 30 % rule: the automatic product (the dear Pro 24, 740 €) would let a
    350 € ad through as 53 % below; Claude's product (407 €) holds it back. The check is part of the screening's call
    (one call, a second only to pick the product it looked for). Without the switch nothing is checked."""
    from app import platforms
    from app.services import search_agents
    from tests.fake_platform import FakePlatform, listing

    fake_site = FakePlatform()
    platforms.set_platform("fake", fake_site)
    try:
        async def match(query, **kw):
            return dict(AUTO_PRO_24)

        async def candidates(queries):
            return [RIGHT_24]

        async def notify(*a, **kw):
            return True

        verdict = {"relevant": True, "deal": "good", "issues": [], "reason": "Passt."}
        monkeypatch.setattr(deals, "geizhals_match", match)
        monkeypatch.setattr(geizhals_check, "_candidates", candidates)
        monkeypatch.setattr(search_agents, "notify", notify)
        fake = Script({"results": [{"nr": 1, **verdict, "geizhals_ok": False, "product": "Ubiquiti UniFi Switch 24 PoE",
                                    "queries": ["USW-24-POE"]}]},
                      {"results": [{"nr": 1, "pick": 1}]},
                      {"results": [{"nr": 1, **verdict}]})
        monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
        monkeypatch.setattr(search_agents.screening, "get_backend", lambda *a: fake)
        a = search_agents.create_agent("https://fake.example/s/unifi")
        fake_site.pages = [[]]
        await search_agents.run_agent(a.id)                                   # baseline
        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.screen, row.geizhals_check, row.geizhals_pct = True, True, 30
            db.add(row)
            db.commit()
        fake_site.pages = [[listing("1", "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", 350.0)]]
        await search_agents.run_agent(a.id)
        with session() as db:
            h = db.exec(select(Hit).where(Hit.agent_id == a.id)).one()
        assert h.geizhals["by"] == "claude" and h.geizhals["price_from"] == 406.87
        assert h.state == "filtered" and len(fake.calls) == 2              # 350 € is only 14 % below 407 €
        assert "geizhals_ok" in fake.calls[0]["system"] and "prüfe das Produkt" in fake.calls[0]["prompt"]
        assert h.screen_deal == "good"                                     # judged in the same call

        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.geizhals_check = False
            db.add(row)
            db.commit()
        fake_site.pages = [[listing("1", "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", 350.0),
                            listing("2", "Ubiquiti UniFiSwitch 24 PoE", 340.0)]]
        await search_agents.run_agent(a.id)
        # switched off: the product the app found is still checked in the screening's call (10-30), but no search for
        # the right one follows – no Geizhals search, no second Claude call
        assert len(fake.calls) == 3 and "geizhals_ok" in fake.calls[2]["system"]
    finally:
        platforms.reset()


async def test_the_screening_picks_from_the_lookups_results_in_one_call(monkeypatch):
    """The lookup saw the right switch among its results: Claude picks it in the screening's call – one Claude call,
    no Geizhals search of its own."""
    from app import platforms
    from app.services import search_agents
    from tests.fake_platform import FakePlatform, listing

    fake_site = FakePlatform()
    platforms.set_platform("fake", fake_site)
    try:
        async def match(query, **kw):
            return dict(AUTO_PRO_24)

        async def candidates(queries):
            raise AssertionError("no search of its own")

        async def notify(*a, **kw):
            return True

        results = [{"name": AUTO_PRO_24["name"], "url": AUTO_PRO_24["url"], "price": 739.88},
                   {"name": RIGHT_24.name, "url": RIGHT_24.url, "price": RIGHT_24.price}]
        monkeypatch.setattr(deals, "geizhals_match", match)
        monkeypatch.setattr(deals, "geizhals_candidates", lambda q: list(results))
        monkeypatch.setattr(geizhals_check, "_candidates", candidates)
        monkeypatch.setattr(search_agents, "notify", notify)
        fake = Script({"results": [{"nr": 1, "relevant": True, "deal": "fair", "issues": [], "reason": "Marktüblich.",
                                    "geizhals_ok": False, "geizhals_pick": 2, "product": "", "queries": []}]})
        monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
        monkeypatch.setattr(search_agents.screening, "get_backend", lambda *a: fake)
        a = search_agents.create_agent("https://fake.example/s/unifi")
        fake_site.pages = [[]]
        await search_agents.run_agent(a.id)
        with session() as db:
            row = db.get(SearchAgent, a.id)
            row.screen, row.geizhals_check, row.geizhals_pct = True, True, 30
            db.add(row)
            db.commit()
        fake_site.pages = [[listing("1", "Ubiquiti UniFiSwitch 24 PoE USW-24-POE", 350.0)]]
        await search_agents.run_agent(a.id)
        with session() as db:
            h = db.exec(select(Hit).where(Hit.agent_id == a.id)).one()
        assert h.geizhals == {"name": RIGHT_24.name, "url": RIGHT_24.url, "price_from": 406.87, "by": "claude",
                              "checked": True}
        assert len(fake.calls) == 1 and h.state == "filtered" and h.screen_deal == "fair"
        assert "2. Ubiquiti UniFiSwitch 24 Rackmount" in fake.calls[0]["prompt"] and "geizhals_pick" in fake.calls[0]["system"]
    finally:
        platforms.reset()


async def test_a_product_geizhals_lists_without_a_price_is_a_final_answer(agent, monkeypatch):
    """The UniFi Dream Router, no longer sold: Claude picks its entry without a price; not asked again every check."""
    h = _hit(agent, 1, "Ubiquiti UniFi UDR Wifi 6 Router", {})
    old = GHit(name="Ubiquiti UniFi Dream Router, UniFi Cloud Gateway", url="https://geizhals.at/udr-a1.html", price=None)

    async def candidates(queries):
        return [GHit(name="Ubiquiti UniFi Dream Router 7", url="https://geizhals.at/udr7-a2.html", price=292.9), old]

    monkeypatch.setattr(geizhals_check, "_candidates", candidates)
    fake = Script({"results": [{"nr": 1, "ok": False, "product": "Ubiquiti UniFi Dream Router (UDR)",
                                "queries": ["Ubiquiti UniFi Dream Router UDR"]}]},
                  {"results": [{"nr": 1, "pick": 2}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    assert await geizhals_check.check(agent, [h]) == 1
    assert _stored(h) == {"name": old.name, "url": old.url, "by": "claude", "checked": True}
    assert geizhals_check.backlog(agent, set()) == []                  # final: not asked again


async def test_an_ad_of_several_products_gets_the_sum_of_theirs(agent, monkeypatch):
    """A self-built PC: Claude names its parts, the app looks each up and stores the sum as the hit's new price."""
    pc = _hit(agent, 7, "PC – i7 / 16GB RAM / GTX 1050 / SSD", {"name": "Acer Veriton S2722GT", "url": "https://geizhals.at/v.html",
                                                                  "price_from": 1342.12})
    prices = {"Intel Core i7-3770K": 120.0, "NVIDIA GeForce GTX 1050": 140.0, "Kingston 16GB DDR3": None}

    async def match(q, raise_errors=False):
        p = prices.get(q)
        return {"name": q + " (Geizhals)", "url": f"https://geizhals.at/{len(q)}.html", "price_from": p} if p else None
    monkeypatch.setattr(deals, "geizhals_match", match)
    fake = Script({"results": [{"nr": 1, "ok": False, "product": "", "queries": [], "parts": list(prices)}]})
    monkeypatch.setattr(geizhals_check, "get_backend", lambda *a: fake)
    assert await geizhals_check.check(agent, [pc]) == 1
    gh = _stored(pc)
    assert gh["price_from"] == 260.0 and gh["by"] == "claude" and gh["checked"] and gh["missing"] == ["Kingston 16GB DDR3"]
    assert [p["query"] for p in gh["parts"]] == ["Intel Core i7-3770K", "NVIDIA GeForce GTX 1050"]
    assert "parts:" in fake.calls[0]["system"]
