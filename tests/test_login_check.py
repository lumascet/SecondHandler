"""The willhaben login check: a 204/401 means logged out, but a 5xx or a block page says nothing about the login."""
from types import SimpleNamespace as NS

from app.willhaben import native
from app.willhaben import session as ws


def _session(monkeypatch, status, text=""):
    s = ws.BrowserSession()
    s.context, s.logged_in = object(), True          # a running browser, logged in
    monkeypatch.setattr(native, "active", lambda: None)

    async def api(method, path, body=None):
        return NS(status=status, text=text, json=lambda: {})

    async def no_sso():
        return False
    monkeypatch.setattr(s, "api", api)
    monkeypatch.setattr(s, "refresh_sso", no_sso)
    return s


async def test_a_server_error_keeps_the_login(monkeypatch):
    s = _session(monkeypatch, 503)
    assert await s.check_login() is True and s.logged_in is True        # inconclusive: unchanged


async def test_a_204_is_logged_out(monkeypatch):
    s = _session(monkeypatch, 204)
    assert await s.check_login() is False and s.logged_in is False
