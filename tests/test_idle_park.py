import time

from app.willhaben import session as session_mod
from app.willhaben.session import BrowserSession


class _Page:
    def __init__(self, url="https://www.willhaben.at/"):
        self.url, self.visits, self.closed = url, [], False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def goto(self, url, **kw):
        self.visits.append(url)
        self.url = url


def _sess(page, idle_for):
    s = BrowserSession()
    s.ui_page = page
    s._ui_used_at = time.monotonic() - idle_for
    return s


async def test_idle_visible_page_is_parked(monkeypatch):
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    page = _Page()
    assert not await _sess(page, 60).park_idle_ui()                     # used a minute ago: stays
    s = _sess(page, session_mod.UI_IDLE_S + 1)
    assert await s.park_idle_ui() and page.visits == ["about:blank"]
    assert not await s.park_idle_ui() and page.visits == ["about:blank"]   # already parked: nothing to do


async def test_page_in_use_or_watched_is_not_parked(monkeypatch):
    page = _Page()
    s = _sess(page, session_mod.UI_IDLE_S + 1)
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: True)
    assert not await s.park_idle_ui()                                    # someone watches in noVNC
    assert time.monotonic() - s._ui_used_at < 5                          # ... which counts as use
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    s._ui_used_at -= session_mod.UI_IDLE_S + 1
    with s.ui_in_use():                                                  # e.g. PayLivery waiting for the bank
        assert not await s.park_idle_ui()
    s._ui_used_at -= session_mod.UI_IDLE_S + 1
    s._capture = {"entries": []}                                         # request recording running
    assert not await s.park_idle_ui()
    assert page.visits == []


def test_vnc_viewer_detection(tmp_path, monkeypatch):
    head = "  sl  local_address rem_address   st\n"
    tcp = tmp_path / "tcp"
    tcp.write_text(head + "   0: 0100007F:170C 00000000:0000 0A\n")              # x11vnc listening only
    real = session_mod.Path
    monkeypatch.setattr(session_mod, "Path", lambda n: real(tcp) if n == "/proc/net/tcp" else real(tmp_path / "none"))
    assert not session_mod.vnc_viewer_connected()
    tcp.write_text(head + "   1: 0100007F:170C 0100007F:D431 01\n")              # websockify connected
    assert session_mod.vnc_viewer_connected()


async def test_the_pages_behind_the_visible_one_are_parked_too(monkeypatch):
    """10-26: the Kleinanzeigen tab was the visible one; the willhaben home behind it and the Vinted tab kept their ads
    (25 renderer processes, 1.2 GB). All shown pages go; the working pages (API, live chat) stay."""
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    home, ka, vinted = _Page(), _Page("https://www.kleinanzeigen.de/m-nachrichten.html"), _Page("https://www.vinted.at/inbox")
    api, chat = _Page("https://www.willhaben.at/robots.txt"), _Page("https://www.willhaben.at/robots.txt")
    s = _sess(ka, session_mod.UI_IDLE_S + 1)
    s.home_page, s.site_pages, s.api_page, s.chat_page = home, {"kleinanzeigen": ka, "vinted": vinted}, api, chat
    assert await s.park_idle_ui()
    assert home.visits == ka.visits == vinted.visits == ["about:blank"] and api.visits == chat.visits == []


async def test_stray_tabs_are_closed_when_idle(monkeypatch):
    """A popup or a link opened in the remote view stayed open for good; idle, it goes – the app's pages stay."""
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    ui, api, stray = _Page(), _Page("https://www.willhaben.at/robots.txt"), _Page("https://example.com/")

    class Ctx:
        pages = [ui, api, stray]
    s = _sess(ui, session_mod.UI_IDLE_S + 1)
    s.api_page, s.context = api, Ctx()
    assert await s.park_idle_ui()
    assert stray.closed and not ui.closed and not api.closed


def test_describe_names_hosts_and_frames_only():
    class F:
        def __init__(self, url):
            self.url = url

    class P(_Page):
        def __init__(self, url, frames):
            super().__init__(url)
            self.frames = [F(u) for u in frames]

    class Ctx:
        service_workers = []
    s = BrowserSession()
    s.api_page = P("https://www.willhaben.at/robots.txt", ["https://www.willhaben.at/robots.txt"])
    s.ui_page = P("https://www.willhaben.at/iad/x?secret=1", ["https://www.willhaben.at/iad/x?secret=1",
                                                              "https://ads.example.com/frame"])
    Ctx.pages = [s.api_page, s.ui_page]
    s.context = Ctx()
    text = s.describe()
    assert text.startswith("2 page(s): api=www.willhaben.at[1 frames: www.willhaben.at]")
    assert "ui=www.willhaben.at[2 frames: ads.example.com, www.willhaben.at]" in text and "secret" not in text


async def test_working_pages_are_neither_parked_nor_closed(monkeypatch):
    """10-27: Vinted's live tab counted as a stray tab and was closed – the live connection with it."""
    monkeypatch.setattr(session_mod, "vnc_viewer_connected", lambda: False)
    ui, live = _Page(), _Page("https://www.vinted.at/inbox")

    class Ctx:
        pages = [ui, live]
    s = _sess(ui, session_mod.UI_IDLE_S + 1)
    s.context = Ctx()
    s.working_pages.add(live)
    assert await s.park_idle_ui()
    assert not live.closed and live.visits == [] and ui.visits == ["about:blank"]


def test_the_live_tab_blocks_ad_hosts_only():
    from app.platforms import vinted_inbox
    block = vinted_inbox._AD_HOSTS.search
    assert block("https://aax-eu.amazon-adsystem.com/e/dtb/bid") and block("https://gum.criteo.com/sid/json")
    assert block("https://securepubads.g.doubleclick.net/tag/js/gpt.js")
    assert not block("https://www.vinted.at/api/v2/inbox") and not block("https://geo.captcha-delivery.com/captcha/")
    assert not block("https://www.vinted.at/?ref=amazon-adsystem.com")
