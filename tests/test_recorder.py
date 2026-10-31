"""Every willhaben request is stored with its answer: keys, redaction, caps, both clients, the timeline, pruning."""
import asyncio
import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import TimelineEvent, WhRecording, session, utcnow
from app.services import timeline
from app.willhaben import recorder
from app.willhaben.public import Blocked, PublicClient, RateLimiter, WillhabenError
from app.willhaben.session import ORIGIN, BrowserSession

SEARCH = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=rad&rows=30"


@pytest.fixture(autouse=True)
def recording_on():
    """Both are opt-in; these tests switch them on (test_timeline.py checks the default)."""
    timeline.set_recording(timeline=True, willhaben=True)


def _recs() -> list[WhRecording]:
    with session() as db:
        return list(db.exec(select(WhRecording).order_by(WhRecording.id)))


def _event(rec: WhRecording) -> TimelineEvent:
    with session() as db:
        return db.get(TimelineEvent, rec.event_id)


# ------------------------------------------------------------------ pure helpers
def test_canonical_key_ignores_order_volatile_params_and_ids():
    k = recorder.canonical_key
    assert k("GET", "https://www.willhaben.at/x?b=2&a=1&_=123") == k("get", "https://WWW.willhaben.at/x/?a=1&b=2&ts=9")
    assert k("GET", SEARCH) != k("GET", SEARCH.replace("rows=30", "rows=5"))          # rows changes the answer
    assert k("GET", SEARCH + "&page=2") != k("GET", SEARCH)
    assert k("POST", "https://x/a", {"text": "hi", "request_id": "r1"}) == k("POST", "https://x/a", '{"request_id": "r2", "text": "hi"}')
    assert k("POST", "https://x/a", {"text": "hi"}) != k("POST", "https://x/a", {"text": "ho"})
    assert k("GET", "https://x/a") != k("POST", "https://x/a")
    up = k("UPLOAD", "https://x/u", b"\xff\xd8 photo 1", upload=("file", "image/jpeg"))
    assert up == k("UPLOAD", "https://x/u", b"\xff\xd8 another photo", upload=("file", "image/jpeg"))


def test_headers_redacted():
    h = recorder.redact_headers({"Cookie": "bbxid=1", "x-bbx-csrf-token": "t", "Authorization": "Bearer x", "accept": "json"})
    assert h == {"Cookie": "<redacted>", "x-bbx-csrf-token": "<redacted>", "Authorization": "<redacted>", "accept": "json"}


def test_bodies_capped_compressed_and_images_hashed_only():
    blob, size, cut, digest = recorder.prepare_body('{"a": 1}', "application/json")
    assert recorder.decode_body(blob) == b'{"a": 1}' and size == 8 and not cut and len(digest) == 64
    big = "x" * (recorder.CAP_JSON + 10)
    blob, size, cut, _ = recorder.prepare_body(big, "application/json")
    assert cut and size == len(big) and len(recorder.decode_body(blob)) == recorder.CAP_JSON
    blob, size, cut, digest = recorder.prepare_body(b"\xff\xd8\xff" * 100, "image/jpeg")
    assert blob is None and size == 300 and digest
    assert recorder.prepare_body(None, None) == (None, 0, False, None)


# ------------------------------------------------------------------ anonymous client
@pytest.fixture
def public():
    answers: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        if isinstance(a, int):
            return httpx.Response(a, text="blocked page")
        return httpx.Response(200, json=a, headers={"content-type": "application/json", "set-cookie": "bbxid=secret"})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    return c, answers


async def test_public_requests_recorded(public):
    c, answers = public
    answers.extend([{"rowsFound": 3}, 429, httpx.ConnectError("down")])
    with timeline.context(agent_id=4):
        await c._get(SEARCH, json_api=True, cache_s=60)
        await c._get(SEARCH, json_api=True, cache_s=60)              # from the cache
    with pytest.raises(Blocked):
        await c._get(SEARCH + "&page=2", json_api=True)
    c.limiter = RateLimiter(0)                                       # past the cool-down the 429 started
    with pytest.raises(WillhabenError):
        await c._get(SEARCH + "&page=3", json_api=True)
    live, cached, blocked, err = _recs()
    assert live.channel == "anonymous" and live.kind == "search" and live.status == 200 and live.outcome == "ok"
    assert json.loads(recorder.decode_body(live.resp_body)) == {"rowsFound": 3}
    assert json.loads(live.resp_headers)["set-cookie"] == "<redacted>" and live.content_type == "application/json"
    assert json.loads(live.req_headers)["x-wh-client"]
    assert cached.cached and cached.outcome == "cached" and cached.resp_body is None and cached.body_hash == live.body_hash
    assert blocked.status == 429 and blocked.outcome == "blocked" and recorder.decode_body(blocked.resp_body) == b"blocked page"
    assert err.status is None and err.outcome == "error" and "down" in err.error
    ev = _event(live)
    assert ev.kind == "wh" and ev.name == "search" and ev.agent_id == 4 and ev.ok and ev.payload["status"] == 200
    assert _event(blocked).ok is False and _event(err).ok is False


async def test_background_writes_and_drain(public):
    c, answers = public
    answers.append({"x": 1})
    recorder.sync_writes = False
    try:
        await c._get(SEARCH, json_api=True)
        await recorder.drain()
    finally:
        recorder.sync_writes = True
    assert len(_recs()) == 1 and _recs()[0].event_id


# ------------------------------------------------------------------ logged-in browser
class _Resp:
    def __init__(self, status, url=ORIGIN + "/robots.txt"):
        self.status, self.url = status, url


class _FakePage:
    def __init__(self, results):
        self.url = ORIGIN + "/robots.txt"
        self.results = list(results)

    def is_closed(self):
        return False

    async def evaluate(self, js, arg=None):
        return self.results.pop(0)

    async def goto(self, url, **kw):
        self.url = url
        return _Resp(200, url)


class _FakeContext:
    async def cookies(self, origin):
        return [{"name": "x-bbx-csrf-token", "value": "csrf-secret"}]


def _session(results) -> BrowserSession:
    s = BrowserSession()
    s.limiter = RateLimiter(0)
    s.context = _FakeContext()
    s.api_page = _FakePage(results)
    s.ui_page = _FakePage([])
    return s


async def test_account_requests_recorded():
    s = _session([
        {"status": 200, "text": '{"conversations": []}', "url": ORIGIN + "/webapi/chat-api/v1/conversations",
         "headers": {"content-type": "application/json"}},
        {"status": 201, "text": '{"id": 7}', "url": ORIGIN + "/webapi/iad-upload/atimage/temp"},
    ])
    with timeline.context(conversation_id="c1"):
        await s.api("GET", "/webapi/chat-api/v1/conversations?limit=10")
        await s.upload("/webapi/iad-upload/atimage/temp", "file", "a.jpg", b"\xff\xd8 photo", "image/jpeg")
        await s._goto(s.ui_page, ORIGIN + "/iad/myprofile/myadverts")
        await s._on_chat_frame(json.dumps({"req_type": "new_message", "conversation_id": "c1", "text": "Noch da?"}))
        await s._on_chat_frame(json.dumps({"req_type": "ping"}))
        await s._on_chat_frame(json.dumps({"_state": "open"}))
    api, up, nav, push = _recs()
    assert api.channel == "account" and api.kind == "chat" and api.method == "GET" and api.status == 200
    assert json.loads(api.req_headers)["x-bbx-csrf-token"] == "<redacted>" and api.content_type == "application/json"
    assert up.method == "UPLOAD" and up.kind == "upload" and json.loads(recorder.decode_body(up.req_body))["bytes"] == 8
    assert up.canonical_key == recorder.canonical_key("UPLOAD", ORIGIN + "/webapi/iad-upload/atimage/temp",
                                                      upload=("file", "image/jpeg"))
    assert nav.channel == "navigation" and nav.resp_body is None and nav.status == 200
    assert push.method == "WSPUSH" and push.kind == "chat_push" and b"Noch da?" in recorder.decode_body(push.resp_body)
    assert all(_event(r).conversation_id == "c1" for r in (api, up, nav, push))


# ------------------------------------------------------------------ timeline view, retention
@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_detail_shows_request_and_answer_as_text(client):
    recorder.record_http("account", "myads", "PUT", ORIGIN + "/webapi/iad/bap/edit", req_body={"title": "Rad <b>"},
                         status=200, resp_headers={"content-type": "text/html"}, resp_body="<script>alert(1)</script>")
    rec = _recs()[0]
    frag = client.get(f"/timeline/{rec.event_id}").text
    assert "PUT https://www.willhaben.at/webapi/iad/bap/edit" in frag and "&#34;title&#34;: &#34;Rad &lt;b&gt;&#34;" in frag
    assert "&lt;script&gt;" in frag and "<script>alert" not in frag
    r = client.get(f"/timeline/wh/{rec.id}/response")
    assert r.text == "<script>alert(1)</script>" and r.headers["content-type"].startswith("text/plain")
    assert client.get(f"/timeline/wh/{rec.id}/nope").status_code == 404
    assert len(re.findall(r'id="tl-\d+"', client.get("/timeline?kind=wh").text)) == 1
    assert not re.findall(r'id="tl-\d+"', client.get("/timeline").text)    # the overview leaves plain requests out


def test_download_entry_with_steps_and_whole_answers(client):
    from app.services import timeline

    async def go():
        async with timeline.run("job", "Activate „Rad“", name="myads-activate"):
            recorder.record_http("account", "myads", "POST", ORIGIN + "/webapi/iad/bap/save", req_body={"title": "Rad"},
                                 status=500, resp_headers={"content-type": "text/html", "cf-ray": "abc"},
                                 resp_body="<html>" + "x" * 210_000 + "</html>")
            await recorder.drain()
    asyncio.run(go())
    run = timeline.query(timeline.Filters(kind="runs"))[0][0]
    assert f'href="/timeline/{run.id}/download"' in client.get(f"/timeline/{run.id}").text
    r = client.get(f"/timeline/{run.id}/download")
    assert r.headers["content-disposition"].startswith(f'attachment; filename="logbuch-{run.id}-')
    data = r.json()
    assert data["title"] == "Activate „Rad“" and len(data["steps"]) == 1
    rec = data["steps"][0]["recording"]
    assert rec["status"] == 500 and rec["resp_headers"]["cf-ray"] == "abc"
    assert rec["response"].endswith("</html>") and not rec["response_cut"]     # whole, not the view's beginning
    assert client.get("/timeline/999999/download").status_code == 404


def test_settings_show_recordings_and_delete_all(client):
    recorder.record_http("anonymous", "search", "GET", SEARCH, status=200, resp_body="{}")
    page = client.get("/settings/timeline").text
    assert "requests stored" in page and "anonymous 1" in page
    client.post("/settings/timeline/recordings/delete")
    assert _recs() == []
    with session() as db:
        assert list(db.exec(select(TimelineEvent).where(TimelineEvent.kind == "wh"))) == []


def test_prune_empties_old_bodies_first_then_deletes():
    for i in range(4):
        recorder.record_http("anonymous", "search", "GET", f"{SEARCH}&page={i}", status=200, resp_body="x" * 50_000)
    before = recorder.storage_size()
    assert recorder.drop_oldest_bodies(2) == 2
    first, second, third, _ = _recs()
    assert first.pruned and first.resp_body is None and second.pruned and not third.pruned
    assert recorder.storage_size() < before
    with session() as db:                                   # the oldest is older than the cutoff
        r = db.get(WhRecording, first.id)
        r.ts = utcnow() - timedelta(days=10)
        db.add(r)
        e = db.get(TimelineEvent, first.event_id)
        e.ts = utcnow() - timedelta(days=10)
        db.add(e)
        db.commit()
    timeline.prune(days=5)
    assert len(_recs()) == 3
    timeline.prune(max_mb=1)                                 # small already: nothing more goes
    assert len(_recs()) == 3
    saved = timeline.EVENT_EST_BYTES
    timeline.EVENT_EST_BYTES = 400_000                       # 3 rows "weigh" 1.2 MB
    try:
        res = timeline.prune(max_mb=1, batch=1)
    finally:
        timeline.EVENT_EST_BYTES = saved
    assert res["bodies_dropped"] >= 1 and len(_recs()) == 2   # bodies first, then the oldest entry with its recording
