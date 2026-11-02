import json
import os
import tempfile
from pathlib import Path

os.environ.update({
    "DATA_DIR": tempfile.mkdtemp(prefix="wh-test-"),
    "BROWSER_ENABLED": "0",
    "HA_LISTENER": "0",
    "TELEGRAM_LISTENER": "0",
    "HUMANIZE_CHAT": "0",       # auto replies answer immediately in tests (test_humanize covers the timing)
    "JOB_GRACE_S": "0",         # irreversible jobs run at once (test_jobs covers the grace period)
    "TELEGRAM_BOT_TOKEN": "",
    "REQUEST_MIN_GAP_S": "0",
    "PLATFORM_KLEINANZEIGEN_MIN_GAP_S": "0",
    "HA_URL": "",
    "HA_TOKEN": "",
    "HA_NOTIFY_SERVICE": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "DASHBOARD_PASSWORD": "",
    "PUBLIC_URL": "",
    "CLAUDE_BIN": "/nonexistent/claude",   # a test that forgot its fake backend fails at once, never a billed prompt
    "LLM_BACKEND": "claude_cli",
})

from app import config as _config  # noqa: E402

_config.Settings.model_config["env_file"] = None    # never the real .env (app keys, the web address) in tests
_config.get_settings.cache_clear()

import socket  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import create_engine  # noqa: E402

from app import db as dbmod  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Tests never reach the internet: a forgotten mock fails at once instead of waiting on a real site.
_LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver")
_real_getaddrinfo, _real_connect, _real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex


def _local(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    return not isinstance(host, str) or host in _LOCAL or host.startswith("127.") or "/" in host   # "/": unix socket


def _getaddrinfo(host, *args, **kwargs):
    if not _local((host,)):
        raise socket.gaierror(f"tests must not reach the network ({host})")
    return _real_getaddrinfo(host, *args, **kwargs)


def _connect(self, address):
    if not _local(address):
        raise ConnectionRefusedError(f"tests must not reach the network ({address})")
    return _real_connect(self, address)


def _connect_ex(self, address):
    return _real_connect_ex(self, address) if _local(address) else 111     # ECONNREFUSED


socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = _getaddrinfo, _connect, _connect_ex

# curl_cffi opens its sockets in libcurl, past the guard above: refuse its requests to other hosts the same way
try:
    from urllib.parse import urlsplit as _urlsplit

    from curl_cffi.requests import AsyncSession as _CurlSession
except ImportError:  # pragma: no cover
    _CurlSession = None
if _CurlSession is not None:
    _real_curl_request = _CurlSession.request

    async def _curl_request(self, method, url, *args, **kwargs):
        if not _local((_urlsplit(str(url)).hostname or "",)):
            raise ConnectionRefusedError(f"tests must not reach the network ({url})")
        return await _real_curl_request(self, method, url, *args, **kwargs)

    _CurlSession.request = _curl_request


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def wait_jobs(timeout: float = 5.0) -> None:
    """TestClient runs the app's event loop in another thread: wait there until the background jobs are done."""
    import time

    from app.services import jobs
    end = time.monotonic() + timeout
    while jobs.busy() and time.monotonic() < end:
        time.sleep(0.02)
    assert not jobs.busy(), "background jobs still running"


async def finish_jobs() -> None:
    """In an async test: run the queued background jobs to the end."""
    import asyncio

    from app.services import jobs
    while jobs._active:
        await asyncio.gather(*(j.task for j in list(jobs._active)))


@pytest.fixture(autouse=True)
def fake_claude_env(monkeypatch):
    """tests/fake_claude.py is steered by FAKE_CLAUDE_* variables: the CLI's environment allowlist lets them pass."""
    from app.llm import claude_cli
    monkeypatch.setattr(claude_cli, "_ENV_KEEP_PREFIXES", (*claude_cli._ENV_KEEP_PREFIXES, "FAKE_CLAUDE_"))


@pytest.fixture(autouse=True)
def quick_refusals(monkeypatch):
    """A run of refusals counts at once (tests send them within milliseconds); tests/test_vpn.py checks the span."""
    from app.platforms import vpn
    monkeypatch.setattr(vpn, "CONFIRM_SPAN_S", 0)


@pytest.fixture(autouse=True)
def no_geizhals_cache(monkeypatch):
    """Every Geizhals lookup asks again (the tests count requests); tests/test_geizhals.py checks the cache."""
    from app.services import geizhals
    monkeypatch.setattr(geizhals, "CACHE_DAYS", 0)


@pytest.fixture(autouse=True)
def no_paylivery_advice(monkeypatch):
    """An offer in a seller's chat asks Claude in the background (stage hook): never the real one in tests;
    tests/test_paylivery_advice.py switches it on with its fake backend."""
    from app.services import paylivery_advice
    monkeypatch.setattr(paylivery_advice, "AUTO", False)


@pytest.fixture(autouse=True)
def no_real_translation(monkeypatch):
    """A new draft or an opened chat is translated in the background: never by the real Claude in tests (the task
    waited on it at the end of the test's event loop and hung the suite); tests/test_translate.py brings its fake."""
    from app.llm.base import LLMError
    from app.services import translate

    class Refuse:
        async def complete(self, **kw):
            raise LLMError("no Claude in tests")
    monkeypatch.setattr(translate, "get_backend", lambda *a, **kw: Refuse())
    translate._running.clear()
    translate._drafts_running.clear()


@pytest.fixture(autouse=True)
def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    dbmod.set_engine(engine)
    dbmod.init_db()
    from app.platforms import ebay, kleinanzeigen, preisjaeger, shpock, vinted, vpn
    from app.services import adaptive_pace, chats, geizhals, jobs, vpn_check
    from app.willhaben import public, recorder
    vpn.reset()                 # VPN tunnels and their (fake) processes are per test
    geizhals._challenged.clear()
    vpn_check.reset()           # so is the VPN test (its task belongs to the test's event loop)
    for mod in (public, ebay, kleinanzeigen, preisjaeger, shpock, vinted, geizhals):
        if mod._limiter is not None:     # module-wide: one test's block (429 -> pause) must not pause the next test
            mod._limiter.reset()
    public._account_limiter = None   # the account's own (willhaben's VPN on): fresh per test
    jobs.reset()                # background jobs are per event loop
    from app.willhaben import access_log
    access_log.reset()          # unflushed request counts of the previous test (counted toward the next one's)
    chats.reset()               # failed automatic answers are per test
    adaptive_pace.reset()       # the learned hourly profiles belong to the test's hits
    from app.services import deals, product_search
    # searches, ads and their ids start afresh with each test's database: what a background lookup of one test left
    # behind (an older search's new prices, a site resting after a slow answer, Claude's product of an ad) must not
    # meet the next test's search 1 or ad (10-26: CI failed, a backfill of search 1 was still "running")
    product_search._backfill.clear()
    product_search._backfill_tried.clear()
    deals._slow_until.clear()
    deals._identities.clear()
    deals._picks.clear()
    from app.platforms import vinted_inbox
    vinted_inbox._pushed.clear()             # the Vinted message and order event last pushed, per chat
    vinted_inbox._pushed_events.clear()
    recorder.sync_writes = True  # one in-memory connection: willhaben recordings are written right away
    yield
    recorder.reset()
    recorder.sync_writes = False
    dbmod.set_engine(None)
