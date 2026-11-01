"""preisjaeger.at deals from the app's signed REST API: the deal list, the empty answer, price limits, OAuth
signing, one deal's details, links and errors."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app import platforms
from app.db import Hit, session, set_setting
from app.platforms import Blocked, InvalidSearchUrl, PlatformError
from app.platforms import preisjaeger as pj
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import from_epoch
from app.services.search_agents import create_agent, run_agent
from tests.conftest import FIXTURES
from tests.test_kleinanzeigen import Queue
from tests.test_platforms_contract import PREISJAEGER_ROUTES, FileTransport


def _body(name="preisjaeger_search.json"):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def client():
    c = pj.PreisjaegerClient(transport=FileTransport(PREISJAEGER_ROUTES))
    c.limiter = RateLimiter(0, name="Preisjäger")
    pj.set_client(c)
    yield c
    pj.set_client(None)


@pytest.fixture
def platform(client):
    platforms.reset()
    set_setting("platform_preisjaeger_enabled", "1")
    yield platforms.get("preisjaeger")
    platforms.reset()


def test_deal_threads_become_listings():
    page = pj.parse_search(_body())
    assert [a.ad_id for a in page.ads] == ["369807", "369875", "369687", "369564"] and page.rows_found == 4
    first, voucher = page.ads[0], page.ads[3]
    assert first.price == 19.92 and first.price_display == "19,92 €" and first.old_price == 28.8
    assert first.location == "EKE Case" and first.is_private is None and first.ref == "preisjaeger:369807"
    assert first.url.startswith("https://www.preisjaeger.at/deals/eke-case-20-der-ecken") and first.url.endswith("-369807")
    assert first.image_url == "https://static.preisjaeger.at/threads/raw/6OsLn/369807_1/re/300x300/qt/60/369807_1.jpg"
    assert first.published_at == from_epoch(1791200337) and first.body.startswith("420°") and "Elektronik" in first.body
    assert voucher.price is None and voucher.old_price is None and "Voucher code FinalCall" in voucher.body
    assert voucher.price_display == "−30 %" and voucher.image_url is None


def test_expired_deals_are_dropped():
    assert "369500" not in [a.ad_id for a in pj.parse_search(_body()).ads]      # expired: true


def test_empty_answer_lists_no_deals():
    assert pj.parse_search('{"data": [], "additional_data": {}}').ads == []
    for bad in ('{"data": null}', "<html><body>Just a moment…</body></html>"):
        with pytest.raises(PlatformError, match="result list"):
            pj.parse_search(bad)


def test_price_limits_are_applied_here():
    assert [a.price for a in pj.parse_search(_body(), 15, 30).ads] == [19.92]
    assert [a.ad_id for a in pj.parse_search(_body(), None, 12).ads] == ["369875"]      # no price: out of a limited search


def test_links():
    p = pj.PreisjaegerPlatform()
    spec = p.parse_search_url("https://www.preisjaeger.at/search?q=Lego%20Technic&priceTo=80&sortBy=hot")
    assert spec.keyword == "Lego Technic" and spec.price_to == 80
    assert spec.url == "https://www.preisjaeger.at/search?q=Lego+Technic&sortBy=new&hide_expired=true&priceTo=80"
    for bad in ("https://www.preisjaeger.at/search?q=", "https://www.preisjaeger.at/share-deal/369807",
                "https://www.preisjaeger.at/search?q=x&priceFrom=viel"):
        with pytest.raises(InvalidSearchUrl):
            p.parse_search_url(bad)
    assert p.parse_ad_url("https://www.preisjaeger.at/share-deal/369807") == "369807"
    assert p.parse_ad_url("https://www.preisjaeger.at/deals/esr-hulle-1615-pro-max-366499?x=1") == "366499"
    assert pj.classify("https://www.preisjaeger.at/search?q=x") == "search" and not p.can("detail")


async def test_request_is_signed_json(platform, client):
    await platform.search(platform.keyword_search("iphone"), rows=25)
    url, headers = client._transport.calls[-1]
    assert "/rest_api/v2/thread?" in url
    qs = parse_qs(urlsplit(url).query)
    assert json.loads(qs["criteria"][0])["query"] == "iphone"            # criteria survives the single URL-encoding
    assert qs["history_item_needed"] == ["false"] and qs["limit"] == ["25"]
    assert "oauth_" not in url                                           # oauth parameters live in the header, not the URL
    assert headers["Authorization"].startswith('OAuth oauth_consumer_key="')
    assert 'oauth_signature="' in headers["Authorization"] and headers["User-Agent"].startswith("com.tippingcanoe.preisjaeger")


async def test_block_and_errors():
    c = pj.PreisjaegerClient(transport=Queue([(403, b""), (503, b"")]))
    c.limiter = RateLimiter(0, name="Preisjäger")
    with pytest.raises(Blocked):
        await c.search_page({"query": "x"}, None, None)
    c.limiter = RateLimiter(0, name="Preisjäger")
    with pytest.raises(PlatformError, match="HTTP 503"):
        await c.search_page({"query": "x"}, None, None)


def test_bundled_credentials_are_used_by_default():
    key, secret, src = pj.oauth_creds()
    assert key and secret and src == "bundled"


async def test_agent_end_to_end(platform, client):
    a = create_agent("https://www.preisjaeger.at/search?q=iphone")
    assert a.platform == "preisjaeger" and a.name == "iphone" and a.interval_s >= 300
    res = await run_agent(a.id)
    assert res.baseline and res.error is None
    with session() as db:
        hits = {h.ad_id: h for h in db.exec(select(Hit).where(Hit.agent_id == a.id))}
    assert set(hits) == {"369807", "369875", "369687", "369564"} and hits["369807"].location == "EKE Case"
