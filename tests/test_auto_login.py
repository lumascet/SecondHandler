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
