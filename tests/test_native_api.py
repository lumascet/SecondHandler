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


class _Resp:
    def __init__(self, status, location=""):
        self.status, self.headers = status, ({"location": location} if location else {})


class _Session:
    """The app's browser: its context's request client carries the Keycloak session's cookies."""

    def __init__(self, fake, logged_in=True):
        self.fake, self.logged_in = fake, logged_in
        self.user = {"email": "me@example.org"}
        self.context = self
        self.request = self
        self.redirects = None

    async def get(self, url, max_redirects=None, **kw):
        self.redirects = max_redirects
        q = parse_qs(urlsplit(url).query)
        self.fake.challenge, self.fake.state = q["code_challenge"][0], q["state"][0]
        if self.logged_in:          # Keycloak's SSO session: straight back to the app with a code
            return _Resp(302, f"{native.REDIRECT_URI}?state={q['state'][0]}&code=CODE1")
        return _Resp(200)           # the login form


async def test_login_through_the_browser(data):
    fake = FakeWillhaben()
    n = native.NativeClient(transport=httpx.MockTransport(fake))
    s = _Session(fake)
    await n.login_via_browser(s)
    assert n.logged_in and n.st.login_user == "me@example.org" and s.redirects == 0
    assert not any("login-actions" in str(c.url) for c in fake.calls)       # no password involved


async def test_browser_logged_out_is_refused(data):
    fake = FakeWillhaben()
    n = native.NativeClient(transport=httpx.MockTransport(fake))
    with pytest.raises(native.LoginRefused, match="not logged in"):
        await n.login_via_browser(_Session(fake, logged_in=False))
    assert not n.logged_in


async def test_probe_fills_in_the_ids_and_skips_the_rest(data):
    fake = FakeWillhaben()
    n = native.NativeClient(transport=httpx.MockTransport(fake))
    await n.login("me@example.org", "right")
    out = dict(await n.probe({"uid": "42", "ad": "1234567890"}))
    assert out["UserSpecificContextLinks/42"] == 404
    assert out["ChatInitialCTAConvoAd/{conv}?adId={ad}"] is None          # no conversation id: not requested
    assert all(c.method == "GET" for c in fake.calls if "willhaben.at/restapi" in str(c.url) or "publicapi" in str(c.url))
    assert n.st.probed == native.PROBE_VERSION


def _routed_client(monkeypatch):
    c = native.NativeClient(transport=httpx.MockTransport(FakeWillhaben()))
    c.st.refresh_token, c.st.user_id = "REFRESH", "42"
    c.st.links.update({"ChatApiCounter": "https://publicapi.willhaben.at/chat-api/v1/counter",
                       "IAD_API_BASE_URL": "https://api.willhaben.at",
                       "UserDataResource": "https://api.willhaben.at/restapi/v2/user"})
    c.st.user_links["showPagedAdsFromUserLink"] = "https://publicapi.willhaben.at/myatz/v1/atverz/showpagedadsfromuser"
    monkeypatch.setattr(native, "_client", c)
    return c


def test_web_paths_map_to_the_app_api(data, monkeypatch):
    c = _routed_client(monkeypatch)
    assert native.native_url(c, "/webapi/chat-api/v1/conversations?limit=50&offset=0") == \
        "https://publicapi.willhaben.at/chat-api/v1/conversations?limit=50&offset=0"
    assert native.native_url(c, "/webapi/iad/bap/1234567890") == "https://api.willhaben.at/restapi/v2/bap/1234567890"
    assert native.native_url(c, "https://www.willhaben.at/webapi/userprofile-service/userprofile/me") == \
        "https://api.willhaben.at/restapi/v2/user/42"
    assert native.native_url(c, "/webapi/iad-upload/atimage/temp") is None
    assert native.native_url(c, "/iad/myprofile/myadverts") is None


def test_reads_and_plain_writes_are_routed(data, monkeypatch):
    from app.db import set_setting

    _routed_client(monkeypatch)
    assert native.route("GET", "/webapi/iad/bap/1")
    assert native.route("PUT", "/webapi/iad/bap/edit") == "https://api.willhaben.at/restapi/v2/bap/edit"
    assert native.route("DELETE", "/webapi/chat-api/v1/conversations")
    assert native.route("POST", "/webapi/iad-messaging/sendrequest/chat") is None     # a new chat: the browser
    assert native.route("PUT", "/webapi/myatz/bap/deactivate/1") == "https://publicapi.willhaben.at/myatz/v1/bap/deactivate/1"
    assert native.route("GET", "/webapi/iad/bap/1", accept="text/html") is None
    set_setting("willhaben_native", "0")
    try:
        assert native.route("GET", "/webapi/iad/bap/1") is None
    finally:
        set_setting("willhaben_native", "1")
    native.get_client().st.refresh_token = ""
    assert native.route("GET", "/webapi/iad/bap/1") is None                    # not logged in there: the browser


async def test_the_browser_session_uses_it_and_falls_back(data, monkeypatch):
    from app.willhaben.session import BrowserSession

    _routed_client(monkeypatch)
    answers = []

    async def request(method, url, body=None, **kw):
        answers.append(url)
        r = answers_plan.pop(0)
        if isinstance(r, Exception):
            raise r
        return native.NativeResponse(r, '{"unread": 2}', url)

    monkeypatch.setattr(native.get_client(), "request", request)
    s = BrowserSession()
    browser = []

    async def in_browser(*a, **k):
        browser.append(a)
        return {"status": 200, "text": '{"unread": 1}', "url": "web"}

    s.context = s.api_page = object()
    monkeypatch.setattr(s, "_csrf", _none)
    monkeypatch.setattr(s, "_ensure_parked", _none)
    s.api_page = type("P", (), {"evaluate": staticmethod(in_browser), "url": "https://www.willhaben.at/robots.txt"})()

    answers_plan = [200]
    r = await s.api("GET", "/webapi/chat-api/v1/counter")
    assert r.json() == {"unread": 2} and not browser                       # the app API answered
    answers_plan = [native.NativeError("down")]
    assert (await s.api("GET", "/webapi/chat-api/v1/counter")).json() == {"unread": 1}
    answers_plan = [401]
    assert (await s.api("GET", "/webapi/chat-api/v1/counter")).json() == {"unread": 1}
    assert len(browser) == 2
    await s.api("GET", "/webapi/userprofile-service/userprofile/me", native_ok=False)   # the browser's own login check
    assert len(answers) == 3 and len(browser) == 3


async def _none(*a, **k):
    return None


def _ad(ad_id, status="active"):
    return {"id": ad_id, "description": f"Ad {ad_id}", "advertStatus": {"id": status, "description": "Aktiv"},
            "attributes": {"attribute": [{"name": "PRICE_FOR_DISPLAY", "values": ["€ 20"]}]},
            "advertImageList": {"advertImage": []}, "startDate": None, "endDate": None,
            "deleteReasonQueryParameter": None,
            "advertEditStatusActionsList": {"contextLinkList": {"contextLink": [
                {"id": "2", "relativePath": f"/bap/deactivate/{ad_id}", "serviceName": "myatz",
                 "uri": f"https://publicapi.willhaben.at/myatz/v1/bap/deactivate/{ad_id}"}]}}}


async def test_own_ads_from_the_app_api_all_pages(data, monkeypatch):
    from app.willhaben.account import AccountClient

    pages = {1: [_ad(1), _ad(2)], 2: [_ad(3)]}

    def handler(req):
        page = int(req.url.params["page"])
        return httpx.Response(200, json={"totalNumberOfAdvertDetails": 3, "verticalList": [
            {"verticalId": 2, "advertDetails": []}, {"verticalId": 5, "advertDetails": pages.get(page, [])}]})

    c = _routed_client(monkeypatch)
    c._transport = httpx.MockTransport(handler)
    c.st.access_token, c.st.access_until = "A", 9e12
    c.st.app_token, c.st.app_token_until = "T", 9e12
    native.MY_ADS_ROWS = 2
    try:
        ads = await AccountClient(session=None).my_adverts()
    finally:
        native.MY_ADS_ROWS = 50
    assert [a.ad_id for a in ads] == ["1", "2", "3"] and ads.complete
    assert ads[0].actions["2"]["serviceName"] == "myatz"
    assert native.native_url(c, "/webapi/myatz/bap/deactivate/1") == \
        "https://publicapi.willhaben.at/myatz/v1/bap/deactivate/1"


def test_more_web_paths(data, monkeypatch):
    c = _routed_client(monkeypatch)
    c.st.links["ChatInitialCTAConvoAd"] = "https://publicapi.willhaben.at/atdetail/v1/chat/ad_list"
    assert native.native_url(c, "/webapi/convoad/v1/chat/ad_list/u-1?ids=5") == \
        "https://publicapi.willhaben.at/atdetail/v1/chat/ad_list/u-1?ids=5"
    assert native.native_url(c, "/webapi/p2p-aggregator/restapi/v2/p2pp/options/ad/5/user/possible") == \
        "https://api.willhaben.at/restapi/v2/p2pp/options/ad/5/user/possible"
    assert [native.falls_back(s) for s in (200, 204, 400, 401, 404, 500)] == [False, False, False, True, True, True]
    # a write after a 5xx may have happened: never repeated through the browser
    assert [native.falls_back(s, "PUT") for s in (200, 401, 404, 405, 500, 502)] == [False, True, True, True, False, False]


async def test_a_failed_write_is_not_repeated_in_the_browser(data, monkeypatch):
    from app.willhaben.session import BrowserSession

    _routed_client(monkeypatch)

    async def down(method, url, body=None, **kw):
        raise native.NativeError("connection reset")

    monkeypatch.setattr(native.get_client(), "request", down)
    s = BrowserSession()
    browser = []

    async def in_browser(*a, **k):
        browser.append(a)
        return {"status": 200, "text": "{}", "url": "web"}

    s.context = object()
    s.api_page = type("P", (), {"evaluate": staticmethod(in_browser), "url": "https://www.willhaben.at/robots.txt"})()
    monkeypatch.setattr(s, "_csrf", _none)
    with pytest.raises(native.NativeError):
        await s.api("PUT", "/webapi/myatz/bap/deactivate/1")
    assert not browser


CODE_PAGE = ('<html><form id="kc-otp" action="https://sso.willhaben.at/auth/realms/willhaben/login-actions/'
             'authenticate?session_code=xyz&amp;execution=otp" method="post">'
             '<input type="hidden" name="tab_id" value="T1"><input type="checkbox" name="trustDevice">'
             '<input type="text" name="emailCode" data-name="x" autocomplete="one-time-code">'
             '<button type="submit" name="submitCode" value="ok">Weiter</button></form></html>')


class CodeWillhaben(FakeWillhaben):
    """A device willhaben does not know: the password is answered with an e-mail code form; once the code went through,
    Keycloak's cookie marks the device and the next login needs neither password nor code."""

    def __call__(self, req):
        url = str(req.url)
        cookies = req.headers.get("cookie", "")
        if url.startswith(AUTH + "/auth") and "KEYCLOAK_IDENTITY=known" in cookies:
            q = parse_qs(urlsplit(url).query)
            self.challenge, self.state = q["code_challenge"][0], q["state"][0]
            return httpx.Response(302, headers={"location": f"{native.REDIRECT_URI}?state={self.state}&code=CODE1"})
        if "login-actions/authenticate" in url and "execution=otp" in url:
            form = parse_qs(req.content.decode())
            assert form["tab_id"] == ["T1"] and form["trustDevice"] == ["on"] and "AUTH_SESSION=1" in cookies
            if form["emailCode"] != ["123456"]:
                return httpx.Response(200, text=CODE_PAGE)
            return httpx.Response(302, headers={
                "location": f"{native.REDIRECT_URI}?state={self.state}&code=CODE1",
                "set-cookie": "KEYCLOAK_IDENTITY=known; Path=/auth/realms/willhaben; Secure"})
        if "login-actions/authenticate" in url:
            self.calls.append(req)
            return httpx.Response(200, text=CODE_PAGE, headers={"set-cookie": "AUTH_SESSION=1; Path=/auth/realms/willhaben"})
        return super().__call__(req)


async def test_login_waits_for_the_email_code_then_remembers_the_device(data):
    fake = CodeWillhaben()
    n = native.NativeClient(transport=httpx.MockTransport(fake))
    with pytest.raises(native.CodeNeeded):
        await n.login("me@example.org", "right")
    assert n.pending and n.pending.code_fields == ["emailCode"] and n.status()["code_needed"]
    with pytest.raises(native.CodeNeeded):              # a wrong code: the form again, the login keeps waiting
        await n.submit_code("999999")
    await n.submit_code(" 123 456 ")
    assert n.logged_in and n.pending is None and n.st.login_via == "password"
    assert any(c["name"] == "KEYCLOAK_IDENTITY" for c in n.st.sso_cookies)
    n.forget()
    again = native.NativeClient(transport=httpx.MockTransport(fake))   # restart: the device is known now
    posts = len([c for c in fake.calls if "login-actions" in str(c.url)])
    await again.login("me@example.org", "right")
    assert again.logged_in and len([c for c in fake.calls if "login-actions" in str(c.url)]) == posts


async def test_no_code_without_a_waiting_login(data):
    n = native.NativeClient(transport=httpx.MockTransport(FakeWillhaben()))
    with pytest.raises(native.LoginRefused, match="start the login again"):
        await n.submit_code("123456")


def test_code_form_and_token_id():
    assert native.challenge_form(CODE_PAGE) == (
        "https://sso.willhaben.at/auth/realms/willhaben/login-actions/authenticate?session_code=xyz&execution=otp",
        {"tab_id": "T1", "trustDevice": "on", "submitCode": "ok"}, ["emailCode"])
    assert native.challenge_form(LOGIN_PAGE.replace('<input name="username">', "")) is None   # password only: no code
    body = base64.urlsafe_b64encode(json.dumps({"sub": "a-b", "loginId": 42}).encode()).decode().rstrip("=")
    assert native.token_user_id(f"h.{body}.s") == "42"
    assert native.token_user_id("garbage") == ""


# willhaben's SMS page (seen 2026-10-30, reduced): one unnamed box per digit, the script fills the hidden "code" field
SMS_PAGE = ('<form id="kc-mobile-update-form" action="https://sso.willhaben.at/auth/realms/willhaben/login-actions/'
            'authenticate?session_code=s&amp;execution=e" method="post"><input type="hidden" id="otp" name="code" value/>'
            '<input id="otp-input-1" class="wh-otp-input" type="text" inputmode="numeric" maxlength="1" data-pos="0"  />'
            '<input id="otp-input-2" class="wh-otp-input" type="text" inputmode="numeric" maxlength="1" data-pos="1"  />'
            '<input type="hidden" id="referenceID" name="referenceID" value="ref1"/>'
            '<button id="submitOneTimePassword" class="submit-button" name="registration" type="submit">Weiter</button>'
            '</form>')


def test_the_sms_code_page():
    action, fields, codes = native.challenge_form(SMS_PAGE)
    assert action.endswith("session_code=s&execution=e")
    assert codes == ["code"] and fields == {"referenceID": "ref1", "registration": ""}


async def test_selftest_runs_every_read_and_says_who_answered(data, monkeypatch):
    from app.willhaben import account, insert

    _routed_client(monkeypatch)

    async def fake(name, value, side="native"):
        native.counts[side] += 1
        return value

    conv = type("C", (), {"id": "c1"})()
    ad = type("A", (), {"ad_id": "7", "status": "active"})()
    ads = account.AdvertList([ad])
    ads.total = 1
    monkeypatch.setattr(account.AccountClient, "me", lambda self: fake("me", {"userData": {"a": 1}}))
    monkeypatch.setattr(account.AccountClient, "unread_count", lambda self: fake("u", 2))
    monkeypatch.setattr(account.AccountClient, "conversations", lambda self, limit=5: fake("c", [conv]))
    monkeypatch.setattr(account.AccountClient, "messages", lambda self, cid: fake("m", [1, 2]))
    monkeypatch.setattr(account.AccountClient, "my_adverts", lambda self: fake("a", ads))
    monkeypatch.setattr(insert, "get_own_ad", lambda s, i: fake("o", {"heading": "Rad"}))
    monkeypatch.setattr(insert, "get_own_images", lambda s, a: fake("i", {"advertImage": [1]}))
    monkeypatch.setattr(insert, "shipping_of", lambda s, a: fake("s", {"handover": "pickup"}))
    monkeypatch.setattr(insert, "suggest_categories", lambda s, h: fake("t", {"x": 1}))
    monkeypatch.setattr(insert, "load_form", lambda s: fake("f", {"p": 1}, side="browser"))

    sess = type("S", (), {"user": None})()
    res = {r["name"]: r for r in await native.selftest(sess)}
    assert res["messages"]["summary"] == "2 messages" and res["own ads"]["summary"] == "1 of 1 ads, 1 active"
    assert res["profile"]["via"] == "api" and res["insertion form (web page)"]["via"] == "browser"
    assert res["PayLivery fee window"]["summary"] == "no ad or user"
    assert all(r["ok"] for r in res.values())
    assert native.get_client().status()["selftest"]
