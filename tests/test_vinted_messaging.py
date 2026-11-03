"""Vinted messaging over the API: the owned cookie jar (seeded from the session), the inbox/conversation/escrow
calls, the logged-out/fallback signal (None), and the pure SSE event framing."""
from types import SimpleNamespace

import pytest

from app.platforms import vinted as vi
from app.platforms import vinted_messaging as vm
from app.platforms.base import Blocked
from app.platforms.ratelimit import RateLimiter
from tests.test_kleinanzeigen import Queue


class Ctx:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, urls):
        return self._cookies


def _sess(cookies):
    return SimpleNamespace(context=Ctx(cookies))


@pytest.fixture(autouse=True)
def _reset():
    vm._jar.clear()
    vm._anon = None
    vm._loaded = True          # skip the on-disk jar load; tests drive seed_from_browser directly
    vm._alive = True           # a working login unless a test says otherwise
    vm._dead.clear()
    yield
    vm._jar.clear()
    vm._anon = None
    vi.set_client(None)


async def test_seed_requires_a_session_cookie():
    assert await vm.seed_from_browser(_sess([{"name": "anon_id", "value": "a1"}])) is False   # no session cookie
    assert await vm.seed_from_browser(SimpleNamespace(context=None)) is False                 # no browser
    assert await vm.seed_from_browser(_sess([{"name": "access_token_web", "value": "t"},
                                             {"name": "anon_id", "value": "a1"}])) is True
    assert vm._jar.get("access_token_web") == "t" and vm._anon == "a1"


async def test_inbox_calls_the_api_with_the_session_cookie():
    c = vi.VintedClient(transport=Queue([(200, b'{"conversations":[{"id":"u1"}]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    data = await vm.inbox(_sess([{"name": "access_token_web", "value": "t"}, {"name": "anon_id", "value": "a1"}]))
    assert data == {"conversations": [{"id": "u1"}]}
    url, headers = c._transport.calls[-1]
    assert url == "https://api.vinted.at/messaging/main/inbox"
    assert "access_token_web=t" in headers["Cookie"] and headers["X-Anon-Id"] == "a1"


async def test_messaging_goes_direct_even_when_the_vpn_is_on(monkeypatch):
    """The logged-in messaging call stays on the home IP (route None), never the anonymous VPN exit — a datacenter
    VPN IP on the owner's session is what the bot protection blocks (403), the bug that took the chat offline."""
    c = vi.VintedClient(transport=Queue([(200, b'{"conversations":[]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    c._tunnel = lambda: object()                 # Vinted's VPN switch is on
    seen: dict = {}
    real_once = c._send_once

    async def spy_once(full, kind, headers, route, *, force_direct=False):
        seen["route"], seen["direct"] = route, force_direct
        return await real_once(full, kind, headers, route, force_direct=force_direct)

    async def no_vpn(*a, **k):
        raise AssertionError("an authenticated messaging request must not go through the VPN")

    monkeypatch.setattr(c, "_send_once", spy_once)
    monkeypatch.setattr(c, "_send_vpn", no_vpn)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "t"}])) == {"conversations": []}
    assert seen == {"route": None, "direct": True}


async def test_conversation_and_escrow_urls():
    c = vi.VintedClient(transport=Queue([(200, b'{"messages":[]}'), (200, b'{"escrow_order":{}}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    s = _sess([{"name": "v_sid", "value": "s"}])
    assert await vm.conversation(s, "01a1-uuid") == {"messages": []}
    assert c._transport.calls[-1][0] == "https://api.vinted.at/messaging/main/conversations/01a1-uuid"
    assert await vm.escrow(s, "77") == {"escrow_order": {}}
    assert c._transport.calls[-1][0] == "https://api.vinted.at/escrow-orders/api/v2/escrow-orders/77"


async def test_logged_out_returns_none():
    assert await vm.inbox(_sess([{"name": "anon_id", "value": "a1"}])) is None   # no auth cookie, no call, fall back


async def test_http_error_without_refresh(monkeypatch):
    async def no(sess):
        return False
    monkeypatch.setattr(vm, "_refresh", no)
    monkeypatch.setattr(vm, "seed_from_browser", no)
    vm._jar.update({"access_token_web": "t"})                 # already seeded; the 401 can't be recovered -> None
    c = vi.VintedClient(transport=Queue([(401, b"nope")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "t"}])) is None


async def test_a_403_reseeds_the_datadome_and_does_not_log_out(monkeypatch):
    """A 403 is the bot-protection cookie (datadome), not a logout: the limiter then pauses Vinted (no in-call retry);
    a browser page (a turn: it gets the login and gives the new datadome back) fetches a fresh one for the next try."""
    fresh = {"n": 0}

    async def datadome(sess):
        fresh["n"] += 1
        vm._jar["datadome"] = "dd2"

    async def never_logout(sess):
        raise AssertionError("a 403 must not log the session out")

    monkeypatch.setattr(vm, "_fresh_datadome", datadome)
    monkeypatch.setattr(vm, "_mark_logged_out", never_logout)
    vm._jar.update({"access_token_web": "stale"})
    c = vi.VintedClient(transport=Queue([(403, b"datadome")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "stale"}])) is None   # falls back this time
    assert fresh["n"] == 1 and vm._jar["datadome"] == "dd2"                                  # but the jar is healed


class TurnCtx:
    """A browser context that remembers what it got (add_cookies) and what a page left behind."""
    def __init__(self, cookies):
        self.store = {c["name"]: dict(c) for c in cookies}

    async def cookies(self, urls=None):
        return [dict(c) for c in self.store.values()]

    async def add_cookies(self, cookies):
        for c in cookies:
            self.store[c["name"]] = dict(c)


async def test_the_browser_borrows_the_login_and_gives_it_back(monkeypatch, tmp_path):
    """One holder: before a Vinted page the browser gets the jar's current token (not its own stale one), after it the
    jar takes back what the page left (the web app refreshed it), and the jar does not refresh meanwhile."""
    monkeypatch.setattr(vm, "_state_path", lambda: tmp_path / "v.json")
    ctx = TurnCtx([{"name": "access_token_web", "value": "old", "domain": ".vinted.at", "path": "/"},
                   {"name": "refresh_token_web", "value": "r-old", "domain": ".vinted.at", "path": "/"}])
    sess = SimpleNamespace(context=ctx)
    vm._jar.update({"access_token_web": "t2", "refresh_token_web": "r2"})
    async with vm.browser_turn(sess):
        assert ctx.store["access_token_web"]["value"] == "t2" and ctx.store["refresh_token_web"]["value"] == "r2"
        assert await vm._refresh(sess) is None                       # the browser holds it now: no second refresh
        ctx.store["access_token_web"]["value"] = "t3"               # the page's web app refreshed it
        ctx.store["refresh_token_web"]["value"] = "r3"
    assert vm._jar["access_token_web"] == "t3" and vm._jar["refresh_token_web"] == "r3"


async def test_an_empty_jar_does_not_overwrite_a_new_browser_login(monkeypatch, tmp_path):
    monkeypatch.setattr(vm, "_state_path", lambda: tmp_path / "v.json")
    ctx = TurnCtx([{"name": "access_token_web", "value": "new-login", "domain": ".vinted.at", "path": "/"}])
    async with vm.browser_turn(SimpleNamespace(context=ctx)):
        pass
    assert ctx.store["access_token_web"]["value"] == "new-login" and vm._jar["access_token_web"] == "new-login"


async def test_a_search_block_does_not_pause_chat():
    """The anonymous search and the logged-in chat pace on separate limiters: a 403 that paused the search limiter
    (a long cool-down) must not stop a chat read — the logged-in session has its own limiter."""
    c = vi.VintedClient(transport=Queue([(200, b'{"conversations":[]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    c.limiter.penalize()                                     # a search block: the shared limiter is now cooling down
    vi.set_client(c)
    assert c.limiter.cooling_down_s > 0                      # a search call would be refused
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "t"}])) == {"conversations": []}


async def test_a_chat_403_does_not_pause_the_search(monkeypatch):
    """A bot-protection 403 on the chat pauses only the chat's own limiter, never the shared search limiter, so a
    datadome refusal on the owner's session does not also stall the anonymous product search."""
    async def reseed(sess):
        return True
    monkeypatch.setattr(vm, "seed_from_browser", reseed)
    c = vi.VintedClient(transport=Queue([(403, b"datadome")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    vm._jar.update({"access_token_web": "t"})
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "t"}])) is None   # 403 -> reseed, fall back
    assert c.limiter.cooling_down_s == 0                     # the search limiter was left untouched
    assert c._authed_limiter().cooling_down_s > 0            # the chat paused itself instead


def test_jar_persists_across_a_restart(monkeypatch, tmp_path):
    p = tmp_path / "vinted-native.json"
    monkeypatch.setattr(vm, "_state_path", lambda: p)
    vm._jar.clear()
    vm._jar.update({"access_token_web": "t1", "_vinted_fr_session": "s1"})
    vm._anon = "a1"
    vm._persist()
    vm._jar.clear()                                # simulate a restart: memory gone, disk remains
    vm._anon = None
    assert vm.has_auth()                           # the rotated token survives on disk
    st = vm._load_persisted()
    assert st["jar"]["access_token_web"] == "t1" and st["anon"] == "a1"


def test_sse_event_framing():
    buf = [""]
    assert vm.sse_events("data: a\n\ndata: b\n", buf) == ["a"]     # one complete event, one still buffered
    assert buf[0] == "data: b\n"
    assert vm.sse_events("\n\n", buf) == ["b"]
    buf = [""]
    assert vm.sse_events(":heartbeat\n\ndata: x\ndata: y\n\n", buf) == ["x\ny"]   # comment skipped, multiline joined


async def test_chat_keeps_its_own_connection_apart_from_the_search():
    """A chat GET must not take over (and close) the anonymous search's transport, nor land the account's cookies in
    the anonymous jar; a chat 403 must not clear the search visitor's cookies."""
    built = []

    class T:
        def __init__(self, proxy):
            self.proxy, self.cleared, self.closed = proxy, 0, False
            built.append(self)

        async def send(self, method, url, *, params=None, headers=None):
            from app.platforms.httpclient import FetchResponse
            return FetchResponse(403 if "messaging" in url else 200, {}, b"{}", url)

        def clear_cookies(self):
            self.cleared += 1

        async def aclose(self):
            self.closed = True

    c = vi.VintedClient()
    c._factory = T
    c._tunnel = lambda: None
    c.limiter = RateLimiter(0, name="Vinted")
    search = await c._use("socks://vpn")              # the anonymous search's transport, on the VPN route
    with pytest.raises(Blocked):                       # the 403
        await c.messaging_get("/messaging/main/inbox", "access_token_web=t")
    assert c._live.transport is search and not search.closed     # the search keeps its connection
    assert search.cleared == 0                                    # and its cookies
    assert c._authed_v.transport is not None and c._authed_v.transport is not search


async def test_a_401_refreshes_before_asking_the_browser(monkeypatch):
    """An expired access token: the jar's refresh token renews it. The browser's cookie used to be taken first; it
    was the same stale token, every retry 401'd again, the refresh never ran and the chat logged itself out."""
    order = []

    async def refresh(sess):
        order.append("refresh")
        vm._jar["access_token_web"] = "fresh"
        return True

    async def reseed(sess):
        order.append("browser")
        return True
    monkeypatch.setattr(vm, "_refresh", refresh)
    monkeypatch.setattr(vm, "seed_from_browser", reseed)
    vm._jar.update({"access_token_web": "stale"})
    c = vi.VintedClient(transport=Queue([(401, b""), (200, b'{"conversations":[]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "stale"}])) == {"conversations": []}
    assert order == ["refresh"]


async def test_the_browsers_same_stale_token_is_no_renewal(monkeypatch):
    async def rejected(sess):
        return False

    async def same(sess):
        vm._jar["access_token_web"] = "stale"          # the browser holds the very token that was refused
        return True
    logged_out = []

    async def mark(sess):
        logged_out.append(1)
    monkeypatch.setattr(vm, "_refresh", rejected)
    monkeypatch.setattr(vm, "seed_from_browser", same)
    monkeypatch.setattr(vm, "_mark_logged_out", mark)
    vm._jar.update({"access_token_web": "stale"})
    c = vi.VintedClient(transport=Queue([(401, b""), (401, b""), (401, b"")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "stale"}])) is None
    assert len(c._transport.calls) == 1 and logged_out == [1]   # one try, no loop on the same token


async def test_a_dead_login_does_not_overwrite_a_new_one_in_the_browser(monkeypatch, tmp_path):
    """The owner logged in again in the browser while the jar still held the refused login: the turn must not hand the
    dead tokens over (it overwrote the new login, 11-02 23:58), and takes the new one back."""
    monkeypatch.setattr(vm, "_state_path", lambda: tmp_path / "v.json")
    vm._jar.update({"access_token_web": "dead", "refresh_token_web": "r-dead"})
    vm._bury()                                                     # Vinted refused it
    ctx = TurnCtx([{"name": "access_token_web", "value": "new", "domain": ".vinted.at", "path": "/"},
                   {"name": "refresh_token_web", "value": "r-new", "domain": ".vinted.at", "path": "/"}])
    async with vm.browser_turn(SimpleNamespace(context=ctx)):
        assert ctx.store["access_token_web"]["value"] == "new"     # left alone
    assert vm._jar["access_token_web"] == "new"


async def test_refused_tokens_are_not_taken_back_from_the_browser(monkeypatch, tmp_path):
    """After a logout the browser keeps the same old cookies: re-reading them only meant another refused refresh every
    few minutes. The new login (other tokens) is taken."""
    monkeypatch.setattr(vm, "_state_path", lambda: tmp_path / "v.json")
    vm._jar.update({"access_token_web": "dead", "refresh_token_web": "r-dead"})
    vm._bury()
    vm._jar.clear()
    old = _sess([{"name": "access_token_web", "value": "dead"}, {"name": "refresh_token_web", "value": "r-dead"}])
    assert await vm.seed_from_browser(old) is False and vm._jar == {}
    new = _sess([{"name": "access_token_web", "value": "fresh"}, {"name": "refresh_token_web", "value": "r-fresh"}])
    assert await vm.seed_from_browser(new) is True and vm._jar["access_token_web"] == "fresh"
