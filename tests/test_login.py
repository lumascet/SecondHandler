import base64

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.main import app


@pytest.fixture
def pw(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(auth, "_fails", type(auth._fails)(auth._fails.default_factory))
    monkeypatch.setattr("app.main.asyncio.sleep", _no_sleep)


async def _no_sleep(_s):
    return None


def test_browser_redirects_to_login_and_cookie_works(pw):
    with TestClient(app) as c:
        r = c.get("/myads?show_sold=1", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fmyads%3Fshow_sold%3D1"
        assert "www-authenticate" not in r.headers                      # no browser popup
        assert 'name="password"' in c.get("/login").text
        r = c.post("/login", data={"username": "admin", "password": "falsch", "next": "/myads"})
        assert r.status_code == 401 and "Wrong user name or password" in r.text
        r = c.post("/login", data={"username": "admin", "password": "geheim", "remember": "on",
                                   "next": "/myads?show_sold=1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/myads?show_sold=1"
        assert "max-age" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
        assert c.get("/myads").status_code == 200
        assert "Log out" in c.get("/").text
        c.post("/logout")
        assert c.get("/myads", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303


def test_api_clients_and_basic_auth(pw):
    with TestClient(app) as c:
        r = c.get("/api/agents")
        assert r.status_code == 401 and "www-authenticate" not in r.headers
        assert c.get("/hits", headers={"hx-request": "true"}).headers.get("hx-redirect", "").startswith("/login")
        basic = base64.b64encode(b"admin:geheim").decode()
        assert c.get("/myads", headers={"authorization": f"Basic {basic}"}).status_code == 200
        assert c.get("/healthz").status_code == 200


def test_token_and_redirect_safety(pw, monkeypatch):
    tok = auth.make_token(remember=False, now=1000)
    assert auth.valid_token(tok, now=1001) and not auth.valid_token(tok, now=1000 + auth.SESSION_S + 1)
    assert not auth.valid_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1001)
    monkeypatch.setattr(get_settings(), "dashboard_password", "neu")       # password change ends sessions
    assert not auth.valid_token(tok, now=1001)
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "", None):
        assert auth.safe_next(bad) == "/"
    assert auth.safe_next("/chats/1#x") == "/chats/1#x"


def test_lockout(pw):
    with TestClient(app) as c:
        for _ in range(auth.MAX_FAILS):
            c.post("/login", data={"username": "admin", "password": "x"})
        r = c.post("/login", data={"username": "admin", "password": "geheim"}, follow_redirects=False)
        assert r.status_code == 401 and "Too many failed attempts" in r.text


def test_the_home_screen_app_gets_its_manifest_and_icons_before_any_login(pw):
    """iOS and Android fetch the manifest and the icon when the page is added to the home screen; the app then opens
    in a window of its own (standalone), the log-in page included."""
    with TestClient(app) as c:
        m = c.get("/static/manifest.json")
        assert m.status_code == 200 and m.json()["display"] == "standalone" and m.json()["start_url"] == "/"
        assert m.json()["name"] == m.json()["short_name"] == "SecondHandler"
        for icon in m.json()["icons"]:
            r = c.get(icon["src"])
            assert r.status_code == 200 and r.headers["content-type"] == icon["type"]
        assert c.get("/static/icons/apple-touch-icon.png").headers["content-type"] == "image/png"
        page = c.get("/login").text
        c.post("/login", data={"username": "admin", "password": "geheim", "next": "/"})
        for html in (page, c.get("/").text):
            assert '<link rel="manifest" href="/static/manifest.json?v=4">' in html
            assert 'name="apple-mobile-web-app-title" content="SecondHandler"' in html
            assert 'name="apple-mobile-web-app-capable" content="yes"' in html and "viewport-fit=cover" in html
            assert 'rel="apple-touch-icon"' in html


def _launch(token, user_id, at, **extra):
    """Mini App launch data as Telegram signs it (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
    import hashlib
    import hmac
    import json
    from urllib.parse import urlencode

    fields = {"auth_date": str(int(at)), "query_id": "AAH", "user": json.dumps({"id": user_id, "first_name": "Owner"}),
              **extra}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(key, check.encode(), hashlib.sha256).hexdigest()})


def test_a_dashboard_opened_from_the_telegram_bot_logs_in_the_linked_user_only(pw, monkeypatch):
    import time

    from app.db import set_setting

    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:secret")
    set_setting("telegram_chat_id", "4242")
    now = time.time()
    assert auth.telegram_user(_launch("123:secret", 4242, now), "123:secret") == 4242
    assert auth.telegram_user(_launch("123:other", 4242, now), "123:secret") is None          # another bot's
    assert auth.telegram_user(_launch("123:secret", 4242, now - 2 * 86400), "123:secret") is None   # stale
    forged = _launch("123:secret", 4242, now).replace("4242", "4243")
    assert auth.telegram_user(forged, "123:secret") is None
    with TestClient(app) as c:
        page = c.get("/login?next=%2Fchats").text
        assert "tgWebAppData" in page and "/login/telegram" in page
        r = c.post("/login/telegram", data={"init_data": _launch("123:secret", 9999, now), "next": "/chats"})
        assert r.status_code == 401 and c.get("/chats", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303
        r = c.post("/login/telegram", data={"init_data": _launch("123:secret", 4242, now), "next": "/chats"})
        assert r.status_code == 200 and r.json() == {"ok": True, "next": "/chats"}
        assert "max-age" in r.headers["set-cookie"].lower() and c.get("/chats").status_code == 200


def test_dashboard_links_in_telegram_open_as_a_mini_app_other_links_stay_links(monkeypatch):
    from app.db import set_setting
    from app.services import telegram

    monkeypatch.setattr(get_settings(), "public_url", "https://agent.example")
    set_setting("telegram_chat_id", "4242")
    assert telegram.open_button("Open", "https://agent.example/hits/1") == \
        {"text": "Open", "web_app": {"url": "https://agent.example/hits/1"}}
    assert telegram.open_button("Open", "https://www.willhaben.at/iad/object?adId=1") == \
        {"text": "Open", "url": "https://www.willhaben.at/iad/object?adId=1"}
    assert telegram.open_button("Open", "https://agent.example.evil/x")["url"]          # only the dashboard itself
    set_setting("telegram_chat_id", "-100123")                                             # a group: links only
    assert "url" in telegram.open_button("Open", "https://agent.example/hits/1")


def test_another_sites_page_cannot_change_anything(pw):
    """A state-changing request whose Origin is another host (or that the browser calls cross-site) is refused, with
    or without the login cookie; same-origin pages, scripts with credentials and plain clients pass."""
    basic = "Basic " + base64.b64encode(b"admin:geheim").decode()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "geheim", "next": "/"})
        evil = c.post("/settings/quiet", data={"start": "01:00"}, headers={"origin": "http://10.0.1.8:9999"})
        assert evil.status_code == 403
        assert c.post("/settings/quiet", data={}, headers={"origin": "null"}).status_code == 403
        assert c.post("/settings/quiet", data={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        ok = c.post("/settings/quiet", data={"start": "01:00"}, headers={"origin": "http://testserver"}, follow_redirects=False)
        assert ok.status_code == 303
        assert c.post("/settings/quiet", data={}, headers={"origin": "http://elsewhere", "authorization": basic},
                      follow_redirects=False).status_code == 303
        assert c.get("/", headers={"origin": "http://elsewhere"}).status_code == 200      # reading is not changing
        assert "frame-ancestors" in c.get("/tg").headers.get("content-security-policy", "")


def test_redirect_targets_stay_local():
    for bad in ("//evil.com", "/\t/evil.com", "/%2fevil.com", "/\\evil.com", "https://evil.com", "/a b", ""):
        assert auth.safe_next(bad) == "/", bad
    assert auth.safe_next("/myads?show_sold=1") == "/myads?show_sold=1"


def test_the_login_page_language_is_this_browsers_only(pw):
    from app.db import get_setting
    with TestClient(app) as c:
        before = get_setting("ui_lang")
        r = c.post("/lang", data={"lang": "de", "next": "/login"}, follow_redirects=False)
        assert r.status_code == 303 and get_setting("ui_lang") == before and "wh_lang=de" in r.headers["set-cookie"]
        assert "Anmelden" in c.get("/login").text or 'lang="de"' in c.get("/login").text
        c.post("/login", data={"username": "admin", "password": "geheim", "next": "/"})
        c.post("/lang", data={"lang": "en", "next": "/"})
        assert get_setting("ui_lang") == "en"


def test_without_a_password_the_public_address_needs_the_telegram_login(monkeypatch, tmp_path):
    """No DASHBOARD_PASSWORD: the home network stays open, a request over the public web address needs the linked
    Telegram account's login (the Mini App); the login page says why and shows no password form."""
    import time

    from app.db import set_setting

    monkeypatch.setattr(get_settings(), "dashboard_password", None)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    from app.db import SETTING_PUBLIC_URL, apply_public_url
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "123:secret")
    monkeypatch.setattr(auth, "_fails", type(auth._fails)(auth._fails.default_factory))
    monkeypatch.setattr("app.main.asyncio.sleep", _no_sleep)
    set_setting("telegram_chat_id", "4242")
    set_setting(SETTING_PUBLIC_URL, "https://agent.example")      # Settings → Web address (applied at startup)
    apply_public_url()
    assert auth.public_host() == "agent.example"
    html = {"accept": "text/html"}
    with TestClient(app) as c:
        assert c.get("/hits", headers=html).status_code == 200                              # home network: open
        outside = {**html, "host": "agent.example", "x-forwarded-proto": "https"}
        r = c.get("/hits", headers=outside, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        spoofed = {**outside, "x-forwarded-host": "10.0.1.8"}
        assert c.get("/hits", headers=spoofed, follow_redirects=False).status_code == 303   # the host still counts
        page = c.get("/login?next=%2Ftg", headers=outside).text
        assert "data-no-password" in page and 'name="password"' not in page and "/login/telegram" in page
        r = c.post("/login/telegram", headers=outside,
                   data={"init_data": _launch("123:secret", 4242, time.time()), "next": "/tg"})
        cookie = r.headers["set-cookie"].lower()
        assert r.status_code == 200 and "samesite=none" in cookie and "secure" in cookie   # Telegram Web's iframe
        token = r.cookies.get(auth.COOKIE) or r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        c.cookies.clear()                                    # Secure: sent over https only, as a browser would
        assert c.get("/hits", headers={**outside, "cookie": f"{auth.COOKIE}={token}"},
                     follow_redirects=False).status_code == 200
    set_setting(SETTING_PUBLIC_URL, "")
    apply_public_url()
    for local in ("http://localhost:8000", "http://10.0.1.8:8010", "http://agent.local"):
        monkeypatch.setattr(get_settings(), "public_url", local)
        assert auth.public_host() is None


def test_the_debug_login_only_straight_from_the_home_network(pw, monkeypatch):
    token = "d" * 32
    ok = {"x-debug-token": token}
    assert not auth.debug_login(ok, "192.168.0.2")                         # off without DEBUG_TOKEN
    monkeypatch.setattr(get_settings(), "debug_token", token)
    assert auth.debug_login(ok, "192.168.0.2") and auth.debug_login(ok, "127.0.0.1")
    assert not auth.debug_login(ok, "8.8.8.8")                             # a public address
    assert not auth.debug_login({**ok, "x-forwarded-for": "203.0.113.9"}, "172.18.0.5")   # through the proxy
    assert not auth.debug_login({**ok, "x-forwarded-host": "app.example.org"}, "172.18.0.5")
    assert not auth.debug_login({"x-debug-token": "x" * 32}, "192.168.0.2")
    assert not auth.debug_login({}, "192.168.0.2") and not auth.debug_login(ok, None)
    monkeypatch.setattr(get_settings(), "debug_token", "short")
    assert not auth.debug_login({"x-debug-token": "short"}, "192.168.0.2")  # too short a token is no login
    monkeypatch.setattr(get_settings(), "debug_token", token)
    with TestClient(app, client=("192.168.0.2", 5000)) as c:
        assert c.get("/myads", headers=ok).status_code == 200
        assert c.get("/myads").status_code == 401
        assert c.get("/myads", headers={**ok, "x-forwarded-for": "203.0.113.9"}).status_code == 401
