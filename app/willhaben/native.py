"""willhaben's own app API (api.willhaben.at, publicapi.willhaben.at): the account without the browser.

Taken from the Android app 8.67.0 (build 615389) and checked live 2026-10-30 (bootstrap and app token):

  1. GET app-aggregator.willhaben.at/service-registry.json -> the application-data URL of each environment.
  2. POST <application-data>?config_item=root-links with an applicationTokenRequest signed by the app (HMAC-SHA1 with a
     key built into the APK) -> an application token (valid 30 days) and the context links (id -> URL). The app has no
     fixed endpoint paths: every URL comes from these links (and, logged in, from UserSpecificContextLinks).
  3. The user logs in at Keycloak (sso.willhaben.at, realm willhaben) as client `apps`: authorization code with PKCE,
     redirect willhaben-app://willhaben.at/token. Here the login form is filled in with the stored password
     (app/services/credentials.py); the refresh token keeps the login afterwards (grant_type=refresh_token).

Every call carries X-WH-Client / X-WH-Date / X-WH-Security-Version / X-WH-Application-Token and the Bearer token.

Kept in DATA_DIR/willhaben-native.json (mode 600, like the other secrets): app token, links, refresh and access token.
Requests go through the same limiter, access log and recorder as the browser's (authorization and the app token are
never recorded; the login and token requests are recorded without their bodies).

`route()` sends the browser's account requests here once logged in: the app API serves the same backends with the same
answers, only under other hosts (seen live 2026-10-30 for the profile, the chats, the own ads, an ad's edit data and
images). The writes of WRITE_PREFIXES follow the same paths; pages, uploads, chat messages and the chat WebSocket stay
with the browser. `probe()` reads a few of the account's own
endpoints once (GET only), so the recordings show the shapes of the ones not routed yet.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.config import get_settings
from app.i18n import t
from app.willhaben import access_log, recorder
from app.willhaben.public import willhaben_limiter

log = logging.getLogger(__name__)

APP_VERSION = "8.67.0"
APP_BUILD = "615389"
ORGANIZATION = "api@tailored-apps.com"
HMAC_KEY = b"REDACTED"      # the app's own (class gq0): signs the app token request only
SECURITY_VERSION = "20130527022532"
CLIENT = f"{ORGANIZATION};willhabenapp;android;{APP_VERSION};responsive_app"
USER_AGENT = f"willhaben/{APP_BUILD} okhttp/5.5.0 Android/14"
LOGIN_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
REGISTRY_URL = "https://app-aggregator.willhaben.at/service-registry.json"
CLIENT_ID = "apps"
REDIRECT_URI = "willhaben-app://willhaben.at/token"
CONFIG_ITEMS = ("root-links",)
APP_TOKEN_MARGIN_S = 2 * 86400          # renewed two days before it runs out
ACCESS_MARGIN_S = 60
LOGIN_PAUSE_S = 24 * 3600               # after a refused login: no new attempt by itself for a day
CODE_WAIT_S = 15 * 60                   # a login waits this long for the e-mail code
TIMEOUT_S = 30

# read once after the first login (GET only): what the account calls would need, to see the shapes. {uid}/{uuid}: the
# account's ids; {ad}: one own ad; {conv}: one conversation. A placeholder without a value skips the entry.
PROBE_VERSION = "3"
PROBE = (
    ("UserSpecificContextLinks", "/{uid}"),
    ("showPagedAdsFromUserLink", ""),
    ("showPagedAdsFromUserLink", "?page=1&rows=30"),
    ("myTransactionsOverviewSellerV2", ""),
    ("myTransactionsOverviewBuyerV2", ""),
    ("ChatInitialCTAConvoAd", "/{conv}?adId={ad}"),
)


class NativeError(RuntimeError):
    pass


class LoginRefused(NativeError):
    """Keycloak did not hand out a code: wrong password, a captcha, a code that did not fit."""


class CodeNeeded(LoginRefused):
    """willhaben sent a code by e-mail (a device it does not know yet): the login waits for submit_code()."""


@dataclass
class Pending:
    """A password login stopped at the e-mail code form, until the code comes (in memory only)."""
    action: str
    fields: dict[str, str]
    code_fields: list[str]
    cookies: list[tuple[str, str, str, str]]
    verifier: str
    state: str
    user: str
    referer: str
    until: float


def timestamp() -> str:
    """The app's X-WH-Date: SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZZ") -> 2026-10-30T18:00:00+0100."""
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def sign(salt: str, ts: str) -> str:
    msg = f"{salt};{ts};{ORGANIZATION}".encode()
    return base64.b64encode(hmac.new(HMAC_KEY, msg, hashlib.sha1).digest()).decode()


def token_request(salt: str, ts: str) -> dict[str, Any]:
    return {"applicationTokenRequest": {"organization": ORGANIZATION, "salt": salt, "timestamp": ts,
                                        "signature": sign(salt, ts)}}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce() -> tuple[str, str]:
    verifier = _b64url(os.urandom(32))
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def parse_links(data: Any) -> dict[str, str]:
    """{id: uri} of every contextLink list in an application-data or usercontextlinks answer."""
    out: dict[str, str] = {}

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(v, dict):
            for link in v.get("contextLink") or []:
                if isinstance(link, dict) and link.get("id") and link.get("uri"):
                    out[str(link["id"])] = str(link["uri"])
            for k, x in v.items():
                if k != "contextLink":
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)

    walk(data)
    return out


_FORM = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', re.DOTALL)
_ERROR = re.compile(r'<(?:span|div)[^>]*(?:id="input-error[^"]*"|class="[^"]*(?:kc-feedback-text|alert-error|'
                    r'wh-input-error)[^"]*")[^>]*>(.*?)</(?:span|div)>', re.DOTALL)


def login_form(html: str) -> str | None:
    m = _FORM.search(html)
    return unescape(m.group(1)) if m else None


_ANY_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.DOTALL | re.IGNORECASE)
_INPUT = re.compile(r"<(?:input|button)\b[^>]*>", re.IGNORECASE)


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'(?<![\w-]){name}\s*=\s*"([^"]*)"', tag, re.IGNORECASE)
    return unescape(m.group(1)) if m else None


def challenge_form(html: str) -> tuple[str, dict[str, str], list[str]] | None:
    """A Keycloak form asking for a code: (action, fields to send as they are, the code field names), or None.
    Hidden fields and named buttons go back unchanged, checkboxes ticked (e.g. "trust this device")."""
    for attrs, inner in _ANY_FORM.findall(html):
        action = _attr(attrs, "action")
        if not action or "login-actions" not in action:
            continue
        fields: dict[str, str] = {}
        codes: list[str] = []
        for tag in _INPUT.findall(inner):
            name = _attr(tag, "name")
            if not name:
                continue
            kind = (_attr(tag, "type") or "text").lower()
            if tag.lower().startswith("<button"):
                fields.setdefault(name, _attr(tag, "value") or "")
            elif kind == "hidden":
                fields[name] = _attr(tag, "value") or ""
            elif kind == "checkbox":
                fields[name] = _attr(tag, "value") or "on"
            elif kind in ("text", "number", "tel") and name not in ("username", "password", "email"):
                codes.append(name)
        if codes:
            return action, fields, codes
    return None


def refusal(html: str) -> str:
    """Why the login page came back instead of the code (as willhaben words it), or what it asks for."""
    if re.search(r'id="g-recaptcha-sitekey-v2"[^>]*value="[^"]+"', html):
        return "captcha"
    if re.search(r'name="(?:otp|totp|code|emailCode)"', html):
        return "code"
    for m in _ERROR.finditer(html):
        text = re.sub(r"<[^>]+>|\s+", " ", unescape(m.group(1))).strip()
        if text:
            return text[:200]
    return "login page again"


def token_user_id(access_token: str) -> str:
    """willhaben's numeric login id from the access token's claims (unverified, only read), or ""."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(access_token.split(".")[1] + "=="))
    except (IndexError, ValueError):
        return ""
    for key in ("loginId", "login_id", "user_id", "userId", "uid", "sub", "preferred_username"):
        v = str(claims.get(key) or "")
        if v.isdigit():
            return v
    return ""


def fingerprint() -> str:
    """A wh-fp cookie as the login page's script sets it (hashes of browser traits, then their hash): one per install,
    kept with the tokens, so every login looks like the same phone."""
    token = {k: secrets.token_hex(32) for k in ("fontPreferences", "fonts", "audio", "languages", "plugins", "canvas",
                                                "math")}
    token["sha256"] = hashlib.sha256("|".join(token.values()).encode()).hexdigest()
    token["ua"] = "Google Inc.  Linux armv81"
    return base64.b64encode(json.dumps(token, separators=(",", ":")).encode()).decode()


@dataclass
class State:
    app_token: str = ""
    app_token_until: float = 0
    links: dict[str, str] = field(default_factory=dict)
    user_links: dict[str, str] = field(default_factory=dict)
    refresh_token: str = ""
    access_token: str = ""
    access_until: float = 0
    salt: str = ""
    visitor_id: str = ""
    fp: str = ""
    login_user: str = ""
    login_via: str = ""              # "password" or "browser"
    sso_cookies: list[dict[str, str]] = field(default_factory=list)   # Keycloak's, from the last password login
    user_id: str = ""                # willhaben's numeric login id: part of the user URLs
    logged_in_at: float = 0
    probed: str = ""                 # PROBE_VERSION that ran
    error: str = ""
    paused_until: float = 0


def _path():
    return get_settings().data_dir / "willhaben-native.json"


def load_state() -> State:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    known = State.__dataclass_fields__
    return State(**{k: v for k, v in raw.items() if k in known})


def save_state(st: State) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st.__dict__, f)
    os.replace(tmp, p)


@dataclass
class NativeResponse:
    status: int
    text: str
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


class NativeClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.st = load_state()
        if not self.st.salt:                     # the app makes one per process; one per install is as good
            self.st.salt, self.st.visitor_id, self.st.fp = _b64url(os.urandom(12)), str(uuid.uuid4()), fingerprint()
        self._transport = transport
        self._lock = asyncio.Lock()
        self.pending: Pending | None = None
        self.limiter = willhaben_limiter()      # same home IP as the browser and the anonymous visitor

    def _client(self, **kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport, **kw)

    # ------------------------------------------------------------ state
    @property
    def logged_in(self) -> bool:
        return bool(self.st.refresh_token)

    def link(self, link_id: str) -> str | None:
        return self.st.user_links.get(link_id) or self.st.links.get(link_id)

    def status(self) -> dict[str, Any]:
        st = self.st
        waiting = self.pending is not None and self.pending.until > time.time()
        return {"logged_in": self.logged_in, "user": st.login_user or None, "links": len(st.links), "via": st.login_via,
                "code_needed": waiting,
                "user_links": len(st.user_links), "error": st.error or None,
                "app_token_until": datetime.fromtimestamp(st.app_token_until).astimezone() if st.app_token_until else None,
                "logged_in_at": datetime.fromtimestamp(st.logged_in_at).astimezone() if st.logged_in_at else None,
                "paused_until": datetime.fromtimestamp(st.paused_until).astimezone()
                if st.paused_until > time.time() else None}

    def forget(self) -> None:
        """Log out of the app API (the browser's login stays)."""
        self.st.refresh_token = self.st.access_token = self.st.login_user = self.st.user_id = ""
        self.st.access_until = self.st.logged_in_at = self.st.paused_until = 0
        self.st.user_links, self.st.error = {}, ""
        save_state(self.st)

    def _headers(self, auth: bool = True) -> dict[str, str]:
        h = {"x-wh-client": CLIENT, "x-wh-date": timestamp(), "x-wh-security-version": SECURITY_VERSION,
             "user-agent": USER_AGENT, "accept": "application/json", "x-wh-visitor-id": self.st.visitor_id,
             "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"}
        if self.st.app_token:
            h["x-wh-application-token"] = self.st.app_token
        if auth and self.st.access_token:
            h["authorization"] = f"Bearer {self.st.access_token}"
        return h

    # ------------------------------------------------------------ app token and links
    async def bootstrap(self) -> None:
        """Service registry -> signed application-data: a fresh app token and the context links."""
        async with self._client() as c:
            async with self.limiter:
                r = await c.get(REGISTRY_URL, headers={"user-agent": USER_AGENT, "accept": "application/json"})
            recorder.record_http("account", "native", "GET", REGISTRY_URL, status=r.status_code, resp_body=r.text)
            if r.status_code != 200:
                raise NativeError(f"service registry: HTTP {r.status_code}")
            url = ((r.json().get("environments") or {}).get("prod") or {}).get("applicationDataUri")
            if not url:
                raise NativeError("service registry: no application-data URL")
            ts = timestamp()
            headers = {**self._headers(auth=False), "x-wh-date": ts, "content-type": "application/json; charset=UTF-8"}
            headers.pop("x-wh-application-token", None)
            async with self.limiter:
                r = await c.post(url, params=[("config_item", i) for i in CONFIG_ITEMS], headers=headers,
                                 json=token_request(self.st.salt, ts))
            # the answer holds the app token: recorded without its body
            recorder.record_http("account", "native", "POST", url, status=r.status_code, store_response=False)
        if r.status_code != 200:
            raise NativeError(f"application-data: HTTP {r.status_code}")
        data = r.json()
        tok = data.get("applicationToken") or {}
        if not tok.get("value"):
            raise NativeError("application-data: no application token")
        self.st.app_token = tok["value"]
        self.st.app_token_until = time.time() + float(tok.get("expireInSeconds") or 86400)
        self.st.links = parse_links(data) or self.st.links
        save_state(self.st)
        log.info("willhaben app API: app token renewed, %d links", len(self.st.links))

    async def ensure_app_token(self) -> None:
        if not self.st.app_token or self.st.app_token_until - time.time() < APP_TOKEN_MARGIN_S or not self.st.links:
            await self.bootstrap()

    def _sso(self, endpoint: str) -> str:
        base = (self.st.links.get("SsoBaseUrl") or "https://sso.willhaben.at/auth").rstrip("/")
        return f"{base}/realms/willhaben/protocol/openid-connect/{endpoint}"

    # ------------------------------------------------------------ user login
    async def login(self, user: str, password: str) -> None:
        """The app's Keycloak login with the stored password. Keycloak's cookies from an earlier login come along (a known
        device: no code, often no password either). CodeNeeded when willhaben sends a code by e-mail first: the login
        then waits for submit_code(); LoginRefused when no code comes for another reason."""
        await self.ensure_app_token()
        self.pending = None
        verifier, challenge = pkce()
        state = _b64url(os.urandom(12))
        params = {"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": "openid",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "ui_locales": "de"}
        auth_url = self._sso("auth")
        host = urlsplit(auth_url).hostname or "sso.willhaben.at"
        async with self._login_client(host) as c:
            async with self.limiter:
                r = await c.get(auth_url, params=params)
            recorder.record_http("account", "login", "GET", auth_url, status=r.status_code, store_response=False)
            code = self._code(r)
            if code is None:
                action = login_form(r.text) if r.status_code == 200 else None
                if not action:
                    raise LoginRefused(f"no login form (HTTP {r.status_code})")
                await asyncio.sleep(1.5 + secrets.randbelow(1500) / 1000)     # a person types for a moment
                form = {"username": user, "password": password, "rememberMe": "on", "login": ""}
                async with self.limiter:
                    r = await c.post(urljoin(str(r.url), action), data=form,
                                     headers={"origin": f"https://{host}", "referer": str(r.url)})
                # never the request body: it holds the password
                recorder.record_http("account", "login", "POST", str(r.url).split("?")[0], status=r.status_code,
                                     store_response=False)
                r, code = await self._follow(c, r)
                if code is None:
                    self._challenge(r, c, verifier, state, user)          # raises CodeNeeded or LoginRefused
            await self._finish(c, code, state, verifier, user)

    def _login_client(self, host: str) -> httpx.AsyncClient:
        c = self._client(headers={"user-agent": LOGIN_UA, "accept": "text/html,application/xhtml+xml",
                                  "accept-language": "de-AT,de;q=0.9", "x-requested-with": "at.willhaben"},
                         follow_redirects=False)
        for ck in self.st.sso_cookies:
            c.cookies.set(ck["name"], ck["value"], domain=ck.get("domain") or host, path=ck.get("path") or "/")
        c.cookies.set("wh-fp", self.st.fp, domain=host, path="/auth/realms/willhaben")
        return c

    async def _follow(self, c: httpx.AsyncClient, r: httpx.Response) -> tuple[httpx.Response, tuple[str, str] | None]:
        """Keycloak may pass through its own pages before the redirect to the app: follow them (at most 5)."""
        for _ in range(5):
            code = self._code(r)
            if code is not None or r.status_code not in (301, 302, 303) or not r.headers.get("location"):
                return r, code
            async with self.limiter:
                r = await c.get(urljoin(str(r.url), r.headers["location"]))
        return r, self._code(r)

    def _challenge(self, r: httpx.Response, c: httpx.AsyncClient, verifier: str, state: str, user: str) -> None:
        """The page after the password: a form asking for the e-mail code -> kept for submit_code(), CodeNeeded."""
        # the page is recorded (no password in it): the form's fields are what submit_code() fills in
        recorder.record_http("account", "login", "GET", str(r.url).split("?")[0], status=r.status_code,
                             resp_body=r.text, content_type="text/html")
        form = challenge_form(r.text)
        if form is None:
            raise LoginRefused(refusal(r.text))
        action, fields, code_fields = form
        self.pending = Pending(action=urljoin(str(r.url), action), fields=fields, code_fields=code_fields,
                               cookies=[(ck.name, ck.value, ck.domain, ck.path) for ck in c.cookies.jar],
                               verifier=verifier, state=state, user=user, referer=str(r.url),
                               until=time.time() + CODE_WAIT_S)
        raise CodeNeeded(t("willhaben sent a code by e-mail – enter it in Settings → willhaben"))

    async def submit_code(self, code: str) -> None:
        """Finish a login waiting for willhaben's e-mail code."""
        p = self.pending
        if p is None or p.until < time.time():
            self.pending = None
            raise LoginRefused(t("No login is waiting for a code (any more) – start the login again"))
        code = re.sub(r"\s+", "", code or "")
        if not code:
            raise LoginRefused(t("Empty code"))
        form = dict(p.fields)
        if len(p.code_fields) > 1 and len(code) == len(p.code_fields):     # one box per digit
            form.update(zip(p.code_fields, code))
        else:
            form[p.code_fields[0]] = code
        host = urlsplit(p.action).hostname or "sso.willhaben.at"
        async with self._login_client(host) as c:
            for name, value, domain, path in p.cookies:
                c.cookies.set(name, value, domain=domain, path=path)
            async with self.limiter:
                r = await c.post(p.action, data=form, headers={"origin": f"https://{host}", "referer": p.referer})
            recorder.record_http("account", "login", "POST", p.action.split("?")[0], status=r.status_code,
                                 store_response=False)
            r, got = await self._follow(c, r)
            if got is None:
                self._challenge(r, c, p.verifier, p.state, p.user)      # wrong code: the form again, still waiting
            self.pending = None
            await self._finish(c, got, p.state, p.verifier, p.user)

    async def _finish(self, c: httpx.AsyncClient, code: tuple[str, str], state: str, verifier: str, user: str) -> None:
        if code[1] != state:
            raise LoginRefused("state mismatch")
        await self._token({"grant_type": "authorization_code", "code": code[0], "redirect_uri": REDIRECT_URI,
                           "client_id": CLIENT_ID, "code_verifier": verifier}, c)
        # Keycloak's cookies (its session and whatever marks this device as known) for the next login
        self.st.sso_cookies = [{"name": ck.name, "value": ck.value, "domain": ck.domain, "path": ck.path}
                               for ck in c.cookies.jar if ck.name != "wh-fp"]
        self.st.login_user, self.st.logged_in_at, self.st.error, self.st.paused_until = user, time.time(), "", 0
        self.st.login_via = "password"
        save_state(self.st)
        log.info("willhaben app API: logged in with the stored password")

    async def login_via_browser(self, session: Any, timeout_s: float = 25) -> None:
        """The app's login URL opened in the app's browser: its Keycloak session (same realm as the web login) hands
        out the code at once, with no password and no new-device check. LoginRefused when the browser is logged out
        there (the login form shows instead of the redirect)."""
        await self.ensure_app_token()
        verifier, challenge = pkce()
        state = _b64url(os.urandom(12))
        q = httpx.QueryParams({"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
                               "scope": "openid", "code_challenge": challenge, "code_challenge_method": "S256",
                               "state": state, "ui_locales": "de"})
        url = f"{self._sso('auth')}?{q}"
        # the browser context's own request client: its cookies (the Keycloak session), the redirect not followed –
        # a page would only see the navigation to willhaben-app:// aborted, without the redirect's Location
        started = time.monotonic()
        async with self.limiter:
            with access_log.track("account", "login") as tr:
                r = await session.context.request.get(url, max_redirects=0, timeout=timeout_s * 1000,
                                                      headers={"accept": "text/html,application/xhtml+xml"})
                tr.status = r.status
        recorder.record_http("account", "login", "GET", url.split("?")[0], status=r.status, store_response=False,
                             duration_ms=int((time.monotonic() - started) * 1000))
        loc = r.headers.get("location") or ""
        if not loc.startswith(REDIRECT_URI):
            raise LoginRefused("the browser is not logged in at willhaben" if r.status == 200
                               else f"no redirect (HTTP {r.status})")
        qs = parse_qs(urlsplit(loc).query)
        if qs.get("error"):
            raise LoginRefused(qs.get("error_description", qs["error"])[0])
        if (qs.get("state") or [""])[0] != state or not qs.get("code"):
            raise LoginRefused("no code in the redirect")
        await self._token({"grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": REDIRECT_URI,
                           "client_id": CLIENT_ID, "code_verifier": verifier})
        self.st.login_user = (session.user or {}).get("email") or "browser"
        self.st.user_id = str((session.user or {}).get("id") or self.st.user_id)
        self.st.login_via = "browser"
        self.st.logged_in_at, self.st.error, self.st.paused_until = time.time(), "", 0
        save_state(self.st)
        log.info("willhaben app API: logged in through the browser's willhaben login")

    @staticmethod
    def _code(r: httpx.Response) -> tuple[str, str] | None:
        loc = r.headers.get("location") or ""
        if r.status_code in (301, 302, 303) and loc.startswith(REDIRECT_URI):
            q = parse_qs(urlsplit(loc).query)
            if q.get("error"):
                raise LoginRefused(q.get("error_description", q["error"])[0])
            if q.get("code"):
                return q["code"][0], (q.get("state") or [""])[0]
        return None

    async def _token(self, form: dict[str, str], c: httpx.AsyncClient | None = None) -> None:
        url = self._sso("token")
        own = c is None
        c = c or self._client()
        try:
            async with self.limiter:
                r = await c.post(url, data=form, headers={"user-agent": USER_AGENT, "accept": "application/json"})
        finally:
            if own:
                await c.aclose()
        recorder.record_http("account", "login", "POST", url, status=r.status_code, store_response=False)
        if r.status_code in (400, 401):
            if form.get("grant_type") == "refresh_token":
                self.st.refresh_token = self.st.access_token = ""
                self.st.error = "logged out (refresh token refused)"
                save_state(self.st)
            raise LoginRefused(f"token: HTTP {r.status_code}")
        if r.status_code != 200:
            raise NativeError(f"token: HTTP {r.status_code}")
        data = r.json()
        self.st.access_token = data["access_token"]
        self.st.access_until = time.time() + float(data.get("expires_in") or 300)
        self.st.refresh_token = data.get("refresh_token") or self.st.refresh_token
        save_state(self.st)

    async def ensure_access(self) -> None:
        async with self._lock:
            await self.ensure_app_token()
            if not self.st.refresh_token:
                raise LoginRefused("not logged in to the app API")
            if self.st.access_until - time.time() < ACCESS_MARGIN_S:
                await self._token({"grant_type": "refresh_token", "refresh_token": self.st.refresh_token,
                                   "scope": "openid", "client_id": CLIENT_ID})

    # ------------------------------------------------------------ requests
    async def request(self, method: str, url: str, body: Any = None, *, kind: str | None = None) -> NativeResponse:
        """One call of the app API, logged in. A 401 refreshes the access token once."""
        await self.ensure_access()
        kind = kind or access_log.classify(url)
        payload = json.dumps(body) if body is not None else None
        for attempt in (0, 1):
            headers = self._headers()
            if payload is not None:
                headers["content-type"] = "application/json; charset=UTF-8"
            started = time.monotonic()
            async with self.limiter:
                with access_log.track("account", kind) as tr:
                    try:
                        async with self._client() as c:
                            r = await c.request(method, url, content=payload, headers=headers)
                    except httpx.HTTPError as e:
                        recorder.record_http("account", kind, method, url, req_headers=headers, req_body=payload,
                                             outcome="error", error=str(e),
                                             duration_ms=int((time.monotonic() - started) * 1000))
                        raise NativeError(f"{method} {url}: {e}") from e
                    tr.status = r.status_code
            recorder.record_http("account", kind, method, url, req_headers=headers, req_body=payload,
                                 status=r.status_code, resp_headers=dict(r.headers), resp_body=r.text,
                                 final_url=str(r.url), duration_ms=int((time.monotonic() - started) * 1000))
            if r.status_code == 401 and attempt == 0:
                self.st.access_until = 0
                await self.ensure_access()
                continue
            return NativeResponse(r.status_code, r.text, str(r.url))
        raise AssertionError("unreachable")

    async def load_user_links(self) -> dict[str, str]:
        url = self.st.links.get("UserSpecificContextLinks")
        if not url or not self.st.user_id:
            raise NativeError("no UserSpecificContextLinks or user id")
        r = await self.request("GET", f"{url.rstrip('/')}/{self.st.user_id}", kind="native")
        if r.ok:
            self.st.user_links = parse_links(r.json())
            save_state(self.st)
        return self.st.user_links

    async def probe(self, ids: dict[str, str] | None = None) -> list[tuple[str, int | None]]:
        """Read the account's own data once through the app API (GET only), so the recordings show each answer."""
        ids = {k: v for k, v in (ids or {}).items() if v}
        out: list[tuple[str, int | None]] = []
        for link_id, suffix in PROBE:
            base = self.link(link_id)
            try:
                suffix = suffix.format(**ids)
            except KeyError:
                base = None
            if not base:
                out.append((link_id + suffix, None))
                continue
            url = base.rstrip("/") + suffix if suffix.startswith("/") else base + suffix
            try:
                r = await self.request("GET", url, kind="native")
                out.append((link_id + suffix, r.status))
                if link_id == "UserSpecificContextLinks" and r.ok:
                    self.st.user_links = parse_links(r.json())
            except NativeError as e:
                log.info("willhaben app API probe %s: %s", link_id, e)
                out.append((link_id + suffix, None))
        self.st.probed = PROBE_VERSION
        save_state(self.st)
        log.info("willhaben app API probe: %s", ", ".join(f"{k}={v}" for k, v in out))
        return out

WEB_PREFIXES = (     # the web's /webapi path -> (context link, path after its host; None: the link's URI itself)
    ("/webapi/chat-api/v1/", "ChatApiCounter", "/chat-api/v1/"),
    ("/webapi/iad/", "IAD_API_BASE_URL", "/restapi/v2/"),
    ("/webapi/myatz/", "showPagedAdsFromUserLink", "/myatz/v1/"),
    ("/webapi/p2p-aggregator/restapi/v2/", "IAD_API_BASE_URL", "/restapi/v2/"),   # the app lists p2pp under iad
    ("/webapi/convoad/v1/chat/ad_list/", "ChatInitialCTAConvoAd", None),
)
WEB_ME = "/webapi/userprofile-service/userprofile/me"
MY_ADS_ROWS = 50
MY_ADS_PAGES = 6


def native_url(c: NativeClient, path: str) -> str | None:
    """The app API's URL for a web /webapi path with the same answer, or None (stays with the browser)."""
    if path.startswith("https://www.willhaben.at/"):
        path = path[len("https://www.willhaben.at"):]
    if path.split("?")[0] == WEB_ME:
        base = c.link("UserDataResource")
        return f"{base.rstrip('/')}/{c.st.user_id}" if base and c.st.user_id else None
    for prefix, link_id, middle in WEB_PREFIXES:
        if path.startswith(prefix) and (base := c.link(link_id)):
            if middle is None:
                return base.rstrip("/") + "/" + path[len(prefix):]
            host = "{0.scheme}://{0.netloc}".format(urlsplit(base))
            return host + middle + path[len(prefix):]
    return None


def active() -> NativeClient | None:
    """The app API client when it is logged in and not switched off (setting willhaben_native = 0)."""
    from app.db import get_setting

    if (get_setting("willhaben_native") or "1") == "0":
        return None
    c = get_client()
    return c if c.logged_in else None


# writes that go to the app API too: the same backends behind both (the action links in the app's own ads list point
# there). Chat messages, new chats and photo uploads are not plain path changes and stay with the browser.
WRITE_PREFIXES = ("/webapi/myatz/", "/webapi/iad/", "/webapi/p2p-aggregator/", "/webapi/chat-api/v1/conversations")


def route(method: str, path: str, accept: str = "application/json") -> str | None:
    """Where an account request of the browser goes instead: the app API's URL for JSON reads and for the writes of
    WRITE_PREFIXES, else None."""
    if "json" not in accept or not (c := active()):
        return None
    if method.upper() != "GET":
        web = path[len("https://www.willhaben.at"):] if path.startswith("https://www.willhaben.at/") else path
        if not web.startswith(WRITE_PREFIXES):
            return None
    return native_url(c, path)


def falls_back(status: int, method: str = "GET") -> bool:
    """An app API answer the browser should give instead. Reads: login gone there, not found, its error. Writes only
    when willhaben surely did nothing (refused or no such endpoint): after a 5xx it may have, so no second try."""
    if method.upper() == "GET":
        return status in (401, 403, 404) or status >= 500
    return status in (401, 403, 404, 405)


async def my_ads_list() -> dict[str, Any] | None:
    """The account's ads as the app lists them (same shape as the my-adverts page's initialUserAdsList, all pages
    merged), or None when the app API is not there for it."""
    c = active()
    url = c.link("showPagedAdsFromUserLink") if c else None
    if not c or not url:
        return None
    merged: dict[str, Any] | None = None
    for page in range(1, MY_ADS_PAGES + 1):
        r = await c.request("GET", f"{url}?page={page}&rows={MY_ADS_ROWS}", kind="myadverts")
        if not r.ok:
            raise NativeError(f"own ads: HTTP {r.status}")
        data = r.json()
        if merged is None:
            merged = data
        else:
            for vertical in data.get("verticalList") or []:
                into = next((v for v in merged["verticalList"] if v.get("verticalId") == vertical.get("verticalId")), None)
                if into is None:
                    merged["verticalList"].append(vertical)
                else:
                    into.setdefault("advertDetails", []).extend(vertical.get("advertDetails") or [])
        have = sum(len(v.get("advertDetails") or []) for v in merged.get("verticalList") or [])
        total = merged.get("totalNumberOfAdvertDetails")
        if not isinstance(total, int) or have >= total or not any(v.get("advertDetails") for v in data.get("verticalList") or []):
            break
    return merged


_client: NativeClient | None = None


def get_client() -> NativeClient:
    global _client
    if _client is None:
        _client = NativeClient()
    return _client


async def login_now(user: str, password: str) -> str:
    """Log in to the app API with these details now (the settings button): a fresh login even when one is there.
    Returns what to tell the owner; a code by e-mail leaves the login waiting for submit_code()."""
    n = get_client()
    try:
        await n.login(user, password)
    except CodeNeeded as e:
        return str(e)
    except LoginRefused as e:
        n.st.error = f"login refused: {e}"
        save_state(n.st)
        return t("willhaben refused the login: {reason}", reason=e)
    await _after_login(n)
    return t("Logged in to the app API")


async def code_now(code: str) -> str:
    n = get_client()
    try:
        await n.submit_code(code)
    except CodeNeeded:
        return t("The code did not fit – enter the code from the newest e-mail")
    except LoginRefused as e:
        return str(e)
    await _after_login(n)
    return t("Logged in to the app API")


async def _after_login(n: NativeClient) -> None:
    from app.willhaben.session import get_session

    with_ids = _probe_ids(get_session())
    if not n.st.user_id:
        n.st.user_id = with_ids["uid"]
    if not n.st.user_id:                     # the browser does not know it: the access token may
        n.st.user_id = token_user_id(n.st.access_token)
    save_state(n.st)
    if n.st.user_id:
        try:
            await n.load_user_links()
        except NativeError as e:
            log.info("willhaben app API: user links not loaded: %s", e)


async def keep_job() -> None:
    """Scheduler: app token fresh; without a login yet, logged in with the stored password (Keycloak's cookies from the
    last login make the device known), or through the browser's willhaben login when no password is stored. A code by
    e-mail is announced and waits for the owner; a refused or waiting login pauses the automatic tries for a day (no
    e-mail per half hour, no retry loop against Keycloak)."""
    from app.services import credentials
    from app.services.notify import notify
    from app.willhaben.session import get_session

    n = get_client()
    try:
        await n.ensure_app_token()
        if not n.logged_in and n.pending is None and n.st.paused_until <= time.time():
            if cred := credentials.get("willhaben"):
                try:
                    await n.login(cred["user"], cred["password"])
                except CodeNeeded as e:
                    n.st.error, n.st.paused_until = str(e), time.time() + LOGIN_PAUSE_S
                    save_state(n.st)
                    await notify("health", t("willhaben login: code needed"), str(e), url="/settings/willhaben")
                    return
                except LoginRefused as e:
                    n.st.error, n.st.paused_until = f"login refused: {e}", time.time() + LOGIN_PAUSE_S
                    save_state(n.st)
                    log.warning("willhaben app API: login refused (%s), next try in a day", e)
                    return
                await _after_login(n)
            else:
                sess = get_session()
                if sess.started and sess.logged_in and getattr(sess, "context", None) is not None:
                    try:
                        await n.login_via_browser(sess)
                    except LoginRefused as e:
                        log.info("willhaben app API: no login through the browser (%s)", e)
        if not n.logged_in:
            return
        await n.ensure_access()
        if not n.st.user_id and (uid := (get_session().user or {}).get("id")):
            n.st.user_id = str(uid)
            save_state(n.st)
        if n.st.user_id and not n.st.user_links:
            await n.load_user_links()
        if n.st.probed != PROBE_VERSION:
            await n.probe(_probe_ids(get_session()))
    except (NativeError, httpx.HTTPError) as e:
        n.st.error = str(e)[:300]
        save_state(n.st)
        log.warning("willhaben app API: %s", e)

def _probe_ids(sess: Any) -> dict[str, str]:
    """The ids the probe fills in: the account's (from the browser's login) and one own ad and one conversation."""
    from sqlmodel import select

    from app.db import Conversation, MyAd, session

    u = getattr(sess, "user", None) or {}
    with session() as db:
        ad = db.exec(select(MyAd.ad_id).where(MyAd.status == "active")).first()
        conv = db.exec(select(Conversation.id).order_by(Conversation.last_message_at.desc())).first()
    return {"uid": str(u.get("id") or ""), "uuid": str(u.get("uuid") or ""), "ad": str(ad or ""), "conv": str(conv or "")}
