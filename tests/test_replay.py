"""WH_REPLAY=1: willhaben requests are answered from the recordings; unknown ones still go out and are recorded."""
import json

import httpx
import pytest
from sqlmodel import select

from app.config import get_settings
from app.db import TimelineEvent, WhRecording, session
from app.willhaben import public as public_mod
from app.services import timeline
from app.willhaben import recorder, replay
from app.willhaben import session as session_mod
from app.willhaben.public import PublicClient, RateLimiter
from app.willhaben.session import ME_PATH, ORIGIN, ReplaySession, SessionUnavailable

SEARCH = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=rad&rows=30"


@pytest.fixture(autouse=True)
def recording_on():
    """Both are opt-in; these tests switch them on (test_timeline.py checks the default)."""
    timeline.set_recording(timeline=True, willhaben=True)


@pytest.fixture
def replay_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "wh_replay", True)
    monkeypatch.setattr(session_mod, "_session", None)
    yield
    session_mod._session = None


def _recs() -> list[WhRecording]:
    with session() as db:
        return list(db.exec(select(WhRecording).order_by(WhRecording.id)))


def _client(calls: list) -> PublicClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"live": True}, headers={"content-type": "application/json"})

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0, per_hour=1, max_wait_s=0)    # a real request beyond the first would be refused
    return c


async def test_recorded_request_is_answered_without_willhaben(replay_on):
    recorder.record_http("anonymous", "search", "GET", SEARCH, status=200, resp_body='{"old": 1}',
                         resp_headers={"content-type": "application/json", "set-cookie": "x"})
    recorder.record_http("anonymous", "search", "GET", SEARCH + "&_=5", status=200, resp_body='{"new": 2}',
                         resp_headers={"content-type": "application/json"})
    calls: list = []
    c = _client(calls)
    for _ in range(3):                                      # also past the hourly budget of 1: nothing is sent
        r = await c._get(SEARCH + "&ts=99", json_api=True)
        assert r.json() == {"new": 2} and r.extensions["wh_replayed"] == 2
    assert calls == [] and len(_recs()) == 2                # no new recording either
    with session() as db:
        notes = list(db.exec(select(TimelineEvent).where(TimelineEvent.title.like("%(replay)"))))
    assert len(notes) == 3 and notes[0].payload["replayed_from"] == 2


async def test_unknown_request_goes_out_and_is_recorded(replay_on):
    calls: list = []
    c = _client(calls)
    r = await c._get(SEARCH, json_api=True)
    assert r.json() == {"live": True} and len(calls) == 1
    assert len(_recs()) == 1
    r = await c._get(SEARCH, json_api=True)                # now it is known
    assert r.json() == {"live": True} and len(calls) == 1


@pytest.mark.parametrize("bad", [{"status": 429}, {"status": 403}, {"cached": True, "outcome": "cached"},
                                 {"outcome": "error"}])
def test_unusable_recordings_are_not_replayed(bad):
    recorder.record_http("anonymous", "search", "GET", SEARCH, resp_body="{}", **{"status": 200, **bad})
    assert replay.find("GET", SEARCH) is None


def test_cut_and_pruned_recordings_are_not_replayed():
    recorder.record_http("anonymous", "detail", "GET", SEARCH, status=200, resp_body="x" * (recorder.CAP_JSON + 1),
                         content_type="application/json")
    assert _recs()[0].resp_truncated and replay.find("GET", SEARCH) is None
    recorder.record_http("anonymous", "detail", "GET", SEARCH, status=200, resp_body="{}")
    assert replay.find("GET", SEARCH) is not None
    recorder.drop_oldest_bodies(5)
    assert replay.find("GET", SEARCH) is None


async def test_account_offline_from_recordings(replay_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_enabled", False)
    me = {"userData": {"loginId": 42, "uuid": "u-1", "firstName": "Erika", "emailAddress": "erika@example.org"}}
    recorder.record_http("account", "login", "GET", ORIGIN + ME_PATH, status=200, resp_body=json.dumps(me))
    recorder.record_http("account", "chat", "GET", ORIGIN + "/webapi/chat-api/v1/conversations?limit=10&offset=0",
                         status=200, resp_body='{"conversations": [{"id": "c1"}]}')
    sess = session_mod.get_session()
    assert isinstance(sess, ReplaySession) and sess.started
    assert await sess.check_login() is True and sess.user["id"] == "42"
    assert (await sess.api_json("GET", "/webapi/chat-api/v1/conversations?offset=0&limit=10")) == {"conversations": [{"id": "c1"}]}
    with pytest.raises(SessionUnavailable):
        await sess.api("PUT", "/webapi/iad/bap/edit", {"title": "neu"})
    assert sess.status()["replay"] is True and await sess.refresh_sso() is False


def test_replay_session_only_without_browser(replay_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_enabled", True)
    assert type(session_mod.get_session()) is session_mod.BrowserSession


def test_whole_app_starts_offline(replay_on, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(get_settings(), "browser_enabled", False)
    monkeypatch.setattr(get_settings(), "chat_live", False)
    recorder.record_http("account", "login", "GET", ORIGIN + ME_PATH, status=200,
                         resp_body=json.dumps({"userData": {"loginId": 7, "emailAddress": "e@example.org"}}))
    public_mod.set_public(_client([]))
    try:
        with TestClient(app) as c:
            assert c.get("/").status_code == 200
            assert session_mod.get_session().logged_in is True
            assert "Replay mode on" in c.get("/settings/timeline").text
    finally:
        public_mod.set_public(None)
