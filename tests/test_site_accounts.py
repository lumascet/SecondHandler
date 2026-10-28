"""Kleinanzeigen and Vinted accounts in the app's browser: the login page in the remote view, the check in a hidden
tab (a redirect to the login page = logged out), the state on the site's settings page."""
from fastapi.testclient import TestClient

from app.services import site_accounts
from app.willhaben import session as session_mod


class _Sess:
    started = True

    def __init__(self, final, text=""):
        self.final, self.text, self.opened, self.loaded = final, text, [], []

    async def load_hidden(self, url, timeout_ms=30000, with_text=False):
        self.loaded.append(url)
        if isinstance(self.final, Exception):
            raise self.final
        return (self.final, self.text) if with_text else self.final

    async def open_url(self, url):
        self.opened.append(url)

    async def open_site(self, pid, url):
        self.opened.append(url)

    async def stop(self):
        pass


async def test_check_reads_the_redirect(monkeypatch):
    sess = _Sess("https://www.kleinanzeigen.de/m-nachrichten.html")
    monkeypatch.setattr(session_mod, "get_session", lambda: sess)
    assert site_accounts.state("kleinanzeigen")["logged_in"] is None
    assert (await site_accounts.check("kleinanzeigen"))["logged_in"] is True
    sess.final = "https://www.kleinanzeigen.de/m-einloggen.html?targetUrl=/m-nachrichten.html"
    assert (await site_accounts.check("kleinanzeigen"))["logged_in"] is False
    sess.final = "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox"
    assert (await site_accounts.check("vinted"))["logged_in"] is False
    sess.final, sess.text = "https://www.vinted.at/inbox", "Deine Sitzung wurde blockiert. Wir haben festgestellt …"
    st = await site_accounts.check("vinted")                      # the block page keeps the address
    assert st["logged_in"] is False and "blocks this browser" in st["error"]
    sess.text = "Katalog Registrieren | Einloggen Artikel verkaufen"   # the logged-out header on the inbox address
    assert (await site_accounts.check("vinted"))["logged_in"] is False
    sess.text = "Posteingang"
    assert (await site_accounts.check("vinted"))["logged_in"] is True
    sess.final = TimeoutError("slow")
    st = await site_accounts.check("vinted")
    assert st["logged_in"] is None and "slow" in st["error"]
    await site_accounts.open_login("vinted")
    assert sess.opened == [site_accounts.SITES["vinted"].login_url]


def test_settings_page_shows_the_account_card(monkeypatch):
    from app.main import app

    sess = _Sess("https://www.vinted.at/inbox")
    monkeypatch.setattr(session_mod, "get_session", lambda: sess)
    with TestClient(app) as c:
        page = c.get("/settings/vinted").text
        assert 'data-site-account="vinted"' in page and "not checked yet" in page
        r = c.post("/settings/account/vinted/check", follow_redirects=False)
        assert r.headers["location"].startswith("/settings/vinted?msg=Logged")
        assert "Vinted account: logged in" in c.get("/settings/vinted").text
        r = c.post("/settings/account/vinted/login", follow_redirects=False)
        assert r.headers["location"] == "/settings/vinted?remote=vinted" and sess.opened   # its own overlay
        assert c.post("/settings/account/ebay/check").status_code == 404
        assert "data-site-account" not in c.get("/settings/ebay").text


def test_capture_keeps_the_sites_requests():
    assert session_mod._other_site("https://gateway.kleinanzeigen.de/messagebox/api/x")
    assert session_mod._other_site("https://www.vinted.at/api/v2/inbox")
    assert not session_mod._other_site("https://www.example.com/kleinanzeigen.de")


def test_the_browser_window_opens_the_sites_message_pages(monkeypatch):
    from app.main import app

    sess = _Sess("")
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_session", lambda: sess)
    with TestClient(app) as c:
        for url in ("https://www.kleinanzeigen.de/m-nachrichten.html", "https://www.vinted.at/inbox"):
            c.post("/session/open", data={"url": url}, follow_redirects=False)
        r = c.post("/session/open", data={"url": "https://evil.example/"}, follow_redirects=False)
        assert "msg=Only" in r.headers["location"]
    assert sess.opened == ["https://www.kleinanzeigen.de/m-nachrichten.html", "https://www.vinted.at/inbox"]


def test_the_sites_own_overlay_shows_no_willhaben_state():
    from types import SimpleNamespace

    from app.config import get_settings
    from app.main import templates
    status = SimpleNamespace(started=True, logged_in=True, user={"nickname": "wh-user"}, last_check=None, error=None,
                             capturing=False, capture_count=0)
    page = templates.env.get_template("_session_panel.html").render(
        session_status=status, captures=[], settings=get_settings(), embed=True,
        site_mode=site_accounts.SITES["vinted"], site_state={"logged_in": False, "error": None}, site_checked=None,
        session_next="/session?embed=1&site=vinted")
    head = page[:page.index("</div>")]
    assert 'data-site-mode="vinted"' in head and "Vinted: not logged in" in head and "wh-user" not in head
    assert 'name="site" value="vinted"' in head and "/session/site-check/vinted" in head


async def test_a_site_gets_its_own_tab_and_willhaben_its_window_back():
    class Page:
        def __init__(self):
            self.urls, self.closed = [], False

        def is_closed(self):
            return self.closed

        def on(self, *a):
            pass

        async def bring_to_front(self):
            pass

        async def goto(self, url, **kw):
            self.urls.append(url)

    class Ctx:
        def __init__(self):
            self.made = []

        async def new_page(self):
            p = Page()
            self.made.append(p)
            return p

    sess = session_mod.BrowserSession.__new__(session_mod.BrowserSession)
    home = Page()
    sess.context, sess.ui_page, sess.home_page, sess.site_pages = Ctx(), home, home, {}
    sess._creating_internal, sess._ui_used_at, sess._ui_holds = False, 0, 0
    sess._require = lambda: None
    await sess.open_site("vinted", "https://www.vinted.at/member/login")
    tab = sess.ui_page
    assert tab is not home and tab.urls == ["https://www.vinted.at/member/login"] and home.urls == []
    await sess.open_url("https://www.vinted.at/inbox")                   # the same tab again
    assert sess.ui_page is tab and len(sess.context.made) == 1
    sess.show_home()
    assert sess.ui_page is home


def test_the_sites_tab_on_its_signup_page_means_logged_out():
    site_accounts._save("vinted", True)
    site_accounts.seen_in_tab("vinted", "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox")
    assert site_accounts.state("vinted")["logged_in"] is False
    site_accounts._save("vinted", True)
    site_accounts.seen_in_tab("vinted", "https://www.vinted.at/inbox")
    assert site_accounts.state("vinted")["logged_in"] is True


async def test_chrome_is_used_when_installed_else_chromium(monkeypatch):
    import shutil

    sess = session_mod.BrowserSession.__new__(session_mod.BrowserSession)
    sess.settings = type("S", (), {"browser_chrome": True, "browser_headless": False, "profile_dir": "/tmp/p"})()
    sess._pw = sess.context = None
    sess.engine = None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert await sess._launch_chrome() is False and sess.engine is None        # no Chrome: the bundled Chromium
    sess.settings.browser_chrome = False
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/google-chrome")
    assert await sess._launch_chrome() is False                                  # switched off


def test_browser_willhaben_and_vinted_have_separate_status_lines():
    from types import SimpleNamespace

    from app.db import set_setting
    from app.main import templates
    set_setting("platform_vinted_enabled", "1")
    site_accounts._save("vinted", False)
    status = SimpleNamespace(started=True, logged_in=True, user={"nickname": "wh"}, last_check=None, error=None,
                             capturing=False, capture_count=0, engine="chrome", chat_live={})
    req = SimpleNamespace(url=SimpleNamespace(path="/x"), query_params={}, headers={}, cookies={})
    page = templates.env.get_template("base.html").render(session_status=status, request=req)
    menu = page[page.index('id="account-menu"'):]
    assert 'data-status="browser"' in menu and "Browser: running (Chrome)" in menu
    assert 'data-status="willhaben"' in menu and "willhaben: logged in" in menu
    assert 'data-status="vinted"' in menu and "Vinted: not logged in" in menu
    assert 'href="/settings/vinted" role="menuitem"' in menu


async def test_a_recording_stops_even_when_an_answer_never_comes(monkeypatch, tmp_path):
    """10-28: a request of a tab closed meanwhile never got its answer, and stopping the recording waited for it
    for good."""
    import asyncio
    from types import SimpleNamespace

    from app.willhaben.session import BrowserSession

    monkeypatch.setattr(session_mod, "CAPTURE_STOP_WAIT_S", 0.05)
    sess = BrowserSession.__new__(BrowserSession)
    sess.context = SimpleNamespace(remove_listener=lambda *a: None)
    sess.settings = SimpleNamespace(captures_dir=tmp_path)
    sess._capture = {"label": "vinted test", "started_at": "x", "entries": [{"url": "https://www.vinted.at/api/v2/x"}]}
    stuck = asyncio.ensure_future(asyncio.sleep(3600))
    sess._capture_tasks = {stuck}
    path = await asyncio.wait_for(sess.stop_capture(), 2)
    assert path.exists() and "vinted-test" in path.name and sess._capture is None
    await asyncio.sleep(0)
    assert stuck.cancelled()
