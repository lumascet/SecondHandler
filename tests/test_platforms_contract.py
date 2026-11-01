"""Every registered platform adapter honours the same contract (app/platforms/base.py), checked against fixtures.

Add a new site here: one CASES entry with a search link, a keyword and the fixtures its fake transport serves.
"""
import json

import httpx
import pytest

from app import platforms
from app.platforms import InvalidSearchUrl, Listing, SearchSpec
from app.platforms.types import AdDetail
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient, RateLimiter
from tests.conftest import FIXTURES, load


def _willhaben_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/webapi/ad-search/search" in request.url.path:
            return httpx.Response(200, json=load("search_fahrrad_rows3.json"))
        if request.url.path.startswith("/iad/object"):
            nd = load("detail_922008415_nextdata.json")
            return httpx.Response(200, text=f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>')
        return httpx.Response(404)
    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    return c


class KaTransport:
    """The kleinanzeigen app API from fixtures (search, one ad, places); unknown ads are 404."""

    def __init__(self):
        self.calls = []

    async def send(self, method, url, *, params=None, headers=None):
        from app.platforms.httpclient import FetchResponse
        self.calls.append((url, headers))
        if url.endswith("/ads.json") or "/ads.json?" in url:
            body = json.dumps(load("kleinanzeigen_ads.json")).encode()
        elif "/ads/2712345678.json" in url:
            body = json.dumps(load("kleinanzeigen_ad.json")).encode()
        elif "/locations.json" in url:
            body = json.dumps(load("kleinanzeigen_locations.json")).encode()
        else:
            return FetchResponse(404, {}, b"", url)
        return FetchResponse(200, {"content-type": "application/json"}, body, url)

    def clear_cookies(self):
        pass

    async def aclose(self):
        pass


def _install_ka():
    from app.platforms import kleinanzeigen as ka
    ka.set_client(ka.KleinanzeigenClient(transport=KaTransport()))
    ka.get_client().limiter = RateLimiter(0, name="Kleinanzeigen")


def _uninstall_ka():
    from app.platforms import kleinanzeigen as ka
    ka.set_client(None)


class FileTransport:
    """Serves fixture files by URL substring (first match wins); everything else is a 404. Records (url, headers)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def send(self, method, url, *, params=None, headers=None):
        from app.platforms.httpclient import FetchResponse
        self.calls.append((url, headers))
        for part, name, ctype in self.routes:
            if part in url:
                return FetchResponse(200, {"content-type": ctype}, (FIXTURES / name).read_bytes(), url)
        return FetchResponse(404, {}, b"", url)

    def clear_cookies(self):
        pass

    async def aclose(self):
        pass


SHPOCK_ROUTES = [("/graphql", "shpock_search.json", "application/json"),
                 ("/i/asu6-t2hxq7bDRFz", "shpock_item.html", "text/html")]
VINTED_ROUTES = [("/catalog", "vinted_catalog.rsc", "text/x-component"),
                 ("/items/10341874098", "vinted_item.rsc", "text/x-component")]
PREISJAEGER_ROUTES = [("/rest_api/v2/thread", "preisjaeger_search.json", "application/json")]
EBAY_ROUTES = [("/itmdesc/920036165580", "ebay_desc.html", "text/html"),
               ("/itm/920036165580", "ebay_item.html", "text/html"), ("/sch/i.html", "ebay_search.html", "text/html")]

FACEBOOK_ROUTES = [("/marketplace/item/1438848741764802", "facebook_item.html", "text/html"),
                   ("/search", "facebook_search.html", "text/html")]


def _installer(module, client_cls, routes):
    def install():
        mod = __import__(f"app.platforms.{module}", fromlist=["x"])
        client = getattr(mod, client_cls)(transport=FileTransport(routes))
        client.limiter = RateLimiter(0, name=module)
        mod.set_client(client)
        return client

    def uninstall():
        __import__(f"app.platforms.{module}", fromlist=["x"]).set_client(None)
    return install, uninstall


def _install_fb():
    import json as _json

    from app.db import set_setting
    from app.platforms import facebook as fb
    set_setting(fb.LOCATION_KEY, _json.dumps({"loc": "vienna", "radius": "40", "place": "Wien, Österreich"}))
    return _installer("facebook", "FacebookClient", FACEBOOK_ROUTES)[0]()


CASES = {
    "facebook": {
        "url": "https://www.facebook.com/marketplace/vienna/search?query=fahrrad&maxPrice=100&radius=20",
        "volatile": "https://www.facebook.com/marketplace/vienna/search?query=fahrrad&maxPrice=100&radius=20&ref=search&referral_code=x&exact=false",
        "invalid": ["https://www.facebook.com/marketplace/item/1438848741764802/", "https://example.com/marketplace/vienna/search?query=x",
                    "https://www.facebook.com/marketplace/vienna/search", "https://www.facebook.com/marketplace/category/search?query=x"],
        "ad_url": "https://www.facebook.com/marketplace/item/1438848741764802/?ref=share",
        "ad_id": "1438848741764802",
        "keyword": "rennrad",
        "install": _install_fb,
        "uninstall": _installer("facebook", "FacebookClient", FACEBOOK_ROUTES)[1],
        "expect_ads": 3,
        "detail_id": "1438848741764802",
    },
    "ebay": {
        "url": "https://www.ebay.at/sch/i.html?_nkw=iphone+13&_udlo=100&_udhi=500",
        "volatile": "https://www.ebay.at/sch/i.html?_nkw=iphone+13&_udlo=100&_udhi=500&_pgn=2&_ipg=240&rt=nc&_from=R40",
        "invalid": ["https://www.ebay.at/itm/287646086506", "https://example.com/sch/i.html?_nkw=x",
                    "https://www.ebay.at/sch/i.html", "https://www.ebay.at/b/Handys/9355"],
        "ad_url": "https://www.ebay.at/itm/287646086506?_skw=lego&hash=item42f90af56a",
        "ad_id": "287646086506",
        "keyword": "rennrad",
        "install": _installer("ebay", "EbayClient", EBAY_ROUTES)[0],
        "uninstall": _installer("ebay", "EbayClient", EBAY_ROUTES)[1],
        "expect_ads": 5,
        "detail_id": "920036165580",
    },
    "shpock": {
        "url": "https://www.shpock.com/de-at/results?q=iphone+15+pro&priceFrom=100&priceTo=500",
        "volatile": "https://www.shpock.com/de-at/results?q=iphone+15+pro&priceFrom=100&priceTo=500&sort=x&page=2",
        "invalid": ["https://www.shpock.com/de-at/i/asu6-t2hxq7bDRFz/apple-iphone", "https://example.com/de-at/results?q=x",
                    "https://www.shpock.com/de-at/results", "https://www.shpock.com/de-at/c/handys"],
        "ad_url": "https://www.shpock.com/de-at/i/asu6-t2hxq7bDRFz/apple-iphone-15-pro-128-gb-titan-schwarz",
        "ad_id": "asu6-t2hxq7bDRFz",
        "keyword": "rennrad",
        "install": _installer("shpock", "ShpockClient", SHPOCK_ROUTES)[0],
        "uninstall": _installer("shpock", "ShpockClient", SHPOCK_ROUTES)[1],
        "expect_ads": 4,
        "detail_id": "asu6-t2hxq7bDRFz",
    },
    "vinted": {
        "url": "https://www.vinted.at/catalog?search_text=iphone+15+pro&brand_ids[]=54&price_from=5&price_to=50&currency=EUR&order=newest_first",
        "volatile": "https://www.vinted.at/catalog?search_text=iphone+15+pro&brand_ids[]=54&price_from=5&price_to=50&page=3&time=1791756814&search_id=9&order=relevance",
        "invalid": ["https://www.vinted.at/items/10341874098-handyhullen", "https://example.com/catalog?search_text=x",
                    "https://www.vinted.at/", "https://www.vinted.at/catalog"],
        "ad_url": "https://www.vinted.at/items/10341874098-handyhullen-iphone-16-max-pro-max?referrer=catalog",
        "ad_id": "10341874098",
        "keyword": "rennrad",
        "install": _installer("vinted", "VintedClient", VINTED_ROUTES)[0],
        "uninstall": _installer("vinted", "VintedClient", VINTED_ROUTES)[1],
        "expect_ads": 2,                  # of 4: the iPhone 16 case and the cable lack "15"
        "detail_id": "10341874098",
    },
    "preisjaeger": {
        "url": "https://www.preisjaeger.at/search?q=iphone",
        "volatile": "https://www.preisjaeger.at/search?q=iphone&sortBy=hot&page=2&hide_expired=true",
        "invalid": ["https://www.preisjaeger.at/deals/eke-case-20-der-ecken-schutz-369807", "https://example.com/search?q=x",
                    "https://www.preisjaeger.at/search", "https://www.preisjaeger.at/gruppe/iphone"],
        "ad_url": "https://www.preisjaeger.at/deals/eke-case-20-der-ecken-schutz-furs-apple-iphone-18-369807",
        "ad_id": "369807",
        "keyword": "rennrad",
        "install": _installer("preisjaeger", "PreisjaegerClient", PREISJAEGER_ROUTES)[0],
        "uninstall": _installer("preisjaeger", "PreisjaegerClient", PREISJAEGER_ROUTES)[1],
        "expect_ads": 4,
        "detail_id": None,
    },
    "kleinanzeigen": {
        "url": "https://www.kleinanzeigen.de/s-preis:100:2000/rennrad/k0",
        "volatile": "https://www.kleinanzeigen.de/s-sortierung:preis/s-preis:100:2000/s-seite:2/rennrad/k0",
        "invalid": ["https://www.kleinanzeigen.de/s-anzeige/rennrad/2712345678-217-3331", "https://example.com/s-rad/k0",
                    "https://www.kleinanzeigen.de/", "https://www.kleinanzeigen.de/s-rad/kx"],
        "ad_url": "https://www.kleinanzeigen.de/s-anzeige/rennrad-canyon/2712345678-217-3331",
        "ad_id": "2712345678",
        "keyword": "rennrad",
        "install": _install_ka,
        "uninstall": _uninstall_ka,
        "expect_ads": 4,
        "detail_id": "2712345678",
    },
    "willhaben": {
        "url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad&PRICE_TO=300",
        "volatile": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad&PRICE_TO=300&page=2&sfId=abc&rows=5",
        "invalid": ["https://www.willhaben.at/iad/kaufen-und-verkaufen/d/fahrrad-123456789/", "https://example.com/iad/x",
                    "https://www.willhaben.at/iad/object?adId=123"],
        "ad_url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/d/damenrad-922008415/",
        "ad_id": "922008415",
        "keyword": "rennrad",
        "install": lambda: public_mod.set_public(_willhaben_client()),
        "uninstall": lambda: public_mod.set_public(None),
        "expect_ads": 3,
        "detail_id": "922008415",
    },
}


@pytest.fixture(params=sorted(CASES))
def case(request):
    c = CASES[request.param]
    c["install"]()
    yield request.param, platforms.get(request.param), c
    c["uninstall"]()


def test_registered_and_described(case):
    pid, p, c = case
    assert p.id == pid and p.name and p.host and p.home_country in ("AT", "DE", "CH") and p.hosts
    assert p.can("search_url") and p.matches(c["url"]) and not p.matches("https://example.com/x")
    assert p.status()["id"] == pid and "last_hour" in p.status()


def test_search_url_round_trip(case):
    pid, p, c = case
    spec = p.parse_search_url(c["url"])
    assert isinstance(spec, SearchSpec) and spec.platform == pid and spec.url.startswith("https://")
    assert p.parse_search_url(spec.url) == spec                       # canonical form is stable
    assert p.parse_search_url(c["volatile"]).url == spec.url          # page/rows/session params do not count
    assert SearchSpec.from_json(spec.to_json()) == spec
    assert p.describe(spec)
    for bad in c["invalid"]:
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)


def test_keyword_search_parses_back(case):
    pid, p, c = case
    if not p.can("keyword_search"):
        pytest.skip("no keyword search")
    spec = p.keyword_search(c["keyword"], 10, 100)
    assert spec.keyword == c["keyword"] and spec.price_from == 10 and spec.price_to == 100
    again = p.parse_search_url(spec.url)
    assert again.keyword == c["keyword"] and again.price_from == 10 and again.price_to == 100


async def test_search_returns_listings(case):
    pid, p, c = case
    page = await p.search(p.parse_search_url(c["url"]))
    assert page.rows_found >= len(page.ads) == c["expect_ads"]
    for ad in page.ads:
        assert isinstance(ad, Listing) and ad.ad_id and ad.heading and ad.url.startswith("https://")
        assert ad.platform == pid and ad.ref == (ad.ad_id if pid == "willhaben" else f"{pid}:{ad.ad_id}")
        assert ad.price is None or ad.price >= 0


async def test_detail(case):
    pid, p, c = case
    if not p.can("detail"):
        pytest.skip("no detail")
    d = await p.detail(c["detail_id"])
    assert isinstance(d, AdDetail) and d.ad_id == c["detail_id"] and d.heading and d.platform == pid
    assert d.ref == (d.ad_id if pid == "willhaben" else f"{pid}:{d.ad_id}")


def test_ad_ids(case):
    pid, p, c = case
    assert p.ad_url(c["ad_id"]).startswith("https://") and c["ad_id"] in p.ad_url(c["ad_id"])
    assert p.parse_ad_url(c["ad_url"]) == c["ad_id"]
    assert p.extract_ad_id(c["ad_url"]) == c["ad_id"] and p.extract_ad_id(c["ad_id"]) == c["ad_id"]
    assert p.extract_ad_id("https://example.com/x/123456789") is None
    assert p.classify(c["url"]) in ("search", "detail", "filters", "other")


def test_fixture_files_exist():
    assert (FIXTURES / "search_fahrrad_rows3.json").exists()
