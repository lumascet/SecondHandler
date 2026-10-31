"""The timeline page (/timeline), one entry's details (/timeline/{id}), the JSON twins and Settings → Timeline.

Kept out of main.py (one include there); `render` is imported from app.main at call time.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.routing import Match

from app.i18n import t
from app.services import timeline
from app.willhaben import recorder

router = APIRouter()
_templates: Jinja2Templates | None = None


def _(text: str) -> str:
    """Marks a label for translation; translated where it is shown."""
    return text


KIND_LABELS = {"llm": _("Claude"), "wh": _("Request"), "action": _("Action"), "job": _("Job"),
               "agent_run": _("Agent run"), "sched": _("Scheduled"), "notify": _("Notification")}
KIND_TONES = {"llm": "brand", "action": "ok", "notify": "warn"}
GROUP_LABELS = [("", _("All")), ("llm", _("Claude")), ("wh", _("Requests")), ("action", _("Actions")),
                ("runs", _("Jobs & runs")), ("notify", _("Notifications"))]
ACTOR_LABELS = {"user": _("you"), "api": _("API"), "mcp": _("MCP"), "telegram": _("Telegram"), "ha": _("Home Assistant"),
                "scheduler": _("scheduler"), "system": _("app")}
RANGE_LABELS = [("all", _("all time")), ("24h", _("last 24 h")), ("7d", _("last 7 days")), ("30d", _("last 30 days"))]
PARAM_LABELS = {"ad": _("Ad"), "conversation": _("Chat"), "agent": _("Search agent"), "purchase": _("Purchase"),
                "search": _("Product search"), "batch": _("Batch"), "draft": _("Ad draft"), "shipment": _("Shipment")}
BLOB_ORDER = ("system", "prompt", "schema", "response")
BLOB_LABELS = {"system": _("System prompt"), "prompt": _("Prompt"), "schema": _("Answer schema"), "response": _("Answer"),
               "request": _("Request body"), "response_body": _("Response body")}


def kind_label(kind: str) -> str:
    return t(KIND_LABELS.get(kind, kind))


def site_name(pid: str | None) -> str:
    """A platform's display name ("Vinted"); the id itself when the platform is gone."""
    from app import platforms

    try:
        return platforms.get(pid or "willhaben").name
    except Exception:  # noqa: BLE001 - an entry of a platform that no longer exists
        return pid or "willhaben"


def pill_label(e) -> str:
    """The row's label: the site for a request ("willhaben", "Vinted"), else the kind."""
    return site_name(timeline.site_of(e)) if e.kind == "wh" else kind_label(e.kind)


def actor_label(actor: str | None) -> str:
    return t(ACTOR_LABELS.get(actor or "", actor or ""))


def duration(ms: int | None) -> str:
    if ms is None:
        return ""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s" if ms < 120_000 else f"{ms / 60000:.0f} min"


def title_of(e) -> str:
    """The list line: actions by their label (the stored title is the fallback)."""
    if e.kind == "action":
        return timeline.action_label(e.name or "", e.title)
    if e.kind == "wh":
        return re.sub(r"^\[[a-z0-9_]+\] ", "", e.title)     # the pill names the site ("[vinted] GET …" before)
    return e.title


def facts(e) -> list[tuple[str, str]]:
    """Payload as (label, value) lines for the detail view."""
    p = dict(e.payload or {})
    out: list[tuple[str, str]] = []
    if e.kind == "wh":
        return []                 # the request block below shows all of it
    if e.kind == "llm":
        p.pop("purpose", None)    # = the kind line
        if model := p.pop("model", None):
            out.append((t("Model"), model))
        tok = p.pop("tokens", None) or {}
        if tok:
            out.append((t("Tokens"), t("{i} in · {o} out · {c} from cache", i=tok.get("input", 0), o=tok.get("output", 0),
                                         c=tok.get("cache_read", 0))))
        if (cost := p.pop("cost_usd", None)) is not None:
            out.append((t("API price"), f"$ {cost:.4f}"))
        imgs = p.pop("images", None) or []
        p.pop("image_count", None)
        if imgs:
            out.append((t("Photos"), ", ".join(imgs)))
        if keys := p.pop("schema_keys", None):
            out.append((t("Answer fields"), ", ".join(keys)))
    for k, v in p.items():
        if v in (None, "", [], {}):
            continue
        out.append((k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
    for k, v in (e.extra or {}).items():
        if k != "mine":       # internal: picks the link to "My ads"
            out.append((k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
    return out


def _filters(request: Request) -> timeline.Filters:
    qp = request.query_params

    def _int(name: str) -> int | None:
        raw = (qp.get(name) or "").strip()
        return int(raw) if raw.isdigit() else None

    rng = qp.get("range") or "all"
    site = (qp.get("site") or "").strip().lower()
    return timeline.Filters(kind=qp.get("kind") or "", errors=qp.get("errors") in ("1", "on", "true"),
                            q=(qp.get("q") or "").strip()[:100], range=rng if rng in timeline.RANGES else "all",
                            before=_int("before"), limit=_int("limit") or 50, job=_int("job"),
                            ids={p: qp[p].strip() for p in timeline.FILTER_PARAMS if (qp.get(p) or "").strip()},
                            site=site if re.fullmatch(r"[a-z][a-z0-9_]{0,30}", site) else "")


def _params(f: timeline.Filters, **change: Any) -> str:
    """Query string for these filters with some values changed (None/"" drops one)."""
    base: dict[str, Any] = {"kind": f.kind, "site": f.site, "errors": "1" if f.errors else "", "q": f.q,
                            "range": "" if f.range == "all" else f.range, "job": f.job or "", **f.ids}
    base.update(change)
    clean = {k: v for k, v in base.items() if v not in (None, "")}
    return ("?" + urlencode(clean)) if clean else ""


def _row_ctx(rows: list, f: timeline.Filters) -> dict[str, Any]:
    runs = [r.id for r in rows if r.kind in timeline.RUN_KINDS]
    return {"rows": rows, "step_counts": timeline.step_counts(runs), "filters": f}


# ------------------------------------------------------------------ every request: context ids + actions
NOT_RECORDED = ("/session/input", "/lang", "/inbox/read-all", "/login", "/logout", "/bugreport", "/settings/timeline",
                "/mcp")    # /mcp: its tools call /api/ in process, recorded there with actor "mcp"
_MINE = ("/myads", "/sold", "/api/myads")          # ad ids of these routes are my own ads
_REPLY_DRAFTS = ("/drafts/", "/api/drafts/")      # draft ids of these routes are chat reply drafts


def route_of(app: FastAPI, scope: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The matched route's path ("/drafts/{draft_id}/send") and its path parameters."""
    for r in app.router.routes:
        match, child = r.matches(scope)
        if match == Match.FULL:
            path = getattr(r, "path", None)
            return (re.sub(r":\w+}", "}", path) if path else None), dict(child.get("path_params") or {})
    return None, {}


def path_ids(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Timeline context from a route's path parameters."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in ("conversation_id", "agent_id", "shipment_id", "search_id", "batch_id"):
            out[k] = v
        elif k == "ad_id":
            out["ad_id"] = timeline.ad_ref_for(params.get("platform"), v)
            if path.startswith(_MINE):
                out["mine"] = True
        elif k == "draft_id":
            out["reply_draft" if path.startswith(_REPLY_DRAFTS) else "draft_id"] = v
        elif k == "pid":
            out["purchase_id"] = v
        elif k == "hit_id":
            out["hit_id"] = v
        elif k in ("action", "section", "carrier", "which"):
            out[k] = v
    return timeline.resolve_ids(out) if out else out


def _message(location: str | None) -> str | None:
    """The ?msg= of a redirect: what the page told the user."""
    if not location:
        return None
    msg = parse_qs(urlsplit(location).query).get("msg")
    return msg[0][:300] if msg else None


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    global _templates
    _templates = templates
    templates.env.globals.update(tl_kind_label=kind_label, tl_actor_label=actor_label, tl_duration=duration,
                                 tl_links=timeline.links, tl_title=title_of, tl_kind_tones=KIND_TONES,
                                 tl_pill_label=pill_label, tl_site_name=site_name)
    app.include_router(router)

    @app.middleware("http")
    async def timeline_context(request: Request, call_next):
        """Who did it and which things it is about, for everything this request causes (also the background jobs it
        starts); state-changing requests are recorded as actions."""
        path = request.url.path
        if path.startswith("/static") or not (timeline.enabled() or recorder.enabled()):
            return await call_next(request)          # both off (the default): nothing to attach or record
        actor = "mcp" if request.headers.get("x-wh-client-kind") == "mcp" else "api" if path.startswith("/api/") else "user"
        route, params = route_of(app, request.scope)
        ids = path_ids(route, params) if route else {}
        record = request.method in ("POST", "PUT", "DELETE") and route is not None and not path.startswith(NOT_RECORDED)
        started = time.monotonic()
        with timeline.fresh(actor=actor, **ids):
            try:
                response = await call_next(request)
            except Exception as e:
                if record:
                    timeline.action(f"{request.method} {route}", route, ok=False, error=str(e) or type(e).__name__)
                raise
            if record and response.status_code not in (401, 403):
                ok = response.status_code < 400
                msg = _message(response.headers.get("location"))
                timeline.record("action", route, name=f"{request.method} {route}", ok=ok,
                                error=None if ok else (msg or f"HTTP {response.status_code}"),
                                duration_ms=int((time.monotonic() - started) * 1000),
                                payload={k: v for k, v in {"status": response.status_code, "message": msg}.items() if v},
                                actor=actor)
        return response


@router.get("/timeline", response_class=HTMLResponse)
async def timeline_page(request: Request):
    from app.main import render

    f = _filters(request)
    rows, has_more = timeline.query(f)
    ctx = {**_row_ctx(rows, f), "has_more": has_more, "next_url": "/timeline" + _params(f, before=rows[-1].id) if rows else ""}
    if request.headers.get("hx-request") and f.before:      # "Load more": only the next rows
        return _templates.TemplateResponse(request, "_timeline_rows.html", ctx)
    counts = timeline.counts_by_kind()
    tabs = [("/timeline" + _params(f, kind=k, site=None, before=None), t(label), counts.get(k) if k else None,
             f.kind == k or (k == "wh" and bool(f.site))) for k, label in GROUP_LABELS]
    site_tabs = []
    if f.kind == "wh" or f.site:      # the requests tab: one more row to pick a site
        by_site = timeline.counts_by_site()
        if len(by_site) > 1 or f.site:
            site_tabs = [("/timeline" + _params(f, kind="wh", site=None, before=None), t("All sites"),
                          sum(by_site.values()), not f.site)]
            site_tabs += [("/timeline" + _params(f, kind="wh", site=pid, before=None), site_name(pid), n, f.site == pid)
                          for pid, n in by_site.items()]
    chips = [(t(PARAM_LABELS[p]) + f" {v}", "/timeline" + _params(f, **{p: None})) for p, v in f.ids.items()]
    if f.job:
        chips.append((t("Run {id}", id=f.job), "/timeline" + _params(f, job=None)))
    return render(request, "timeline.html", **ctx, tabs=tabs, site_tabs=site_tabs, chips=chips,
                  ranges=[(k, t(label)) for k, label in RANGE_LABELS], size=timeline.size(), cfg=timeline.config())


@router.get("/timeline/{event_id:int}", response_class=HTMLResponse)
async def timeline_detail(request: Request, event_id: int):
    ev, blobs = timeline.get(event_id)
    if ev is None:
        raise HTTPException(404)
    order = [n for n in BLOB_ORDER if n in blobs] + [n for n in blobs if n not in BLOB_ORDER]
    texts = [(n, t(BLOB_LABELS.get(n, n)), blobs[n]) for n in order]
    steps = timeline.steps(ev.id) if ev.kind in timeline.RUN_KINDS else []
    filters = [(t("{thing}: all entries", thing=t(PARAM_LABELS[p])), "/timeline?" + urlencode({p: v}))
               for p, v in timeline.filter_ids(ev)]
    rec = timeline.recording_view(ev) if ev.kind == "wh" else None
    return _templates.TemplateResponse(request, "_timeline_detail.html", {
        "e": ev, "facts": facts(ev), "texts": texts, "steps": steps, "step_filters": timeline.Filters(),
        "narrow": filters, "rec": rec, "step_counts": {}})


@router.get("/timeline/{event_id:int}/text/{name}")
async def timeline_text(event_id: int, name: str):
    ev, blobs = timeline.get(event_id)
    if ev is None or name not in blobs:
        raise HTTPException(404)
    return PlainTextResponse(blobs[name], headers={"Content-Disposition": f'inline; filename="timeline-{event_id}-{name}.txt"'})


def _full(e) -> dict[str, Any]:
    """One entry with its long texts and, for a willhaben request, the whole request and answer."""
    d = _json(e, timeline.get(e.id)[1])
    if e.kind == "wh":
        d["recording"] = recorder.view_for_event(e.id, limit=None)
    return d


@router.get("/timeline/{event_id:int}/download")
async def timeline_download(event_id: int):
    """One entry as a JSON file to keep or pass on: texts, willhaben bodies and headers uncut, a run with its steps."""
    from fastapi.responses import Response

    ev, _blobs = timeline.get(event_id)
    if ev is None:
        raise HTTPException(404)

    def build() -> bytes:
        d = _full(ev)
        if ev.kind in timeline.RUN_KINDS:
            d["steps"] = [_full(s) for s in timeline.steps(ev.id)]
        return json.dumps(d, ensure_ascii=False, indent=2, default=str).encode()

    data = await asyncio.to_thread(build)
    name = f"logbuch-{event_id}-{ev.ts:%Y%m%d-%H%M%S}.json"
    return Response(data, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/timeline/wh/{recording_id:int}/{side}")
async def timeline_wh_body(recording_id: int, side: str):
    """A whole stored request or response body of a site (the detail view shows the beginning)."""
    from fastapi.responses import Response

    from app.willhaben import recorder

    if side not in ("request", "response"):
        raise HTTPException(404)
    body, ctype = recorder.body_of(recording_id, side)
    if body is None:
        raise HTTPException(404)
    # shown as text, never rendered: a recorded page must not run its scripts in the dashboard
    text_like = not ctype or ctype.startswith("text/") or "json" in ctype or "xml" in ctype or "javascript" in ctype
    media = "text/plain; charset=utf-8" if text_like else ctype
    return Response(body, media_type=media, headers={"Content-Disposition": f'inline; filename="wh-{recording_id}-{side}"',
                                                     "X-Content-Type-Options": "nosniff"})


@router.post("/settings/timeline/recordings/export")
async def settings_recordings_export(since: str = Form("7d"), kinds: str = Form(""), keep_texts: str | None = Form(None),
                                     site: str = Form("")):
    """Redacted fixture files of the stored requests (of one site, or all), as one ZIP download."""
    import io
    import tempfile
    import zipfile
    from pathlib import Path

    from fastapi.responses import Response

    from app.willhaben import export

    try:
        window = export.parse_since(since)
    except ValueError:
        window = None
    wanted = {k.strip() for k in kinds.split(",") if k.strip()} or None
    site = site.strip().lower() if re.fullmatch(r"[a-z][a-z0-9_]{0,30}", site.strip().lower()) else ""

    def build() -> bytes:
        with tempfile.TemporaryDirectory(prefix="wh-export-") as tmp:
            files = export.export_recordings(Path(tmp), since=window, kinds=wanted, keep_texts=bool(keep_texts),
                                             platforms={site} if site else None)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(f, f.relative_to(tmp).as_posix())
            return buf.getvalue()

    data = await asyncio.to_thread(build)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{site or "all-sites"}-fixtures.zip"'})


@router.post("/settings/timeline/recordings/delete")
async def settings_recordings_delete():
    from app.willhaben import recorder

    n = await asyncio.to_thread(recorder.delete_all)
    return RedirectResponse(f"/settings/timeline?msg={quote(t('{n} stored requests deleted.', n=n), safe='')}",
                            status_code=303)


def _json(e, blobs: dict[str, str] | None = None) -> dict[str, Any]:
    d = e.model_dump(mode="json")
    d["links"] = timeline.links(e)
    if blobs is not None:
        d["texts"] = blobs
    return d


@router.get("/api/timeline")
async def api_timeline(request: Request):
    f = _filters(request)
    rows, has_more = timeline.query(f)
    return {"entries": [_json(e) for e in rows], "has_more": has_more, "next_before": rows[-1].id if rows and has_more else None}


@router.get("/api/timeline/{event_id:int}")
async def api_timeline_entry(event_id: int):
    ev, blobs = timeline.get(event_id)
    if ev is None:
        raise HTTPException(404)
    d = _json(ev, blobs)
    if ev.kind in timeline.RUN_KINDS:
        d["steps"] = [_json(s) for s in timeline.steps(ev.id)]
    if ev.kind == "wh":
        d["recording"] = timeline.recording_view(ev)
    return d


@router.post("/settings/timeline")
async def settings_timeline(record: str | None = Form(None), enabled: str | None = Form(None), days: str = Form(""),
                            max_mb: str = Form("")):
    def _n(v: str) -> int:
        v = v.strip()
        return int(v) if v.isdigit() else 0
    timeline.set_recording(timeline=bool(record))
    timeline.save(bool(enabled), _n(days), _n(max_mb))
    return RedirectResponse(f"/settings/timeline?msg={quote(t('Changes saved'), safe='')}", status_code=303)


@router.post("/settings/timeline/recording")
async def settings_timeline_recording(record_wh: str | None = Form(None)):
    """The requests card's own switch (the timeline form above does not touch it)."""
    timeline.set_recording(willhaben=bool(record_wh))
    msg = t("Requests to the sites are recorded from now on.") if record_wh else t("Requests to the sites are no longer recorded.")
    return RedirectResponse(f"/settings/timeline?msg={quote(msg, safe='')}", status_code=303)


@router.post("/settings/timeline/prune")
async def settings_timeline_prune():
    cfg = timeline.config()
    if not (cfg["days"] or cfg["max_mb"]):
        msg = t("Set a number of days or a size first.")
    else:
        res = await asyncio.to_thread(timeline.prune, cfg["days"], cfg["max_mb"])
        msg = t("{n} entries deleted, {b} stored answers emptied.", n=res["deleted"], b=res["bodies_dropped"])
    return RedirectResponse(f"/settings/timeline?msg={quote(msg, safe='')}", status_code=303)
