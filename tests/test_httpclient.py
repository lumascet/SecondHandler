"""The shared platform HTTP client: pacing, cache, blocks, network errors, recording per platform, transports."""
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from sqlmodel import select

from app.db import WhRecording, session
from app.platforms import Blocked, PlatformError
from app.platforms.base import NotTheExit
from app.platforms.httpclient import (CurlTransport, ExitMoved, FetchResponse, HttpPlatformClient, HttpxTransport,
                                      Trial, TransportError, trial)
from app.platforms.ratelimit import RateLimiter
from app.services import access_stats, timeline
from app.willhaben import access_log, replay
from app.config import get_settings


class FakeTransport:
    def __init__(self, answers):
        self.answers, self.calls, self.cleared = list(answers), [], 0

    async def send(self, method, url, *, params=None, headers=None):
        self.calls.append((method, url, params, headers))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, body, *headers = a          # (status, body) answers JSON; a third item gives the headers
        return FetchResponse(status, headers[0] if headers else {"content-type": "application/json"}, body, url)

    def clear_cookies(self):
        self.cleared += 1

    async def aclose(self):
        pass


def _client(answers, **kw) -> tuple[HttpPlatformClient, FakeTransport]:
    tr = FakeTransport(answers)
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport=tr, **kw)
    return c, tr


async def test_get_counts_records_and_caches():
    timeline.set_recording(timeline=True, willhaben=True)
    c, tr = _client([(200, b'{"a": 1}'), (404, b"")])
    r = await c._get("https://fake.example/api?q=rad", params={"page": 1}, headers={"accept": "application/json"}, cache_s=60)
    assert r.status_code == 200 and r.status == 200 and r.json() == {"a": 1} and r.text == '{"a": 1}'
    again = await c._get("https://fake.example/api?q=rad", params={"page": 1}, headers={"accept": "application/json"}, cache_s=60)
    assert again is r and len(tr.calls) == 1                           # the second one came from the cache
    gone = await c._get("https://fake.example/ad/1", cache_s=60)
    assert gone.status_code == 404
    assert access_stats.last_hour(platform="fake") == 2 and access_stats.last_hour() == 0
    with session() as db:
        recs = list(db.exec(select(WhRecording).order_by(WhRecording.id)))
    assert [(x.platform, x.cached, x.status) for x in recs] == [("fake", False, 200), ("fake", True, 200), ("fake", False, 404)]
    assert recs[0].url == "https://fake.example/api?q=rad&page=1" == tr.calls[0][1]
    assert tr.calls[0][3] == {"accept": "application/json"}


async def test_blocks_errors_and_status_hook():
    c, tr = _client([(429, b""), TransportError("tls handshake failed"), (401, b"")])
    with pytest.raises(Blocked) as info:
        await c._get("https://fake.example/x")
    assert info.value.sent and tr.cleared == 1 and c.limiter.cooling_down_s > 0 and "Fake responded 429" in str(info.value)
    c.limiter = RateLimiter(0, name="Fake")
    with pytest.raises(PlatformError, match="tls handshake"):
        await c._get("https://fake.example/x")

    class Picky(HttpPlatformClient):
        def _check_status(self, r):
            if r.status_code == 401:
                raise PlatformError("credentials rotated")
            super()._check_status(r)

    picky = Picky("fake", RateLimiter(0, name="Fake"), transport=tr)
    with pytest.raises(PlatformError, match="credentials rotated"):
        await picky._get("https://fake.example/x")
    assert tr.cleared == 1                                                # a 401 is not a block: no new visitor


async def test_jar_rotation_and_classify():
    c, tr = _client([(200, b"{}"), (200, b"{}")])
    c.session_s = 0
    await c._get("https://fake.example/x")
    await c._get("https://fake.example/x")
    assert tr.cleared >= 1 and c.classify("https://www.willhaben.at/iad/object?adId=1") == "detail"


async def test_replay_answers_before_the_transport(monkeypatch):
    timeline.set_recording(timeline=True, willhaben=True)
    c, tr = _client([(200, b'{"live": 1}')])
    await c._get("https://fake.example/api?q=rad")
    monkeypatch.setattr(get_settings(), "wh_replay", True)
    r = await c._get("https://fake.example/api?q=rad&_=123")
    assert r.json() == {"live": 1} and r.extensions["wh_replayed"] and len(tr.calls) == 1
    assert replay.find("GET", "https://fake.example/api?q=rad").platform == "fake"


async def test_httpx_transport_and_curl_transport_build():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-test"))
        return httpx.Response(200, text="ok", headers={"set-cookie": "a=1"})

    tr = HttpxTransport({"x-test": "1"}, httpx.MockTransport(handler))
    r = await tr.send("GET", "https://fake.example/x", params={"a": "b"})
    assert r.status_code == 200 and r.text == "ok" and seen == ["1"] and tr.client.cookies
    tr.clear_cookies()
    assert not tr.client.cookies
    await tr.aclose()
    curl = CurlTransport({"x-test": "1"})
    curl.clear_cookies()
    await curl.aclose()


async def test_public_client_is_a_platform_client():
    from app.willhaben.public import PublicClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rowsFound": 0, "advertSummaryList": {}})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    page = await c.search("https://www.willhaben.at/webapi/ad-search/search/atz/seo/x?keyword=rad")
    assert page.rows_found == 0 and c.platform == "willhaben" and c._client is not None
    assert access_log.pending() and access_stats.last_hour() == 1
    await c.aclose()


# ------------------------------------------------------------------ through the VPN (app/platforms/vpn.py)
class FakeTunnel:
    """Hands out routes http://exit1, http://exit2, ... (each the site's server); failed() moves on, as a switch-over
    would. `dead`: no server works (fail closed). `running`: routes of other servers still up (the search's).
    `confirm`: refusals in a row before one counts (vpn.CONFIRM; 1 here unless a test asks for more)."""

    def __init__(self, dead=False, confirm=1):
        from app.platforms.vpn import Route

        self.Route, self.dead, self.n, self.route, self.failures, self.details = Route, dead, 0, None, [], []
        self.odd_site, self.running, self.held = False, set(), []
        self.confirm, self.strikes, self.refusals, self.accepted = confirm, 0, [], []

    async def refused(self, route, reason, detail=""):
        self.refusals.append((route.proxy, reason))
        self.strikes += 1
        if self.strikes < self.confirm:
            return False
        self.strikes = 0
        return await self.failed(route, reason, detail)

    def answered(self, route):
        self.strikes = 0
        self.accepted.append(route.proxy)

    async def ensure(self):
        if self.dead:
            raise PlatformError("No working VPN server for Fake right now")
        if self.route is None:
            self.n += 1
            self.route = self.Route(f"http://exit{self.n}", self.n)
        return self.route

    async def failed(self, route, reason, detail=""):
        self.failures.append((route.proxy, reason))
        self.details.append(detail)
        if self.route is not None and route.gen == self.route.gen:
            self.route = None
            return True
        return False

    def current(self):
        return self.route

    def alive(self, route):
        return route == self.route or route in self.running

    @contextmanager
    def hold(self, route):
        self.held.append(route)
        try:
            yield
        finally:
            self.held.remove(route)

    def odd(self):
        return self.odd_site

def _vpn_client(answers_by_exit: dict, tunnel: FakeTunnel, cls=HttpPlatformClient, **kw):
    transports = {proxy: FakeTransport(answers) for proxy, answers in answers_by_exit.items()}
    built = []

    def factory(proxy):
        built.append(proxy)
        return transports[proxy]

    c = cls("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=factory, tunnel=lambda: tunnel, **kw)
    return c, transports, built


async def test_a_refused_server_moves_on_without_pausing_the_site():
    tunnel = FakeTunnel()
    c, transports, built = _vpn_client({"http://exit1": [(403, b"cloudflare")], "http://exit2": [(200, b"{}")]}, tunnel)
    before = access_stats.last_hour(platform="fake")
    r = await c._get("https://fake.example/api")
    assert r.status_code == 200 and c.proxy == "http://exit2"
    assert built == ["http://exit1", "http://exit2"] and tunnel.failures == [("http://exit1", "blocked")]
    assert tunnel.details == ["HTTP 403"]
    assert c.limiter.cooling_down_s == 0          # the shared limiter (logged-in session too) is not paused
    assert access_stats.last_hour(platform="fake") - before == 2        # both requests count, the 403 as blocked


async def test_three_refusals_give_up_for_this_request():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({f"http://exit{i}": [(429 if i == 2 else 403, b"")] for i in (1, 2, 3)}, tunnel)
    with pytest.raises(Blocked) as info:
        await c._get("https://fake.example/api")
    assert info.value.sent and "refused the VPN server" in str(info.value) and len(built) == 3
    assert [f[1] for f in tunnel.failures] == ["blocked", "rate", "blocked"] and c.limiter.cooling_down_s == 0


async def test_a_refusal_is_asked_again_through_the_same_server_with_the_same_visitor_until_it_counts():
    tunnel = FakeTunnel(confirm=3)
    c, transports, built = _vpn_client({"http://exit1": [(403, b""), (403, b""), (200, b"{}")]}, tunnel)
    r = await c._get("https://fake.example/api")
    assert r.status_code == 200 and built == ["http://exit1"]           # one server, one visitor (its cookies kept)
    assert tunnel.failures == [] and len(tunnel.refusals) == 2 and tunnel.accepted == ["http://exit1"]
    assert c.limiter.cooling_down_s == 0


async def test_refusals_in_a_row_move_on_to_the_next_server_with_a_new_visitor():
    tunnel = FakeTunnel(confirm=3)
    c, transports, built = _vpn_client({"http://exit1": [(403, b"")] * 3, "http://exit2": [(200, b"{}")]}, tunnel)
    r = await c._get("https://fake.example/api")
    assert r.status_code == 200 and built == ["http://exit1", "http://exit2"]
    assert tunnel.failures == [("http://exit1", "blocked")] and len(transports["http://exit1"].calls) == 3


async def test_no_server_means_no_request_at_all():
    c, _, built = _vpn_client({}, FakeTunnel(dead=True))
    with pytest.raises(PlatformError, match="No working VPN server"):
        await c._get("https://fake.example/api")
    assert built == []                            # never a direct transport (factory(None))


async def test_an_unreachable_server_moves_on():
    tunnel = FakeTunnel()
    c, _, _ = _vpn_client({"http://exit1": [TransportError("proxy connect refused")], "http://exit2": [(200, b"{}")]}, tunnel)
    assert (await c._get("https://fake.example/api")).status_code == 200
    assert tunnel.failures == [("http://exit1", "unreachable")]


async def test_cached_answers_do_not_touch_the_tunnel():
    tunnel = FakeTunnel()
    c, transports, _ = _vpn_client({"http://exit1": [(200, b"{}")]}, tunnel)
    await c._get("https://fake.example/api", cache_s=60)
    tunnel.dead = True
    assert (await c._get("https://fake.example/api", cache_s=60)).status_code == 200


async def test_the_vpn_follows_the_switch_without_a_new_client(monkeypatch):
    from app.platforms import vpn

    tunnel = FakeTunnel()
    monkeypatch.setattr(vpn, "tunnel_for", lambda pid: tunnel if vpn.enabled(pid) else None)
    transports = {None: FakeTransport([(200, b"{}")]), "http://exit1": FakeTransport([(200, b"{}")])}
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), transport_factory=lambda p: transports[p])
    await c._get("https://fake.example/a")
    assert c.proxy is None and len(transports[None].calls) == 1
    vpn.set_enabled("fake", True)
    await c._get("https://fake.example/b")
    assert c.proxy == "http://exit1" and len(transports["http://exit1"].calls) == 1


async def test_kleinanzeigen_tells_an_exit_block_from_rotated_credentials():
    from app.platforms.kleinanzeigen import KleinanzeigenClient

    ka = KleinanzeigenClient(transport=FakeTransport([]))
    html = FetchResponse(403, {"Content-Type": "text/html; charset=utf-8", "Server": "AkamaiGHost"}, b"<html>", "u")
    creds = FetchResponse(403, {"content-type": "application/json"}, b"{}", "u")
    assert ka._exit_blocked(html) and not ka._exit_blocked(creds)
    assert ka._exit_blocked(FetchResponse(429, {}, b"", "u")) and not ka._exit_blocked(FetchResponse(401, {}, b"", "u"))

    tunnel = FakeTunnel()
    ka._factory = lambda proxy: FakeTransport([(403, b'{"error": "unauthorized"}')])
    ka._tunnel, ka._transport = (lambda: tunnel), None
    with pytest.raises(PlatformError, match="app credentials") as info:   # through the VPN too: no rotation
        await ka._get("https://api.kleinanzeigen.de/api/ads.json")
    assert tunnel.failures == [] and isinstance(info.value, NotTheExit)    # the VPN test does not move on either


async def test_ebay_warms_up_on_an_exit_instead_of_leaving_it():
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    page = (FIXTURES / "ebay_search.html").read_bytes()
    exits = {"http://exit1": FakeTransport([(403, b""), (200, page), TransportError("down")]),
             "http://exit2": FakeTransport([(403, b""), (200, page), (403, b"")]),
             "http://exit3": FakeTransport([(403, b""), (200, page)])}
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    assert len((await c.search_page({"_nkw": "iphone"})).ads) == 5          # the warm-up 403 keeps the server
    assert tunnel.failures == [] and len(exits["http://exit1"].calls) == 2
    assert len((await c.search_page({"_nkw": "ipad"})).ads) == 5            # exit1 gone: exit2 warms up anew
    assert tunnel.failures == [("http://exit1", "unreachable")] and len(exits["http://exit2"].calls) == 2
    assert len((await c.search_page({"_nkw": "ipod"})).ads) == 5            # warm: a 403 refuses the server
    assert tunnel.failures[1:] == [("http://exit2", "blocked")] and len(exits["http://exit3"].calls) == 2


async def test_a_bot_page_through_the_vpn_refuses_the_server():
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    html = {"content-type": "text/html"}
    page = (FIXTURES / "ebay_search.html").read_bytes()
    exits = {"http://exit1": FakeTransport([(200, b"<html>Pardon Our Interruption</html>", html)]),
             "http://exit2": FakeTransport([(503, b"<html>", {**html, "cf-mitigated": "challenge"})]),
             "http://exit3": FakeTransport([(200, page, html)])}
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    assert len((await c.search_page({"_nkw": "iphone"})).ads) == 5          # eBay's page in place of the results
    assert tunnel.failures == [("http://exit1", "botcheck"), ("http://exit2", "botcheck")]   # and a challenge
    assert c.limiter.cooling_down_s == 0 and c.proxy == "http://exit3"


async def test_bot_pages_of_a_site_the_last_shuffle_found_changed_refuse_no_server():
    tunnel = FakeTunnel()
    tunnel.odd_site = True
    c, _, built = _vpn_client({"http://exit1": [(200, b"<html>new layout</html>", {"content-type": "text/html"})]},
                              tunnel)
    r = await c._get("https://fake.example/api", valid=lambda r: r.text.startswith("{"))
    assert r.status_code == 200 and tunnel.failures == [] and built == ["http://exit1"]   # the adapter decides


async def test_a_network_error_of_an_optional_host_keeps_the_server():
    from app.platforms import ebay

    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    exits = {f"http://exit{i}": FakeTransport([TransportError("timed out")] * (2 if i == 1 else 1)) for i in (1, 2, 3)}
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    with pytest.raises(PlatformError, match="Network error through the VPN"):
        await c._get("https://itm.ebaydesc.com/itmdesc/1")       # the seller's description: optional
    assert tunnel.failures == [] and len(exits["http://exit1"].calls) == 1
    with pytest.raises(PlatformError):
        await c._get("https://www.ebay.at/itm/1")
    assert tunnel.failures[0] == ("http://exit1", "unreachable")  # the site itself: the server is refused


async def test_a_new_connection_gets_a_new_transport_even_on_an_old_port():
    from app.platforms.vpn import Route

    tunnel = FakeTunnel()
    c, transports, built = _vpn_client({"http://exit1": [(200, b"{}"), (200, b"{}")]}, tunnel)
    tunnel.route = Route("http://exit1", 1, "at-vie-wg-001")
    await c._get("https://fake.example/a")
    tunnel.route = Route("http://exit1", 2, "at-vie-wg-002")    # another server got the port the old one had
    await c._get("https://fake.example/b")
    assert built == ["http://exit1", "http://exit1"]             # new connections and a new visitor


async def test_a_direct_request_queued_when_the_vpn_is_switched_on_goes_through_it(monkeypatch):
    from app.platforms import vpn

    tunnel = FakeTunnel()
    monkeypatch.setattr(vpn, "tunnel_for", lambda pid: tunnel if vpn.enabled(pid) else None)
    sent = []

    class T(FakeTransport):
        def __init__(self, proxy):
            super().__init__([(200, b"{}")])
            self.proxy = proxy

        async def send(self, method, url, *, params=None, headers=None):
            sent.append(self.proxy)
            return await super().send(method, url, params=params, headers=headers)

    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), transport_factory=T)
    async with c.limiter:                                        # another request holds the limiter ...
        task = asyncio.create_task(c._get("https://fake.example/a"))
        for _ in range(10):
            await asyncio.sleep(0)
        vpn.set_enabled("fake", True)                            # ... while the user switches the VPN on
    await task
    assert sent == ["http://exit1"] and c.limiter.last_hour == 2  # never directly; the wait was refunded


async def test_ebay_keeps_the_server_when_the_description_is_refused():
    from app.platforms import ebay
    from tests.conftest import FIXTURES

    exits = {"http://exit1": FakeTransport([(200, (FIXTURES / "ebay_item.html").read_bytes()), (403, b"")])}
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory, c._tunnel, c._transport = exits.__getitem__, (lambda: tunnel), None
    d = await c.item("920036165580")                                       # itm.ebaydesc.com answers 403
    assert d.place == "Thalgau, Österreich" and tunnel.failures == [] and c.proxy == "http://exit1"
    assert exits["http://exit1"].cleared == 0 and c._warm and c.limiter.cooling_down_s == 0


async def test_a_page_in_place_of_the_one_asked_for_is_not_kept():
    c, tr = _client([(200, b"<html>bot check</html>"), (200, b'{"a": 1}')])
    asked = []

    def valid(r):
        asked.append(r.text)
        return r.text.startswith("{")

    assert (await c._get("https://fake.example/api", cache_s=60, valid=valid)).text == "<html>bot check</html>"
    r = await c._get("https://fake.example/api", cache_s=60, valid=valid)        # the bot check was not kept
    again = await c._get("https://fake.example/api", cache_s=60, valid=valid)    # the answer was
    assert r.json() == {"a": 1} and again is r and len(tr.calls) == 2 and len(asked) == 2   # cached: not judged again


def test_a_proxy_and_a_mock_transport_do_not_mix():
    with pytest.raises(ValueError):
        HttpxTransport({}, httpx.MockTransport(lambda r: httpx.Response(200)), proxy="http://127.0.0.1:1")
    tr = HttpxTransport({}, proxy="http://127.0.0.1:1")
    assert tr.proxy == "http://127.0.0.1:1"
    curl = CurlTransport({}, proxy="http://127.0.0.1:1")
    assert curl.proxy == "http://127.0.0.1:1"


async def test_a_request_that_waited_for_the_limiter_never_takes_an_old_route():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit2": [(200, b"{}")]}, tunnel)
    await tunnel.ensure()                         # exit1
    async with c.limiter:                         # another request holds the limiter ...
        task = asyncio.create_task(c._get("https://fake.example/api"))
        for _ in range(10):
            await asyncio.sleep(0)
        tunnel.route = None                       # ... while the connection changes (a shuffle)
        await tunnel.ensure()
    assert (await task).status_code == 200
    assert built == ["http://exit2"] and tunnel.failures == [] and c.limiter.last_hour == 2   # the wait was refunded


# ------------------------------------------------------------------ the shuffle's Trial (app/services/vpn_check.py)
async def test_a_trial_tries_one_server_only_and_leaves_the_marks_to_the_shuffle():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit1": [(403, b"cloudflare")], "http://exit2": [(200, b"{}")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr), pytest.raises(Blocked) as info:
        await c._get("https://fake.example/api")
    assert info.value.sent and (tr.refused, tr.detail, tr.sent, tr.status) == ("blocked", "HTTP 403", 1, 403)
    assert built == ["http://exit1"] and tunnel.failures == []      # not on to exit2, and no second shuffle
    assert c.limiter.cooling_down_s == 0

    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit1": [TransportError("proxy connect refused")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr), pytest.raises(PlatformError, match="Network error through the VPN"):
        await c._get("https://fake.example/api")
    assert (tr.refused, tr.sent, tr.detail) == ("unreachable", 1, "proxy connect refused")
    assert tunnel.failures == [] and built == ["http://exit1"]


async def test_a_trial_sends_nothing_when_the_server_moved():
    tunnel = FakeTunnel()
    c, _, built = _vpn_client({"http://exit2": [(200, b"{}")]}, tunnel)
    tr = Trial(await tunnel.ensure())
    await tunnel.failed(tr.route, "blocked")      # the connection left exit1 ...
    await tunnel.ensure()                         # ... for exit2
    with trial(tr), pytest.raises(ExitMoved):
        await c._get("https://fake.example/api")
    assert built == [] and tr.sent == 0


async def test_a_trial_never_goes_direct():
    from app.platforms.vpn import Route

    c, _, built = _vpn_client({}, None)           # the switch went off: no tunnel
    with trial(Trial(Route("http://exit1", 1, "x"))), pytest.raises(PlatformError, match="VPN is off"):
        await c._get("https://fake.example/api")
    assert built == []                            # never factory(None)


async def test_a_trial_notes_a_challenge_and_the_content_type():
    tunnel = FakeTunnel()
    c, _, _ = _vpn_client({"http://exit1": [(503, b"<html>", {"cf-mitigated": "challenge",
                                                                "content-type": "text/html"})]}, tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr):
        r = await c._get("https://fake.example/api")
    assert r.status_code == 503 and tr.challenge and tr.status == 503 and "html" in tr.ctype and not tr.refused
    assert tunnel.failures == []                  # the shuffle decides about bot checks, not the client


async def test_a_cancelled_trial_request_drops_its_transport():
    gate = asyncio.Event()

    class Hanging(FakeTransport):
        closed = False

        async def send(self, method, url, *, params=None, headers=None):
            self.calls.append(url)
            await gate.wait()
            return FetchResponse(200, {}, b"{}", url)

        async def aclose(self):
            self.closed = True

    hanging, fresh, seen = Hanging([]), FakeTransport([(200, b"{}")]), []

    def factory(proxy):
        seen.append(proxy)
        return hanging if len(seen) == 1 else fresh

    tunnel = FakeTunnel()
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=factory,
                           tunnel=lambda: tunnel)
    tr = Trial(await tunnel.ensure())

    async def run():
        with trial(tr):
            await c._get("https://fake.example/api")

    task = asyncio.create_task(run())
    while not hanging.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c._transport is None and c.proxy is None
    await asyncio.sleep(0)
    assert hanging.closed                         # closed in the background
    assert (await c._get("https://fake.example/api")).status_code == 200      # a live request: a new transport
    assert seen == ["http://exit1", "http://exit1"] and len(fresh.calls) == 1


async def test_a_trial_is_a_visitor_of_its_own_and_the_live_one_keeps_its_transport():
    from app.platforms import ebay
    from app.platforms.vpn import Route
    from tests.conftest import FIXTURES

    page = (FIXTURES / "ebay_search.html").read_bytes()
    exits = {"http://exit1": FakeTransport([(200, page), (200, page)]),
             "http://exit9": FakeTransport([(403, b""), (200, page)])}
    built = []
    tunnel = FakeTunnel()
    c = ebay.EbayClient(transport=FakeTransport([]))
    c.limiter = RateLimiter(0, name="eBay")
    c._factory = lambda proxy: built.append(proxy) or exits[proxy]
    c._tunnel, c._transport = (lambda: tunnel), None
    await c.search_page({"_nkw": "iphone"})                  # the live visitor, warm on exit1
    live = c._transport
    assert c._warm and c.proxy == "http://exit1"
    search = Route("http://exit9", 9, "de-ber-wg-001")        # a server the search tests, not the site's
    tunnel.running.add(search)
    tr = Trial(search, limit=3)
    with trial(tr):
        assert not c._warm                                    # a new visitor: its own warm-up
        await c.search_page({"_nkw": "fahrrad"})              # its warm-up 403, then the page
        assert c.proxy == "http://exit9" and c._transport is exits["http://exit9"] and c._warm
    assert (tr.sent, tr.wire, tr.refused) == (2, 2, None) and tunnel.failures == []
    assert c._transport is live and c.proxy == "http://exit1" and c._warm      # the live one untouched
    tr.close()
    assert tr.visitors == {}
    await c.search_page({"_nkw": "ipad"})                     # still the live visitor's transport
    assert built == ["http://exit1", "http://exit9"] and len(exits["http://exit1"].calls) == 2


async def test_a_trial_request_gives_up_after_its_timeout(monkeypatch):
    from app.platforms import httpclient

    monkeypatch.setattr(httpclient, "TRIAL_TIMEOUT_S", 0.05)

    class Silent(FakeTransport):
        closed = False

        async def send(self, method, url, *, params=None, headers=None):
            self.calls.append(url)
            await asyncio.Event().wait()                     # a server that never answers

        async def aclose(self):
            self.closed = True

    silent = Silent([])
    tunnel = FakeTunnel()
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=lambda proxy: silent,
                           tunnel=lambda: tunnel)
    tr = Trial(await tunnel.ensure())
    with trial(tr), pytest.raises(PlatformError, match="timed out after 0.05 s"):
        await c._get("https://fake.example/api")
    assert (tr.refused, tr.sent, tr.wire) == ("unreachable", 1, 1) and tunnel.failures == []
    await asyncio.sleep(0)
    assert silent.closed                                      # dropped: never reused


async def test_a_request_is_held_on_its_route_while_it_is_under_way():
    gate = asyncio.Event()
    tunnel = FakeTunnel()

    class Slow(FakeTransport):
        async def send(self, method, url, *, params=None, headers=None):
            await gate.wait()
            return await super().send(method, url, params=params, headers=headers)

    slow = Slow([(200, b"{}")])
    c = HttpPlatformClient("fake", RateLimiter(0, name="Fake"), name="Fake", transport_factory=lambda proxy: slow,
                           tunnel=lambda: tunnel)
    task = asyncio.create_task(c._get("https://fake.example/api"))
    while not tunnel.held:
        await asyncio.sleep(0)
    assert tunnel.held == [tunnel.route]                      # a switch-over lets it finish first
    gate.set()
    await task
    assert tunnel.held == []


# ------------------------------------------------------------------ parallel lanes (vpn.lanes, the product search)
async def test_a_lane_sends_through_its_own_server_with_its_own_limiter_and_visitor(monkeypatch):
    from app.platforms import httpclient, vpn
    from app.platforms.vpn import Route

    tunnel = FakeTunnel()
    helper = Route("http://exit7", 7, "de-fra-wg-007")
    tunnel.running.add(helper)
    c, transports, built = _vpn_client({"http://exit1": [(200, b"{}")], "http://exit7": [(200, b"{}"), (200, b"{}")]},
                                       tunnel)
    marked = []
    monkeypatch.setattr(vpn, "lane_refused", lambda pid, route, reason: marked.append((pid, route.name, reason)))
    lim = httpclient.lane_limiter("fake", "de-fra-wg-007", c.limiter)
    assert lim is not c.limiter and lim.per_hour == c.limiter.per_hour
    assert httpclient.lane_limiter("fake", "de-fra-wg-007", c.limiter) is lim          # one per server and site
    ln = httpclient.Lane("fake", helper, lim)
    await c._get("https://fake.example/a")                     # the site's own server, the live visitor
    with httpclient.lane(ln):
        assert c.limiter is lim
        await c._get("https://fake.example/b")
        await c._get("https://fake.example/c")
    assert c.limiter is not lim and lim.last_hour == 2 and c.proxy == "http://exit1"
    assert [len(transports[p].calls) for p in ("http://exit1", "http://exit7")] == [1, 2] and marked == []


async def test_a_refused_lane_is_lost_and_its_server_marked(monkeypatch):
    from app.platforms import httpclient, vpn
    from app.platforms.vpn import Route

    tunnel = FakeTunnel()
    helper = Route("http://exit7", 7, "de-fra-wg-007")
    tunnel.running.add(helper)
    c, transports, _ = _vpn_client({"http://exit7": [(403, b"cloudflare")]}, tunnel)
    marked = []
    monkeypatch.setattr(vpn, "lane_refused", lambda pid, route, reason: marked.append((pid, route.name, reason)))
    ln = httpclient.Lane("fake", helper, RateLimiter(0, name="Fake · x"))
    with httpclient.lane(ln), pytest.raises(httpclient.LaneLost):
        await c._get("https://fake.example/b")
    assert ln.lost == "blocked" and marked == [("fake", "de-fra-wg-007", "blocked")]
    assert tunnel.failures == [] and c.limiter.cooling_down_s == 0      # the site's own server and limiter untouched
    with httpclient.lane(ln), pytest.raises(httpclient.LaneLost):
        await c._get("https://fake.example/c")                          # a lost lane sends nothing more
    assert len(transports["http://exit7"].calls) == 1
    tunnel.running.clear()
    gone = httpclient.Lane("fake", helper, RateLimiter(0))
    with httpclient.lane(gone), pytest.raises(httpclient.LaneLost):
        await c._get("https://fake.example/d")                          # its server stopped: never another route
    assert gone.lost == "gone"


async def test_refusals_that_do_not_count_yet_stop_after_a_few_sends(monkeypatch):
    """Refusals within the span count for nothing yet (vpn.CONFIRM_SPAN_S): the request stops after a few sends to
    that server instead of up to VPN_TRIES within seconds; a later request asks again."""
    from app.platforms import httpclient
    tunnel = FakeTunnel(confirm=99)                           # nothing counts
    c, transports, built = _vpn_client({"http://exit1": [(403, b"")] * 8}, tunnel)
    with pytest.raises(Blocked):
        await c._get("https://fake.example/api")
    assert len(transports["http://exit1"].calls) == httpclient.VPN_SAME_SERVER_TRIES
