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

`route()` sends the browser's account reads here once logged in: the app API serves the same backends with the same
answers, only under other hosts (seen live 2026-10-30 for the profile, the chats, an ad's edit data and images). Writes,
pages and the chat WebSocket stay with the browser until each was tried live. `probe()` reads a few of the account's own
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
    """Keycloak did not hand out a code: wrong password, a captcha or a code by e-mail (finish it in the app)."""


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
        return {"logged_in": self.logged_in, "user": st.login_user or None, "links": len(st.links),
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
        """The app's Keycloak login, the form filled in with the stored password. LoginRefused when no code comes."""
        await self.ensure_app_token()
        verifier, challenge = pkce()
        state = _b64url(os.urandom(12))
        params = {"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": "openid",
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "ui_locales": "de"}
        auth_url = self._sso("auth")
        host = urlsplit(auth_url).hostname or "sso.willhaben.at"
        browser = {"user-agent": LOGIN_UA, "accept": "text/html,application/xhtml+xml",
                   "accept-language": "de-AT,de;q=0.9", "x-requested-with": "at.willhaben"}
        async with self._client(headers=browser, follow_redirects=False) as c:
            c.cookies.set("wh-fp", self.st.fp, domain=host, path="/auth/realms/willhaben")
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
                # never the body: it holds the password
                recorder.record_http("account", "login", "POST", str(r.url).split("?")[0], status=r.status_code,
                                     store_response=False)
                for _ in range(5):                     # Keycloak may pass through its own pages first
                    code = self._code(r)
                    if code is not None or r.status_code not in (301, 302, 303) or not r.headers.get("location"):
                        break
                    async with self.limiter:
                        r = await c.get(urljoin(str(r.url), r.headers["location"]))
                if code is None:
                    raise LoginRefused(refusal(r.text))
            if code[1] != state:
                raise LoginRefused("state mismatch")
            await self._token({"grant_type": "authorization_code", "code": code[0], "redirect_uri": REDIRECT_URI,
                               "client_id": CLIENT_ID, "code_verifier": verifier}, c)
        self.st.login_user, self.st.logged_in_at, self.st.error, self.st.paused_until = user, time.time(), "", 0
        save_state(self.st)
        log.info("willhaben app API: logged in")

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

WEB_PREFIXES = (     # the web's /webapi path -> (context link whose URI starts the app API's, path in between)
    ("/webapi/chat-api/v1/", "ChatApiCounter", "/chat-api/v1/"),
    ("/webapi/iad/", "IAD_API_BASE_URL", "/restapi/v2/"),
)
WEB_ME = "/webapi/userprofile-service/userprofile/me"


def native_url(c: NativeClient, path: str) -> str | None:
    """The app API's URL for a web /webapi path with the same answer, or None (stays with the browser)."""
    if path.startswith("https://www.willhaben.at/"):
        path = path[len("https://www.willhaben.at"):]
    if path.split("?")[0] == WEB_ME:
        base = c.link("UserDataResource")
        return f"{base.rstrip('/')}/{c.st.user_id}" if base and c.st.user_id else None
    for prefix, link_id, middle in WEB_PREFIXES:
        if path.startswith(prefix) and (base := c.link(link_id)):
            host = "{0.scheme}://{0.netloc}".format(urlsplit(base))
            return host + middle + path[len(prefix):]
    return None


def route(method: str, path: str, accept: str = "application/json") -> str | None:
    """Where an account request of the browser goes instead: the app API's URL for reads (GET of JSON) once logged in
    there, else None. Switched off with the setting willhaben_native = 0."""
    if method.upper() != "GET" or "json" not in accept:
        return None
    from app.db import get_setting

    if (get_setting("willhaben_native") or "1") == "0":
        return None
    c = get_client()
    if not c.logged_in:
        return None
    return native_url(c, path)

_client: NativeClient | None = None


def get_client() -> NativeClient:
    global _client
    if _client is None:
        _client = NativeClient()
    return _client


async def keep_job() -> None:
    """Scheduler: app token fresh; without a login yet, logged in through the browser's willhaben login (silent), else
    with the stored password; the probe once after the first login. A refused password login pauses for a day (no retry
    loop against Keycloak)."""
    from app.services import credentials
    from app.willhaben.session import get_session

    n = get_client()
    try:
        await n.ensure_app_token()
        if not n.logged_in:
            sess = get_session()
            if sess.started and sess.logged_in and getattr(sess, "context", None) is not None:
                try:
                    await n.login_via_browser(sess)
                except LoginRefused as e:
                    log.info("willhaben app API: no login through the browser (%s)", e)
        if not n.logged_in:
            cred = credentials.get("willhaben")
            if not cred or n.st.paused_until > time.time():
                return
            try:
                await n.login(cred["user"], cred["password"])
            except LoginRefused as e:
                n.st.error, n.st.paused_until = f"login refused: {e}", time.time() + LOGIN_PAUSE_S
                save_state(n.st)
                log.warning("willhaben app API: login refused (%s), next try in a day", e)
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
