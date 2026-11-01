"""Vinted messaging over the API: the owned cookie jar (seeded from the session), the inbox/conversation/escrow
calls, the logged-out/fallback signal (None), and the pure SSE event framing."""
from types import SimpleNamespace

import pytest

from app.platforms import vinted as vi
from app.platforms import vinted_messaging as vm
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
    """A 403 is the bot-protection cookie (datadome), not a logout: the limiter then pauses Vinted (no in-call retry),
    so re-seed the datadome from the logged-in browser for the next attempt and leave the login untouched."""
    seeded = {"n": 0}

    async def reseed(sess):
        seeded["n"] += 1
        vm._jar.update({"access_token_web": "fresh", "datadome": "dd2"})
        return True

    async def never_logout(sess):
        raise AssertionError("a 403 must not log the session out")

    monkeypatch.setattr(vm, "seed_from_browser", reseed)
    monkeypatch.setattr(vm, "_mark_logged_out", never_logout)
    vm._jar.update({"access_token_web": "stale"})
    c = vi.VintedClient(transport=Queue([(403, b"datadome")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    assert await vm.inbox(_sess([{"name": "access_token_web", "value": "stale"}])) is None   # falls back this time
    assert seeded["n"] == 1 and vm._jar["datadome"] == "dd2"                                 # but the jar is healed


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
