"""The MCP server inside the app (app/mcp_http.py): Streamable HTTP at /mcp, protected like the JSON API, the tools
calling the app in-process with the caller's credentials."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import mcp_http, mcp_server
from app.config import get_settings
from app.db import Purchase, session, set_setting
from app.main import app
from app.services import agent_events

TOKEN = "t0ken-for-the-agent"
BEARER = {"authorization": f"Bearer {TOKEN}"}
BASIC = {"authorization": "Basic " + base64.b64encode(b"admin:geheim").decode()}
JSON_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locked(monkeypatch, tmp_path):
    """The dashboard has a password and the agent an API token."""
    monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_setting(agent_events.SETTING_TOKEN, TOKEN)


def rpc(client, method, params=None, headers=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", content=json.dumps(body), headers={**JSON_HEADERS, **(headers or {})})


def call(client, tool, args=None, headers=None, many=False):
    r = rpc(client, "tools/call", {"name": tool, "arguments": args or {}}, headers)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    blocks = [json.loads(b["text"]) for b in result["content"]]
    return blocks if many else blocks[0]               # a tool returning a list sends one content block per item


def _order(**kw) -> int:
    base = dict(ad_id="2000000001", ad_title="Bosch Geschirrspüler", ad_url="https://www.willhaben.at/iad/x",
                asking_price=250.0, max_price=280.0, mode="paylivery", status="negotiating", log=[])
    base.update(kw)
    with session() as db:
        row = Purchase(**base)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def test_initialize_and_list_the_tools(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "secondhandler"
    assert "update_settings" in r.json()["result"]["instructions"]
    tools = {t["name"]: t for t in rpc(client, "tools/list", id_=2).json()["result"]["tools"]}
    for name in ("status", "get_events", "list_purchases", "get_purchase", "list_sales", "get_settings", "update_settings"):
        assert name in tools
    assert tools["get_settings"]["annotations"]["readOnlyHint"] is True
    assert tools["update_settings"]["annotations"]["readOnlyHint"] is False
    assert tools["update_settings"]["annotations"]["destructiveHint"] is False
    assert "source" in tools["list_purchases"]["inputSchema"]["properties"]


def test_the_lifespan_can_start_again():
    for _ in range(2):                                 # every start builds a fresh session manager (it runs only once)
        with TestClient(app) as c:
            assert rpc(c, "tools/list").status_code == 200
    assert mcp_http._manager is None                   # stopped with the app


def test_no_mcp_without_a_running_app():
    r = TestClient(app).post("/mcp", content="{}", headers=JSON_HEADERS)    # no lifespan: the manager is not running
    assert r.status_code == 503


def test_tool_call_goes_through_the_api_in_process(client):
    pid = _order()
    got = call(client, "list_purchases", {"status": "negotiating"}, many=True)
    assert [p["id"] for p in got] == [pid] and got[0]["ad_title"] == "Bosch Geschirrspüler"
    assert call(client, "get_purchase", {"purchase_id": pid})["id"] == pid


def test_only_post_is_served(client):
    for method in ("GET", "DELETE"):
        r = client.request(method, "/mcp", headers={"accept": "text/event-stream"})
        assert r.status_code == 405 and r.headers["allow"] == "POST"


def test_the_trailing_slash_reaches_the_server_too(client):
    r = client.post("/mcp/", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), headers=JSON_HEADERS)
    assert r.status_code == 200 and r.json()["result"]["tools"]


def test_login_needed_for_mcp_like_for_the_api(client, locked):
    assert client.post("/mcp", content="{}", headers=JSON_HEADERS).status_code == 401
    r = client.get("/mcp", headers={"accept": "text/html"})
    assert r.status_code == 401 and r.json() == {"detail": "login required"}         # never a redirect to the login page
    assert rpc(client, "tools/list", headers={"authorization": "Bearer wrong"}).status_code == 401
    assert rpc(client, "tools/list", headers=BEARER).status_code == 200
    assert rpc(client, "tools/list", headers=BASIC).status_code == 200
    assert client.get("/api/purchases").status_code == 401                           # unchanged: the API is locked too


def test_the_token_opens_mcp_and_the_api_but_not_the_pages(client, locked):
    assert client.get("/", headers={**BEARER, "accept": "text/html"}, follow_redirects=False).status_code in (303, 401)
    assert rpc(client, "tools/list", headers=BEARER).status_code == 200


def test_credentials_are_forwarded_to_the_api(client, locked):
    pid = _order()
    assert [p["id"] for p in call(client, "list_purchases", headers=BEARER, many=True)] == [pid]
    assert [p["id"] for p in call(client, "list_purchases", headers=BASIC, many=True)] == [pid]
    # a valid login cookie works as well
    from app import auth
    client.cookies.set(auth.COOKIE, auth.make_token(False))
    assert [p["id"] for p in call(client, "list_purchases", many=True)] == [pid]


def test_the_api_check_runs_on_the_forwarded_credentials(client, locked, monkeypatch):
    """The in-process request carries exactly what /mcp got: without it /api/* answers 401, as it would for anyone."""
    seen = []

    async def spy(method, path, **kw):
        seen.append(mcp_http.current.get())
        return await real(method, path, **kw)
    real = mcp_server._call
    monkeypatch.setattr(mcp_server, "_call", spy)
    call(client, "list_purchases", headers=BEARER, many=True)
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}" and seen[0].app is app
    # without credentials the API refuses (the tool reports the 401 instead of data)
    token = mcp_http.current.set(mcp_http.InProcess(app, {}))
    try:
        import asyncio
        out = asyncio.run(real("GET", "/api/purchases"))
    finally:
        mcp_http.current.reset(token)
    assert out["error"] == 401


def test_the_timeline_sees_the_actor_mcp(client, monkeypatch):
    seen = []
    from app import timeline_web
    real = timeline_web.timeline.fresh

    def spy(**kw):
        seen.append(kw.get("actor"))
        return real(**kw)
    monkeypatch.setattr(timeline_web.timeline, "fresh", spy)
    monkeypatch.setattr(timeline_web.timeline, "enabled", lambda: True)
    _order()
    call(client, "list_purchases", many=True)
    assert "mcp" in seen                              # the in-process API request, marked x-wh-client-kind: mcp


def test_the_in_process_request_is_marked_as_mcp(client, monkeypatch):
    sent = []
    import httpx
    real_init = httpx.AsyncClient.__init__

    def spy(self, *a, **kw):
        sent.append(kw.get("headers"))
        real_init(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    call(client, "list_purchases", many=True)
    assert sent and sent[0]["x-wh-client-kind"] == "mcp"


def test_settings_tools_make_these_requests(client, monkeypatch):
    calls = []

    async def fake(method, path, **kw):
        calls.append((method, path, kw))
        return {"ok": True}
    monkeypatch.setattr(mcp_server, "_call", fake)
    call(client, "get_settings")
    call(client, "get_settings", {"section": "agent"})
    call(client, "update_settings", {"values": {"agent_webhook_url": "https://x.example/hook", "n": 3}})
    call(client, "list_purchases", {"source": "chat", "limit": 5})
    assert calls == [
        ("GET", "/api/settings", {"params": None}),
        ("GET", "/api/settings", {"params": {"section": "agent"}}),
        ("PATCH", "/api/settings", {"json": {"values": {"agent_webhook_url": "https://x.example/hook", "n": 3}}}),
        ("GET", "/api/purchases", {"params": {"limit": 5, "source": "chat"}}),
    ]


def test_stdio_mode_stays_an_http_client(monkeypatch):
    import asyncio
    import httpx

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})
    real = httpx.AsyncClient
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient",
                        lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))
    assert mcp_http.current.get() is None
    assert asyncio.run(mcp_server._call("GET", "/api/status")) == {"ok": True}
    assert seen["url"] == mcp_server.BASE + "/api/status"
