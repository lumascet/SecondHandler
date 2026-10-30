"""willhaben's app API (app/willhaben/native.py): the signed app token request, the Keycloak login with the stored
password (PKCE code, then tokens), a refresh on 401, a refused login pausing for a day, and the secrets staying out of
the recordings and in a mode-600 file."""
import base64
import hashlib
import hmac
import json
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import get_settings
from app.willhaben import native, recorder

AUTH = "https://sso.willhaben.at/auth/realms/willhaben/protocol/openid-connect"
APPDATA = "https://app-aggregator.willhaben.at/api/v1/application-data"
LOGIN_PAGE = ('<html><form id="kc-form-login" action="https://sso.willhaben.at/auth/realms/willhaben/login-actions/'
              'authenticate?session_code=abc&amp;client_id=apps" method="post"><input name="username">'
              '<input name="password"></form></html>')


class FakeWillhaben:
    def __init__(self, password="right"):
        self.password = password
        self.calls: list[httpx.Request] = []
        self.challenge = self.state = None
        self.access = 0
        self.refused_once = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        url = str(req.url)
        if url.endswith("service-registry.json"):
            return httpx.Response(200, json={"environments": {"prod": {"applicationDataUri": APPDATA}}})
        if url.startswith(APPDATA):
            body = json.loads(req.content)["applicationTokenRequest"]
            ts = req.headers["x-wh-date"]
            assert body["timestamp"] == ts
            want = base64.b64encode(hmac.new(native.HMAC_KEY, f"{body['salt']};{ts};api@tailored-apps.com".encode(),
                                             hashlib.sha1).digest()).decode()
            if body["signature"] != want:
                return httpx.Response(403)
            return httpx.Response(200, json={
                "applicationToken": {"value": "APPTOKEN", "expireInSeconds": 2592000},
                "rootLinkList": {"contextLink": [
                    {"id": "SsoBaseUrl", "uri": "https://sso.willhaben.at/auth"},
                    {"id": "UserSpecificContextLinks", "uri": "https://api.willhaben.at/restapi/v2/usercontextlinks"},
                    {"id": "ChatApiCounter", "uri": "https://publicapi.willhaben.at/chat-api/v1/counter"}]}})
        if url.startswith(AUTH + "/auth"):
            q = parse_qs(urlsplit(url).query)
            assert q["client_id"] == ["apps"] and q["code_challenge_method"] == ["S256"]
            self.challenge, self.state = q["code_challenge"][0], q["state"][0]
            return httpx.Response(200, text=LOGIN_PAGE)
        if "login-actions/authenticate" in url:
            form = parse_qs(req.content.decode())
            assert "wh-fp=" in req.headers.get("cookie", "")
            if form["password"] != [self.password]:
                return httpx.Response(200, text='<span id="input-error">Ungültige E-Mail oder Passwort.</span>')
            return httpx.Response(302, headers={"location": f"{native.REDIRECT_URI}?state={self.state}&code=CODE1"})
        if url == AUTH + "/token":
            form = parse_qs(req.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                v = form["code_verifier"][0]
                assert native._b64url(hashlib.sha256(v.encode()).digest()) == self.challenge
                assert form["code"] == ["CODE1"]
            else:
                assert form["refresh_token"] == ["REFRESH"]
            self.access += 1
            return httpx.Response(200, json={"access_token": f"ACCESS{self.access}", "expires_in": 300,
                                             "refresh_token": "REFRESH"})
        if url.endswith("/counter"):
            if req.headers.get("authorization") == "Bearer ACCESS1" and not self.refused_once:
                self.refused_once = True
                return httpx.Response(401)
            assert req.headers["x-wh-application-token"] == "APPTOKEN"
            assert req.headers["x-wh-client"].startswith("api@tailored-apps.com;willhabenapp;android;")
            return httpx.Response(200, json={"count": 3})
        return httpx.Response(404)


@pytest.fixture
def data(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(native.asyncio, "sleep", _nosleep)
    return tmp_path


async def _nosleep(*_a, **_k):
    return None


def test_signature_is_the_apps():
    # the app's own scheme: Base64(HMAC-SHA1(key, salt;timestamp;organization))
    sig = native.sign("c2FsdA", "2026-10-30T18:00:00+0100")
    want = hmac.new(b"REDACTED", b"c2FsdA;2026-10-30T18:00:00+0100;api@tailored-apps.com",
                    hashlib.sha1).digest()
    assert base64.b64decode(sig) == want
    assert native.timestamp()[-5] in "+-"


async def test_login_refresh_and_call(data):
    fake = FakeWillhaben()
    n = native.NativeClient(transport=httpx.MockTransport(fake))
    await n.login("me@example.org", "right")
    assert n.logged_in and n.link("ChatApiCounter")
    p = data / "willhaben-native.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "right" not in p.read_text()                      # the password is never kept here
    r = await n.request("GET", n.link("ChatApiCounter"))     # 401 with the first token: refreshed once, then answered
    assert r.status == 200 and r.json() == {"count": 3}
    assert fake.access == 2
    again = native.NativeClient(transport=httpx.MockTransport(fake))   # a restart keeps the login
    assert again.logged_in and again.st.app_token == "APPTOKEN"


async def test_refused_login_says_why(data):
    n = native.NativeClient(transport=httpx.MockTransport(FakeWillhaben()))
    with pytest.raises(native.LoginRefused, match="Ungültige"):
        await n.login("me@example.org", "wrong")
    assert not n.logged_in


async def test_keep_job_pauses_after_a_refusal(data, monkeypatch):
    from app.services import credentials

    fake = FakeWillhaben()
    credentials.save("willhaben", "me@example.org", "wrong")
    monkeypatch.setattr(native, "_client", native.NativeClient(transport=httpx.MockTransport(fake)))
    await native.keep_job()
    st = native.get_client().status()
    assert not st["logged_in"] and st["paused_until"] and "login refused" in st["error"]
    posts = sum("login-actions" in str(c.url) for c in fake.calls)
    await native.keep_job()                                  # paused: no second try against Keycloak
    assert sum("login-actions" in str(c.url) for c in fake.calls) == posts == 1


def test_secrets_never_recorded():
    h = recorder.redact_headers({"authorization": "Bearer X", "x-wh-application-token": "T", "x-wh-client": "c"})
    assert h == {"authorization": "<redacted>", "x-wh-application-token": "<redacted>", "x-wh-client": "c"}


def test_refusal_reasons():
    assert native.refusal('<input type="hidden" id="g-recaptcha-sitekey-v2" value="6Lc">') == "captcha"
    assert native.refusal('<input type="hidden" id="g-recaptcha-sitekey-v2" value="">') == "login page again"
    assert native.refusal('<input name="otp">') == "code"
