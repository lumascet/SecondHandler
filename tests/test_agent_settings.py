"""Settings → AI agent (app/agent_settings_routes.py): token, webhook, kinds, test button, status; the cards answer
htmx in place."""
from fastapi.testclient import TestClient

from app.db import get_setting, set_setting
from app.main import app
from app.services import agent_events

HX = {"HX-Request": "true"}


def test_page_renders_with_both_cards():
    with TestClient(app) as c:
        r = c.get("/settings/agent")
    assert r.status_code == 200
    assert 'id="agent-token-card"' in r.text and 'id="agent-webhook-card"' in r.text
    assert "Create token" in r.text and "No token yet" in r.text
    assert 'name="kind" value="purchase" checked' in r.text            # the default kinds are ticked
    assert 'name="kind" value="hit"' in r.text and 'name="kind" value="hit" checked' not in r.text
    assert 'id="s-agent-test"' not in r.text                           # no URL, no test button
    assert "/settings/agent" in c.get("/settings").text                # the overview lists the section


def test_token_create_regenerate_remove():
    with TestClient(app) as c:
        r = c.post("/settings/agent/token", headers=HX)
        tok = get_setting("agent_token")
        assert r.status_code == 200 and tok and tok in r.text
        assert f"Bearer {tok}" in r.text and f"WH_AGENT_TOKEN={tok}" in r.text      # the usage names the token
        c.post("/settings/agent/token", headers=HX)
        assert get_setting("agent_token") not in ("", tok)                         # regenerated
        r = c.post("/settings/agent/token/remove", headers=HX)
        assert get_setting("agent_token") == "" and "Create token" in r.text and "&lt;token&gt;" in r.text


def test_without_htmx_the_browser_is_sent_back():
    with TestClient(app) as c:
        r = c.post("/settings/agent/token", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/agent" and get_setting("agent_token")


def test_webhook_save_with_kinds_and_secret():
    with TestClient(app) as c:
        r = c.post("/settings/agent/webhook", headers=HX,
                   data={"url": " https://example.org/hook ", "kind": ["sale", "hit", "bogus"]})
    assert get_setting("agent_webhook_url") == "https://example.org/hook"
    assert get_setting("agent_kinds") == "sale,hit"                    # known kinds only, in the list's order
    secret = get_setting("agent_webhook_secret")
    assert secret and secret in r.text                                 # created with the first URL, shown
    assert 'name="kind" value="sale" checked' in r.text and 'name="kind" value="purchase" checked' not in r.text
    assert 'id="s-agent-test"' in r.text
    assert agent_events.kinds() == ["sale", "hit"]
    with TestClient(app) as c:
        c.post("/settings/agent/webhook", headers=HX, data={"url": "https://example.org/hook"})
    assert get_setting("agent_kinds") == "" and agent_events.kinds() == []       # none ticked = no events
    assert get_setting("agent_webhook_secret") == secret                         # kept


def test_new_secret_saves_the_form_too():
    set_setting("agent_webhook_secret", "OLDSECRET")
    with TestClient(app) as c:
        r = c.post("/settings/agent/webhook/secret", headers=HX, data={"url": "https://example.org/h", "kind": ["purchase"]})
    new = get_setting("agent_webhook_secret")
    assert new not in ("", "OLDSECRET") and new in r.text and "OLDSECRET" not in r.text
    assert get_setting("agent_webhook_url") == "https://example.org/h" and get_setting("agent_kinds") == "purchase"


def test_send_test_shows_the_result_in_the_card(monkeypatch):
    results = iter([{"ok": True, "status": 204, "error": None}, {"ok": False, "status": 500, "error": "HTTP 500: boom"}])
    calls = []

    async def fake_test():
        calls.append(get_setting("agent_webhook_url"))
        return next(results)
    monkeypatch.setattr(agent_events, "send_test", fake_test)
    with TestClient(app) as c:
        ok = c.post("/settings/agent/webhook/test", headers=HX, data={"url": "https://example.org/h", "kind": ["purchase"]})
        bad = c.post("/settings/agent/webhook/test", headers=HX, data={"url": "https://example.org/h", "kind": ["purchase"]})
    assert calls == ["https://example.org/h"] * 2                      # the typed URL is saved before the test
    assert "The test arrived (HTTP 204)." in ok.text and "bg-ok-soft" in ok.text
    assert "The test failed: HTTP 500: boom" in bad.text and "bg-bad-soft" in bad.text


def test_status_line(monkeypatch):
    from datetime import datetime, timezone
    status = {"url": "https://example.org/h", "pending": 2, "failed": 3, "last_delivered_at": datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
              "last_attempt_at": None, "last_error": "HTTP 502: bad gateway"}
    monkeypatch.setattr(agent_events, "webhook_status", lambda: status)
    set_setting("agent_webhook_url", "https://example.org/h")
    with TestClient(app) as c:
        page = c.get("/settings/agent").text
    assert "3 events were given up" in page and "2 events are waiting" in page
    assert "Last delivery 29.10.2026" in page and "Last error: HTTP 502: bad gateway" in page


def test_section_is_in_german_too():
    set_setting("ui_lang", "de")
    with TestClient(app) as c:
        page = c.get("/settings/agent").text
    assert "Token erzeugen" in page and "Welche Ereignisse" in page and "Create token" not in page


def test_without_a_dashboard_password_the_page_warns_and_the_section_is_flagged(monkeypatch):
    from app.agent_settings_routes import attention
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "dashboard_password", "")
    set_setting("agent_token", "")
    set_setting("agent_webhook_url", "")
    with TestClient(app) as c:
        assert "No dashboard password is set" in c.get("/settings/agent").text
        assert not attention()                                             # nothing set up: no flag yet
        c.post("/settings/agent/token", headers=HX)
        assert attention()                                                 # a token without a password: flagged
        monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
        assert not attention()
        assert "No dashboard password is set" not in c.get("/settings/agent", auth=("admin", "geheim")).text
    set_setting("agent_token", "")
