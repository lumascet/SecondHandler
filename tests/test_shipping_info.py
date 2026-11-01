"""Shipping to the owner: what each site states in its result list or on the ad page, and how it is shown."""
import pytest

from app.platforms import ebay, facebook, kleinanzeigen, preisjaeger, shpock
from app.platforms import shipping_info as si
from tests.conftest import FIXTURES, load


@pytest.mark.parametrize("text, expected", [
    ("+EUR 5,00 Versand", (5.0, "ship")), ("+EUR 59,99 Versand", (59.99, "ship")),
    ("Kostenloser Versand", (0.0, "ship")), ("Gratisversand", (0.0, "ship")), ("Nur Abholung", (None, "pickup")),
    ("Versand möglich", (None, "ship")), ("+EUR 1.234,50 Versand", (1234.5, "ship")), ("", (None, None)),
    ("oder Preisvorschlag", (None, None)),
])
def test_from_text(text, expected):
    assert si.from_text(text) == expected


def test_labels_and_prompt():
    assert si.label(5.0, "ship") == "+ € 5,00 shipping" and si.label(0.0, "ship") == "free shipping"
    assert si.label(None, "pickup") == "pickup only" and si.label(None, "ship") == "shipping possible"
    assert si.label(None, None) == "" and si.label_of({}) == "" and si.label_of({"shipping": "pickup"}) == "pickup only" and si.total(20.0, 4.5) == 24.5 and si.total(20.0, None) is None
    assert si.prompt_line(4.5, "ship").startswith("Versand: 4.5 €") and si.prompt_line(None, None) == "Versand: nicht angegeben"


def test_apply_keeps_what_the_list_does_not_state():
    class O:
        shipping_price = None
        shipping = None

    hit, page, plain = O(), O(), O()
    page.shipping_price = 4.9
    si.apply_shipping(hit, page)
    assert (hit.shipping_price, hit.shipping) == (4.9, "ship")
    si.apply_shipping(hit, plain)                          # a list without shipping: kept
    assert (hit.shipping_price, hit.shipping) == (4.9, "ship")
    plain.shipping = "pickup"
    si.apply_shipping(hit, plain)
    assert (hit.shipping_price, hit.shipping) == (None, "pickup")


def test_the_sites_state_it():
    ads = ebay.parse_search((FIXTURES / "ebay_search.html").read_text(encoding="utf-8")).ads
    assert {(a.shipping_price, a.shipping) for a in ads} >= {(5.0, "ship"), (0.0, "ship")}
    assert kleinanzeigen.parse_ad(load("kleinanzeigen_ad_handy.json")).shipping == "pickup"
    assert {a.shipping for a in shpock.parse_search(load("shpock_search.json")).ads} <= {"ship", "pickup", None}
    fb = facebook.parse_search((FIXTURES / "facebook_search.html").read_text(encoding="utf-8")).ads
    assert "pickup" in {a.shipping for a in fb}
    pj = preisjaeger.parse_search((FIXTURES / "preisjaeger_search.json").read_text(encoding="utf-8")).ads
    assert pj and all(a.shipping is None and a.shipping_price is None for a in pj)   # the REST API states no shipping


def test_ebay_asks_for_the_owners_postcode():
    from app.services import shipping

    assert ebay.ship_to() == {}
    shipping.save_sender({"name": "Max Muster", "street": "Musterweg", "house_no": "1", "zip": "1010", "city": "Wien"})
    assert ebay.ship_to() == {"_stpos": "1010"}


async def test_a_pushed_hit_gets_its_shipping_from_the_ad_page(monkeypatch):
    """The list states no shipping: the hit about to be pushed has its ad page loaded, and the push names it."""
    from app import platforms
    from app.db import Hit, session
    from app.services import search_agents
    from app.services.search_agents import create_agent, run_agent
    from sqlmodel import select
    from tests.fake_platform import FakePlatform, detail_of, listing

    sent = []

    async def notify(kind, title, text="", **kw):
        sent.append((kind, title, kw.get("data") or {}))

    monkeypatch.setattr(search_agents, "notify", notify)
    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    try:
        a = create_agent("https://fake.example/s/rad")
        fake.pages = [[]]
        await run_agent(a.id)
        d = detail_of(listing("1", "Rennrad", 300.0))
        d.shipping_price = 4.9
        fake.details = {"1": d}
        fake.pages = [[listing("1", "Rennrad", 300.0)]]
        await run_agent(a.id)
        assert fake.detail_calls == ["1"]
        kind, title, data = sent[-1]
        assert kind == "hit" and "+ € 4,90 shipping" in title and data["shipping_price"] == 4.9
        with session() as db:
            h = db.exec(select(Hit)).one()
            assert (h.shipping_price, h.shipping, h.site_status) == (4.9, "ship", "active")
        fake.pages = [[listing("1", "Rennrad", 300.0)]]                 # the next list keeps what the page told
        await run_agent(a.id)
        with session() as db:
            assert db.exec(select(Hit)).one().shipping_price == 4.9
    finally:
        platforms.reset()


def test_search_cards_load_the_shipping_once_in_view(monkeypatch):
    """A result whose list stated no price loads its ad page when the card is in view; kept for hours."""
    from fastapi.testclient import TestClient

    from app import platforms
    from app.main import _ship_cache, app
    from tests.fake_platform import FakePlatform, detail_of, listing

    fake = FakePlatform()
    platforms.set_platform("fake", fake)
    _ship_cache.clear()
    try:
        d = detail_of(listing("5", "Rad", 50.0))
        d.shipping_price = 6.0
        fake.details = {"5": d}
        with TestClient(app) as c:
            assert "+ € 6,00 shipping" in c.get("/ship/fake/5").text
            assert "+ € 6,00 shipping" in c.get("/ship/fake/5").text
            assert fake.detail_calls == ["5"]                                # the second from the cache
            assert c.get("/ship/nosuchsite/5").text == ""
        from app.main import templates
        ui = templates.get_template("_ui.html").module
        assert 'hx-get="/ship/fake/7"' in str(ui.ship_lazy({"platform": "fake", "ad_id": "7"}))
        assert "hx-get" not in str(ui.ship_lazy({"platform": "fake", "ad_id": "7", "shipping": "pickup"}))
        assert "free shipping" in str(ui.ship_lazy({"platform": "fake", "ad_id": "7", "shipping_price": 0.0}))
    finally:
        platforms.reset()
        _ship_cache.clear()


def test_willhaben_lists_say_paylivery_ships():
    from app.willhaben.parse import parse_search
    from tests.conftest import load

    data = load("search_fahrrad_rows3.json")
    first = data["advertSummaryList"]["advertSummary"][0]
    next(a for a in first["attributes"]["attribute"] if a["name"] == "p2penabled")["values"] = ["true"]
    ads = parse_search(data).ads
    assert ads[0].shipping == "ship" and ads[1].shipping is None


def test_claudes_unclear_shipping_flag_is_dropped():
    from app.services.product_search import red_flags

    flags = ["Versand möglich, Kosten unklar", "nur Versand", "Preis unrealistisch niedrig", "Versandkosten unbekannt"]
    assert red_flags(flags) == ["nur Versand", "Preis unrealistisch niedrig"]


async def test_the_best_offers_get_their_shipping_before_the_photo_check():
    from app import platforms
    from app.services.product_search import fill_shipping
    from tests.fake_platform import FakePlatform, detail_of, listing

    fake = FakePlatform()
    d = detail_of(listing("1", "Lautsprecher", 25.0))
    d.shipping_price, d.shipping_options = 3.49, [{"name": "Post", "home": 5.7, "point": 3.49}]
    fake.details = {"1": d}
    platforms.set_platform("fake", fake)
    try:
        rs = [{"platform": "fake", "ad_id": "1", "shipping": "ship"}, {"platform": "fake", "ad_id": "2"},
              {"platform": "fake", "ad_id": "3", "shipping": "ship", "shipping_price": 4.0}]
        await fill_shipping(rs)
    finally:
        platforms.reset()
    assert rs[0]["shipping_price"] == 3.49 and rs[0]["shipping_options"][0]["home"] == 5.7
    assert fake.detail_calls == ["1"]                     # the list said nothing / the price was known: no request


def test_the_ad_viewer_breaks_the_price_down():
    from fastapi.testclient import TestClient

    from app import platforms
    from app.main import app
    from tests.fake_platform import FakePlatform, detail_of, listing

    fake = FakePlatform()
    d = detail_of(listing("1", "Lautsprecher", 25.0))
    d.shipping_price, d.shipping_options, d.paylivery_enabled = 3.49, [{"name": "Post", "home": 5.7, "point": 3.49}], True
    d.platform = "willhaben"                              # PayLivery: willhaben's buyer fee is not known here
    v = detail_of(listing("2", "Jacke", 20.0))
    v.platform, v.shipping_price, v.buyer_fee = "vinted", 2.95, 1.70     # Vinted states its fee
    p = detail_of(listing("3", "Rad", 20.0))
    p.shipping_price = 4.9                                # a site without a buyer fee
    fake.details = {"1": d, "2": v, "3": p}
    platforms.set_platform("fake", fake)
    try:
        with TestClient(app) as c:
            frags = [c.get(f"/hits/ad/fake/{i}").text for i in "123"]
    finally:
        platforms.reset()
    box = frags[0].split("data-price-breakdown")[1].split("</div>")[0]
    assert "Shipping Post to a pickup point" in box and "€ 3,49" in box and "€ 5,70" in box
    assert "Buyer protection fee" not in box                   # willhaben's varies by category: never guessed
    assert "Total from" in box and "€ 28,49" in box
    assert 'hx-get="/wh-fee/1?price=25.0&amp;sp=3.49"' in frags[0]           # the exact fee, from the PayLivery window
    box = frags[1].split("data-price-breakdown")[1].split("</div>")[0]
    assert "Buyer protection fee" in box and "€ 1,70" in box and "€ 24,65" in box        # as Vinted states it
    box = frags[2].split("data-price-breakdown")[1].split("</div>")[0]
    assert "Buyer protection fee" not in box and "€ 24,90" in box
