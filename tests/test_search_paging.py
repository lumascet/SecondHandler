"""Paging of the sites' keyword searches (Platform.search(page=k)): page 1 asks exactly as before, page k the k-th
result page of the same search; PAGE_SIZE_MAX and CAN_PAGE tell the caller how far one request goes."""
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from app import platforms
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient, RateLimiter
from tests.conftest import load
from tests.test_platforms_contract import (EBAY_ROUTES, PREISJAEGER_ROUTES, SHPOCK_ROUTES,
                                           VINTED_ROUTES, KaTransport, _install_fb, _installer)


def _query(url: str, params=None) -> dict[str, str]:
    """The query of a sent request (in the URL, or in the transport's params)."""
    q = dict(parse_qsl(urlsplit(url).query))
    q.update({k: str(v) for k, v in (params or {}).items() if v is not None})
    return q


class Recording:
    """Wraps a fake transport and keeps the params of every request, which FileTransport does not record."""

    def __init__(self, inner):
        self.inner, self.sent = inner, []

    async def send(self, method, url, *, params=None, headers=None):
        self.sent.append(_query(url, params))
        return await self.inner.send(method, url, params=params, headers=headers)

    def clear_cookies(self):
        pass

    async def aclose(self):
        pass


def _sent(client) -> list[dict[str, str]]:
    return client._transport.sent


def _wrap(module: str, cls: str, routes) -> object:
    install, _ = _installer(module, cls, routes)
    client = install()
    client._transport = Recording(client._transport)
    return client


@pytest.fixture
def sites():
    yield
    for module in ("ebay", "shpock", "vinted", "preisjaeger", "kleinanzeigen", "facebook"):
        __import__(f"app.platforms.{module}", fromlist=["x"]).set_client(None)
    public_mod.set_public(None)


def test_every_site_says_how_far_it_pages():
    expect = {"willhaben": (200, True), "kleinanzeigen": (50, True), "ebay": (60, True), "shpock": (40, True),
              "vinted": (96, True), "preisjaeger": (30, False), "facebook": (24, False)}
    for pid, (size, can) in expect.items():
        p = platforms.get(pid)
        assert (p.PAGE_SIZE_MAX, p.CAN_PAGE) == (size, can), pid


async def test_willhaben_pages_with_page(sites):
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=load("search_fahrrad_rows3.json"))
    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    public_mod.set_public(c)
    p = platforms.get("willhaben")
    spec = p.keyword_search("fahrrad")
    await p.search(spec, rows=100)
    await p.search(spec, rows=100, page=3)
    first, third = _query(urls[0]), _query(urls[1])
    assert "page" not in first and first["rows"] == "100"
    assert third["page"] == "3" and third["rows"] == "100"
    await p.search(spec, rows=1000)                                     # one request never asks for more than a page
    assert _query(urls[2])["rows"] == "200"


async def test_kleinanzeigen_counts_pages_from_zero(sites):
    from app.platforms import kleinanzeigen as ka
    t = Recording(KaTransport())
    c = ka.KleinanzeigenClient(transport=t)
    c.limiter = RateLimiter(0, name="Kleinanzeigen")
    ka.set_client(c)
    p = platforms.get("kleinanzeigen")
    spec = p.keyword_search("fahrrad")
    await p.search(spec, rows=30)
    await p.search(spec, rows=50, page=2)
    assert t.sent[0]["page"] == "0" and t.sent[0]["size"] == "30"
    assert t.sent[1]["page"] == "1" and t.sent[1]["size"] == "50"


@pytest.mark.parametrize("pid,module,cls,routes,param,expect", [
    ("ebay", "ebay", "EbayClient", EBAY_ROUTES, "_pgn", "2"),
    ("vinted", "vinted", "VintedClient", VINTED_ROUTES, "page", "2"),
])
async def test_page_two_asks_for_the_next_page(sites, pid, module, cls, routes, param, expect):
    client = _wrap(module, cls, routes)
    p = platforms.get(pid)
    spec = p.keyword_search("rennrad")
    await p.search(spec)
    await p.search(spec, page=2)
    sent = _sent(client)
    assert param not in sent[0]                                         # page 1 as before
    assert sent[1].get(param) == expect
    assert {k: v for k, v in sent[1].items() if k != param} == sent[0]  # the same search otherwise


async def test_shpock_pages_with_its_offset(sites):
    import json
    client = _wrap("shpock", "ShpockClient", SHPOCK_ROUTES)
    p = platforms.get("shpock")
    spec = p.keyword_search("rennrad")
    await p.search(spec, rows=40)
    await p.search(spec, rows=40, page=3)
    first, third = (json.loads(s["variables"])["pagination"] for s in _sent(client))
    assert first == {"limit": 40}
    assert third == {"limit": 40, "offset": 80}


async def test_preisjaeger_has_one_page(sites):
    client = _wrap("preisjaeger", "PreisjaegerClient", PREISJAEGER_ROUTES)
    p = platforms.get("preisjaeger")
    spec = p.keyword_search("rennrad")
    assert (await p.search(spec)).ads                                    # page 1 fetches
    assert (await p.search(spec, page=2)).ads == []                      # the deal feed is a single page
    assert len(_sent(client)) == 1                                       # no request for a page that is not there


async def test_facebook_has_one_page(sites):
    client = _install_fb()
    client._transport = Recording(client._transport)
    p = platforms.get("facebook")
    spec = p.parse_search_url("https://www.facebook.com/marketplace/vienna/search?query=fahrrad")
    assert (await p.search(spec, page=2)).ads == []                      # no request for a page that is not there
    assert not _sent(client)
