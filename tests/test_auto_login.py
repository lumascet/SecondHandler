"""Logged out with stored login details: the app logs in again by itself, at most once an hour, and pushes the owner
only when the site asks for something a person has to do (a captcha, a code)."""
import pytest

from app.services import credentials


@pytest.fixture
def creds(monkeypatch, tmp_path):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    credentials.save("vinted", "me@example.org", "pw")
    pushed = []

    async def notify(kind, title, text, **kw):
        pushed.append(title)
    monkeypatch.setattr("app.services.notify.notify", notify)
    return pushed


async def test_it_logs_in_by_itself_at_most_hourly(creds, monkeypatch):
    calls = []

    async def login(site):
        calls.append(site)
        return "ok"
    monkeypatch.setattr(credentials, "login", login)
    assert await credentials.auto_login("vinted") == "ok"
    assert await credentials.auto_login("vinted") is None            # again within the hour: not tried
    assert calls == ["vinted"] and creds == []


async def test_a_captcha_is_pushed_to_the_owner(creds, monkeypatch):
    async def login(site):
        return "sent"
    monkeypatch.setattr(credentials, "login", login)
    assert await credentials.auto_login("vinted") == "sent"
    assert creds == ["Vinted needs you"]


async def test_switched_off_or_without_details_nothing_happens(creds, monkeypatch):
    from app.db import set_setting

    async def login(site):
        raise AssertionError("must not log in")
    monkeypatch.setattr(credentials, "login", login)
    set_setting("auto_login_vinted", "0")
    assert await credentials.auto_login("vinted") is None
    set_setting("auto_login_vinted", "")
    credentials.forget("vinted")
    assert await credentials.auto_login("vinted") is None


async def test_an_attempt_that_sent_nothing_tries_again_soon(creds, monkeypatch):
    """No login form (the page showed something else): nothing reached the site, so no lock-out risk – again after
    AUTO_RETRY_UNSENT_S, not an hour."""
    import time as _time
    results = iter(["no_form", "ok"])

    async def login(site):
        return next(results)
    monkeypatch.setattr(credentials, "login", login)
    assert await credentials.auto_login("vinted") == "no_form"
    assert await credentials.auto_login("vinted") is None                      # not at once
    now = _time.time()
    monkeypatch.setattr(credentials.time, "time", lambda: now + credentials.AUTO_RETRY_UNSENT_S + 1)
    assert await credentials.auto_login("vinted") == "ok"


async def test_the_dead_vinted_login_cookies_go_before_a_login(monkeypatch):
    from types import SimpleNamespace

    from app.platforms import vinted_messaging as vm
    cleared = []

    class Ctx:
        async def clear_cookies(self, name=None, domain=None):
            cleared.append(name)
    monkeypatch.setattr(vm, "_alive", False)
    await credentials._drop_dead_vinted_login(SimpleNamespace(context=Ctx()))
    assert "access_token_web" in cleared and "refresh_token_web" in cleared and "datadome" not in cleared
    cleared.clear()
    monkeypatch.setattr(vm, "_alive", True)                                   # a working login stays
    await credentials._drop_dead_vinted_login(SimpleNamespace(context=Ctx()))
    assert cleared == []


async def test_a_sent_login_the_check_did_not_see_tries_again_after_15_min(creds, monkeypatch):
    import time as _time
    results = iter(["sent", "ok"])

    async def login(site):
        return next(results)
    monkeypatch.setattr(credentials, "login", login)
    assert await credentials.auto_login("vinted") == "sent"
    now = _time.time()
    monkeypatch.setattr(credentials.time, "time", lambda: now + credentials.AUTO_RETRY_SENT_S - 5)
    assert await credentials.auto_login("vinted") is None
    monkeypatch.setattr(credentials.time, "time", lambda: now + credentials.AUTO_RETRY_SENT_S + 1)
    assert await credentials.auto_login("vinted") == "ok"
