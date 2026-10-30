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
            want = base64.b64encode(hmac.new(TEST_KEY.encode(), f"{body['salt']};{ts};api@tailored-apps.com".encode(),
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


TEST_KEY = "not-the-apps-key"     # the real one is never part of the code (WILLHABEN_APP_KEY / Settings)


@pytest.fixture
def data(monkeypatch, tmp_path):
    from app.willhaben import access_log

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "willhaben_app_key", TEST_KEY)
    monkeypatch.setattr(native, "_typing_pause", _nosleep)
    yield tmp_path
    access_log.reset()                  # the fake requests counted here must not reach the access statistics tests


async def _nosleep(*_a, **_k):
    return None


def test_signature_is_the_apps():
    # the app's own scheme: Base64(HMAC-SHA1(key, salt;timestamp;organization))
    sig = native.sign("c2FsdA", "2026-10-30T18:00:00+0100", key=TEST_KEY)
    want = hmac.new(TEST_KEY.encode(), b"c2FsdA;2026-10-30T18:00:00+0100;api@tailored-apps.com",
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
    n.st.links.update({"AdInsertionVerticalSelection": "https://app-aggregator-adin.willhaben.at/api/v1/adin/vs",
                       "ChatRealtimeWS": "wss://chat-realtime-apps.willhaben.at/v1/ws/"})
    out = dict(await n.probe({"uid": "42", "ad": "1234567890"}))
    assert out["AdInsertionVerticalSelection"] == 404                  # asked (the fake knows no such page)
    assert out["AdInsertionDeepLinkAdInput?adTypeId=67&productId=67"] is None   # no such link: not requested
    assert out["ChatRealtimeWS"] is None                               # a socket: never fetched as a page
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
    assert native.native_url(c, "/webapi/dac/x") is None
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
    assert res["profile"]["via"] == "api" and res["insertion form"]["via"] == "browser"
    assert res["PayLivery fee window"]["summary"] == "no ad or user"
    assert all(r["ok"] for r in res.values())
    assert native.get_client().status()["selftest"]


def _anon(handler):
    """The anonymous visitor's app identity, its app token already fetched (this hour's identity)."""
    import time

    anon = native.NativeClient(transport=httpx.MockTransport(handler), state_file=native.ANON_STATE_FILE)
    anon.st.links["Search"] = "https://ad-search.willhaben.at/restapi/v2/search/atz"
    anon.st.app_token, anon.st.app_token_until, anon.st.logged_in_at = "APPTOKEN", 9e12, time.time()
    return anon


async def test_anonymous_search_through_the_app_api_never_as_the_account(data, monkeypatch):
    from app.willhaben import public

    account = _routed_client(monkeypatch)
    account.st.access_token, account.st.visitor_id = "ACCOUNT", "account-device"
    seen = []

    def app_side(req):
        if str(req.url).endswith("service-registry.json") or str(req.url).startswith(APPDATA):
            return FakeWillhaben()(req)
        seen.append(req)
        return httpx.Response(200, json={"rowsFound": 1, "advertSummaryList": {"advertSummary": []}})

    anon = _anon(app_side)
    monkeypatch.setattr(native, "_anon", anon)
    # the anonymous client's own requests go through PublicClient's transport (pacing, recording, VPN)
    web = []
    pc = public.PublicClient(transport=httpx.MockTransport(lambda r: (seen.append(r) or web.append(r) or
                                                                       httpx.Response(200, json={"rowsFound": 0}))))
    pc.app_search = True                       # a canned transport skips it otherwise
    url = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=rad&rows=30"
    r = await pc._get(url, json_api=True)
    assert r.status_code == 200
    req = seen[-1]
    assert str(req.url) == ("https://ad-search.willhaben.at/restapi/v2/search/atz/seo/kaufen-und-verkaufen/marktplatz"
                            "?keyword=rad&rows=30")
    assert "authorization" not in req.headers and req.headers["x-wh-application-token"] == "APPTOKEN"
    assert req.headers["x-wh-visitor-id"] != "account-device" and native.get_client().st.visitor_id == "account-device"


async def test_anonymous_search_falls_back_to_the_web(data, monkeypatch):
    from app.db import set_setting
    from app.willhaben import public

    anon = _anon(FakeWillhaben())
    monkeypatch.setattr(native, "_anon", anon)
    asked = []

    def site(req):
        asked.append(str(req.url))
        return httpx.Response(503 if "ad-search.willhaben.at" in str(req.url) else 200, json={"rowsFound": 0})

    pc = public.PublicClient(transport=httpx.MockTransport(site))
    pc.app_search = True
    url = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/x?rows=5"
    monkeypatch.setattr(pc, "_check_status", lambda r: None)
    assert (await pc._get(url, json_api=True)).status_code == 200
    assert asked[-1] == url and any("ad-search.willhaben.at" in u for u in asked)
    set_setting("willhaben_native_search", "0")
    try:
        assert await native.anon_route(url) is None
    finally:
        set_setting("willhaben_native_search", "1")
    assert await native.anon_route("https://www.willhaben.at/iad/object?adId=1") is None    # pages: the web


def test_each_vpn_server_has_its_own_anonymous_identity(data):
    from app.platforms.vpn import Route

    a, b = Route("http://127.0.0.1:9001", 1, "at-vie-1"), Route("http://127.0.0.1:9002", 1, "de-fra-1")
    home = native.anon_client()
    one, two = native.anon_client(a), native.anon_client(b)
    assert one is native.anon_client(a) and one is not two and home not in (one, two)
    assert one.proxy == "http://127.0.0.1:9001" and one.st.visitor_id != two.st.visitor_id
    one.st.app_token = "X"
    native.save_state(one.st, one._file)                          # memory only: nothing written
    assert not list(data.glob("willhaben-native*.json"))
    moved = native.anon_client(Route("http://127.0.0.1:9101", 2, "at-vie-1"))   # restarted: a new local port
    assert moved is not one and moved.proxy.endswith(":9101")


class _History:
    """The session part send_message confirms with: the conversation's newest messages (through the app API in real
    life, routed by BrowserSession.api), and the browser's socket, which must stay unused here."""

    def __init__(self, shows=True):
        self.shows, self.sent, self.ws = shows, [], []
        self.user = {}

    async def api_json(self, method, path, body=None):
        msgs = [{"client_message_id": cid, "message_id": f"m-{cid[:4]}", "text": txt, "is_mine": True}
                for cid, txt in self.sent] if self.shows else []
        return {"messages": msgs}

    async def ws_request(self, *a, **k):
        self.ws.append(a)
        return {"status": "success"}

    async def ws_send(self, url, frames, settle_ms=1500):
        self.ws.append(frames)
        return {"status": "ok"}


def _chat_client(monkeypatch, answer):
    from app.willhaben import account

    monkeypatch.setattr(account, "CONFIRM_WAIT_S", 0)
    c = _routed_client(monkeypatch)
    c.st.links["ChatApiSendMessage"] = "https://publicapi.willhaben.at/chat-api/v1/conversations/"
    c.st.links["ChatApiMarkConversationRead"] = \
        "https://publicapi.willhaben.at/chat-api/v1/conversations/$conversation_id/read"
    c.st.access_token, c.st.access_until, c.st.app_token, c.st.app_token_until = "A", 9e12, "T", 9e12
    calls = []

    def handler(req):
        calls.append(req)
        return answer(req)

    c._transport = httpx.MockTransport(handler)
    return calls


async def test_a_message_goes_through_the_app_api_and_counts_once_the_history_shows_it(data, monkeypatch):
    from app.willhaben.account import AccountClient

    hist = _History()

    def answer(req):
        body = json.loads(req.content)
        hist.sent.append((body["client_message_id"], body["text"]))
        return httpx.Response(204)

    calls = _chat_client(monkeypatch, answer)
    mid = await AccountClient(hist).send_message("c-1", "  Hallo, noch da?  ")
    req = calls[0]
    assert req.method == "POST" and str(req.url) == "https://publicapi.willhaben.at/chat-api/v1/conversations/c-1"
    body = json.loads(req.content)
    assert body["text"] == "Hallo, noch da?" and body["createdAt"].endswith("Z") and "type" not in body
    assert mid.startswith("m-") and not hist.ws                     # the browser's socket stayed unused


async def test_a_refused_message_goes_through_the_browser_a_broken_one_never_twice(data, monkeypatch):
    from app.willhaben.account import AccountClient, SendFailed

    hist = _History()
    _chat_client(monkeypatch, lambda req: httpx.Response(404))           # no such endpoint: nothing went out
    hist.sent.append(("x", "Hallo"))
    await AccountClient(hist).send_message("c-1", "Hallo")
    assert len(hist.ws) == 1

    hist = _History(shows=False)
    _chat_client(monkeypatch, lambda req: httpx.Response(502))           # may have gone out: only the history decides
    with pytest.raises(SendFailed, match="history"):
        await AccountClient(hist).send_message("c-1", "Hallo")
    assert not hist.ws

    hist = _History()
    _chat_client(monkeypatch, lambda req: httpx.Response(400, text="too long"))   # explained: nothing went out
    with pytest.raises(SendFailed, match="rejected"):
        await AccountClient(hist).send_message("c-1", "Hallo")
    assert not hist.ws


async def test_read_receipt_is_one_put(data, monkeypatch):
    from app.willhaben.account import AccountClient, ChatMessage

    hist = _History()
    calls = _chat_client(monkeypatch, lambda req: httpx.Response(204))
    msgs = [ChatMessage(ext_id=f"m{i}", direction="in", text="x", created_at=None, read=False) for i in range(3)]
    assert await AccountClient(hist).mark_read("c-9", msgs) == 3
    assert [(r.method, str(r.url)) for r in calls] == \
        [("PUT", "https://publicapi.willhaben.at/chat-api/v1/conversations/c-9/read")]
    assert not hist.ws


async def test_the_insertion_form_and_photo_uploads_from_the_app_api(data, monkeypatch):
    from app.willhaben import insert
    from app.willhaben.session import BrowserSession

    seen = []

    def answer(req):
        seen.append(req)
        url = str(req.url)
        if url.endswith("/restapi/v2/bap"):
            return httpx.Response(200, json={"adId": 0, "contextLinkList": {"contextLink": []}})
        if "/categorytree/withattributes/67" in url:
            return httpx.Response(200, json={"categoryNode": {"code": 1, "children": []}, "attributes": []})
        if url.endswith("/restapi/v2/atimage/temp"):
            assert b'name="file"; filename="a.jpg"' in req.content
            return httpx.Response(201, json={"reference": "r1"})
        return httpx.Response(404)

    _chat_client(monkeypatch, answer)
    form = await insert.load_form(None)                       # never the web page (no browser here)
    assert form["ad"]["adId"] == 0 and form["categoryTree"]["categoryNode"]["code"] == 1
    s = BrowserSession()
    r = await s.upload("/webapi/iad-upload/atimage/temp", "file", "a.jpg", b"\xff\xd8jpeg", "image/jpeg")
    assert r.status == 201 and r.json() == {"reference": "r1"}
    assert str(seen[-1].url) == "https://api.willhaben.at/restapi/v2/atimage/temp"
    assert native.route("PUT", "/webapi/iad-upload/atimage/5/imagelist/order-and-associate") == \
        "https://api.willhaben.at/restapi/v2/atimage/5/imagelist/order-and-associate"


async def test_the_live_chat_socket_logs_in_like_the_app_and_hands_over_when_refused(data, monkeypatch):
    import asyncio

    import websockets
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    seen_headers, frames = [], []

    async def handler(ws):
        seen_headers.append(ws.request.headers)
        await ws.send(json.dumps({"type": "new_message", "payload": {"conversation_id": "c-1"}}))
        msg = json.loads(await ws.recv())                       # the ping
        frames.append(msg)
        await ws.close()

    def gate(conn, request):
        if request.headers.get("authorization") != "Bearer A":
            return Response(403, "Forbidden", Headers())
        return None

    async with websockets.serve(handler, "127.0.0.1", 0, process_request=gate) as server:
        port = server.sockets[0].getsockname()[1]
        c = _routed_client(monkeypatch)
        c.st.links["ChatRealtimeWS"] = f"ws://127.0.0.1:{port}/v1/ws/"
        c.st.access_token, c.st.access_until, c.st.app_token, c.st.app_token_until = "A", 9e12, "T", 9e12
        got = []

        async def on_frame(raw):
            got.append(json.loads(raw))

        live = native.LiveChat(c, on_frame)
        monkeypatch.setattr(native.LiveChat, "PING_S", 0.05)
        live.start()
        for _ in range(100):
            if frames:
                break
            await asyncio.sleep(0.05)
        await live.stop()
        assert got[0] == {"_state": "open"} and got[1]["payload"]["conversation_id"] == "c-1"
        assert frames == [{"req_type": "ping"}]
        h = seen_headers[0]
        assert h["x-wh-application-token"] == "T" and h["x-wh-client"].startswith("api@tailored-apps.com;")

        c.st.access_token = "WRONG"                              # the socket turns the login down: the browser's turn
        refused = native.LiveChat(c, on_frame)
        refused.start()
        await asyncio.wait_for(refused.task, 5)
        assert refused.refused and got[-1] == {"_state": "closed", "code": 403}


def test_ad_bodies_carry_the_description_as_text_to_the_app_api():
    html = "<p>Wurde getestet.</p><p><br></p><p>Zwei Stück.</p><ul><li>Kabel</li></ul>"
    body = {"adId": 1, "heading": "Lüfter", "description": html}
    out = native.app_body("/webapi/iad/bap/edit", body)
    assert out["description"] == "Wurde getestet.\n\nZwei Stück.\n• Kabel" and out["heading"] == "Lüfter"
    assert body["description"] == html                                  # the browser path keeps its HTML
    assert native.app_body("/webapi/iad/bap/save", {"description": "schon Text"})["description"] == "schon Text"
    assert native.app_body("/webapi/chat-api/v1/conversations", {"description": html})["description"] == html


async def test_a_failed_anonymous_app_token_pauses_the_app_search(data, monkeypatch):
    def down(req):
        raise httpx.ConnectError("unreachable")

    anon = native.NativeClient(transport=httpx.MockTransport(down), state_file=native.ANON_STATE_FILE)
    monkeypatch.setattr(native, "_anon", anon)
    url = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/x?rows=5"
    assert await native.anon_route(url) is None
    assert anon.st.paused_until > 0
    anon._transport = httpx.MockTransport(FakeWillhaben())                 # back, but the pause holds
    assert await native.anon_route(url) is None and not anon.st.app_token


def test_the_app_key_from_env_wins_over_the_setting_and_without_one_the_app_api_is_off(data, monkeypatch):
    from app.db import set_setting

    _routed_client(monkeypatch)
    assert native.app_key() == (TEST_KEY, "env")
    set_setting("willhaben_app_key", "from-the-gui")
    try:
        assert native.app_key() == (TEST_KEY, "env")                     # the environment wins
        monkeypatch.setattr(get_settings(), "willhaben_app_key", "")
        assert native.app_key() == ("from-the-gui", "settings")
        set_setting("willhaben_app_key", "")
        assert native.app_key() == ("", "")
        assert native.active() is None and native.route("GET", "/webapi/iad/bap/1") is None   # off: the browser
        with pytest.raises(native.NoAppKey):
            native.sign("s", "t")
    finally:
        set_setting("willhaben_app_key", "")


def test_the_key_field_shows_the_env_value_read_only_with_a_yellow_outline(monkeypatch, tmp_path):
    import re

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "willhaben_app_key", "from-env-123")
    with TestClient(app) as c:
        page = c.get("/settings/willhaben").text
        field = re.search(r'<input name="key"[^>]*>', page).group(0)
        assert 'value="from-env-123"' in field and "readonly" in field and "border-warning" in field
        assert "WILLHABEN_APP_KEY" in field and "title=" in field                   # the hover hint
        assert 'value="save"' not in page.split('action="/settings/native/key"')[1].split("</form>")[0]
        monkeypatch.setattr(get_settings(), "willhaben_app_key", "")
        field = re.search(r'<input name="key"[^>]*>', c.get("/settings/willhaben").text).group(0)
        assert "readonly" not in field and 'type="password"' in field and "value=" not in field


def test_the_new_chat_form_goes_to_the_app_with_its_own_fields(data, monkeypatch):
    c = _routed_client(monkeypatch)
    c.st.links["ContactAdvertiserByNewChat"] = "https://api.willhaben.at/restapi/v2/sendrequest/chat"
    path = "/webapi/iad-messaging/sendrequest/chat"
    assert native.route("POST", path) == "https://api.willhaben.at/restapi/v2/sendrequest/chat"
    web = {"fromFullName": "A B", "shareTenantProfile": False, "from": "a@example.org", "adId": 123, "copyToSender": False,
           "showTelephoneNumber": False, "telephone": "", "selectedContactSuggestions": [], "mailContent": "Hallo",
           "firstName": "A", "lastName": "B"}
    out = native.app_body(path, web)
    assert out == {"mailContent": "Hallo", "firstName": "A", "lastName": "B", "telephone": "", "from": "a@example.org",
                   "copyToSender": False, "showTelephoneNumber": False, "shareTenantProfile": False, "adId": "123"}
    assert native.falls_back(400, "POST", path) and not native.falls_back(500, "POST", path)   # a 5xx may have gone out
    assert not native.falls_back(400, "PUT", "/webapi/iad/bap/edit")
