"""The browser heals what closed under the app (10-30: preparing a draft failed with "Target page, context or browser
has been closed", and every willhaben request after it, until the app was restarted)."""
import pytest

from app.willhaben.public import RateLimiter
from app.willhaben.session import ORIGIN, PARK_URL, BrowserSession, SessionUnavailable


class _Page:
    def __init__(self, url=PARK_URL, answer=None, fail=None):
        self.url, self.closed, self.answer, self.fail = url, False, answer, fail

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        pass

    async def close(self):
        self.closed = True

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, js, arg=None):
        if self.fail:
            self.closed = True
            raise self.fail
        return self.answer


class _Context:
    def __init__(self, new_pages=()):
        self.new_pages, self.handlers = list(new_pages), {}

    async def new_page(self):
        page = self.new_pages.pop(0)
        if not page.closed:
            self.handlers.get("page", lambda p: None)(page)     # what Playwright does: the "page" event
        return page

    async def cookies(self, origin):
        return []

    def on(self, event, handler):
        self.handlers[event] = handler

    async def close(self):
        pass


def _sess(context, api):
    s = BrowserSession()
    s.limiter = RateLimiter(0)
    s.context, s.api_page, s.ui_page = context, api, _Page(ORIGIN + "/")
    context.on("page", s._on_new_page)
    return s


async def test_a_closed_api_page_is_opened_again():
    fresh = _Page("about:blank", answer={"status": 200, "text": "{}", "url": ORIGIN + "/webapi/x"})
    old = _Page()
    old.closed = True
    s = _sess(_Context([fresh]), old)
    ui = s.ui_page
    res = await s.api("GET", "/webapi/x")
    assert res.status == 200 and s.api_page is fresh and fresh.url == PARK_URL
    assert s.ui_page is ui                                    # the remote view does not jump to the new page


async def test_a_page_closing_during_the_request_is_session_unavailable():
    """Playwright's and patchright's errors are no RuntimeError: the prepare job left no note in the row."""
    s = _sess(_Context(), _Page(fail=Exception("Page.evaluate: Target page, context or browser has been closed")))
    with pytest.raises(SessionUnavailable, match="has been closed"):
        await s.api("GET", "/webapi/x")


async def test_a_crashed_browser_starts_again_on_the_next_request(monkeypatch):
    s = _sess(_Context(), _Page())
    s._on_context_closed()                                    # Chrome gone without stop()
    assert not s.started and s.api_page is None
    with pytest.raises(SessionUnavailable, match="starts again"):
        s._require()

    async def start():
        s.context, s.api_page, s.ui_page = _Context(), _Page(answer={"status": 200, "text": "", "url": ""}), _Page()
    monkeypatch.setattr(s, "start", start)
    assert (await s.api("GET", "/webapi/x")).status == 200 and s.started and not s._lost


async def test_starting_by_hand_after_a_crash_is_no_second_restart(monkeypatch):
    """"Start browser" after a crash: the old driver goes, and the next request does not start the browser again."""
    s = _sess(_Context(), _Page())
    stopped = []

    class _Driver:
        async def stop(self):
            stopped.append(True)
    s._pw = _Driver()
    s._on_context_closed()
    monkeypatch.setattr(s, "_launch_chrome", _launched(s))
    await s.start()
    assert stopped == [True] and not s._lost and s.started
    started_with = s.context
    await s._ready()
    assert s.context is started_with


def _launched(s):
    async def launch():
        s._pw, s.context = None, _Context([_Page("about:blank")])
        s.context.pages = [_Page(ORIGIN + "/")]
        s.engine = "chrome"
        return True
    return launch


async def test_stopping_on_purpose_is_no_crash():
    s = _sess(_Context(), _Page())
    s.context.close = lambda: s._on_context_closed() or _noop()
    await s.stop()
    assert not s._lost


async def _noop():
    pass
