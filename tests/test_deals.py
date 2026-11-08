import copy
import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.llm.base import LLMError, LLMResult
from app.services import deals, geizhals, price_zones
from app.willhaben import public as public_mod
from app.willhaben.parse import AdDetail
from app.willhaben.public import PublicClient
from tests.conftest import load
from tests.test_geizhals import PRODUCT, SEARCH
from tests.test_search_agents import _set_attr

AD_ID = "1000000001"
PRICES = [40, 60, 80, 90, 100, 110, 120, 130, 150, 200]


@pytest.fixture(autouse=True)
def _zones():
    """The cases here were written against the zones that were the defaults until 2026-10-14: they stay put when the
    defaults move (test_price_zones checks the defaults)."""
    from app.services import price_zones
    price_zones.save({"used": [30, 50, 80, 90], "new": [75, 100, 110, 125]})


def _ad(price=100.0, heading="Bosch GSR 18V-55 Akkuschrauber neuwertig OVP", ad_id=AD_ID):
    return AdDetail(ad_id=ad_id, heading=heading, description="Kaum benutzt.", price=price, price_display=None,
                    status="active", published_at=None, first_published_at=None, end_date=None,
                    url=f"https://www.willhaben.at/iad/object?adId={ad_id}", images=[], location="1010 Wien",
                    is_private=True, category_path=["Haus & Garten", "Werkzeug"])


def _search_json(prices, include_self_at=None):
    base = load("search_fahrrad_rows3.json")
    tmpl = base["advertSummaryList"]["advertSummary"][0]
    items = []
    for n, p in enumerate(prices):
        ad = copy.deepcopy(tmpl)
        ad["id"] = f"10000001{n:02d}"
        _set_attr(ad, "ADID", ad["id"])
        _set_attr(ad, "HEADING", f"Bosch GSR 18V-55 Nr {n}")
        _set_attr(ad, "PRICE", str(p))
        _set_attr(ad, "PRICE/AMOUNT", str(p))
        items.append(ad)
    if include_self_at is not None:              # the checked ad itself shows up in the search
        me = copy.deepcopy(tmpl)
        me["id"] = AD_ID
        _set_attr(me, "ADID", AD_ID)
        _set_attr(me, "HEADING", "Bosch GSR 18V-55 Akkuschrauber")
        _set_attr(me, "PRICE", str(include_self_at))
        _set_attr(me, "PRICE/AMOUNT", str(include_self_at))
        items.append(me)
    wanted = copy.deepcopy(tmpl)                 # "Suche ..." ads are not comparables
    wanted["id"] = "1000000199"
    _set_attr(wanted, "ADID", wanted["id"])
    _set_attr(wanted, "HEADING", "Suche Bosch GSR 18V-55")
    _set_attr(wanted, "PRICE", "1")
    _set_attr(wanted, "PRICE/AMOUNT", "1")
    items.append(wanted)
    base["advertSummaryList"]["advertSummary"] = items
    base["rowsFound"] = len(items)
    return base


def _detail_html(price=100, heading="Bosch GSR 18V-55 Akkuschrauber neuwertig"):
    props = load("detail_1662146981_nextdata.json")
    ad = props["advertDetails"]
    ad["id"], ad["description"] = int(AD_ID), heading
    ad["breadcrumbs"] = ad["breadcrumbs"][:2] + [{"displayName": "Haus & Garten"}, {"displayName": "Werkzeug"}]
    _set_attr(ad, "PRICE", str(price))
    _set_attr(ad, "PRICE/AMOUNT", str(price))
    _set_attr(ad, "PRICE_FOR_DISPLAY", f"€ {price}")
    nd = {"page": "/iad/kaufen-und-verkaufen/d/[[...seopath]]", "buildId": "x", "props": {"pageProps": props}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


@pytest.fixture(autouse=True)
def _clean():
    deals.clear_cache()
    yield
    deals.clear_cache()


@pytest.fixture
def wh():
    """Mocked willhaben: counts search and detail requests."""
    calls = {"search": 0, "detail": 0}
    state = {"search": _search_json(PRICES, include_self_at=100), "detail": _detail_html()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/webapi/ad-search/search"):
            calls["search"] += 1
            return httpx.Response(200, json=state["search"])
        if request.url.path == "/iad/object":
            calls["detail"] += 1
            return httpx.Response(200, text=state["detail"])
        return httpx.Response(404)

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    yield calls, state
    public_mod.set_public(None)


@pytest.fixture
def gh(monkeypatch):
    """Mocked Geizhals pages (same structure as the real ones), counting requests."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text=SEARCH)
        if request.url.path.endswith(".html"):
            return httpx.Response(200, text=PRODUCT)
        return httpx.Response(404)

    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0))
    return seen


class FakeClaude:
    model = "fake-haiku"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.fail:
            raise LLMError("rate limited")
        return LLMResult(data={"deal": "good", "reason": "Günstig: rund 30 % unter dem üblichen Gebrauchtpreis."},
                         model=self.model)


# ------------------------------------------------------------------ pure functions
def test_query_from_title_drops_filler():
    assert deals.query_from_title("Verkaufe Bosch GSR 18V-55 Akkuschrauber neuwertig OVP!") == "Bosch GSR 18V-55"
    assert deals.query_from_title("Top Zustand: iPhone 13 128GB") == "iPhone 13 128GB"
    assert deals.query_from_title("neu und OVP") == ""


def test_query_from_title_glues_sizes():
    assert deals.query_from_title("Raspberry Pi 4 4 GB") == "Raspberry Pi 4"        # the second "4" is no duplicate
    assert deals.query_from_title("Raspberry Pi 4 4 GB", words=4) == "Raspberry Pi 4 4GB"
    assert deals.query_from_title("iPhone 13 128 GB blau") == "iPhone 13 128GB"     # in the 3 words: one word
    assert deals.query_from_title("MacBook Air M1 8GB 256GB") == "MacBook Air M1"   # no size pulled in from further on


@pytest.mark.parametrize("title, query", [
    ("Sony PlayStation 5 Digital Edition", "Sony PlayStation 5 Digital"),        # the variant right after the model
    ("Ubiquiti UniFi U6 Mesh WiFi 6 Access Point", "Ubiquiti UniFi U6 Mesh"),
    ("Apple iPhone 13 Pro Max 256GB", "Apple iPhone 13 Pro"),                     # one word only
    ("Dyson V11 Absolute Extra", "Dyson V11 Absolute"),                            # not after the model
    ("Nintendo Switch OLED Modell weiß", "Nintendo Switch OLED"),
    ("Damen Winterjacke schwarz Größe XL", "Damen Winterjacke schwarz"),
])
def test_query_from_title_keeps_the_variant_after_the_model(title, query):
    assert deals.query_from_title(title) == query


def test_geizhals_query_only_for_products():
    assert deals.geizhals_query("iPhone 13 128GB Display kaputt") == "iPhone 13 128GB"
    assert deals.geizhals_query("Bosch GSR 18V-55 Akkuschrauber") == "Bosch GSR 18V-55"
    assert deals.geizhals_query("Cannondale Kinderfahrrad 20 Zoll") is None       # size, not a model
    assert deals.geizhals_query("Schöner alter Holzschrank") is None
    assert deals.geizhals_query("Jacke Größe 38", ["Mode & Accessoires"]) is None


@pytest.mark.parametrize("title, query", [
    ("Raspberry Pi 4 4GB", "Raspberry Pi 4 4GB"),
    ("Raspberry Pi 4 Model B 4GB", "Raspberry Pi 4 4GB"),                       # capacity further on is added
    ("Raspberry Pi 4 Modell B 4GB RAM mit Gehäuse", "Raspberry Pi 4 4GB"),
    ("Raspberry Pi 4 Model B 4 GB", "Raspberry Pi 4 4GB"),                      # "4 GB" is one word, not a 2nd "4"
    ("Neuwertiger Raspberry Pi 4 (4 GB)", "Raspberry Pi 4 4GB"),
    ("MacBook Air M1 8GB 256GB", "MacBook Air M1 8GB 256GB"),
    ("Raspberry Pi5 mit 1gb und Netzteil", "Raspberry Pi5 1gb"),                # right after "mit": the variant
    ("Raspberry Pi 5 mit 8 GB RAM", "Raspberry Pi 5 8GB"),
    ("Raspberry Pi 5 8GB mit Netzteil", "Raspberry Pi 5 8GB"),
    ("Pi 5 8GB + Samsung SSD 500 GB", "Pi 5 8GB"),                              # after "+": what comes along
    ("Raspberry Pi 5 mit 64GB SD-Karte", "Raspberry Pi 5"),                     # the card's, not the Pi's
    ("Raspberry Pi 5, 64GB microSD, Netzteil, Gehäuse", "Raspberry Pi 5"),
    ("2 Stk. Raspberry Pi 4 Modell B, 8GB RAM + Alugehäuse + 32GB microSD + Netzteil",
     "Raspberry Pi 4 8GB"),                                                     # after the list's comma, a count
])
def test_geizhals_query_keeps_the_variant(title, query):
    assert deals.geizhals_query(title) == query


def test_stats_percentiles():
    st = deals.stats(PRICES + [0, None])
    assert st["count"] == 10 and st["min"] == 40 and st["max"] == 200 and st["median"] == 105
    assert st["p10"] < st["p25"] < st["median"] < st["p75"] < st["p90"]
    assert deals.stats([10, 20]) is None


@pytest.mark.parametrize("price,lvl", [(30, "bargain"), (80, "cheap"), (105, "fair"), (130, "expensive"),
                                       (250, "very_expensive")])
def test_position_from_comparables(price, lvl):
    """Without a new price the similar ads rate: the price as a share of their median (105) against the user's zones
    for similar ads (default 70 / 90 / 115 / 135 %), on a scale with the median at 100 %."""
    p = deals.position(price, deals.stats(PRICES), None)
    assert p["basis"] == "comparables" and p["level"] == lvl and 0 <= p["pos"] <= 1
    assert p["zones"] == [70, 90, 115, 135] and p["median"] == 105 and p["count"] == 10
    m = deals.position(105, deals.stats(PRICES), None)
    assert m["pct"] == 100 and m["axis"] == 150 and m["pos"] == pytest.approx(0.667, abs=0.001)


def test_position_from_comparables_follows_the_users_limits():
    zones = {**price_zones.config(), "similar": [50, 60, 70, 80]}
    assert deals.position(80, deals.stats(PRICES), None, zones=zones)["level"] == "expensive"     # 76 % of 105


def test_position_labels_and_suspicious():
    i18n_labels = {deals.level_label(x) for x in deals.LEVEL_NAMES}
    assert i18n_labels == {"bargain", "cheap", "fair", "expensive", "very expensive"}
    assert deals.position(10, deals.stats(PRICES), None)["suspicious"] is True
    assert deals.position(100, deals.stats(PRICES), None)["suspicious"] is False


USER_CASE = [120, 135, 150, 170]          # the user's report: 4 comparables, p25 131.25, p75 155


def test_position_by_zones_the_users_case():
    """139 € for a product new from 202,30 €: 68 % of new is "fair" under the default used zones (was "teuer")."""
    p = deals.position(139, deals.stats(USER_CASE), 202.30)
    assert p["basis"] == "geizhals" and p["level"] == "fair" and p["pct"] == 68 and p["axis"] == 110
    assert p["group"] == "used" and p["zones"] == [30, 50, 80, 90] and p["new_price"] == 202.30
    assert p["band"] == {"low": 131.25, "high": 155, "count": 4} and p["pos"] == pytest.approx(0.625, abs=0.001)
    assert "cross" not in p and p["mismatch"] is None and p["suspicious"] is False


def test_enough_comparables_rate_a_used_item_the_zones_a_new_one():
    """A used item's market beats a share of a new price, which for an old product is often a leftover shop's: a
    UniFi AP AC Pro new from 146 €, used ones at 75–79 €, read 45 € as a bargain. New items keep the zones."""
    st = deals.stats(PRICES)                                  # comparables: 105 is fair
    for cond in (None, "used", "like_new"):
        p = deals.position(105, st, 120.0, cond)               # 87 % of new would be expensive by the zones
        assert p["basis"] == "comparables" and p["level"] == "fair" and p["market"] is True
    p = deals.position(105, st, 120.0, "new")
    assert p["basis"] == "geizhals" and p["level"] == "cheap" and p["pct"] == 87     # new items: 75–99 % cheap
    assert deals.position(105, st, None)["market"] is False                         # no new price to beat
    few = deals.position(105, deals.stats([100, 110, 120]), 120.0)                  # under 5 comparables: the zones
    assert few["basis"] == "geizhals" and few["level"] == "expensive"


def test_a_used_item_dearer_than_new_is_expensive_at_best():
    """Comparable ads as dear as this one (bundles, a market above new) do not make a used item above the lowest new
    price fair: new costs no more and comes with warranty and returns."""
    st = deals.stats(PRICES)
    p = deals.position(105, st, 90.0, "used")                  # fair among the comparables, 116 % of new
    assert p["basis"] == "comparables" and p["level"] == "expensive" and p["above_new"] == 116
    assert p["pos"] >= p["zones"][2] / p["axis"]                                    # the ring in its zone
    assert deals.position(105, st, 105.0)["level"] == "expensive"                  # exactly the new price too
    q = deals.position(105, st, 105.01)                                             # a cent below new: the market
    assert q["level"] == "fair" and q["above_new"] is None
    assert deals.position(105, st, 90.0, "new")["basis"] == "geizhals"              # new items: the zones anyway
    r = deals.position(max(PRICES) + 50, st, 90.0)                                  # dearer than all: no note
    assert r["level"] == "very_expensive" and r["above_new"] is None


def test_new_items_use_the_new_zones():
    assert deals.position(195.02, None, 199.0, "new")["level"] == "cheap"      # 98 %: below the lowest shop price
    p = deals.position(205, None, 199.0, "new")
    assert p["level"] == "fair" and p["group"] == "new" and p["axis"] == 140
    for cond in ("like_new", "refurbished", "used", None):                      # everything else: the used zones
        q = deals.position(195.02, None, 199.0, cond)
        assert q["group"] == "used" and q["level"] == "very_expensive"


def test_defect_gets_no_label():
    p = deals.position(50, deals.stats(PRICES), 100.0, "defect")
    assert p == {"pos": None, "reason": "defect"}
    assert "Defective according to the ad" in deals.explain({"position": p, "comparables": {"count": 10}})


def test_suspicious_by_zones():
    assert deals.position(14, None, 100.0)["suspicious"] is True             # below half the first used limit (15 %)
    assert deals.position(15, None, 100.0)["suspicious"] is False
    assert deals.position(37, None, 100.0, "new")["suspicious"] is True      # 37.5 % for new items
    st = deals.stats([100, 110, 120, 130, 140, 150])                         # p25 112.5: below 56.25 is suspicious
    p = deals.position(56, st, 150.0, "new")
    assert p["basis"] == "geizhals" and p["pct"] == 37 and p["suspicious"] is True
    assert deals.position(56, st, 150.0)["suspicious"] is True                   # a used one: by the comparables


def test_hard_mismatch_ignores_the_new_price():
    st = deals.stats([380, 390, 400, 410, 420, 430])                         # median 405 = 4 × new
    p = deals.position(400, st, 100.0)
    assert p["basis"] == "comparables" and p["new_price_ignored"] == 405 and p["level"] == "fair"
    low = deals.position(10, deals.stats([10, 11, 12, 13, 14]), 100.0)      # median 12 % of new
    assert low["basis"] == "comparables" and low["new_price_ignored"] == 12


def test_soft_mismatch_hint():
    st = deals.stats([130, 140, 150, 160, 170])                              # median 150 = 1.5 × new
    p = deals.position(120, st, 100.0, "new")                                # new items: the zones, with the hint
    assert p["basis"] == "geizhals" and p["mismatch"] == 150 and p["level"] == "expensive"
    used = deals.position(140, st, 100.0)                                    # a used one: its comparables decide
    assert used["basis"] == "comparables" and used["level"] == "fair" and "mismatch" not in used
    few = deals.position(140, deals.stats([130, 150, 170]), 100.0)            # under 5 comparables: no hint
    assert few["basis"] == "geizhals" and few["mismatch"] is None and few["band"]["count"] == 3


def test_a_few_comparables_far_below_the_new_price_draw_no_usual_range():
    """A new Raspberry Pi 5 at 106 €, new from 158.82 €: the three ads the filters left at 15–18 € are accessories, not
    its usual price. Five or more stay a range (their own rules handle a mismatch); far above it stays a hint."""
    few = deals.position(106, deals.stats([15, 16.5, 18]), 158.82, "new")
    assert few["basis"] == "geizhals" and few["band"] is None and few["level"] == "bargain"
    fits = deals.position(106, deals.stats([90, 100, 110]), 158.82, "new")
    assert fits["band"] == {"low": 95.0, "high": 105.0, "count": 3}
    above = deals.position(60, deals.stats([300, 320, 340]), 100.0)
    assert above["band"]["count"] == 3


def test_foreign_titles_of_accessories_are_not_the_item():
    """Vinted and eBay show ads from all of Europe: a case, power supply or fan in Italian, French or Spanish."""
    m = deals.TitleMatch("Raspberry PI 5 2 GB di RAM e 64 GB di memoria rom")
    for other in ("Custodia per Raspberry Pi 5", "Alimentatore ufficiale Raspberry Pi 5 27W", "Boîtier pour Raspberry Pi 5",
                  "Ventola attiva Raspberry Pi 5", "Funda para Raspberry Pi 5", "Raspberry Pi 5 dissipatore",
                  "Câble micro HDMI Raspberry Pi 5"):
        assert m.check(other)[1] == "kind", other
    assert m.check("Raspberry Pi 5 2GB")[1] is None
    assert m.check("Raspberry Pi 5 2GB con alimentatore")[1] is None             # comes along: still the item
    assert deals.TitleMatch("Custodia iPhone 13").check("Coque iPhone 13")[1] is None   # a case among cases
    assert deals.TitleMatch("Raspberry Pi 5 4GB Active Cooler Set").check("Raspberry Pi 5 4GB")[1] is None


def test_comparables_rule_by_price():
    st = deals.stats(PRICES)                                                   # median 105: limits 73.5/94.5/120.75/141.75
    assert [deals.comparables_level(x, st) for x in (73.49, 73.5, 94.49, 94.5, 120.74, 120.75, 141.74, 141.75)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]
    for price in range(30, 260):
        p = deals.position(price, st, None)
        assert p["level"] == deals.comparables_level(price, st)
        i = deals.LEVEL_NAMES.index(p["level"])
        lim = [0, *p["zones"], 10_000]
        assert lim[i] / p["axis"] - 0.001 <= p["pos"] <= min(1, lim[i + 1] / p["axis"]) + 0.001


def test_level_at_bounds():
    assert [deals.level_at(x) for x in (0.09, 0.1, 0.24, 0.25, 0.75, 0.76, 0.9, 0.91)] == \
        ["bargain", "cheap", "cheap", "fair", "fair", "expensive", "expensive", "very_expensive"]


def test_zone_span_and_hit_level():
    p = deals.position(139, None, 202.30)
    assert deals.zone_span(p) == {"from_pct": 50, "to_pct": 80, "from": 101.15, "to": 161.84}
    assert deals.zone_span(deals.position(20, None, 202.30)) == {"from_pct": None, "to_pct": 30, "from": None,
                                                               "to": 60.69}
    assert deals.zone_span(deals.position(200, None, 202.30))["to"] is None

    class H:
        price, geizhals, item_condition = 161.84, {"price_from": 202.30}, None
    assert deals.hit_level(H()) == "expensive"
    H.price = 161.83
    assert deals.hit_level(H()) == "fair"
    H.item_condition = "defect"
    assert deals.hit_level(H()) is None
    H.item_condition, H.geizhals = None, {}
    assert deals.hit_level(H()) is None


def test_card_position_reads_the_stats_or_the_stored_position():
    st = deals.stats(PRICES)
    assert deals.card_position({"price": 140, "stats": st})["level"] == "expensive"
    assert deals.card_position({"price": 140, "stats": st, "item_condition": "defect"})["reason"] == "defect"
    legacy = {"price": 140, "position": {"pos": 0.8, "level": "expensive", "basis": "comparables"}}
    assert deals.card_position(legacy)["level"] == "expensive"
    assert deals.card_position({"price": 140, "position": {"pos": 0.5, "level": "expensive"}})["level"] == "fair"
    assert deals.card_position({"price": 140}) == {}


def test_position_not_enough_data():
    assert deals.position(100, deals.stats([90, 100, 110]), None) == {"pos": None, "reason": "few_data"}
    assert deals.position(None, deals.stats(PRICES), 100.0)["reason"] == "no_price"
    assert deals.position(0, deals.stats(PRICES), 100.0)["reason"] == "free"
    res = {"position": {"pos": None, "reason": "few_data"}, "comparables": {"count": 2}}
    assert "only 2 comparable ads" in deals.explain(res)


@pytest.mark.parametrize("text,ad_id", [
    ("1000000001", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-gsr-18v-55-1000000001/", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-gsr-18v-55-1000000001", "1000000001"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/d/iphone-13-1000000002/?utm_source=x", "1000000002"),
    ("https://www.willhaben.at/iad/object?adId=1000000003", "1000000003"),
    ("willhaben.at/iad/object?foo=1&adId=1000000004#top", "1000000004"),
    ("https://m.willhaben.at/iad/gebrauchtwagen/d/auto/vw-golf-1000000005/", "1000000005"),
    ("iPhone 13 128GB", None),
    ("rtx 3080", None),
    ("", None),
])
def test_parse_ad_ref(text, ad_id):
    assert deals.parse_ad_ref(text) == (("willhaben", ad_id) if ad_id else None)
    assert deals.parse_ad_ref("kleinanzeigen:2712345678") == ("kleinanzeigen", "2712345678")


# ------------------------------------------------------------------ analysis with fakes
async def test_comparables_exclude_ad_itself_and_wanted_ads(wh, monkeypatch):
    calls, _ = wh
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    comp = res["comparables"]
    assert calls["search"] == 1 and comp["query"] == "GSR 18V-55"            # the model as sellers write it
    assert comp["count"] == len(PRICES)                     # without the ad itself and the "Suche" ad
    assert AD_ID not in {a["ad_id"] for a in comp["closest"]} and len(comp["closest"]) == 10
    assert [a["price"] for a in comp["closest"]] == sorted(a["price"] for a in comp["closest"])
    assert res["position"]["basis"] == "comparables" and res["geizhals"] is None


async def test_caching_avoids_repeat_requests(wh, gh):
    calls, _ = wh
    first = await deals.analyze(_ad(100))
    assert calls["search"] == 1 and len(gh) == 1               # one Geizhals search, no product page (card price)
    again = await deals.analyze(_ad(100))
    assert again == first and calls["search"] == 1 and len(gh) == 1     # re-rated on read: an equal copy
    other = await deals.analyze(_ad(90, ad_id="1000000002"))    # other ad, same query: search + Geizhals cached
    assert calls["search"] == 1 and len(gh) == 1 and other["ad"]["ad_id"] == "1000000002"
    await deals.analyze(_ad(100), query="Akkuschrauber Bosch")   # new query -> exactly one new search
    assert calls["search"] == 2


async def test_geizhals_match_and_optional(wh, gh):
    res = await deals.analyze(_ad(100))
    assert res["geizhals"] == {"name": "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo",
                               "url": "https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html",
                               "price_from": 139.0}
    plain = await deals.analyze(_ad(100, heading="Alter Holzschrank Eiche", ad_id="1000000003"))
    assert plain["geizhals"] is None and len(gh) == 1           # generic title: Geizhals not asked


async def test_geizhals_error_and_mismatch_give_none(wh, monkeypatch):
    async def boom(q, **kw):
        raise geizhals.GeizhalsError("HTTP 503")
    monkeypatch.setattr(deals.geizhals, "lookup", boom)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None

    async def other(q, **kw):
        return {"match": {"name": "Makita DDF484Z", "url": "https://geizhals.at/x-a1.html", "price_from": 99.0}}
    monkeypatch.setattr(deals.geizhals, "lookup", other)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None


async def test_few_comparables_use_geizhals(wh, gh):
    _, state = wh
    state["search"] = _search_json([100, 120])
    res = await deals.analyze(_ad(70))
    assert res["comparables"]["count"] == 2 and res["position"]["basis"] == "geizhals"
    assert res["position"]["new_price"] == 139.0


async def test_claude_only_when_asked(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100))
    assert fake.calls == [] and res["claude"] is None
    res = await deals.analyze(_ad(100), use_claude=True)
    assert len(fake.calls) == 1 and res["claude"]["deal"] == "good"
    assert "Median 105 €" in fake.calls[0]["prompt"] and "Bosch GSR 18V-55" in fake.calls[0]["prompt"]
    res = await deals.analyze(_ad(100), use_claude=True)        # cached verdict, no second call
    assert len(fake.calls) == 1 and res["claude"]["deal"] == "good"


async def test_claude_failure_is_not_cached(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    monkeypatch.setattr(deals, "get_backend", lambda *a: FakeClaude(fail=True))
    res = await deals.analyze(_ad(100), use_claude=True)
    assert res["claude"] == {"error": "rate limited"}
    assert deals.cached(AD_ID)["claude"] is None


# ------------------------------------------------------------------ pages
@pytest.fixture
def app_client(wh, gh, monkeypatch):
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    from app.main import app

    with TestClient(app) as c:
        c.fake, c.calls = fake, wh[0]
        yield c


def test_viewer_has_lazy_price_section(app_client):
    page = app_client.get(f"/hits/ad/{AD_ID}").text
    assert f'hx-get="/hits/ad/{AD_ID}/deal?quick=1"' in page and 'hx-trigger="load delay:700ms"' in page
    assert app_client.calls["search"] == 0                       # opening the ad does not search yet
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal").text
    assert "deal-zones" in frag and "10 comparable ads" in frag and "fair" in frag
    assert "For used items enough comparable ads decide" in frag and "this ad is 71 % of new" in frag   # 10 ads
    assert 'href="https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html" target="_blank" rel="noopener"' in frag
    assert "new from €\xa0139 on Geizhals" in frag and "Ask Claude" in frag        # the amount never breaks
    assert "Comparable ads" in frag and "closest by price" in frag   # list in the viewer too
    assert "this ad" in frag
    assert app_client.fake.calls == []
    app_client.get(f"/hits/ad/{AD_ID}/deal")                     # reopening: no new requests
    assert app_client.calls == {"search": 1, "detail": 1}


def test_viewer_keeps_screening_and_uses_agent_keyword(app_client, wh):
    _, state = wh
    state["detail"] = _detail_html(heading="neu OVP")            # title gives no query
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Bohrer", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=bohrschrauber",
                              webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = dbmod.Hit(agent_id=a.id, ad_id=AD_ID, heading="neu OVP", url="u", screen_relevant=True, screen_deal="good",
                      screen_reason="Passt.")
        db.add(h)
        db.commit()
        db.refresh(h)
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text
    assert "Ask Claude" not in frag   # screened hit: assessment already shown


def test_price_section_follows_a_condition_the_hit_learned_since(app_client, monkeypatch):
    """The first check knew no condition (used: its 10 comparables decide). Once the hit knows one (the viewer wrote
    what the ad page said), the cached check is re-rated with it at once, without a new search: new items by the
    zones."""
    from app.platforms import condition
    monkeypatch.setattr(condition, "willhaben_detail", lambda ad: (None, None))     # a page that states none
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Bosch", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=bosch",
                              webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = dbmod.Hit(agent_id=a.id, ad_id=AD_ID, heading="Bosch GSR 18V-55", url="u")
        db.add(h)
        db.commit()
        db.refresh(h)
    frag = _plain(app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text)
    assert "For used items enough comparable ads decide" in frag and "means:" not in frag
    searches = app_client.calls["search"]
    with dbmod.session() as db:
        hit = db.get(dbmod.Hit, h.id)
        hit.item_condition, hit.item_condition_label = "new", "Neu"
        db.add(hit)
        db.commit()
    frag = _plain(app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text)
    assert "Condition according to the ad: Neu." in frag and "For new items „bargain“ means: below 75 %" in frag
    assert app_client.calls["search"] == searches
    frag = _plain(app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text)          # now cached with it
    assert "For new items „bargain“" in frag and app_client.calls["search"] == searches


def test_deals_pages_and_claude_on_post(app_client):
    page = app_client.get("/deals")
    assert page.status_code == 200 and "Product search" in page.text and "up to 4 willhaben/Geizhals requests" in page.text
    assert 'href="/deals"' in page.text                          # nav tab

    r = app_client.get("/deals", params={"q": f"https://www.willhaben.at/iad/kaufen-und-verkaufen/d/bosch-{AD_ID}/"},
                       follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/deals/ad/{AD_ID}"

    listing = app_client.get("/deals", params={"q": "Bosch GSR 18V-55"}).text
    assert "Bosch GSR 18V-55 Nr 0" in listing and f"/deals/ad/{AD_ID}?q=Bosch%20GSR%2018V-55" in listing
    assert "bg-lvl-" in listing and app_client.calls["search"] == 1

    page = app_client.get(f"/deals/ad/{AD_ID}", params={"q": "Bosch GSR 18V-55"}).text   # answers at once ...
    assert f'hx-get="/deals/ad/{AD_ID}/result?q=Bosch%20GSR%2018V-55' in page
    assert app_client.calls["search"] == 1                                    # ... without willhaben requests
    result = app_client.get(f"/deals/ad/{AD_ID}/result", params={"q": "Bosch GSR 18V-55"}).text   # ... the check loads into it
    assert "Comparable ads" in result and "this ad" in result and "<html" not in result
    # a pasted link lands here: the way to write to the seller (and make an offer) is on the page, via the ad viewer
    assert f'data-ad="{AD_ID}" data-deal-message' in result or f'data-ad="willhaben:{AD_ID}" data-deal-message' in result
    assert "Recently checked" in app_client.get(f"/deals/ad/{AD_ID}").text      # the check is remembered
    assert app_client.calls["search"] == 2   # comparables served from cache; +1 for the category navigator (selector)
    assert 'name="cat"' in result and "Automatic (all categories)" in result
    assert 'name="was" value="Bosch GSR 18V-55"' in result and 'name="own"' not in result
    assert app_client.fake.calls == []                           # never automatic

    verdict = app_client.post(f"/deals/ad/{AD_ID}/claude", data={"q": "Bosch GSR 18V-55"}).text
    assert len(app_client.fake.calls) == 1 and "Claude&#39;s assessment" in verdict and "30 %" in verdict
    again = app_client.get(f"/deals/ad/{AD_ID}/result", params={"q": "Bosch GSR 18V-55"}).text
    assert "Claude&#39;s assessment" in again and len(app_client.fake.calls) == 1
    with dbmod.session() as db:
        row = db.get(dbmod.DealCheck, AD_ID)
    assert row.claude["deal"] == "good" and row.result["position"]["level"]


def test_deals_page_german(app_client):
    from app.db import set_setting
    set_setting("ui_lang", "de")
    page = app_client.get("/deals").text
    assert "Produktsuche" in page and "Claude wird nur auf Klick gefragt" in page
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal").text
    assert "neu ab € 139 auf Geizhals" in _plain(frag) and "vergleichbare Anzeigen" in frag


# ------------------------------------------------------------------ review fixes
def test_matches_model_numbers_as_whole_words():
    assert deals._matches("Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo", "Bosch GSR 18V-55")
    assert not deals._matches("Apple iPhone 113 Pro", "Apple iPhone 13")
    assert not deals._matches("Tisch 230cm Eiche", "Tisch 23 Eiche")


@pytest.mark.parametrize("name, query, ok", [
    ("Raspberry Pi 4 Modell B, 8GB RAM", "Raspberry Pi 4 4GB", False),         # other variant
    ("Raspberry Pi 4 Modell B, 4GB RAM", "Raspberry Pi 4 4GB", True),
    ("Raspberry Pi 4 Modell B 4 GB", "Raspberry Pi 4 4GB", True),               # "4 GB" = "4GB"
    ("Raspberry Pi 5, 8GB RAM", "Raspberry Pi 4", False),                       # single digit counts too
    ("Raspberry Pi 400", "Raspberry Pi 4", False),
    ("Raspberry Pi 4 Modell B, 8GB RAM", "Raspberry Pi 4", True),               # query without capacity
])
def test_matches_variants(name, query, ok):
    assert deals._matches(name, query) is ok


async def test_geizhals_cache_per_variant_and_only_verified(wh, monkeypatch):
    asked = []

    async def lookup(q, **kw):
        asked.append(q)
        gb = "8GB" if "8GB" in q else "4GB"
        return {"query": q, "verified": True, "candidates": [],
                "match": {"name": f"Raspberry Pi 4 Modell B, {gb} RAM", "url": f"https://geizhals.at/pi4-{gb}-a1.html",
                          "price_from": 89.9 if gb == "8GB" else 59.9}}
    monkeypatch.setattr(deals.geizhals, "lookup", lookup)
    four = await deals.geizhals_match(deals.geizhals_query("Raspberry Pi 4 Model B 4 GB"))
    eight = await deals.geizhals_match(deals.geizhals_query("Raspberry Pi 4 Model B 8GB"))
    assert four["price_from"] == 59.9 and eight["price_from"] == 89.9 and len(asked) == 2
    assert len(deals._geizhals) == 2                                         # one cache entry per variant

    async def guess(q, **kw):                                                      # Geizhals' first hit, not confirmed
        return {**await lookup(q), "verified": False}
    monkeypatch.setattr(deals.geizhals, "lookup", guess)
    assert await deals.geizhals_match("Raspberry Pi 4 Modell 4GB") is None


async def test_geizhals_error_is_negatively_cached(wh, monkeypatch):
    n = []

    async def boom(q, **kw):
        n.append(q)
        raise geizhals.GeizhalsError("HTTP 429")
    monkeypatch.setattr(deals.geizhals, "lookup", boom)
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None
    assert await deals.geizhals_match("Bosch GSR 18V-55") is None
    assert len(n) == 1
    deals._geizhals_fail["bosch gsr 18v-55"] = (deals.time.monotonic() - deals.GEIZHALS_FAIL_TTL_S - 1, None)
    await deals.geizhals_match("Bosch GSR 18V-55")
    assert len(n) == 2


async def test_viewing_does_not_extend_freshness(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    old = {**res, "checked_at": (dbmod.utcnow() - timedelta(seconds=deals.RESULT_TTL_S + 60)).isoformat()}
    deals.remember(old)                                         # viewed just now, analysed over an hour ago
    deals.clear_cache()
    assert deals.cached(AD_ID) is None
    fresh = {**res, "checked_at": (dbmod.utcnow() - timedelta(seconds=deals.RESULT_TTL_S - 60)).isoformat()}
    deals.remember(fresh)
    deals.clear_cache()
    assert deals.cached(AD_ID) is not None
    assert deals.time.monotonic() - deals._results[(AD_ID, "")][0] > deals.RESULT_TTL_S - 120


async def test_claude_verdict_tied_to_price(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    fake = FakeClaude()
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), use_claude=True)
    deals.remember(res)
    assert res["claude"]["price"] == 100.0 and len(fake.calls) == 1
    deals.clear_cache()
    cheaper = await deals.analyze(_ad(80))                     # seller lowered the price: old verdict not shown
    assert cheaper["claude"] is None
    await deals.analyze(_ad(80), use_claude=True)
    assert len(fake.calls) == 2


async def test_results_before_the_size_filter_are_recomputed(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    res = await deals.analyze(_ad(100))
    assert res["comparables"]["filters"] == deals.FILTERS
    old = {**res, "comparables": {k: v for k, v in res["comparables"].items() if k != "filters"}}
    deals.remember(old)                                          # stored by a version without the size filter
    deals.clear_cache()
    assert deals.cached(AD_ID) is None
    deals.remember(res)
    deals.clear_cache()
    assert deals.cached(AD_ID) is not None


def test_deals_page_rejects_non_ad_links(app_client):
    page = app_client.get("/deals", params={"q": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552"}).text
    assert "This link is not an ad on a supported site." in page and app_client.calls["search"] == 0



def _summ(n, title, price, cats="2691;5013000"):
    return {"ad_id": f"20000000{n:02d}", "title": title, "price": price, "category_ids": cats.split(";")}


async def test_comparables_same_item_and_trimmed(monkeypatch):
    """iPhone 15 Pro at 490: cases, a 15 Pro Max and a €1 swap must not shape the bar."""
    phone, case_cat = "2691;2722;2724;5013241", "2691;2722;2752"
    ads = [_summ(n, "iPhone 15 Pro 128GB", p, phone) for n, p in enumerate([520, 560, 600, 640, 580, 610, 590])]
    ads += [_summ(20, "iPhone 15 Pro Max 256GB", 900, phone), _summ(21, "iPhone 15 Pro Hülle MagSafe", 12, phone),
            _summ(22, "Apple iPhone 15 Pro", 1, phone)]
    ads += [_summ(30 + n, "Panzerglas iPhone 15 Pro", 8, case_cat) for n in range(6)]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("iPhone 15 pro", AD_ID, 490, title="iPhone 15 pro")
    assert out["scope"] is None and out["count"] == 7
    assert out["filtered"] == {"model": 1, "kind": 7, "condition": 0, "outliers": 1}
    assert out["stats"]["min"] == 520 and out["stats"]["max"] == 640
    assert {a["title"] for a in out["closest"]} == {"iPhone 15 Pro 128GB"}           # the list is what the stats use
    assert deals.position(490, out["stats"], None)["level"] == "cheap"        # 82 % of the median; "teuer" against cases at €12


async def test_comparables_ignore_the_ads_category(monkeypatch):
    """The same drill sits in several willhaben leaves (an ad in two trees reads "[…, …]"): the title decides."""
    cats = ["3541;3778;3817;3818;3819", "3541;3778;3817;3818;3821", "3541;3778;3817;3834;3843", "3541;3778;3779;3816",
            "[5387;5714;5717, 5387;5418;5430]"]
    ads = [_summ(n, "Bosch Professional GSR 18V-55", 100 + 10 * n, c) for n, c in enumerate(cats)]
    ads += [_summ(10, "Bosch Professional 18V Geräteset", 390, cats[0])]          # found by its description
    _fake_search(monkeypatch, ads)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    ad = _ad(120)
    ad.category_id = "3819"
    res = await deals.analyze(ad)
    assert res["comparables"]["count"] == 5 and res["position"]["basis"] == "comparables"


async def _no_geizhals(q):
    return None


@pytest.mark.parametrize("other, ad, ok", [
    # spellings: glued, abbreviated, spelled out, cut short
    ("UniFi U7pro", "UniFi U7 Pro WiFi 7 Access Point mit Halterung", True),
    ("Apple iPhone 13mini", "Apple iPhone 13 128GB", False),
    ("Sony PlayStation5", "Sony PlayStation 5", True),
    ("Kinderwagen Bugaboo Fox3", "Bugaboo Fox 3 Kinderwagen", True),
    ("UniFi 6 Lite Access Point", "UniFi Access Point U6 Lite", True),                 # U6 = UniFi 6
    ("Sony PS5 Digital Edition", "Sony PlayStation 5 Digital Edition", True),
    ("Ubiquiti UniFiSwitch 24 Gigabit Managed PoE Switch, 24x RJ-45, 2x SFP USW-24-POE",
     "Ubiquiti UniFi Switch USW-24-POE", True),
    ("Dyson V11 Abolute", "Dyson V11 Absolute Akkusauger", True),
    ("Ikea-Regal Kallax", "IKEA Kallax Regal 4x4 weiss", True),
    # the comparables of the reported U6 Mesh check: none is that access point
    ("Ubiquiti UniFi Cloud Key UC-CK", "Ubiquiti UniFi U6 Mesh WiFi 6 Access Point neuwertig", False),
    ("Ubiquiti UniFi U-POE++ PoE-Injektor (60W) - Neuwertig", "Ubiquiti UniFi U6 Mesh WiFi 6 Access Point", False),
    ("Unifi - Ubiquiti - U6 lite Cover in Concrete", "Ubiquiti UniFi U6 Mesh WiFi 6 Access Point", False),
    ("Ubiquiti UniFi nanoHD-RCM-3 Deckeneinbauhalterung 3er-Set", "Ubiquiti UniFi U6 Mesh WiFi 6 Access Point", False),
    ("UniFi 6 Professional Access Point", "Ubiquiti UniFi U6 Mesh WiFi 6 Access Point", False),   # the U6 Pro
    # other models and variants
    ("Dyson V11 Absolute Extra", "Dyson V11 Absolute", False),
    ("iPhone 15 Pro Max", "iPhone 15 Pro", False),
    ("Raspberry Pi 5 4GB", "Raspberry Pi 4 4GB", False),
    ("Raspberry Pi 4 8GB", "Raspberry Pi 4 4GB", False),
    ("Raspberry Pi Compute Module 4", "Raspberry Pi 4", False),
    ("Raspberry Pi 3 Model B / B+ / 4 + CM4 Adapter Konvolut", "Raspberry Pi 4 4GB", False),
    ("Dyson Led Fugendüse V7 V8 V10 V11", "Dyson V11 Absolute", False),               # a compatibility list
    ("Bugaboo Fox, Fox 2, Fox 3 Fahrgestell", "Bugaboo Fox 3", False),
    ("Apple iPhone 13/ 256", "Apple iPhone 13 128GB", False),
    ("Bosch GSB 18V-55", "Bosch GSR 18V-55", False),                                  # another line
    ("Sony PlayStation 2 Tekken 5", "Sony PlayStation 5", False),
    ("Bosch Professional 18V Geräteset", "Bosch GSR 18V-55 Akkuschrauber", False),    # no model: a description hit
    ("Ubiquiti UniFi Switch 24 250W (US-24-250W)", "Ubiquiti UniFi Switch US-24", False),   # a longer order code
    ("Ikea Kallax 2x4", "IKEA Kallax Regal 4x4 weiß", False),
    ("Kinderfahrrad 24 Zoll", "Kinderfahrrad 20 Zoll Puky", False),
    ("IKEA Billy Regal", "IKEA Kallax Regal", False),
    # accessories, parts and extras
    ("Dyson V11 ABSOLUTE mit Zubehör, Ladegerät & Wandhalterung", "Dyson V11 Absolute", True),
    ("Bugaboo Fox 3 + Zubehör", "Bugaboo Fox 3 Kinderwagen", True),
    ("Bosch Akku-Bohrschrauber GSR 18V-55", "Bosch GSR 18V-55", True),               # cordless, not a battery
    ("Dyson V11 absolute Akku Staubsauger", "Dyson V11 Absolute", True),
    ("Akku Bohrschrauber 2x Akku 4.0Ah Bosch Professional GSR 18V-55", "Bosch GSR 18V-55 Akkuschrauber", True),
    ("Apple iPhone 13 128GB Akku 89%", "Apple iPhone 13 128GB", True),                # the battery's health
    ("NEUER 3900mAh Akku für Dyson V11", "Dyson V11 Absolute", False),
    ("Handyhülle für Apple iPhone 13", "Apple iPhone 13", False),
    ("Hutschienengehäuse für Raspberry Pi 4", "Raspberry Pi 4 4GB", False),
    ("Sony Playstation 5 Controller", "Sony PlayStation 5", False),
    ("Apple iPhone 13 (Nur! Schachtel + Zubehör)", "Apple iPhone 13", False),
    ("iPhone 15 Pro OVP nur 2 Monate alt", "iPhone 15 Pro", True),
    ("Ubiquiti UniFi US-48-500W Ersatzteilträger defekt", "Ubiquiti UniFi US-48-500W", False),
    ("Apple Leather Case iPhone 13", "Apple iPhone 13 Hülle", True),                  # an accessory: the same kind
    ("Apple iPhone 13", "Hülle für iPhone 13", False),
    ("iPhone 15 Pro Display defekt", "iPhone 15 Pro defekt", True),                   # the ad itself is defective
    ("iPhone 15 Pro Display defekt", "iPhone 15 Pro", False),
    # products no word list here was written for
    ("Samsung S23 Ultra", "Samsung Galaxy S23 Ultra 256GB", True),
    ("Samsung Galaxy S23", "Samsung Galaxy S23 Ultra 256GB", False),
    ("RTX 3080 10GB", "MSI GeForce RTX 3080 Gaming X Trio 10GB", True),
    ("RTX 3080 Ti", "MSI GeForce RTX 3080 Gaming X Trio 10GB", False),
    ("Canon 80D", "Canon EOS 80D Body", True),
    ("Canon EOS 80D Kamera", "Canon EOS 80D Body", True),                              # a camera is no accessory
    ("FIFA 23 PS5", "FIFA 23 PS5 Spiel", True),
    ("Sony WH-1000XM5", "Sony WH-1000XM4 Kopfhörer", False),
    ("Lego 42115", "LEGO Technic 42115 Lamborghini Sián", True),
    ("Apple Watch Series 8 41mm", "Apple Watch Series 8 45mm GPS", False),
    ("Nintendo Switch", "Nintendo Switch OLED", False),
    ("Cybex Priam", "Cybex Priam Kinderwagen", True),
    ("Cybex Balios Kinderwagen", "Cybex Priam Kinderwagen", False),
    ("MacBook Air 13 Zoll M1 8GB 256GB", "MacBook Air M1 8GB 256GB", True),
    ("iMac M1", "MacBook Air M1 8GB 256GB", False),
    ("Lego 42115", "LEGO Technic 42115 Lamborghini Sián", True),
    ("Bosch GSR 18V-55 Professional Akku-Bohrschrauber", "Bosch GSR 18V-55 Akkuschrauber", True),   # Bosch's line
    # the model only in the extras: the item is something else
    ("Gaming PC + PS5", "Sony PlayStation 5", False),
    ("Neu mit OVP: Apple iPhone 13", "Apple iPhone 13", True),                       # extras before the name
    # condition both ways
    ("iPhone 13 128GB", "iPhone 13 Display defekt", False),
    ("iPhone 13 kaputt", "iPhone 13 Display defekt", True),
    # more accessories and parts
    ("MacBook Air M1 Tastatur", "MacBook Air M1 8GB 256GB", False),
    ("Bugaboo Fox 3 Sitzeinheit", "Bugaboo Fox 3 Kinderwagen", False),
    ("Bugaboo Fox 3 Babywanne", "Bugaboo Fox 3 Kinderwagen", False),
    ("Dyson Halter V7 V8 V10 V11 V15", "Dyson V11 Absolute", False),
    ("Raspberry Pi Netzstecker", "Raspberry Pi 4 4GB", False),
    ("Steelcase Bürostuhl schwarz", "Bürostuhl ergonomisch schwarz", True),          # a brand, no case
    # titles without a model: what the item is, not how it looks
    ("Bürostuhl grau", "Bürostuhl ergonomisch schwarz", True),
    ("Ergonomischer Bürostuhl Drehstuhl", "Bürostuhl ergonomisch schwarz", True),
    ("Ergonomischer Bürostuhl schwarz/ weiß - Mesh Rückenlehne", "Bürostuhl ergonomisch schwarz", True),
    ("Drehstuhl schwarz", "Bürostuhl ergonomisch schwarz", False),
    ("Nintendo Switch Lite grau", "Nintendo Switch Lite türkis", True),
    ("Nintendo Switch grau", "Nintendo Switch Lite türkis", False),
    ("Couchtisch Glas eckig", "Couchtisch Holz rund", True),
    ("Beistelltisch Holz rund", "Couchtisch Holz rund", False),
    ("Ecksofa grau", "Ecksofa grau Schlaffunktion", True),
    ("Kinderhochbett mit Rutsche und Vorhang", "Hochbett mit Rutsche", True),
    ("IKEA Billy Regal weiß", "IKEA Kallax Regal 4x4 weiss", False),
    ("Kommode Hemnes 8 Schubladen", "Weiße Kommode mit 3 Schubladen", False),       # 8 drawers, not 3
    # clothing sizes are no model, "XL" no variant
    ("Damen Winterjacke, Größe XL, schwarz", "Damen Winterjacke, Größe M, schwarz", True),
    ("Damen Winterjacke grau Gr. 40", "Damen Winterjacke schwarz in 44", True),
    ("Herren Lederjacke schwarz Größe 50", "Herren Lederjacke braun gr.48", True),
    ("Herren Winterjacke schwarz", "Damen Winterjacke schwarz", False),
    # tyres: the size is the model, the season its line
    ("Michelin Sommerreifen 205/55 R16", "Sommerreifen 205/55 R16", True),
    ("Sommerreifen 205/60 R16", "Sommerreifen 205/55 R16", False),
    ("Winterreifen 205/55 R16", "Sommerreifen 205/55 R16", False),
])
def test_similarity(other, ad, ok):
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok


@pytest.mark.parametrize("title", [
    "Sommerreifen 205/55 R16", "205/55/ R16 Sommerreifen", "Gr. 98/104 Matschhose", "Kinderjacke 128/134",
    "ECKSOFA 244/157 cm in Velours Grau Schlaffunktion, Stauraum", "MTB 27,5 Zoll", "Damen Winterjacke schwarz 44 L",
    "Bugaboo Fox, Fox 2, Fox 3 Fahrgestell", "Dyson Led Fugendüse V7 V8 V10 V11", "Raspberry pi 4/5 flirc case",
    "Apple iPhone 13 (Nur! Schachtel + Zubehör)", "Kinderfahrrad 20zoll und 12zoll", "Weiße Kommode mit 3 Schubladen",
    "Ubiquiti UniFiSwitch Flex Desktop 2.5G Smart Switch - USW-Flex-2.5G-8", "Hochbett mit Rutsche",
])
def test_a_title_is_its_own_comparable(title):
    """Numbers the ad itself names ("205/55", "98/104", "Fox 2, Fox 3") are no other model, its own extras no
    accessory: an ad's title always passes against itself, else such ads never get comparables."""
    assert deals.TitleMatch(title).check(title) == (1.0, None)


def test_why_an_ad_is_no_comparable():
    m = deals.TitleMatch("Apple iPhone 13 128GB")
    assert m.check("Apple iPhone 13 128GB Mitternacht") == (1.0, None)
    assert m.check("Apple iPhone 13 mini")[1] == "model"
    assert m.check("Apple iPhone 12 128GB")[1] == "model"
    assert m.check("Handyhülle für Apple iPhone 13")[1] == "kind"
    assert m.check("Apple iPhone 13 128GB Display defekt")[1] == "condition"


def test_similarity_scores():
    assert deals.similarity("Apple iPhone 13", "Apple iPhone 13") == 1.0
    assert deals.similarity("iPhone 13", "Apple iPhone 13") == pytest.approx(0.833, abs=0.001)   # no brand
    assert deals.similarity("Apple 13 Pro Max Hülle", "Apple iPhone 13") == 0.0                   # variant
    assert deals.similarity("Tekken 5 Sony", "Sony PlayStation 5") < 0.7                        # model, no line
    assert deals.similarity("anything", "") == 1.0                       # nothing to compare with: no filter


@pytest.mark.parametrize("other, ad, ok", [
    ("Kallax 2x4", "Kallax 4x4", False), ("Kallax", "Kallax 4x4", True), ("Fahrrad 20\"", "Fahrrad 20 Zoll", True),
    ("Fahrrad 24 Zoll", "Fahrrad 20 Zoll", False), ("Apple Watch 41mm", "Apple Watch 45 mm", False),
    ("Kallax 147 cm", "Kallax 4x4", True),                                     # another kind of measure says nothing
    ("Malm Kommode 3 Schubladen", "IKEA Malm Kommode 6 Schubladen", False), ("Sofa 3-Sitzer", "Sofa 2 Sitzer", False),
    ("Schrank 2-türig", "Schrank 2 Türen", True), ("MTB 27,5 Zoll", "MTB 27.5\"", True),
])
def test_same_measures(other, ad, ok):
    assert deals._same_measures(other, ad) is ok


@pytest.mark.parametrize("other, ad, ok", [
    ("Raspberry Pi 4 8GB", "Raspberry Pi 4 4GB", False),
    ("Raspberry Pi 4 Model B", "Raspberry Pi 4 4GB", True),                   # no size: may be any, kept
    ("Raspberry Pi 4 4 GB + 32GB SD-Karte", "Raspberry Pi 4 4GB", True),      # names the ad's size too
    ("Raspberry Pi 4 4GB", "Raspberry Pi 4 4 GB", True),                      # "4 GB" = "4GB"
    ("MacBook Air M1 16GB 256GB", "MacBook Air M1 8GB 256GB", False),
    ("MacBook Air M1 8GB 512GB", "MacBook Air M1 8GB 256GB", False),
    ("MacBook Air M1 256GB", "MacBook Air M1 8GB 256GB", True),
    ("MacBook Air M1 8 GB / 256 GB", "MacBook Air M1 8GB 256GB", True),
    ("Samsung T7 1,5 TB", "Samsung T7 1.5TB", True),
    ("Raspberry Pi 4 8GB", "Raspberry Pi 4", True),                           # ad without a size: no change
    ("Anker Powerbank 20000mAh", "Anker Powerbank 20.000 mAh", True),         # thousands separator
    ("Anker Powerbank 10.000 mAh", "Anker Powerbank 20000mAh", False),
    ("Samsung T7 2TB", "Samsung T7 2.0TB", True),
    ("MacBook Air M1 16/256GB", "MacBook Air M1 8GB 256GB", False),           # RAM/storage
    ("MacBook Air M1 8/256 GB", "MacBook Air M1 8GB 256GB", True),
    ("Raspberry Pi 4 / 8GB", "Raspberry Pi 4 4GB", False),                    # model / size, not RAM/storage
])
def test_same_size(other, ad, ok):
    assert deals._same_size(other, ad) is ok


def test_trim_keeps_a_second_group():
    """Extreme prices are few; 5 consoles among 6 games are a second group – trimming around the games' median would
    drop the consoles, so nothing is dropped."""
    ads = [{"price": p} for p in [480, 500, 520, 550, 600, 15, 20, 25, 30, 35, 40]]
    assert deals._trim(ads) == ads
    ads = [{"price": p} for p in [1, 480, 500, 520, 550, 600]]
    assert [a["price"] for a in deals._trim(ads)] == [480, 500, 520, 550, 600]


def test_trim_drops_placeholder_prices_however_many():
    """Two "1 €" ads among six at 280–330 are more than TRIM_MAX, yet no second group: placeholders always go."""
    ads = [{"price": p} for p in [1, 1, 280, 290, 300, 310, 320, 330]]
    assert [a["price"] for a in deals._trim(ads)] == [280, 290, 300, 310, 320, 330]
    ads = [{"price": p} for p in [1, 280, 290, 300, 310, 320, 330, 1500, 1600]]
    assert [a["price"] for a in deals._trim(ads)] == [280, 290, 300, 310, 320, 330, 1500, 1600]   # 2 of 8 high


@pytest.mark.parametrize("other, ad, ok", [
    ("Sony playstation 5", "PS5", True), ("Sony Playstation 5", "Sony PS5", True), ("PlayStation5", "PS5", True),
    ("SONY PLAYSTATION 5 DIGITAL EDITION", "PS5 Digital Edition", True), ("playstation 5 digital", "PS5 Digital", True),
    ("PS5", "Sony playstation 5", True), ("Sony PS5 Digital", "SONY PLAYSTATION 5 DIGITAL EDITION", True),
    ("PS5 Digital Edition", "PlayStation5 Digital", True), ("Sony Playstation 5 Konsole", "Sony PS5 Konsole", True),
    ("Sony Playstation 4 Pro", "Sony PS4 Pro", True), ("Sony Playstation 4", "PS4", True),
    ("Philips 5", "PS5", False), ("Pens 5", "PS5", False), ("Playstation4", "PS5", False),   # not the letters, model
    ("Sony PlayStation 5", "PS5 Digital Edition", False),
    # other words with the letters: only the aliases count, not any word that has them in order
    ("Persona 5 Royal", "Sony PS5", False), ("Persona 5 Royal", "PS5", False),
    ("Persona 5 Strikers", "PS5 Konsole", False),
    ("Persona 4 Golden", "Sony PS4", False), ("Postal 4", "Sony PS4", False), ("Persona 3 Reload", "Sony PS3", False),
    ("Passat 5", "Sony PS5", False), ("Passat 5", "PS5", False), ("Pistole 5", "Sony PS5 Konsole", False),
    ("Pass 2", "Sony PS2", False), ("Maxi 5", "Mazda MX5", False),
])
def test_abbreviation_whatever_the_case(other, ad, ok):
    """"PS5" is "PlayStation 5" however the title writes it: "Playstation", "PLAYSTATION", glued "PlayStation5" – by
    a short alias table, so "Persona 5" or "Passat 5" are no PS5."""
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok


@pytest.mark.parametrize("other, ad", [
    ("Sony Playstation 5 Controller", "Sony PS5"), ("Sony PlayStation 5 Controller", "Sony PS5"),
    ("Sony Playstation 5 Controller", "PS5"), ("Sony PlayStation 5 Controller", "PS5"),
    ("Playstation 5 Controller", "PS5 Konsole"), ("Playstation 5 Spiele", "PS5 Konsole"),
    ("Playstation 5 Spiele", "Sony PS5"), ("Playstation 5 Ladestation", "Sony PS5"),
    ("PLAYSTATION 5 Controller", "Sony PS5"), ("Playstation5 Controller", "Sony PS5"), ("PS 5 Controller", "Sony PS5"),
    ("Sony Playstation 5 Digital Edition Controller", "Sony PS5 Digital Edition"),
    ("Playstation 5 Slim Controller", "PS5 Slim"), ("Sony Playstation 5 Controller", "Neue PS5"),
])
def test_the_number_of_the_model_is_no_count(other, ad):
    """"2 Controller" are extras, but the "5" of "Playstation 5 Controller" names the PS5: the ad is a controller."""
    assert deals.TitleMatch(ad).check(other)[1] == "kind"


@pytest.mark.parametrize("ad, others", [
    ("Neuwertige PS5 Digital Edition", ["PS5 Digital Edition", "Sony PS5 Digital Edition"]),
    ("Neue PS5", ["PS5", "Sony PS5", "Sony PlayStation 5"]),
    ("Meine PS5 Digital", ["PS5 Digital Edition", "Sony PS5 Digital Edition"]),
    ("Schwarze PS5 Slim Digital", ["PS5 Slim Digital", "Sony PS5 Slim Digital"]),
])
def test_no_product_line_in_words_before_the_model(ad, others):
    """Praise, possessives and colours before the model are no product line: the model comes first, any brand will
    do."""
    m = deals.TitleMatch(ad)
    assert m.anchor is None and m.weights == {}
    assert all(m.check(o) == (1.0, None) for o in others)


@pytest.mark.parametrize("other, ad, ok", [
    ("Bose 700 Kopfhörer", "Bose Soundbar 700", False),
    ("Bose 700 Noise Cancelling Kopfhörer", "Bose Soundbar 700", False),
    ("Bose Soundbar 700", "Neue Bose Soundbar 700", True), ("Bose Soundbar 600", "Bose Smart Soundbar 600", True),
    ("Michelin Sommerreifen 205/55 R16", "Kompletträder Sommerreifen (Alu) 205/55 R16", False),
    ("Sommerreifen 205/55 R16", "Kompletträder Sommerreifen (Alu) 205/55 R16", False),
    ("Kompletträder Sommerreifen Alu 205/55 R16", "Kompletträder Sommerreifen (Alu) 205/55 R16", True),
])
def test_a_line_word_that_ends_like_an_adjective_stays_the_line(other, ad, ok):
    """"Soundbar" ends like "ausziehbar", "Alu" is a material: before the model they still say what the item is."""
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok


@pytest.mark.parametrize("a, b", [
    ("Nintendo Switch", "Nintendo Switch OLED"), ("Nintendo Switch", "Nintendo Switch (OLED-Modell) weiß"),
    ("Nintendo Switch", "Nintendo Switch OLED Zelda Edition"), ("Nintendo Switch Neon", "Nintendo Switch OLED Neon"),
    ("Nintendo Switch", "Nintendo Switch OLED Bundle"), ("Nintendo Switch", "Nintendo Switch 64GB OLED"),
    ("Nintendo Switch", "Nintendo Switch, OLED"), ("Nintendo Switch", "Nintendo Switch OLED Modell"),
    ("Nintendo Switch Konsole", "Nintendo Switch Oled Konsole"), ("Nintendo Switch Lite", "Nintendo Switch OLED"),
    ("Xbox Series X", "Xbox Series X Digital Edition"),
    ("Microsoft Xbox Series X 1TB", "Xbox Series X Digital Edition"),
    ("Sony PS5", "Sony PS5 Digital Gaming Konsole"), ("Sony PS5 Konsole", "Sony PS5 Digital Gaming Konsole"),
    ("Sony PS5", "Sony PS5 Digital CFI-1216B"), ("Sony PS5", "PS5 Digital Bundle"), ("Sony PS5", "PS5 Slim Bundle"),
    ("Sony PS5", "PS5 Digital Edition"), ("Sony PS5 Digital Edition", "Sony PS5 Disc Edition"),
    ("Sony PS5 Slim", "Sony CFI-2016 PS5 Slim Digital"), ("Sony PlayStation 5", "Sony PlayStation 5 Digital Edition"),
    ("Sony PlayStation 4", "Sony PlayStation 4 Slim Bundle"), ("PS4", "PS4 Slim Bundle"),
    ("PS4", "Playstation 4 Slim Konsole"), ("Sony PlayStation 4", "Sony PlayStation 4 Slim Konsole 500GB"),
    ("Dyson V11 Absolute", "Dyson V11 Absolute Extra Akkusauger"), ("AMD RX 6700", "Sapphire Pulse RX 6700 XT"),
    ("Yamaha P-45", "Yamaha P-45 Digital"), ("Canon EOS 2000D", "Canon EOS 2000D Digital"),
    ("Shimano SLX Bremse", "Shimano XT Bremse"), ("Samsung Odyssey G9 Monitor", "Samsung Odyssey G9 OLED Monitor"),
    ("LG 55 Zoll TV", "LG 55 Zoll OLED TV"),
])
def test_variant_words_differ_either_way(a, b):
    """A variant word one title has and the other lacks makes another product, whichever is the ad: also after a
    capacity, a comma or in brackets, before "Bundle", a colour or an edition's name."""
    assert deals.similarity(a, b) < deals.MATCH_MIN and deals.similarity(b, a) < deals.MATCH_MIN


@pytest.mark.parametrize("other, ad, both", [
    ("Canon EOS 2000D Digital Spiegelreflexkamera", "Canon EOS 2000D", True),
    ("Casio CT-S1 Digital Keyboard", "Casio CT-S1 Keyboard", True),
    ("Yamaha P-45", "Yamaha P-45 Digital Piano", True),
    ("Cube Attention 29 Zoll Disc", "Cube Attention 29 Zoll", True),
    ("Cube Attention 29\" Disc", "Cube Attention 29 Zoll", False),          # as the ad: "29" (no unit) its model
    ("Technisat Sat-Receiver HD", "Technisat Digital Sat-Receiver HD", True),
    ("Shimano Deore XT M8100 Bremse", "Shimano XT Bremse", False),          # as the ad: M8100 its model
])
def test_a_plain_variant_word_that_describes_a_noun_or_a_size_is_none(other, ad, both):
    """"Digital Piano", "Digital Spiegelreflexkamera", "Digital Sat-Receiver": the kind of thing; "29 Zoll Disc": what
    the wheel size has. No variant, whichever title is the ad."""
    assert deals.similarity(other, ad) >= deals.MATCH_MIN
    assert deals.TitleMatch(other).variant_set == deals.TitleMatch(ad).variant_set
    if both:
        assert deals.similarity(ad, other) >= deals.MATCH_MIN


def test_variant_words_of_a_title():
    assert deals._Title("LG OLED 55 C1").variants() == {"oled"}
    assert deals._Title("PS5 Slim Digital Edition").variants() == {"slim", "digital"}
    assert deals._Title("Nintendo Switch (OLED-Modell) weiß").variants() == {"oled"}
    assert deals._Title("Yamaha P-45 Digital Piano").variants() == set()
    assert deals._Title("Sony PS5 Disc-Laufwerk").variants() == set()          # a part: the drive for discs
    assert deals.TitleMatch("Sony PS5").check("Sony PS5 Disc-Laufwerk")[1] == "kind"
    assert deals._Title("Samsung Galaxy S23+ 256GB").variants() == {"plus"}
    assert deals._Title("UniFi Switch USW-24-PoE+").variants() == set()
    assert deals._Title("Sony PS5+ 2 Spiele").variants() == set()


@pytest.mark.parametrize("other, ad, ok", [
    ("Galaxy S23 Plus", "Samsung Galaxy S23+", True), ("Samsung Galaxy S23+ 256GB", "Galaxy S23 Plus 256GB", True),
    ("Samsung Galaxy S23 Plus", "Samsung Galaxy S23+", True), ("Ubiquiti UniFi U6 Plus", "Ubiquiti UniFi U6+", True),
    ("Galaxy S23", "Samsung Galaxy S23+", False), ("Samsung Galaxy S23+", "Samsung Galaxy S23", False),
    ("UniFi U6+", "Ubiquiti UniFi U6", False),
    # no Plus: the "+" of PoE+, inside an order code, or adding extras
    ("Ubiquiti PoE Injektor", "Ubiquiti PoE+ Injektor", True),
    ("UniFi Switch USW-24-PoE+", "UniFi Switch USW-24-POE", True),
    ("Sony PS5+ 2 Spiele", "Sony PS5", True),
])
def test_a_plus_glued_to_the_model_is_the_plus(other, ad, ok):
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok


@pytest.mark.parametrize("other, ok", [
    ("Defektfreies iPhone 13", True), ("iPhone 13 - keine Defekte", True), ("iPhone 13 nicht defekt", True),
    ("iPhone 13 ohne Defekt", True), ("iPhone 13 nicht kaputt", True), ("iPhone 13 128GB, kein Bastler", True),
    ("iPhone 13 defekt", False), ("iPhone 13 Display-defekt", False),
])
def test_no_defect_is_no_defect(other, ok):
    assert (deals.similarity(other, "Apple iPhone 13") >= deals.MATCH_MIN) is ok


@pytest.mark.parametrize("other, ad, ok", [
    ("Makita BL1850B 18V 5,0Ah Akku", "Makita Akku 18V 5Ah", True),     # a battery: "18V 5Ah" is no battery health
    ("Makita 18V Akku 5Ah", "Makita Akku 18V 5Ah", True),
    ("Makita Akku 18V 5Ah", "Makita DHP482 Akku-Schlagbohrschrauber", False),
    # the cordless kind
    ("Bosch Akku 18V Bohrschrauber GSR 18V-55", "Bosch GSR 18V-55 Akkuschrauber", True),
    ("Makita Akku 18V Bohrschrauber DDF485", "Makita Akku Bohrschrauber 18V", True),
    ("Bosch Akku 18V GSR 18V-55", "Bosch GSR 18V-55", True), ("Bosch Akku GSR 18V-55", "Bosch GSR 18V-55", True),
    # the battery's health
    ("iPhone 13 128GB Akku 89%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku: 89%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku 89 %", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku 89 Prozent", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB, Akku: 90 %", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB (Akku: 100%)", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku bei 89%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku 89,5%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128 GB Akkuzustand 89%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku-Kapazität 88%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Batterie 89%", "Apple iPhone 13 128GB", True),
    ("iPhone 13 128GB Akku", "Apple iPhone 13 128GB", False),
])
def test_battery_health_only_as_a_percentage(other, ad, ok):
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok



@pytest.mark.parametrize("other, ad, ok", [
    # "Akku" with a voltage or capacity before the model or the item: a cordless tool, an e-bike, a phone's battery
    ("Makita Akku 18V DHP482 Schlagbohrschrauber", "Makita DHP482", True),
    ("Makita DHP482", "Makita Akku 18V DHP482 Schlagbohrschrauber", True),
    ("DeWalt Akku 18V DCD796", "DeWalt DCD796", True),
    ("KTM Macina Sport 610 Akku 625Wh", "KTM Macina Sport 610", True),
    ("Apple iPhone 12 64GB, Akku ~85%", "Apple iPhone 12 64GB", True),
    ("Apple iPhone 12 64GB Akku 2023 getauscht", "Apple iPhone 12 64GB", True),
    ("Makita Akku 18V 5Ah", "Makita DHP482", False),                             # still a battery
    # a "+" glued to a model is the Plus only for a letter (or "Note") and digits: S23+, U6+, Note10+
    ("Sony PS5+ FIFA 24", "Sony PS5", True),
    ("Thermomix TM6+ Varoma", "Thermomix TM6", True),
    ("Samsung Galaxy S23+", "Samsung Galaxy S23", False),
    ("Samsung Galaxy Note10+", "Samsung Galaxy Note10", False),
    # a general variant word both titles have is the same item, wherever it stands
    ("Levis 511 Slim Fit Jeans", "Levis 511 Slim", True),
    ("Yamaha P-45 Digital Piano", "Yamaha P-45 Digital", True),
    ("Casio Uhr Digital", "Casio Digital Uhr", True),
    ("Yamaha P-45 Digital", "Yamaha P-45", False),
    # only "disc" after a size is no variant (disc brakes); "29 Zoll XT" is one
    ("Cube Attention 29 Zoll Disc", "Cube Attention 29 Zoll", True),
    ("Cube Aim 29 Zoll XT", "Cube Aim 29 Zoll", False),
])
def test_akku_plus_and_general_variant_words(other, ad, ok):
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok

def test_a_long_reference_is_cut_and_its_sizes_read_once(monkeypatch):
    """A typed term can be any length ("?q=<2000 digits>&own=1"): only its start is compared, and its sizes are read
    once, not again for every ad (that took seconds per search)."""
    m = deals.TitleMatch("1" * 2000 + " iPhone 13")
    assert len(m.title) == deals.REFERENCE_MAX
    assert deals.TitleMatch(" " + "1" * 2000).title.strip()                   # never cut down to nothing
    m = deals.TitleMatch("Apple iPhone 13 128GB " + "neu " * 100)
    assert len(m.title) <= deals.REFERENCE_MAX and m.model == "13" and m.sizes == {"128gb"}
    read = []
    real = deals._capacities
    monkeypatch.setattr(deals, "_capacities", lambda t: read.append(t) or real(t))
    assert [m.check("Apple iPhone 13 128GB")[1], m.check("Apple iPhone 13 256GB")[1]] == [None, "model"]
    assert read == ["Apple iPhone 13 128GB", "Apple iPhone 13 256GB"]        # the other ads' titles only


UNIFI = load("deals_unifi_searches.json")


@pytest.fixture
def unifi(monkeypatch):
    """The willhaben searches of three reported Kleinanzeigen UniFi checks (fixture), Geizhals off."""
    async def search_ads(q):
        return copy.deepcopy(UNIFI.get(q) or {"rows_found": 0, "ads": []})
    orig = deals._search

    async def search(site, q):                       # their own site (Kleinanzeigen) finds nothing here
        return await orig(site, q) if site == "willhaben" else {"rows_found": 0, "ads": []}
    monkeypatch.setattr(deals, "search_ads", search_ads)
    monkeypatch.setattr(deals, "_search", search)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)


def _hit(title, price, ad_id="3540356386"):
    """What a Kleinanzeigen hit or list result knows: title and price (AdDetail.from_result)."""
    return AdDetail.from_result({"ad_id": ad_id, "title": title, "price": price, "platform": "kleinanzeigen"})


async def test_unifi_u7_pro_is_compared_with_the_u7_pro(unifi):
    """The reported check: a U7 Pro "mit Halterung" (title only); willhaben has one U7 Pro – too few for a level."""
    res = await deals.analyze(_hit("UniFi U7 Pro WiFi 7 Access Point mit Halterung", 139.0))
    comp = res["comparables"]
    assert res["query"] == "U7 Pro" and [a["title"] for a in comp["closest"]] == ["UniFi U7pro"]   # then "UniFi U7 Pro"
    assert [s["query"] for s in comp["searches"]] == ["U7 Pro", "U7 Pro", "UniFi U7 Pro", "UniFi U7 Pro"]
    assert comp["count"] == 1 and res["position"] == {"pos": None, "reason": "few_data"}


async def test_unifi_u6_mesh_gets_none_of_the_reported_list(unifi):
    """The list the user saw (Cloud Key 30, PoE injector 30, U6 Lite cover 35, nanoHD mount 35) came from the U6 Mesh
    check's search "Ubiquiti UniFi U6" (today's query adds "Mesh"; the search is given here as a list's link would):
    none of it is a U6 Mesh, nor are the U6 Pros; too few is too few, the list is not topped up."""
    hit = _hit("Ubiquiti UniFi U6 Mesh WiFi 6 Access Point neuwertig", 139.0, "3540356279")
    assert deals.query_from_title(hit.heading) == "Ubiquiti UniFi U6 Mesh"
    res = await deals.analyze(hit, query="Ubiquiti UniFi U6")
    comp = res["comparables"]
    assert comp["count"] == 0 and comp["closest"] == []
    # model: the Cloud Key and the two U6 Pros; kind: the PoE injector, the U6 Lite cover and the nanoHD mount
    assert comp["filtered"] == {"model": 3, "kind": 3, "condition": 0, "outliers": 0}
    assert res["position"]["reason"] == "few_data"


async def test_unifi_u6_lite_out_of_a_broad_search(unifi):
    """ "UniFi Access Point" finds 41 UniFi devices and others; only the two U6 Lites ("UniFi 6 Lite") compare."""
    res = await deals.analyze(_hit("UniFi Access Point U6 Lite", 70.0, "3540000001"))
    comp = res["comparables"]
    assert sorted(a["title"] for a in comp["closest"]) == [
        "Ubiquiti UniFi 6 Lite Access Point + Trench Networks WLAN-System", "UniFi 6 Lite Access Point"]
    assert comp["count"] == 2 and res["position"]["reason"] == "few_data"


async def test_typed_search_term_is_what_comparables_must_be(monkeypatch):
    """"Compare again" with a term of the user's own: that term, not the title, says what counts. A term that came
    along from a list ("Check" on the deals page, a product search's term) or the form's own unchanged default only
    changes the search: an iPhone 13 Pro Max checked from a search for "iphone 13" is no base iPhone 13."""
    ads = [_summ(0, "Bosch GSR 18V-55 Akkuschrauber", 100), _summ(1, "Makita DDF484 Akkuschrauber", 120),
           _summ(2, "Einhell Akkuschrauber", 60), _summ(3, "Bohrer Set", 15)]
    _fake_search(monkeypatch, ads)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    by_title = await deals.analyze(_ad(100))
    assert [a["title"] for a in by_title["comparables"]["closest"]] == ["Bosch GSR 18V-55 Akkuschrauber"]
    typed = await deals.analyze(_ad(100), query="Akkuschrauber", own_query=True)
    assert typed["comparables"]["count"] == 3 and typed["own_query"] is True
    linked = await deals.analyze(_ad(100), query="Akkuschrauber")          # the same term from a list's link
    assert linked["comparables"]["count"] == 1 and linked["own_query"] is False
    assert deals.cached(AD_ID, "Akkuschrauber", None, True) == typed        # each kept apart (re-rated copies)
    assert deals.cached(AD_ID, "Akkuschrauber") == linked

    phones = [_summ(n, "Apple iPhone 13 128GB", 300 + n, "2691") for n in range(6)]
    phones += [_summ(10 + n, "Apple iPhone 13 Pro Max 256GB", 600 + n, "2691") for n in range(2)]
    _fake_search(monkeypatch, phones)
    pro_max = _ad(620, heading="Apple iPhone 13 Pro Max 256GB", ad_id="1000000009")
    for q, own in [("iphone 13", False), ("Apple iPhone 13 Pro", True)]:        # a list's term; the unchanged default
        deals.clear_cache()
        res = await deals.analyze(pro_max, query=q, own_query=own)
        assert {a["title"] for a in res["comparables"]["closest"]} == {"Apple iPhone 13 Pro Max 256GB"}
    deals.clear_cache()
    res = await deals.analyze(pro_max, query="iphone 13", own_query=True)       # typed on purpose: what the user says
    assert res["comparables"]["count"] == 6


async def test_a_typed_term_of_filler_words_leaves_the_title(monkeypatch):
    """"neu" or "OVP" typed to compare with names no item: the title still says what counts, not everything goes."""
    assert deals._reference("Bosch GSR 18V-55 Akkuschrauber", "neu OVP", True, None) == "Bosch GSR 18V-55 Akkuschrauber"
    ads = [_summ(0, "Bosch GSR 18V-55 Akkuschrauber", 100), _summ(1, "Makita DDF484 Akkuschrauber", 120),
           _summ(2, "Einhell Akkuschrauber", 60), _summ(3, "Bohrer Set", 15)]
    _fake_search(monkeypatch, ads)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    res = await deals.analyze(_ad(100), query="neu", own_query=True)
    assert [a["title"] for a in res["comparables"]["closest"]] == ["Bosch GSR 18V-55 Akkuschrauber"]


def test_compare_again_marks_a_changed_term_as_the_users_own(app_client):
    """The "Compare again" form sends the term it showed as `was`: a changed term is the user's own (own=1 goes on to
    the result), an unchanged one is not; a list's "Check" link sends none."""
    base = f"/deals/ad/{AD_ID}"
    assert "own=1" not in app_client.get(base, params={"q": "Bosch GSR 18V-55"}).text
    assert "own=1" not in app_client.get(base, params={"q": "Bosch GSR 18V-55", "was": "Bosch GSR 18V-55"}).text
    assert f'hx-get="{base}/result?q=Akkuschrauber&amp;cat=&amp;own=1"' in \
        app_client.get(base, params={"q": "Akkuschrauber", "was": "Bosch GSR 18V-55"}).text
    assert "own=1" in app_client.get(base, params={"q": "Akkuschrauber", "own": "1"}).text   # the category form
    result = app_client.get(f"{base}/result", params={"q": "Akkuschrauber", "own": "1"}).text
    assert 'name="was" value=""' in result and 'name="own" value="1"' in result and '"own": "1"' in result


def _fake_search(monkeypatch, ads):
    async def search_ads(q):
        return {"rows_found": len(ads), "ads": ads}
    monkeypatch.setattr(deals, "search_ads", search_ads)


async def test_comparables_leave_out_other_sizes(monkeypatch):
    pi = "2691;5013000"
    ads = [_summ(n, "Raspberry Pi 4 4 GB", p, pi) for n, p in enumerate([45, 50, 55, 60])]
    ads += [_summ(10, "Raspberry Pi 4 Model B", 52, pi), _summ(11, "Raspberry Pi 4 4 GB + 32GB SD-Karte", 58, pi)]
    ads += [_summ(20 + n, "Raspberry Pi 4 8GB", p, pi) for n, p in enumerate([80, 85, 90])]
    ads += [_summ(30, "Raspberry Pi 4 Gehäuse", 10, pi)]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("Raspberry Pi 4", AD_ID, 50, title="Raspberry Pi 4 4GB")
    assert out["count"] == 6 and out["stats"]["max"] == 60
    assert out["filtered"] == {"model": 3, "kind": 1, "condition": 0, "outliers": 0}   # 3 x 8GB, the case

    mac = [_summ(n, "MacBook Air M1 8GB 256GB", p, pi) for n, p in enumerate([600, 650, 700, 720])]
    mac += [_summ(10, "MacBook Air M1 8 GB / 256 GB", 680, pi), _summ(11, "MacBook Air M1 256GB", 640, pi)]
    mac += [_summ(20, "MacBook Air M1 16GB 256GB", 900, pi), _summ(21, "MacBook Air M1 8GB 512GB", 820, pi)]
    _fake_search(monkeypatch, mac)
    out = await deals.comparables("MacBook Air M1", AD_ID, 650, title="MacBook Air M1 8GB 256GB")
    assert out["count"] == 6 and out["stats"]["max"] == 720 and out["filtered"]["model"] == 2


async def test_comparables_too_few_of_the_size_stay_too_few(monkeypatch):
    """Three Pi 4 4GB among other sizes: three comparables (no level from them), not all sizes as before."""
    pi = "2691;5013000"
    ads = [_summ(n, "Raspberry Pi 4 4GB", p, pi) for n, p in enumerate([50, 55])] + [_summ(5, "Raspberry Pi 4", 52, pi)]
    ads += [_summ(20 + n, "Raspberry Pi 4 8GB", p, pi) for n, p in enumerate([80, 85, 90, 95, 100, 70])]
    ads += [_summ(30, "Raspberry Pi 4 Gehäuse", 10, pi)]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("Raspberry Pi 4", AD_ID, 50, title="Raspberry Pi 4 4GB")
    assert out["count"] == 3 and out["filtered"] == {"model": 6, "kind": 1, "condition": 0, "outliers": 0}
    assert out["stats"]["min"] == 50 and deals.position(50, out["stats"], None)["reason"] == "few_data"


async def test_category_choices_and_chosen_category(monkeypatch):
    from app.willhaben.parse import FilterValue, SearchFilter, SearchFilters

    seen = []

    async def search_filters(url, rows=6):
        seen.append(url)
        nav = SearchFilter(id="category", label="Kategorie", kind="HIERARCHICAL", multi=False,
                           values=[FilterValue("Smartphones / Telefonie", "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/smartphones-telefonie-2691?keyword=iphone", 36)],
                           selected=[])
        return SearchFilters(url=url, rows_found=50, groups=[("Kategorie", [nav])], ads=[])

    monkeypatch.setattr(deals.get_public(), "search_filters", search_filters)
    ch = await deals.category_choices("iphone 15 pro")
    assert seen[0].endswith("keyword=iphone+15+pro") and ch["values"][0]["hits"] == 36

    cat = ch["values"][0]["url"]
    got = []

    async def search_category(url):
        got.append(url)
        return {"rows_found": 2, "ads": [_summ(n, "Handy", p, "9") for n, p in enumerate([100, 120, 140, 160, 180])]}

    monkeypatch.setattr(deals, "search_category", search_category)
    out = await deals.comparables("iphone", AD_ID, 150, title="Handy", category_url=cat)
    assert got == [cat] and out["scope"] == "chosen" and out["count"] == 5


# ------------------------------------------------------------------ Claude looks for the product on Geizhals
class ScriptedClaude:
    """Answers the Geizhals search: first the queries, then the pick (or an error)."""
    model = "fake-haiku"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return LLMResult(data=a, model=self.model)


@pytest.fixture
def no_auto_gh(monkeypatch):
    """The automatic lookup finds nothing; Geizhals' search pages (gh fixture) still answer Claude's searches."""
    async def nothing(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", nothing)


QUERIES = {"product": "Bosch GSR 18V-55 solo", "queries": ["Bosch GSR 18V-55 solo", "Bosch GSR 18V-55"]}


async def test_claude_finds_the_geizhals_product(wh, gh, no_auto_gh, monkeypatch):
    _, state = wh
    state["search"] = _search_json([100, 120])                  # few comparables: the new price places the ad
    fake = ScriptedClaude(QUERIES, {"pick": 1, "reason": "Genau dieses Modell, ohne Akku."})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(70))
    assert res["geizhals"] is None and res["position"]["pos"] is None and fake.calls == []   # never automatic

    res = await deals.analyze(_ad(70), find_geizhals=True)
    assert len(fake.calls) == 2 and "Bosch GSR 18V-55 Akkuschrauber" in fake.calls[0]["prompt"]
    assert "1. Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo · ab 139 €" in fake.calls[1]["prompt"]
    assert "2. Bosch GSR 18V-55 Set · ab 199.9 €" in fake.calls[1]["prompt"]
    assert gh == ["https://geizhals.at/?fs=Bosch+GSR+18V-55+solo&hloc=at",      # one request per search, no page
                  "https://geizhals.at/?fs=Bosch+GSR+18V-55&hloc=at"]
    assert res["geizhals"] == {"name": "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo",
                               "url": "https://geizhals.at/bosch-professional-gsr-18v-55-solo-06019h5202-a2221732.html",
                               "price_from": 139.0, "by": "claude", "reason": "Genau dieses Modell, ohne Akku."}
    assert res["position"]["basis"] == "geizhals"

    again = await deals.analyze(_ad(70), find_geizhals=True)     # found already: no further Claude call
    assert len(fake.calls) == 2 and again["geizhals"]["by"] == "claude"
    deals.remember(again)
    deals.clear_cache()
    with dbmod.session() as db:                                 # the analysis gets old and is recomputed …
        row = db.get(dbmod.DealCheck, AD_ID)
        row.result = {**row.result, "checked_at": (dbmod.utcnow() - timedelta(hours=2)).isoformat()}
        db.add(row)
        db.commit()
    later = await deals.analyze(_ad(70))
    assert later["geizhals"]["by"] == "claude" and len(fake.calls) == 2         # … and keeps Claude's product


async def test_claude_finds_nothing_or_fails(wh, gh, no_auto_gh, monkeypatch):
    fake = ScriptedClaude(LLMError("rate limited"))
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert res["geizhals_error"] == "rate limited" and res["geizhals"] is None
    assert "geizhals_error" not in deals.cached(AD_ID)          # a failure is not kept: the next click tries again

    fake.answers = [QUERIES, {"pick": 0, "reason": "Nur Sets mit Akku gefunden."}]
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert res["geizhals"] is None and res["geizhals_none"] == "Nur Sets mit Akku gefunden."
    await deals.analyze(_ad(100), find_geizhals=True)          # Claude's "none" is kept with the analysis
    assert len(fake.calls) == 3
    deals._results.clear()                                       # the cache gone (a restart, its TTL): still known
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert res["geizhals_none"] == "Nur Sets mit Akku gefunden." and res["geizhals_none_at"] and len(fake.calls) == 3

    fake.answers = [{"product": "", "queries": []}]
    res = await deals.analyze(_ad(100, ad_id="1000000005", heading="Alter Holzschrank Eiche"), find_geizhals=True)
    assert "no specific product" in res["geizhals_none"] and len(fake.calls) == 4 and len(gh) == 2


async def test_claude_geizhals_search_blocked(wh, no_auto_gh, monkeypatch):
    monkeypatch.setattr(geizhals, "_transport", httpx.MockTransport(lambda r: httpx.Response(429)))
    monkeypatch.setattr(geizhals, "_limiter", geizhals.RateLimiter(0))
    fake = ScriptedClaude(QUERIES)
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100), find_geizhals=True)
    assert "429" in res["geizhals_error"] and len(fake.calls) == 1          # no pick without results


async def test_the_ad_photo_counts_for_the_ads_own_site(monkeypatch):
    """Kleinanzeigen and eBay share i.ebayimg.com: an eBay ad's photo for Claude is an eBay download (statistics,
    eBay's VPN), not Kleinanzeigen's, both in the price check and in the Geizhals search."""
    from app.services import screening
    from app.willhaben import access_log

    sites = []
    monkeypatch.setattr(access_log, "record", lambda *a, platform="willhaben", **k: sites.append(platform))
    monkeypatch.setattr(screening, "_transport", httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})))
    fake = ScriptedClaude({"deal": "good", "reason": "Günstig."}, {"product": "", "queries": []})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    ad = _ad()
    ad.platform, ad.images = "ebay", ["https://i.ebayimg.com/images/g/qvYAAeSwmhhqy9Wh/s-l1600.jpg"]
    assert (await deals.ask_claude(ad, {}))["deal"] == "good"
    assert "none" in await deals.claude_geizhals(ad)
    assert all("Foto: ./foto1.jpg" in c["prompt"] for c in fake.calls) and sites == ["ebay", "ebay"]


def test_viewer_offers_claude_and_updates_the_hit(app_client, no_auto_gh, monkeypatch):
    from app.services import screening

    async def no_photo(*a, **k):
        return None
    monkeypatch.setattr(screening, "_download", no_photo)
    fake = ScriptedClaude(QUERIES, {"pick": 1, "reason": "Passt."})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    with dbmod.session() as db:
        a = dbmod.SearchAgent(name="Bohrer", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=bosch",
                              webapi_url="x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = dbmod.Hit(agent_id=a.id, ad_id=AD_ID, heading="Bosch GSR 18V-55", url="u", geizhals={})
        db.add(h)
        db.commit()
        db.refresh(h)
    frag = app_client.get(f"/hits/ad/{AD_ID}/deal?hit={h.id}").text
    assert "No matching product found on Geizhals." in frag and "Let Claude search Geizhals" in frag
    assert f'hx-post="/deals/ad/willhaben/{AD_ID}/geizhals"' in frag and 'hx-target="closest section"' in frag
    assert fake.calls == []

    section = app_client.post(f"/deals/ad/willhaben/{AD_ID}/geizhals", data={"hit": str(h.id), "compact": "1"}).text
    assert section.lstrip().startswith("<section") and "new from € 139 on Geizhals" in _plain(section)
    assert "found by Claude" in section and "Let Claude search Geizhals" not in section
    from app.services import jobs
    shown = jobs.snapshot()["recent"][0]                         # shown in the jobs indicator while Claude searched
    assert shown["label"] == "Claude searches Geizhals: „Bosch GSR 18V-55“" and shown["quiet"] is True
    assert shown["result"] == "Bosch Professional GSR 18V-55 Akku-Bohrschrauber solo"
    with dbmod.session() as db:
        assert db.get(dbmod.Hit, h.id).geizhals["price_from"] == 139.0
        assert db.get(dbmod.DealCheck, AD_ID).result["geizhals"]["by"] == "claude"


# ------------------------------------------------------------------ zones on read, condition filter, texts
async def test_zone_change_applies_to_cached_results_without_a_request(wh, gh):
    from app.services import price_zones
    calls, _ = wh
    new = _ad(100)
    new.item_condition = "new"                               # new items: the zones decide, comparables or not
    res = await deals.analyze(new)                           # € 100 of € 139 = 71 %: a bargain (below 75 %)
    assert res["position"]["level"] == "bargain" and calls["search"] == 1
    deals.remember(res)
    price_zones.save({"used": [30, 50, 80, 90], "new": [70, 100, 110, 125]})
    assert deals.cached(AD_ID)["position"]["level"] == "cheap"                # from memory
    deals.clear_cache()
    assert deals.cached(AD_ID)["position"]["level"] == "cheap"                # revived from the DealCheck row
    again = await deals.analyze(new)
    assert again["position"]["level"] == "cheap" and calls["search"] == 1 and len(gh) == 1   # one card search


async def test_a_stored_label_is_never_read(wh, monkeypatch):
    calls, _ = wh

    async def no_gh(q, **kw):
        return None

    async def fails(ad):
        return {"error": "rate limited"}
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    monkeypatch.setattr(deals, "claude_geizhals", fails)
    await deals.analyze(_ad(100))
    at, stored = deals._results[(AD_ID, "")]
    deals._results[(AD_ID, "")] = (at, {**stored, "position": {"pos": 0.99, "level": "very_expensive"}})
    assert deals.cached(AD_ID)["position"]["level"] == "fair"
    out = await deals.analyze(_ad(100), find_geizhals=True)                     # the geizhals_error answer too
    assert out["geizhals_error"] == "rate limited" and out["position"]["level"] == "fair" and calls["search"] == 1
    assert deals.rerate(None) is None and deals.rerate({"query": "x"}) == {"query": "x"}


def _cond(n, price, condition):
    return {**_summ(n, "iPhone 15 Pro 128GB", price, "2691;5013241"), "item_condition": condition}


async def test_comparables_in_another_condition_are_left_out(monkeypatch):
    ads = [_cond(n, p, "used") for n, p in enumerate([500, 520, 540, 560, 580])] + [_cond(5, 600, None)]
    ads += [_cond(10 + n, p, "new") for n, p in enumerate([900, 950])] + [_cond(20, 200, "defect")]
    _fake_search(monkeypatch, ads)
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, title="iPhone 15 Pro")
    assert out["count"] == 6 and out["filtered"]["condition"] == 3 and out["stats"]["max"] == 600   # used ad
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, title="iPhone 15 Pro", condition="defect")
    assert out["count"] == 9 and out["filtered"]["condition"] == 0          # a defective ad: nothing to pick
    _fake_search(monkeypatch, ads[2:])               # 3 used + 1 unknown left: too few, not topped up with new ones
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 500, title="iPhone 15 Pro")
    assert out["count"] == 4 and out["filtered"]["condition"] == 3
    new = [_cond(30 + n, p, "new") for n, p in enumerate([900, 920, 940, 960])] + [_cond(39, 950, None)]
    _fake_search(monkeypatch, new + ads[:5])
    out = await deals.comparables("iPhone 15 Pro", AD_ID, 950, title="iPhone 15 Pro", condition="new")
    assert out["count"] == 5 and out["filtered"]["condition"] == 5 and out["stats"]["min"] == 900


async def test_results_of_filter_version_2_are_recomputed(wh, monkeypatch):
    async def no_gh(q, **kw):
        return None
    monkeypatch.setattr(deals.geizhals, "lookup", no_gh)
    assert deals.FILTERS == 6
    res = await deals.analyze(_ad(100))
    deals.remember({**res, "comparables": {**res["comparables"], "filters": 2}})
    deals.clear_cache()
    assert deals.cached(AD_ID) is None


def test_deal_prompt_names_the_zones_and_the_condition():
    res = {"comparables": {"count": 4, "stats": deals.stats(USER_CASE), "query": "Bosch"},
           "geizhals": {"name": "Bosch GSR", "url": "u", "price_from": 202.30},
           "position": deals.position(139, deals.stats(USER_CASE), 202.30)}
    prompt = deals.deal_prompt(_ad(139), res, "Deutsch", None)
    assert "Zustand: unbekannt" in prompt
    assert ("Einordnung der App: fair – 68 % vom günstigsten Geizhals-Neupreis (Zonen des Nutzers für Gebrauchtes: "
            "Schnäppchen unter 30 %, günstig ab 30 %, fair ab 50 %, teuer ab 80 %, sehr teuer ab 90 %)") in prompt
    ad = _ad(139)
    ad.item_condition, ad.item_condition_label = "like_new", "Neuwertig"
    assert "Zustand laut Inserat: Neuwertig" in deals.deal_prompt(ad, res, "Deutsch", None)
    ad.item_condition, ad.item_condition_label = "new", None
    assert "Zustand angenommen: neu" in deals.deal_prompt(ad, res, "Deutsch", None)
    assert "Versand" not in deals.deal_prompt(ad, res, "Deutsch", None)          # the site states none
    ad.shipping_price = 5.5
    assert "Versand: 5.5 €" in deals.deal_prompt(ad, res, "Deutsch", None)
    st = deals.stats([380, 390, 400, 410, 420, 430])
    res = {**res, "comparables": {"count": 6, "stats": st}, "position": deals.position(400, st, 100.0)}
    prompt = deals.deal_prompt(_ad(400), res, "Deutsch", None)
    assert "Einordnung der App: fair unter den Vergleichsanzeigen (die mittlere Hälfte gilt als fair)" in prompt
    assert "die Vergleichsanzeigen kosten im Mittel 405 % davon" in prompt
    res = {**res, "position": deals.position(400, st, 100.0, "defect")}
    assert "Einordnung der App: keine (laut Inserat defekt)" in deals.deal_prompt(_ad(400), res, "Deutsch", None)


def _plain(html):
    """The text without the no-break spaces and word joiners that keep amounts on one line (main._nb)."""
    return html.replace("\xa0", " ").replace("\u2060", "")


def _render_deal(res, lang="de"):
    from app import i18n
    from app.main import templates
    i18n.set_request_lang(lang)
    try:
        return templates.env.get_template("_deal.html").render(res=res, error=None, compact=True, h=None)
    finally:
        i18n.set_request_lang("en")


def _res(price, prices, new_price, condition=None, label=None):
    st = deals.stats(prices) if prices else None
    return deals.rerate({"ad": {"ad_id": AD_ID, "platform": "willhaben", "title": "Bosch GSR 18V-55", "price": price,
                                "item_condition": condition, "item_condition_label": label},
                         "comparables": {"count": len(prices or []), "stats": st, "closest": [], "filtered": {},
                                         "scope": None, "query": "Bosch"},
                         "geizhals": {"name": "Bosch GSR", "url": "https://geizhals.at/x-a1.html",
                                      "price_from": new_price} if new_price else None,
                         "category_path": [], "claude": None})


def test_price_section_by_zones_in_german():
    html = _render_deal(_res(139, USER_CASE, 202.30))
    small = re.search(r'<small class="text-muted">([^<]*)</small>', html).group(1)
    assert small == "68 % vom Neupreis · 4 vergleichbare Anzeigen"              # never a leading " · "
    assert "neu ab €\xa0202,30 auf Geizhals" in html and "data-gh-name" in html
    assert 'üblich bei Vergleichsanzeigen: <span class="whitespace-nowrap">€ 131–€ 155</span>' in html
    # amounts, shares and ranges never break across lines
    assert "Bei Gebrauchtem gilt als „fair“: 50–\u206079\xa0% vom Neupreis (€\xa0101,15 bis unter €\xa0161,84)." in html
    assert "Zustand unbekannt – es gelten die Zonen für Gebrauchtes." in html and "Zonen ändern" in html
    assert 'href="/settings/prices"' in html and "deal-zones" in html and "diese Anzeige kostet" not in html
    assert 'aria-label="€ 139: fair, 68 % vom Neupreis"' in html
    html = _plain(_render_deal(_res(195.02, None, 199.0, "new", "Neu")))
    assert "Bei Neuware gilt als „günstig“: 75–99 % vom Neupreis (€ 149,25 bis unter € 199)." in html
    assert "Zustand laut Inserat: Neu." in html
    # the open ends read as German too: the sentence has its subject in every form
    html = _plain(_render_deal(_res(400, None, 202.30)))
    assert "Bei Gebrauchtem gilt als „sehr teuer“: ab 90 % vom Neupreis (ab € 182,07)." in html
    html = _plain(_render_deal(_res(20, None, 202.30, "new", None)))              # inferred: "angenommen"
    assert "Bei Neuware gilt als „Schnäppchen“: unter 75 % vom Neupreis (unter € 151,73)." in html
    assert "Zustand angenommen: Neu." in html
    html = _plain(_render_deal(_res(400, None, 202.30), lang="en"))
    assert "For used items „very expensive“ means: from 90 % of the new price (from € 182,07)." in html


def test_price_header_names_the_condition_and_tiny_shares():
    """The site's words next to "Price" never read as a verdict ("Price · In Ordnung"): the pill says "Condition: …"
    and the group wraps on narrow columns; a price under 1 % of new does not claim 0 %."""
    res = _res(80, [70, 90], None, "used", "In Ordnung")
    res["ad"]["platform"] = "kleinanzeigen"
    html = _render_deal(res)
    head = html[html.index("<strong>Preis</strong>"):html.index("</span>\n", html.index("<strong>Preis</strong>"))]
    assert ">Zustand: In Ordnung</span>" in head and "bg-lvl-" not in head          # few data: no level badge
    assert '<span class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1"><strong>Preis</strong>' in html
    html = _render_deal(_res(139, None, 202.30, "new", "Neu – Sonstige (siehe Artikelbeschreibung)"))
    assert ">Zustand: Neu – Sonstige (siehe A…</span>" in html
    small = re.search(r'<small class="text-muted">([^<]*)</small>', _render_deal(_res(1, None, 202.30))).group(1)
    assert small == "unter 1 % vom Neupreis"


def test_price_section_warnings_and_defect():
    html = _render_deal(_res(140, [130, 140, 150, 160, 170], 100.0), lang="en")     # used: its comparables decide
    assert "For used items enough comparable ads decide" in html and "this ad is 140 % of new" in html
    assert "By the new price it is" not in html and "Comparable ads cost 150" not in html
    html = _render_deal(_res(120, [130, 140, 150, 160, 170], 100.0, "new", "New"), lang="en")
    assert "Comparable ads cost 150\xa0% of the new price on average" in html
    assert "usual among comparable ads:" in html                                  # their range under the zones
    html = _render_deal(_res(400, [380, 390, 400, 410, 420, 430], 100.0), lang="en")
    assert "Geizhals product probably wrong – not used." in html and "data-gh-doubt" in html
    assert "The comparable ads cost 405\xa0% of its new price on average." in html
    assert "The similar ads rate this ad instead." in html and "probably another product, not used" in html
    assert "this ad is 400 % of new" not in html and "Rated by 6 similar ads" in html
    html = _render_deal(_res(50, PRICES, 100.0, "defect", "Defekt"), lang="en")
    assert "Defective according to the ad" in html and "deal-zones" not in html and "this ad is 50 % of new" in html
    assert "below the median" not in html and "10 comparable ads" in html          # not measured against working items
    assert "bg-bad-soft text-error" in html                                        # the red condition pill


def test_price_section_by_comparables_reads_the_stats_median():
    html = _render_deal(_res(100, PRICES, None))
    small = re.search(r'<small class="text-muted">([^<]*)</small>', html).group(1)
    assert small == "5 % unter dem Median · 10 vergleichbare Anzeigen"
    assert "Eingestuft nach 10 ähnlichen Anzeigen – nicht nach einem Neupreis" in html and "data-similar-stripes" in html
    assert "„fair“ heißt 90–114 % davon (€ 95 bis unter € 121)" in _plain(html)
    assert 'href="/settings/prices#s-prices-similar"' in html
    assert ">€ 95<" in html and ">€ 121<" in html and ">Ø<" in html                # the limits below the bar, the median


# ------------------------------------------------------------------ the model query, the ad's own site
@pytest.mark.parametrize("title, queries", [
    # brands sellers leave out: an AND search loses their ads (willhaben: "Ubiquiti UniFi U6 Mesh" 0, "U6 Mesh" 13)
    ("Ubiquiti UniFi USW Pro Max 24 PoE Switch mit Rechnung", ["USW Pro Max 24", "Ubiquiti UniFi USW"]),
    ("Ubiquiti UniFi U6 Mesh WiFi 6 Access Point neuwertig", ["U6 Mesh", "Ubiquiti UniFi U6 Mesh"]),
    ("UniFi Access Point U6 Lite", ["U6 Lite", "UniFi Access Point"]),
    ("Amazon Echo Show 8 (3. Generation)", ["Echo Show 8", "Amazon Echo Show"]),
    ("Apple Watch Series 6 44mm", ["Watch Series 6", "Apple Watch Series"]),
    ("Apple iPhone 13 128GB Mitternacht", ["iPhone 13 128GB", "Apple iPhone 13"]),
    ("Ubiquiti UniFi USW Pro PoE 48 Port Switch", ["USW Pro PoE 48", "Ubiquiti UniFi USW"]),
    # an order code alone; the extras stay out; never wider than the title's own words
    ("Ubiquiti UniFi AC Pro UAP-AC-PRO WLAN Access Point", ["UAP-AC-PRO", "Ubiquiti UniFi AC"]),
    ("Raspberry Pi 5 8GB, 64GB SD-XC MicroSD Karte, Netzteil, Gehäuse", ["Raspberry Pi 5 8GB", "Raspberry Pi 5"]),
    ("Ubiquiti UniFi Cloud Gateway Max + Samsung 980 SSD 500 GB", ["Cloud Gateway Max", "Ubiquiti UniFi Cloud"]),
    ("Dyson V11 Absolute Akkusauger", ["Dyson V11 Absolute"]),
    ("Bosch GSR 18V-55 Akkuschrauber", ["GSR 18V-55", "Bosch GSR 18V-55"]),
    ("IKEA Kallax Regal 4x4 weiss", ["IKEA Kallax Regal"]),            # no model, no brand: as before
    ("Bürostuhl ergonomisch schwarz", ["Bürostuhl ergonomisch schwarz"]),
])
def test_search_queries_the_model_first(title, queries):
    assert deals.search_queries(title) == queries


def _ads(n0, title, prices, platform="willhaben"):
    return [{**_summ(n0 + n, title, p), "platform": platform} for n, p in enumerate(prices)]


async def test_comparables_broaden_only_while_too_few_and_pool_the_ads(monkeypatch):
    found = {"iPhone 13 128GB": _ads(0, "iPhone 13 128GB", [250, 260, 270]),
             "Apple iPhone 13": _ads(1, "Apple iPhone 13 128GB", [255, 280, 300])}   # 01, 02 are found twice
    asked = []

    async def search(site, q):
        asked.append((site, q))
        return {"rows_found": len(found.get(q, [])), "ads": copy.deepcopy(found.get(q, []))}
    monkeypatch.setattr(deals, "_search", search)
    out = await deals.comparables("iPhone 13 128GB", None, 260, title="Apple iPhone 13 128GB", more=["Apple iPhone 13"])
    assert asked == [("willhaben", "iPhone 13 128GB"), ("willhaben", "Apple iPhone 13")]       # 3 < 5: the wide one
    assert out["count"] == 4 and out["query"] == "iPhone 13 128GB"                      # 4 different ads
    assert [s["query"] for s in out["searches"]] == ["iPhone 13 128GB", "Apple iPhone 13"]
    found["iPhone 13 128GB"] = _ads(10, "iPhone 13 128GB", [250, 260, 270, 280, 290])
    asked.clear()
    deals.clear_cache()
    out = await deals.comparables("iPhone 13 128GB", None, 260, title="Apple iPhone 13 128GB", more=["Apple iPhone 13"],
                                  condition="new")                 # the plan follows the title, not the condition
    assert asked == [("willhaben", "iPhone 13 128GB")] and out["count"] == 5


async def test_an_ad_of_another_site_compares_on_its_site_too(monkeypatch):
    """A Kleinanzeigen ad: Kleinanzeigen first, then willhaben, pooled; the ad itself is left out by its site."""
    found = {("kleinanzeigen", "Raspberry Pi 5 8GB"): _ads(0, "Raspberry Pi 5 8GB", [149, 170, 185, 190], "kleinanzeigen"),
             ("willhaben", "Raspberry Pi 5 8GB"): _ads(0, "Raspberry pi 5 8GB", [175, 159])}   # same ids, other site
    asked = []

    async def search(site, q):
        asked.append((site, q))
        return {"rows_found": 9, "ads": copy.deepcopy(found.get((site, q), []))}
    monkeypatch.setattr(deals, "_search", search)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    ad = AdDetail.from_result({"ad_id": "2000000000", "title": "Raspberry Pi 5 8GB, 64GB SD-Karte, Netzteil, Gehäuse",
                               "price": 149.0, "platform": "kleinanzeigen"})
    res = await deals.analyze(ad)
    comp = res["comparables"]
    assert asked == [("kleinanzeigen", "Raspberry Pi 5 8GB"), ("willhaben", "Raspberry Pi 5 8GB")]
    assert comp["count"] == 5 and sorted(a["price"] for a in comp["closest"]) == [159, 170, 175, 185, 190]   # not 149
    assert deals.comparable_sites("willhaben") == ["willhaben"] == deals.comparable_sites("preisjaeger")


def test_comparables_come_from_every_site_switched_on():
    from app.db import set_setting

    for pid in ("vinted", "shpock", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    sites = deals.comparable_sites("kleinanzeigen")
    assert sites[:2] == ["kleinanzeigen", "willhaben"] and set(sites) == {"kleinanzeigen", "willhaben", "vinted", "shpock"}
    assert deals.comparable_sites("preisjaeger")[0] == "willhaben" and "preisjaeger" not in deals.comparable_sites("preisjaeger")


async def test_a_vpn_wait_is_named_as_such(monkeypatch):
    from app.platforms import vpn

    async def waiting(site, q):
        raise vpn.VpnWait("willhaben hat den VPN-Server abgewiesen; die Anfragen pausieren")
    monkeypatch.setattr(deals, "_search", waiting)
    out = await deals.comparables("Raspberry Pi 5", None, 100, title="Raspberry Pi 5", more=["Raspberry Pi"])
    assert out["error"].startswith("willhaben hat") and out["error_kind"] == "vpn" and out["error_site"] == "willhaben"

    async def broken(site, q):
        raise deals.WillhabenError("HTTP 500")
    monkeypatch.setattr(deals, "_search", broken)
    out = await deals.comparables("Raspberry Pi 5", None, 100, title="Raspberry Pi 5")
    assert out["error"] == "HTTP 500" and "error_kind" not in out


@pytest.mark.parametrize("other, ad, ok", [
    # a list after a number names what comes along: the ad is a Pi with extras, not a case
    ("Gehäuse für Raspberry Pi 5", "Raspberry Pi 5 8GB, 64GB SD-Karte, Netzteil, Gehäuse", False),
    ("Raspberry Pi 5 8GB", "Raspberry Pi 5 8GB, 64GB SD-Karte, Netzteil, Gehäuse", True),
    ("Ubiquiti UniFiSwitch 24 Gigabit Managed PoE Switch, 24x RJ-45, 2x SFP USW-24-POE", "Ubiquiti UniFi Switch USW-24-POE",
     True),                                                          # the model may still follow the list
    ("Makita BL1850B 18V 5,0Ah Akku", "Makita Akku 18V 5Ah", True),  # a decimal comma is no list
    # another generation is another product, when both name one
    ("Alexa Echo Show 8 - 2. Generation", "Amazon Echo Show 8 (3. Generation)", False),
    ("Amazon Echo Show 8 - 4th Generation", "Amazon Echo Show 8 (3. Generation)", False),
    ("Echo Show 8 Gen. 3", "Amazon Echo Show 8 (3. Generation)", True),
    ("Echo show 8", "Amazon Echo Show 8 (3. Generation)", True),
])
def test_bundle_lists_and_generations(other, ad, ok):
    assert (deals.similarity(other, ad) >= deals.MATCH_MIN) is ok


def test_swap_ads_anywhere_in_the_title():
    assert deals._wanted("Prusa MK3s+ 3D Drucker Tausche auch gegen Raspberry Pi5 8GB")
    assert deals._wanted("Suche Raspberry Pi 5") and not deals._wanted("Raspberry Pi 5 8GB, Tausch möglich")


async def test_a_hits_own_geizhals_product_is_used(monkeypatch):
    """The hit card showed "54 % von € 528,41 neu" (the agent's lookup and Claude's check) while the price section
    looked again by the title, found nothing and offered "Claude auf Geizhals suchen lassen"."""
    async def search(site, q):
        return {"rows_found": 0, "ads": []}
    monkeypatch.setattr(deals, "_search", search)
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    ad = AdDetail.from_result({"ad_id": "3540000999", "title": "Ubiquiti UniFi Dream Machine (UDM SE)", "price": 290.0,
                               "platform": "kleinanzeigen"})
    known = {"name": "Ubiquiti UniFi Dream Machine SE", "url": "https://geizhals.at/udm-se-a1.html", "price_from": 528.41,
             "checked": True}
    res = await deals.analyze(ad)
    assert res["geizhals"] is None
    res = await deals.analyze(ad, known_geizhals=known)              # the cached result without one is redone
    assert res["geizhals"]["price_from"] == 528.41 and res["position"]["basis"] == "geizhals"
    assert res["position"]["pct"] == 54
    assert (await deals.analyze(ad, known_geizhals={"name": "x"}))["geizhals"]["price_from"] == 528.41   # kept


async def test_claude_names_the_parts_of_a_self_built_pc(wh, no_auto_gh, monkeypatch):
    """An ad of several products: Claude names the parts, the price check's new price is the sum of theirs."""
    async def match(q, raise_errors=False):
        return {"name": q, "url": f"https://geizhals.at/{len(q)}", "price_from": 100.0}
    monkeypatch.setattr(deals, "geizhals_match", match)
    fake = ScriptedClaude({"product": "", "queries": [], "parts": ["Intel Core i7-3770K", "NVIDIA GeForce GTX 1050"]})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.claude_geizhals(_ad(250, heading="PC – i7 / 16GB RAM / GTX 1050 / SSD"))
    assert res["match"]["price_from"] == 200.0 and res["match"]["by"] == "claude" and len(res["match"]["parts"]) == 2
    assert len(fake.calls) == 1 and "parts" in fake.calls[0]["schema"]["required"]
    from app.main import templates
    html = templates.get_template("_deal.html").render(res={"ad": {"price": 250}, "geizhals": res["match"],
                                                           "position": {"pos": None}}, ad=_ad(250))
    assert "data-gh-parts" in html and "Sum of 2 parts" in html


async def test_claude_calls_the_geizhals_product_wrong_and_the_right_one_is_used(wh, no_auto_gh, monkeypatch):
    """The price check's verdict says the automatic Geizhals product is another one: its price goes, Claude's product
    is looked up instead, and the wrong one does not come back for this ad."""
    wrong = {"name": "ASUS NUC 16 Mini PC", "url": "https://geizhals.at/nuc16", "price_from": 1063.22}
    asked = []

    async def match(q, raise_errors=False):
        asked.append(q)
        return {"name": "Intel NUC 8 i5-8260U", "url": "https://geizhals.at/nuc8", "price_from": 299.0} if "NUC 8" in q else None
    monkeypatch.setattr(deals, "geizhals_match", match)
    fake = ScriptedClaude({"deal": "overpriced", "reason": "Anderer NUC.", "geizhals_ok": False,
                           "product": "Intel NUC 8 i5-8260U", "parts": []})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    ad = _ad(220, ad_id="1000000077", heading="Intel NUC Mini PC i5 16GB")
    res = await deals.analyze(ad, use_claude=True, known_geizhals=wrong)
    assert res["geizhals"]["url"] == "https://geizhals.at/nuc8" and res["geizhals"]["by"] == "claude"
    assert res["geizhals_rejected"] == "ASUS NUC 16 Mini PC" and asked == ["Intel NUC 8 i5-8260U"]
    assert "geizhals_ok" in fake.calls[0]["schema"]["required"]
    deals.remember(res)
    assert deals.stored_geizhals(ad.ref)["url"] == "https://geizhals.at/nuc8"        # kept for the ad


# ------------------------------------------------------------------ an automatic Geizhals lookup that finds another product
KINGSTON = {"name": "Kingston NV3 NVMe PCIe 4.0 SSD 1TB, M.2 2280 / M-Key / PCIe 4.0 x4",
            "url": "https://geizhals.at/kingston-nv3-a3155.html", "price_from": 148.69}


@pytest.mark.parametrize("title, query", [
    # 10-26: "M.2" was taken for the model, so "SSD NVMe M.2" found any SSD; French "Go" is GB
    ("SSD NVMe M.2 Western Digital de 512 Go", "Western Digital 512GB"),
    ("Samsung 990 Pro 2TB NVMe M.2 PCIe 4.0", "Samsung 990 Pro 2TB"),
    ("WD SN740 256GB NVMe M.2 2230 SSD, PCIe Gen4", "WD SN740 256GB"),
    ("Kingston NV3 1TB M.2 2280", "Kingston NV3 1TB"),
])
def test_geizhals_query_skips_slots_and_interfaces(title, query):
    assert deals.geizhals_query(title) == query


@pytest.mark.parametrize("name, title, why", [
    (KINGSTON["name"], "SSD NVMe M.2 Western Digital de 512 Go", "capacity"),
    (KINGSTON["name"], "SSD NVMe M.2 Western Digital", "product"),           # no size to compare: not its brand or model
    ("Raspberry Pi 5 8GB", "Raspberry Pi 5 4GB", "capacity"),
    (KINGSTON["name"], "Kingston NV3 1TB", None),
    ("Apple iPhone 13 128GB", "iPhone 13 128GB Mitternacht", None),          # the brand Geizhals adds: the next word
    ("Ubiquiti UniFi U6 Lite", "UniFi Access Point U6 Lite", None),
    ("Western Digital WD_BLACK SN850X NVMe SSD 1TB", "WD SN850X 1TB", None),  # the model word
    ("Bosch Professional GSR 18V-55", "GSR 18V-55 Akkuschrauber", None),
    ("Samsung SSD 990 PRO 2TB", "Samsung 990 Pro 2 TB NVMe", None),
])
def test_title_conflict(name, title, why):
    assert geizhals.title_conflict(name, title) == why


async def test_price_check_drops_a_lookup_of_another_product_and_keeps_the_agents_verdict(monkeypatch):
    """The price box looked the product up by itself when an ad was opened and took a 1 TB Kingston for a 512 GB WD;
    and it did so although the agent's check (Claude, the same candidates) had found none of them to fit."""
    _fake_search(monkeypatch, [_summ(n, "WD SN5000S 512GB NVMe", p) for n, p in enumerate([45, 50, 55, 60, 65])])
    asked = []

    async def match(q, raise_errors=False):
        asked.append(q)
        return KINGSTON
    monkeypatch.setattr(deals, "geizhals_match", match)
    ad = _ad(55, "SSD NVMe M.2 Western Digital de 512 Go")
    res = await deals.analyze(ad)
    assert asked == ["Western Digital 512GB"] and res["geizhals"] is None
    assert res["position"].get("basis") != "geizhals"                    # not rated by its new price
    deals.clear_cache()
    asked.clear()
    res = await deals.analyze(_ad(55, "Kingston NV3 1TB M.2", ad_id="1234567"), known_geizhals={"checked": True})
    assert asked == [] and res["geizhals"] is None                       # the agent's check said: none fits


async def test_quick_analysis_shows_the_new_price_and_loads_the_comparables_next(monkeypatch):
    """The ad viewer's first request: the Geizhals bar without waiting for the sites' searches; the section then
    fetches itself in full (cached afterwards: no second quick step)."""
    from app.deals_web import _analysis
    searched = []

    async def search_ads(q):
        searched.append(q)
        return {"rows_found": 5, "ads": [_summ(n, "Bosch GSR 18V-55", p) for n, p in enumerate([90, 95, 100, 105, 110])]}
    monkeypatch.setattr(deals, "search_ads", search_ads)
    monkeypatch.setattr(deals, "comparable_sites", lambda platform: ["willhaben"])

    async def match(q, raise_errors=False):
        return {"name": "Bosch Professional GSR 18V-55", "url": "https://geizhals.at/a1.html", "price_from": 150.0}
    monkeypatch.setattr(deals, "geizhals_match", match)
    ad = _ad(100)

    async def detail(ad_id, url=None):
        return ad
    from app import platforms
    monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
    res, err = await _analysis("willhaben", AD_ID, None, quick=True)
    assert err is None and searched == [] and res["comparables"]["loading"] and res["position"]["basis"] == "geizhals"
    html = _plain(_render_deal(res, lang="en"))
    assert "data-comparables-loading" in html and f'hx-get="/hits/ad/willhaben/{AD_ID}/deal"' in html
    assert "Loading comparable ads … The rating may still change." in html and "Compared across" not in html
    res, err = await _analysis("willhaben", AD_ID, None)
    assert searched and res["comparables"]["count"] == 5 and "loading" not in res["comparables"]
    assert "data-comparables-loading" not in _render_deal(res, lang="en")
    searched.clear()
    res, err = await _analysis("willhaben", AD_ID, None, quick=True)            # cached: the full result at once
    assert searched == [] and res["comparables"]["count"] == 5


async def test_price_the_components_of_a_mini_pc(wh, no_auto_gh, monkeypatch):
    """10-26: a NUC 13 Pro with 16 GB RAM and a 990 PRO had no Geizhals price as a whole; "Price the components"
    asks Claude for its parts – any time, also over a product found before – and rates by the sum."""
    prices = {"Intel NUC 13 Pro Barebone NUC13ANKi7": 520.0, "Kingston 16GB DDR4-3200 SO-DIMM": 35.0,
              "Samsung 990 PRO 1TB": 95.0}

    async def match(q, raise_errors=False):
        return {"name": q, "url": f"https://geizhals.at/{len(q)}", "price_from": prices[q]} if q in prices else None
    monkeypatch.setattr(deals, "geizhals_match", match)
    fake = ScriptedClaude({"parts": list(prices), "reason": ""})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    ad = _ad(450, heading="Intel NUC 13 Pro i7-1360P | 16 GB RAM | Samsung 990 PRO 1 TB")
    res = await deals.analyze(ad, find_parts=True)
    gh = res["geizhals"]
    assert gh["price_from"] == 650.0 and gh["by"] == "claude" and len(gh["parts"]) == 3
    assert res["position"]["basis"] == "geizhals" and res["position"]["pct"] == 69
    assert "Barebone" in fake.calls[0]["system"] and fake.calls[0]["schema"]["required"] == ["parts", "reason"]
    html = _plain(_render_deal(res, lang="en"))
    assert "Sum of 3 parts" in html and "data-gh-parts-ask" not in html           # a sum already: no button
    fake2 = ScriptedClaude({"parts": [], "reason": "Ein Einzelgerät ohne Komponenten."})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake2)
    deals.clear_cache()
    res = await deals.analyze(_ad(450, heading="Dyson V11", ad_id="2000000001"), find_parts=True)
    assert res["parts_none"] == "Ein Einzelgerät ohne Komponenten." and res["geizhals"] is None
    html = _render_deal(res, lang="en")
    assert "data-gh-parts-ask" in html and "Ein Einzelgerät ohne Komponenten." in html and '"parts": "1"' in html


def test_viewer_shows_the_sellers_account(app_client):
    """The seller's sign-up date comes with the ad; the PayLivery ratings are read from their profile next."""
    page = app_client.get(f"/hits/ad/{AD_ID}").text
    assert "data-seller-trust" in page and "member since 20" in page
    assert f"/hits/ad/willhaben/{AD_ID}/seller?ad=" in page


# ------------------------------------------------------------------ similar ads chosen by Claude
async def test_claude_names_the_product_and_picks_the_same_variant(monkeypatch):
    """10-26: the rules compared an "HP EliteDesk 800 G6" with G2 and G3 ads and searched a HomePod mini as "Space
    Grau 2025". Claude names the product and variant, the app searches its terms, Claude picks the same ones."""
    ads = {"EliteDesk 800 G6": [_summ(n, t, p) for n, (t, p) in enumerate([
        ("HP EliteDesk 800 G6 Mini i5-10500T 16GB", 260), ("HP EliteDesk 800 G6 Mini PC", 240),
        ("HP EliteDesk 800 G3 Mini i5-7500T", 110), ("HP EliteDesk 800 G6 Mini 8GB 256GB", 230),
        ("HP EliteDesk 800 G2 Mini", 90), ("HP EliteDesk 800 G6 Mini, OVP", 280), ("HP EliteDesk 800 G6 Mini Win 11", 250)])]}
    asked = []

    async def search_ads(q):
        asked.append(q)
        found = ads.get(q, [])
        return {"rows_found": len(found), "ads": found}
    monkeypatch.setattr(deals, "search_ads", search_ads)
    monkeypatch.setattr(deals, "comparable_sites", lambda platform: ["willhaben"])
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: "token")
    monkeypatch.setattr(deals, "_identities", {})
    fake = ScriptedClaude({"product": "HP EliteDesk 800 G6 Mini", "variant": "G6",
                           "queries": ["EliteDesk 800 G6", "HP EliteDesk 800 G6 Mini"]},
                          {"same": [1, 2, 3, 4, 5]})
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(250, heading="HP EliteDesk 800 G6 Desktop Mini PC"))
    comp = res["comparables"]
    assert asked[0] == "EliteDesk 800 G6" and comp["by_claude"] == "HP EliteDesk 800 G6 Mini"
    titles = {a["title"] for a in comp["closest"]}
    assert comp["count"] == 5 and not any("G3" in x or "G2" in x for x in titles)
    assert comp["filtered"]["model"] == 2
    pick = fake.calls[1]["prompt"]
    assert "Variante: G6" in pick and "HP EliteDesk 800 G2 Mini · 90 €" in pick
    assert "Similar ads chosen by Claude for „HP EliteDesk 800 G6 Mini“." in _plain(_render_deal(res, lang="en"))


async def test_claude_failing_leaves_the_rules(monkeypatch):
    _fake_search(monkeypatch, [_summ(n, "Bosch GSR 18V-55", p) for n, p in enumerate([90, 95, 100, 105, 110])])
    monkeypatch.setattr(deals, "comparable_sites", lambda platform: ["willhaben"])
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: "token")
    monkeypatch.setattr(deals, "_identities", {})
    fake = ScriptedClaude({"product": "Bosch GSR 18V-55", "variant": "", "queries": ["GSR 18V-55"]},
                          LLMError("overloaded"))
    monkeypatch.setattr(deals, "get_backend", lambda *a: fake)
    res = await deals.analyze(_ad(100))
    assert res["comparables"]["count"] == 5 and "by_claude" not in res["comparables"]        # the rules decided
    deals.clear_cache()
    from app.db import set_setting
    set_setting(deals.COMPARE_SETTING, "0")                                   # switched off: Claude is not asked
    fake.calls.clear()
    await deals.analyze(_ad(100, ad_id="1000000099"))
    assert fake.calls == []
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: None)
    set_setting(deals.COMPARE_SETTING, "1")
    assert not deals.compare_by_claude()                                       # no Claude login: the rules


async def test_sold_ads_of_the_agents_hits_show_beside_the_comparables(monkeypatch):
    """Hits that went sold or offline lately keep their last price: "sold or taken down recently", apart from the
    asking prices of the stats; only those of this product (the title filter here, Claude's pick with Claude)."""
    from datetime import timedelta

    from app.db import Hit, session, utcnow
    _fake_search(monkeypatch, [_summ(n, "Bosch GSR 18V-55", p) for n, p in enumerate([90, 95, 100, 105, 110])])
    monkeypatch.setattr(deals, "comparable_sites", lambda platform: ["willhaben"])
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    with session() as db:
        for n, (title, price, status, days) in enumerate([("Bosch GSR 18V-55 Akkuschrauber", 70.0, "sold", 3),
                                                          ("Bosch GSR 18V-55 solo", 80.0, "gone", 10),
                                                          ("Bosch GSR 18V-60 Akkuschrauber", 150.0, "sold", 2),
                                                          ("Bosch GSR 18V-55", 60.0, "sold", 200),       # too long ago
                                                          ("Bosch GSR 18V-55", 75.0, "active", 1)]):
            db.add(Hit(agent_id=1, ad_id=f"55{n}", heading=title, url=f"https://x/{n}", price=price, site_status=status,
                       status_checked_at=utcnow() - timedelta(days=days)))
        db.commit()
    res = await deals.analyze(_ad(100))
    sold = res["comparables"]["sold"]
    assert sold["count"] == 2 and sold["median"] == 75.0 and res["comparables"]["count"] == 5
    assert {a["price"] for a in sold["ads"]} == {70.0, 80.0}                     # the 18V-60 is another model
    html = _plain(_render_deal(res, lang="en"))
    assert "Sold or taken down recently:</span> median € 75 of 2 ads" in html and "data-sold" in html


async def test_the_status_sweep_reads_recent_hits_quietly(monkeypatch):
    from datetime import timedelta

    from app import platforms
    from app.db import Hit, session, utcnow
    from app.services import search_agents
    read = []

    async def detail(ad_id, url=None):
        read.append(ad_id)
        return None if ad_id == "601" else _ad(100, ad_id=ad_id)
    monkeypatch.setattr(platforms.get("willhaben"), "detail", detail)
    with session() as db:
        db.add(Hit(agent_id=1, ad_id="601", heading="a", url="x", price=10.0, first_seen_at=utcnow()))
        db.add(Hit(agent_id=1, ad_id="602", heading="b", url="x", price=10.0, first_seen_at=utcnow(), site_status="active",
                   status_checked_at=utcnow() - timedelta(days=5)))
        db.add(Hit(agent_id=1, ad_id="603", heading="c", url="x", price=10.0, first_seen_at=utcnow(),
                   screen_relevant=False))                                         # ruled out: not read
        db.add(Hit(agent_id=1, ad_id="604", heading="d", url="x", price=10.0, first_seen_at=utcnow() - timedelta(days=60)))
        db.add(Hit(agent_id=1, ad_id="605", heading="e", url="x", price=10.0, first_seen_at=utcnow(),
                   platform="facebook"))                                       # its login block: never swept
        db.commit()
    await search_agents.status_sweep_job()
    assert sorted(read) == ["601", "602"]
    with session() as db:
        st = {h.ad_id: h.site_status for h in db.exec(deals.select(Hit))}
    assert st["601"] == "gone" and st["602"] == "active"
    read.clear()
    await search_agents.status_sweep_job()
    assert read == []                                                              # checked: not again for 2 days


def test_own_ad_gets_the_sellers_view(app_client):
    """My ads: the price section of one's own ad says where fair lies and what sells faster."""
    frag = app_client.get(f"/hits/ad/willhaben/{AD_ID}/deal?mine=1").text
    assert "data-seller-hint" in frag and "As a seller: fair here is" in frag
    assert "data-seller-hint" not in app_client.get(f"/hits/ad/willhaben/{AD_ID}/deal").text
    quick = app_client.get(f"/hits/ad/willhaben/{AD_ID}/deal?quick=1&mine=1").text
    if "data-comparables-loading" in quick:                       # cached already: the full section at once
        assert "mine=1" in quick


def test_seller_hint():
    p = deals.position(100, deals.stats(PRICES), None)              # median 105, similar zones 70/90/115/135
    assert deals.seller_hint(p) == {"fair_from": 95, "fair_to": 121, "level": "fair", "basis": "comparables"}
    assert deals.seller_hint({"pos": None}) is None


async def test_a_buyers_draft_knows_the_price_rating(monkeypatch):
    """Writing to a seller (no purchase order): Claude's draft gets the price check's rating and where to start."""
    from app.db import Conversation
    from app.services import chats
    _fake_search(monkeypatch, [_summ(n, "Bosch GSR 18V-55", p) for n, p in enumerate([90, 95, 100, 105, 110])])
    monkeypatch.setattr(deals, "comparable_sites", lambda platform: ["willhaben"])
    monkeypatch.setattr(deals, "geizhals_match", _no_geizhals)
    await deals.analyze(_ad(100))                                            # the price check ran: cached
    conv = Conversation(id="b1", ad_id=AD_ID, role="buyer", partner_name="x", ad_context={"title": "Bosch", "price": 100})
    req = await chats.compose(conv, [("in", None, "Hallo, noch da?")], auto=False, ad=None)
    assert "Preiseinschätzung der App (nach ähnlichen Anzeigen)" in req["prompt"] and "zuerst um 90 €" in req["prompt"]          # median 100: fair from 90
    seller = Conversation(id="s1", ad_id=AD_ID, role="seller", partner_name="x", ad_context={"title": "Bosch"})
    assert "Preiseinschätzung" not in (await chats.compose(seller, [("in", None, "?")], auto=False, ad=None))["prompt"]
