"""The timeline: what happened, for which ad / chat / search / purchase, and why (page /timeline).

Every Claude call (with the full prompt and answer), every request to willhaben or another platform
(app/willhaben/recorder.py; the entry's payload names the platform when it is not willhaben), every
state-changing click, background job, search agent run and notification becomes one `TimelineEvent`. Which things
an entry belongs to comes from a context variable: the place that knows the ids sets them once
(`with timeline.context(conversation_id=...)`), and everything recorded inside, also in background jobs started from
there, carries them. `run()` opens a job / agent run / scheduler entry; entries inside it get its id as `run_id`, so
the page can show one run with all its steps.

Long texts (prompts, answers) live in `TimelineBlob`, compressed. Nothing here is ever part of the bug report.
Recording never breaks the work it describes: errors are logged and swallowed. Both the timeline and the recording of
requests to the sites are opt-in (Settings → Timeline, off by default); while the timeline is off nothing is stored here.
Retention is off unless switched on there too (days and/or a size budget; the oldest entries go first).
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import time
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, update
from sqlmodel import col, func, select

from app.db import TimelineBlob, TimelineEvent, get_setting, session, set_setting, utcnow

log = logging.getLogger(__name__)

KINDS = ("llm", "wh", "action", "job", "agent_run", "sched", "notify")
RUN_KINDS = ("job", "agent_run", "sched")
ID_KEYS = ("ad_id", "conversation_id", "agent_id", "purchase_id", "search_id", "batch_id", "draft_id", "shipment_id")
_STR_IDS = {"ad_id", "conversation_id"}
_META_KEYS = {"actor", "run_id"}

BLOB_COMPRESS_MIN = 2048          # bytes; shorter texts are stored as they are
BLOB_MAX = 4_000_000              # a longer text is cut (a prompt is a few KB, a page of chat history maybe 50 KB)
EVENT_EST_BYTES = 400             # rough size of one row without blobs, for the size budget

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("timeline_ctx", default=None)

SETTING_TIMELINE = "timeline_enabled"     # record the timeline at all (opt-in)
SETTING_WH = "wh_recording"               # store willhaben requests with their answers (opt-in, app/willhaben/recorder.py)
FLAG_TTL_S = 10
_flags: dict[str, tuple[Any, float, bool]] = {}


def flag(key: str) -> bool:
    """An on/off setting, cached for a few seconds (asked on every recorded call). Saving clears the cache."""
    from app.db import get_engine

    engine, now = get_engine(), time.monotonic()
    hit = _flags.get(key)
    if hit and hit[0] is engine and now - hit[1] < FLAG_TTL_S:
        return hit[2]
    try:
        value = get_setting(key) == "1"
    except Exception:  # noqa: BLE001 - no database yet: nothing is recorded
        value = False
    _flags[key] = (engine, now, value)
    return value


def enabled() -> bool:
    """The timeline is switched on (Settings → Timeline)."""
    return flag(SETTING_TIMELINE)


def set_recording(timeline: bool | None = None, willhaben: bool | None = None) -> None:
    """Switch the timeline and/or the willhaben recording on or off."""
    if timeline is not None:
        set_setting(SETTING_TIMELINE, "1" if timeline else "0")
    if willhaben is not None:
        set_setting(SETTING_WH, "1" if willhaben else "0")
    _flags.clear()


# ------------------------------------------------------------------ context
def current() -> dict[str, Any]:
    """The current context (ids, actor, run_id, extra refs), a copy."""
    return dict(_ctx.get() or {})


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None and v != ""}


@contextmanager
def context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Merge ids (ad_id, conversation_id, …), `actor` and extra refs into the context for everything inside.

    Works in sync and async code; tasks started inside (asyncio.create_task, jobs.submit, asyncio.to_thread) take
    a copy along. None values leave the outer value in place."""
    new = {**current(), **_clean(fields)}
    tok = _ctx.set(new)
    try:
        yield new
    finally:
        _ctx.reset(tok)


@contextmanager
def fresh(**fields: Any) -> Iterator[dict[str, Any]]:
    """Start from an empty context (scheduler callbacks: they may inherit one from the request that scheduled them)."""
    tok = _ctx.set(_clean(fields))
    try:
        yield current()
    finally:
        _ctx.reset(tok)


def update_context(**fields: Any) -> None:
    """Change the context for the rest of the current task (e.g. a bulk job moving on to the next ad)."""
    _ctx.set({**current(), **_clean(fields)})


def scoped(**fields: str | Callable[[dict[str, Any]], Any]) -> Callable:
    """Decorator: run the function inside context(...) built from its arguments.

    `@scoped(conversation_id="conversation_id")` takes the argument of that name; a callable gets all arguments as a
    dict (`ad_id=lambda a: a["ad"].ad_id`). Ids found later inside the function can be added with update_context();
    they end with the call."""
    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        def _fields(args: tuple, kwargs: dict) -> dict[str, Any]:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            out = {}
            for key, src in fields.items():
                try:
                    out[key] = src(bound.arguments) if callable(src) else bound.arguments.get(src)
                except Exception:  # noqa: BLE001 - a missing attribute never breaks the call
                    out[key] = None
            return out

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with context(**_fields(args, kwargs)):
                    return await fn(*args, **kwargs)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with context(**_fields(args, kwargs)):
                return fn(*args, **kwargs)
        return wrapper
    return deco


def set_actor(actor: str) -> None:
    """Who caused what follows (middleware: user / api; Telegram, HA buttons, MCP, scheduler)."""
    update_context(actor=actor)


def marker_ids(marker: str | None) -> dict[str, Any]:
    """Ids from a background job's page marker ("ad:123", "addraft:5", "drafting:c1", "draft:7")."""
    if not marker or ":" not in marker:
        return {}
    kind, _, ref = marker.partition(":")
    if kind == "ad":
        return {"ad_id": ref, "mine": True}
    if kind in ("addraft", "refining"):
        return {"draft_id": ref}
    if kind == "drafting":
        return {"conversation_id": ref}
    if kind == "draft":
        return {"reply_draft": ref}
    return {}


# ------------------------------------------------------------------ recording
def _coerce(key: str, value: Any) -> Any:
    if key in _STR_IDS:
        return str(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blob(event_id: int, name: str, value: str | bytes) -> TimelineBlob:
    data = value.encode("utf-8", "replace") if isinstance(value, str) else bytes(value)
    if len(data) > BLOB_MAX:
        data = data[:BLOB_MAX] + "\n… [cut]".encode()
    size = len(data)
    if size >= BLOB_COMPRESS_MIN:
        return TimelineBlob(event_id=event_id, name=name, encoding="zlib", size=size, data=zlib.compress(data, 6))
    return TimelineBlob(event_id=event_id, name=name, encoding="raw", size=size, data=data)


def blob_text(b: TimelineBlob) -> str:
    data = zlib.decompress(b.data) if b.encoding == "zlib" else b.data
    return data.decode("utf-8", "replace")


def record(kind: str, title: str, *, name: str | None = None, ok: bool | None = True, error: str | None = None,
           duration_ms: int | None = None, payload: dict[str, Any] | None = None,
           blobs: dict[str, str | bytes | None] | None = None, url: str | None = None,
           extra: dict[str, Any] | None = None, actor: str | None = None, **refs: Any) -> int | None:
    """Store one entry; returns its id (None: the timeline is off, or storing failed). Ids given here win over the
    context; other keyword arguments (hit_ids=…, step=…) go to `extra` like unknown context keys."""
    if not enabled():
        return None
    try:
        ctx = current()
        ids = {k: ctx.get(k) for k in ID_KEYS}
        more = {k: v for k, v in ctx.items() if k not in ID_KEYS and k not in _META_KEYS}
        for k, v in refs.items():
            if k in ID_KEYS:
                if v is not None and v != "":
                    ids[k] = v
            elif v is not None:
                more[k] = v
        more.update(_clean(extra or {}))
        ids = {k: _coerce(k, v) for k, v in ids.items() if v is not None and v != ""}
        blobs = {k: v for k, v in (blobs or {}).items() if v}
        ev = TimelineEvent(kind=kind, name=name, actor=actor or ctx.get("actor") or "system", title=(title or "")[:300],
                           ok=ok, error=(str(error)[:1000] if error else None), duration_ms=duration_ms,
                           run_id=ctx.get("run_id"), url=url, payload=payload or None, extra=more or None,
                           has_blobs=bool(blobs), **ids)
        with session() as db:
            db.add(ev)
            db.flush()
            for bname, value in blobs.items():
                db.add(_blob(ev.id, bname, value))
            db.commit()
            return ev.id
    except Exception as e:  # noqa: BLE001 - the timeline must never break the work it records
        log.warning("timeline: could not record %s %r: %s", kind, title, e)
        return None


def _finish(event_id: int | None, *, ok: bool | None, error: str | None, duration_ms: int | None,
            payload: dict[str, Any] | None, title: str | None = None) -> None:
    if not event_id:
        return
    try:
        with session() as db:
            ev = db.get(TimelineEvent, event_id)
            if ev is None:
                return
            ev.ok, ev.duration_ms = ok, duration_ms
            ev.error = str(error)[:1000] if error else None
            if title:
                ev.title = title[:300]
            ev.payload = {**(ev.payload or {}), **(payload or {})}
            ev.payload.pop("status", None)
            db.add(ev)
            db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("timeline: could not finish entry %s: %s", event_id, e)


@dataclass
class Run:
    """An open job / agent run / scheduler entry; set the outcome with finish() (else: ok unless it raised)."""
    id: int | None
    ok: bool | None = None
    error: str | None = None
    title: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def finish(self, ok: bool = True, error: str | None = None, payload: dict[str, Any] | None = None,
               title: str | None = None) -> None:
        self.ok, self.error, self.title = ok, error, title or self.title
        self.payload.update(payload or {})


@asynccontextmanager
async def run(kind: str, title: str, *, name: str | None = None, actor: str | None = None,
              **refs: Any) -> AsyncIterator[Run]:
    """Open a run: one entry at the start (shown as running), its id as `run_id` for everything inside, the outcome
    and duration at the end. An exception marks it failed and is re-raised."""
    rid = record(kind, title, name=name, ok=None, actor=actor, payload={"status": "running"}, **refs)
    r = Run(rid)
    started = time.monotonic()
    inner = {**_clean({k: v for k, v in refs.items()}), "run_id": rid or current().get("run_id")}
    if actor:
        inner["actor"] = actor
    tok = _ctx.set({**current(), **inner})
    try:
        yield r
    except BaseException as e:
        if r.ok is None:
            r.ok, r.error = False, str(e) or type(e).__name__
        raise
    finally:
        _ctx.reset(tok)
        _finish(rid, ok=True if r.ok is None else r.ok, error=r.error,
                duration_ms=int((time.monotonic() - started) * 1000), payload=r.payload, title=r.title)


def action(name: str, title: str | None = None, *, ok: bool = True, error: str | None = None,
           payload: dict[str, Any] | None = None, **refs: Any) -> int | None:
    """A state-changing click or command ("chat.send", "myads.delete", "settings.claude")."""
    return record("action", title or name, name=name, ok=ok, error=error, payload=payload,
                  actor=current().get("actor") or "user", **refs)


@contextmanager
def action_scope(name: str, title: str | None = None, **refs: Any) -> Iterator[dict[str, Any]]:
    """Record an action when the block ends, with its outcome and duration; `payload` may be filled inside."""
    payload: dict[str, Any] = {}
    started = time.monotonic()
    try:
        with context(**refs):
            yield payload
    except Exception as e:
        record("action", title or name, name=name, ok=False, error=str(e) or type(e).__name__, payload=payload or None,
               duration_ms=int((time.monotonic() - started) * 1000), actor=current().get("actor") or "user", **refs)
        raise
    record("action", title or name, name=name, payload=payload or None,
           duration_ms=int((time.monotonic() - started) * 1000), actor=current().get("actor") or "user", **refs)


def llm_call(*, purpose: str | None, model: str, system: str, prompt: str, images: list[Path] | None,
             schema: dict[str, Any] | None, data: dict[str, Any] | None = None, error: str | None = None,
             duration_ms: int | None = None, cost_usd: float | None = None,
             raw: dict[str, Any] | None = None) -> int | None:
    """One Claude call with everything that went in and came out (app/llm/claude_cli.py)."""
    from app.i18n import t
    from app.llm.base import PURPOSES
    from app.llm.usage import _rows_from

    rows = _rows_from(raw) if raw else []
    tokens = {k: sum(r[k] for r in rows) for k in ("input", "output", "cache_read", "cache_write")} if rows else None
    raw = raw or {}
    label = t(PURPOSES[purpose][1]) if purpose in PURPOSES else t("Other (tests, misc.)")
    payload = {"purpose": purpose or "other", "model": model, "images": [str(p) for p in images or []],
               "image_count": len(images or []), "schema_keys": sorted((schema or {}).get("properties", {}).keys()),
               "tokens": tokens, "cost_usd": cost_usd if cost_usd is not None else raw.get("total_cost_usd"),
               "session_id": raw.get("session_id"), "num_turns": raw.get("num_turns")}
    blobs = {"system": system, "prompt": prompt,
             "schema": json.dumps(schema, ensure_ascii=False, indent=1) if schema else None,
             "response": json.dumps(data, ensure_ascii=False, indent=2) if data is not None else None}
    return record("llm", f"{label} · {model}", name=purpose or "other", ok=error is None, error=error,
                  duration_ms=duration_ms, payload={k: v for k, v in payload.items() if v not in (None, [], {})},
                  blobs=blobs)


def _(text: str) -> str:
    """Marks a label for translation; translated where it is shown (the timeline page)."""
    return text


# Labels of actions: route actions are named "POST <route path>" (app/timeline_web.py records every state-changing
# request), others by what they do. Unknown names show their stored title.
ACTION_LABELS: dict[str, str] = {
    "POST /agents": _("Search agent created"), "POST /agents/{agent_id}/run": _("Search agent checked now"),
    "POST /agents/{agent_id}/toggle": _("Search agent switched on/off"), "POST /agents/{agent_id}/edit": _("Search agent edited"),
    "POST /agents/{agent_id}/delete": _("Search agent deleted"), "POST /hits/{hit_id}/state": _("Hit marked"),
    "POST /hits/mark-seen": _("Hits marked as seen"),
    "POST /chats/bulk/{action}": _("Chats changed"), "POST /chats/{conversation_id}/hide": _("Chat hidden"),
    "POST /chats/{conversation_id}/remove": _("Chat removed"), "POST /chats/sync": _("Chats synced"),
    "POST /chats/{conversation_id}/refresh": _("Chat reloaded"), "POST /chats/{conversation_id}/draft": _("Reply draft requested"),
    "POST /drafts/{draft_id}/send": _("Reply sent"), "POST /drafts/{draft_id}/mark-sent": _("Reply marked as sent"),
    "POST /drafts/{draft_id}/discard": _("Reply draft discarded"),
    "POST /chats/{conversation_id}/calendar": _("Appointment added to the calendar"),
    "POST /chats/{conversation_id}/calendar/find": _("Appointment looked up"),
    "POST /chats/{conversation_id}/calendar/forget": _("Appointment forgotten"),
    "POST /chats/{conversation_id}/shipping/detect": _("Shipping details read from the chat"),
    "POST /chats/{conversation_id}/paylivery/{action}": _("PayLivery step"),
    "POST /myads/check": _("My ads checked"), "POST /myads/{ad_id}/auto-renew": _("Auto-renew switched"),
    "POST /myads/{ad_id}/auto-reply": _("Automatic answers changed"), "POST /myads/{ad_id}/edit/ai": _("Ad text revised by Claude"),
    "POST /myads/{ad_id}/edit": _("Ad edited"), "POST /myads/{ad_id}/photos": _("Ad photos changed"),
    "POST /myads/bulk/{action}": _("Ads changed"), "POST /myads/cleanup": _("Ads cleaned up"),
    "POST /myads/{ad_id}/deactivate": _("Ad deactivated"), "POST /myads/{ad_id}/delete": _("Ad deleted"),
    "POST /sold/{ad_id}/willhaben": _("Ad marked sold / reserved"), "POST /sold/{ad_id}/notices": _("Notices to other buyers drafted"),
    "POST /sold/{ad_id}/notices/send": _("Notices to other buyers sent"), "POST /sold/{ad_id}/finish": _("Sale finished"),
    "POST /sold/{ad_id}/reset": _("Sale reset"), "POST /sold/{ad_id}/sales/{sale_id}/undo": _("Sale undone"),
    "POST /myads/{ad_id}/quantity": _("Pieces changed"),
    "POST /new-ad": _("Ad draft created"), "POST /new-ad/{draft_id}": _("Ad draft saved"),
    "POST /new-ad/{draft_id}/refine": _("Ad draft revised"), "POST /new-ad/{draft_id}/undo": _("Ad draft change undone"),
    "POST /new-ad/{draft_id}/geizhals": _("Geizhals specs looked up"),
    "POST /new-ad/{draft_id}/willhaben/prepare": _("Publishing prepared"), "POST /new-ad/{draft_id}/willhaben/category": _("Category chosen"),
    "POST /new-ad/{draft_id}/willhaben/publish": _("Ad published"), "POST /new-ad/batch": _("Batch created"),
    "POST /new-ad/batch/{batch_id}": _("Batch sorted"), "POST /new-ad/batch/{batch_id}/delete": _("Batch deleted"),
    "POST /new-ad/batch/{batch_id}/drafts": _("Batch ads changed"),
    "POST /shipments/{shipment_id}": _("Shipment saved"), "POST /shipments/{shipment_id}/option": _("Parcel option chosen"),
    "POST /shipments/{shipment_id}/label": _("Label form opened"), "POST /shipments/{shipment_id}/status": _("Shipment status changed"),
    "POST /shipments/{shipment_id}/paid": _("Marked as paid"), "POST /shipments/{shipment_id}/unpaid": _("Marked as unpaid"),
    "POST /shipments/{shipment_id}/approve": _("Label purchase approved"), "POST /shipments/{shipment_id}/decline": _("Label purchase declined"),
    "POST /shipments/{shipment_id}/retry": _("Label purchase retried"), "POST /shipments/{shipment_id}/print": _("Label printed"),
    "POST /purchases": _("Purchase order created"), "POST /purchases/{pid}/{action}": _("Purchase order changed"),
    "POST /deals/search": _("Product search started"), "POST /deals/search/{search_id}/rerun": _("Product search run again"),
    "POST /deals/search/{search_id}/answers": _("Product search questions answered"),
    "POST /deals/search/{search_id}/agent": _("Search agent created from a product search"),
    "POST /deals/ad/{ad_id}/claude": _("Price verdict asked of Claude"),
    "POST /session/start": _("Browser started"), "POST /session/login": _("willhaben login opened"),
    "POST /session/capture/start": _("API recording started"), "POST /session/capture/stop": _("API recording stopped"),
    "POST /settings": _("Settings saved"),
    "POST /api/chats/{conversation_id}/draft": _("Reply draft requested"), "POST /api/drafts/{draft_id}/send": _("Reply sent"),
    "POST /api/agents": _("Search agent created"),
}


def action_label(name: str, fallback: str = "") -> str:
    from app.i18n import t

    label = ACTION_LABELS.get(name)
    return t(label) if label else (fallback or name)


def ad_ref_for(platform: Any, ad_id: Any) -> str:
    """The ad id as it is stored on entries: bare for willhaben, "<platform>:<id>" for other sites."""
    from app.platforms import ad_ref

    return ad_ref(str(platform) if platform else None, str(ad_id))


def resolve_ids(fields: dict[str, Any]) -> dict[str, Any]:
    """Add the ids a known thing leads to: a reply draft or shipment -> its chat, a chat -> its ad, a hit -> its agent
    and ad, a purchase -> its ad and chat. Missing rows are ignored."""
    from app.db import Conversation, Draft, Hit, Purchase, Shipment

    out = dict(fields)
    try:
        with session() as db:
            if (rd := out.get("reply_draft")) and str(rd).isdigit() and (d := db.get(Draft, int(rd))):
                out.setdefault("conversation_id", d.conversation_id)
            if (sid := out.get("shipment_id")) and str(sid).isdigit() and (sh := db.get(Shipment, int(sid))):
                out.setdefault("conversation_id", sh.conversation_id)
                out.setdefault("ad_id", sh.ad_id)
            if (hid := out.get("hit_id")) and str(hid).isdigit() and (h := db.get(Hit, int(hid))):
                out.setdefault("agent_id", h.agent_id)
                out.setdefault("ad_id", ad_ref_for(h.platform, h.ad_id))
            if (pid := out.get("purchase_id")) and str(pid).isdigit() and (pu := db.get(Purchase, int(pid))):
                out.setdefault("ad_id", pu.ad_id)
                out.setdefault("conversation_id", pu.conversation_id)
                out.setdefault("search_id", pu.search_id)
            if (cid := out.get("conversation_id")) and (c := db.get(Conversation, str(cid))):
                out.setdefault("ad_id", c.ad_id)
                if c.role == "seller":
                    out.setdefault("mine", True)
    except Exception as e:  # noqa: BLE001
        log.debug("timeline: resolving ids failed: %s", e)
    return {k: v for k, v in out.items() if v is not None}


def recording_stats() -> dict[str, Any] | None:
    """Stored willhaben requests for Settings → Timeline (None: recording not installed)."""
    rec = _recorder()
    return rec.stats() if rec else None


def recording_view(e: TimelineEvent) -> dict[str, Any] | None:
    """The stored willhaben request/response of a "wh" entry, for the detail view (None: none stored)."""
    rec = _recorder()
    return rec.view_for_event(e.id) if rec else None


# ------------------------------------------------------------------ queries (page + API)
KIND_GROUPS = {"llm": ("llm",), "wh": ("wh",), "action": ("action",), "runs": RUN_KINDS, "notify": ("notify",)}
RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
FILTER_PARAMS = {"ad": "ad_id", "conversation": "conversation_id", "agent": "agent_id", "purchase": "purchase_id",
                 "search": "search_id", "batch": "batch_id", "draft": "draft_id", "shipment": "shipment_id"}


@dataclass
class Filters:
    kind: str = ""                 # a KIND_GROUPS key, or "" for all
    errors: bool = False
    q: str = ""
    range: str = "all"
    before: int | None = None      # id cursor ("Load more")
    limit: int = 50
    job: int | None = None         # a run and everything inside it
    ids: dict[str, str] = field(default_factory=dict)   # FILTER_PARAMS keys -> value
    site: str = ""                 # requests to this platform only ("willhaben", "vinted", …)

    @property
    def flat(self) -> bool:
        """The plain "All" view folds the steps of a run into the run's entry; filters show every match."""
        return bool(self.kind or self.errors or self.q or self.job or self.ids or self.site)


def _site_of() -> Any:
    """The platform of a request entry: payload.platform, absent (NULL) for willhaben."""
    return func.coalesce(func.json_extract(TimelineEvent.payload, "$.platform"), "willhaben")


def _where(f: Filters) -> list[Any]:
    w: list[Any] = []
    if f.kind in KIND_GROUPS:
        w.append(col(TimelineEvent.kind).in_(KIND_GROUPS[f.kind]))
    if f.errors:
        w.append(col(TimelineEvent.ok).is_(False))
    if f.q:
        like = f"%{f.q.strip()}%"
        w.append(or_(col(TimelineEvent.title).like(like), col(TimelineEvent.name).like(like),
                     col(TimelineEvent.error).like(like)))
    if RANGES.get(f.range):
        w.append(TimelineEvent.ts >= utcnow() - RANGES[f.range])
    if f.job:
        w.append(or_(TimelineEvent.id == f.job, TimelineEvent.run_id == f.job))
    if f.site:
        w.append(TimelineEvent.kind == "wh")
        w.append(_site_of() == f.site)
    for param, value in f.ids.items():
        column = FILTER_PARAMS.get(param)
        if column and value not in (None, ""):
            v = _coerce(column, value)
            if v is not None:
                w.append(getattr(TimelineEvent, column) == v)
    if not f.flat:      # the overview: steps stay inside their run (notifications are shown on their own as well),
        # plain requests to the sites only in the requests tab, a filter or their run; failed ones always
        w.append(or_(col(TimelineEvent.run_id).is_(None), TimelineEvent.kind == "notify"))
        w.append(or_(TimelineEvent.kind != "wh", col(TimelineEvent.ok).is_not(True)))
    return w


def query(f: Filters) -> tuple[list[TimelineEvent], bool]:
    """Entries matching the filters, newest first, and whether there are more."""
    limit = max(1, min(f.limit, 200))
    q = select(TimelineEvent).where(*_where(f))
    if f.before:
        q = q.where(TimelineEvent.id < f.before)
    with session() as db:
        rows = list(db.exec(q.order_by(col(TimelineEvent.id).desc()).limit(limit + 1)))
    return rows[:limit], len(rows) > limit


def step_counts(run_ids: list[int]) -> dict[int, dict[str, int]]:
    """Entries inside each run, per kind: {run_id: {"llm": 2, "wh": 5}}."""
    if not run_ids:
        return {}
    out: dict[int, dict[str, int]] = {}
    with session() as db:
        for rid, kind, n in db.exec(select(TimelineEvent.run_id, TimelineEvent.kind, func.count())
                                    .where(col(TimelineEvent.run_id).in_(run_ids))
                                    .group_by(TimelineEvent.run_id, TimelineEvent.kind)):
            out.setdefault(rid, {})[kind] = n
    return out


def steps(run_id: int, limit: int = 200) -> list[TimelineEvent]:
    with session() as db:
        return list(db.exec(select(TimelineEvent).where(TimelineEvent.run_id == run_id)
                            .order_by(col(TimelineEvent.id)).limit(limit)))


def get(event_id: int) -> tuple[TimelineEvent | None, dict[str, str]]:
    """One entry and its long texts, decoded."""
    with session() as db:
        ev = db.get(TimelineEvent, event_id)
        if ev is None:
            return None, {}
        blobs = list(db.exec(select(TimelineBlob).where(TimelineBlob.event_id == event_id).order_by(col(TimelineBlob.id))))
    return ev, {b.name: blob_text(b) for b in blobs}


def counts_by_kind() -> dict[str, int]:
    with session() as db:
        rows = db.exec(select(TimelineEvent.kind, func.count()).group_by(TimelineEvent.kind)).all()
    by = {k: int(n) for k, n in rows}
    return {g: sum(by.get(k, 0) for k in kinds) for g, kinds in KIND_GROUPS.items()}


def counts_by_site() -> dict[str, int]:
    """Request entries per platform, most first ({"willhaben": 120, "vinted": 4})."""
    with session() as db:
        rows = db.exec(select(_site_of(), func.count()).where(TimelineEvent.kind == "wh").group_by(_site_of())).all()
    return dict(sorted(((str(k), int(n)) for k, n in rows), key=lambda kv: -kv[1]))


def site_of(e: TimelineEvent) -> str:
    """The platform a request entry went to (willhaben when the entry does not say)."""
    return str((e.payload or {}).get("platform") or "willhaben") if e.kind == "wh" else ""


def links(e: TimelineEvent) -> list[dict[str, Any]]:
    """Pages of this app the entry belongs to: [{label, href, ad}] (`ad` opens the ad viewer)."""
    from app.i18n import t
    from app.services.notify import app_path

    out: list[dict[str, Any]] = []
    extra = e.extra or {}
    if e.kind == "notify":
        path = app_path(e.name or "", e.url, e.ad_id, e.conversation_id)
        if path:
            out.append({"label": t("Open"), "href": path, "ad": None})
        return out
    if e.conversation_id:
        out.append({"label": t("Chat"), "href": f"/chats/{e.conversation_id}", "ad": None})
    if e.ad_id:
        if extra.get("mine"):
            out.append({"label": t("My ad"), "href": f"/myads?ad={e.ad_id}", "ad": None})
        else:
            out.append({"label": t("Ad"), "href": f"/hits?view={e.ad_id}", "ad": e.ad_id})
    if e.agent_id:
        out.append({"label": t("Search agent"), "href": f"/hits?agent_id={e.agent_id}&state=all", "ad": None})
    if e.search_id:
        out.append({"label": t("Product search"), "href": f"/deals?search={e.search_id}", "ad": None})
    if e.purchase_id:
        out.append({"label": t("Purchase"), "href": f"/purchases/{e.purchase_id}", "ad": None})
    if e.draft_id:
        out.append({"label": t("Ad draft"), "href": f"/new-ad/{e.draft_id}", "ad": None})
    if e.batch_id:
        out.append({"label": t("Batch"), "href": f"/new-ad/batch/{e.batch_id}", "ad": None})
    return out


def filter_ids(e: TimelineEvent) -> list[tuple[str, str]]:
    """(param, value) pairs to narrow the page down to this entry's things."""
    return [(p, str(getattr(e, c))) for p, c in FILTER_PARAMS.items() if getattr(e, c) not in (None, "")]


# ------------------------------------------------------------------ retention
def config() -> dict[str, Any]:
    def _int(key: str) -> int:
        raw = get_setting(key)
        return int(raw) if raw.strip().isdigit() else 0
    return {"record": get_setting(SETTING_TIMELINE) == "1", "record_wh": get_setting(SETTING_WH) == "1",
            "enabled": get_setting("timeline_prune") == "1", "days": _int("timeline_days"), "max_mb": _int("timeline_max_mb")}


def save(enabled: bool, days: int | None, max_mb: int | None) -> None:
    set_setting("timeline_prune", "1" if enabled else "0")
    set_setting("timeline_days", str(max(0, days or 0)))
    set_setting("timeline_max_mb", str(max(0, max_mb or 0)))


def _recorder():
    try:
        from app.willhaben import recorder
        return recorder
    except ImportError:      # recording of willhaben requests not installed
        return None


_size_cache: tuple[float, dict[str, Any]] | None = None
SIZE_CACHE_S = 120          # the pages show a size this old: summing ~1 GB of texts for every view is too slow


def size(fresh: bool = False) -> dict[str, Any]:
    """Stored entries and their approximate size (long texts + willhaben bodies + rows), plus what the database
    file takes and how much of it is free pages (left by pruning until a VACUUM). Cached SIZE_CACHE_S unless
    `fresh` (the pruning's own loop). Reads the whole blob table: call it off the event loop."""
    global _size_cache
    import time as _time
    if not fresh and _size_cache and _time.monotonic() - _size_cache[0] < SIZE_CACHE_S:
        return _size_cache[1]
    with session() as db:
        events = int(db.exec(select(func.count()).select_from(TimelineEvent)).one())
        blob_bytes = int(db.exec(select(func.coalesce(func.sum(func.length(TimelineBlob.data)), 0))).one() or 0)
        oldest = db.exec(select(func.min(TimelineEvent.ts))).one()
    rec = _recorder()
    rec_bytes = rec.storage_size() if rec else 0
    total = blob_bytes + rec_bytes + events * EVENT_EST_BYTES
    out = {"events": events, "blob_bytes": blob_bytes, "recording_bytes": rec_bytes, "bytes": total,
           "mb": round(total / 1_000_000, 1), "oldest": oldest, **_file_size()}
    _size_cache = (_time.monotonic(), out)
    return out


def _file_size() -> dict[str, Any]:
    """The database file: its size and its free pages (SQLite reuses them, the file only shrinks with a VACUUM)."""
    try:
        with session() as db:
            conn = db.connection()
            page, pages, free = (int(conn.exec_driver_sql(f"PRAGMA {p}").scalar() or 0)
                                 for p in ("page_size", "page_count", "freelist_count"))
    except Exception:  # noqa: BLE001 - an in-memory test database, a driver without PRAGMA rows
        return {"file_mb": None, "free_mb": None}
    return {"file_mb": round(page * pages / 1_000_000, 1), "free_mb": round(page * free / 1_000_000, 1)}


VACUUM_FREE_MB = 100        # free pages worth giving back to the disk (at night, in the quiet hours)


def compact(force: bool = False) -> bool:
    """Give the free pages back (VACUUM) and shorten the WAL: only when at least VACUUM_FREE_MB are free (or
    `force`). Locks the database while it runs (seconds for ~1 GB): the caller picks a quiet time, off the loop."""
    info = _file_size()
    if not force and (info["free_mb"] or 0) < VACUUM_FREE_MB:
        return False
    from app.db import get_engine
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:   # VACUUM: no transaction
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.exec_driver_sql("VACUUM")
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    global _size_cache
    _size_cache = None
    log.info("database compacted: %s MB were free", info["free_mb"])
    return True


def _delete_events(db, ids: list[int]) -> None:
    db.exec(delete(TimelineBlob).where(col(TimelineBlob.event_id).in_(ids)))
    rec = _recorder()
    if rec:
        rec.delete_for_events(db, ids)
    db.exec(delete(TimelineEvent).where(col(TimelineEvent.id).in_(ids)))


def prune(days: int | None = None, max_mb: int | None = None, batch: int = 500) -> dict[str, int]:
    """Delete entries older than `days`, then the oldest ones until the size is within `max_mb` (willhaben bodies of
    old requests go first, their metadata stays a little longer). 0/None = no limit of that kind."""
    deleted = bodies = 0
    rec = _recorder()
    if days:
        cutoff = utcnow() - timedelta(days=days)
        if rec:
            rec.prune_before(cutoff)
        while True:
            with session() as db:
                ids = list(db.exec(select(TimelineEvent.id).where(TimelineEvent.ts < cutoff).limit(batch)))
                if not ids:
                    break
                _delete_events(db, ids)
                db.commit()
            deleted += len(ids)
    if max_mb:
        budget = max_mb * 1_000_000
        while size(fresh=True)["bytes"] > budget:
            if rec and (n := rec.drop_oldest_bodies(200)):
                bodies += n
                continue
            with session() as db:
                ids = list(db.exec(select(TimelineEvent.id).order_by(col(TimelineEvent.id)).limit(batch)))
                if not ids:
                    break
                _delete_events(db, ids)
                db.commit()
            deleted += len(ids)
    return {"deleted": deleted, "bodies_dropped": bodies}


async def prune_job() -> None:
    """Scheduler (hourly): apply Settings → Timeline when pruning is switched on."""
    import asyncio

    cfg = config()
    if not cfg["enabled"] or not (cfg["days"] or cfg["max_mb"]):
        return
    try:
        res = await asyncio.to_thread(prune, cfg["days"], cfg["max_mb"])
    except Exception as e:  # noqa: BLE001 - keep the scheduler job alive
        log.warning("timeline prune failed: %s", e)
        return
    if res["deleted"] or res["bodies_dropped"]:
        log.info("timeline pruned: %s", res)
    from app.services import quiet_hours
    q = quiet_hours.state()
    if q and q.active:                   # the night: nobody waits on the app while VACUUM holds the database
        try:
            await asyncio.to_thread(compact)
        except Exception as e:  # noqa: BLE001 - keep the scheduler job alive
            log.warning("database compaction failed: %s", e)


def mark_interrupted() -> int:
    """At startup: runs still marked as running were cut off by a restart."""
    try:
        with session() as db:
            res = db.exec(update(TimelineEvent).where(col(TimelineEvent.ok).is_(None))
                          .values(ok=False, error="interrupted (restart)"))
            db.commit()
            return int(res.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("timeline: %s", e)
        return 0
