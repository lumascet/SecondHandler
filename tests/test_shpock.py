"""shpock.com through the website's GraphQL API: the listings, the item page, the links, the request and its errors."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app import platforms
from app.db import Hit, session, set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.platforms import shpock as sh
from app.platforms.ratelimit import RateLimiter
from app.services.search_agents import create_agent, run_agent
from tests.conftest import FIXTURES, load
from tests.test_kleinanzeigen import Queue
from tests.test_platforms_contract import SHPOCK_ROUTES, FileTransport

ITEM = "asu6-t2hxq7bDRFz"


@pytest.fixture
def client():
    c = sh.ShpockClient(transport=FileTransport(SHPOCK_ROUTES))
    c.limiter = RateLimiter(0, name="Shpock")
    sh.set_client(c)
    yield c
    sh.set_client(None)


@pytest.fixture
def platform(client):
    platforms.reset()
    set_setting("platform_shpock_enabled", "1")
    yield platforms.get("shpock")
    platforms.reset()


def test_search_answer_becomes_listings():
    page = sh.parse_search(load("shpock_search.json"))
    assert [a.ad_id for a in page.ads] == [ITEM, "asuokiMmuRvIB_Ms", "asujonHZ09fRAmEi", "asuXd_pn-ixnBBF2"]   # sold: out
    first, reduced, free, _ = page.ads
    assert first.price == 450 and first.price_display == "450 €" and first.location == "6971 Hard" and first.postcode == "6971"
    assert first.image_url == "https://m1.secondhandapp.at/2.0/6acbbafadda1c6aedb52e82b" and first.published_at is None
    assert first.body.startswith("Ich verkaufe mein iPhone 15 Pro") and first.ref == f"shpock:{ITEM}"
    assert first.url == f"https://www.shpock.com/de-at/i/{ITEM}/apple-iphone-15-pro-128-gb-titan-schwarz"
    assert reduced.price == 499 and reduced.old_price == 549 and first.old_price is None
    assert free.price == 0 and free.price_display == "Zu verschenken"
    assert page.rows_found == 4 and first.is_private is None and first.is_bumped is None
    with pytest.raises(PlatformError, match="failed"):
        sh.parse_search({"errors": [{"message": "Unexpected server error"}], "data": None})


def test_item_page_becomes_detail():
    html = (FIXTURES / "shpock_item.html").read_text(encoding="utf-8")
    d = sh.parse_item_page(html, ITEM)
    assert d.heading == "Apple IPhone 15 Pro 128 GB Titan Schwarz" and d.seller_name == "Max M." and d.is_private is True
    assert d.published_at.isoformat() == "2026-10-11T16:36:10+00:00" and len(d.images) == 5 and d.status == "active"
    assert d.description.endswith("Zustand: Gut") and "Marke: Apple" in d.description and d.place == "6971 Hard, Österreich"
    assert d.ref == f"shpock:{ITEM}" and d.price_display == "450 €"
    assert sh.parse_item_page(html, "asOTHER000000000") is None and sh.parse_item_page("<html></html>", ITEM) is None


def test_links_and_ids():
    p = sh.ShpockPlatform()
    spec = p.parse_search_url("https://www.shpock.com/de-de/results?q=Rennrad%20Carbon")
    assert spec.keyword == "Rennrad Carbon" and spec.price_from is None
    assert spec.url == "https://www.shpock.com/de-at/results?q=Rennrad+Carbon"
    assert p.parse_search_url("https://shpock.com/results?q=x&priceTo=80").price_to == 80
    for bad in ("https://www.shpock.com/de-at/results?q=x&priceFrom=abc", f"https://www.shpock.com/de-at/i/{ITEM}",
                "ftp://www.shpock.com/de-at/results?q=x"):
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)
    assert p.extract_ad_id(ITEM) == ITEM and p.extract_ad_id("123456789012") is None
    assert p.ad_url(ITEM) == f"https://www.shpock.com/de-at/i/{ITEM}"
    assert sh.classify("https://www.shpock.com/graphql?x=1") == "search" and sh.classify(p.ad_url(ITEM)) == "detail"
    assert p.filters_of(p.keyword_search("rad", 10, None, sort="price_asc")) == {
        "q": "rad", "sort": {"type": "price", "direction": "a"}, "price": {"from": 10}}


async def test_search_is_one_graphql_get(platform, client):
    page = await platform.search(platform.keyword_search("iphone 15 pro", 100, 500), rows=3)
    assert len(page.ads) == 3 and page.title == "iphone 15 pro"
    url, headers = client._transport.calls[-1]
    assert url.startswith("https://www.shpock.com/graphql?") and headers["apollo-require-preflight"] == "true"
    qs = parse_qs(urlsplit(url).query)
    assert qs["operationName"] == ["ItemSearch"] and "itemSearch(" in qs["query"][0]
    variables = json.loads(qs["variables"][0])
    assert variables["pagination"] == {"limit": 3} and variables["trackingSource"] == "Search"
    assert json.loads(variables["serializedFilters"]) == {
        "q": "iphone 15 pro", "sort": {"type": "date_listed", "direction": "d"}, "price": {"from": 100, "to": 500}}
    d = await platform.detail(ITEM)
    assert d.seller_name == "Max M." and client._transport.calls[-1][0] == f"https://www.shpock.com/de-at/i/{ITEM}"


async def test_errors_and_missing_items():
    deleted = (b'<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"apolloState":'
               b'{"ROOT_QUERY":{}}}}}</script>')
    c = sh.ShpockClient(transport=Queue([(429, b""), (500, b""), (200, b"<html>consent</html>"), (200, deleted),
                                         (404, b"")]))
    c.limiter = RateLimiter(0, name="Shpock")
    with pytest.raises(Blocked):
        await c.search_items({"q": "x"}, 5)
    c.limiter = RateLimiter(0, name="Shpock")
    with pytest.raises(PlatformError, match="HTTP 500"):
        await c.search_items({"q": "x"}, 5)
    with pytest.raises(PlatformError, match="could not be read"):    # no page state: never "gone" for good
        await c.item(ITEM)
    assert await c.item("asDEL00000000000") is None                     # the page says it has no such item
    assert await c.item("asGONE0000000000") is None


def _decoy(n=3):
    """The bot trap's answer, made up here the same way: padded ids, photo ids padded with zeros."""
    items = [{"__typename": "ItemSummary", "id": f"AHf{i}tAAAAAAAAAAA", "title": "Lorem ipsum", "price": 400,
              "locality": "1758 Est quis", "path": f"/en-gb/i/AHf{i}tAAAAAAAAAAA/lorem", "isSold": False,
              "isExpired": False, "media": [{"id": f"0077{i}cc60000000000000000"}]} for i in range(n)]
    return {"data": {"itemSearch": {"__typename": "FetchItemsResponse", "total": None,
                                    "itemResults": [{"distanceGroup": "ungrouped", "items": items}]}}}


async def test_the_bot_traps_made_up_ads_are_no_hits():
    assert not sh.is_decoy(load("shpock_search.json")) and not sh.is_decoy({}) and not sh.is_decoy(None)
    assert sh.is_decoy(_decoy())
    one_real = _decoy(1)
    one_real["data"]["itemSearch"]["itemResults"][0]["items"] += load("shpock_search.json")["data"]["itemSearch"][
        "itemResults"][0]["items"]
    assert not sh.is_decoy(one_real)                         # one odd id among real ones: a real answer
    c = sh.ShpockClient(transport=Queue([(200, json.dumps(_decoy()).encode())]))
    c.limiter = RateLimiter(0, name="Shpock")
    with pytest.raises(PlatformError, match="made-up ads"):
        await c.search_items({"q": "x"}, 5)


async def test_the_bot_trap_through_the_vpn_is_a_bot_check():
    from app.platforms.httpclient import Trial, trial
    from tests.test_httpclient import FakeTunnel, _vpn_client

    tunnel = FakeTunnel()
    c, _, _ = _vpn_client({"http://exit1": [(200, json.dumps(_decoy()).encode())]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr):
        r = await c._get("https://fake.example/api", valid=sh._real_answer)
    assert r.status_code == 200 and tr.challenge                # the VPN search: "botcheck", the server is marked


async def test_agent_end_to_end(platform, client):
    a = create_agent("https://www.shpock.com/de-at/results?q=iphone+15+pro")
    assert a.platform == "shpock" and a.name == "iphone 15 pro"
    res = await run_agent(a.id)
    assert res.baseline and res.error is None
    with session() as db:
        hits = list(db.exec(select(Hit).where(Hit.agent_id == a.id)))
    assert len(hits) == 4 and ITEM in {h.ad_id for h in hits} and {h.platform for h in hits} == {"shpock"}
