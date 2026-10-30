"""willhaben access log: recording (anonymous client, browser session, live chat), kinds, outcomes, buckets, page, API."""
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db as dbmod
from app.db import AccessCount, set_setting
from app.services import access_stats
from app.willhaben import access_log
from app.willhaben import public as public_mod
from app.willhaben.public import Blocked, PublicClient, RateLimiter, WillhabenError
from app.willhaben.session import ORIGIN, BrowserSession

AD = "1000000001"
NOW = 1_791_000_000.0          # fixed "now" (UTC epoch seconds) for the bucket tests; a multiple of 7200
NOW_MIN = int(NOW // 60)


@pytest.fixture(autouse=True)
def clean_buffer():
    access_log.reset()
    yield
    access_log.reset()


def _rows():
    with dbmod.session() as db:
        return list(db.exec(select(AccessCount)))


def _counts():
    access_log.flush()
    out = {}
    for r in _rows():
        key = (r.channel, r.kind, r.outcome)
        out[key] = out.get(key, 0) + r.count
    return out


def _put(minute, channel="anonymous", kind="search", outcome="ok", count=1, platform="willhaben"):
    with dbmod.session() as db:
        db.add(AccessCount(minute=minute, platform=platform, channel=channel, kind=kind, outcome=outcome, count=count))
        db.commit()


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("url,kind", [
    ("https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=rad&rows=30", "search"),
    ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad", "search"),
    (f"https://www.willhaben.at/iad/object?adId={AD}", "detail"),
    (f"https://www.willhaben.at/iad/kaufen-und-verkaufen/d/ktm-rad-{AD}/", "detail"),
    ("/webapi/chat-api/v1/conversations?limit=20&offset=0", "chat"),
    ("/webapi/chat-api/v1/conversations/abc-123/messages", "chat"),
    ("wss://chat-realtime-web.willhaben.at/webapi/chat-ws/v1/ws/", "chat"),
    (f"/webapi/iad-messaging/conversation/init/{AD}?chatSystem=chat", "chat"),
    ("/webapi/iad-messaging/sendrequest/chat", "send"),
    ("/webapi/userprofile-service/userprofile/me", "login"),
    ("https://www.willhaben.at/webapi/sso/login?background-login", "login"),
    ("/iad/myprofile/login?r=/iad/myprofile/myadverts", "login"),
    ("/iad/myprofile/myadverts", "myads"),
    (f"/webapi/iad/bap/sold/{AD}", "myads"),
    (f"/webapi/iad/soldreasons?adId={AD}", "myads"),
    ("/webapi/iad/bap/categorysuggestion/attributetree?title=Rad&adTypeId=67", "categories"),
    ("/iad/anzeigenaufgabe/marktplatz?adTypeId=67&productId=67", "categories"),
    ("/webapi/iad/bap/presave", "publish"),
    ("/webapi/iad/bap/save", "publish"),
    (f"/webapi/iad/bap/{AD}", "publish"),
    (f"/webapi/myatz/bap/{AD}", "myads"),                               # delete own ad
    (f"/webapi/myatz/bap/deactivate/{AD}", "myads"),
    ("/webapi/iad/products/privates/carts/42/checkout", "publish"),
    ("/webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery", "publish"),
    ("/webapi/iad-upload/atimage/temp", "upload"),
    ("https://www.willhaben.at/robots.txt", "navigation"),
    ("https://www.willhaben.at/", "navigation"),
    ("/webapi/something/new", "other"),
])
def test_classify(url, kind):
    assert access_log.classify(url) == kind


def test_outcome_mapping():
    assert [access_log.outcome_for_status(s) for s in (None, 200, 204, 302, 400, 401, 403, 404, 429, 500, 503)] == [
        "ok", "ok", "ok", "ok", "client_error", "client_error", "blocked", "client_error", "blocked", "error", "error"]
    assert [access_log.ws_outcome(s) for s in ("ok", "success", "error", "timeout", "ws_error", "closed", None)] == [
        "ok", "ok", "client_error", "error", "error", "error", "error"]


def test_record_never_raises_and_only_stores_fixed_words(monkeypatch):
    access_log.record("anonymous", f"/iad/object?adId={AD}", "weird")      # free text collapses to fixed words
    access_log.record("someone@example.at", "search", "ok")
    monkeypatch.setattr(access_log, "now_minute", lambda: 1 / 0)
    access_log.record("anonymous", "search")                               # swallowed
    monkeypatch.undo()
    assert _counts() == {("anonymous", "other", "error"): 1, ("other", "search", "ok"): 1}


# ------------------------------------------------------------------ per platform
def test_counts_are_kept_per_platform():
    access_log.record("anonymous", "search", platform="kleinanzeigen")
    access_log.record("anonymous", "search", platform="kleinanzeigen")
    access_log.record("anonymous", "search")
    access_log.record("anonymous", "detail", "cached", platform="kleinanzeigen")
    access_log.record("anonymous", "search", platform="Bad Platform!")      # free text -> "other"
    assert access_stats.last_hour() == 1                                  # willhaben only: the footer gauge
    assert access_stats.last_hour(platform="kleinanzeigen") == 2
    assert access_stats.by_platform() == {"kleinanzeigen": 2, "willhaben": 1, "other": 1}
    access_log.flush()
    assert {(r.platform, r.channel, r.kind, r.outcome, r.count) for r in _rows()} == {
        ("kleinanzeigen", "anonymous", "search", "ok", 2), ("willhaben", "anonymous", "search", "ok", 1),
        ("kleinanzeigen", "anonymous", "detail", "cached", 1), ("other", "anonymous", "search", "ok", 1)}
    assert access_stats.last_hour(platform="kleinanzeigen") == 2 and access_stats.last_hour() == 1
    assert access_stats.by_platform() == {"kleinanzeigen": 2, "willhaben": 1, "other": 1}
    s = access_stats.series("1h", platform="kleinanzeigen")
    assert s["platform"] == "kleinanzeigen" and s["total"] == 2 and s["cached"] == 1 and s["last_hour"] == 2
    assert access_stats.series("1h")["total"] == 1
    with access_log.track("anonymous", "search", platform="kleinanzeigen") as tr:
        tr.status = 429
    assert access_stats.series("1h", platform="kleinanzeigen")["blocked"] == 1


# ------------------------------------------------------------------ recording: anonymous client
async def test_public_client_records(monkeypatch):
    status = {"next": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/boom":
            raise httpx.ConnectError("down")
        code, status["next"] = status["next"], 200
        return httpx.Response(code, json={"advertSummaryList": {"advertSummary": []}, "rowsFound": 0})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    monkeypatch.setattr(c.limiter, "penalize", lambda: 0.0)
    url = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=max+muster"
    await c.search(url)                                  # agent poll: uncached
    await c.search(url, cache_s=300)                     # one-off lookup: request, then cached
    await c.search(url, cache_s=300)
    await c.search_filters("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad")
    status["next"] = 404
    assert await c.get_detail(AD) is None
    status["next"] = 429
    with pytest.raises(Blocked):
        await c.search(url)
    with pytest.raises(WillhabenError):
        await c._get("https://www.willhaben.at/boom", json_api=True)
    await c.aclose()
    assert _counts() == {
        ("anonymous", "search", "ok"): 2, ("anonymous", "search", "cached"): 1, ("anonymous", "filters", "ok"): 1,
        ("anonymous", "detail", "client_error"): 1, ("anonymous", "search", "blocked"): 1,
        ("anonymous", "other", "error"): 1,
    }
    assert c.limiter.last_hour == 6                      # cache hits never pass the limiter


async def test_limiter_refusal_is_not_an_access():
    def handler(request):
        return httpx.Response(200, json={})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0, per_hour=1, max_wait_s=0)
    await c._get("https://www.willhaben.at/webapi/ad-search/search/x", json_api=True)
    with pytest.raises(Blocked):
        await c._get("https://www.willhaben.at/webapi/ad-search/search/y", json_api=True)
    await c.aclose()
    assert _counts() == {("anonymous", "search", "ok"): 1}


# ------------------------------------------------------------------ recording: logged-in browser
class _Resp:
    def __init__(self, status):
        self.status = status


class _FakePage:
    def __init__(self, results):
        self.url = ORIGIN + "/robots.txt"
        self.results = list(results)
        self.gotos = []

    async def evaluate(self, js, arg=None):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url
        return _Resp(200)

    async def bring_to_front(self):
        pass

    async def go_back(self, **kw):
        return _Resp(200)

    async def reload(self, **kw):
        return None                                                    # Playwright: no response (e.g. cached)

    async def wait_for_load_state(self, *a, **kw):
        pass


class _FakeContext:
    async def cookies(self, origin):
        return []


def _session(results):
    s = BrowserSession()
    s.limiter = RateLimiter(0)
    s.context = _FakeContext()
    s.api_page = _FakePage(results)
    s.ui_page = _FakePage([])
    return s


async def test_browser_session_records(monkeypatch):
    s = _session([
        {"status": 200, "text": "{}", "url": "x"},                    # chat list
        {"status": 200, "text": "<html>", "url": "x"},                 # my adverts page
        {"status": 429, "text": "", "url": "x"},                       # throttled
        {"status": 201, "text": "{}", "url": "x"},                     # upload
        RuntimeError("page crashed"),
    ])
    monkeypatch.setattr(s.limiter, "penalize", lambda: 0.0)
    await s.api("GET", "/webapi/chat-api/v1/conversations?limit=20&offset=0")
    await s.api("GET", "/iad/myprofile/myadverts", accept="text/html")
    with pytest.raises(Blocked):
        await s.api("PUT", f"/webapi/iad/bap/sold/{AD}")
    await s.upload("/webapi/iad-upload/atimage/temp", "file", "foto1.jpg", b"x", "image/jpeg")
    with pytest.raises(RuntimeError):
        await s.api("GET", "/webapi/userprofile-service/userprofile/me")
    s.api_page.url = "about:blank"                                      # next call parks the page first
    s.api_page.results.append({"status": 204, "text": "", "url": "x"})
    await s.api("GET", "/webapi/userprofile-service/userprofile/me")
    await s.open_login()
    assert _counts() == {
        ("account", "chat", "ok"): 1, ("account", "myads", "ok"): 1, ("account", "myads", "blocked"): 1,
        ("account", "upload", "ok"): 1, ("account", "login", "error"): 1, ("account", "login", "ok"): 2,
        ("account", "navigation", "ok"): 1,
    }


async def test_remote_back_and_reload_are_counted():
    s = _session([])
    await s.remote_input({"type": "back"})
    await s.remote_input({"type": "reload"})
    assert _counts() == {("account", "navigation", "ok"): 2}


async def test_screening_cdn_images_are_counted(monkeypatch, tmp_path):
    from app.services import screening

    def handler(req):
        if req.url.host == "cache.willhaben.at" and req.url.path.endswith("broken.jpg"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")

    monkeypatch.setattr(screening, "_transport", httpx.MockTransport(handler))
    assert await screening._download("https://cache.willhaben.at/mmo/1/000/000/001_1.jpg", tmp_path, "a")
    assert await screening._download("https://cache.willhaben.at/mmo/1/000/000/001_broken.jpg", tmp_path, "b") is None
    assert await screening._download("https://example.org/foto.jpg", tmp_path, "c")      # not willhaben
    assert _counts() == {("anonymous", "other", "ok"): 1, ("anonymous", "other", "error"): 1}


async def test_live_chat_counts_connects_not_frames():
    s = BrowserSession()
    for raw in ['{"_state": "open"}', '{"req_type": "ping"}', '{"conversation_id": "c-1", "req_type": "x"}',
                '{"_state": "closed", "code": 1006}',     # disconnect after a live connection: not an attempt
                '{"_state": "closed", "code": 1006}',     # reconnect attempt failed
                '{"_state": "open"}']:
        await s._on_chat_frame(raw)
    assert _counts() == {("chat", "chat", "ok"): 2, ("chat", "chat", "error"): 1}


# ------------------------------------------------------------------ aggregation
@pytest.mark.parametrize("rng,n,bucket", [("1h", 60, 60), ("6h", 72, 300), ("24h", 96, 900), ("7d", 84, 7200)])
def test_empty_ranges(rng, n, bucket):
    d = access_stats.series(rng, now=NOW)
    assert len(d["buckets"]) == n and d["bucket_s"] == bucket
    assert d["total"] == 0 and d["peak"] is None and d["blocked"] == 0
    assert (d["budget_per_bucket"] is None) == (rng == "7d")


def test_bucket_edges_and_totals():
    # 24h range, 15-min buckets; NOW is on a bucket boundary -> the current bucket starts at NOW
    first_min = NOW_MIN - 95 * 15
    _put(first_min - 1, count=50)                                        # just before the range: ignored
    _put(first_min, count=2)                                             # first bucket
    _put(first_min + 14, "account", "chat", count=3)                     # still the first bucket
    _put(first_min + 15, "chat", "send", count=1)                        # second bucket
    _put(NOW_MIN, "anonymous", "search", "blocked", count=4)             # current bucket
    _put(NOW_MIN, "anonymous", "detail", "cached", count=7)              # cache: shown, not counted
    _put(NOW_MIN + 1, count=99)                                          # future: ignored
    d = access_stats.series("24h", now=NOW)
    b = d["buckets"]
    assert (b[0]["anonymous"], b[0]["account"], b[0]["total"]) == (2, 3, 5)
    assert (b[1]["chat"], b[1]["total"]) == (1, 1)
    assert (b[-1]["total"], b[-1]["blocked"], b[-1]["cached"]) == (4, 4, 7)
    assert d["total"] == 10 and d["blocked"] == 4 and d["cached"] == 7
    assert d["by_channel"] == {"anonymous": 6, "account": 3, "chat": 1}
    assert d["by_kind"] == {"search": 6, "chat": 3, "send": 1}
    assert d["by_outcome"]["cached"] == 7 and d["by_outcome"]["ok"] == 6
    assert d["peak"]["total"] == 5 and d["peak"]["start"] == b[0]["start"]
    assert d["budget"] == 240 and d["budget_per_bucket"] == 60
    assert b[0]["start"].endswith("+00:00")                              # UTC in data, local labels for display


def test_series_includes_unflushed_buffer(monkeypatch):
    monkeypatch.setattr(access_log, "now_minute", lambda: NOW_MIN)
    access_log.record("anonymous", "search")
    assert access_stats.series("1h", now=NOW)["total"] == 1
    assert access_stats.last_hour(NOW) == 1
    access_log.flush()
    assert access_stats.series("1h", now=NOW)["total"] == 1             # not counted twice


def test_last_hour_window():
    _put(NOW_MIN - 60, count=5)                                          # 61st minute back: outside
    _put(NOW_MIN - 59, count=2)
    _put(NOW_MIN, count=1)
    _put(NOW_MIN, outcome="cached", count=9)
    assert access_stats.last_hour(NOW) == 3


def test_footer_leaves_out_unpaced_reconnects(client):
    access_log.record("anonymous", "search")
    access_log.record("chat", "send", "error")
    for _ in range(5):
        access_log.record("chat", "chat", "error")                   # failed live-chat reconnects
    assert access_stats.last_hour() == 7
    assert access_stats.last_hour(exclude=access_stats.UNPACED) == 2
    access_log.flush()
    assert access_stats.last_hour(exclude=access_stats.UNPACED) == 2
    assert "2 / 240 willhaben requests in the last hour" in client.get("/stats/willhaben?range=1h").text


def test_7d_buckets_align_to_local_time():
    winter = 1_798_000_000.0 - 1_798_000_000.0 % 7200                  # Dec 2026, UTC+1; a UTC multiple of 2 h
    for now in (NOW, winter, winter + 3600):
        b = access_stats.series("7d", now=now)["buckets"]
        assert all(x["label"].endswith((":00",)) and int(x["label"][-5:-3]) % 2 == 0 for x in b)
    _put(int(winter // 60) - 90, count=4)                                # 03:30 local: in the 02-04 bar
    d = access_stats.series("7d", now=winter)                            # now = 05:00 local, current bar 04-06
    assert d["buckets"][-2]["total"] == 4 and d["buckets"][-2]["label"].endswith("02:00")


def test_series_sums_rows_in_buckets():
    for m in range(NOW_MIN - 119, NOW_MIN + 1):                           # 2 h of per-minute rows, 2 combos each
        _put(m, count=1)
        _put(m, "account", "chat", count=2)
    d = access_stats.series("7d", now=NOW)
    assert d["total"] == 360 and d["by_channel"] == {"anonymous": 120, "account": 240, "chat": 0}
    assert sum(b["total"] for b in d["buckets"]) == 360


def test_prune_keeps_eight_days():
    _put(NOW_MIN - 8 * 1440 - 1)
    _put(NOW_MIN - 8 * 1440)
    _put(NOW_MIN)
    assert access_log.prune(NOW_MIN) == 1
    assert sorted(r.minute for r in _rows()) == [NOW_MIN - 8 * 1440, NOW_MIN]


async def test_flush_job_writes_and_prunes(monkeypatch):
    _put(access_log.now_minute() - 9 * 1440)
    access_log.record("anonymous", "search")
    access_log.record("anonymous", "search")
    monkeypatch.setattr(access_log, "_last_prune", [-1e9])
    await access_log.flush_job()
    rows = _rows()
    assert [(r.kind, r.count) for r in rows] == [("search", 2)] and not access_log.pending()


def test_chart_geometry():
    _put(NOW_MIN, count=3)
    _put(NOW_MIN, "account", "chat", count=1)
    d = access_stats.series("1h", now=NOW)
    ch = access_stats.chart(d)
    last = ch["bars"][-1]
    assert [s["key"] for s in last["segs"]] == ["anonymous", "account"]
    assert last["segs"][1]["y"] < last["segs"][0]["y"]                  # stacked upwards
    assert last["segs"][1]["y"] + last["segs"][1]["h"] == pytest.approx(last["segs"][0]["y"] - access_stats.SEG_GAP, abs=0.02)
    assert ch["budget_y"] is not None and ch["yticks"][0]["label"] == "0"
    assert all(not b["segs"] for b in ch["bars"][:-1])


# ------------------------------------------------------------------ privacy
async def test_no_personal_data_stored():
    def handler(request):
        return httpx.Response(200, json={"advertSummaryList": {"advertSummary": []}})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    await c._get(f"https://www.willhaben.at/iad/object?adId={AD}", json_api=False)
    await c._get("https://www.willhaben.at/webapi/ad-search/search/x?keyword=Max%20Muster&PRICE_TO=200", json_api=True)
    await c.aclose()
    s = _session([{"status": 200, "text": "[]", "url": "x"}])
    await s.api("GET", "/webapi/chat-api/v1/conversations/conv-1000000001/messages?limit=10&offset=0")
    access_log.flush()
    rows = _rows()
    assert rows
    for r in rows:
        for v in (r.channel, r.kind, r.outcome):
            assert not re.search(r"\d|[?=/&@]|max|muster", v, re.I), v
        assert r.channel in access_log.CHANNELS and r.kind in access_log.KINDS and r.outcome in access_log.OUTCOMES
        assert r.platform == "willhaben"
    cols = {c.name for c in AccessCount.__table__.columns}
    assert cols == {"minute", "platform", "channel", "kind", "outcome", "count"}


# ------------------------------------------------------------------ page, footer, API
@pytest.fixture
def client():
    def handler(request):
        return httpx.Response(200, json={"advertSummaryList": {"advertSummary": []}})

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    from app.main import app

    with TestClient(app) as c:
        yield c
    public_mod.set_public(None)


@pytest.mark.parametrize("rng", ["1h", "6h", "24h", "7d"])
def test_page_renders_each_range(client, rng):
    access_log.record("anonymous", "search")
    access_log.record("account", "chat", "blocked")
    r = client.get(f"/stats/willhaben?range={rng}")
    assert r.status_code == 200
    assert "<svg viewBox" in r.text and "<title>" in r.text and 'aria-current="true">' + rng in r.text
    assert 'data-outcome="blocked"' in r.text


def test_page_empty_state(client):
    r = client.get("/stats/willhaben?range=bogus")
    assert r.status_code == 200 and "<svg viewBox" not in r.text and "No willhaben requests logged" in r.text
    assert 'href="/stats/willhaben"' in r.text                         # footer counter links here


def test_footer_count_survives_restart(client):
    for _ in range(3):
        access_log.record("anonymous", "search")
    access_log.record("anonymous", "detail", "cached")
    access_log.flush()
    access_log.reset()                                                   # "restart": empty buffer, fresh limiter
    public_mod._limiter = None
    assert public_mod.willhaben_limiter().last_hour == 0
    r = client.get("/stats/willhaben?range=1h")
    assert "3 / 240 willhaben requests in the last hour" in r.text
    public_mod._limiter = None


def test_api_shape(client):
    access_log.record("anonymous", "search")
    d = client.get("/api/stats/willhaben?range=24h").json()
    assert d["range"] == "24h" and d["bucket_s"] == 900 and len(d["buckets"]) == 96
    assert set(d["buckets"][0]) == {"start", "total", "blocked", "cached", "anonymous", "account", "chat"}
    assert d["total"] == 1 and d["by_channel"]["anonymous"] == 1 and d["by_kind"] == {"search": 1}
    assert {"budget", "budget_per_bucket", "last_hour", "peak", "by_outcome", "blocked", "cached"} <= set(d)


# ------------------------------------------------------------------ per site and per type
def test_every_kind_has_a_type():
    assert set(access_stats.TYPE_OF) == set(access_log.KINDS) and set(access_stats.TYPE_OF.values()) == set(access_stats.TYPES)
    assert [access_stats.TYPE_OF[k] for k in ("filters", "send", "chat", "navigation")] == ["search", "messages", "messages", "account"]


def test_other_site_stacks_by_type():
    _put(NOW_MIN, count=3, platform="ebay")
    _put(NOW_MIN, kind="detail", count=2, platform="ebay")
    _put(NOW_MIN, kind="detail", outcome="blocked", platform="ebay")
    _put(NOW_MIN, kind="other", outcome="cached", count=4, platform="ebay")
    d = access_stats.series("1h", now=NOW, platform="ebay", by="channel")               # channels are willhaben's only
    assert d["stack"] == "type" and d["stacks"] == ["type"] and d["stack_keys"] == ["search", "detail"]
    assert d["by_type"] == {"search": 3, "detail": 3, "photos": 0, "messages": 0, "account": 0, "other": 0}
    assert d["type_outcomes"]["detail"]["requests"] == 3 and d["type_outcomes"]["detail"]["blocked"] == 1
    assert d["type_outcomes"]["other"]["cached"] == 4 and d["type_outcomes"]["other"]["requests"] == 0
    assert d["type_kinds"] == {} and d["budget"] == 240                                # willhaben's: none of its own
    assert d["buckets"][-1]["by_type"]["detail"] == 3 and d["buckets"][-1]["blocked_by"] == {"detail": 1}
    bar = access_stats.chart(d)["bars"][-1]
    assert [s["key"] for s in bar["segs"]] == ["search", "detail"]
    assert "6 eBay requests" in bar["title"] and "1 blocked (ad detail 1)" in bar["title"]


def test_willhaben_stacks_by_channel_or_type():
    _put(NOW_MIN, count=2)
    _put(NOW_MIN, "anonymous", "filters")
    _put(NOW_MIN, "account", "myads")
    _put(NOW_MIN, "chat", "send")
    d = access_stats.series("1h", now=NOW)
    assert d["stack"] == "channel" and d["stacks"] == ["channel", "type"] and d["stack_keys"] == ["anonymous", "account", "chat"]
    ty = access_stats.series("1h", now=NOW, by="type")
    assert ty["stack_keys"] == ["search", "messages", "account"]
    assert ty["by_type"] == {"search": 3, "detail": 0, "photos": 0, "messages": 1, "account": 1, "other": 0}
    assert ty["type_kinds"] == {"search": {"search": 2, "filters": 1}, "messages": {"send": 1}, "account": {"myads": 1}}
    assert [s["key"] for s in access_stats.chart(ty)["bars"][-1]["segs"]] == ["search", "messages", "account"]


def test_all_sites_series():
    _put(NOW_MIN, count=5)
    _put(NOW_MIN, outcome="blocked", count=2, platform="kleinanzeigen")
    _put(NOW_MIN, kind="detail", platform="kleinanzeigen")
    _put(NOW_MIN, outcome="cached", count=3, platform="kleinanzeigen")
    _put(NOW_MIN, platform="other")                                                         # a name the registry does not know
    d = access_stats.series("1h", now=NOW, platform=access_stats.ALL, by="channel")
    assert d["stack"] == "type" and d["stacks"] == ["type"] and d["stack_keys"] == ["search", "detail"]
    assert (d["total"], d["blocked"], d["cached"], d["last_hour"]) == (9, 2, 3, 9)
    assert d["budget"] is None and d["budget_per_bucket"] is None and d["type_cols"] == ["search", "detail"]
    assert d["buckets"][-1]["by_site"] == {"willhaben": 5, "kleinanzeigen": 3, "other": 1}
    rows = {r["id"]: r for r in d["sites"]}
    assert list(rows) == ["willhaben", "kleinanzeigen", "other"]                              # off and silent: left out
    ka = rows["kleinanzeigen"]
    assert (ka["total"], ka["blocked"], ka["cached"], ka["by_type"]["detail"], ka["budget"], ka["enabled"], ka["known"]) == \
        (3, 2, 3, 1, 240, False, True)
    assert rows["willhaben"]["last_hour"] == 5 and rows["other"]["known"] is False
    assert d["spark_bucket_s"] == 300 and len(ka["spark"]) == 12 and (ka["spark"][-1]["total"], ka["spark"][-1]["blocked"]) == (3, 2)
    ch = access_stats.chart(d)
    assert [s["key"] for s in ch["bars"][-1]["segs"]] == ["search", "detail"]
    assert "willhaben 5, Kleinanzeigen 3, other 1" in ch["bars"][-1]["title"]
    sp = ch["sparks"]["kleinanzeigen"]
    assert sp["budget_y"] is not None and sp["bars"][-1]["bh"] > 0 and ch["sparks"].get("shpock") is None


def test_hour_by_platform_leaves_out_cache_and_unpaced():
    access_log.record("anonymous", "search")
    access_log.record("chat", "chat", "error")                                              # unpaced reconnect
    access_log.record("anonymous", "search", "blocked", platform="ebay")
    access_log.record("anonymous", "detail", "cached", platform="ebay")
    want = {"willhaben": {"n": 1, "blocked": 0}, "ebay": {"n": 1, "blocked": 1}}
    assert access_stats.hour_by_platform() == want
    access_log.flush()
    assert access_stats.hour_by_platform() == want


def test_site_usage_tone(monkeypatch):
    import time

    from app import platforms
    set_setting("platform_ebay_per_hour", "40")                                         # a budget of its own
    for _ in range(38):
        access_log.record("anonymous", "search", platform="ebay")                          # 38 of 40 = 95 %
    u = {x["id"]: x for x in access_stats.site_usage()}
    assert (u["ebay"]["n"], u["ebay"]["pct"], u["ebay"]["tone"]) == (38, 95, "full") and u["vinted"]["tone"] == "ok"
    monkeypatch.setattr(platforms.get("vinted").limiter, "_cooldown_until", time.monotonic() + 120)
    assert {x["id"]: x for x in access_stats.site_usage()}["vinted"]["tone"] == "full"


def test_all_sites_page(client):
    access_log.record("anonymous", "search")
    access_log.record("anonymous", "search", "blocked", platform="kleinanzeigen")
    page = client.get("/stats/requests?platform=all&range=1h").text
    assert "<title>Requests to the sites" in page and 'value="all" selected' in page
    assert 'data-site="willhaben"' in page and 'data-site="kleinanzeigen"' in page
    assert 'href="/stats/requests?platform=kleinanzeigen&range=1h"' in page and "fill-type-search" in page
    assert '<svg aria-hidden="true"' in page and 'data-outcome="blocked"' in page and 'id="by-type"' not in page


def test_all_sites_empty_state(client):
    page = client.get("/stats/requests?platform=all&range=1h").text
    assert "No requests to any site logged" in page and 'data-site="willhaben"' in page and "<svg viewBox" not in page


def test_willhaben_type_view(client):
    access_log.record("anonymous", "detail")
    page = client.get("/stats/requests?platform=willhaben&range=1h&by=type").text
    assert "fill-type-detail" in page and 'data-type="detail"' in page and 'data-legend="detail"' in page
    assert 'href="/stats/requests?platform=willhaben&range=6h&by=type"' in page            # the range keeps the stacking
    assert 'platform=willhaben&amp;range=1h"' in page                                      # "By channel" (ui.seg escapes &)
    plain = client.get("/stats/requests?platform=willhaben&range=1h").text
    assert "fill-ch-anonymous" in plain and 'href="/stats/requests?platform=willhaben&range=6h"' in plain
    assert 'platform=willhaben&amp;range=1h&amp;by=type"' in plain
    ebay = client.get("/stats/requests?platform=ebay&range=1h&by=channel").text
    assert "By channel" not in ebay and "eBay requests" in ebay


def test_default_view_and_api(client):
    from app.db import set_setting

    assert 'value="willhaben" selected' in client.get("/stats/requests").text              # only willhaben on
    set_setting("platform_ebay_enabled", "1")
    assert 'value="all" selected' in client.get("/stats/requests").text
    access_log.record("anonymous", "search", platform="kleinanzeigen")
    d = client.get("/api/stats/requests?platform=all&range=1h").json()
    assert d["platform"] == "all" and d["stack"] == "type" and [s["id"] for s in d["sites"]] == ["willhaben", "kleinanzeigen", "ebay"]
    assert d["by_site"] == {"willhaben": 0, "kleinanzeigen": 1, "ebay": 0} and len(d["sites"][0]["spark"]) == 12
    assert set(d["sites"][0]["spark"][0]) == {"start", "total", "blocked"} and "sparks" not in d
    assert client.get("/api/stats/requests?platform=willhaben&by=type").json()["stack"] == "type"
    assert "by_type" not in client.get("/api/stats/willhaben?range=1h").json()["buckets"][0]


def test_css_has_the_type_colours():                                                     # catches a forgotten scripts/build-css.sh
    from pathlib import Path
    css = (Path(access_stats.__file__).resolve().parents[1] / "static" / "app.css").read_text(encoding="utf-8")
    for ty in access_stats.TYPES:
        assert f".fill-type-{ty}" in css and f".bg-type-{ty}" in css, ty


def test_photos_have_their_own_category():
    """An ad's photo is "image" (Fotos von Anzeigen) on every site, not "other"."""
    assert access_log.classify("https://cache.willhaben.at/mmo/1/123/456/789_123_hoved.jpg") == "image"
    assert access_log.image_kind("https://img.shpock.com/12.34/abcdef") == "image"
    assert access_log.image_kind("https://scontent-vie1-1.xx.fbcdn.net/v/t39/1_2_n.jpg") == "image"
    assert access_log.image_kind("https://images1.vinted.net/t/01_abc/f800/1.webp") == "image"
    assert access_log.classify("https://www.willhaben.at/webapi/iad/atimage/123") == "myads"
    assert access_log.image_kind("https://www.willhaben.at/iad/object?adId=1") is None
    assert access_stats.TYPE_OF["image"] == "photos" and "image" in access_log.KINDS
