"""vinted.at through the website's server-rendered data (RSC): the catalog, an item page, links, the request, errors."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app import platforms
from app.db import Hit, session, set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.platforms import vinted as vi
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import mentions
from app.services.search_agents import create_agent, run_agent
from tests.conftest import FIXTURES
from tests.test_kleinanzeigen import Queue
from tests.test_platforms_contract import VINTED_ROUTES, FileTransport

ITEM = "10341874098"
NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def client():
    c = vi.VintedClient(transport=FileTransport(VINTED_ROUTES))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    yield c
    vi.set_client(None)


@pytest.fixture
def platform(client):
    platforms.reset()
    set_setting("platform_vinted_enabled", "1")
    yield platforms.get("vinted")
    platforms.reset()


def test_catalog_becomes_listings():
    page = vi.parse_catalog((FIXTURES / "vinted_catalog.rsc").read_text(encoding="utf-8"))
    assert page.rows_found == 960 and [a.ad_id for a in page.ads] == [ITEM, "10341865289", "10341867751", "10341838716"]
    first, _, dealer, discounted = page.ads
    assert first.heading == "Handyhüllen Iphone 16 max/ Pro max" and first.price == 2 and first.price_display == "2 €"
    assert first.body == "Cimar · Neu, mit Etikett · 2,80 € incl. buyer protection" and first.is_private is True
    assert first.url == f"https://www.vinted.at/items/{ITEM}-handyhullen-iphone-16-max-pro-max"
    assert first.image_url.startswith("https://images1.vinted.net/") and first.location is None and first.published_at is None
    assert dealer.is_private is False and first.ref == f"vinted:{ITEM}"
    assert discounted.price == 1.5 and discounted.old_price == 20 and discounted.price_display == "1,50 €"
    with pytest.raises(PlatformError, match="result list"):
        vi.parse_catalog('1:"$Sreact.fragment"\n')


def test_item_page_becomes_detail():
    d = vi.parse_item((FIXTURES / "vinted_item.rsc").read_text(encoding="utf-8"), ITEM, now=NOW)
    assert d.heading == "Handyhüllen Iphone 16 max/ Pro max" and d.price == 2 and d.price_display == "2 €"
    assert d.description.startswith("unbenutzt und neu verpackt") and d.description.endswith("Farbe: Bunt")
    assert "Zustand: Neu, mit Etikett" in d.description and "Hochgeladen" not in d.description
    assert d.published_at == NOW - timedelta(minutes=6) and d.seller_name == "seller123" and d.is_private is True
    assert len(d.images) == 6 and d.url == f"https://www.vinted.at/items/{ITEM}" and d.ref == f"vinted:{ITEM}"
    assert vi.parse_item("nothing here", ITEM) is None
    assert d.shipping_price is None and d.shipping is None          # the fixture has no pricing block


def _priced(services: str) -> str:
    return (FIXTURES / "vinted_item.rsc").read_text(encoding="utf-8") + '\n2c:["$","$Ld3",null,{"pricingServices":' \
        '{"services":' + services + '}}]\n'


@pytest.mark.parametrize("services,price,kind", [
    ('{"isPickUpOnly":false,"shipping":{"type":"shipping","originalPrice":{"amount":"6.5","currencyCode":"EUR"},'
     '"finalPrice":{"amount":"4.95","currencyCode":"EUR"},"isFree":false}}', 4.95, "ship"),
    ('{"isPickUpOnly":false,"shipping":{"finalPrice":{"amount":"0","currencyCode":"EUR"},"isFree":true}}', 0.0, "ship"),
    ('{"isPickUpOnly":true,"shipping":null}', None, "pickup"),
])
def test_item_page_shipping(services, price, kind):
    d = vi.parse_item(_priced(services), ITEM, now=NOW)
    assert (d.shipping_price, d.shipping) == (price, kind)


@pytest.mark.parametrize("text,age", [("6 Min.", timedelta(minutes=6)), ("2 Std.", timedelta(hours=2)),
                                      ("3 Tage", timedelta(days=3)), ("1 Woche", timedelta(days=7)),
                                      ("vor 2 Monaten", timedelta(days=60)), ("eine Stunde", timedelta(hours=1)),
                                      ("Hochgeladen 6 min.", timedelta(minutes=6)), ("gerade eben", timedelta(0)),
                                      ("2 hours ago", timedelta(hours=2)), ("9 h", timedelta(hours=9)),
                                      ("2 d", timedelta(days=2)), ("5 min", timedelta(minutes=5)),
                                      ("3 Mon.", timedelta(days=90)), ("1 T", timedelta(days=1))])
def test_upload_age(text, age):
    assert vi.parse_age(text, NOW) == NOW - age


def test_unreadable_age():
    assert vi.parse_age("", NOW) is None and vi.parse_age("bald", NOW) is None and vi.parse_age(None) is None


def test_links_keep_the_sites_filters():
    p = vi.VintedPlatform()
    spec = p.parse_search_url("https://www.vinted.de/catalog/2994-electronics?search_text=switch&brand_ids[]=1&"
                              "status_ids[]=6&page=4&time=1&order=relevance&search_session_id=x")
    assert spec.keyword == "switch" and spec.category == "2994" and spec.extra == {
        "params": [["brand_ids[]", "1"], ["status_ids[]", "6"]], "host": "www.vinted.de"}
    assert spec.url == ("https://www.vinted.de/catalog/2994?search_text=switch&brand_ids%5B%5D=1&status_ids%5B%5D=6"
                        "&order=newest_first")
    assert p.parse_search_url(spec.url) == spec
    only_filter = p.parse_search_url("https://www.vinted.at/catalog?brand_ids[]=53")
    assert only_filter.keyword is None and p.describe(only_filter)
    for bad in ("https://www.vinted.at/catalog?price_from=x&search_text=a", "https://www.vinted.at/member/1-x",
                "https://www.vinted.fr/catalog?search_text=x"):
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)
    assert p.ad_url(ITEM) == f"https://www.vinted.at/items/{ITEM}" and p.extract_ad_id(ITEM) == ITEM
    assert vi.classify("https://www.vinted.at/catalog?x") == "search" and vi.classify(p.ad_url(ITEM)) == "detail"


async def test_request_asks_for_the_flight_data_newest_first(platform, client):
    page = await platform.search(platform.keyword_search("iphone", 5, 50), rows=2)
    assert len(page.ads) == 2 and page.title == "iphone"
    url, headers = client._transport.calls[-1]
    assert url == "https://www.vinted.at/catalog?search_text=iphone&price_from=5&price_to=50&currency=EUR&order=newest_first"
    assert headers["RSC"] == "1"
    await platform.search(platform.keyword_search("iphone", sort="relevance"))
    assert client._transport.calls[-1][0].endswith("order=relevance")
    d = await platform.detail(ITEM, url=f"https://www.vinted.de/items/{ITEM}-x")
    assert d.heading and client._transport.calls[-1][0] == f"https://www.vinted.de/items/{ITEM}"


async def test_results_without_every_search_word_are_left_out(platform, client):
    page = await platform.search(platform.keyword_search("iphone 15 pro"))
    assert [a.heading for a in page.ads] == ["Cover iPhone 15 pro Dsquared2", "Rhode coque iPhone 15/16 pro Max"]
    assert page.rows_found == 960                                       # Vinted's own count stays
    assert len((await platform.search(platform.keyword_search("Cimar"))).ads) == 1      # the brand line counts


def test_mentions_ignores_case_accents_spaces_and_punctuation():
    assert mentions("Pi5 8GB", "pi 5") and mentions("Écho show-8 (2. Gen)", "Echo Show 8") and mentions("x", "")
    assert not mentions("Fond de teint Luna by Lisa", "Unifi") and not mentions(None, "Unifi")


async def test_block_and_missing_items():
    c = vi.VintedClient(transport=Queue([(403, b""), (500, b""), (404, b""), (200, b"<html>consent</html>")]))
    c.limiter = RateLimiter(0, name="Vinted")
    with pytest.raises(Blocked):
        await c.catalog("https://www.vinted.at/catalog?search_text=x", "www.vinted.at")
    c.limiter = RateLimiter(0, name="Vinted")
    with pytest.raises(PlatformError, match="HTTP 500"):
        await c.catalog("https://www.vinted.at/catalog?search_text=x", "www.vinted.at")
    assert await c.item("1") is None
    with pytest.raises(PlatformError, match="could not be read"):       # a 200 without the item: never "gone"
        await c.item("2")


async def test_agent_end_to_end(platform, client):
    a = create_agent("https://www.vinted.at/catalog?search_text=iphone+15+pro&order=newest_first")
    assert a.platform == "vinted" and a.name == "iphone 15 pro" and a.interval_s >= 300
    res = await run_agent(a.id)
    assert res.baseline and res.error is None and res.rows_found == 960
    with session() as db:
        hits = list(db.exec(select(Hit).where(Hit.agent_id == a.id)))
    assert len(hits) == 2 and {h.platform for h in hits} == {"vinted"}      # the iPhone 16 case and the cable: out
