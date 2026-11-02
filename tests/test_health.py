"""Connection watchdog: debounce, transitions, channel fallback, check functions, home page tiles."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.db import get_setting, set_setting, utcnow
from app.llm import base as llm_base
from app.llm.base import LLMError
from app.llm.claude_cli import ClaudeCLI
from app.services import ha_actions, ha_auth, health, notify as notify_mod, quiet_hours, telegram
from app.services.health import Status
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setitem(llm_base.last_call, "ok", None)
    monkeypatch.setitem(llm_base.last_call, "error", None)
    monkeypatch.setitem(llm_base.last_call, "at", None)
    monkeypatch.setattr(ha_actions, "listener", {"running": False, "connected": False, "error": None})
    monkeypatch.setattr(telegram, "api_health", {"ok_at": None, "error_at": None, "error": None})


@pytest.fixture
def world(monkeypatch):
    """Fake items whose state the test sets; records every health notification."""
    states = {"ha": "ok", "telegram": "ok", "claude": "ok"}
    sent = []

    def make(key):
        return lambda: Status(key, key.upper(), states[key], f"{key} is {states[key]}")

    monkeypatch.setattr(health, "CHECKS", [make(k) for k in states])

    async def fake_send(title, items, channel):
        sent.append((title, [s.key for s in items], channel))

    monkeypatch.setattr(health, "_send", fake_send)
    return states, sent


# ------------------------------------------------------------------ transitions + debounce
async def test_baseline_is_silent_then_problem_needs_two_checks(world):
    states, sent = world
    await health.check()                          # baseline
    assert sent == []
    states["claude"] = "problem"
    await health.check()                          # first sighting: not yet
    assert sent == []
    await health.check()                          # persists: report once
    assert sent == [("Connection problem", ["claude"], None)]
    await health.check()
    await health.check()
    assert len(sent) == 1                         # no repeats while it stays broken
    states["claude"] = "ok"
    await health.check()                          # recovery right away
    assert sent[-1] == ("Connection restored", ["claude"], None)
    assert len(sent) == 2


async def test_flapping_is_debounced(world):
    states, sent = world
    await health.check()
    for _ in range(4):
        states["ha"] = "problem"
        await health.check()
        states["ha"] = "ok"
        await health.check()
    assert sent == []


async def test_problem_at_baseline_and_off_are_silent(world):
    states, sent = world
    states["claude"] = "problem"
    await health.check()
    await health.check()
    states["claude"] = "ok"
    await health.check()                          # never reported -> no "restored" either
    states["telegram"] = "off"
    await health.check()
    await health.check()
    states["telegram"] = "problem"                # off -> problem (e.g. the Bot API became unreachable)
    await health.check()
    await health.check()
    assert sent == []


async def test_state_survives_restart(world):
    states, sent = world
    await health.check()
    states["claude"] = "problem"
    await health.check()
    await health.check()
    assert get_setting("health_state")
    # the stored state is all a "restarted" app has: no second warning, recovery still reported
    await health.check()
    states["claude"] = "ok"
    await health.check()
    assert [s[0] for s in sent] == ["Connection problem", "Connection restored"]


def test_step_ignores_unknown():
    state, problems, _ = health.step({}, [Status("x", "X", "unknown")])
    assert state == {} and problems == []


# ------------------------------------------------------------------ distinct observations + restart
@pytest.fixture
def real_claude(monkeypatch):
    sent = []
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: "sk-ant-oat01-x")
    monkeypatch.setattr(health, "CHECKS", [health._claude])

    async def fake_send(title, items, channel):
        sent.append((title, [s.key for s in items]))

    monkeypatch.setattr(health, "_send", fake_send)
    return sent


async def test_one_failed_claude_call_read_by_many_checks_is_silent(real_claude):
    sent = real_claude
    llm_base.record_call(True)
    await health.check()                          # baseline ok
    llm_base.record_call(False, "timeout")
    for _ in range(4):
        await health.check()                      # same failed call read again and again
    assert sent == []
    llm_base.last_call["at"] = utcnow() + timedelta(seconds=1)   # a second failed call
    await health.check()
    assert sent == [("Connection problem", ["claude"])]


async def test_restart_sends_no_recovery_without_successful_call(real_claude):
    sent = real_claude
    llm_base.record_call(True)
    await health.check()
    llm_base.record_call(False, "timeout")
    await health.check()
    llm_base.last_call["at"] = utcnow() + timedelta(seconds=1)
    await health.check()
    assert [s[0] for s in sent] == ["Connection problem"]
    llm_base.last_call.update(ok=None, at=None, error=None)     # "restart": in-memory state is gone
    await health.check()
    await health.check()
    assert len(sent) == 1
    assert health.load_state()["claude"]["state"] == "problem"
    llm_base.record_call(True)                   # first real successful call
    await health.check()
    assert sent[-1] == ("Connection restored", ["claude"])


async def test_one_failed_session_check_read_by_three_checks_is_silent(monkeypatch):
    from app.willhaben import session as sess_mod

    class FakeSess:
        started, logged_in, user, last_check = True, True, {"nickname": "Max Muster"}, utcnow()

    fake = FakeSess()
    monkeypatch.setattr(sess_mod, "get_session", lambda: fake)
    monkeypatch.setattr(health.get_settings(), "browser_enabled", True)
    monkeypatch.setattr(health, "CHECKS", [health._willhaben])
    sent = []

    async def fake_send(title, items, channel):
        sent.append(title)

    monkeypatch.setattr(health, "_send", fake_send)
    await health.check()
    fake.logged_in, fake.last_check = False, utcnow()   # one failed session check (e.g. transient 5xx)
    for _ in range(3):
        await health.check()
    assert sent == []
    fake.logged_in, fake.last_check = True, utcnow()    # next session check recovers
    await health.check()
    assert sent == []
    fake.logged_in, fake.last_check = False, utcnow()   # two failed session checks in a row -> report
    await health.check()
    fake.last_check = utcnow() + timedelta(seconds=1)
    await health.check()
    assert sent == ["Connection problem"]


def test_step_same_sample_does_not_count():
    state = {"x": {"state": "ok", "reason": "", "since": "", "count": 0, "notified": False}}
    for _ in range(3):
        state, problems, _ = health.step(state, [Status("x", "X", "problem", "r", sample="A")])
        assert problems == []
    state, problems, _ = health.step(state, [Status("x", "X", "problem", "r", sample="B")])
    assert [p.key for p in problems] == ["x"]


# ------------------------------------------------------------------ channel fallback
@pytest.fixture
def channels(monkeypatch):
    sent = {"ha_push": 0, "ha_event": 0, "telegram": 0}
    hass = {"token": "tok"}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/events/" in request.url.path:
            sent["ha_event"] += 1
        elif "/api/services/notify/" in request.url.path:
            sent["ha_push"] += 1
        return httpx.Response(200, json={})

    monkeypatch.setattr(notify_mod, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(ha_auth, "ha_url", lambda: "https://ha.example.com")
    monkeypatch.setattr(ha_auth, "ha_token", lambda: hass["token"])
    monkeypatch.setattr(ha_auth, "notify_service", lambda: "mobile_app_x")
    monkeypatch.setattr(telegram, "active", lambda: True)

    async def fake_tg(kind, *a, **kw):
        assert kind == "health"
        sent["telegram"] += 1
        return True

    monkeypatch.setattr(telegram, "send", fake_tg)
    return sent, hass


def _statuses(ha="ok", tg="ok"):
    return [Status("ha", "Home Assistant", ha, "r"), Status("telegram", "Telegram", tg, "r"),
            Status("claude", "Claude", "problem", "Not signed in")]


@pytest.mark.parametrize("setting, ha, tg, want", [
    ("ha", "ok", "ok", None),
    ("ha", "problem", "ok", "both"),            # HA broken -> Telegram must get it too
    ("ha", "off", "ok", "both"),
    ("auto", "ok", "problem", "both"),          # Telegram broken -> HA push too
    ("telegram", "ok", "ok", None),
    ("both", "ok", "problem", "both"),
])
def test_channel_for(monkeypatch, setting, ha, tg, want):
    monkeypatch.setattr(telegram, "active", lambda: True)      # "auto" = Telegram once linked
    set_setting("push_channel", setting)
    assert health.channel_for(_statuses(ha, tg)) == want


async def test_ha_token_missing_still_goes_out_via_telegram(channels):
    sent, hass = channels
    set_setting("push_channel", "ha")
    hass["token"] = None
    await health._send("Connection problem", _statuses(), health.channel_for(_statuses(ha="problem")))
    assert sent["telegram"] == 1 and sent["ha_push"] == 0


async def test_telegram_broken_still_goes_out_via_ha(channels):
    sent, _ = channels
    set_setting("push_channel", "auto")           # auto + linked Telegram = Telegram only normally
    await health._send("Connection problem", _statuses(), health.channel_for(_statuses(tg="problem")))
    assert sent["ha_push"] == 1 and sent["ha_event"] == 1


async def test_full_check_with_real_notify(channels, monkeypatch):
    sent, _ = channels
    set_setting("push_channel", "auto")
    item = {"tg": "ok"}
    monkeypatch.setattr(health, "CHECKS", [
        lambda: Status("ha", "Home Assistant", "ok", "Connected"),
        lambda: Status("telegram", "Telegram", item["tg"], "x"),
    ])
    await health.check()
    item["tg"] = "problem"
    await health.check()
    await health.check()
    assert sent["ha_push"] == 1 and sent["telegram"] == 1   # both: Telegram tried, HA delivered


async def test_telegram_send_health_ignores_kind_filter(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": 1}}})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(telegram, "active", lambda: True)
    monkeypatch.setattr(telegram, "token", lambda: "123:abc")
    monkeypatch.setattr(telegram, "chat_id", lambda: 1)
    telegram.set_kinds(["hit"])
    assert await telegram.send("message", "T", "M") is False
    assert await telegram.send("health", "T", "M") is True
    assert telegram.api_health["ok_at"] is not None


# ------------------------------------------------------------------ check functions
def test_claude_check(monkeypatch):
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: None)
    assert health._claude().state == "problem"
    monkeypatch.setattr("app.llm.claude_login.claude_token", lambda: "sk-ant-oat01-x")
    st = health._claude()
    assert st.state == "unknown" and st.reason == "Signed in"   # no call since startup: no evidence
    llm_base.record_call(False, "Failed to authenticate")
    st = health._claude()
    assert st.state == "problem" and "Failed to authenticate" in st.reason
    llm_base.record_call(True)
    assert health._claude().state == "ok"


async def test_llm_hook_records_without_changing_behavior(monkeypatch):
    r = await ClaudeCLI(cmd=FAKE, timeout_s=30).complete(system="S", prompt="P", schema={})
    assert "argv" in r.data and llm_base.last_call["ok"] is True
    monkeypatch.setenv("FAKE_CLAUDE_ERROR", "1")
    with pytest.raises(LLMError):
        await ClaudeCLI(cmd=FAKE, timeout_s=30).complete(system="S", prompt="P", schema={})
    assert llm_base.last_call["ok"] is False and "OAuth" in llm_base.last_call["error"]


def test_ha_check(monkeypatch):
    monkeypatch.setattr(ha_auth, "ha_url", lambda: None)
    monkeypatch.setattr(ha_auth, "ha_token", lambda: None)
    assert health._ha().state == "off"
    monkeypatch.setattr(ha_auth, "ha_url", lambda: "https://ha.example.com")
    assert health._ha().state == "off"           # an address without signing in: not set up, no error
    monkeypatch.setattr(ha_auth, "ha_token", lambda: "tok")
    assert health._ha().state == "ok"            # listener not running (HA_LISTENER=0): token is enough
    monkeypatch.setattr(health, "get_settings", lambda: type("S", (), {"ha_listener": True})())
    ha_actions.listener.update(running=True, connected=False)
    assert health._ha().state == "problem"
    ha_actions.listener["connected"] = True
    assert health._ha().state == "ok"


async def test_telegram_check_and_api_hook(monkeypatch):
    status = {"code": 200}

    def handler(request):
        if status["code"] == 200:
            return httpx.Response(200, json={"ok": True, "result": True})
        if status["code"] == 400:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"})
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(telegram, "token", lambda: "123:abc")
    monkeypatch.setattr(telegram, "active", lambda: False)
    assert health._telegram().state == "off"       # token but no chat linked: setup unfinished, no error
    monkeypatch.setattr(telegram, "active", lambda: True)
    assert health._telegram().state == "unknown"   # no Bot API call since startup
    await telegram.api("getMe")
    assert health._telegram().state == "ok"
    status["code"] = 400
    with pytest.raises(telegram.TelegramError):
        await telegram.api("editMessageText")
    assert health._telegram().state == "ok"        # a 400 says nothing about the connection
    status["code"] = 401
    with pytest.raises(telegram.TelegramError):
        await telegram.api("getMe")
    st = health._telegram()
    assert st.state == "problem" and "401" in st.reason and "123:abc" not in st.reason
    status["code"] = 200
    await telegram.api("getMe")
    assert health._telegram().state == "ok"
    monkeypatch.setattr(telegram, "token", lambda: "")
    assert health._telegram().state == "off"


def test_willhaben_and_live_chat_checks(monkeypatch):
    from app.willhaben import session as sess_mod

    class FakeSess:
        started, logged_in, user, last_check = True, True, {"nickname": "Max Muster"}, utcnow()
        live = True

        def chat_live_ok(self):
            return self.live

    fake = FakeSess()
    monkeypatch.setattr(sess_mod, "get_session", lambda: fake)
    s = health.get_settings()
    monkeypatch.setattr(s, "browser_enabled", True)
    monkeypatch.setattr(s, "chat_live", True)
    quiet_hours.save(False, "01:00", "07:00", "pause", 60)     # any time of day
    st = health._willhaben()
    assert st.state == "ok" and "Max Muster" in st.reason
    assert health._live_chat().state == "ok"
    fake.live = False
    assert health._live_chat().state == "problem"
    fake.last_check = utcnow() - timedelta(seconds=3 * s.session_check_interval_s + 600)
    assert health._willhaben().state == "problem"
    fake.logged_in = None
    assert health._willhaben().state == "unknown"
    fake.logged_in = False
    assert health._willhaben().state == "problem"
    assert health._live_chat().state == "unknown"    # the willhaben tile says why; the live chat keeps its last state
    monkeypatch.setattr(s, "browser_enabled", False)
    assert health._willhaben().state == "off"


def test_login_check_is_not_overdue_in_quiet_hours(monkeypatch):
    from app.willhaben import session as sess_mod

    def vienna(h, m=0):
        return datetime(2026, 10, 9, h, m, tzinfo=quiet_hours.VIENNA).astimezone(timezone.utc)

    class FakeSess:
        started, logged_in, user, last_check = True, True, {"nickname": "Max Muster"}, vienna(0, 30)

    monkeypatch.setattr(sess_mod, "get_session", lambda: FakeSess())
    s = health.get_settings()
    monkeypatch.setattr(s, "browser_enabled", True)
    now = [vienna(4, 0)]
    for mod in (health, quiet_hours):
        monkeypatch.setattr(mod, "utcnow", lambda: now[0])
    quiet_hours.save(True, "01:00", "07:00", "pause", 60)
    assert health._willhaben().state == "ok"             # the scheduler holds the login check at night
    end = quiet_hours.last_end(vienna(12, 0))
    now[0] = end + timedelta(minutes=30)
    assert health._willhaben().state == "ok"             # due again from the end of quiet hours
    now[0] = end + timedelta(seconds=3 * s.session_check_interval_s + 180)
    assert health._willhaben().state == "problem"


def test_gcal_check(monkeypatch):
    from app.services import gcal

    monkeypatch.setattr(gcal, "has_client", lambda: False)
    assert health._gcal().state == "off"
    monkeypatch.setattr(gcal, "has_client", lambda: True)
    monkeypatch.setattr(gcal, "connected", lambda: False)
    assert health._gcal().state == "off"         # client saved, sign-in not finished: not an error
    monkeypatch.setattr(gcal, "connected", lambda: True)
    assert health._gcal().state == "ok"


def test_disk_check(monkeypatch):
    usage = type("U", (), {"total": 100 * 1024 ** 3, "used": 0, "free": 512 * 1024 ** 2})
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: usage)
    st = health._disk()
    assert st.state == "problem" and "0.5" in st.reason
    set_setting("health_disk_min_mb", "100")
    assert health._disk().state == "ok"


def test_broken_check_does_not_hide_others(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(health, "CHECKS", [boom, lambda: Status("disk", "Disk", "ok", "1 GB free")])
    out = health.collect()
    assert [s.state for s in out] == ["unknown", "ok"]


# ------------------------------------------------------------------ home page tiles
def test_home_page_tiles(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(health, "CHECKS", [
        lambda: Status("ha", "Home Assistant", "problem", "Push buttons not connected", "/settings/ha"),
        lambda: Status("claude", "Claude", "ok", "Signed in", "/settings/claude"),
        lambda: Status("gcal", "Google Calendar", "off", "Not set up", "/settings/calendar"),
        lambda: Status("disk", "Disk", "ok", "12.0 GB free"),
    ])
    html = client.get("/").text
    assert 'id="connections"' in html
    assert 'href="/settings/ha" class=' in html and 'data-health="ha" data-state="problem"' in html
    assert 'href="/settings/claude" class=' in html and 'data-state="ok"' in html
    assert 'data-health="gcal" data-state="off"' in html
    assert 'data-health="disk" data-state="ok"' in html and "12.0 GB free" in html
    assert "new hits" in html                         # existing layout still there
    html = client.get("/", headers={"accept-language": "de"}).text
    assert "Verbindungen" in html


def test_home_page_real_checks_render(client):  # noqa: F811
    html = client.get("/").text
    for key in ("willhaben", "claude", "ha", "telegram", "live_chat", "gcal", "disk"):
        assert f'data-health="{key}"' in html


def test_a_site_account_logout_is_a_problem(monkeypatch):
    """Kleinanzeigen / Vinted logged out in the browser: their messages stop, which nothing else reports."""
    import json

    from app import platforms
    monkeypatch.setattr(platforms, "enabled", lambda pid: pid == "vinted")
    assert health._site_login("kleinanzeigen").state == "off"             # the site is off
    assert health._site_login("vinted").state == "off"                    # never logged in there
    set_setting("account_vinted", json.dumps({"logged_in": True}))
    assert health._site_login("vinted").state == "ok"
    set_setting("account_vinted", json.dumps({"logged_in": False}))
    st = health._site_login("vinted")
    assert st.state == "problem" and st.href == "/settings/vinted" and "Vinted" in st.reason
    assert {"account_vinted", "account_kleinanzeigen"} <= {s.key for s in health.collect()}


async def test_a_vinted_logout_is_pushed(monkeypatch):
    import json

    from app import platforms
    monkeypatch.setattr(platforms, "enabled", lambda pid: pid == "vinted")
    monkeypatch.setattr(health, "CHECKS", [health._site_logins])
    sent = []

    async def fake_send(title, items, channel):
        sent.append((title, [s.key for s in items]))
    monkeypatch.setattr(health, "_send", fake_send)
    set_setting("account_vinted", json.dumps({"logged_in": True}))
    await health.check()                                                   # baseline
    set_setting("account_vinted", json.dumps({"logged_in": False}))
    await health.check()
    await health.check()
    assert sent == [("Connection problem", ["account_vinted"])]
