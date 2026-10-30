"""Every request to willhaben and the other sites with its answer, stored for the timeline, offline replay and test
fixtures (WhRecording.platform says which site; the name stayed from when there was only willhaben).

The choke points (HttpPlatformClient._get for the anonymous requests of every site, BrowserSession.api/upload/ws_* for
the willhaben account) and
the side channels (page loads, the live chat socket, label and image downloads, the manual API recording) call
`record_http()` / `record_ws()` / `record_push()`. Each call becomes a WhRecording row plus a "wh" timeline entry that
carries the timeline context (which ad, chat, search, run it belongs to).

What is stored: method, URL, a canonical key for replay matching, status, duration, outcome, redacted headers (cookies,
CSRF tokens, authorization never), and both bodies zlib-compressed up to a cap per content type. Images are kept as
size + sha256 only. Bodies over the cap keep their head and are flagged `truncated` (replay never uses those).

Opt-in: nothing is stored unless "Record requests to the sites" is switched on in Settings → Timeline (setting
wh_recording). The timeline entry of a request needs the timeline switched on as well; without it the recording is
still kept for replay and export.

Writing happens off the event loop (a commit or compressing a 3 MB page must not stall it), like access_log; `drain()`
waits for pending writes (shutdown, tests). Recording never raises into the request it describes.

Anonymous modules import this file: it must never import the logged-in session (tests/test_anonymous_public.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

log = logging.getLogger(__name__)

REDACT_HEADERS = {"cookie", "set-cookie", "authorization", "x-bbx-csrf-token", "x-csrf-token", "proxy-authorization",
                  "x-wh-application-token"}
# query parameters that change on every call without changing the answer
VOLATILE_PARAMS = {"_", "t", "ts", "timestamp", "cb", "nocache", "rnd", "sfid", "isnavigation", "islog"}
# JSON body keys that differ per call (request ids, client timestamps)
VOLATILE_BODY_KEYS = {"request_id", "requestId", "client_message_id", "clientMessageId", "timestamp", "ts", "nonce",
                      "createdAt", "clientTimestamp"}

KiB, MiB = 1024, 1024 * 1024
CAP_REQUEST = 256 * KiB
CAP_JSON = 2 * MiB          # JSON, text, XML
CAP_HTML = 3 * MiB          # pages carry their data in __NEXT_DATA__ at the very end
CAP_PDF = 1 * MiB           # labels are ~100 KB
CAP_OTHER = 256 * KiB
VIEW_MAX = 200_000          # characters shown in the timeline (the full body is a link away)

sync_writes = False         # tests: write in the calling thread (one shared in-memory connection)
_pending: set[asyncio.Task] = set()


# ------------------------------------------------------------------ pure helpers
def redact_headers(headers: dict[str, Any] | None) -> dict[str, str] | None:
    if headers is None:
        return None
    return {str(k): ("<redacted>" if str(k).lower() in REDACT_HEADERS else str(v)) for k, v in dict(headers).items()}


def _canon_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _canon_json(v) for k, v in obj.items() if k not in VOLATILE_BODY_KEYS}
    if isinstance(obj, list):
        return [_canon_json(v) for v in obj]
    return obj


def _canon_body(body: Any) -> str:
    if body is None or body == b"" or body == "":
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(_canon_json(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return "sha256:" + hashlib.sha256(bytes(body)).hexdigest()
    text = str(body).strip()
    try:
        return _canon_body(json.loads(text)) if text[:1] in "{[" else text
    except ValueError:
        return text


def canonical_url(url: str) -> str:
    """Host + path + sorted query without volatile parameters (scheme and fragment dropped)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = parts.path.rstrip("/") or "/"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in VOLATILE_PARAMS)
    return f"{(parts.hostname or '').lower()}{path}" + (f"?{urlencode(query)}" if query else "")


def canonical_key(method: str, url: str, body: Any = None, *, upload: tuple[str, str] | None = None) -> str:
    """What replay matches on: method, canonical URL and canonical body. Uploads match by field and type, not bytes."""
    canon = f"multipart:{upload[0]}:{upload[1]}" if upload else _canon_body(body)
    raw = "\n".join((method.upper(), canonical_url(url), canon))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cap_for(content_type: str | None, is_request: bool) -> int:
    if is_request:
        return CAP_REQUEST
    ct = (content_type or "").lower()
    if "html" in ct:
        return CAP_HTML
    if "json" in ct or ct.startswith("text/") or "xml" in ct or "javascript" in ct:
        return CAP_JSON
    if "pdf" in ct:
        return CAP_PDF
    return CAP_OTHER


def _bytes(data: Any) -> bytes | None:
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return str(data).encode("utf-8", "replace")


def prepare_body(data: Any, content_type: str | None, *, is_request: bool = False,
                 store: bool = True) -> tuple[bytes | None, int, bool, str | None]:
    """(zlib blob or None, size, truncated, sha256) for one body. Images and store=False keep only size + hash."""
    raw = _bytes(data)
    if raw is None:
        return None, 0, False, None
    digest = hashlib.sha256(raw).hexdigest()
    if not store or (content_type or "").lower().startswith("image/") or not raw:
        return None, len(raw), False, digest
    cap = _cap_for(content_type, is_request)
    truncated = len(raw) > cap
    return zlib.compress(raw[:cap], 6), len(raw), truncated, digest


def decode_body(blob: bytes | None) -> bytes | None:
    return zlib.decompress(blob) if blob is not None else None


def _content_type(headers: dict[str, Any] | None) -> str | None:
    for k, v in (headers or {}).items():
        if str(k).lower() == "content-type":
            return str(v).split(";")[0].strip().lower() or None
    return None


def _sniff(data: Any) -> str | None:
    """A content type when the transport gave none (fetch() answers, WebSocket frames)."""
    if isinstance(data, (dict, list)):
        return "application/json"
    if isinstance(data, (bytes, bytearray)):
        if bytes(data[:5]) == b"%PDF-":
            return "application/pdf"
        try:
            data = bytes(data[:200]).decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
    text = str(data or "").lstrip()[:200].lower()
    if text.startswith(("{", "[")):
        return "application/json"
    if text.startswith(("<!doctype html", "<html")):
        return "text/html"
    return "text/plain" if text else None


# ------------------------------------------------------------------ recording
@dataclass
class Pending:
    channel: str
    kind: str
    method: str
    url: str
    status: int | None = None
    duration_ms: int | None = None
    outcome: str = "ok"
    cached: bool = False
    final_url: str | None = None
    error: str | None = None
    ws_status: str | None = None
    req_headers: dict[str, str] | None = None
    req_body: Any = None
    resp_headers: dict[str, str] | None = None
    resp_body: Any = None
    content_type: str | None = None
    store_response: bool = True
    upload: tuple[str, str] | None = None
    key_body: Any = None
    platform: str = "willhaben"
    context: dict[str, Any] = field(default_factory=dict)


def enabled() -> bool:
    """Recording is switched on (Settings → Timeline → Record requests to the sites)."""
    from app.services import timeline

    return timeline.flag(timeline.SETTING_WH)


def record_http(channel: str, kind: str, method: str, url: str, *, req_headers: dict[str, Any] | None = None,
                req_body: Any = None, status: int | None = None, resp_headers: dict[str, Any] | None = None,
                resp_body: Any = None, duration_ms: int | None = None, outcome: str | None = None,
                cached: bool = False, final_url: str | None = None, error: str | None = None,
                content_type: str | None = None, ws_status: str | None = None, store_response: bool = True,
                upload: tuple[str, str] | None = None, key_body: Any = None, platform: str = "willhaben") -> None:
    """Store one request and its answer (in the background), when recording is switched on. Never raises."""
    try:
        if not enabled():
            return
        from app.willhaben import access_log

        p = Pending(channel=channel, kind=kind, method=method.upper(), url=url, status=status, duration_ms=duration_ms,
                    outcome=outcome or access_log.outcome_for_status(status), cached=cached, final_url=final_url,
                    error=(str(error)[:300] if error else None), ws_status=ws_status,
                    req_headers=redact_headers(req_headers), req_body=req_body,
                    resp_headers=redact_headers(resp_headers), resp_body=resp_body,
                    content_type=content_type or _content_type(resp_headers) or (_sniff(resp_body) if resp_body is not None else None),
                    store_response=store_response, upload=upload, key_body=key_body, platform=platform or "willhaben")
        _enqueue(p)
    except Exception as e:  # noqa: BLE001 - the recording must never break the request
        log.warning("willhaben recording failed for %s %s: %s", method, url, e)


def record_ws(kind: str, url: str, frames: Any, result: Any, *, duration_ms: int | None = None,
              outcome: str | None = None) -> None:
    """One WebSocket call: the frame(s) sent and the answer (or the {status} the page reported)."""
    status = result.get("status") if isinstance(result, dict) else None
    record_http("chat", kind, "WS", url, req_body=frames, resp_body=result, duration_ms=duration_ms,
                outcome=outcome, ws_status=str(status) if status is not None else None, content_type="application/json")


def record_push(url: str, frame: dict[str, Any]) -> None:
    """A frame the live chat socket at `url` pushed (new message, read receipt, …)."""
    record_http("chat", "chat_push", "WSPUSH", url, resp_body=frame, content_type="application/json",
                key_body=frame.get("req_type") or frame.get("type"))


def _enqueue(p: Pending) -> None:
    from app.services import timeline

    p.context = timeline.current()
    if sync_writes:
        _write(p)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _write(p)
        return
    task = loop.create_task(asyncio.to_thread(_write, p))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for the recordings still being written (shutdown, tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def reset() -> None:
    for task in list(_pending):
        task.cancel()
    _pending.clear()


def _path(url: str) -> str:
    try:
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    except ValueError:
        return url


def _write(p: Pending) -> None:
    from app.db import WhRecording, session
    from app.services import timeline

    try:
        req_blob, req_size, req_trunc, _ = prepare_body(p.req_body, _sniff(p.req_body) if p.req_body is not None else None,
                                                        is_request=True)
        resp_blob, resp_size, resp_trunc, digest = prepare_body(p.resp_body, p.content_type, store=p.store_response)
        key = canonical_key(p.method, p.url, p.key_body if p.key_body is not None else p.req_body, upload=p.upload)
        status = p.status if p.status is not None else p.ws_status
        title = f"{p.method} {_path(p.url)[:160]}" + (f" → {status}" if status is not None else "") + (" (cache)" if p.cached else "")
        if p.platform != "willhaben":
            title = f"[{p.platform}] {title}"
        ok = p.outcome in ("ok", "cached")
        with timeline.context(**p.context):
            failure = f"HTTP {p.status}" if p.status is not None else str(p.ws_status or p.outcome)
            event_id = timeline.record("wh", title, name=p.kind, ok=ok, error=p.error or (None if ok else failure),
                                       duration_ms=p.duration_ms,
                                       payload={k: v for k, v in {"channel": p.channel, "status": status, "bytes": resp_size,
                                                                  "cached": p.cached or None,
                                                                  "platform": p.platform if p.platform != "willhaben" else None}.items()
                                                if v is not None})
        rec = WhRecording(event_id=event_id, platform=p.platform, channel=p.channel, kind=p.kind, method=p.method, url=p.url[:2000],
                          final_url=(p.final_url[:2000] if p.final_url and p.final_url != p.url else None),
                          canonical_key=key, status=p.status, ws_status=p.ws_status, duration_ms=p.duration_ms,
                          outcome=p.outcome, cached=p.cached, error=p.error,
                          req_headers=json.dumps(p.req_headers, ensure_ascii=False) if p.req_headers else None,
                          req_body=req_blob, req_size=req_size, req_truncated=req_trunc,
                          resp_headers=json.dumps(p.resp_headers, ensure_ascii=False) if p.resp_headers else None,
                          resp_body=resp_blob, resp_size=resp_size, resp_truncated=resp_trunc,
                          content_type=p.content_type, body_hash=digest)
        with session() as db:
            db.add(rec)
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("willhaben recording not stored (%s %s): %s", p.method, p.url, e)


# ------------------------------------------------------------------ reading (timeline detail, replay, export)
def _text(blob: bytes | None, limit: int | None = VIEW_MAX) -> tuple[str | None, bool]:
    raw = decode_body(blob)
    if raw is None:
        return None, False
    text = raw.decode("utf-8", "replace")
    if text[:1] in "{[":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    if limit and len(text) > limit:
        return text[:limit], True
    return text, False


def view_for_event(event_id: int | None, limit: int | None = VIEW_MAX) -> dict[str, Any] | None:
    """The recording of a "wh" timeline entry, decoded for the detail view (limit=None: the whole bodies)."""
    from sqlmodel import select

    from app.db import WhRecording, session

    if not event_id:
        return None
    with session() as db:
        r = db.exec(select(WhRecording).where(WhRecording.event_id == event_id)).first()
    if r is None:
        return None
    req, req_cut = _text(r.req_body, limit)
    resp, resp_cut = _text(r.resp_body, limit)
    return {"id": r.id, "platform": r.platform or "willhaben", "channel": r.channel, "kind": r.kind, "method": r.method,
            "url": r.url, "final_url": r.final_url,
            "status": r.status, "ws_status": r.ws_status, "outcome": r.outcome, "cached": r.cached, "error": r.error,
            "content_type": r.content_type, "key": r.canonical_key, "pruned": r.pruned,
            "req_headers": json.loads(r.req_headers) if r.req_headers else None,
            "resp_headers": json.loads(r.resp_headers) if r.resp_headers else None,
            "request": req, "request_cut": req_cut, "req_size": r.req_size, "req_truncated": r.req_truncated,
            "response": resp, "response_cut": resp_cut, "resp_size": r.resp_size, "resp_truncated": r.resp_truncated,
            "image": (r.content_type or "").startswith("image/"), "body_hash": r.body_hash}


def body_of(recording_id: int, side: str) -> tuple[bytes | None, str | None]:
    """The whole stored body of one side ("request" / "response") and its content type."""
    from app.db import WhRecording, session

    with session() as db:
        r = db.get(WhRecording, recording_id)
    if r is None:
        return None, None
    if side == "request":
        body = decode_body(r.req_body)
        return body, _sniff(body) if body is not None else None
    return decode_body(r.resp_body), r.content_type


# ------------------------------------------------------------------ size and retention (Settings → Timeline)
def storage_size() -> int:
    """Bytes the recordings take (bodies + headers + URLs; SQLite's own overhead not counted)."""
    from sqlmodel import func, select

    from app.db import WhRecording, session

    with session() as db:
        return int(db.exec(select(
            func.coalesce(func.sum(func.length(WhRecording.req_body)), 0)
            + func.coalesce(func.sum(func.length(WhRecording.resp_body)), 0)
            + func.coalesce(func.sum(func.length(WhRecording.req_headers)), 0)
            + func.coalesce(func.sum(func.length(WhRecording.resp_headers)), 0)
            + func.coalesce(func.sum(func.length(WhRecording.url)), 0))).one() or 0)


def stats() -> dict[str, Any]:
    from sqlmodel import func, select

    from app.db import WhRecording, session

    with session() as db:
        rows = int(db.exec(select(func.count()).select_from(WhRecording)).one())
        oldest, newest = db.exec(select(func.min(WhRecording.ts), func.max(WhRecording.ts))).one()
        by_channel = {c: int(n) for c, n in db.exec(select(WhRecording.channel, func.count()).group_by(WhRecording.channel))}
        by_platform: dict[str, int] = {}
        for p, n in db.exec(select(WhRecording.platform, func.count()).group_by(WhRecording.platform)):
            by_platform[p or "willhaben"] = by_platform.get(p or "willhaben", 0) + int(n)
        by_platform = dict(sorted(by_platform.items(), key=lambda kv: -kv[1]))     # most first
    return {"rows": rows, "bytes": storage_size(), "oldest": oldest, "newest": newest, "by_channel": by_channel,
            "by_platform": by_platform}


def delete_for_events(db, event_ids: list[int]) -> None:
    """Part of timeline.prune(): the recordings of deleted timeline entries go with them (caller commits)."""
    from sqlalchemy import delete
    from sqlmodel import col

    from app.db import WhRecording

    db.exec(delete(WhRecording).where(col(WhRecording.event_id).in_(event_ids)))


def prune_before(cutoff: datetime) -> int:
    from sqlalchemy import delete

    from app.db import WhRecording, session

    with session() as db:
        res = db.exec(delete(WhRecording).where(WhRecording.ts < cutoff))
        db.commit()
        return int(res.rowcount or 0)


def drop_oldest_bodies(n: int = 200) -> int:
    """Empty the bodies of the n oldest recordings that still have any (metadata stays). Returns how many."""
    from sqlalchemy import or_, update
    from sqlmodel import col, select

    from app.db import WhRecording, session

    with session() as db:
        ids = list(db.exec(select(WhRecording.id).where(or_(col(WhRecording.resp_body).is_not(None),
                                                             col(WhRecording.req_body).is_not(None)))
                           .order_by(col(WhRecording.id)).limit(n)))
        if not ids:
            return 0
        db.exec(update(WhRecording).where(col(WhRecording.id).in_(ids)).values(req_body=None, resp_body=None, pruned=True))
        db.commit()
    return len(ids)


def delete_all() -> int:
    from sqlalchemy import delete

    from app.db import TimelineEvent, WhRecording, session

    with session() as db:
        n = int(db.exec(delete(WhRecording)).rowcount or 0)
        db.exec(delete(TimelineEvent).where(TimelineEvent.kind == "wh"))
        db.commit()
    return n
