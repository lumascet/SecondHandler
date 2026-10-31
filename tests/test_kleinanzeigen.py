"""kleinanzeigen.de through the app's JSON API: the answers' shape, the website's link grammar, credentials, pacing."""
import base64
import json

import pytest

from app import platforms
from app.config import get_settings
from app.db import set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError, SearchSpec
from app.platforms import kleinanzeigen as ka
from app.platforms.httpclient import FetchResponse
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import euro
from app.services.search_agents import create_agent, run_agent
from tests.conftest import load
from tests.test_platforms_contract import KaTransport


class Queue:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def send(self, method, url, *, params=None, headers=None):
        self.calls.append((url, headers))
        status, body = self.answers.pop(0)
        return FetchResponse(status, {"content-type": "application/json"}, body if isinstance(body, bytes) else json.dumps(body).encode(), url)

    def clear_cookies(self):
        pass

    async def aclose(self):
        pass


@pytest.fixture
def client():
    c = ka.KleinanzeigenClient(transport=KaTransport(), auth="android:test")
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    ka.set_client(c)
    yield c
    ka.set_client(None)


@pytest.fixture
def platform(client):
    platforms.reset()
    set_setting("platform_kleinanzeigen_enabled", "1")
    yield platforms.get("kleinanzeigen")
    platforms.reset()


# ------------------------------------------------------------------ parsing
def test_search_answer_becomes_listings():
    page = ka.parse_ads(load("kleinanzeigen_ads.json"))
    assert page.rows_found == 12345 and [a.ad_id for a in page.ads] == ["2712345678", "2712345679", "2712345680", "2712345681"]
    vb, dealer, free, novalue = page.ads
    assert vb.price == 1250 and vb.price_display == "1.250 € VB" and vb.is_private is True and vb.platform == "kleinanzeigen"
    assert vb.location == "10115 Berlin" and vb.postcode == "10115" and vb.published_at.isoformat() == "2026-10-10T07:15:30+00:00"
    assert vb.url == "https://www.kleinanzeigen.de/s-anzeige/rennrad-canyon-endurace/2712345678-217-3331"
    assert vb.image_url.endswith("$_59.JPG") and vb.category_ids == ["217"] and vb.is_bumped is None and vb.ref == "kleinanzeigen:2712345678"
    assert dealer.is_private is False and dealer.price_display == "60 €" and dealer.body == "Gewerblicher Verkauf mit Rechnung & Garantie."
    assert free.price == 0 and free.price_display == "Zu verschenken" and free.image_url is None
    assert novalue.price is None and novalue.price_display == "VB"
    assert ka.parse_ads({}).rows_found == 0 and ka.parse_ads({"x": 1}).ads == []
    single = {ka.ADS_NS: {"value": {"paging": {"numFound": "1"}, "ad": load("kleinanzeigen_ads.json")[ka.ADS_NS]["value"]["ad"][0]}}}
    assert len(ka.parse_ads(single).ads) == 1                            # one result comes as a dict, not a list


def test_ad_answer_becomes_detail():
    d = ka.parse_ad(load("kleinanzeigen_ad.json"))
    assert d.ad_id == "2712345678" and d.heading == "Rennrad Canyon Endurace" and d.seller_name == "Erika"
    assert d.description.startswith("Canyon Endurace CF 8") and "\n" in d.description and len(d.images) == 3
    assert d.is_vb is True and d.price_display == "1.250 € VB" and d.category_path == ["Fahrräder & Zubehör"]
    assert euro(12.5) == "12,50 €" and euro(1234567) == "1.234.567 €"
    assert d.platform == "kleinanzeigen" and d.ref == "kleinanzeigen:2712345678" and d.is_private is True
    assert ka.parse_ad({}) is None
    assert ka.parse_locations(load("kleinanzeigen_locations.json")) == [(3331, "Berlin"), (3360, "Berlin Mitte")]


# ------------------------------------------------------------------ links
@pytest.mark.parametrize("url,keyword,price_from,price_to,category,location,extra", [
    ("https://www.kleinanzeigen.de/s-fahrrad/k0", "fahrrad", None, None, None, None, {}),
    ("https://www.kleinanzeigen.de/s-preis:10:100/fahrrad/k0", "fahrrad", 10, 100, None, None, {}),
    ("https://www.kleinanzeigen.de/s-preis::300/rennrad-carbon/k0", "rennrad carbon", None, 300, None, None, {}),
    ("https://www.kleinanzeigen.de/s-berlin/fahrrad/k0l3331", "fahrrad", None, None, None, "berlin", {"location_id": 3331}),
    ("https://www.kleinanzeigen.de/s-fahrraeder/berlin/rennrad/k0c217l3331r20", "rennrad", None, None, "217", "berlin",
     {"category_slug": "fahrraeder", "location_id": 3331, "radius_km": 20}),
    ("https://www.kleinanzeigen.de/s-fahrraeder/c217", None, None, None, "217", None, {"category_slug": "fahrraeder"}),
    ("https://www.kleinanzeigen.de/s-anzeige:angebote/s-seite:3/fahrrad/k0?x=1", "fahrrad", None, None, None, None, {}),
    ("https://www.kleinanzeigen.de/s-berlin/k0l3331", None, None, None, None, "berlin", {"location_id": 3331}),
])
def test_search_links(url, keyword, price_from, price_to, category, location, extra):
    p = ka.KleinanzeigenPlatform()
    spec = p.parse_search_url(url)
    assert (spec.keyword, spec.price_from, spec.price_to, spec.category, spec.location, spec.extra) == \
        (keyword, price_from, price_to, category, location, extra)
    assert p.parse_search_url(spec.url) == spec and spec.url.startswith("https://www.kleinanzeigen.de/s-")


def test_bad_links_and_keyword_search():
    p = ka.KleinanzeigenPlatform()
    for bad in ("https://www.kleinanzeigen.de/s-preis:abc:1/rad/k0", "https://www.kleinanzeigen.de/s-anzeige/x/1-2-3",
                "https://www.kleinanzeigen.de/m-nachrichten.html", "ftp://www.kleinanzeigen.de/s-rad/k0"):
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)
    spec = p.keyword_search("Rennrad Größe M", None, 500, sort="price_asc")
    assert spec.url == "https://www.kleinanzeigen.de/s-preis::500/rennrad-groesse-m/k0" and spec.sort == "price_asc"
    assert p.describe(spec) == "Rennrad Größe M" and p.describe(SearchSpec(platform="kleinanzeigen", url="x", location="berlin")) == "Berlin"
    assert p.ad_url("1") == "https://www.kleinanzeigen.de/s-anzeige/1"
    assert p.parse_ad_url("https://www.kleinanzeigen.de/s-anzeige/rennrad/2712345678-217-3331") == "2712345678"
    assert p.parse_ad_url("https://api.kleinanzeigen.de/api/ads/2712345678.json") == "2712345678"
    assert p.extract_ad_id("2712345678") == "2712345678" and p.parse_ad_url("https://www.kleinanzeigen.de/s-rad/k0") is None
    assert ka.classify("https://api.kleinanzeigen.de/api/ads.json?q=x") == "search"
    assert ka.classify("https://api.kleinanzeigen.de/api/ads/1.json") == "detail"


# ------------------------------------------------------------------ the client
async def test_search_sends_the_apps_headers_and_params(platform, client):
    spec = platform.parse_search_url("https://www.kleinanzeigen.de/s-preis:100:2000/berlin/rennrad/k0")
    page = await platform.search(spec, rows=20)
    assert page.rows_found == 12345 and page.title == "rennrad"
    urls = [u for u, _ in client._transport.calls]
    assert urls[0].startswith("https://api.kleinanzeigen.de/api/locations.json?q=berlin")   # the place, once
    search = urls[1]
    assert search.startswith("https://api.kleinanzeigen.de/api/ads.json?") and "q=rennrad" in search
    assert "minPrice=100" in search and "maxPrice=2000" in search and "locationId=3331" in search
    assert "sortType=DATE_DESCENDING" in search and "adType=OFFERED" in search and "size=20" in search
    headers = client._transport.calls[1][1]
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"android:test").decode()
    assert headers["X-EBAYK-APP"] == client.app_id and headers["X-ECG-USER-VERSION"] == ka.APP_VERSION
    await platform.search(spec)
    assert len([u for u in (u for u, _ in client._transport.calls) if "locations" in u]) == 1   # cached place


async def test_credentials_setting_and_errors(platform, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "kleinanzeigen_api_auth", "")      # no env for the setting tests
    set_setting("kleinanzeigen_api_auth", "me:secret")
    assert ka.api_auth() == ("me:secret", "settings")
    c = ka.KleinanzeigenClient(transport=Queue([(401, b""), (429, b""), (500, b"")]))
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    assert c.headers["Authorization"] == "Basic " + base64.b64encode(b"me:secret").decode()
    with pytest.raises(PlatformError, match="Settings"):
        await c.search_ads({"q": "x"})
    assert c.limiter.cooling_down_s == 0                                   # rotated credentials are not a block
    with pytest.raises(Blocked):
        await c.search_ads({"q": "x"})
    assert c.limiter.cooling_down_s > 0
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    with pytest.raises(PlatformError, match="HTTP 500"):
        await c.search_ads({"q": "x"})
    # KLEINANZEIGEN_API_AUTH in .env wins over the setting
    monkeypatch.setattr(get_settings(), "kleinanzeigen_api_auth", "env:key")
    assert ka.api_auth() == ("env:key", "env") and platform.status()["auth_source"] == "env"
    monkeypatch.setattr(get_settings(), "kleinanzeigen_api_auth", "")
    # none bundled: without env or setting the API is off and get_client says so
    set_setting("kleinanzeigen_api_auth", "")
    assert ka.api_auth() == ("", "") and platform.status()["auth_source"] == ""
    ka.set_client(None)
    with pytest.raises(PlatformError, match="credentials"):
        ka.get_client()


async def test_detail_404_and_agent_end_to_end(platform, client):
    assert await platform.detail("999") is None
    d = await platform.detail("2712345678")
    assert d.heading == "Rennrad Canyon Endurace"
    a = create_agent("https://www.kleinanzeigen.de/s-preis::2000/rennrad/k0")
    assert a.platform == "kleinanzeigen" and a.name == "rennrad" and a.interval_s >= 180
    res = await run_agent(a.id)
    assert res.baseline and res.rows_found == 12345 and res.error is None
    from app.db import Hit, session
    from sqlmodel import select
    with session() as db:
        hits = list(db.exec(select(Hit).where(Hit.agent_id == a.id)))
    assert len(hits) == 4 and {h.platform for h in hits} == {"kleinanzeigen"}
    assert all(h.url.startswith("https://www.kleinanzeigen.de/s-anzeige/") for h in hits)


def test_registry_knows_kleinanzeigen():
    platforms.reset()
    assert platforms.get("kleinanzeigen").name == "Kleinanzeigen"
    assert not platforms.enabled("kleinanzeigen")                          # off until switched on in Settings
    with pytest.raises(InvalidSearchUrl, match="switched off"):
        platforms.for_url("https://www.kleinanzeigen.de/s-rad/k0")
    set_setting("platform_kleinanzeigen_enabled", "1")
    assert platforms.for_url("https://www.kleinanzeigen.de/s-rad/k0").id == "kleinanzeigen"
    assert [p.id for p in platforms.all()] == ["willhaben", "kleinanzeigen"]
    assert platforms.for_host("img.kleinanzeigen.de").id == "kleinanzeigen"
    assert platforms.names() == "willhaben, Kleinanzeigen"
    assert get_settings().platforms_enabled == "willhaben,kleinanzeigen,shpock,vinted,preisjaeger,ebay,facebook"


def test_city_with_district_is_one_line():
    ad = load("kleinanzeigen_ads.json")[ka.ADS_NS]["value"]["ad"][0]
    ad["ad-address"]["state"] = {"value": "Hamburg\tWaltershof"}
    assert ka.parse_listing(ad).location == "10115 Hamburg, Waltershof"
