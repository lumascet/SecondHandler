"""The Telegram Mini App (/tg): the app rebuilt in Telegram's own look, for the bot's menu button and the buttons in
its messages. Grouped lists with section headers, cells with an icon, a value and a chevron, Telegram's theme
colours, its back button and its main button (templates/tg/). The same data as the dashboard: new and saved hits
with their photos and the ad itself, the chats with their drafts and sending, my ads and purchase orders.

Logged in like every page: inside Telegram by the signed launch data of the linked chat (auth.telegram_user via
/login, no password). Kept out of main.py (one include there); `render` is imported from app.main at call time.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import col, select

from app import platforms
from app.db import Conversation, Draft, Hit, MyAd, Purchase, SearchAgent, session
from app.i18n import t
from app.platforms import PlatformError

log = logging.getLogger(__name__)
router = APIRouter()
_background: set[asyncio.Task] = set()
LIST_LIMIT = 60


def install(app: FastAPI) -> None:
    from app import tg_bridge
    from app.main import templates
    from app.services import purchase
    from app import tg_agents, tg_deals, tg_more, tg_new_ad, tg_purchases
    app.include_router(router)
    for area in (tg_new_ad, tg_deals, tg_purchases, tg_agents, tg_more):    # each area of the Mini App in its own module
        app.include_router(area.router)
    tg_bridge.install(app, templates)   # the Mini App's forms post to the dashboard's endpoints (app/tg_bridge.py)
    templates.env.globals.update(myad_status=label_of, purchase_status=purchase.status_label)


def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    from app.main import render
    return render(request, f"tg/{name}", embed=True, **ctx)


_OWN = {"request", "embed", "myad_status", "purchase_status"}


async def _page(request: Request, name: str, handler, *args: Any, **kw: Any):
    """A /tg page from a dashboard page's context: the dashboard's handler runs (its data, its side effects such as
    marking read) and `name` shows its context in the Mini App's look; a redirect (not found, login) passes as it is."""
    resp = await handler(request, *args, **kw)
    ctx = getattr(resp, "context", None)
    if ctx is None:
        return resp
    return _render(request, name, **{k: v for k, v in ctx.items() if k not in _OWN})


def _back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _later(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ------------------------------------------------------------------ home
@router.get("/tg", response_class=HTMLResponse)
async def tg_home(request: Request):
    from app.main import _nav_counts
    from app.services import chats
    counts = _nav_counts()
    with session() as db:
        o = chats.overview(db)
        saved = len(list(db.exec(select(Hit.id).where(Hit.state == "saved"))))
        agents = list(db.exec(select(SearchAgent)))
        myads = len(list(db.exec(select(MyAd.ad_id).where(MyAd.status == "active", col(MyAd.hidden).is_not(True)))))
        buys = len(list(db.exec(select(Purchase.id).where(col(Purchase.status).not_in(("done", "cancelled", "failed"))))))
    from app.services import health
    problems = [st for st in health.collect() if st.state == "problem"]   # as the dashboard's connection tiles
    return _render(request, "home.html", counts=counts, saved=saved, agents=agents, myads=myads, buys=buys,
                   chats_shown=len(o["shown"]), problems=problems)


# ------------------------------------------------------------------ hits
TABS = {"new": ("new",), "saved": ("saved",), "filtered": ("filtered", "pending"), "seen": ("seen",),
        "ignored": ("ignored",)}


@router.get("/tg/hits", response_class=HTMLResponse)
async def tg_hits(request: Request, state: str = "new", agent_id: str = "", platform: str = "", sort: str = "",
                  ship: str = "", level: str = "", offset: int = 0):
    """The hits page's tabs (New, Saved, Held back, Seen, Ignored) and its cards' content, as the dashboard's view
    shows them (main.HitView: the agent and site filters, the sort, shipping and price filters, far pickup-only ads
    left out); the dashboard's parameter names, so its bulk actions come back to the same view. `offset`: the next
    page of the list."""
    from app.main import HitView
    from app.services import search_agents
    state = state if state in TABS else "new"
    view = HitView.of(",".join(TABS[state]), agent_id, platform, sort, ship, level)
    offset = max(offset, 0)
    with session() as db:
        hits = view.hits(db, limit=LIST_LIMIT + 1, offset=offset)
        agents = {a.id: a for a in db.exec(select(SearchAgent).order_by(col(SearchAgent.name)))}
        counts = {k: HitView.of(",".join(v), agent_id, platform, sort, ship, level).count(db) for k, v in TABS.items()}
        sites = sorted({p for p in db.exec(select(Hit.platform).distinct()) if p})
    more = len(hits) > LIST_LIMIT
    return _render(request, "hits.html", hits=hits[:LIST_LIMIT], state=state, agents=agents, counts=counts,
                   sites=sites, agent_id=view.agent_id, platform=view.platform, sort=view.sort, ship=view.ship,
                   level=view.level, offset=offset, more=more, watch_saved=search_agents.watch_saved_on(),
                   keep_days=search_agents.keep_days())


@router.post("/tg/hits/{hit_id}/quick")
async def tg_hit_quick(request: Request, hit_id: int, state: str = Form(...), tab: str = Form("new")):
    """A card's Save / Seen / Ignore in the list: back to the same tab (and filters)."""
    from urllib.parse import urlencode
    await tg_hit_state(hit_id, state)
    form = await request.form()
    keep = {k: str(form.get(k)) for k in ("agent_id", "platform", "sort", "ship", "level") if form.get(k)}
    return _back(f"/tg/hits?{urlencode({'state': tab if tab in TABS else 'new', **keep})}")


@router.post("/tg/hits/{hit_id}/sync")
async def tg_hit_sync(hit_id: int):
    """Sync: the ad from its site now (still online, reserved, sold)."""
    from app.services import search_agents
    try:
        await search_agents.check_hit(hit_id)
    except PlatformError as e:
        log.info("mini app sync of %s failed: %s", hit_id, e)
    return _back(f"/tg/hits/{hit_id}")


@router.post("/tg/hits/{hit_id}/deal")
async def tg_hit_deal(hit_id: int):
    """Check the price closely: comparable ads, the Geizhals new price and Claude's verdict (as the ad viewer)."""
    from app import deals_web
    from app.services import deals
    h, fallback = deals_web._hit(hit_id)
    if h is None:
        raise HTTPException(404)

    async def check() -> None:
        try:
            res, error = await deals_web._analysis(h.platform or "willhaben", h.ad_id, None, fallback,
                                                   use_claude=True, hit=h)
        except Exception as e:  # noqa: BLE001 - shown in the price section
            log.warning("mini app price check of %s failed", hit_id, exc_info=True)
            res, error = None, str(e) or type(e).__name__
        if res is not None:
            deals.remember(res)
        if error:
            _check_errors[hit_id] = error
            log.info("mini app price check of %s: %s", hit_id, error)

    if not (task := _checks.get(hit_id)) or task.done():
        _check_errors.pop(hit_id, None)
        _checks[hit_id] = asyncio.create_task(check())
    return _back(f"/tg/hits/{hit_id}#deal")


async def _detail(h: Hit) -> Any:
    """The ad itself from its site (photos, description), else what the hit knows; a sold ad's kept photos."""
    from app.services import search_agents
    p = platforms.get(h.platform or "willhaben")
    d = None
    if p.can("detail"):
        try:
            d = await p.detail(h.ad_id, url=h.url)
        except (PlatformError, Exception) as e:  # noqa: BLE001 - the stored hit is shown instead
            log.info("mini app: ad %s not loaded: %s", h.ad_id, e)
        else:
            search_agents.record_status(h.platform or "willhaben", h.ad_id, search_agents.site_status(d), d)
    if d is None:
        d = platforms.AdDetail.from_hit(h)
    if not d.images:
        kept = search_agents.kept_photos(h)
        d.images = ([f"/hits/{h.id}/image"] + [f"/hits/{h.id}/image/{i}" for i in range(1, len(kept))] if kept
                    else [h.image_url] if h.image_url else [])
    return d


@router.get("/tg/hits/{hit_id}", response_class=HTMLResponse)
async def tg_hit(request: Request, hit_id: int):
    from app.services import search_agents
    with session() as db:
        h = db.get(Hit, hit_id)
        if h is None:
            raise HTTPException(404)
        if h.state == "new":             # opened = seen, as in the ad viewer; its push goes off the phone
            h.state = "seen"
            db.add(h)
            db.commit()
            db.refresh(h)
            search_agents.pushes_seen([h])
        agent = db.get(SearchAgent, h.agent_id)
    d = await _detail(h)
    with session() as db:
        h = db.get(Hit, hit_id) or h
    return _render(request, "hit.html", h=h, d=d, agent=agent, site=platforms.get(h.platform or "willhaben"),
                   **_deal_ctx(h))


# the price checks running (hit id → task) and the last one's error; a check takes up to half a minute, longer than a
# phone keeps a request open, so it runs on its own and the page's price section asks again until it is done
_checks: dict[int, asyncio.Task] = {}
_check_errors: dict[int, str] = {}


def _deal_ctx(h: Hit) -> dict[str, Any]:
    """The price section: the stored analysis, the price bar (before a check from the Geizhals new price alone) and
    whether a check is running."""
    from app.services import deals, price_bar
    res = deals.rerate(deals.cached(platforms.get(h.platform or "willhaben").ref(h.ad_id)))
    pos = (res or {}).get("position") or {}
    if pos.get("pos") is None and h.price and (h.geizhals or {}).get("price_from"):    # before a price check: the zones
        pos = deals.position(h.price, None, h.geizhals["price_from"], h.item_condition)
    bar = price_bar.layout(pos, f"{h.price:g} €" if h.price else "") if pos.get("pos") is not None else None
    task = _checks.get(h.id)
    return {"deal": res, "bar": bar, "checking": bool(task and not task.done()), "check_error": _check_errors.get(h.id)}


@router.get("/tg/hits/{hit_id}/deal", response_class=HTMLResponse)
async def tg_hit_deal_part(request: Request, hit_id: int):
    with session() as db:
        h = db.get(Hit, hit_id)
    if h is None:
        raise HTTPException(404)
    return _render(request, "_deal.html", h=h, **_deal_ctx(h))


@router.post("/tg/hits/{hit_id}/state")
async def tg_hit_state(hit_id: int, state: str = Form(...)):
    from app.services import owner_actions
    if state not in owner_actions.HIT_STATES:
        raise HTTPException(400)
    if not owner_actions.set_hit_state([hit_id], state):     # as on the web: taste, push, photos, Telegram message
        raise HTTPException(404)
    return _back(f"/tg/hits/{hit_id}")


@router.get("/tg/ad/{ref:path}")
async def tg_ad(ref: str):
    """A push or message button about an ad (its ref, "<platform>:<id>" for other sites): its newest hit."""
    platform, _, ad_id = ref.rpartition(":")
    with session() as db:
        h = db.exec(select(Hit).where(Hit.ad_id == ad_id, Hit.platform == (platform or "willhaben"))
                    .order_by(col(Hit.id).desc())).first()
    return _back(f"/tg/hits/{h.id}" if h else "/tg/hits")


# ------------------------------------------------------------------ chats
@router.get("/tg/chats", response_class=HTMLResponse)
async def tg_chats(request: Request, show: str = "", role: str = ""):
    from app.services import chats
    with session() as db:
        o = chats.overview(db)
    convs = o["shown"]
    if role in ("seller", "buyer"):
        convs = [c for c in convs if c.role == role]
    if show == "action":
        convs = [c for c in convs if c.id in o["waiting"] or c.id in o["open_drafts"]]
    elif show == "unread":
        convs = [c for c in convs if o["unread"].get(c.id)]
    elif show == "finished":
        convs = [c for c in convs if c.id in o["finished"]]
    elif show != "all":                  # the default: the open chats, the finished ones behind their own filter
        convs = [c for c in convs if c.id not in o["finished"]]
    return _render(request, "chats.html", convs=convs, o=o, show=show, role=role)


@router.get("/tg/chats/{conversation_id}", response_class=HTMLResponse)
async def tg_chat(request: Request, conversation_id: str):
    from app.services import chats, selling, translate
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404)
    translate.schedule(conversation_id)          # foreign messages: the 8-s reload of the thread brings the translation
    chats.seen_in_app(conversation_id)
    _later(chats.ask_presence(conv))
    drafts = chats.drafts_of(conversation_id)
    with session() as db:
        o = chats.overview(db)
    open_draft = next((d for d in drafts if d.status == "draft"), None)
    from app.services import telegram
    return _render(request, "chat.html", conv=conv, messages=chats.messages_of(conversation_id),
                   draft=open_draft, draft_sendable=telegram.can_send(open_draft) if open_draft else True, translating=bool(open_draft and open_draft.lang is None and translate.on()),
                   sending=next((d for d in drafts if d.status == "sending"), None),
                   presence=chats.presence_of(conv), place=chats.partner_place(conv),
                   finished=conv.id in o["finished"], notes=_notes(conv, drafts),
                   contact_warn=selling.contact_hits(draft.text) if (draft := next(
                       (d for d in drafts if d.status == "draft"), None)) else [])


@router.get("/tg/chats/{conversation_id}/info", response_class=HTMLResponse)
async def tg_chat_info(request: Request, conversation_id: str, sort: str | None = None):
    """Everything beside the thread: the ad, this buyer, the purchase order, PayLivery, shipping, the calendar."""
    from app import main
    return await _page(request, "chat_info.html", main.chat_page, conversation_id, msg=None, sort=sort,
                       show_hidden=False)


@router.post("/tg/chats/{conversation_id}/send")
async def tg_chat_send(conversation_id: str, text: str = Form(""), draft_id: str = Form("")):
    """The composer's main button: the open draft as edited, or my own text when there is none – right away."""
    from app.services import chats
    text = text.strip()
    if not text:
        return _back(f"/tg/chats/{conversation_id}")
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(404)
        d = db.get(Draft, int(draft_id)) if draft_id.isdigit() else None
        if d is None or d.conversation_id != conversation_id or d.status != "draft":
            d = Draft(conversation_id=conversation_id, text=text, model="manual")
            db.add(d)
            db.commit()
            db.refresh(d)
    chats.enqueue_send(d.id, text, now=True)
    return _back(f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/draft")
async def tg_chat_draft(conversation_id: str, hint: str = Form("")):
    from app.services import chats
    chats.enqueue_draft(conversation_id, hint.strip() or None)
    return _back(f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/revise")
async def tg_chat_revise(conversation_id: str, draft_id: int = Form(...), instruction: str = Form("")):
    """One of Claude's other decisions under the draft (a chip): the draft is rewritten in the background."""
    from app.services import chats
    chats.enqueue_revise(draft_id, instruction)
    return _back(f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/discard")
async def tg_chat_discard(conversation_id: str, draft_id: int = Form(...)):
    from app.services import owner_actions
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is not None and d.conversation_id == conversation_id and d.status == "draft":
        owner_actions.discard_draft(draft_id)     # dismissed for good: the next sync writes no new answer for it
    return _back(f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/finished")
async def tg_chat_finished(conversation_id: str, finished: str = Form("1")):
    from app.services import chats
    chats.set_finished(conversation_id, finished == "1")
    return _back(f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/unread")
async def tg_chat_unread(conversation_id: str):
    from app.services import chats
    chats.mark_unread(conversation_id)
    return _back("/tg/chats")


@router.post("/tg/chats/{conversation_id}/hide")
async def tg_chat_hide(conversation_id: str, hidden: str = Form("1")):
    from app.services import chats
    chats.hide(conversation_id, hidden == "1")
    return _back("/tg/chats" if hidden == "1" else f"/tg/chats/{conversation_id}")


@router.post("/tg/chats/{conversation_id}/mark-sent")
async def tg_chat_mark_sent(conversation_id: str, draft_id: int = Form(...), text: str = Form("")):
    """Sent by hand elsewhere (willhaben's own app): stored as sent, nothing goes out from here."""
    from app.services import chats
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is not None and d.conversation_id == conversation_id and d.status == "draft":
        chats.mark_sent(draft_id, text.strip() or d.text)
    return _back(f"/tg/chats/{conversation_id}")


# ------------------------------------------------------------------ my ads, purchases, agents
@router.get("/tg/myads", response_class=HTMLResponse)
async def tg_myads(request: Request, show: str = "active", hidden: bool = False):
    """My ads as the dashboard lists them, one group at a time (active, deactivated, sold / reserved)."""
    from app import main
    return await _page(request, "myads.html", main.myads_page, msg=None, show_hidden=hidden, show_sold=show == "sold",
                       show_inactive=show == "inactive", show_active=show == "active")


@router.get("/tg/myads/cleanup", response_class=HTMLResponse)
async def tg_myads_cleanup(request: Request):
    """Old ads to delete on willhaben (no photos, sold, the old copy of a republished ad)."""
    from app import main
    return await _page(request, "myads_cleanup.html", main.myads_cleanup_page)


@router.get("/tg/myads/{ad_id}", response_class=HTMLResponse)
async def tg_myad(request: Request, ad_id: str):
    with session() as db:
        a = db.get(MyAd, ad_id)
    if a is None:
        raise HTTPException(404)
    return _render(request, "myad.html", a=a)


@router.get("/tg/myads/{ad_id}/edit", response_class=HTMLResponse)
async def tg_myad_edit(request: Request, ad_id: str):
    from app import main
    return await _page(request, "myad_edit.html", main.myads_edit_page, ad_id, msg=None)


@router.get("/tg/sold/{ad_id}", response_class=HTMLResponse)
async def tg_sold(request: Request, ad_id: str, kind: str = "sold", buyer: str | None = None):
    from app import main
    return await _page(request, "sold.html", main.sold_page, ad_id, kind=kind, buyer=buyer or None, msg=None)


def tg_link(url: str) -> str:
    """The Mini App page for a link to an app page (a Telegram button opens that instead of the dashboard)."""
    from urllib.parse import parse_qs, urlsplit

    from app.config import get_settings
    base = (get_settings().public_url or "").rstrip("/")
    if not base or not url.startswith(base):
        return url
    parts = urlsplit(url[len(base):] or "/")
    path, q = parts.path, parse_qs(parts.query)
    if path.startswith("/chats/"):
        return base + "/tg" + path
    if path == "/hits" and q.get("view"):
        return f"{base}/tg/ad/{q['view'][0]}"
    if path == "/hits":
        return base + "/tg/hits"
    if path.startswith("/myads"):
        return base + "/tg/myads"
    if path.startswith("/purchases"):
        return base + "/tg/purchases"
    if path in ("", "/"):
        return base + "/tg"
    return url


def _notes(conv: Conversation, drafts: list[Draft]) -> list[str]:
    """What the open draft asks the owner to check before sending (Claude's needs_info and notes), as on the chat page."""
    d = next((x for x in drafts if x.status == "draft"), None)
    if d is None:
        return []
    out = [n[8:].strip() if n.startswith("Hinweis:") else n for n in (d.needs_info or []) if n]
    if d.error:
        out.insert(0, d.error)
    return out


def menu_url() -> str | None:
    """The Mini App's address for the bot's menu button: only an https public URL (Telegram opens nothing else)."""
    from app.config import get_settings
    base = (get_settings().public_url or "").rstrip("/")
    return base + "/tg" if base.startswith("https://") else None


def label_of(status: str) -> str:
    return {"active": t("online"), "reserved": t("reserved"), "sold": t("sold"), "expired": t("expired")}.get(status, status)
