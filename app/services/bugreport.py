"""Anonymised bug report: app/runtime facts, connection states, activity counts, recent warnings/errors.

Built locally from in-app state only (no willhaben, Claude, HA or Telegram request) and NEVER sent anywhere by
the app: the owner sees it on /bugreport and may copy or download it. Everything that goes into the report passes
through one `Anonymizer`, which removes secrets, contact data, addresses, people's names, chat texts and ad titles
and replaces ids by placeholders that stay stable within one report (ad#1, conv#2, user#1, ...), so references in
different log lines still line up. When unsure it removes rather than keeps.

Sections: description (the owner's text), app, connections, activity, errors, settings (optional; values only
for an allow-list of harmless keys).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import sys
import time
from datetime import timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from sqlmodel import col, func, select

from app import __version__
from app.config import get_settings
from app.db import (
    DEFAULT_SETTINGS, AdBatch, AdDraft, Conversation, DealCheck, Draft, EventLog, Hit, LLMCall, Message, MyAd,
    SearchAgent, Setting, Shipment, TimelineEvent, get_setting, session, utcnow,
)
from app.services import logbuffer

log = logging.getLogger(__name__)

PACKAGES = ("fastapi", "starlette", "uvicorn", "pydantic", "sqlmodel", "httpx", "apscheduler", "playwright", "mcp")
REPO = Path(__file__).resolve().parent.parent.parent

# What the anonymiser removes; shown on the page and at the top of the report.
REMOVED = ("tokens, passwords, cookies and other secrets", "e-mail addresses and phone numbers",
           "IP addresses and your server's host names", "URL query strings and logins in URLs",
           "names of people (chat partners, your account, Telegram, shipping addresses)",
           "chat messages, ad titles and descriptions (only counts are included)",
           "ad, conversation and user ids (replaced by placeholders such as ad#1)")

# ------------------------------------------------------------------ anonymiser patterns
_SK_ANT = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_TELEGRAM = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_\-]{30,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]*){0,2}")
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic|token)(\s+)(?!<)[A-Za-z0-9._~+/=\-]{8,}")
_COOKIE_LINE = re.compile(r"(?i)\b(set-cookie|cookie)([\"']?\s*[:=]\s*)[^\n]+")
# Playwright / browser cookie objects: {'name': 'IADVISITOR', 'value': '...', 'domain': ...}
_COOKIE_OBJ = re.compile(r"(?i)[\"'](?:domain|name|httponly|samesite|expires)[\"']\s*:")
_COOKIE_VALUE = re.compile(r"(?i)([\"']value[\"']\s*:\s*[\"'])[^\"']*")
# password-like keys: the value may contain spaces, so it runs to the closing quote or the end of the line
_PASSWORD_KV = re.compile(r"(?i)\b([\w\-]*(?:passw(?:or)?d|pwd|passphrase)[\w\-]*)([\"']?\s*[:=]\s*[\"']?)(?!<)[^\n\"']+")
_SECRET_KV = re.compile(
    r"(?i)\b([\w\-]*(?:token|secret|passw(?:or)?d|pwd|sess(?:ion)?(?:[_\-]?(?:id|key))?|csrf|api[_\-]?key|apikey|"
    r"access[_\-]?key|private[_\-]?key|authorization|refresh|code[_\-]?verifier|signature|hmac|cookie)[\w\-]*)"
    r"(\"?'?\s*[:=]\s*\"?'?)(?!<)([^\s\"'&,;)}\]]+)")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s<>\"'`)\]]+")
_QUERY = re.compile(r"\?[\w.%\-\[\]]+=[^\s\"'<>`)]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]*\d)")
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%\w+)?(?![\w:])")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PHONE_INTL = re.compile(r"(?<![\w+])(?:\+|00)[1-9]\d{0,3}(?:[ /\-]?\(0\))?(?:[ /\-]?\d{1,6}){1,6}(?!\d)")
_PHONE_NAT = re.compile(r"(?<![\w./:+\-])0\d{1,4}(?:[ /.\-]\d{2,8}){1,3}(?![\w])")
_DATE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}")
_PHONE_COMPACT = re.compile(r"(?<![\w./:+\-])0[1-9]\d{8,12}(?!\d)")
_AD_ID = re.compile(r"(?<![\w.#])\d{9,10}(?![\w.]*\d)")
_ADID_PARAM = re.compile(r"(?i)(ad_?id[\"']?\s*[=:]\s*[\"']?)(\d{5,12})")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_B64 = re.compile(r"(?<![\w+/\-])[A-Za-z0-9+/_\-]{32,}={0,2}")
_HOME = re.compile(r"/home/[^/\s]+")
# willhaben ad URL slug (the ad title): /d/<title-slug>-<adid>/, also in paths logged without scheme and host
_AD_SLUG = re.compile(r"(/d/)[^/\s?#\"'<>]*?-?(\d{5,12})(?=[/?#\s\"'<>]|$)")
# any other title-like slug in front of an id: two or more hyphenated words, then -<id>
_ID_SLUG = re.compile(r"(?<=/)[A-Za-z0-9%]+(?:-[A-Za-z0-9%]+){2,}(?=-\d{5,12}(?:/|$))")
_NUM_SEGMENT = re.compile(r"(?<=/)\d{6,}(?=/|$)")
_LONG_SLUG = re.compile(r"(?<=/)(?=[^/]{21,})[^/]*[A-Za-z][^/]*-[^/]*(?=/|$)")
_WILLHABEN_HOSTS = ("willhaben.at",)
_NOT_NAMES = {"none", "true", "false", "error", "info", "warning", "user", "admin", "test", "hallo", "null", "unknown",
              "auto", "chat", "claude", "willhaben", "telegram", "agent", "search", "draft", "debug", "ok", "ja", "nein"}


def _digits(s: str) -> int:
    return sum(ch.isdigit() for ch in s)


class Anonymizer:
    """Removes personal data and secrets from text. One instance per report keeps placeholders stable."""

    def __init__(self, *, secrets: Iterable[str] = (), hosts: Iterable[str] = (), names: Iterable[str] = (),
                 texts: Iterable[str] = (), ids: dict[str, str] | None = None, data_dir: Path | str | None = None):
        self.secrets = sorted({s.strip() for s in secrets if s and len(s.strip()) >= 4}, key=len, reverse=True)
        # short secrets only as whole words (a 5-letter password must not eat parts of other words)
        self._short_secrets = {sec: re.compile(r"(?<!\w)" + re.escape(sec) + r"(?!\w)")
                               for sec in self.secrets if len(sec) < 8}
        self.hosts = sorted({h.lower().strip() for h in hosts if h and h.strip().lower() not in ("localhost", "")},
                            key=len, reverse=True)
        parts: set[str] = set()
        for n in names:
            n = (n or "").strip()
            if len(n) >= 2 and n.lower() not in _NOT_NAMES:      # a whole name may be short ("Bo"), parts not
                parts.add(n)
            parts.update(p for p in re.split(r"[\s,]+", n) if len(p) >= 3 and p.lower() not in _NOT_NAMES)
        self.names = sorted(parts, key=len, reverse=True)
        self._name_rx = {n: re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.IGNORECASE) for n in self.names}
        prefixes: set[str] = set()
        for x in texts:
            x = " ".join((x or "").split())
            if len(x) >= 6:
                prefix = x[:24]
                prefixes.add(prefix)
                # the same text inside JSON (escaped quotes, \u00fc for umlauts)
                prefixes.add(json.dumps(prefix)[1:-1])
                prefixes.add(json.dumps(prefix, ensure_ascii=False)[1:-1])
        self.texts = sorted(prefixes, key=len, reverse=True)
        # case- and whitespace-insensitive; the longest word is a cheap pre-filter before the regex runs
        self._text_rx = [(max(p.split(), key=len).lower(),
                          re.compile(r"\s+".join(map(re.escape, p.split())) + r"[^\n]*", re.IGNORECASE))
                         for p in self.texts]
        self.ids = dict(ids or {})                     # exact value -> kind (conv, user, ad, tg)
        self.data_dirs = sorted({str(p).rstrip("/") for p in self._dirs(data_dir) if len(str(p)) > 1},
                                key=len, reverse=True)
        self._dir_rx = [re.compile(r"(?<![\w/.\-])" + re.escape(d) + r"(?![\w\-])") for d in self.data_dirs]
        self._numbers: dict[str, dict[str, int]] = {}

    @staticmethod
    def _dirs(data_dir: Path | str | None) -> list[str]:
        if not data_dir:
            return []
        p = Path(data_dir)
        out = [str(p)] if p.is_absolute() else []      # a relative "data" would hit every "metadata"
        try:
            out.append(str(p.resolve()))
        except OSError:
            pass
        return [d for d in out if d not in (".", "/")]

    def placeholder(self, kind: str, value: str) -> str:
        table = self._numbers.setdefault(kind, {})
        if value not in table:
            table[value] = len(table) + 1
        return f"{kind}#{table[value]}"

    # -------------------------------------------------------------- steps
    def _url(self, m: re.Match) -> str:
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in ".,;:!?":
            raw, trail = raw[:-1], raw[-1] + trail
        try:
            u = urlsplit(raw)
            host = u.hostname or ""
            port = f":{u.port}" if u.port else ""
        except ValueError:
            return "<url>" + trail
        lhost = host.lower()
        path = self._path(u.path, lhost)
        if self._is_home(lhost):
            host = "<host>"
        else:
            try:
                ip = ipaddress.ip_address(host)
                if not ip.is_loopback and not ip.is_unspecified:
                    host = "<ip>"
            except ValueError:
                pass
        netloc = (host if ":" not in host or host.startswith("<") else f"[{host}]") + port
        query = "<query>" if u.query else ""
        return urlunsplit((u.scheme, netloc, path, query, "")) + trail

    def _is_home(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def _id_placeholder(self, value: str) -> str:
        kind = self.ids.get(value) or ("ad" if 9 <= len(value) <= 10 else "id")
        return "<telegram-chat>" if kind == "tg" else self.placeholder(kind, value)

    def _path(self, path: str, host: str) -> str:
        """URL path: ad title slugs and numeric ids (seller profiles, ...) out."""
        path = _AD_SLUG.sub(lambda m: m.group(1) + "<slug>-" + self.placeholder("ad", m.group(2)), path)
        path = _ID_SLUG.sub("<slug>", path)
        path = _NUM_SEGMENT.sub(lambda m: self._id_placeholder(m.group(0)), path)
        if host == "willhaben.at" or host.endswith(tuple("." + h for h in _WILLHABEN_HOSTS)):
            path = _LONG_SLUG.sub("<slug>", path)
        return path

    @staticmethod
    def _ip(m: re.Match) -> str:
        s = m.group(0)
        try:
            ip = ipaddress.ip_address(s.split("%")[0])
        except ValueError:
            return s
        return s if ip.is_loopback or ip.is_unspecified else "<ip>"

    def _uuid(self, m: re.Match) -> str:
        v = m.group(0).lower()
        return self.placeholder(self.ids.get(v, "id"), v)

    @staticmethod
    def _phone(m: re.Match) -> str:
        s = m.group(0)
        return "<phone>" if _digits(s) >= 8 and not _DATE.fullmatch(s) else s

    @staticmethod
    def _b64(m: re.Match) -> str:
        s = m.group(0)
        if sum(c.isupper() for c in s) >= 2 and sum(c.islower() for c in s) >= 2 and _digits(s) >= 3:
            return "<secret>"
        return s

    def clean(self, text: Any) -> str:
        if text is None:
            return ""
        s = str(text)
        if not s:
            return s
        # 1. exact secrets the app knows (tokens, passwords) - before anything can split them up
        for sec in self.secrets:
            if sec in s:
                rx = self._short_secrets.get(sec)
                s = rx.sub("<secret>", s) if rx else s.replace(sec, "<secret>")
        # 2. token shapes
        s = _SK_ANT.sub("<claude-token>", s)
        s = _TELEGRAM.sub("<telegram-token>", s)
        s = _JWT.sub("<jwt>", s)
        # URLs (user-info, query, fragment, home host), then stray query strings
        s = _URL.sub(self._url, s)
        s = _AD_SLUG.sub(lambda m: m.group(1) + "<slug>-" + self.placeholder("ad", m.group(2)), s)  # bare paths
        s = _QUERY.sub("?<query>", s)
        s = _COOKIE_LINE.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", s)
        if _COOKIE_OBJ.search(s):
            s = _COOKIE_VALUE.sub(lambda m: m.group(1) + "<redacted>", s)
        s = _AUTH_HEADER.sub(lambda m: m.group(1) + m.group(2) + "<secret>", s)
        s = _PASSWORD_KV.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", s)
        s = _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", s)
        # 3. chat texts, ad titles, descriptions: the known start of the text and the rest of that line
        low = s.lower()
        for word, rx in self._text_rx:
            if word in low:
                s2 = rx.sub("<text>", s)
                if s2 != s:
                    s, low = s2, s2.lower()
        # 4. contact data and network addresses
        s = _EMAIL.sub("<email>", s)
        low = s.lower()
        for host in self.hosts:
            if host in low:                            # the host and every other subdomain of it
                s = re.sub(r"(?i)(?<![\w.\-])(?:[\w\-]+\.)*" + re.escape(host) + r"(?![\w\-])", "<host>", s)
                low = s.lower()
        s = _IPV4.sub(self._ip, s)
        s = _IPV6.sub(self._ip, s)
        # 6. ids -> stable placeholders
        s = _UUID.sub(self._uuid, s)
        for value, kind in self.ids.items():
            if not _UUID.fullmatch(value) and value in s:
                if kind == "tg":
                    s = re.sub(r"(?<![\w#])" + re.escape(value) + r"(?!\w)", "<telegram-chat>", s)
                else:
                    s = re.sub(r"(?<![\w#])" + re.escape(value) + r"(?!\w)",
                               lambda m, k=kind: self.placeholder(k, m.group(0)), s)
        s = _ADID_PARAM.sub(lambda m: m.group(1) + self.placeholder("ad", m.group(2)), s)
        s = _PHONE_INTL.sub(self._phone, s)
        s = _PHONE_NAT.sub(self._phone, s)
        s = _PHONE_COMPACT.sub("<phone>", s)
        s = _AD_ID.sub(lambda m: self.placeholder("ad", m.group(0)), s)
        # 7. remaining long random strings
        s = _HEX.sub("<hex>", s)
        s = _B64.sub(self._b64, s)
        # 8. people's names (any case, whole words)
        low = s.lower()
        for name in self.names:
            if name.lower() in low:
                s = self._name_rx[name].sub("<name>", s)
                low = s.lower()
        # 9. file paths: data dir relative, no user names from home directories
        for d, rx in zip(self.data_dirs, self._dir_rx, strict=True):
            if d in s:
                s = rx.sub("<data>", s)
        s = _HOME.sub("/home/<user>", s)
        return s

    def __call__(self, text: Any) -> str:
        return self.clean(text)


# ------------------------------------------------------------------ what the app knows (for the anonymiser)
def _read(path: Path, limit: int = 64 * 1024) -> str:
    try:
        if path.is_file() and path.stat().st_size <= limit:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        pass
    return ""


_SECRET_KEY = re.compile(r"(?i)secret|token|password|passwd|pwd|user|login|client")


def _json_pairs(data: Any, key: str = "") -> list[tuple[str, str]]:
    """(nearest key, string value) for every string in a JSON document."""
    if isinstance(data, str):
        return [(key, data)]
    if isinstance(data, dict):
        return [x for k, v in data.items() for x in _json_pairs(v, str(k))]
    if isinstance(data, list):
        return [x for v in data for x in _json_pairs(v, key)]
    return []


def carrier_login_values() -> tuple[list[str], list[str]]:
    """(users + passwords, users) from DATA_DIR/carrier-logins.json - short values too."""
    try:
        data = json.loads(_read(get_settings().data_dir / "carrier-logins.json") or "{}")
    except ValueError:
        return [], []
    secrets, users = [], []
    for v in (data.values() if isinstance(data, dict) else []):
        if isinstance(v, dict):
            for k in ("user", "password"):
                x = str(v.get(k) or "").strip()
                if len(x) >= 3:
                    secrets.append(x)
                    if k == "user":
                        users.append(x)
    return secrets, users


def known_secrets() -> list[str]:
    """Secret values from the environment and DATA_DIR (*-token, *-secret, *.json credential files)."""
    s = get_settings()
    out = [s.dashboard_password or "", s.debug_token or "", s.ha_token or "", s.claude_code_oauth_token or "", s.telegram_bot_token or "",
           os.environ.get("VNC_PASSWORD", ""), os.environ.get("HA_TOKEN", ""), os.environ.get("ANTHROPIC_API_KEY", ""),
           get_setting("agent_token"), get_setting("agent_webhook_secret")]     # the AI agent's (Settings → AI agent)
    d = s.data_dir
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except OSError:
        files = []
    for p in files:
        if p.name.endswith(("-token", "-secret")) or p.name in ("action-secret",):
            out.append(_read(p))
        elif p.name == "carrier-logins.json":
            out += carrier_login_values()[0]
        elif p.suffix == ".json":                       # google-calendar.json: client secret, refresh token
            try:
                out += [v for k, v in _json_pairs(json.loads(_read(p) or "null"))
                        if len(v) >= 16 or (len(v) >= 4 and _SECRET_KEY.search(k))]
            except ValueError:
                pass
    return [x for x in out if x]


# second-level registry suffixes: the owner's domain is the last three labels there
_SECOND_LEVEL = {"co.at", "or.at", "ac.at", "gv.at", "co.uk", "org.uk", "ac.uk", "me.uk", "com.au", "net.au", "org.au",
                 "co.nz", "com.br", "co.jp", "com.de", "co.de"}
# never treated as the owner's domain, even if a home host happens to sit below it
_PUBLIC_DOMAINS = {"willhaben.at", "anthropic.com", "claude.ai", "telegram.org", "google.com", "googleapis.com",
                   "duckdns.org", "github.io", "nip.io", "sslip.io", "ts.net", "home.arpa"}


def _registrable(host: str) -> str | None:
    labels = host.lower().strip(".").split(".")
    if len(labels) < 3:
        return None
    n = 3 if ".".join(labels[-2:]) in _SECOND_LEVEL else 2
    if len(labels) <= n:
        return None
    dom = ".".join(labels[-n:])
    return None if dom in _PUBLIC_DOMAINS else dom


def known_hosts() -> list[str]:
    from app.services.ha_auth import ha_url

    s = get_settings()
    urls = [s.public_url, s.ha_url, s.novnc_url, ha_url()]
    hosts = []
    for u in urls:
        if not u:
            continue
        try:
            h = urlsplit(u if "://" in u else "http://" + u).hostname
        except ValueError:
            h = None
        if h and h not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            hosts.append(h)
    hosts += [h.split(":")[0] for h in re.split(r"[,\s]+", os.environ.get("PROXY_HOSTS", "")) if h]
    # sibling subdomains of the owner's domain (ha.example.net next to willhaben.example.net) give it away too
    out = list(hosts)
    for h in hosts:
        try:
            ipaddress.ip_address(h)
            continue
        except ValueError:
            pass
        dom = _registrable(h)
        if dom:
            out.append(dom)
    return out


def _setting(db, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else ""


def build_anonymizer() -> Anonymizer:
    """Collects names, texts, ids and secrets the app knows, so they can be recognised in log lines."""
    from app.willhaben.session import get_session

    names: list[str] = []
    texts: list[str] = []
    ids: dict[str, str] = {}
    user = get_session().user or {}
    for k in ("nickname", "firstName", "lastName", "first_name", "last_name", "name", "displayName"):
        if isinstance(user.get(k), str):
            names.append(user[k])
    if user.get("uuid"):
        ids[str(user["uuid"]).lower()] = "user"
    if user.get("id"):
        ids[str(user["id"])] = "user"
    names += carrier_login_values()[1]

    def meetup(m: Any) -> list[str]:
        return [str(m.get(k) or "") for k in ("location", "summary")] if isinstance(m, dict) else []

    def address(rec: dict) -> list[str]:
        g = lambda k: str(rec.get(k) or "").strip()  # noqa: E731
        return ([g(k) for k in ("street", "extra", "city", "phone", "email")]
                + [f"{g('street')} {g('house_no')}".strip(), f"{g('zip')} {g('city')}".strip()])

    with session() as db:
        for k in ("telegram_chat_name", "telegram_username"):
            names.append(_setting(db, k))
        if _setting(db, "telegram_chat_id"):
            ids[_setting(db, "telegram_chat_id")] = "tg"
        try:
            sender = json.loads(_setting(db, "ship_sender") or "{}")
            if isinstance(sender, dict):
                names.append(str(sender.get("name") or ""))
                texts += address(sender)
        except ValueError:
            pass
        for k in ("reply_rules", "auto_reply_rules", "sell_notes", "ship_sender_geo", "gcal_calendars", "gcal_target"):
            texts += [line for line in _setting(db, k).splitlines() if len(line.strip()) >= 6]
        for cid, partner, title, last, meet in db.exec(select(
                Conversation.id, Conversation.partner_name, Conversation.ad_title, Conversation.last_message_text,
                Conversation.meetup)):
            ids[str(cid).lower()] = "conv"
            names.append(partner or "")
            texts += [title or "", last or ""] + meetup(meet)
        texts += list(db.exec(select(Message.text).order_by(col(Message.id).desc()).limit(1000)))
        for text, note, meet in db.exec(select(Draft.text, Draft.feedback_note, Draft.meetup)
                                        .order_by(col(Draft.id).desc()).limit(500)):
            texts += [text or "", note or ""] + meetup(meet)
        for heading, loc in db.exec(select(Hit.heading, Hit.location).order_by(col(Hit.id).desc()).limit(2000)):
            texts += [heading or "", loc or ""]
        for body in db.exec(select(Hit.body).where(col(Hit.body).is_not(None))
                            .order_by(col(Hit.id).desc()).limit(300)):
            texts += [line for line in str(body).splitlines()[:30] if len(line.strip()) >= 12]
        for heading, instr in db.exec(select(MyAd.heading, MyAd.auto_reply_instructions)):
            texts += [heading or "", instr or ""]
        for row in db.exec(select(AdDraft.title, AdDraft.input_text, AdDraft.description)
                           .order_by(col(AdDraft.id).desc()).limit(200)):
            texts += [x or "" for x in row]
        for title, query in db.exec(select(DealCheck.title, DealCheck.query).limit(500)):
            texts += [title or "", query or ""]
        texts += [x or "" for x in db.exec(select(AdBatch.notes).limit(200))]
        for name, brief in db.exec(select(SearchAgent.name, SearchAgent.screen_brief)):
            texts += [name or "", brief or ""]
        for rec, note in db.exec(select(Shipment.recipient, Shipment.note)):
            texts.append(note or "")
            if isinstance(rec, dict):
                names.append(str(rec.get("name") or ""))
                texts += address(rec)
    # long free texts: every line on its own, so a line logged alone is found too
    split_texts: list[str] = []
    for x in texts:
        split_texts.append(x)
        split_texts += [line for line in str(x).splitlines() if len(line.strip()) >= 12]
    return Anonymizer(secrets=known_secrets(), hosts=known_hosts(), names=names, texts=split_texts, ids=ids,
                      data_dir=get_settings().data_dir)


# ------------------------------------------------------------------ report sections
def _git_commit() -> str:
    for k in ("GIT_COMMIT", "SOURCE_COMMIT", "GITHUB_SHA"):
        if os.environ.get(k):
            return os.environ[k][:12]
    try:
        git = REPO / ".git"
        if git.is_file():                              # worktree: "gitdir: <path>"
            git = Path(git.read_text().split(":", 1)[1].strip())
            if not git.is_absolute():
                git = (REPO / git).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head[:12]
        ref = head[4:].strip()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text().strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text().strip()[:12] + f" ({ref.rsplit('/', 1)[-1]})"
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0][:12] + f" ({ref.rsplit('/', 1)[-1]})"
    except (OSError, IndexError):
        pass
    return "unknown"


def _uptime() -> str:
    secs = int(time.time() - logbuffer.STARTED)
    d, rest = divmod(secs, 86400)
    h, rest = divmod(rest, 3600)
    return (f"{d} d " if d else "") + f"{h} h {rest // 60} min"


def app_section() -> dict[str, Any]:
    s = get_settings()
    versions = {}
    for p in PACKAGES:
        try:
            versions[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            versions[p] = "not installed"
    return {
        "version": __version__, "commit": _git_commit(), "uptime": _uptime(),
        "python": platform.python_version(), "packages": versions,
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "docker": Path("/.dockerenv").exists(), "supervisord": bool(os.environ.get("SUPERVISOR_ENABLED")),
        "claude_cli_found": bool(shutil.which(s.claude_bin)), "browser_enabled": s.browser_enabled,
        "browser_headless": s.browser_headless, "executable": Path(sys.executable).name,
    }


def connections_section(anon: Anonymizer) -> dict[str, Any]:
    from app.llm import base
    from app.services import health

    items = [{"item": st.key, "state": st.state, "reason": anon(st.reason)} for st in health.collect()]
    last = base.last_call
    return {"items": items, "claude_last_call": {
        "ok": last.get("ok"), "at": last["at"].isoformat(timespec="seconds") if hasattr(last.get("at"), "isoformat") else None,
        "error": anon(last.get("error")) or None}}


def _count(db, model, *where) -> int:
    q = select(func.count()).select_from(model)
    for w in where:
        q = q.where(w)
    return int(db.exec(q).one())


def _by(db, column) -> dict[str, int]:
    return {str(k): int(n) for k, n in db.exec(select(column, func.count()).group_by(column))}


def activity_section(anon: Anonymizer, events: int = 30) -> dict[str, Any]:
    from app import scheduler
    from app.llm import usage
    from app.willhaben.public import willhaben_limiter

    day = utcnow() - timedelta(hours=24)
    with session() as db:
        counts = {
            "search_agents": _count(db, SearchAgent), "search_agents_enabled": _count(db, SearchAgent, SearchAgent.enabled),
            "search_agents_with_error": _count(db, SearchAgent, col(SearchAgent.last_error).is_not(None)),
            "hits": _count(db, Hit), "hits_last_24h": _count(db, Hit, Hit.first_seen_at >= day),
            "conversations": _count(db, Conversation), "messages": _count(db, Message),
            "drafts": _by(db, Draft.status), "my_ads": _by(db, MyAd.status), "ad_drafts": _by(db, AdDraft.status),
            "ad_batches": _by(db, AdBatch.status), "shipments": _by(db, Shipment.status),
            "price_checks": _count(db, DealCheck),
            # the timeline only as counts: its titles, prompts and willhaben answers hold chat texts and addresses
            "timeline_entries": _by(db, TimelineEvent.kind),
            "timeline_failures": {str(k): int(n) for k, n in db.exec(
                select(TimelineEvent.kind, func.count()).where(col(TimelineEvent.ok).is_(False)).group_by(TimelineEvent.kind))},
        }
        recent = list(db.exec(select(EventLog.ts, EventLog.kind).order_by(col(EventLog.id).desc()).limit(events)))
    llm = [{"purpose": g["purpose"], "model": g["model"], "calls": g["calls"], "failures": g["errors"],
            "last": g["last"].isoformat(timespec="seconds") if g.get("last") else None}
           for g in usage.summary()["groups"]]
    lim = willhaben_limiter()
    limiter = {"requests_last_hour": lim.last_hour, "budget_per_hour": lim.per_hour,
               "cooling_down_s": round(lim.cooling_down_s), "strikes": getattr(lim, "_strikes", None)}
    jobs = []
    try:
        for j in scheduler.scheduler.get_jobs():
            nxt = getattr(j, "next_run_time", None)
            jobs.append({"id": anon(j.id), "next_run": nxt.isoformat(timespec="seconds") if nxt else None})
    except Exception as e:  # noqa: BLE001 - scheduler not running (tests, restore)
        jobs.append({"id": "<unavailable>", "next_run": anon(str(e))[:100]})
    return {"counts": counts, "llm_calls": llm, "limiter": limiter, "scheduler_jobs": jobs,
            "recent_events": [{"ts": ts.isoformat(timespec="seconds"), "kind": kind} for ts, kind in recent]}


def errors_section(anon: Anonymizer, include_info: bool = False, llm_limit: int = 20) -> dict[str, Any]:
    logs = [{"ts": r["ts"].isoformat(timespec="seconds"), "level": r["level"], "logger": r["logger"],
             "message": anon(r["message"]), "traceback": anon(r["traceback"]) if r.get("traceback") else None}
            for r in logbuffer.records(include_info)]
    with session() as db:
        failed = list(db.exec(select(LLMCall).where(col(LLMCall.ok).is_(False))
                              .order_by(col(LLMCall.id).desc()).limit(llm_limit)))
        agents = list(db.exec(select(SearchAgent.id, SearchAgent.last_error, SearchAgent.last_run_at)
                              .where(col(SearchAgent.last_error).is_not(None))))
        ads = list(db.exec(select(MyAd.ad_id, MyAd.last_error).where(col(MyAd.last_error).is_not(None)).limit(20)))
    return {
        "log": logs,
        "llm_failures": [{"ts": c.ts.isoformat(timespec="seconds"), "purpose": c.purpose, "model": c.model,
                          "error": anon(c.error)} for c in reversed(failed)],
        "search_agent_errors": [{"agent": f"agent {aid}", "error": anon(err),
                                 "last_run": ts.isoformat(timespec="seconds") if ts else None} for aid, err, ts in agents],
        "my_ad_errors": [{"ad": anon.placeholder("ad", str(ad_id)), "error": anon(err)} for ad_id, err in ads],
    }


# Settings shown with their value; everything else only as <set> / <empty>.
SAFE_SETTINGS = {
    "agent_breaks", "agent_variance_pct", "auto_draft", "chat_sync_limit", "chat_sync_days", "expiry_warn_days", "myads_expired_days", "gcal_days", "gcal_duration", "gcal_in_auto",
    "gcal_in_drafts", "gcal_window_end", "gcal_window_start", "geizhals_auto_specs", "health_disk_min_mb", "llm_model",
    "notify_messages", "push_channel", "quiet_enabled", "quiet_end", "quiet_interval_min", "quiet_mode", "quiet_start",
    "reply_delay_max_s", "reply_delay_min_s", "reply_fast_max_s", "reply_fast_min_s", "reply_humanize", "send_verified",
    "sell_carrier", "sell_delivery", "sell_handover", "sell_payment", "sell_shipping_cost",
    "ship_carriers", "ship_detect", "ship_sort", "telegram_enabled", "telegram_kinds", "timeline_days", "timeline_enabled", "timeline_max_mb", "timeline_prune", "ui_lang",
    "ui_lang_seen", "wh_recording",
}
SAFE_ENV = {
    "llm_backend", "llm_model", "llm_timeout_s", "request_min_gap_s", "requests_per_hour", "default_agent_interval_s",
    "min_agent_interval_s", "search_rows", "browser_enabled", "browser_headless", "chat_poll_interval_s", "humanize_chat",
    "session_check_interval_s", "myads_check_hour", "ha_listener", "telegram_listener", "chat_live",
}
_SAFE_VALUE = re.compile(r"^[\w.:,\- ]{0,80}$")


def _safe_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    v = str(value)
    return v if _SAFE_VALUE.match(v) else "<set>"


def settings_section(anon: Anonymizer) -> dict[str, Any]:
    s = get_settings()
    env = {}
    for k, v in s.model_dump().items():
        if k in SAFE_ENV:
            env[k] = _safe_value(v)
        else:
            env[k] = "<set>" if v not in (None, "") else "<empty>"
    with session() as db:
        stored = {row.key: row.value for row in db.exec(select(Setting))}
    keys = sorted(set(stored) | set(DEFAULT_SETTINGS))
    app_settings = {}
    for k in keys:
        v = stored.get(k, DEFAULT_SETTINGS.get(k, ""))
        if k in SAFE_SETTINGS or k.startswith("llm_model_"):
            val = _safe_value(v)
            app_settings[k] = anon(val) if isinstance(val, str) else val
        else:
            app_settings[k] = "<set>" if v else "<empty>"
    return {"environment": env, "app": app_settings}


def build(description: str = "", *, include_info: bool = False, include_settings: bool = True) -> dict[str, Any]:
    """The whole report as a dict (JSON download); `render_text` turns it into the text version."""
    anon = build_anonymizer()
    report: dict[str, Any] = {
        "format": "secondhandler-bugreport/1",
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "removed": list(REMOVED),
        "description": anon(description.strip()[:5000]),
    }

    def section(name: str, fn, *args, **kw) -> None:
        try:
            report[name] = fn(*args, **kw)
        except Exception as e:  # noqa: BLE001 - a broken section must not cost the whole report
            log.warning("bug report section %s failed: %s", name, e)
            report[name] = {"error": anon(f"{type(e).__name__}: {e}")}

    section("app", app_section)
    section("connections", connections_section, anon)
    section("activity", activity_section, anon)
    section("errors", errors_section, anon, include_info)
    if include_settings:
        section("settings", settings_section, anon)
    return report


# ------------------------------------------------------------------ text rendering
def _kv(d: dict[str, Any], indent: str = "") -> list[str]:
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"{indent}{k}:")
            out += _kv(v, indent + "  ") if v else [f"{indent}  –"]
        else:
            out.append(f"{indent}{k}: {v}")
    return out


def render_text(r: dict[str, Any]) -> str:
    lines = ["# SecondHandler bug report", "",
             f"Generated {r['generated_at']} UTC. Anonymised and created locally; the app sent it nowhere.",
             "Removed: " + "; ".join(r.get("removed", [])) + ".", "",
             "## What happened", "", r.get("description") or "(no description)", ""]
    a = r.get("app") or {}
    lines += ["## App", ""] + _kv(a) + [""]
    c = r.get("connections") or {}
    lines += ["## Connections", ""]
    for it in c.get("items", []):
        lines.append(f"- {it['item']}: {it['state']}" + (f" ({it['reason']})" if it.get("reason") else ""))
    if c.get("claude_last_call"):
        cl = c["claude_last_call"]
        lines.append(f"- last Claude call: ok={cl.get('ok')} at={cl.get('at')}" + (f" error={cl['error']}" if cl.get("error") else ""))
    if c.get("error"):
        lines.append(f"- section failed: {c['error']}")
    lines.append("")
    act = r.get("activity") or {}
    lines += ["## Activity", ""] + _kv(act.get("counts", {}))
    if act.get("error"):
        lines.append(f"section failed: {act['error']}")
    lines += ["", "### Claude calls (per purpose and model)", ""]
    for g in act.get("llm_calls", []) or []:
        lines.append(f"- {g['purpose']} / {g['model']}: {g['calls']} calls, {g['failures']} failed, last {g['last']}")
    if not act.get("llm_calls"):
        lines.append("- none")
    lines += ["", "### willhaben rate limiter", ""] + _kv(act.get("limiter", {}))
    lines += ["", "### Scheduler jobs", ""]
    lines += [f"- {j['id']}: next {j['next_run']}" for j in act.get("scheduler_jobs", [])] or ["- none"]
    lines += ["", "### Recent events (kinds only)", ""]
    lines += [f"- {e['ts']} {e['kind']}" for e in act.get("recent_events", [])] or ["- none"]
    lines.append("")
    err = r.get("errors") or {}
    lines += ["## Errors", "", "### Log", ""]
    for e in err.get("log", []):
        lines.append(f"[{e['ts']}] {e['level']} {e['logger']}: {e['message']}")
        if e.get("traceback"):
            lines += ["    " + line for line in e["traceback"].splitlines()]
    if not err.get("log"):
        lines.append("(no warnings or errors since the start)")
    lines += ["", "### Failed Claude calls", ""]
    lines += [f"- {f['ts']} {f['purpose']} / {f['model']}: {f['error']}" for f in err.get("llm_failures", [])] or ["- none"]
    lines += ["", "### Search agent errors", ""]
    lines += [f"- {f['agent']} (last run {f['last_run']}): {f['error']}" for f in err.get("search_agent_errors", [])] or ["- none"]
    lines += ["", "### My ads errors", ""]
    lines += [f"- {f['ad']}: {f['error']}" for f in err.get("my_ad_errors", [])] or ["- none"]
    if err.get("error"):
        lines.append(f"section failed: {err['error']}")
    lines.append("")
    if "settings" in r:
        lines += ["## Settings", ""] + _kv(r["settings"]) + [""]
    return "\n".join(lines)
