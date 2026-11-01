"""Vinted messaging over the API: auth taken from the session's cookies, the inbox/conversation calls, and the
fall-back-to-browser signal (None) when logged out or on failure."""
from types import SimpleNamespace

from app.platforms import vinted as vi
from app.platforms import vinted_messaging as vm
from app.platforms.ratelimit import RateLimiter
from tests.test_kleinanzeigen import Queue


class Ctx:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, url):
        return self._cookies


def _sess(cookies):
    return SimpleNamespace(context=Ctx(cookies))


async def test_auth_needs_a_logged_in_cookie():
    assert await vm.auth(_sess([{"name": "anon_id", "value": "a1"}])) is None          # no session cookie
    assert await vm.auth(SimpleNamespace(context=None)) is None                        # no browser
    jar, anon = await vm.auth(_sess([{"name": "access_token_web", "value": "tok"},
                                     {"name": "anon_id", "value": "a1"}]))
    assert "access_token_web=tok" in jar and anon == "a1"


async def test_inbox_calls_the_api_with_the_session_cookie():
    c = vi.VintedClient(transport=Queue([(200, b'{"conversations":[{"id":"u1"}]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    try:
        data = await vm.inbox(_sess([{"name": "access_token_web", "value": "tok"},
                                     {"name": "anon_id", "value": "a1"}]))
        assert data == {"conversations": [{"id": "u1"}]}
        url, headers = c._transport.calls[-1]
        assert url == "https://api.vinted.at/messaging/main/inbox"
        assert headers["Cookie"].startswith("access_token_web=tok") and headers["X-Anon-Id"] == "a1"
        assert headers["Accept"].startswith("application/json")
    finally:
        vi.set_client(None)


async def test_conversation_url_and_logged_out_fallback():
    c = vi.VintedClient(transport=Queue([(200, b'{"messages":[]}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    try:
        assert await vm.conversation(_sess([{"name": "v_sid", "value": "s"}]), "01a1-uuid") == {"messages": []}
        assert c._transport.calls[-1][0] == "https://api.vinted.at/messaging/main/conversations/01a1-uuid"
        # logged out: no call, None so the caller uses the browser tab
        assert await vm.inbox(_sess([{"name": "anon_id", "value": "a1"}])) is None
    finally:
        vi.set_client(None)


async def test_escrow_url():
    c = vi.VintedClient(transport=Queue([(200, b'{"escrow_order":{"id":77}}')]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    try:
        assert await vm.escrow(_sess([{"name": "v_sid", "value": "s"}]), "77") == {"escrow_order": {"id": 77}}
        assert c._transport.calls[-1][0] == "https://api.vinted.at/escrow-orders/api/v2/escrow-orders/77"
    finally:
        vi.set_client(None)


async def test_http_error_falls_back():
    c = vi.VintedClient(transport=Queue([(403, b"nope")]))
    c.limiter = RateLimiter(0, name="Vinted")
    vi.set_client(c)
    try:
        assert await vm.inbox(_sess([{"name": "access_token_web", "value": "tok"}])) is None
    finally:
        vi.set_client(None)
