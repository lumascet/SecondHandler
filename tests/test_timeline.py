"""Timeline: context ids, runs, Claude calls with their full prompt and answer, retention, the page, the bug report."""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import TimelineBlob, TimelineEvent, log_event, session, utcnow
from app.llm.base import LLMError
from app.llm.claude_cli import ClaudeCLI
from app.services import jobs, timeline
from tests.conftest import finish_jobs

FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]


@pytest.fixture(autouse=True)
def recording_on():
    """Both are opt-in; these tests switch them on (test_timeline.py checks the default)."""
    timeline.set_recording(timeline=True, willhaben=True)


def _all() -> list[TimelineEvent]:
    with session() as db:
        return list(db.exec(select(TimelineEvent).order_by(TimelineEvent.id)))


def test_record_merges_context_and_explicit_ids():
    with timeline.context(conversation_id="c1", ad_id="5", hit_ids=[1, 2]):
        with timeline.context(ad_id="6", purchase_id=3):
            eid = timeline.record("action", "x", agent_id=7, step="s")
        assert timeline.current()["ad_id"] == "5"            # the inner context is gone again
    assert timeline.current() == {}
    e = _all()[0]
    assert e.id == eid and e.conversation_id == "c1" and e.ad_id == "6" and e.purchase_id == 3 and e.agent_id == 7
    assert e.extra == {"hit_ids": [1, 2], "step": "s"}
    assert e.actor == "system" and e.ok is True


def test_record_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(timeline, "session", boom)
    assert timeline.record("action", "x") is None


def test_marker_ids():
    assert timeline.marker_ids("ad:12") == {"ad_id": "12", "mine": True}
    assert timeline.marker_ids("addraft:5") == timeline.marker_ids("refining:5") == {"draft_id": "5"}
    assert timeline.marker_ids("drafting:c9") == {"conversation_id": "c9"}
    assert timeline.marker_ids("draft:3") == {"reply_draft": "3"}
    assert timeline.marker_ids(None) == {} and timeline.marker_ids("weird") == {}


async def test_run_sets_run_id_and_outcome():
    async with timeline.run("job", "Update", name="myads-update", ad_id="9") as r:
        timeline.record("llm", "inside")
        r.finish(True, payload={"done": 3})
    with pytest.raises(ValueError):
        async with timeline.run("agent_run", "Agent", agent_id=2):
            timeline.record("wh", "GET search 500", ok=False)
            raise ValueError("boom")
    job, step, run2, step2 = _all()
    assert job.ok is True and job.duration_ms is not None and job.payload == {"done": 3}
    assert step.run_id == job.id and step.ad_id == "9"
    assert run2.ok is False and run2.error == "boom" and step2.run_id == run2.id and step2.agent_id == 2
    assert timeline.current() == {}


async def test_context_reaches_background_jobs_and_threads():
    async def work(job):
        timeline.record("llm", "in job")
        await asyncio.to_thread(timeline.record, "wh", "in thread")
        return "fine"

    with timeline.context(conversation_id="c1", actor="user"):
        jobs.submit("chat-draft", "Draft", work, lane="claude", current="drafting:c1")
    await finish_jobs()
    by_title = {e.title: e for e in _all()}
    assert by_title["in job"].conversation_id == "c1" and by_title["in thread"].conversation_id == "c1"
    assert by_title["in job"].actor == "user"


def test_blobs_compressed_and_decoded():
    long = "Hallo " * 2000
    eid = timeline.record("llm", "x", blobs={"prompt": long, "system": "kurz", "response": None})
    with session() as db:
        blobs = {b.name: b for b in db.exec(select(TimelineBlob))}
    assert blobs["prompt"].encoding == "zlib" and len(blobs["prompt"].data) < len(long) / 10
    assert blobs["system"].encoding == "raw" and "response" not in blobs
    ev, texts = timeline.get(eid)
    assert ev.has_blobs and texts == {"prompt": long, "system": "kurz"}


async def test_claude_call_recorded_with_final_prompt_and_answer(tmp_path):
    img = tmp_path / "rad.png"
    img.write_bytes(b"\x89PNG")
    with timeline.context(conversation_id="c7", ad_id="11"):
        res = await ClaudeCLI(cmd=FAKE, timeout_s=30, purpose="chat").complete(
            system="SYS", prompt="Ist das Rad noch da?", schema={"type": "object", "properties": {"reply": {}}}, images=[img])
    e = _all()[-1]
    assert e.kind == "llm" and e.name == "chat" and e.ok and e.conversation_id == "c7" and e.ad_id == "11"
    assert e.payload["model"] == "claude-opus-5-5" and e.payload["image_count"] == 1
    assert e.payload["schema_keys"] == ["reply"]
    _, texts = timeline.get(e.id)
    assert texts["system"] == "SYS"
    assert texts["prompt"] == res.data["stdin"]          # exactly what the CLI got, photo line included
    assert texts["prompt"].startswith("Ist das Rad noch da?") and "./foto1.png" in texts["prompt"]
    assert '"stdin"' in texts["response"] and '"reply"' in texts["schema"]


async def test_failed_claude_call_recorded(monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    with pytest.raises(LLMError):
        await ClaudeCLI(cmd=FAKE, timeout_s=30, purpose="screen").complete(system="S", prompt="P", schema={})
    e = _all()[-1]
    assert e.kind == "llm" and e.ok is False and "authenticate" in e.error
    _, texts = timeline.get(e.id)
    assert texts["prompt"] == "P" and "response" not in texts


def test_notifications_are_mirrored():
    log_event("hit", "Neuer Treffer: Rad", "https://www.willhaben.at/iad/object?adId=123", ad_id="123")
    e = _all()[-1]
    assert e.kind == "notify" and e.name == "hit" and e.ad_id == "123" and e.title == "Neuer Treffer: Rad"
    assert timeline.links(e) == [{"label": "Open", "href": "/hits?view=123", "ad": None}] and e.extra is None
    log_event("hit", "Unifi: held back by the screening", ad_id="124", quiet=True)
    assert _all()[-1].extra == {"push": False}                  # the logbook keeps it, marked "no push"


def test_links_and_filter_ids():
    eid = timeline.record("llm", "x", conversation_id="c1", ad_id="5", purchase_id=2, search_id=4)
    e, _ = timeline.get(eid)
    hrefs = [l["href"] for l in timeline.links(e)]
    assert hrefs == ["/chats/c1", "/hits?view=5", "/deals?search=4", "/purchases/2"]
    mine = timeline.record("action", "x", ad_id="9", mine=True)
    assert timeline.links(timeline.get(mine)[0])[0]["href"] == "/myads?ad=9"
    assert ("conversation", "c1") in timeline.filter_ids(e)


async def test_query_folds_runs_unless_filtered():
    async with timeline.run("agent_run", "Agent 1", agent_id=1):
        timeline.record("wh", "GET search")
        timeline.record("llm", "screen", ok=False, error="bad")
    timeline.record("action", "chat.send", conversation_id="c1")
    rows, more = timeline.query(timeline.Filters())
    assert [r.kind for r in rows] == ["action", "agent_run"] and not more
    run_id = rows[1].id
    assert timeline.step_counts([run_id]) == {run_id: {"wh": 1, "llm": 1}}
    assert [r.kind for r in timeline.query(timeline.Filters(kind="llm"))[0]] == ["llm"]
    assert [r.title for r in timeline.query(timeline.Filters(errors=True))[0]] == ["screen"]
    assert len(timeline.query(timeline.Filters(job=run_id))[0]) == 3
    assert len(timeline.query(timeline.Filters(ids={"agent": "1"}))[0]) == 3
    assert [r.title for r in timeline.query(timeline.Filters(q="chat"))[0]] == ["chat.send"]
    page, more = timeline.query(timeline.Filters(kind="llm", limit=1))
    assert len(page) == 1 and not more


def test_prune_by_days_and_size():
    old = timeline.record("llm", "old", blobs={"prompt": "x" * 5000})
    timeline.record("llm", "new", blobs={"prompt": "y" * 5000})
    with session() as db:
        e = db.get(TimelineEvent, old)
        e.ts = utcnow() - timedelta(days=40)
        db.add(e)
        db.commit()
    assert timeline.prune(days=30)["deleted"] == 1
    assert [e.title for e in _all()] == ["new"]
    with session() as db:
        assert len(list(db.exec(select(TimelineBlob)))) == 1
    for i in range(5):
        timeline.record("llm", f"n{i}")
    timeline.prune(max_mb=0)                    # 0 = no size limit
    assert len(_all()) == 6
    timeline.EVENT_EST_BYTES, saved = 300_000, timeline.EVENT_EST_BYTES
    try:
        timeline.prune(max_mb=1, batch=1)       # 6 rows "weigh" 1.8 MB: the oldest go until it fits
    finally:
        timeline.EVENT_EST_BYTES = saved
    assert [e.title for e in _all()] == ["n2", "n3", "n4"]


async def test_prune_job_only_when_switched_on():
    timeline.record("llm", "x")
    with session() as db:
        e = db.exec(select(TimelineEvent)).one()
        e.ts = utcnow() - timedelta(days=99)
        db.add(e)
        db.commit()
    await timeline.prune_job()
    assert len(_all()) == 1                       # off by default
    timeline.save(True, 30, None)
    await timeline.prune_job()
    assert _all() == []


def test_interrupted_runs_marked_at_startup():
    timeline.record("job", "Update", ok=None)
    assert timeline.mark_interrupted() == 1
    assert _all()[0].ok is False and "interrupted" in _all()[0].error


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_page_detail_text_and_api(client):
    with timeline.context(conversation_id="c1"):
        eid = timeline.llm_call(purpose="chat", model="claude-sonnet-5-5", system="SYS-TEXT", prompt="PROMPT <b>TEXT</b>",
                                images=None, schema={"properties": {"reply": {}}}, data={"reply": "Ja, noch da"},
                                duration_ms=1500, raw={"modelUsage": {"m": {"inputTokens": 10, "outputTokens": 5}}})
    page = client.get("/timeline").text
    assert f'id="tl-{eid}"' in page and "Chat replies · claude-sonnet-5-5" in page and "15 tokens" in page
    frag = client.get(f"/timeline/{eid}").text
    assert "SYS-TEXT" in frag and "PROMPT &lt;b&gt;TEXT&lt;/b&gt;" in frag and "Ja, noch da" in frag
    assert 'onclick="tlCopy(this)"' in frag and "/chats/c1" in frag
    assert client.get(f"/timeline/{eid}/text/prompt").text == "PROMPT <b>TEXT</b>"
    assert client.get(f"/timeline/{eid}/text/nope").status_code == 404
    data = client.get("/api/timeline?conversation=c1").json()
    assert data["entries"][0]["id"] == eid and "texts" not in data["entries"][0]
    assert client.get(f"/api/timeline/{eid}").json()["texts"]["response"].startswith("{")
    assert client.get("/timeline/999999").status_code == 404


def test_load_more_returns_rows_only(client):
    for i in range(3):
        timeline.record("action", f"a{i}")
    page = client.get("/timeline?limit=2").text
    assert "Load more" in page and "a1" in page and "a0" not in page
    first = timeline.query(timeline.Filters(limit=2))[0][-1].id
    more = client.get(f"/timeline?limit=2&before={first}", headers={"HX-Request": "true"}).text
    assert "a0" in more and 'id="rail"' not in more


def test_a_notification_sent_without_push_is_marked(client):
    log_event("hit", "Unifi: pushed")
    log_event("hit", "Unifi: held back", quiet=True)
    rows = client.get("/timeline").text.split('id="tl-')
    assert ">no push<" in next(r for r in rows if "Unifi: held back" in r)
    assert ">no push<" not in next(r for r in rows if "Unifi: pushed" in r)


def test_run_detail_lists_steps(client):
    async def go():
        async with timeline.run("job", "Delete ads", name="myads-delete"):
            timeline.record("wh", "DELETE ad 9")
    asyncio.run(go())
    run = timeline.query(timeline.Filters(kind="runs"))[0][0]
    frag = client.get(f"/timeline/{run.id}").text
    assert "DELETE ad 9" in frag and "Steps (1)" in frag


def test_settings_save_and_prune_now(client):
    r = client.post("/settings/timeline", data={"record": "on", "enabled": "on", "days": "14", "max_mb": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert timeline.config() == {"record": True, "record_wh": True, "enabled": True, "days": 14, "max_mb": 0}  # untouched
    assert "14" in client.get("/settings/timeline").text
    timeline.record("llm", "old")
    with session() as db:
        e = db.exec(select(TimelineEvent).where(TimelineEvent.title == "old")).one()
        e.ts = utcnow() - timedelta(days=20)
        db.add(e)
        db.commit()
    client.post("/settings/timeline/prune")
    assert "old" not in [e.title for e in _all()]


def test_bug_report_has_counts_but_no_prompts():
    from app.services import bugreport

    secret = "IBAN AT61 1904 3002 3457 3201 SENTINEL-PROMPT"
    timeline.llm_call(purpose="purchase", model="m", system="s", prompt=secret, images=None, schema=None,
                      error="failed: " + secret)
    timeline.record("action", "chat.send " + secret)
    rep = bugreport.build("", include_settings=True)
    text = bugreport.render_text(rep)
    assert "SENTINEL-PROMPT" not in text and "SENTINEL-PROMPT" not in str(rep)
    assert rep["activity"]["counts"]["timeline_entries"] == {"action": 1, "llm": 1}
    assert rep["activity"]["counts"]["timeline_failures"] == {"llm": 1}


async def test_nothing_is_recorded_by_default(monkeypatch):
    """Opt-in: with the switches at their defaults, no entry, Claude call, action or willhaben request is stored."""
    import httpx

    from app.db import WhRecording
    from app.willhaben.public import PublicClient, RateLimiter

    timeline.set_recording(timeline=False, willhaben=False)
    assert timeline.config()["record"] is False and timeline.config()["record_wh"] is False
    from app import db as dbmod
    with session() as db:                                   # the defaults themselves, not only what the test set
        for k in ("timeline_enabled", "wh_recording"):
            db.delete(db.get(dbmod.Setting, k))
        db.commit()
    timeline._flags.clear()
    assert timeline.record("action", "x") is None
    await ClaudeCLI(cmd=FAKE, timeout_s=30, purpose="chat").complete(system="S", prompt="P", schema={})
    log_event("hit", "Neuer Treffer")
    c = PublicClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    c.limiter = RateLimiter(0)
    await c._get("https://www.willhaben.at/webapi/ad-search/search/atz/seo/x", json_api=True)
    from app.main import app
    with TestClient(app) as cl:
        cl.post("/settings/web", data={"public_url": ""})
        assert "switched off" in cl.get("/timeline").text
    with session() as db:
        assert list(db.exec(select(TimelineEvent))) == [] and list(db.exec(select(WhRecording))) == []


async def test_recording_without_timeline_keeps_requests_only():
    import httpx

    from app.db import WhRecording
    from app.willhaben.public import PublicClient, RateLimiter

    timeline.set_recording(timeline=False, willhaben=True)
    c = PublicClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"a": 1})))
    c.limiter = RateLimiter(0)
    await c._get("https://www.willhaben.at/webapi/ad-search/search/atz/seo/x", json_api=True)
    with session() as db:
        recs = list(db.exec(select(WhRecording)))
        assert len(recs) == 1 and recs[0].event_id is None
        assert list(db.exec(select(TimelineEvent))) == []


def test_willhaben_switch_lives_in_its_own_card(client):
    page = client.get("/settings/timeline").text
    upper, lower = page.split('action="/settings/timeline/recording"', 1)
    assert 'name="record_wh"' not in upper and 'name="record_wh"' in lower
    client.post("/settings/timeline/recording", data={})
    assert timeline.config()["record_wh"] is False and timeline.config()["record"] is True
    client.post("/settings/timeline/recording", data={"record_wh": "on"})
    assert timeline.config()["record_wh"] is True


def test_size_is_cached_and_compact_runs_vacuum(monkeypatch):
    """The pages read a cached size (summing the texts is slow on a big database); the pruning asks fresh. Compacting
    runs only with enough free pages, unless forced."""
    a = timeline.size(fresh=True)
    monkeypatch.setattr(timeline, "_file_size", lambda: {"file_mb": 1.0, "free_mb": 0.0})
    assert timeline.size() is a or timeline.size()["events"] == a["events"]
    assert timeline.compact() is False                                   # nothing worth giving back
    ran = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def execution_options(self, **kw):
            assert kw == {"isolation_level": "AUTOCOMMIT"}
            return self

        def exec_driver_sql(self, sql):
            ran.append(sql)

    class Engine:
        def connect(self):
            return Conn()
    monkeypatch.setattr("app.db.get_engine", lambda: Engine())
    assert timeline.compact(force=True) is True and "VACUUM" in ran
