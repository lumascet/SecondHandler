"""Recording everything the browser does: one JSON line per request or WebSocket frame, secrets left out, only while
switched on, old files dropped."""
import json
import os
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import get_settings
from app.willhaben import record_all


def test_secrets_in_bodies_are_left_out():
    assert record_all.redact_body('{"login":"a","password":"geheim","card_number":"4111","x":1}') == \
        '{"login":"a","password":"***","card_number":"***","x":1}'
    assert record_all.redact_body("user=a&password=geheim&cvc=123&q=x") == "user=a&password=***&cvc=***&q=x"
    assert record_all.redact_body('{"shipping":"5","distance":"3","company":"x","accessToken":"t"}') == \
        '{"shipping":"5","distance":"3","company":"x","accessToken":"***"}'


class Req:
    resource_type, method, url, post_data, failure = "fetch", "POST", "https://www.vinted.at/api/v2/x", '{"pin":"1"}', None
    frame = SimpleNamespace(url="https://www.vinted.at/inbox")

    async def response(self):
        class R:
            status = 200

            async def body(self):
                return b'{"ok":true}'

            async def header_value(self, k):
                return "application/json"

            async def all_headers(self):
                return {"content-type": "application/json", "set-cookie": "s=1"}
        return R()

    async def all_headers(self):
        return {"cookie": "secret", "x-csrf-token": "t", "accept": "*/*"}


async def test_a_request_is_written_as_one_line(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    record_all.set_on(True)
    await record_all._request(Req())
    line = json.loads(next((tmp_path / "captures" / "all").glob("*.jsonl")).read_text().splitlines()[0])
    assert line["url"].endswith("/api/v2/x") and line["response_body"] == '{"ok":true}' and line["post_data"] == '{"pin":"***"}'
    assert "secret" not in json.dumps(line) and line["frame"] == "https://www.vinted.at/inbox"
    img = Req()
    img.resource_type = "image"
    await record_all._request(img)
    assert len(next((tmp_path / "captures" / "all").glob("*.jsonl")).read_text().splitlines()) == 1


def test_old_files_go(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    d = tmp_path / "captures" / "all"
    d.mkdir(parents=True)
    old = d / "20260101-00.jsonl"
    old.write_text("{}\n")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    done = d / "20260101-01.jsonl"                             # a finished hour of today's kind
    done.write_text('{"a": 1}\n')
    monkeypatch.setattr(record_all, "_pruned_hour", "")
    monkeypatch.setattr("threading.Thread", _Inline)
    record_all._write({"x": 1})
    assert not old.exists() and not done.exists() and (d / "20260101-01.jsonl.gz").exists()
    assert record_all.stats()["files"] == 2                    # the gzipped hour and the current one


class _Inline:
    """threading.Thread that runs at once (the tidy-up in the test's own thread)."""
    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_the_oldest_go_beyond_the_size_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(record_all, "MAX_BYTES", 10)
    d = tmp_path / "captures" / "all"
    d.mkdir(parents=True)
    for h in ("20261101-01", "20261101-02"):
        (d / f"{h}.jsonl.gz").write_bytes(b"x" * 8)
    record_all._tidy("20261101-03")
    assert sorted(p.name for p in d.iterdir()) == ["20261101-02.jsonl.gz"]


def test_the_switch_on_the_session_page():
    from app.main import app
    with TestClient(app) as c:
        c.post("/session/record-all", data={"on": "1"}, follow_redirects=False)
        assert record_all.on()
        c.post("/session/record-all", data={"on": "0"}, follow_redirects=False)
    assert not record_all.on()


async def test_scripts_keep_no_body(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    record_all.set_on(True)
    js = Req()
    js.resource_type, js.url = "script", "https://assets.example.com/_next/static/chunk.js"
    await record_all._request(js)
    line = json.loads(next((tmp_path / "captures" / "all").glob("*.jsonl")).read_text().splitlines()[0])
    assert line["response_body"] is None and line["size"] == 11


def test_the_remote_view_has_scroll_and_record_controls():
    from types import SimpleNamespace

    from app.config import get_settings
    from app.main import templates
    status = SimpleNamespace(started=True, logged_in=True, user=None, last_check=None, error=None, capturing=False,
                             capture_count=0, engine="chrome")
    page = templates.env.get_template("_session_panel.html").render(
        session_status=status, captures=["20261021-2200-vinted.json"], settings=get_settings(), embed=True)
    assert 'data-scroll="-650"' in page and 'data-scroll="650"' in page
    rec = page[page.index("data-remote-record"):]
    assert 'action="/session/capture/start"' in rec[:800] and "20261021-2200-vinted.json" in page
    assert page.count('action="/session/capture/start"') == 1                 # only under the window now
    status.capturing, status.capture_count = True, 12
    page = templates.env.get_template("_session_panel.html").render(
        session_status=status, captures=[], settings=get_settings(), embed=True)
    assert "requests so far" in page and ">12<" in page and 'action="/session/capture/stop"' in page
