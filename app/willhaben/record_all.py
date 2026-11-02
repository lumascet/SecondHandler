"""Record everything the app's browser does (the window in the remote view and its hidden tabs), for reverse
engineering the sites' web apps: every request of every site with its answer, and the WebSocket frames.

Switched on the session page (setting browser_record_all); the listeners stay attached and read the switch on every
request (cached ON_CACHE_S), so it applies at once. Lines go to data/captures/all/<YYYYMMDD-HH>.jsonl (local, like
every capture: they hold chat content); a finished hour is gzipped (.jsonl.gz, a tenth of the size), files older than
KEEP_DAYS go, and so do the oldest once all of them together pass MAX_BYTES. Never kept: cookies, CSRF tokens and authorization headers
(recorder.redact_headers), passwords, card numbers, PINs and tokens in request bodies (redact_body), and images,
fonts, style sheets and media (no use for the API, only size).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import get_setting, set_setting, utcnow
from app.willhaben import recorder

log = logging.getLogger(__name__)

SETTING = "browser_record_all"
KEEP_DAYS = 7
MAX_BYTES = 2_000_000_000     # all kept files together (gzipped hours mostly): the oldest go first beyond this
ON_CACHE_S = 5                # the switch, read for every request of the browser: not a database query each time
SKIP_TYPES = {"image", "font", "stylesheet", "media", "texttrack", "manifest"}
DOC_LIMIT, BODY_LIMIT, FRAME_LIMIT, POST_LIMIT = 3_000_000, 300_000, 20_000, 100_000
# a field whose name has one of these as a whole part ("password", "card_number", "accessToken"; not "shipping")
_SECRET = r"password|passwort|passwd|pwd|pin|cvc|cvv|card_?number|cardnumber|pan|iban|otp|tan|secret|token"
_KEY = rf"(?:[\w.%-]*[_.%-])?(?:{_SECRET})(?:[_.%-][\w.%-]*)?|[a-z]+(?:Password|Token|Secret|Pin)"
_JSON_SECRET = re.compile(rf'("(?:{_KEY})"\s*:\s*)"[^"]*"', re.I)
_FORM_SECRET = re.compile(rf"((?:^|&)(?:{_KEY})=)[^&]*", re.I)
_tasks: set[asyncio.Task] = set()
_pruned_hour = ""


_on_cache: list[float | bool] = [0.0, False]


def on() -> bool:
    now = time.monotonic()
    if now - float(_on_cache[0]) > ON_CACHE_S:
        _on_cache[:] = [now, get_setting(SETTING) != "0"]   # on unless switched off (asked for: record everything)
    return bool(_on_cache[1])


def set_on(value: bool) -> None:
    set_setting(SETTING, "1" if value else "0")
    _on_cache[:] = [time.monotonic(), value]


def folder() -> Path:
    return get_settings().captures_dir / "all"


def stats() -> dict[str, Any]:
    """{files, bytes} of the kept recordings (the session page)."""
    files = _files()
    return {"files": len(files), "bytes": sum(f.stat().st_size for f in files),
            "newest": files[-1].name if files else None}


def redact_body(text: str | None) -> str | None:
    """Passwords, card data, PINs and tokens in a JSON or form body replaced by ***."""
    if not text:
        return text
    return _FORM_SECRET.sub(r"\1***", _JSON_SECRET.sub(r'\1"***"', text))


def _files() -> list[Path]:
    d = folder()
    return sorted([*d.glob("*.jsonl"), *d.glob("*.jsonl.gz")], key=lambda p: p.name) if d.is_dir() else []


def _tidy(current: str) -> None:
    """Gzip the finished hours, then drop what is older than KEEP_DAYS and the oldest beyond MAX_BYTES. Runs in a
    thread: an hour's file is tens of MB."""
    import gzip
    import shutil

    import os

    d = folder()
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in _files():                           # first the old ones (a gzipped copy would look new)
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
    for p in d.glob("*.jsonl"):
        if p.name != f"{current}.jsonl":
            try:
                st = p.stat()
                gz = p.with_name(p.name + ".gz")
                with open(p, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                os.utime(gz, (st.st_atime, st.st_mtime))   # its age stays the hour's
                p.unlink()
            except OSError as e:
                log.info("record_all: %s not compressed: %s", p.name, e)
    files = [p for p in _files() if p.name != f"{current}.jsonl"]
    total = sum(p.stat().st_size for p in files)
    for p in files:                              # oldest first (the names sort by hour)
        if total <= MAX_BYTES:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)


def _write(entry: dict[str, Any]) -> None:
    global _pruned_hour
    now = utcnow()
    hour = f"{now:%Y%m%d-%H}"
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{hour}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    if hour != _pruned_hour:                     # once an hour: compress the last one, drop the old ones
        _pruned_hour = hour
        import threading
        threading.Thread(target=_tidy, args=(hour,), daemon=True, name="record-all-tidy").start()


def _spawn(coro) -> None:
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _request(request, failed: bool = False) -> None:
    if request.resource_type in SKIP_TYPES:
        return
    try:
        resp = None if failed else await request.response()
        body = None
        size = None
        if resp is not None:
            try:
                raw = await resp.body()
                size = len(raw)
                ctype = (await resp.header_value("content-type") or "").lower()
                # scripts: address and size only (a site's bundles are megabytes on every page, 84 MB in 13 min)
                if request.resource_type != "script" and (
                        any(k in ctype for k in ("json", "text", "xml", "html", "x-www-form")) or not ctype):
                    limit = DOC_LIMIT if request.resource_type == "document" else BODY_LIMIT
                    body = raw[:limit].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - redirects and aborted answers have no body
                pass
        _write({"ts": utcnow().isoformat(), "kind": "http", "method": request.method, "url": request.url,
                "type": request.resource_type, "frame": _frame_url(request),
                "request_headers": recorder.redact_headers(await request.all_headers()),
                "post_data": redact_body((request.post_data or "")[:POST_LIMIT]) or None,
                "status": resp.status if resp else None, "failed": request.failure if failed else None,
                "response_headers": recorder.redact_headers(await resp.all_headers()) if resp else None,
                "size": size, "response_body": body})
    except Exception as e:  # noqa: BLE001 - a page that closed meanwhile: nothing to record
        log.debug("record_all: %s", e)


def _frame_url(request) -> str | None:
    try:
        return request.frame.url
    except Exception:  # noqa: BLE001 - service workers have no frame
        return None


def _websocket(ws) -> None:
    url = ws.url

    def frame(direction: str, payload: Any) -> None:
        if not on():
            return
        text = payload if isinstance(payload, str) else f"<{len(payload or b'')} bytes>"
        _write({"ts": utcnow().isoformat(), "kind": "ws", "dir": direction, "url": url,
                "data": redact_body(text[:FRAME_LIMIT])})

    if on():
        _write({"ts": utcnow().isoformat(), "kind": "ws", "dir": "open", "url": url})
    ws.on("framesent", lambda p: frame("sent", p))
    ws.on("framereceived", lambda p: frame("received", p))
    ws.on("close", lambda *_: on() and _write({"ts": utcnow().isoformat(), "kind": "ws", "dir": "close", "url": url}))


def watch_page(page) -> None:
    page.on("websocket", _websocket)


def attach(context) -> None:
    """At the browser's start: listeners on every request and every page's WebSockets (they check the switch)."""
    context.on("requestfinished", lambda r: on() and _spawn(_request(r)))
    context.on("requestfailed", lambda r: on() and _spawn(_request(r, failed=True)))
    for page in context.pages:
        watch_page(page)
    context.on("page", watch_page)
