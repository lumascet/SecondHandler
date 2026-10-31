"""Dashboard login: a login page with a signed session cookie (no browser popup).

Active only when DASHBOARD_PASSWORD is set. Browsers get a cookie from /login; scripts, the MCP server and
other API clients can keep sending HTTP Basic auth. Changing the password or user ends all sessions, because
the cookie signature covers both. The AI agent's bearer token (valid_bearer) opens the JSON API (/api/…) only.
Opened from a Telegram button as a Mini App, the page logs in with Telegram's
signed launch data instead (telegram_user(), /login/telegram): only the linked chat's user, no password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.config import get_settings

COOKIE = "wh_session"
REMEMBER_S = 30 * 86400      # "stay logged in"
SESSION_S = 12 * 3600        # otherwise: browser session, at most 12 h
PUBLIC_PATHS = {"/healthz", "/login", "/login/telegram", "/lang", "/sw.js"}   # /sw.js: the offline page's worker    # /lang: the language switch on the login page
PUBLIC_PREFIXES = ("/static/",)    # stylesheets: the login page needs them before any login

MAX_FAILS, FAIL_WINDOW_S = 10, 15 * 60
TELEGRAM_MAX_AGE_S = 24 * 3600   # Telegram's launch data older than this logs in no more (a Mini App opened yesterday)
_fails: dict[str, deque[float]] = defaultdict(deque)


def enabled() -> bool:
    """A dashboard password is set (DASHBOARD_PASSWORD): every request needs a login."""
    return bool(get_settings().dashboard_password)


def public_host() -> str | None:
    """The host of the configured web address (Settings → Web address, PUBLIC_URL) when it is one from outside the
    home network; None for localhost or a private address."""
    host = (urlsplit(get_settings().public_url or "").hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    return None if ip.is_private or ip.is_loopback else host


def login_needed(headers: Any) -> bool:
    """Does this request need a login? Always with a password. Without one the dashboard stays open in the home
    network, but a request that came in over the public web address needs the login of the linked Telegram account
    (the Mini App, /login/telegram) – otherwise anyone who learns the address could use it. Host and
    X-Forwarded-Host both count: the proxy routes by the host, a client cannot change that one."""
    if enabled():
        return True
    pub = public_host()
    if not pub:
        return False
    hosts = [headers.get("host") or "", *(headers.get("x-forwarded-host") or "").split(",")]
    return any(h.strip().lower().rsplit(":", 1)[0] == pub for h in hosts if h.strip())


def _secret() -> bytes:
    path = get_settings().data_dir / "session-secret"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        path.chmod(0o600)
        return key


def _sign(payload: str) -> str:
    s = get_settings()
    msg = f"{payload}|{s.dashboard_user}|{s.dashboard_password}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_token(remember: bool, now: float | None = None) -> str:
    exp = int((now or time.time()) + (REMEMBER_S if remember else SESSION_S))
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    if not token or token.count(".") != 2:
        return False
    payload, _, sig = token.rpartition(".")
    try:
        exp = int(payload.split(".")[0])
    except ValueError:
        return False
    return exp > (now or time.time()) and hmac.compare_digest(sig, _sign(payload))


def check_password(user: str, password: str) -> bool:
    s = get_settings()
    return bool(s.dashboard_password) and secrets.compare_digest(user.encode(), s.dashboard_user.encode()) \
        and secrets.compare_digest(password.encode(), s.dashboard_password.encode())


def valid_basic(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return check_password(user, pw)


def valid_bearer(header: str) -> bool:
    """`Authorization: Bearer <token>` with the AI agent's API token (Settings → AI agent, setting agent_token). No token
    set: never valid. Compared in constant time. The caller lets it open the JSON API (/api/…) only."""
    from app.db import get_setting
    from app.services.agent_events import SETTING_TOKEN
    scheme, _, sent = header.partition(" ")
    expected = get_setting(SETTING_TOKEN).strip()
    sent = sent.strip()
    return scheme.lower() == "bearer" and bool(expected) and bool(sent) \
        and secrets.compare_digest(sent.encode(), expected.encode())


def locked_out(client: str, now: float | None = None) -> bool:
    """Too many wrong passwords from this address in the last 15 minutes."""
    now = now or time.time()
    q = _fails[client]
    while q and q[0] < now - FAIL_WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


def record_failure(client: str, now: float | None = None) -> None:
    _fails[client].append(now or time.time())


def safe_next(target: str | None) -> str:
    """Only local paths, so the login form cannot be used to redirect elsewhere: no "//", no backslash, no control
    characters or spaces (a browser drops a tab: "/\t/evil.com" would be "//evil.com"), no encoded slashes."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target \
            or any(ord(c) <= 0x20 or ord(c) == 0x7f for c in target) or re.search(r"%(2f|5c)", target, re.I):
        return "/"
    return target


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def debug_login(headers: Any, client: str | None) -> bool:
    """The debug login (DEBUG_TOKEN): the X-Debug-Token header with that token, sent straight from the home network.
    A request the reverse proxy forwarded (X-Forwarded-*: it may come from the internet) never counts, nor one from a
    public address."""
    token = get_settings().debug_token
    sent = headers.get("x-debug-token") or ""
    if not token or len(token) < 16 or not sent or headers.get("x-forwarded-for") or headers.get("x-forwarded-host"):
        return False
    try:
        ip = ipaddress.ip_address(client or "")
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback) and secrets.compare_digest(sent.encode(), token.encode())


def cross_site(method: str, headers: Any) -> bool:
    """A state-changing browser request from another origin (CSRF): its Origin names another host than the one it was
    sent to, or the browser says cross-site. Scripts with an Authorization header and requests without these browser
    headers (curl, the tests) pass."""
    if method.upper() not in UNSAFE_METHODS or headers.get("authorization"):
        return False
    origin = headers.get("origin")
    if origin:
        host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip().lower()
        return origin == "null" or urlsplit(origin).netloc.lower() != host
    return headers.get("sec-fetch-site") == "cross-site"


def telegram_user(init_data: str | None, bot_token: str | None, now: float | None = None) -> int | None:
    """The Telegram user of a Mini App launch (the URL fragment's tgWebAppData, Telegram.WebApp.initData), when Telegram
    signed it with this bot's token and it is at most TELEGRAM_MAX_AGE_S old; None otherwise. The check of
    core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app: HMAC-SHA256 over the other fields,
    sorted, "key=value" per line, keyed with HMAC-SHA256("WebAppData", bot token)."""
    if not init_data or not bot_token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    sent = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not sent or not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), sent):
        return None
    try:
        signed_at = int(fields.get("auth_date", ""))
        user = int(json.loads(fields.get("user") or "{}")["id"])
    except (ValueError, KeyError, TypeError):
        return None
    return user if abs((now or time.time()) - signed_at) <= TELEGRAM_MAX_AGE_S else None
