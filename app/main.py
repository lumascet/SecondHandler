"""FastAPI app: HTMX dashboard + JSON API (used by the MCP server)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import and_, or_
from sqlmodel import col, func, select
from starlette.middleware.base import BaseHTTPMiddleware

from app import AUTHOR, __version__, auth, i18n, scheduler
from app.i18n import t
from app.config import address_check, get_settings
from app.db import platform_per_hour as _platform_per_hour  # noqa: E402,I001  (willhaben's budget: own or all platforms')
from app.db import (
    SETTING_PUBLIC_URL, AdBatch, AdDraft, Conversation, Draft, EventLog, Hit, MyAd, Purchase, SearchAgent, SimChat, apply_public_url,
    current_ad, get_setting, init_db, recent_events, requests_per_hour, session, set_setting, utcnow,
)
from app.llm import usage as llm_usage
from app.llm import models as llm_models
from app.llm.base import PURPOSES, LLMError, get_backend, model_for
from app.llm.claude_login import claude_token, forget_token, get_login, token_source
from app.services import access_stats, ad_batch, ai_rules, ad_creator, chat_sim, chats, my_ads, screening, sold
from app.services import purchase as purchase_svc
from app.services import gcal, geizhals, ha_auth, label_fill, meetups, quiet_hours, shipping, telegram
from app.services import adaptive_pace, geo, health, human_pace, jobs, price_zones, site_accounts
from app.services import credentials as credentials_svc
from app.services import deals as deals_svc
from app.services import min_price as min_price_svc
from app.services import scam, selling
from app.services import style as style_svc
from app.services import stock, system_prompts
from app.services.notify import PUSH_CHANNELS, notify
from app import backup_routes, label_buy_routes, paylivery_routes, platforms, printing_routes, purchase_routes
from app import agent_api_routes, agent_settings_routes, mcp_http, settings_api_routes, style_routes
from app.services import search_agents as search_agents_svc
from app.services.search_agents import refile_screened
from app.platforms import vinted_inbox as vinted_inbox_mod
from app.willhaben import access_log, record_all
from app.willhaben import insert as _wh_insert
from app.willhaben.account import EndpointUnknown, SendFailed
from app.platforms import PlatformError, ad_ref, vpn
from app.willhaben.parse import InvalidSearchUrl, to_webapi_url, url_param, with_params
from app.willhaben.public import WillhabenError, get_public, willhaben_limiter
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session

log = logging.getLogger("secondhandler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
VIENNA = ZoneInfo("Europe/Vienna")
BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    init_db()
    from app.services import timeline as _timeline
    _timeline.mark_interrupted()                   # runs cut off by the last restart
    apply_public_url()                             # Settings → Web address wins over PUBLIC_URL in .env
    refile_screened()                              # once: screened-out hits leave "new" for "filtered"
    search_agents_svc.refile_negotiable()          # once: Kleinanzeigen "VB" hits leave "new" for "filtered"
    from app.llm import usage as _llm_usage
    _llm_usage.reprice_calls()                     # once: stored Claude costs from the tokens and the API prices
    ad_batch.recover()
    my_ads.recover()                               # queued jobs died with the last process: unmark what they held
    ad_creator.recover_publishing()
    chats.recover_sending()
    chat_sim.recover()
    deals_web.product_search.recover()
    purchase_svc.recover()
    try:
        paylivery_routes.paylivery.recheck_all()   # chats on a stage that older notice rules misread
    except Exception as e:  # noqa: BLE001 - the chat sync rechecks them too
        log.warning("PayLivery recheck failed: %s", e)
    vpn.secure()                                   # WireGuard configs owner-only (also after a backup restore)
    if s.browser_enabled or s.wh_replay:            # WH_REPLAY without a browser: the login comes from recordings
        sess = get_session()
        await sess.start()
        if sess.started:
            try:
                await sess.check_login()
            except Exception as e:  # noqa: BLE001
                log.warning("initial login check failed: %s", e)
    scheduler.start()
    from app.services import ha_actions
    tasks = [asyncio.create_task(ha_actions.listen_forever())] if s.ha_listener else []  # HA push buttons
    if s.telegram_listener:                         # Telegram buttons and replies (long polling)
        tasks.append(asyncio.create_task(telegram.listen_forever()))
    sess = get_session()
    if s.chat_live and sess.started:               # new chat messages arrive without polling
        try:
            await sess.start_chat_listener(chats.on_live_event)
            tasks.append(asyncio.create_task(sess.chat_watchdog()))
        except Exception as e:  # noqa: BLE001 - polling still works
            log.warning("live chat not started: %s", e)
    if sess.started and platforms.enabled("vinted"):   # Vinted's push stream, through its own inbox tab
        from app.platforms import vinted_inbox
        try:
            if await vinted_inbox.start_live():
                await vinted_inbox.sync(force=True)       # what came while the app was down
        except Exception as e:  # noqa: BLE001 - the chat list's Sync still works
            log.warning("vinted live not started: %s", e)
        tasks.append(asyncio.create_task(vinted_inbox.live_watchdog()))
    app.state.listener_tasks = tasks             # Settings → Backup stops them before a restore
    async with mcp_http.running():               # the MCP server at /mcp lives as long as the app
        yield
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    scheduler.shutdown()
    try:
        await vpn_check.shutdown()                # a running VPN search, before its clients and tunnels close
    except Exception as e:  # noqa: BLE001 - the cleanup below must run
        log.warning("VPN search at shutdown: %s", e)
    try:
        await get_session().stop()
        await get_public().aclose()
        await platforms.aclose_all()              # the other sites' clients
    finally:
        await vpn.shutdown()                      # the wireproxy processes of the VPN
        from app.willhaben import recorder
        await recorder.drain()                    # willhaben recordings still being written
        access_log.flush()                        # last unwritten willhaben access counts


_background: set[asyncio.Task] = set()
app = FastAPI(title="SecondHandler", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(backup_routes.router)   # Settings → Backup
app.include_router(label_buy_routes.router)   # Settings → Shipping → payment card, checkout
app.include_router(printing_routes.router)    # Settings → Printers, print buttons
app.include_router(paylivery_routes.router)   # PayLivery card on the chat page
app.include_router(purchase_routes.router)    # Kaufauftrag: buy dialog, order pages, order buttons, Settings → Buying
app.include_router(style_routes.router)       # Settings → My rules → Writing style
app.include_router(agent_api_routes.router)   # AI agent: /api/events, /api/purchases, /api/sales (docs/agent-api.md)
app.include_router(agent_settings_routes.router)   # Settings → AI agent: token, webhook
app.include_router(settings_api_routes.router)   # AI agent: /api/settings (read and change settings)
mcp_http.install(app)                           # AI agent: the MCP server over HTTP at /mcp (app/mcp_http.py)
paylivery_routes.paylivery.on_stage(purchase_svc.on_paylivery_stage)      # purchase orders follow PayLivery stages
paylivery_routes.paylivery.on_action(purchase_svc.on_paylivery_action)
from app.services import paylivery_advice  # noqa: E402
paylivery_routes.paylivery.on_stage(paylivery_advice.on_stage)          # an offer came in: a draft with its step
paylivery_routes.paylivery.on_stage(sold.on_paylivery_stage)            # a paid piece counts; pieces left: online again
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class DashboardAuth(BaseHTTPMiddleware):
    """Login cookie for browsers (see /login), HTTP Basic auth still accepted for scripts and the MCP server. A
    state-changing request another site's page sends is refused first (auth.cross_site), with or without a password."""
    async def dispatch(self, request: Request, call_next):
        if auth.cross_site(request.method, request.headers):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if not auth.login_needed(request.headers) or request.url.path in auth.PUBLIC_PATHS \
                or request.url.path.startswith(auth.PUBLIC_PREFIXES):
            return await call_next(request)
        mcp_path = request.url.path == "/mcp" or request.url.path.startswith("/mcp/")
        agent_path = mcp_path or request.url.path.startswith("/api/")
        if auth.valid_token(request.cookies.get(auth.COOKIE)) or auth.valid_basic(request.headers.get("authorization", "")) \
                or (agent_path and auth.valid_bearer(request.headers.get("authorization", ""))):
            return await call_next(request)           # the AI agent's bearer token opens the JSON API and /mcp only
        if auth.debug_login(request.headers, request.client.host if request.client else None):
            log.info("debug login: %s %s", request.method, request.url.path)
            return await call_next(request)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        login = f"/login?next={quote(target, safe='')}"
        if mcp_path:                                                # an MCP client: 401 JSON, never a redirect to the login page
            return JSONResponse({"detail": "login required"}, status_code=401)
        if request.headers.get("hx-request"):                       # htmx fragment: reload the whole page to the login
            return Response(status_code=401, headers={"HX-Redirect": login})
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(login, status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)   # no WWW-Authenticate: no browser popup


app.add_middleware(DashboardAuth)


@app.middleware("http")
async def tg_frames(request: Request, call_next):
    """The Mini App's pages may be framed only by Telegram's web client (and the app itself)."""
    response = await call_next(request)
    if request.url.path.startswith("/tg"):
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://*.telegram.org"
    return response


LANG_COOKIE = "wh_lang"      # the language picked on the login page, before anyone is logged in (this browser only)


@app.middleware("http")
async def ui_language(request: Request, call_next):
    pref = request.cookies.get(LANG_COOKIE) if request.cookies.get(LANG_COOKIE) in i18n.LANGS else i18n.preference()
    code = pref if pref in i18n.LANGS else i18n.from_accept_language(request.headers.get("accept-language"))
    if pref not in i18n.LANGS and request.headers.get("accept-language") and get_setting("ui_lang_seen") != code:
        set_setting("ui_lang_seen", code)
    i18n.set_request_lang(code)
    return await call_next(request)


# ------------------------------------------------------------------ template helpers
def _local(dt: datetime | str | None, fmt: str = "%d.%m. %H:%M") -> str:
    if isinstance(dt, str):          # ISO times kept in JSON columns (e.g. the chat simulator's runs)
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if not dt:
        return "–"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VIENNA).strftime(fmt)


def _ago(dt: datetime | str | None) -> str:
    if isinstance(dt, str):              # an ISO time kept in JSON (a stored analysis)
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return "–"
    if not dt:
        return "–"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (utcnow() - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 90:
        txt = t("{n} s", n=f"{secs:.0f}")
    elif secs < 5400:
        txt = t("{n} min", n=f"{secs / 60:.0f}")
    elif secs < 172800:
        txt = t("{n} h", n=f"{secs / 3600:.0f}")
    else:
        txt = t("{n} days", n=f"{secs / 86400:.0f}")
    return t("in {t}", t=txt) if future else t("{t} ago", t=txt)


def _eur(v: float | None) -> str:
    if v is None:
        return "–"
    return f"€ {v:,.0f}".replace(",", ".") if v == int(v) else f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_KEEP_TOGETHER = re.compile(r"(?<=€) (?=\d)|(?<=\d) (?=%)|(?<=\d)–(?=[\d€])")


def _nb(s: Any) -> Any:
    """Running text whose amounts, shares and ranges never break across lines ("€ 101,15", "79 %", "50–79"): their
    space becomes a no-break space, a dash between them gets a word joiner. The rest of the text still wraps."""
    if not isinstance(s, str):
        return s
    out = _KEEP_TOGETHER.sub(lambda m: "\u00a0" if m.group() == " " else "\u2013\u2060", s)
    return Markup(out) if isinstance(s, Markup) else out


templates.env.filters.update(local=_local, ago=_ago, eur=_eur, nb=_nb)
from app.services.price_gauge import gauge as _price_gauge  # noqa: E402
from app.services.deals import level_label as _level_label  # noqa: E402

templates.env.globals.update(price_gauge=_price_gauge, deal_level_label=_level_label)
templates.env.globals.update(geizhals_levels=search_agents_svc.level_choices)
templates.env.globals.update(hit_can_sync=lambda h: _can_sync(h))     # the hit cards' sync button
from app.platforms import shipping_info as _shipping_info  # noqa: E402
from app.services import translate as _translate_svc  # noqa: E402
from app.services import taste as _taste_svc  # noqa: E402
from app.services import vinted_offers as _vinted_offers_svc  # noqa: E402
from app.platforms import fees as _fees  # noqa: E402
templates.env.globals.update(buyer_fee_line=_fees.ad_fee)   # the buyer fee in the ad viewer's price breakdown
templates.env.globals.update(shipping_label=_shipping_info.label_of)
templates.env.globals.update(vinted_offer_card=_vinted_offers_svc.card)   # a Vinted chat's price steps
templates.env.globals.update(taste_counts=_taste_svc.counts)   # the agent's saved / discarded hits (swipes)
templates.env.globals.update(ad_foreign=lambda text: _translate_svc.on() and _translate_svc.foreign(text))   # the viewer's translation    # ui.ship(): shipping next to every price


def _site_can_detail(pid: str) -> bool:
    try:
        return platforms.get(pid).can("detail")
    except platforms.UnknownPlatform:
        return False


templates.env.globals.update(site_can_detail=_site_can_detail)          # ui.ship_lazy()   # the agent drawer's Geizhals rule
templates.env.globals.update(deal_label=screening.deal_label, screen_labels=screening.verdict_labels,
                             screen_issue_labels=screening.issue_labels, screen_hideable=screening.HIDEABLE,
                             screen_hide_set=screening.hide_set, screen_default_hide=screening.DEFAULT_HIDE)   # Claude verdict on hit cards
templates.env.globals.update(scam_warnings=scam.warnings, scam_for_viewer=scam.for_viewer)   # scam check badges
templates.env.globals.update(paylivery_card=paylivery_routes.card)   # _paylivery.html on the chat page
templates.env.globals.update(record_all_stats=lambda: {**record_all.stats(), "on": record_all.on()})   # session page
templates.env.globals.update(site_logins=lambda: [(s.name, {**site_accounts.state(s.id), "checked_at": site_accounts.checked_at(s.id)})
                                                  for s in site_accounts.SITES.values() if platforms.enabled(s.id)])   # session page
templates.env.globals.update(vinted_live=lambda: vinted_inbox_mod.live)   # the chat list's Vinted line
templates.env.globals.update(presence_of=lambda conv: chats.presence_of(conv),    # the open chat: their online state
                             partner_place=lambda conv: chats.partner_place(conv),     # and where they are from
                             partner_trust=lambda conv: chats.partner_trust(conv))     # their account's age, ratings


def _seller_trust(since, ratings, platform=None, reply=None) -> dict:
    from app.services import trust
    return {"flags": trust.flags(since, ratings, platform), "facts": trust.facts(since, ratings, reply)}


templates.env.globals.update(seller_trust=_seller_trust)      # the ad viewer's seller line (_seller_trust.html)
templates.env.globals.update(credentials_state=credentials_svc.stored)
templates.env.globals.update(chat_for_ad=chats.chat_for_ad, is_own_ad=chats.is_own_ad,    # the ad pane's chat button
                             contact_questions=chats.contact_questions)   # login details card (services/credentials.py)
templates.env.globals.update(login_sites=lambda: [("willhaben", "willhaben")] + [(s.id, s.name) for s in site_accounts.SITES.values()])
templates.env.globals.update(paylivery_step=lambda d, conv: paylivery_advice.view(d, conv))   # the draft's PayLivery step
templates.env.globals.update(intent_label=chats.intent_label, ad_status_label=chats.ad_status_label)        # the draft's header on the chat page
templates.env.globals.update(style_panel=style_svc.panel)   # Settings → My rules → Writing style
templates.env.globals.update(purchase_card=purchase_svc.card, purchase_enabled=purchase_routes.enabled,   # Kaufauftrag
                             purchase_settings=purchase_routes.settings_values, purchase_notice=purchase_routes.notice)
templates.env.globals.update(geizhals_sheet=geizhals.find_spec_block)   # the spec sheet in a description (myad_edit.html)
templates.env.globals.update(paylivery_offers=ad_creator.paylivery_offers, paylivery_pick=_wh_insert.preselect_offer,
                             parcel_sizes=_wh_insert.PARCEL_SIZES, parcel_carriers=_wh_insert.PAYLIVERY_CARRIERS,   # batch table
                             sell_handover_default=selling.handover)   # _wh_handover.html (preset: Selling preferences)
templates.env.globals.update(_=t, ui_lang=i18n.lang, ui_langs=i18n.LANGS, ui_lang_pref=i18n.preference,
                             app_version=__version__, app_author=AUTHOR, auth_enabled=auth.enabled, public_host=auth.public_host, wh_limiter=willhaben_limiter,
                             wh_budget=lambda: _platform_per_hour("willhaben"), wh_popup=access_stats.popup,
                             wh_popups=access_stats.popups,
                             bg_jobs=jobs.snapshot, bg_working=jobs.working,   # background jobs (header, spinners)
                             live_cursor=lambda: _live_cursor())                 # the newest event (notices start there)


def _asset_version() -> str:
    """Cache key for /static/app.css: changes whenever the stylesheet is rebuilt."""
    try:
        return str(int((BASE_DIR / "static" / "app.css").stat().st_mtime))
    except OSError:
        return __version__


templates.env.globals.update(asset_version=_asset_version)


def _site_usage() -> list[dict[str, Any]]:
    """The other sites' last hour for the sidebar meter and its popover (access_stats.site_usage): every switched-on
    site, and a switched-off one that still has requests in the hour."""
    return [{**u, "used": u["n"], "pct": 100 if u["cooling_down_s"] else u["pct"],
             "cooling_min": int(-(-u["cooling_down_s"] // 60)), "href": f"/stats/requests?platform={u['id']}"}
            for u in access_stats.site_usage() if u["id"] != "willhaben" and (u["enabled"] or u["n"])]


templates.env.globals.update(site_usage=_site_usage)


def _platform_name(pid: str | None) -> str:
    try:
        return platforms.get(pid or "willhaben").name
    except Exception:  # noqa: BLE001 - a row of a platform that no longer exists
        return pid or "willhaben"


# platforms (app/platforms): refs in links, names on cards, whether more than one site is watched
from app.services.search_agents import agent_platforms as _agent_platforms  # noqa: E402
from app.services.search_agents import agent_search_items as _agent_search_items  # noqa: E402
from app.services.search_agents import geizhals_below_pct as _geizhals_below_pct  # noqa: E402

templates.env.globals.update(agent_sites=_agent_platforms, agent_search_items=_agent_search_items,
                             geizhals_below_pct=_geizhals_below_pct,
                             held_back_reasons=search_agents_svc.held_back_reasons)   # Filtered out: why
from app.services import geo as _geo  # noqa: E402

templates.env.globals.update(home_point=_geo.home_cached, home_address=_geo.home_address,   # distance from home to ads
                             result_km=_geo.distance_km, hit_km=_geo.hit_km)
from app.services.product_search import discount as _ps_discount  # noqa: E402
from app.services.product_search import geizhals_passes as _ps_gh_passes  # noqa: E402
from app.services.product_search import red_flags as _red_flags  # noqa: E402

templates.env.globals.update(ps_red_flags=_red_flags, ps_discount=_ps_discount, ps_gh_passes=_ps_gh_passes)   # older stored results: without "shipping costs unclear"
templates.env.globals.update(ad_ref=ad_ref, platform_name=_platform_name, platforms_multi=platforms.multi,
                             platforms_all=platforms.all, platform_get=platforms.get, platform_enabled=platforms.enabled,
                             vpn_health=vpn.health)


def _inbox_seen_at() -> datetime:
    raw = get_setting("inbox_seen_at")
    try:
        return datetime.fromisoformat(raw) if raw else datetime(2000, 1, 1, tzinfo=timezone.utc)
    except ValueError:
        return datetime(2000, 1, 1, tzinfo=timezone.utc)


def _inbox_events(limit: int = 200) -> list[EventLog]:
    """The inbox's events: newer than its last "Clear", not quiet, and not about a hit or a chat already seen in the
    app (the hit opened or handled, the chat opened since) – those leave it by themselves. Newest first."""
    seen = _inbox_seen_at()
    with session() as db:
        events = list(db.exec(select(EventLog).where(EventLog.ts > seen, col(EventLog.quiet).is_not(True))
                              .order_by(col(EventLog.ts).desc()).limit(limit)))
        refs = {e.ad_id for e in events if e.ad_id}
        handled: set[str] = set()
        if refs:
            plain = {r.split(":", 1)[-1] for r in refs}
            for platform, ad_id in db.exec(select(Hit.platform, Hit.ad_id).where(col(Hit.ad_id).in_(plain),
                                                                                 Hit.state != "new")):
                handled |= {ad_id, f"{platform or 'willhaben'}:{ad_id}"}
        cids = {e.conversation_id for e in events if e.conversation_id}
        opened = {c.id: c.app_seen_at for c in db.exec(select(Conversation).where(col(Conversation.id).in_(cids)))} if cids else {}

    def done(e: EventLog) -> bool:
        if e.ad_id and e.ad_id in handled and e.kind in ("hit", "price_drop", "saved"):
            return True
        at = opened.get(e.conversation_id or "")
        ts = e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=timezone.utc)
        return bool(at) and (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= ts
    return [e for e in events if not done(e)]


def _nav_counts() -> dict[str, int]:
    """Badges in the sidebar: new hits, unread messages and chats that need you (chats.badge_counts), purchases waiting for your payment,
    and notifications since the inbox was last opened (those sent without a push left out)."""
    with session() as db:
        return {"inbox": len(_inbox_events()),
                "hits": HitView.of("new").count(db),        # as the list shows them: far pickup-only ads left out
                **chats.badge_counts(db),
                "purchases": db.exec(select(func.count()).select_from(Purchase).where(Purchase.status == "pay_now")).one()}


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("session_status", get_session().status())
    if not ctx.get("embed"):
        ctx.setdefault("nav_counts", _nav_counts())
        lim = llm_usage.limit()
        if lim["limit"]:       # the Claude meter + popover in the sidebar (monthly limit from Settings → Claude)
            ctx.setdefault("claude_usage", {**lim, "by_task": llm_usage.month_by_task(), "by_model": llm_usage.month_by_model(),
                                            "cost_by_model": llm_usage.month_cost_by_model()})
        ctx.setdefault("inbox_items", _inbox_items())
        ctx.setdefault("inbox_unread", ctx["nav_counts"].get("inbox", 0))
        if auth.enabled():
            ctx.setdefault("current_user", get_settings().dashboard_user)
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ login
def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if not auth.login_needed(request.headers) or auth.valid_token(request.cookies.get(auth.COOKIE)):
        return back(auth.safe_next(next))
    return templates.TemplateResponse(request, "login.html", {"next": auth.safe_next(next), "error": None,
                                                              "user": get_settings().dashboard_user,
                                                              "password_set": auth.enabled()})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       remember: str | None = Form(None), next: str = Form("/")):
    client = request.client.host if request.client else "?"
    target = auth.safe_next(next)
    if auth.locked_out(client):
        error = t("Too many failed attempts. Try again in 15 minutes.")
    elif auth.check_password(username.strip(), password):
        r = back(target)
        r.set_cookie(auth.COOKIE, auth.make_token(bool(remember)), max_age=auth.REMEMBER_S if remember else None,
                     httponly=True, samesite="lax", secure=_https(request))
        return r
    else:
        auth.record_failure(client)
        await asyncio.sleep(1)                     # slows down guessing
        error = t("Wrong user name or password.")
    return templates.TemplateResponse(request, "login.html", {"next": target, "error": error, "user": username,
                                                              "password_set": auth.enabled()}, status_code=401)


@app.post("/login/telegram")
async def login_telegram(request: Request, init_data: str = Form(""), next: str = Form("/")):
    """The dashboard opened from a Telegram button (a Mini App: telegram.open_button) logs in with the launch data
    Telegram signed (auth.telegram_user), for the linked chat's user only: a private chat's id is its user's id."""
    from app.services import telegram

    client = request.client.host if request.client else "?"
    linked = telegram.chat_id()
    if auth.locked_out(client) or linked is None or auth.telegram_user(init_data, telegram.token()) != linked:
        auth.record_failure(client)
        await asyncio.sleep(1)
        return JSONResponse({"ok": False}, status_code=401)
    r = JSONResponse({"ok": True, "next": auth.safe_next(next)})
    # Telegram Web shows the Mini App in its own iframe (another site): only a SameSite=None cookie goes along there.
    # Safe since state-changing cross-site requests are refused (auth.cross_site); over plain http it stays Lax.
    https = _https(request)
    r.set_cookie(auth.COOKIE, auth.make_token(True), max_age=auth.REMEMBER_S, httponly=True,
                 samesite="none" if https else "lax", secure=https)
    return r


@app.post("/logout")
async def logout():
    r = back("/login")
    r.delete_cookie(auth.COOKIE)
    return r


# ------------------------------------------------------------------ dashboard
@app.get("/sw.js")
async def service_worker():
    """The service worker at the root, so its scope is the whole app: the offline page while the app is away."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "browser": get_session().started, "logged_in": get_session().logged_in}


HOME_EVENTS = 8              # activity entries on Home (the inbox in the sidebar has more)
HOME_EXPIRING_DAYS = 5       # "Expiring soon" on Home: own ads that end within these days


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with session() as db:
        stats = {
            "agents": db.exec(select(func.count()).select_from(SearchAgent).where(SearchAgent.enabled)).one(),
            "new_hits": db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one(),
            # as the chat list counts: shown chats only (not hidden, removed or older than the sync window)
            **dict(zip(("unread", "open_drafts"), chats.summary_counts(db), strict=True)),
            # online only (as the Mini App's home): not expired, sold, deleted or hidden from the list
            "myads": db.exec(select(func.count()).select_from(MyAd).where(MyAd.status == "active",
                                                                            col(MyAd.hidden).is_not(True))).one(),
        }
        # expired as the My-ads list counts them: ended within the cutoff (Settings → My ads), not hidden
        cutoff = int(get_setting("myads_expired_days") or 0)
        stats["expired"] = sum(my_ads.is_expired(a, cutoff) for a in db.exec(
            select(MyAd).where(MyAd.status == "expired", col(MyAd.hidden).is_not(True))))
        soon = list(db.exec(select(MyAd).where(MyAd.status == "active", col(MyAd.end_date).is_not(None),
                                               col(MyAd.end_date) <= utcnow() + timedelta(days=HOME_EXPIRING_DAYS))
                            .order_by(MyAd.end_date).limit(5)))
        latest = list(db.exec(select(Hit).where(Hit.state == "new").order_by(col(Hit.first_seen_at).desc()).limit(8)))
    return render(request, "index.html", stats=stats, soon=soon, latest=latest, events=recent_events(HOME_EVENTS),
                  llm_ok=bool(claude_token()), health=health.collect())


# ------------------------------------------------------------------ search agents
@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, error: str | None = None, msg: str | None = None):
    with session() as db:
        agents = list(db.exec(select(SearchAgent).order_by(SearchAgent.created_at)))
        counts = dict(db.exec(select(Hit.agent_id, func.count()).where(Hit.state == "new").group_by(Hit.agent_id)).all())
        # all hits, pictures or not: the template shows a placeholder for a hit without one
        thumbs: dict[int, list[Hit]] = {}
        for h in db.exec(select(Hit).order_by(col(Hit.first_seen_at).desc())):
            row = thumbs.setdefault(h.agent_id, [])
            if len(row) < 4:
                row.append(h)
    return render(request, "agents.html", agents=agents, counts=counts, thumbs=thumbs, error=error, msg=msg,
                  quiet=quiet_hours.state(), quiet_cfg=quiet_hours.config(), checking=set(jobs.current_keys("agent:")),
                  next_run=scheduler.next_run, settings=get_settings(), thresholds=screening.thresholds())


BUILDER_START = "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"


def _num_or_none(v: str | None) -> float | None:
    """A price as typed: "1500", "1.500,50" (German), "1500.50" or "1,500.50"."""
    v = re.sub(r"[€\s]", "", v or "")
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return float(v) if v else None
    except ValueError:
        return None


@app.get("/agents/new", response_class=HTMLResponse)
async def agents_new(request: Request):
    """Search-agent builder for every platform: a pasted search link (the site is recognised), a search term with a
    price range, and for willhaben its own filters, no copied link needed.

    willhaben's state is the browser search URL in `url`; `keyword` and `r_<PARAM>` (range inputs such as
    r_PRICE_TO) are merged into it. Every filter value links to the URL with that value applied.
    """
    qp = request.query_params
    url = (qp.get("url") or "").strip()
    keyword = (qp.get("keyword") or "").strip()
    choices = platforms.all()                                   # switched on, in display order
    picked = {x.strip().lower() for x in qp.getlist("sites") if x.strip()}
    error = None
    p = None
    if url:
        try:
            p = platforms.for_url(url)
        except InvalidSearchUrl as e:
            error, url = str(e), ""
        if p is not None and p.id not in picked and len(picked) <= 1:
            picked = {p.id}                 # the link's site replaces the default tick (willhaben), it is no addition
    if p is None:
        pid = (qp.get("platform") or next((c.id for c in choices if c.id in picked), "")).strip().lower()
        try:
            p = platforms.get(pid or "willhaben")
        except platforms.UnknownPlatform as e:
            raise HTTPException(404) from e
        if not platforms.enabled(p.id):
            error = error or t("{name} is switched off in Settings → Platforms", name=p.name)
            p = platforms.get("willhaben")
    # one agent, several sites: the main one (p: its link or willhaben's filters) and the others with the same term
    others = [c for c in choices if c.id in picked and c.id != p.id and c.can("keyword_search")]
    preview: dict[str, Any] = {"url": None, "rows_found": 0, "ads": [], "name": "", "keyword": keyword,
                               "price_from": _num_or_none(qp.get("price_from")), "price_to": _num_or_none(qp.get("price_to")),
                               "sites": []}
    filters = None
    if p.can("filters"):
        base = url or BUILDER_START
        changes: dict[str, str | None] = {k[2:]: re.sub(r"[^0-9]", "", v) or None
                                          for k, v in qp.items() if k.startswith("r_") and k[2:].isidentifier()}
        if "keyword" in qp:
            changes["keyword"] = keyword or None
        for qk, wk in (("price_from", "PRICE_FROM"), ("price_to", "PRICE_TO")):   # typed for another site first
            if qp.get(qk) and f"r_{wk}" not in qp and not url_param(base, wk):
                changes[wk] = re.sub(r"[^0-9]", "", qp.get(qk) or "") or None
        if changes:
            base = with_params(base, changes)
        url = base
        try:
            filters = await p.filters(base)
        except (PlatformError, InvalidSearchUrl) as e:
            error = error or str(e)
        if filters:
            preview.update(url=filters.url, rows_found=filters.rows_found, ads=filters.ads,
                           name=filters.suggested_name(), keyword=filters.keyword,
                           price_from=_num_or_none(url_param(filters.url, "PRICE_FROM")),
                           price_to=_num_or_none(url_param(filters.url, "PRICE_TO")))
    else:
        spec = None
        try:
            if url:
                spec = p.parse_search_url(url)
            elif keyword:
                spec = p.keyword_search(keyword, preview["price_from"], preview["price_to"])
        except InvalidSearchUrl as e:
            error = error or str(e)
        if spec is not None:
            preview.update(url=spec.url, name=p.describe(spec), keyword=spec.keyword, price_from=spec.price_from,
                           price_to=spec.price_to)
            try:
                page = await p.search(spec, rows=12, cache_s=120)
            except PlatformError as e:
                error = error or str(e)
            else:
                preview.update(rows_found=page.rows_found, ads=page.ads)
    if preview["url"]:
        preview["sites"].append({"p": p, "rows_found": preview["rows_found"], "url": preview["url"], "error": None})
    if others and preview["url"] and preview["keyword"]:
        merged = list(preview["ads"][:6])
        for o in others:
            row: dict[str, Any] = {"p": o, "rows_found": 0, "url": None, "error": None}
            try:
                spec = o.keyword_search(preview["keyword"], preview["price_from"], preview["price_to"])
                row["url"] = spec.url
                page = await o.search(spec, rows=6, cache_s=120)
            except (PlatformError, InvalidSearchUrl) as e:
                row["error"] = str(e)
            else:
                row["rows_found"] = page.rows_found
                merged += page.ads[:6]
            preview["sites"].append(row)
        preview["ads"] = merged
        preview["rows_found"] = sum(r["rows_found"] for r in preview["sites"])
    return render(request, "agent_new.html", f=filters, url=url, preview=preview, platform=p,
                  platform_choices=choices, chosen=[p.id] + [o.id for o in others], builder_error=error, url_param=url_param,
                  platform_off=[x for x in platforms.all(enabled_only=False) if not platforms.enabled(x.id)
                                and (x.can("search_url") or x.can("keyword_search"))],
                  settings=get_settings(), thresholds=screening.thresholds())


@app.post("/agents")
async def agents_create(url: str = Form(""), platform: str = Form("willhaben"), keyword: str = Form(""),
                        price_from: str = Form(""), price_to: str = Form(""), name: str = Form(""),
                        interval_min: int = Form(5), screen_on: str | None = Form(None), screen_brief: str = Form(""),
                        screen_min: str = Form("match"), screen_hide: list[str] = Form([]), sites: list[str] = Form([])):
    try:
        agent = _create_agent(url, platform, keyword, price_from, price_to, name or None, interval_min * 60, sites)
    except InvalidSearchUrl as e:
        return back(f"/agents?error={quote(str(e), safe='')}")
    if screen_on:
        agent = _set_screening(agent.id, True, screen_brief, screen_min, screen_hide)
    scheduler.schedule_agent(agent)
    return back("/agents")


def _create_agent(url: str, platform: str, keyword: str, price_from: Any, price_to: Any, name: str | None,
                  interval_s: int | None, sites: list[str] | None = None) -> SearchAgent:
    """One agent from a pasted link of any site and/or a search term, on one site or several (`sites`): the link is
    the main search, every other chosen site gets its search term and price range. Raises InvalidSearchUrl."""
    from app.services.search_agents import create_agent_on_sites

    url, keyword = (url or "").strip(), (keyword or "").strip()
    pf = price_from if isinstance(price_from, (int, float)) else _num_or_none(price_from)
    pt = price_to if isinstance(price_to, (int, float)) else _num_or_none(price_to)
    specs: list[Any] = []
    if url:
        base = platforms.for_url(url).parse_search_url(url)
        specs.append(base)
        keyword = keyword or (base.keyword or "")
        pf = base.price_from if pf is None else pf
        pt = base.price_to if pt is None else pt
    wanted = list(dict.fromkeys(x.strip().lower() for x in sites or [] if x and x.strip()))
    if not wanted and not url:
        wanted = [(platform or "willhaben").strip().lower()]
    for pid in wanted:
        if specs and pid == specs[0].platform:
            continue
        try:
            p = platforms.get(pid)
        except platforms.UnknownPlatform as e:
            raise InvalidSearchUrl(t("Unknown platform: {id}", id=pid)) from e
        if not platforms.enabled(p.id):
            raise InvalidSearchUrl(t("{name} is switched off in Settings → Platforms", name=p.name))
        if not p.can("keyword_search"):
            raise InvalidSearchUrl(t("{name} has no keyword search.", name=p.name))
        if not keyword:
            raise InvalidSearchUrl(t("Give a search term for {name} (the link has none).", name=p.name) if url
                                   else t("Give a search link or a search term."))
        specs.append(p.keyword_search(keyword, pf, pt))
    if not specs:
        raise InvalidSearchUrl(t("Give a search link or a search term."))
    return create_agent_on_sites(specs, name, interval_s)


@app.post("/agents/{agent_id}/run")
async def agents_run(agent_id: int):
    """"Check now": the check runs as a background job (lane agents); the card shows it and refreshes itself when it
    is done (the hits page and the pushes tell what it found)."""
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        last = a.last_run_at if a else None
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last and (utcnow() - last).total_seconds() < 60:   # no hammering willhaben via the button
        return back(f"/agents?msg={quote(t('Checked less than a minute ago – the next check waits a moment'), safe='')}")
    search_agents_svc.enqueue_run(agent_id)
    return back("/agents")


@app.post("/agents/bulk")
async def agents_bulk(request: Request):
    """Several agents at once (the agents page's selection): each field left empty stays as it is per agent."""
    from app.services import search_agents

    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    flag = lambda k: {"1": True, "0": False}.get(str(form.get(k) or ""))   # noqa: E731 - None: unchanged
    enabled, notify_on, drops, screen = flag("enabled"), flag("notify"), flag("drops"), flag("screen")
    minutes = str(form.get("interval_min") or "").strip()
    km_mode, km = str(form.get("km_mode") or ""), _num_or_none(str(form.get("max_km") or ""))
    changed = []
    with session() as db:
        for a in db.exec(select(SearchAgent).where(col(SearchAgent.id).in_(ids))):
            if enabled is not None:
                a.enabled = enabled
            if notify_on is not None:
                a.notify = notify_on
            if drops is not None:
                a.notify_price_drops = drops
            if screen is not None:
                a.screen = screen
                a.geizhals_check = a.geizhals_check and screen
            if minutes.isdigit() and int(minutes) > 0:
                a.interval_s = search_agents.agent_interval(a, int(minutes) * 60)
            if km_mode == "off":
                a.max_km = None
            elif km_mode == "set" and km is not None:
                a.max_km = min(max(int(km), 1), 2000)
            db.add(a)
            changed.append(a)
        db.commit()
        for a in changed:
            db.refresh(a)
    for a in changed:
        scheduler.schedule_agent(a, run_soon=False)
    return back(f"/agents?msg={quote(t('{n} search agents changed', n=len(changed)), safe='')}")


@app.post("/agents/{agent_id}/toggle")
async def agents_toggle(agent_id: int):
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.enabled = not a.enabled
        db.add(a)
        db.commit()
        db.refresh(a)
    scheduler.schedule_agent(a)
    return back("/agents")


@app.post("/agents/{agent_id}/edit")
async def agents_edit(agent_id: int, name: str = Form(...), interval_min: int = Form(...),
                      notify_on: str | None = Form(None), drops_on: str | None = Form(None),
                      screen_on: str | None = Form(None), screen_brief: str = Form(""), screen_min: str = Form("match"),
                      screen_hide: list[str] = Form([]), drop_query: list[int] = Form([]), drop_term: list[str] = Form([]),
                      sites: list[str] = Form([]), sites_shown: str = Form(""), add_keyword: list[str] = Form([]),
                      add_price_from: list[str] = Form([]), add_price_to: list[str] = Form([]),
                      gh_on: str | None = Form(None), gh_pct: str = Form(""), gh_level: str = Form("pct"),
                      gh_check: str | None = Form(None), km_on: str | None = Form(None), max_km: str = Form(""),
                      auto_buy_shown: str = Form(""), auto_buy_on: str | None = Form(None), auto_buy_max: str = Form("")):
    from app.services import search_agents

    # the drawer's searches: terms to add (each added row and the add row send keyword, from and to, so the three
    # lists line up), terms to remove on every site (drop_term), searches without a term to remove (drop_query)
    adds = [(kw, _num_or_none(pf), _num_or_none(pt))
            for kw, pf, pt in zip_longest(add_keyword, add_price_from, add_price_to, fillvalue="") if kw.strip()]
    gone = [x for x in map(search_agents.parse_term_key, drop_term) if x]
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        if drop_query or gone or adds or sites_shown:     # the drawer's searches and sites
            try:       # new searches start quietly: what their first check finds counts as seen
                search_agents.edit_searches(a, drop_query, sites if sites_shown else None,
                                            drop_terms=gone, add_terms=adds)
            except ValueError as e:
                return back(f"/agents?error={quote(str(e), safe='')}")
        a.name = name.strip() or a.name
        a.interval_s = search_agents.agent_interval(a, interval_min * 60)
        a.notify, a.notify_price_drops = bool(notify_on), bool(drops_on)
        pct = _num_or_none(gh_pct)     # the rule: a price zone or cheaper (gh_level), or at least gh_pct % below
        level = gh_level if gh_on and gh_level in search_agents.LEVELS else None
        a.geizhals_level = level
        a.geizhals_pct = min(max(int(pct if pct is not None else 30), 1), 95) if gh_on and not level else None
        km = _num_or_none(max_km)      # pickup only within this distance from home
        a.max_km = min(max(int(km if km is not None else 30), 1), 2000) if km_on else None
        if auto_buy_shown:             # a form with the auto-buy fields (another form leaves them as they are)
            most = _num_or_none(auto_buy_max)
            if auto_buy_on and not (most and most > 0):
                return back(f"/agents?error={quote(t('Auto-buy needs a maximum price'), safe='')}")
            a.auto_buy, a.auto_buy_max = bool(auto_buy_on), round(float(most), 2) if most else None
        db.add(a)
        db.commit()
        db.refresh(a)
    a = _set_screening(agent_id, bool(screen_on), screen_brief, screen_min, screen_hide, gh_check=bool(gh_check))
    scheduler.schedule_agent(a, run_soon=False)
    return back("/agents")


def _set_screening(agent_id: int, on: bool, brief: str, minimum: str, hide: list[str] | None = None,
                   gh_check: bool | None = None) -> SearchAgent:
    """Claude screening of new hits (see app/services/screening.py); `gh_check`: Claude also checks the Geizhals
    product (app/services/geizhals_check.py, only with the screening on); None = unchanged."""
    with session() as db:
        a = db.get(SearchAgent, agent_id)
        a.screen = on
        if gh_check is not None:
            a.geizhals_check = gh_check and on
        a.screen_brief = brief.replace("\r\n", "\n").strip() or None
        a.screen_min = minimum if minimum in screening.MIN_RANK else "match"
        if hide is not None:
            a.screen_hide = ",".join(i for i in screening.HIDEABLE if i in hide) or "-"
        db.add(a)
        db.commit()
        db.refresh(a)
    return a


@app.post("/agents/{agent_id}/delete")
async def agents_delete(agent_id: int):
    scheduler.unschedule_agent(agent_id)
    with session() as db:
        for h in db.exec(select(Hit).where(Hit.agent_id == agent_id)):
            db.delete(h)
        a = db.get(SearchAgent, agent_id)
        if a:
            db.delete(a)
        db.commit()
    return back("/agents")


HIT_PAGE = 200      # hits per batch; scrolling to the end of the list loads the next one
HIT_SORTS = ("newest", "oldest", "published", "price", "price_desc", "total", "distance")
HIT_SHIP = ("ship", "protected", "pickup")
HIT_LEVELS = ("bargain", "cheap", "fair")       # "this or better"
_LEVEL_RANK = {"bargain": 0, "cheap": 1, "fair": 2, "expensive": 3, "very_expensive": 4}
_SCREEN_LEVEL = {"great": "bargain", "good": "cheap", "fair": "fair", "overpriced": "expensive"}


HIT_STATES = ("new", "pending", "saved", "seen", "ignored", "filtered")   # the states the hits page shows (not cleared)


def _states(value: Any) -> tuple[str, ...]:
    """The states a view shows: several (the multi-select's repeated fields, a comma list), "all", or "new" when
    nothing valid is given."""
    parts = _many(value)
    if "all" in parts:
        return HIT_STATES
    return tuple(dict.fromkeys(p for p in parts if p in HIT_STATES)) or ("new",)


def _many(value: Any) -> list[str]:
    """A multi-select's values: repeated fields (["1", "2"]), one comma list ("1,2") or a single value."""
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [p.strip() for v in items if v is not None for p in str(v).split(",") if p.strip()]


@dataclass
class HitView:
    """What the hits page shows: tab, agents, sites (several of each, none = all) and the sort and filters; also what
    its bulk buttons act on."""
    states: tuple[str, ...] = ("new",)
    agent_ids: tuple[int, ...] = ()
    platforms: tuple[str, ...] = ()
    sort: str = "newest"
    ship: str = ""
    level: str = ""

    @classmethod
    def of(cls, state: str = "new", agent_id: Any = None, platform: Any = "", sort: str = "", ship: str = "",
           level: str = "") -> "HitView":
        ids = tuple(dict.fromkeys(int(x) for x in _many(agent_id) if x.isdigit() and int(x)))
        sites = tuple(dict.fromkeys(x.lower() for x in _many(platform)))
        return cls(states=_states(state), agent_ids=ids, platforms=sites,
                   sort=sort if sort in HIT_SORTS else "newest", ship=ship if ship in HIT_SHIP else "",
                   level=level if level in HIT_LEVELS else "")

    @property
    def state(self) -> str:
        """The one state shown ("new", "filtered" …), "all" for every one, "mixed" for some (the multi-select)."""
        if set(self.states) >= set(HIT_STATES):
            return "all"
        return self.states[0] if len(self.states) == 1 else "mixed"

    @state.setter
    def state(self, value: Any) -> None:
        self.states = _states(value)

    @property
    def agent_id(self) -> int | None:
        """The one agent shown (its name in the heading, no agent badge on the cards), None for several or all."""
        return self.agent_ids[0] if len(self.agent_ids) == 1 else None

    @property
    def platform(self) -> str:
        return self.platforms[0] if len(self.platforms) == 1 else ""

    def params(self, **change: Any) -> dict[str, Any]:
        state = self.state
        d = {"state": ",".join(self.states) if state == "mixed" else state,
             "agent_id": ",".join(map(str, self.agent_ids)), "platform": ",".join(self.platforms),
             "sort": "" if self.sort == "newest" else self.sort, "ship": self.ship, "level": self.level, **change}
        return {k: v for k, v in d.items() if v}

    def url(self, **change: Any) -> str:
        query = urlencode(self.params(**change))
        return "/hits" + (f"?{query}" if query else "")

    @property
    def filtered(self) -> bool:
        return bool(self.ship or self.level)

    def where(self, q, *, state: bool = True):
        if self.agent_ids:
            q = q.where(col(Hit.agent_id).in_(self.agent_ids))
        if self.platforms:
            q = q.where(col(Hit.platform).in_(self.platforms))
        # Kleinanzeigen ads with "VB" (price negotiable: "VB" alone or "120 € VB") are not shown
        q = q.where(or_(Hit.platform != "kleinanzeigen", ~func.coalesce(Hit.price_display, "").like("%VB%")))
        if self.ship == "ship":
            q = q.where(or_(Hit.shipping == "ship", col(Hit.shipping_price).is_not(None)))
        elif self.ship == "protected":       # shipping paid through the site, with its buyer protection
            ships = or_(Hit.shipping == "ship", col(Hit.shipping_price).is_not(None))
            others = [p for p in _shipping_info.BUYER_PROTECTION_SITES if p not in ("willhaben", "vinted")]
            # willhaben: PayLivery on; a hit from before the flag counts when it has PayLivery's shipping price
            paylivery = or_(Hit.paylivery == True, and_(col(Hit.paylivery).is_(None), col(Hit.shipping_price).is_not(None)))  # noqa: E712
            q = q.where(or_(and_(Hit.platform == "willhaben", paylivery),
                            and_(col(Hit.platform).in_(others), ships),
                            and_(Hit.platform == "vinted", or_(col(Hit.shipping).is_(None), Hit.shipping != "pickup"))))
        elif self.ship == "pickup":
            q = q.where(Hit.shipping == "pickup")
        if state:
            q = q.where(col(Hit.state).in_(self.states))         # every state but cleared, or some of them
        return q

    def _radius(self) -> float | None:
        return geo.pickup_radius() if "filtered" not in self.states and self.state != "all" else None

    def _python_filters(self) -> bool:
        return bool(self.level or self._radius())

    def _filtered(self, rows: list[Hit]) -> list[Hit]:
        """The filters SQL cannot do: far pickup-only ads (Settings → Shipping & pickup; a saved hit stays, it is
        the person's own choice) and the price label."""
        if radius := self._radius():
            home = geo.home_cached()
            rows = [h for h in rows if h.state == "saved" or not geo.too_far(h, radius, home)]
        return self._of_level(rows)

    def _of_level(self, rows: list[Hit]) -> list[Hit]:
        if not self.level:
            return rows
        zones, top = price_zones.config(), _LEVEL_RANK[self.level]
        return [h for h in rows if _LEVEL_RANK.get(hit_label(h, zones) or "", 9) <= top]

    def _far(self, db, radius: float, state: bool = True) -> dict[str, int]:
        """Hits per state the pickup radius hides: only pickup-only ones (no shipping stated) can be too far, so only
        those are loaded (a few columns) and measured in Python; saved hits are never hidden."""
        q = self.where(select(Hit.state, Hit.lat, Hit.lon, Hit.postcode, Hit.location, Hit.shipping, Hit.shipping_price),
                       state=state)
        q = q.where(or_(col(Hit.shipping).is_(None), Hit.shipping != "ship"), col(Hit.shipping_price).is_(None),
                    col(Hit.state).not_in(("saved", search_agents_svc.CLEARED)))
        home, out = geo.home_cached(), {}
        for st, lat, lon, postcode, location, ship, ship_price in db.exec(q):
            x = {"lat": lat, "lon": lon, "postcode": postcode, "location": location, "shipping": ship,
                 "shipping_price": ship_price}
            if geo.too_far(x, radius, home):
                out[st] = out.get(st, 0) + 1
        return out

    def count(self, db) -> int:
        """How many hits this view lists (all its pages): counted in SQL, the radius's far ones taken off."""
        if self.level:                    # the price label is computed in Python: the whole list
            return len(self._filtered(list(db.exec(self.where(select(Hit))))))
        n = db.exec(self.where(select(func.count()).select_from(Hit))).one()
        radius = self._radius()
        return n - sum(self._far(db, radius).values()) if radius else n

    def counts(self, db) -> dict[str, int]:
        """Hits per state as each tab lists them with this view's filters (cleared ones left out), and "all": the
        tabs' counters. The pickup radius hides far ads in the tabs but not under filtered out and all (_radius)."""
        radius = geo.pickup_radius()
        if self.level:
            rows = self._of_level(list(db.exec(self.where(select(Hit), state=False)
                                               .where(Hit.state != search_agents_svc.CLEARED))))
            home = geo.home_cached() if radius else None
            out = {"all": len(rows)}
            for h in rows:
                if not radius or h.state in ("filtered", "saved") or not geo.too_far(h, radius, home):
                    out[h.state] = out.get(h.state, 0) + 1
            return out
        q = self.where(select(Hit.state, func.count()), state=False).group_by(Hit.state)
        out = dict(db.exec(q).all())
        out.pop(search_agents_svc.CLEARED, None)
        out["all"] = sum(out.values())
        if radius:
            for st, n in self._far(db, radius, state=False).items():
                if st != "filtered" and st in out:
                    out[st] -= n
        return out

    def hits(self, db, limit: int = 200, offset: int = 0) -> list[Hit]:
        """The page's hits in its order; `offset`: the next batch when the list is scrolled down."""
        q = self.where(select(Hit))
        if self.sort == "price":
            q = q.order_by(col(Hit.price).asc().nulls_last(), col(Hit.id).desc())
        elif self.sort == "price_desc":
            q = q.order_by(col(Hit.price).desc().nulls_last(), col(Hit.id).desc())
        elif self.sort == "total":
            q = q.order_by((func.coalesce(Hit.price, 1e12) + func.coalesce(Hit.shipping_price, 0) + func.coalesce(Hit.buyer_fee, 0)).asc(), col(Hit.id).desc())
        elif self.sort == "oldest":       # by when the hit was found
            q = q.order_by(col(Hit.first_seen_at).asc(), col(Hit.id).asc())
        elif self.sort == "published":    # by the site's (re)publishing date
            q = q.order_by(col(Hit.published_at).desc().nulls_last(), col(Hit.id).desc())
        else:                             # newest hits first
            q = q.order_by(col(Hit.first_seen_at).desc(), col(Hit.id).desc())
        if not self._python_filters() and self.sort != "distance":
            return list(db.exec(q.offset(offset).limit(limit)))
        rows = self._filtered(list(db.exec(q.limit(max((offset + limit) * 10, 2000)))))
        if self.sort == "distance":       # in Python: the distance comes from coordinates or a cached postcode
            home = geo.home_cached()
            dist = {h.id: geo.hit_km(h, home) if home else None for h in rows}
            rows.sort(key=lambda h: (dist[h.id] is None, dist[h.id] or 0))
        return rows[offset:offset + limit]


def hit_label(h: Hit, zones: dict[str, Any] | None = None) -> str | None:
    """The price label a hit card shows: the zone from Geizhals' new price, else Claude's verdict."""
    lvl = deals_svc.hit_level(h, zones) if search_agents_svc.geizhals_below_pct(h) is not None else None
    if lvl is None and h.screen_relevant and h.screen_deal in _SCREEN_LEVEL:
        lvl = _SCREEN_LEVEL[h.screen_deal]
    return lvl


@app.get("/hits", response_class=HTMLResponse)
async def hits_page(request: Request, state: str = "new", sort: str = "", ship: str = "", level: str = "",
                    offset: int = 0):
    """agent_id and platform: several (repeated fields of the multi-selects, or comma lists in links); all ticked or
    none = no filter."""
    qp = request.query_params
    view = HitView.of(qp.getlist("state") or state, qp.getlist("agent_id"), qp.getlist("platform"), sort, ship, level)
    with session() as db:
        agents = {a.id: a for a in db.exec(select(SearchAgent))}
        used = sorted({pid for a in agents.values() for pid in _agent_platforms(a)})
        # the sites to filter by: those the agents search and those hits came from (an agent's sites may change)
        shown_q = select(Hit.platform).where(Hit.state != search_agents_svc.CLEARED).distinct()
        if view.agent_ids:
            shown_q = shown_q.where(col(Hit.agent_id).in_(view.agent_ids))
        with_hits = {p for p in db.exec(shown_q).all() if p}
        filter_ids = sorted(with_hits | set(used) | set(view.platforms), key=_platform_name)
        # every option ticked is no filter (a clean URL, and a new agent or site shows up too)
        if view.agent_ids and set(agents) <= set(view.agent_ids):
            view.agent_ids = ()
        if view.platforms and set(filter_ids) <= set(view.platforms):
            view.platforms = ()
        offset = max(offset, 0)
        hits = view.hits(db, HIT_PAGE, offset)
        state_counts = view.counts(db)
    return render(request, "hits.html", hits=hits, agents=agents, agent_id=view.agent_id, state=view.state,
                  state_counts=state_counts, platform=view.platform, platform_ids=used if len(used) > 1 else [],
                  filter_platform_ids=filter_ids if len(filter_ids) > 1 or view.platforms else [], view=view,
                  offset=offset, page=HIT_PAGE, more=len(hits) == HIT_PAGE,
                  watch_saved=search_agents_svc.watch_saved_on(),
                  keep_days=search_agents_svc.keep_days())


def _keep_condition(platform_id: str, ad_id: str, d: platforms.AdDetail, h: Hit) -> None:
    """The condition the ad page states goes to every hit of the ad (Shpock states it only there, and the price
    zones depend on it); a page without one shows what the list said."""
    if d.item_condition and (d.item_condition, d.item_condition_label) != (h.item_condition, h.item_condition_label):
        with session() as db:
            for x in db.exec(select(Hit).where(Hit.platform == platform_id, Hit.ad_id == ad_id)):
                x.item_condition, x.item_condition_label = d.item_condition, d.item_condition_label
                db.add(x)
            db.commit()
        h.item_condition, h.item_condition_label = d.item_condition, d.item_condition_label
    elif d.item_condition is None and h.item_condition:
        d.item_condition, d.item_condition_label = h.item_condition, h.item_condition_label


async def ad_fragment(request: Request, platform: platforms.Platform, ad_id: str, hit: int | None) -> HTMLResponse:
    """The ad viewer's content: the ad from its site (or, for sites without ad details, what the hit knows)."""
    h = None
    with session() as db:
        if hit:
            h = db.get(Hit, hit)
        else:                            # push, inbox and timeline links name the ad only: its newest hit, if any
            h = db.exec(select(Hit).where(Hit.platform == platform.id, Hit.ad_id == ad_id)
                        .order_by(col(Hit.id).desc())).first()
        if h:
            if h.state == "new":         # opened = seen: its push goes off the phone too
                h.state = "seen"
                db.add(h)
                db.commit()
                db.refresh(h)
                search_agents_svc.pushes_seen([h])
    error = d = detail_error = None
    reduced = not platform.can("detail")

    def known() -> platforms.AdDetail | None:
        """What the hit or the list that showed the ad knows (product search, direct search)."""
        from app.services import deals

        if h is not None:
            return platforms.AdDetail.from_hit(h)
        r = deals.shown_result(platform.id, ad_id)
        return platforms.AdDetail.from_result(r) if r else None

    if reduced:
        d = known()
    else:
        try:
            d = await platform.detail(ad_id, url=h.url if h else None)
        except PlatformError as e:
            d = known()
            if d is None:
                error = str(e)
            else:                       # the site failed: show what the list showed, and why
                reduced, detail_error = True, str(e)
    if d is not None and h is not None and not reduced:
        _keep_condition(platform.id, ad_id, d, h)
    if h is not None and not reduced and error is None:     # the ad page just loaded: its status for the hit cards
        search_agents_svc.record_status(platform.id, ad_id, search_agents_svc.site_status(d), d)
        with session() as db:
            h = db.get(Hit, h.id) or h                       # the pane shows the status just loaded
    if d is not None and h is not None:
        if d.images and not reduced:
            search_agents_svc.remember_photos(h.id, d.images)       # saving it now keeps them all
        elif not d.images:                  # a sold ad's page drops its photos: the ones the hit kept
            kept = search_agents_svc.kept_photos(h)
            d.images = ([f"/hits/{h.id}/image"] + [f"/hits/{h.id}/image/{i}" for i in range(1, len(kept))] if kept
                        else [h.image_url] if h.image_url else [])
    # the price verdict is the lazily loaded deals section (/hits/ad/{id}/deal), one verdict per ad
    return templates.TemplateResponse(request, "_ad_detail.html", {"d": d, "h": h, "ad_id": ad_id, "error": error,
                                                                   "platform": platform, "reduced": reduced,
                                                                   "detail_error": detail_error,
                                                                   "ref": platform.ref(ad_id)})


@app.post("/hits/ad/{pid}/{ad_id}/translation", response_class=HTMLResponse)
async def ad_translation(request: Request, pid: str, ad_id: str, text: str = Form("")):
    """The translation of an ad's description in another language than German or English (the viewer loads it under
    the text; asked once per ad and text)."""
    from app.services import translate
    if pid not in {p.id for p in platforms.all(enabled_only=False)} or not re.fullmatch(r"[\w-]{1,40}", ad_id):
        raise HTTPException(400)
    tr = await translate.translate_ad(pid, ad_id, text)
    return templates.TemplateResponse(request, "_ad_translation.html", {"tr": tr})


@app.get("/wh-fee/{ad_id}", response_class=HTMLResponse)
async def wh_paylivery_fee(request: Request, ad_id: str, price: float | None = None, sp: float = 0.0):
    """The ad viewer's price breakdown: willhaben's exact PayLivery fee, read from the chat's PayLivery window in a
    hidden read-only tab (app/willhaben/paylivery_fee.py); `sp` the cheapest shipping for the total."""
    from app.willhaben import paylivery_fee
    if not ad_id.isdigit():
        raise HTTPException(400)
    fee = error = None
    try:
        fee = (await paylivery_fee.fetch(get_session(), ad_id, price))["fee"]
        if fee is None:
            error = t("willhaben did not show the fee")
    except (paylivery_fee.FeeError, NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001 - the breakdown stays usable whatever the window does
        log.warning("PayLivery fee for %s failed: %s", ad_id, e)
        error = t("willhaben did not show the fee")
    return templates.TemplateResponse(request, "_wh_fee.html", {"fee": fee, "error": error, "price": price, "sp": sp})


@app.post("/hits/ad/willhaben/{ad_id}/message/draft", response_class=HTMLResponse)
async def ad_message_draft(request: Request, ad_id: str, instruction: str = Form(""), text: str = Form("")):
    """"Claude writes" in the ad pane's message form: my instruction and what the field holds (e.g. standard questions)
    become a first message in my style – back into the field, nothing is sent."""
    from app.llm.base import LLMError
    if not ad_id.isdigit():
        raise HTTPException(400)
    error = None
    try:
        text = await chats.draft_contact(ad_id, instruction, text.splitlines())
    except LLMError as e:
        error = t("Claude could not write the message: {error}", error=e)
    return render(request, "_contact_text.html", ad_id=ad_id, text=text, error=error)


@app.post("/settings/contact-questions")
async def contact_questions_save(questions: str = Form("")):
    """The standard questions of the ad pane's message form (Settings → Chats), one per line."""
    set_setting("contact_questions", "\n".join(q.strip() for q in questions.splitlines() if q.strip()))
    return back(f"/settings/chats?msg={quote(t('Standard questions saved'), safe='')}#s-contact-questions")


@app.post("/hits/ad/willhaben/{ad_id}/message")
async def ad_message_seller(ad_id: str, text: str = Form("")):
    """The ad pane's "Message the seller": opens the chat on willhaben with this first message, then the chat page.
    A chat about this ad that is already there is opened instead (no second opener)."""
    from app.willhaben.account import OpenerUnconfirmed, SendFailed
    if not ad_id.isdigit():
        raise HTTPException(400)
    if cid := chats.chat_for_ad(ad_id):
        return back(f"/chats/{quote(cid, safe='')}")
    try:
        cid = await chats.contact_seller(ad_id, text)
    except (SendFailed, NotLoggedIn, SessionUnavailable, ValueError) as e:      # nothing went out
        return back(f"/deals/ad/{ad_id}?msg={quote(t('willhaben did not open the chat: {error}', error=e), safe='')}")
    except OpenerUnconfirmed as e:        # it went out: the chat shows up with the next sync, never write again
        return back(f"/chats?msg={quote(t('The message was sent, the chat shows up shortly: {error}', error=e), safe='')}")
    return back(f"/chats/{quote(cid, safe='')}")


@app.get("/hits/ad/{ad_id}", response_class=HTMLResponse)
async def hit_ad_detail(request: Request, ad_id: str, hit: int | None = None):
    """A willhaben ad as a fragment for the in-dashboard viewer (other sites: /hits/ad/<platform>/<id>)."""
    if not ad_id.isdigit():
        raise HTTPException(400)
    return await ad_fragment(request, platforms.get("willhaben"), ad_id, hit)


def _settle_hits(hits: list[Hit], state: str) -> None:
    """A hit kept or ignored here: its Telegram message says so and loses Keep / Ignore (services/telegram.settle)."""
    from app.services import telegram
    line = {"saved": "⭐ " + t("Kept in the app"), "ignored": "🚫 " + t("Ignored in the app")}.get(state)
    if not line:
        return
    for h in hits[:50]:
        telegram.settle_later([search_agents_svc.hit_tag(h)], line, h.url)


def _settle_draft(d: Draft | None, line: str) -> None:
    """A draft sent or discarded here: its Telegram message (the draft offer, or the chat's message it took over)
    says so and loses Send / Revise / Discard."""
    from app.services import ha_actions, telegram
    if d is None:
        return
    url = f"{get_settings().public_url.rstrip('/')}/chats/{d.conversation_id}"
    telegram.settle_later([ha_actions.tag_for(d.id), chats.message_tag(d.conversation_id)], line, url)


@app.post("/hits/{hit_id}/state", response_class=HTMLResponse)
async def hit_state(request: Request, hit_id: int, state: str = Form(...)):
    with session() as db:
        h = db.get(Hit, hit_id)
        h.state = state
        db.add(h)
        db.commit()
        db.refresh(h)
    if state in ("saved", "ignored"):     # the owner's taste, kept after the hit is cleared (services/taste.py)
        from app.services import taste
        taste.record(h, state == "saved")
        _settle_hits([h], state)
    if state != "new":                   # handled: its push goes off the phone
        search_agents_svc.pushes_seen([h])
    if state == "saved":                # its photos, before the site drops them (sold, deleted)
        search_agents_svc.keep_image_later(hit_id, search_agents_svc.shown_photos(hit_id))
    if request.headers.get("hx-request"):
        return HTMLResponse("")
    return back("/hits")


@app.get("/hits/swipe", response_class=HTMLResponse)
async def hits_swipe(request: Request, agent_id: int | None = None):
    """The new hits one at a time: swipe right to save, left to discard (Tinder-like). Each verdict is kept for the
    agent's taste (services/taste.py)."""
    with session() as db:
        q = select(Hit).where(Hit.state == "new")
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        hits = list(db.exec(q.order_by(col(Hit.first_seen_at).desc()).limit(40)))
        agents = {a.id: a for a in db.exec(select(SearchAgent))}
    return render(request, "hits_swipe.html", hits=hits, agents=agents, agent_id=agent_id)


@app.post("/hits/{hit_id}/vote")
async def hit_vote(hit_id: int, liked: str = Form(...)):
    """A swipe: right saves the hit, left discards it (ignored); both are kept for the agent's taste."""
    from app.services import taste
    like = liked in ("1", "true", "yes")
    with session() as db:
        h = db.get(Hit, hit_id)
        if h is None:
            raise HTTPException(404)
        h.state = "saved" if like else "ignored"
        db.add(h)
        db.commit()
        db.refresh(h)
    taste.record(h, like)
    _settle_hits([h], h.state)
    search_agents_svc.pushes_seen([h])
    if like:
        search_agents_svc.keep_image_later(hit_id, search_agents_svc.shown_photos(hit_id))
    return JSONResponse({"ok": True, "state": h.state})


_SHIP_TTL_S = 6 * 3600
_ship_cache: dict[tuple[str, str], tuple[float, float | None, str | None]] = {}   # (site, ad) -> (at, price, kind)


@app.get("/ship/{pid}/{ad_id}", response_class=HTMLResponse)
async def ad_shipping(request: Request, pid: str, ad_id: str):
    """The shipping of a result whose list stated no price (product and direct search cards load it once they are
    in view, ui.ship_lazy): the ad page, one request through the site's limiter and VPN, kept for _SHIP_TTL_S.
    Every hit of the ad keeps it too. Empty when the page states nothing or the site cannot be asked."""
    import time as _time

    from app.platforms import shipping_info

    key = (pid, ad_id)
    got = _ship_cache.get(key)
    if got is None or _time.monotonic() - got[0] > _SHIP_TTL_S:
        try:
            p = platforms.get(pid)
            d = await p.detail(ad_id) if p.can("detail") else None
        except (platforms.UnknownPlatform, PlatformError) as e:
            log.info("shipping of %s %s not loaded: %s", pid, ad_id, e)
            return HTMLResponse("")
        if d is not None:
            search_agents_svc.record_status(pid, ad_id, search_agents_svc.site_status(d), d)
        got = (_time.monotonic(), d.shipping_price if d else None, d.shipping if d else None)
        _ship_cache[key] = got
    text = shipping_info.label(got[1], got[2])
    return HTMLResponse(templates.get_template("_ui.html").module.ship({"shipping_price": got[1], "shipping": got[2]})
                        if text else "")


@app.get("/hits/{hit_id}/image")
async def hit_image(hit_id: int):
    """A saved hit's thumbnail as kept locally (search_agents.keep_image)."""
    with session() as db:
        h = db.get(Hit, hit_id)
    folder = get_settings().hit_images_dir
    path = (folder / h.image_path) if h is not None and h.image_path else None
    if path is None or path.parent != folder or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/hits/{hit_id}/image/{n}")
async def hit_image_n(hit_id: int, n: int):
    """A saved hit's further kept photos (the ad page's second, third, …)."""
    folder = get_settings().hit_images_dir
    path = next(iter(sorted(folder.glob(f"hit-{hit_id}-{n}.*"))), None) if n > 0 else None
    if path is None or path.parent != folder or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/hits/{hit_id}/sync", response_class=HTMLResponse)
async def hit_sync(request: Request, hit_id: int):
    """A hit card's sync button: the ad's status from its site now (sold, reserved, gone …); the card's status line."""
    error = None
    try:
        h = await search_agents_svc.check_hit(hit_id)
    except PlatformError as e:
        error, h = str(e), None
        with session() as db:
            h = db.get(Hit, hit_id)
    if h is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_hit_status.html", {"h": h, "sync_error": error,
                                                                    "can_sync": _can_sync(h)})


def _can_sync(h: Hit) -> bool:
    try:
        return platforms.get(h.platform or "willhaben").can("detail")
    except platforms.UnknownPlatform:
        return False


def _view_form(form: Any, state: str | None = None) -> HitView:
    """The hits page's view as its forms send it (hidden fields of the same names)."""
    return HitView.of(state or (form.getlist("state") if hasattr(form, "getlist") else form.get("state")) or "new",
                      form.getlist("agent_id") if hasattr(form, "getlist")
                      else form.get("agent_id"), form.getlist("platform") if hasattr(form, "getlist") else form.get("platform"),
                      form.get("sort") or "", form.get("ship") or "", form.get("level") or "")


@app.post("/hits/clear-filtered")
async def hits_clear_filtered(request: Request):
    """Hits → Filtered out → Clear: those the page shows (agent, site, filters), or all."""
    view = _view_form(await request.form(), "filtered")
    with session() as db:                   # exactly what the page shows (several agents or sites, the filters)
        ids = [h.id for h in view.hits(db, limit=100000)]
    search_agents_svc.clear_hits(("filtered",), ids=ids)
    return back(view.url())


@app.post("/hits/keep-days")
async def hits_keep_days(request: Request):
    """Hits: after how many days they are cleared by themselves, all but saved ones (0 = never)."""
    form = await request.form()
    days = str(form.get("days") or "")
    search_agents_svc.set_keep_days(int(days) if days.strip().isdigit() else 0)
    view = _view_form(form)
    if view.state not in (*search_agents_svc.EXPIRING, "saved", "all", "mixed"):
        view.state = "new"
    return back(view.url())


@app.post("/hits/watch-saved")
async def hits_watch_saved(request: Request):
    """Hits → Saved: whether saved ads are loaded about once a day for price drops and status changes."""
    form = await request.form()
    set_setting("watch_saved", "1" if form.get("on") else "0")
    return back(_view_form(form, "saved").url())


@app.post("/hits/bulk")
async def hits_bulk(request: Request):
    """The hits page's selection: save, mark as seen, ignore or clear the ticked hits."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    action = str(form.get("action") or "")
    view = _view_form(form)
    if ids and action == "clear":
        search_agents_svc.clear_hits(search_agents_svc.EXPIRING, ids=ids)
    elif ids and action in ("saved", "seen", "ignored"):
        with session() as db:
            done = list(db.exec(select(Hit).where(col(Hit.id).in_(ids), Hit.state != search_agents_svc.CLEARED)))
            for h in done:
                h.state = action
                db.add(h)
            db.commit()
        _settle_hits(done, action)
    return back(view.url())


@app.post("/hits/check-status")
async def hits_check_status(request: Request):
    """"Check availability": every hit the page shows (its filters) read from its site again, as a background job;
    the cards refresh when it is done."""
    view = _view_form(await request.form())
    with session() as db:
        ids = [h.id for h in view.hits(db, limit=search_agents_svc.CHECK_MAX)]
    if not ids:
        return back(view.url())

    async def run(job: jobs.Job) -> str:
        r = await search_agents_svc.check_hits(ids, job)
        return t("{checked} checked: {online} still online, {gone} sold, reserved or gone", **r) + \
            (" · " + t("{n} not reachable", n=r["failed"]) if r["failed"] else "")
    jobs.submit("hits-status", t("Check availability of {n} hits", n=len(ids)), run, lane="agents", total=len(ids))
    if request.headers.get("hx-request"):
        return Response(status_code=204)
    return back(view.url())


@app.post("/hits/mark-seen")
async def hits_mark_seen(request: Request):
    """New → Mark all as seen: the hits the page shows (agent, site, filters)."""
    view = _view_form(await request.form(), "new")
    with session() as db:
        seen = list(view.hits(db, limit=100000))
        for h in seen:
            h.state = "seen"
            db.add(h)
        db.commit()
    search_agents_svc.pushes_seen(seen)
    return back(view.url(state=""))


# ------------------------------------------------------------------ chats
def _chat_list(show_hidden: bool) -> dict[str, Any]:
    """The conversation list (chats.html); the open chat (chat.html) shows it next to the thread."""
    with session() as db:
        o = chats.overview(db)
    # older than the sync's window (Settings → Automatic answers: the last N days): out of the list like hidden ones,
    # which the sync no longer updates anyway; "Show hidden chats" brings both back
    convs = o["convs"] if show_hidden else o["shown"]
    hidden_count = len(o["convs"]) - len(o["shown"])
    open_drafts, waiting = o["open_drafts"], o["waiting"]
    # a site shows (its login hint, live line, filter) only when it is set up: willhaben logged in or with chats,
    # Vinted switched on and logged in or with chats
    have = {c.platform or "willhaben" for c in convs}
    wh_on = "willhaben" in have or bool(get_session().logged_in)
    vinted_on = platforms.enabled("vinted") and ("vinted" in have or bool(site_accounts.state("vinted")["logged_in"]))
    return {"convs": convs, "open_drafts": open_drafts, "hidden_count": hidden_count, "show_hidden": show_hidden,
            "waiting": waiting, "finished": o["finished"], "finished_fresh": o["finished_fresh"], "finished_why": o["finished_why"], "unread_by_chat": o["unread"], "thumbs": o["thumbs"], "wh_on": wh_on, "vinted_on": vinted_on}


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, msg: str | None = None, show_hidden: bool = False):
    return render(request, "chats.html", msg=msg, live_state=chats.live_state(), **_chat_list(show_hidden))


@app.get("/chats/live-state")
async def chats_live_state(c: str | None = None):
    """What the open chat page shows, as fingerprints: it polls this (local database only, no willhaben request)
    and swaps just the part that changed – new messages appear without a reload."""
    if c:
        chats.seen_in_app(c)            # the open chat shows its new messages: read here (the sidebar badge)
    return chats.live_state(c)


@app.post("/chats/bulk/{action}")
async def chats_bulk(request: Request, action: str):
    """Selected chats: mark read (read receipts on willhaben, best effort), hide here, or delete (on willhaben and
    here, _delete_chats)."""
    if action not in ("read", "hide", "remove"):
        raise HTTPException(404)
    ids = [str(i) for i in (await request.form()).getlist("ids")]
    for cid in ids:
        if action == "hide":
            chats.hide(cid)
        elif action == "remove":
            continue                     # all at once below: one willhaben request
        else:
            chats.seen_in_app(cid)
            if get_session().logged_in:
                await chats.mark_read(cid)
            with session() as db:
                conv = db.get(Conversation, cid)
                if conv and conv.unread_count:
                    conv.unread_count = 0
                    db.add(conv)
                    db.commit()
    if action == "remove":
        msg, _ok = await _delete_chats(ids) if ids else ("", True)
        return back(f"/chats?msg={quote(msg, safe='')}")
    msg = t("{n} chats hidden", n=len(ids)) if action == "hide" else t("{n} chats marked read", n=len(ids))
    return back(f"/chats?msg={quote(msg, safe='')}")


@app.post("/chats/{conversation_id}/hide")
async def chat_hide(conversation_id: str, hidden: str = Form("1")):
    chats.hide(conversation_id, hidden == "1")
    return back(f"/chats?msg={quote(t('Chat hidden here (still on willhaben); it comes back when they write again') if hidden == '1' else '', safe='')}")


@app.post("/chats/{conversation_id}/finished")
async def chat_finished(conversation_id: str, finished: str = Form("1")):
    """Mark as finished (or open again): no answer due; a new message from them opens it again by itself."""
    chats.set_finished(conversation_id, finished == "1")
    return back(f"/chats/{conversation_id}")


@app.post("/chats/{conversation_id}/unread")
async def chat_unread(conversation_id: str):
    """Mark as unread (here only): back to the list, where it counts as unread again."""
    chats.mark_unread(conversation_id)
    return back("/chats")


async def _delete_chats(ids: list[str]) -> tuple[str, bool]:
    """Delete chats on willhaben and here (chats.delete); Vinted's delete is not known: those are only removed here,
    and the message says so. (message, ok); not ok when willhaben refused or could not be reached: nothing changed."""
    try:
        deleted, kept = await chats.delete(ids)
    except Exception as e:  # noqa: BLE001 - not logged in, browser away, willhaben refused: shown, nothing removed
        log.warning("deleting chats failed: %s", e)
        return t("The chat could not be deleted on willhaben: {error}", error=e), False
    for cid in kept:
        chats.remove(cid)
    parts = []
    if deleted:
        parts.append(t("Chat deleted on willhaben and here") if len(deleted) == 1
                     else t("{n} chats deleted on willhaben and here", n=len(deleted)))
    if kept:
        parts.append(t("Vinted chats can only be removed here so far ({n}) – on Vinted they stay", n=len(kept)))
    return " · ".join(parts), True


@app.post("/chats/{conversation_id}/remove")
async def chat_remove(conversation_id: str):
    """Delete the chat on its site (willhaben) and here."""
    msg, ok = await _delete_chats([conversation_id])
    return back(f"/chats{'' if ok else '/' + quote(conversation_id, safe='')}?msg={quote(msg, safe='')}")


@app.post("/chats/sync")
async def chats_sync(request: Request):
    """Background job (lane chat): the list updates itself as the conversations come in (live view)."""
    chats.enqueue_sync()
    if _hx(request):
        return Response(status_code=204)
    return back(f"/chats?msg={quote(t('Sync started – it runs in the background'), safe='')}")


# ------------------------------------------------------------------ chat simulator (app/services/chat_sim.py)
def _sim_url(sim_id: int | None = None, msg: str | None = None, anchor: str = "") -> str:
    q = [f"id={sim_id}"] if sim_id else []
    if msg:
        q.append(f"msg={quote(msg, safe='')}")
    return "/simulator" + (f"?{'&'.join(q)}" if q else "") + anchor


@app.get("/simulator", response_class=HTMLResponse)
async def simulator_page(request: Request, id: int | None = None, msg: str | None = None, round: int | None = None):
    sim = chat_sim.get(id) if id else None
    played = [r["n"] for r in (sim.iterations or []) if not r.get("base")] if sim else []
    cur_round = round if round in played or round == 1 else (played[-1] if played else 1)   # default: the newest
    with session() as db:
        ads = list(db.exec(select(MyAd).where(MyAd.status == "active", col(MyAd.hidden).is_not(True))
                           .order_by(col(MyAd.heading))))
    live_ad = None
    if sim is not None and sim.ad_id:
        with session() as db:
            live_ad = current_ad(db, sim.ad_id)
    return render(request, "simulator.html", msg=msg, sims=chat_sim.all_chats(), sim=sim, ads=ads, live_ad=live_ad,
                  purchases=purchase_svc.recent(20), personas=chat_sim.PERSONAS, platforms=chat_sim.PLATFORMS,
                  expectations=chat_sim.EXPECTATIONS, proposals=chat_sim.open_proposals(), sim_diff=chat_sim.diff,
                  proposal_of=chat_sim.proposal, cur_round=cur_round,
                  review_model=llm_models.resolve_alias(chat_sim.REVIEW_MODEL),
                  targets=chat_sim.TARGETS, expectation_label=chat_sim.expectation_label,
                  persona_label=chat_sim.persona_label, ad_view=chat_sim.ad_view, sim_stage=chat_sim.stage_of,
                  can_suggest=chat_sim.can_suggest, intent_label=chats.intent_label,
                  purchase_status=purchase_svc.status_label, sim_templates=chat_sim.templates_for,
                  next_script=chat_sim.next_script, model_choices=llm_models.choices(),
                  model_label=llm_models.label, default_model=model_for, system_prompts=system_prompts,
                  reviewing=jobs.current_keys("simreview:"))


@app.post("/simulator/new")
async def simulator_new(role: str = Form("seller"), platform: str = Form("willhaben"), ad_id: str = Form(""),
                        purchase_id: str = Form(""), persona: str = Form(""), title: str = Form("")):
    try:
        sim = await chat_sim.create(role, platform, ad_id=ad_id or None,
                                    purchase_id=int(purchase_id) if purchase_id.isdigit() else None,
                                    persona=persona or None, title=title)
    except ValueError as e:
        return back(_sim_url(msg=str(e)))
    return back(_sim_url(sim.id))


@app.post("/simulator/random")
async def simulator_random(role: str = Form("seller"), platform: str = Form("willhaben"), persona: str = Form("")):
    try:
        sim = await chat_sim.create_random(role, platform, persona=persona or None)
    except ValueError as e:
        return back(_sim_url(msg=str(e)))
    return back(_sim_url(sim.id))


@app.post("/simulator/template")
async def simulator_template(role: str = Form("seller"), template: str = Form(""), platform: str = Form("willhaben"),
                             ad_id: str = Form(""), purchase_id: str = Form("")):
    """One template, or "all" of the role's: scenarios set up (nothing sent or run yet)."""
    keys = [k for k, _l in chat_sim.templates_for(role)] if template == "all" else [template]
    keys = [k for k in keys if k in chat_sim.TEMPLATES]
    if not keys:
        raise HTTPException(404)
    sim = None
    try:
        for k in keys:
            sim = await chat_sim.create_from_template(k, platform, ad_id=ad_id or None,
                                                      purchase_id=int(purchase_id) if purchase_id.isdigit() else None)
    except ValueError as e:
        return back(_sim_url(msg=str(e)))
    msg = t("{n} scenarios set up – send their messages or run them when you like", n=len(keys)) if len(keys) > 1 else None
    return back(_sim_url(sim.id, msg))


@app.post("/simulator/{sim_id}/random")
async def simulator_randomize(sim_id: int):
    _sim_or_404(sim_id)
    try:
        await chat_sim.randomize(sim_id)
    except ValueError as e:
        return back(_sim_url(sim_id, str(e)))
    return back(_sim_url(sim_id, t("New random ad as the article – the conversation starts again")))


def _sim_or_404(sim_id: int):
    sim = chat_sim.get(sim_id)
    if sim is None:
        raise HTTPException(404)
    return sim


SIM_TEXT_FIELDS = ("title_ad", "description", "location", "seller_name", "instructions", "pickup_window", "mode",
                   "delivery")
SIM_NUMBER_FIELDS = ("price", "min_price", "max_price", "wish_price", "shipping_cost", "quantity", "ad_shipping")


@app.post("/simulator/{sim_id}/setup")
async def simulator_setup(request: Request, sim_id: int):
    _sim_or_404(sim_id)
    form = await request.form()
    values: dict[str, Any] = {}
    for k in SIM_TEXT_FIELDS:
        if k in form:
            values["title" if k == "title_ad" else k] = str(form[k]).strip()[:4000]
    for k in SIM_NUMBER_FIELDS:
        if k in form:
            raw = str(form[k]).strip()
            n = chat_sim._number(raw) if raw else None
            values[k] = (int(n) if k == "quantity" and n else n)
    if "questions" in form:
        values["questions"] = [q.strip()[:300] for q in str(form["questions"]).splitlines() if q.strip()][:8]
    chat_sim.update_setup(sim_id, values, persona=str(form.get("persona") or "") or None,
                          persona_text=str(form["persona_text"]) if "persona_text" in form else None,
                          title=str(form.get("title") or ""), platform=str(form.get("platform") or "") or None,
                          model=str(form["model"]) if "model" in form else None,
                          partner_model=str(form["partner_model"]) if "partner_model" in form else None)
    return back(_sim_url(sim_id, t("Saved")))


@app.post("/simulator/{sim_id}/say")
async def simulator_say(sim_id: int, text: str = Form("")):
    _sim_or_404(sim_id)
    ok = chat_sim.say(sim_id, text)
    return back(_sim_url(sim_id, None if ok else t("Busy or empty message"), "#sim-end"))


@app.post("/simulator/{sim_id}/{action}")
async def simulator_action(sim_id: int, action: str, rounds: int = Form(3), on: str = Form(""),
                           kind: str = Form(""), value: str = Form(""), feedback: str = Form("")):
    _sim_or_404(sim_id)
    msg = None
    if action == "reply":
        ok = chat_sim.reply(sim_id)
    elif action == "partner":
        ok = chat_sim.partner_once(sim_id)
    elif action == "play":
        ok = chat_sim.play(sim_id, rounds)
    elif action == "suggest":
        ok = chat_sim.suggest(sim_id)
    elif action == "run":
        ok = chat_sim.run_scenarios([sim_id]) > 0
        msg = None if ok else t("Nothing to run: the scenario needs messages and is not running already")
    elif action == "review_round":              # a stronger model writes feedback for the next round
        ok = chat_sim.review_round(sim_id, kind or None)
    elif action == "round":                     # the next round from my ratings and feedback
        ok = chat_sim.next_round(sim_id, feedback)
        msg = None if ok else t("Rate an answer or write feedback first (or wait until the chat is done)")
        if ok:
            return back(_sim_url(sim_id, None, "#sim-end"))
    elif action == "clear":
        chat_sim.clear(sim_id)
        ok = True
    elif action == "delete":
        chat_sim.delete(sim_id)
        return back(_sim_url(msg=t("Test chat deleted")))
    elif action == "scenario":
        chat_sim.set_scenario(sim_id, on == "1")
        ok = True
    elif action == "expect":
        ok = chat_sim.add_expectation(sim_id, kind, value)
        msg = None if ok else t("Expectation not added: it needs a value")
    else:
        raise HTTPException(404)
    if not ok and msg is None:
        msg = t("The test chat is busy – wait until it is done")
    return back(_sim_url(sim_id, msg, "#sim-end"))


@app.get("/simulator/{sim_id}/prompt/{index}", response_class=HTMLResponse)
async def simulator_prompt(request: Request, sim_id: int, index: int, run: str = ""):
    """The whole prompt Claude got for one message (of the chat, or of its last run / the proposal's run)."""
    sim = _sim_or_404(sim_id)
    if run.startswith("round:") and run[6:].isdigit():
        turns = next((r.get("turns") for r in sim.iterations or [] if r.get("n") == int(run[6:])), None)
    else:
        turns = (sim.last_run or {}).get("turns") if run == "last" else \
            (sim.proposal_run or {}).get("turns") if run == "proposal" else sim.turns
    turns = turns or []
    if not 0 <= index < len(turns):
        raise HTTPException(404)
    return templates.TemplateResponse(request, "_sim_prompt.html", {"p": turns[index].get("prompt") or {},
                                                                      "model": turns[index].get("model")})


@app.post("/simulator/{sim_id}/expect/{index}/remove")
async def simulator_expect_remove(sim_id: int, index: int):
    chat_sim.remove_expectation(sim_id, index)
    return back(_sim_url(sim_id, anchor="#sim-expect"))


@app.post("/simulator/{sim_id}/round/{n}/rate/{index}")
async def simulator_round_rate(sim_id: int, n: int, index: int, rating: str = Form(""), note: str | None = Form(None)):
    chat_sim.rate_round(sim_id, n, index, rating or None, note)
    return back(_sim_url(sim_id, anchor=f"#round-{n}-{index}"))


@app.post("/simulator/{sim_id}/rate/{index}")
async def simulator_rate(sim_id: int, index: int, rating: str = Form(""), note: str | None = Form(None)):
    chat_sim.rate(sim_id, index, rating or None, note)
    return back(_sim_url(sim_id, anchor=f"#turn-{index}"))


@app.post("/simulator-delete")
async def simulator_delete_many(request: Request):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    for sid in ids:
        chat_sim.delete(sid)
    back_to = str(form.get("back_to") or "")
    keep = int(back_to) if back_to.isdigit() and int(back_to) not in ids else None
    return back(_sim_url(keep, t("{n} test chats deleted", n=len(ids))))


@app.post("/simulator-prompts/{kind}/{action}")
async def simulator_prompts(kind: str, action: str, text: str = Form(""), focus: str = Form(""),
                            model: str = Form(""), back_to: str = Form("")):
    """System prompts: save (applies to real answers), test as a proposal, Claude review, reset to the default."""
    if kind not in system_prompts.KINDS:
        raise HTTPException(404)
    sid = int(back_to) if back_to.isdigit() else None
    if action == "save":
        system_prompts.save(kind, text)
        msg = t("System prompt saved – it applies to automatic answers now")
    elif action == "propose":
        msg = t("Proposal created – test it with the scenarios, then apply it") if chat_sim.propose_text(kind, text) \
            else t("Nothing changed")
    elif action == "review":
        msg = t("Claude reviews the system prompt – the proposal appears above") if chat_sim.review(kind, focus, model or None) \
            else t("A review of this prompt is already running")
    elif action == "reset":
        system_prompts.reset(kind)
        msg = t("System prompt reset to the default")
    else:
        raise HTTPException(404)
    return back(_sim_url(sid, msg, "#sim-prompts"))


@app.post("/simulator-run-all")
async def simulator_run_all(back_to: str = Form("")):
    n = chat_sim.run_scenarios()
    sid = int(back_to) if back_to.isdigit() else None
    return back(_sim_url(sid, t("{n} scenarios are running", n=n) if n else t("No scenarios to run")))


@app.post("/simulator-proposal/{pid}/{action}")
async def simulator_proposal(pid: int, action: str, text: str | None = Form(None), back_to: str = Form("")):
    sid = int(back_to) if back_to.isdigit() else None
    if action == "try":
        n = chat_sim.run_scenarios(proposal_id=pid)
        msg = t("{n} scenarios are running with the proposal", n=n) if n else t("No scenarios to run")
    elif action == "apply":
        msg = t("Rule change applied") if chat_sim.apply_proposal(pid, text) else t("Proposal not open any more")
    elif action == "discard":
        chat_sim.discard_proposal(pid)
        msg = t("Proposal discarded")
    else:
        raise HTTPException(404)
    return back(_sim_url(sid, msg))


@app.get("/chats/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: str, msg: str | None = None, sort: str | None = None,
                    show_hidden: bool = False):
    with session() as db:
        conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404)
    from app.services import translate
    translate.schedule(conversation_id)          # foreign messages: their translation comes in by itself
    chats.seen_in_app(conversation_id)                   # the sidebar badge: read here
    task = asyncio.create_task(chats.ask_presence(conv))   # their online state: the header shows it once it is known
    _background.add(task)
    task.add_done_callback(_background.discard)
    if conv.unread_count and get_session().logged_in:   # you read it here, so the buyer sees "read" (background)
        task = asyncio.create_task(chats.mark_read(conversation_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    drafts = chats.drafts_of(conversation_id)
    open_draft = next((d for d in drafts if d.status == "draft"), None)    # the one the composer shows
    return render(request, "chat.html", conv=conv, messages=chats.messages_of(conversation_id),
                  drafts=drafts, msg=msg, live_state=chats.live_state(conversation_id), meetup=meetups.current(conv, drafts), gcal_on=gcal.connected(),
                  sale=sold.state_for(conv), contact_warn=selling.contact_hits(open_draft.text) if open_draft else [],
                  sale_panel=sold.panel(conv.ad_id, conv.id) if conv.role == "seller" and conv.ad_id else None,
                  gcal_duration=gcal.config()["duration"], **await _shipping_ctx(conversation_id, conv, sort),
                  **_chat_list(show_hidden or bool(conv.hidden_at)))


async def _shipping_ctx(conversation_id: str, conv: Conversation, sort: str | None = None) -> dict[str, Any]:
    from app.services import dropoff
    s = shipping.open_for(conversation_id)
    if sort in ("price", "near"):
        set_setting("ship_sort", sort)
    sort = get_setting("ship_sort") or "price"
    near: dict[str, list[dict[str, Any]]] = {}
    if s and conv.role == "seller":
        try:
            near = await asyncio.wait_for(dropoff.nearest(shipping.sender(), shipping.enabled_carriers()), 10)
        except TimeoutError:
            near = {}
    options = shipping.quote(s.parcel) if s else []
    if sort == "near" and near:
        far = 10 ** 9
        options = sorted(options, key=lambda o: ((near.get(o.carrier) or [{}])[0].get("dist_m") or far, o.price))
    # "→ Shop" option: the parcel shops near the buyer; the stored pick, else the nearest, is the target
    shops = await shipping.shops_near(s) if s and conv.role == "seller" else []
    stored = shipping.shop_for(s) if s else None
    return {"shipment": s, "ship_options": options, "ship_near": near, "ship_sort": sort,
            "ship_pref": selling.preference_text(), "fmt_dist": dropoff.fmt_dist,
            "ship_shops": shops, "ship_shop": stored or (shops[0] if shops else None), "ship_shop_stored": bool(stored),
            "ship_option": shipping.by_key(s.option) if s else None, "ship_on": conv.role == "seller",
            "ship_detecting": f"shipping:{conversation_id}" in jobs.current_keys("shipping:"),
            "ship_sender_ok": shipping.sender_complete(), "ship_carriers": shipping.CARRIERS,
            "ship_all": shipping.all_options(s.parcel) if s else [], **(shipping.page_state(s) if s else {}),
            **printing_routes.shipment_panel(s)}


@app.post("/chats/{conversation_id}/refresh")
async def chat_refresh(request: Request, conversation_id: str):
    """Background job (lane chat): the thread updates itself (live view)."""
    chats.enqueue_reload(conversation_id)
    if _hx(request):
        return Response(status_code=204)
    return back(f"/chats/{conversation_id}")


@app.post("/chats/{conversation_id}/draft", response_class=HTMLResponse)
async def chat_draft(request: Request, conversation_id: str, hint: str = Form("")):
    """Background job (lane claude): the composer shows "Claude is writing…", the draft appears by itself."""
    chats.enqueue_draft(conversation_id, hint.strip() or None)
    return back(f"/chats/{conversation_id}")


@app.post("/chats/{conversation_id}/send-direct")
async def chat_send_direct(conversation_id: str, hint: str = Form("")):
    """My own text from the composer, sent as it is right away (no Claude draft, no countdown)."""
    text = hint.strip()
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(404)
        if not text:
            return back(f"/chats/{conversation_id}?msg={quote(t('Write the message first'), safe='')}")
        d = Draft(conversation_id=conversation_id, text=text, model="manual")
        db.add(d)
        db.commit()
        db.refresh(d)
    chats.enqueue_send(d.id, text, now=True)
    return back(f"/chats/{conversation_id}#draft-{d.id}")


@app.post("/drafts/{draft_id}/send")
async def draft_send(draft_id: int, text: str = Form(...), paylivery_step: str = Form(""), pl_amount: str = Form(""),
                     now: str = Form("")):
    """Queued: the chat page comes back at once with the message marked "sending…" (chats.enqueue_send).
    `paylivery_step`: the draft's PayLivery step (accept, counter, decline) starts in the browser along with it.
    `now`: Send now – no countdown to take it back."""
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None:
        raise HTTPException(404)
    step = paylivery_step if paylivery_step and (d.paylivery or {}).get("action") == paylivery_step else ""
    pl = paylivery_routes.paylivery
    if step:              # checked first: a refusal (amount, order maximum) keeps the answer unsent as well
        try:
            pl.validate_action(d.conversation_id, step, pl_amount)
        except pl.PayLiveryError as e:
            return back(f"/chats/{d.conversation_id}?msg={quote(str(e), safe='')}")
    # with a PayLivery step the answer goes out at once: the step cannot be taken back, so neither can the answer
    if not chats.enqueue_send(draft_id, text, now=now == "1" or bool(step)):
        return back(f"/chats/{d.conversation_id}?msg={quote(t('Not sent: the draft is not open any more or empty'), safe='')}")
    _settle_draft(d, "✅ " + t("Sent in the app"))
    if step:
        try:
            await pl.start_action(d.conversation_id, step, pl_amount)
        except pl.PayLiveryError as e:
            return back(f"/chats/{d.conversation_id}?msg={quote(t('Answer sent, but the PayLivery step did not start: {error}', error=e), safe='')}")
    return back(f"/chats/{d.conversation_id}#draft-{draft_id}")


@app.post("/chats/{conversation_id}/vinted-offer/{step}")
async def chat_vinted_offer(conversation_id: str, step: str, amount: str = Form("")):
    """A price step of a Vinted chat (accept, decline, counter-offer, offer, reserve), done on Vinted at once."""
    try:
        msg = await _vinted_offers_svc.run(conversation_id, step, amount or None)
    except _vinted_offers_svc.OfferError as e:
        msg = str(e)
    return back(f"/chats/{conversation_id}?msg={quote(msg, safe='')}#vinted-offer")


@app.post("/drafts/{draft_id}/revise")
async def draft_revise(draft_id: int, instruction: str = Form("")):
    """One of Claude's alternative decisions (buttons under the draft): revised in the background like a redraft."""
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None:
        raise HTTPException(404)
    chats.enqueue_revise(draft_id, instruction)
    return back(f"/chats/{d.conversation_id}")


@app.post("/drafts/{draft_id}/mark-sent")
async def draft_mark_sent(draft_id: int, text: str = Form("")):
    d = chats.mark_sent(draft_id, text or None)
    _settle_draft(d, "✅ " + t("Marked as sent in the app"))
    return back(f"/chats/{d.conversation_id}")


@app.post("/drafts/{draft_id}/discard")
async def draft_discard(draft_id: int):
    with session() as db:
        d = db.get(Draft, draft_id)
    chats.discard_draft(draft_id, by_owner=True)
    _settle_draft(d, "🗑 " + t("Discarded in the app"))
    return back(f"/chats/{d.conversation_id}")


# ------------------------------------------------------------------ my ads
_MYADS_FILTERS = ("show_active", "show_inactive", "show_sold", "show_hidden")
templates.env.globals.update(stock_of=stock.numbers, stock_text=stock.stock_text,   # ads with a quantity (myads.html)
                             sale_panel=sold.panel)                                 # the sale panel of the open ad


def _myads_back(request: Request, msg: str = "", anchor: str = "", form: Any = None) -> RedirectResponse:
    """Back to My ads with the filters the list was showing: from the posted form, else from the page that posted."""
    page = request.headers.get("hx-current-url") or request.headers.get("referer") or ""   # htmx sends its page
    src = form if form is not None and any(form.get(k) for k in _MYADS_FILTERS) else dict(parse_qsl(urlsplit(page).query))
    keep = "".join(f"&{k}=1" for k in _MYADS_FILTERS if src.get(k))
    ad = anchor[4:] if anchor.startswith("#ad-") else str((form.get("ad") if form is not None else None) or src.get("ad") or "")
    if ad:
        keep += f"&ad={quote(ad, safe='')}"   # the ad whose detail is open (myads.html list -> detail)
    return back(f"/myads?msg={quote(msg, safe='')}{keep}{anchor}")


@app.get("/myads", response_class=HTMLResponse)
async def myads_page(request: Request, msg: str | None = None, show_hidden: bool = False, show_sold: bool = False,
                     show_inactive: bool = False, show_active: bool = False):
    with session() as db:
        empty = db.exec(select(MyAd).limit(1)).first() is None
    if empty and get_session().logged_in:
        try:
            await my_ads.import_from_account()
        except (NotLoggedIn, SessionUnavailable, ValueError) as e:
            msg = msg or t("Could not load ads: {error}", error=e)
    with session() as db:   # deleted on willhaben: gone from the list (the row stays for old chats and the sold workflow)
        rows = list(db.exec(select(MyAd).where(MyAd.status != "deleted")
                            .order_by(col(MyAd.end_date).desc().nulls_last())))   # most days left first
    rows = [a for a in rows if not a.replaced_by]   # a renewed ad's old copy: its chats use the new one (current_ad)
    hidden = [a for a in rows if a.hidden]
    ads = rows if show_hidden else [a for a in rows if not a.hidden]
    # sections: active on top, then expired (ran out recently, always shown: they need renewing), then the rest;
    # sold / given away and inactive (deactivated, expired before the cutoff) ads are hidden unless asked for
    # The filter chips toggle three groups on and off: Active (active + recently expired: they need renewing),
    # Deactivated (deactivated, expired before the cutoff), Sold / reserved (sold, given away, reserved).
    # Nothing switched on: Active.
    cutoff = int(get_setting("myads_expired_days") or 0)
    reserved = lambda a: a.status == "reserved" or a.sale_status == "reserved"   # noqa: E731
    sold = [a for a in ads if my_ads.is_sold(a) or reserved(a)]
    # a draft only on Vinted is still to be dealt with (publish it, or put it on willhaben): with the active ones,
    # not hidden under "Deactivated" (10-28: "I don't see Vinted only ads")
    active = [a for a in ads if a not in sold and (a.status == "active" or a.status == "draft")]
    expired = [a for a in ads if a not in sold and my_ads.is_expired(a, cutoff)]
    inactive = [a for a in ads if a not in sold and a not in active and a not in expired]
    show_active = show_active or not (show_inactive or show_sold)
    groups = [g for g in ((t("Active ({n})", n=len(active)), active if show_active else []),
                          (t("Expired ({n})", n=len(expired)), expired if show_active else []),
                          (t("Deactivated ({n})", n=len(inactive)), inactive if show_inactive else []),
                          (t("Sold / reserved ({n})", n=len(sold)), sold if show_sold else [])) if g[1]]
    sections = {}    # list index -> section heading; only when there is more than one section
    if len(groups) > 1:
        i = 0
        for title, group in groups:
            sections[i] = title
            i += len(group)
    ads = [a for _, group in groups for a in group]
    # the open ad (?ad=) stays open even when the chips hide its group, e.g. right after it was deactivated
    wanted = request.query_params.get("ad")
    cur_ad = next((a for a in rows if wanted and str(a.ad_id) == wanted), None)
    return render(request, "myads.html", ads=ads, msg=msg, myadverts_url=my_ads.MYADVERTS_URL, cur_ad=cur_ad,
                  show_active=show_active, active_count=len(active) + len(expired),
                  hidden_count=len(hidden), show_hidden=show_hidden, sold_count=len(sold), show_sold=show_sold,
                  inactive_count=len(inactive), show_inactive=show_inactive, sections=sections,
                  vinted_ok=site_accounts.state("vinted")["logged_in"] is not False)   # unknown: try it


def _hx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


@app.post("/myads/check")
async def myads_check_all(request: Request):
    my_ads.start_update()
    if _hx(request):        # the header shows the progress, the list refreshes itself when it is done
        return Response(status_code=204)
    return _myads_back(request, t("Update from willhaben started – it runs in the background"))


@app.post("/myads/{ad_id}/auto-renew")
async def myads_auto(request: Request, ad_id: str, on: str = Form("0")):
    my_ads.set_auto_renew(ad_id, on == "1")
    if request.headers.get("x-autosave"):          # My ads saves switches as you flip them (no reload)
        return JSONResponse({"ok": True})
    return _myads_back(request)


@app.post("/myads/{ad_id}/auto-reply")
async def myads_auto_reply(request: Request, ad_id: str, on: str | None = Form(None), instructions: str = Form(""),
                           min_price: str = Form("")):
    error = None
    with session() as db:
        row = db.get(MyAd, ad_id)
        was_on = bool(row and row.auto_reply)
    try:
        floor = min_price_svc.parse_form(min_price)     # empty = no floor
        my_ads.set_auto_reply(ad_id, bool(on), instructions, floor)
    except ValueError:  # bad floor: still save on/off + instructions, keep the stored floor
        error = t('Minimum price must be a number, e.g. 25 or 25,50')
        my_ads.set_auto_reply(ad_id, bool(on), instructions)
    if request.headers.get("x-autosave"):          # auto-save while typing: no reload, no reply run per keystroke
        if on and not was_on:
            task = asyncio.create_task(chats.process_pending_auto(ad_id))
            _background.add(task)
            task.add_done_callback(_background.discard)
        return JSONResponse({"ok": error is None, "error": error})
    if on:  # answer buyers who are already waiting, in the background (Claude takes a few seconds)
        task = asyncio.create_task(chats.process_pending_auto(ad_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    msg = error or (t('Automatic replies on') if on else t('Automatic replies off'))
    return _myads_back(request, msg, f"#ad-{ad_id}")


@app.post("/myads/{ad_id}/quantity")
async def myads_quantity(request: Request, ad_id: str, quantity: str = Form("")):
    """Pieces offered in the ad (empty = one item); saved as you type (X-Autosave)."""
    raw = quantity.strip()
    try:
        my_ads.set_quantity(ad_id, int(raw) if raw.isdigit() else (None if not raw else 0))
        error = None
    except ValueError as e:
        error = str(e)
    if request.headers.get("x-autosave"):
        return JSONResponse({"ok": error is None, "error": error})
    return _myads_back(request, error or t("Changes saved"), f"#ad-{ad_id}")


@app.get("/myads/{ad_id}/edit", response_class=HTMLResponse)
async def myads_edit_page(request: Request, ad_id: str, msg: str | None = None):
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    try:
        cur = await my_ads.load_for_edit(ad_id)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        return back(f"/myads?msg={quote(t('Could not load the ad from willhaben: {error}', error=e), safe='')}")
    unsaved = (row.pending_payload or {}).get("unsaved") if not row.pending_action else None
    if unsaved:     # a save that failed or was interrupted: its text comes back instead of willhaben's
        cur = {**cur, **unsaved}
    return render(request, "myad_edit.html", a=row, cur=cur, msg=msg, unsaved=bool(unsaved))


@app.get("/myads/{ad_id}/description", response_class=HTMLResponse)
async def myads_description(request: Request, ad_id: str, refresh: bool = False):
    """My ads → the ad's panel: the ad as buyers see it (price, photos, delivery, text; anonymous, cached briefly;
    `refresh`: the "Reload" button, fetched anew)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    error = d = None
    try:
        d = await get_public().get_detail(ad_id, refresh=refresh)
    except WillhabenError as e:
        error = t("Could not load the ad from willhaben: {error}", error=e)
    return templates.TemplateResponse(request, "_myad_description.html", {"a": row, "d": d, "error": error})


@app.post("/myads/{ad_id}/edit/ai", response_class=HTMLResponse)
async def myads_edit_ai(request: Request, ad_id: str, heading: str = Form(""), description: str = Form(""),
                        price: str = Form(""), instruction: str = Form("")):
    """Claude's proposal goes back into the form, unsaved; the user checks it and saves to willhaben."""
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    try:
        value = min_price_svc.parse_form(price.replace("€", ""))    # 12.5, 12,50 and 1.250,50 alike
    except ValueError:
        value = None
    cur = {"heading": heading, "description": description, "price": value}
    form = await request.form()
    try:                               # answers to the questions of the last proposal, as the instruction
        asked = json.loads(str(form.get("questions") or "[]"))
    except ValueError:
        asked = []
    answers = {k[2:]: str(v) for k, v in form.items() if k.startswith("q_") and str(v).strip()}
    answers.update({k[2:]: str(v) for k, v in form.items() if k.startswith("t_") and str(v).strip()})
    if from_answers := ad_creator.answers_instruction(asked if isinstance(asked, list) else [], answers):
        instruction = f"{instruction.strip()}\n\n{from_answers}".strip()
    try:
        prop = await my_ads.ai_revise(ad_id, heading, description, value, instruction)
    except LLMError as e:   # shown inside the part htmx swaps (#edit-main), not in the page head
        return render(request, "myad_edit.html", a=row, cur=cur, instruction=instruction,
                      ai_error=t("Claude could not revise the ad: {error}", error=e))
    return render(request, "myad_edit.html", a=row, instruction=instruction, proposal=prop,
                  cur={"heading": prop["heading"], "description": prop["description"], "price": value})


@app.post("/myads/{ad_id}/edit/geizhals", response_class=HTMLResponse)
async def myads_edit_geizhals(ad_id: str, request: Request, heading: str = Form(""), description: str = Form(""),
                              price: str = Form(""), gh_action: str = Form("search"), gh_query: str = Form(""),
                              gh_url: str = Form("")):
    """The Geizhals spec sheet of the editor, like in ad creation: search a product, put its sheet into the text
    (replacing the one there) or remove it. Only the form changes; saving is "Save on willhaben"."""
    with session() as db:
        row = db.get(MyAd, ad_id)
    if row is None:
        raise HTTPException(404)
    try:
        value = ad_creator.parse_price(price)
    except ValueError:
        value = None
    cur = {"heading": heading, "description": description.replace("\r\n", "\n"), "price": value}
    gh: dict[str, Any] = {"query": gh_query.strip()}
    try:
        if gh_action == "remove":
            cur["description"] = geizhals.with_spec_block(cur["description"], "")
        elif gh_action == "use" and gh_url.strip():
            prod = await geizhals.product(gh_url.strip())
            block = geizhals.spec_block(asdict(prod))
            if not block:
                gh["error"] = t("No spec sheet on the product page.")
            else:
                cur["description"] = geizhals.with_spec_block(cur["description"], block)
            gh["match"] = asdict(prod)
        elif gh["query"]:
            gh.update(await geizhals.lookup(gh["query"]) or {})
    except geizhals.GeizhalsError as e:
        gh["error"] = str(e)
    return render(request, "myad_edit.html", a=row, cur=cur, gh=gh)


@app.post("/myads/{ad_id}/edit")
async def myads_edit_save(ad_id: str, heading: str = Form(...), description: str = Form(...), price: str = Form("")):
    """Background job (lane ads): back to the ad, which shows "Saving…" until willhaben has it."""
    from app.willhaben.insert import InsertError
    try:
        value = min_price_svc.parse_form(price.replace("€", ""))    # 12.5, 12,50 and 1.250,50 alike
    except ValueError:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Invalid price'), safe='')}")
    try:
        queued = my_ads.enqueue_edit(ad_id, heading, description, value)
    except InsertError as e:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Saving failed: {error}', error=e), safe='')}")
    if not queued:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Nothing queued: these ads are already being processed'), safe='')}")
    return back(f"/myads?ad={quote(ad_id, safe='')}&msg={quote(t('Saving on willhaben in the background'), safe='')}")


@app.post("/myads/{ad_id}/price")
async def myads_price(request: Request, ad_id: str, price: str = Form("")):
    """The price alone, from the ad's panel (title and text stay): a background job, the panel shows "Saving…"."""
    try:
        value = min_price_svc.parse_form(price.replace("€", ""))
    except ValueError:
        value = None
    if value is None:
        return _myads_back(request, t("Invalid price"), f"#ad-{ad_id}")
    if not my_ads.enqueue_price(ad_id, value):
        return _myads_back(request, t("Nothing queued: these ads are already being processed"), f"#ad-{ad_id}")
    return _myads_back(request, "", f"#ad-{ad_id}")


@app.post("/myads/{ad_id}/photos")
async def myads_photos_save(ad_id: str, order: list[str] = Form(default=[]),
                            photos: list[UploadFile] = File(default=[]), new_first: str = Form("")):
    """Photos in the order shown on the edit page; new uploads go to the start or the end."""
    from app.willhaben.insert import InsertError
    files = [(p.filename or "foto.jpg", await p.read()) for p in photos if p.filename]
    new = [f"new:{i}" for i in range(len(files))]
    order = (new + order) if new_first else (order + new)
    try:
        queued = my_ads.enqueue_photos(ad_id, order, files)     # background job: uploads one after another
    except InsertError as e:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Saving the photos failed: {error}', error=e), safe='')}#photos")
    if not queued:
        return back(f"/myads/{ad_id}/edit?msg={quote(t('Nothing queued: these ads are already being processed'), safe='')}#photos")
    return back(f"/myads?ad={quote(ad_id, safe='')}&msg={quote(t('Saving the photos on willhaben in the background'), safe='')}")


@app.post("/myads/bulk/{action}")
async def myads_bulk(request: Request, action: str):
    if action not in ("activate", "deactivate", "delete", "hide", "unhide", "vinted", "vinted-sync"):
        raise HTTPException(404)
    form = await request.form()
    ids = [str(i) for i in form.getlist("ids") if str(i).isdigit()]
    if not ids:
        return _myads_back(request, t("No ads selected"), form=form)
    if action in ("vinted", "vinted-sync"):   # the selected ads on Vinted: drafts for the new ones / one sync job
        from app.services import crosspost
        if action == "vinted":
            n = crosspost.enqueue_many(ids)
            return _myads_back(request, "" if n else t("All selected ads are on Vinted already"), form=form)
        n = crosspost.schedule_sync_many(ids)
        return _myads_back(request, t("Syncing {n} ads with Vinted", n=n) if n
                           else t("None of the selected ads is on Vinted"), form=form)
    if action in ("hide", "unhide"):          # only this list: nothing happens on willhaben, so no queue
        for ad_id in ids:
            my_ads.set_hidden(ad_id, action == "hide")
        msg = t("Hidden from the list (still online on willhaben): {n}", n=len(ids)) if action == "hide" \
            else t("Shown in the list again: {n}", n=len(ids))
        return _myads_back(request, msg, form=form)
    return _queued(request, my_ads.enqueue(action, ids), form)


def _queued(request: Request, n: int, form: Any = None) -> RedirectResponse:
    """The rows show what is queued (with a cancel while it waits): only a refusal needs words."""
    return _myads_back(request, "" if n else t("Nothing queued: these ads are already being processed"), form=form)


@app.get("/myads/cleanup", response_class=HTMLResponse)
async def myads_cleanup_page(request: Request):
    try:
        cands = await my_ads.cleanup_candidates()
    except (NotLoggedIn, SessionUnavailable, WillhabenError, ValueError) as e:
        return back(f"/myads?msg={quote(t('Could not load ads: {error}', error=e), safe='')}")
    with session() as db:   # already queued (a clean-up or delete still running): not offered twice
        pending = set(db.exec(select(MyAd.ad_id).where(col(MyAd.pending_action).is_not(None))))
    return render(request, "myads_cleanup.html", cands=cands, pending=pending)


@app.post("/myads/cleanup")
async def myads_cleanup(request: Request):
    """Runs as a background job (the header shows the progress); the picked ads are marked in the list at once."""
    ids = [str(i) for i in (await request.form()).getlist("ids") if str(i).isdigit()]
    if not ids:
        return back(f"/myads?msg={quote(t('No ads selected'), safe='')}")
    n = my_ads.enqueue_cleanup(ids)
    return back(f"/myads?msg={quote(t('Clean-up of {n} ads started – it runs in the background', n=n), safe='')}")


@app.post("/myads/{ad_id}/vinted")
async def myads_vinted(request: Request, ad_id: str):
    """The own ad as a draft on Vinted (services/crosspost.py), as a background job."""
    from app.services import crosspost
    return _queued(request, 1 if crosspost.enqueue(ad_id) else 0)


@app.post("/myads/{ad_id}/willhaben")
async def myads_to_willhaben(request: Request, ad_id: str):
    """An ad only on Vinted onto willhaben: its own text and photos, willhaben's category, published as a job."""
    from app.services import crosspost
    return _queued(request, 1 if crosspost.enqueue_willhaben(ad_id) else 0)   # straight online, as a job


@app.post("/agents/{agent_id}/refine", response_class=HTMLResponse)
async def agent_refine(request: Request, agent_id: int):
    """"Refine with my swipes": Claude's proposal for the agent's instruction, to take into the form or leave."""
    from app.services import taste
    error = proposal = None
    try:
        proposal = await taste.suggest(agent_id)
    except ValueError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001 - Claude down, over the limit: said in the panel
        log.warning("refining agent %s failed: %s", agent_id, e)
        error = t("Claude did not answer: {error}", error=e)
    return templates.TemplateResponse(request, "_agent_refine.html",
                                      {"agent_id": agent_id, "p": proposal, "error": error})


@app.post("/myads/{ad_id}/paylivery")
async def myads_paylivery(request: Request, ad_id: str):
    """PayLivery for an own ad that has none (the delivery step of willhaben's form), as a background job."""
    from app.services import crosspost
    if not ad_id.isdigit():
        raise HTTPException(400)
    return _queued(request, 1 if crosspost.enqueue_paylivery(ad_id) else 0)


@app.post("/myads/{ad_id}/vinted/publish")
async def myads_vinted_publish(request: Request, ad_id: str):
    from app.services import crosspost
    return _queued(request, 1 if crosspost.enqueue_publish(ad_id) else 0)


@app.post("/myads/{ad_id}/vinted/sync")
async def myads_vinted_sync(request: Request, ad_id: str):
    from app.services import crosspost
    crosspost.schedule_sync(ad_id)
    return _myads_back(request, "")


@app.post("/myads/{ad_id}/deactivate")
async def myads_deactivate(request: Request, ad_id: str):
    return _queued(request, my_ads.enqueue("deactivate", [ad_id]))


@app.post("/myads/{ad_id}/reopen")
async def myads_reopen(request: Request, ad_id: str, available: str = Form("")):
    """A sold ad with pieces left: the count corrected and online again (background job)."""
    raw = available.strip()
    try:
        queued = my_ads.reopen(ad_id, int(raw) if raw.isdigit() else 0)
    except ValueError as e:
        return _myads_back(request, str(e), f"#ad-{ad_id}")
    return _queued(request, int(queued))


@app.post("/myads/{ad_id}/delete")
async def myads_delete(request: Request, ad_id: str):
    return _queued(request, my_ads.enqueue("delete", [ad_id]))


LIVE_EVENTS = 5      # notices one poll brings at most (a page that slept long gets the newest ones)
PS_STEPS = {"planning": "Claude plans the searches", "searching": "Searching the sites",
            "ranking": "Claude ranks the offers", "photos": "Claude checks the photos",
            "prices": "Looking up new prices on Geizhals"}


def _product_search_jobs() -> dict[str, list[dict[str, Any]]]:
    """The Claude product search among the background jobs. It runs as its own task (product_search.py), so its
    state comes from its row: running, waiting for answers, or finished within the last minutes. The id carries the
    status, so a search that waited for answers and then finished brings a second notice."""
    from app.services import product_search

    run = product_search.running()
    now = utcnow()
    active, recent = [], []
    for row in product_search.recent(3):
        text = row.description if len(row.description) <= 50 else row.description[:49] + "…"
        view = {"id": f"ps-{row.id}", "kind": "product-search", "label": t("Product search: „{text}“", text=text),
                "lane": "claude", "status": "running", "step": "", "current": None, "done": 0, "total": 0, "result": "",
                "problems": 0, "link": f"/deals?search={row.id}#ps-result", "quiet": False, "finished_at": None}
        if run is not None and row.id == run.id:
            prog = (row.progress or {}) if row.status == "searching" else {}
            active.append({**view, "step": t(PS_STEPS.get(row.status, "Searching the sites")),
                           "done": int(prog.get("done") or 0), "total": int(prog.get("total") or 0)})
            continue
        at = row.finished_at or (row.started_at if row.status == "asking" else None)
        at = at.replace(tzinfo=timezone.utc) if at and at.tzinfo is None else at
        if at is None or (now - at).total_seconds() >= jobs.SHOW_FINISHED_S or row.status not in ("done", "asking", "error"):
            continue
        result = {"done": t("{n} offers", n=len(row.results or [])), "error": row.error or "",
                  "asking": t("Claude has questions – answer them to go on")}[row.status]
        recent.append({**view, "id": f"ps-{row.id}-{row.status}", "status": "failed" if row.status == "error" else "done",
                       "result": result, "finished_at": at.isoformat()})
    return {"active": active, "recent": recent}


jobs.add_source(_product_search_jobs)


def _live_cursor() -> int:
    with session() as db:
        return db.exec(select(func.max(EventLog.id))).one() or 0


def _live_event(e: EventLog) -> dict[str, Any]:
    return {"id": e.id, "kind": "pay" if e.kind == "paylivery" else e.kind,
            "title": t(INBOX_KINDS.get(e.kind, "Notification")), "text": e.message, "link": event_link(e)}


@app.get("/jobs")
async def jobs_status(since: int | None = None):
    """Background jobs for the jobs indicator, plus every element still waiting for one (spinners on the page):
    keys "ad:<id>" (own ad action), "draft:<id>" (chat message being sent), "addraft:<id>" (ad being published),
    "refining:<id>" (Claude revises an ad draft), "preparing:<id>" (willhaben categories for an ad draft),
    "drafting:<conversation>" (Claude writes an answer), "style" (Claude derives the writing style, Settings → My rules).
    Every open page also asks now and then for what happened meanwhile: the sidebar badges ("counts") and the inbox
    events after `since` (its last known event id), shown as notices – not the ones sent without a push. Without
    `since` only "last_event", the page's starting point."""
    with session() as db:
        pending = [f"ad:{i}" for i in db.exec(select(MyAd.ad_id).where(col(MyAd.pending_action).is_not(None)))]
        pending += [f"draft:{i}" for i in db.exec(select(Draft.id).where(Draft.status == "sending"))]
        pending += [f"addraft:{i}" for i in db.exec(select(AdDraft.id).where(AdDraft.publishing == True))]  # noqa: E712
        pending += [f"refining:{i}" for i in db.exec(select(AdDraft.id).where(AdDraft.refining == True))]  # noqa: E712
        pending += [f"drafting:{i}" for i in db.exec(select(Conversation.id).where(Conversation.drafting == True))]  # noqa: E712
        pending += [f"sim:{i}" for i in db.exec(select(SimChat.id).where(col(SimChat.busy).is_not(None)))]
        pending += jobs.current_keys("simreview:")         # Claude reviews a system prompt (chat simulator)
        pending += jobs.current_keys("preparing:")         # willhaben categories for an ad draft (batch table)
        pending += jobs.current_keys("style")              # Claude derives the writing style (app/services/style.py)
        pending += jobs.current_keys("agent:")             # a search agent's check (scheduled or "Check now")
        pending += jobs.current_keys("shipping:")          # Claude prepares a parcel from a chat
        pending += jobs.current_keys("purchase:")          # a purchase order about to contact the seller
        pending += jobs.current_keys("chatreload:")        # a chat reloading its history (Reload button)
        pending += [f"batch:{i}" for i in db.exec(select(AdBatch.id).where(col(AdBatch.status).in_(("sorting", "creating"))))]
        last = _live_cursor()
        new = [] if since is None or since >= last else list(db.exec(
            select(EventLog).where(EventLog.id > since, col(EventLog.quiet).is_not(True))
            .order_by(col(EventLog.id).desc()).limit(LIVE_EVENTS)))
    return {**jobs.snapshot(), "pending": pending, "counts": _nav_counts(), "last_event": last,
            "events": [_live_event(e) for e in reversed(new)]}


@app.post("/jobs/{job_id}/cancel")
async def job_cancel(job_id: int):
    """Take a queued job back (the ✕ in the jobs popover or next to the element waiting for it): it never runs."""
    return {"ok": jobs.cancel(job_id)}


# ------------------------------------------------------------------ sold / reserved workflow (app/services/sold.py)
def _sold_url(ad_id: str, kind: str, buyer: str | None, msg: str | None = None, anchor: str = "") -> str:
    q = f"kind={sold.kind_of(kind)}" + (f"&buyer={quote(buyer, safe='')}" if buyer else "")
    return f"/sold/{quote(ad_id, safe='')}?{q}" + (f"&msg={quote(msg, safe='')}" if msg else "") + anchor


@app.get("/sold/{ad_id}", response_class=HTMLResponse)
async def sold_page(request: Request, ad_id: str, kind: str = "sold", buyer: str | None = None, msg: str | None = None):
    return _sold_render(request, ad_id, kind, buyer, msg)


def _sold_render(request: Request, ad_id: str, kind: str, buyer: str | None, msg: str | None,
                 reasons: list | None = None) -> HTMLResponse:
    kind = sold.kind_of(kind)
    ad_id = sold.current_id(ad_id)    # a chat of a renewed ad links the old id: the page works on the live copy
    convs = sold.seller_conversations(ad_id)
    buyer_conv = next((c for c in convs if c.id == buyer), None)
    ad = sold.ad_row(ad_id)
    numbers = stock.numbers(ad)       # an ad with a quantity: the stock block, the sale's pieces and price
    return render(request, "sold.html", ad_id=ad_id, kind=kind, ad=ad, title=sold.title_of(ad_id),
                  convs=convs, buyer=buyer_conv, others=sold.others(ad_id, buyer_conv.id if buyer_conv else None),
                  panel=sold.panel(ad_id, buyer_conv.id if buyer_conv else None),
                  reasons=reasons or sold.pending_reasons(ad_id), msg=msg,   # reasons a background step left behind
                  stock=numbers, sales=stock.sales_of(ad_id) if numbers else [], names={c.id: c.partner_name for c in convs},
                  agreed_price=sold.agreed_price(buyer_conv.id if buyer_conv else None) if numbers else None)


def _sale_back(next_url: str, ad_id: str, kind: str, buyer: str | None, msg: str | None = None, anchor: str = "") -> RedirectResponse:
    """Back to the page the sale panel sits on (`next`: the chat, My ads; only paths of this app), else the sale page."""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return back(next_url + (("&" if "?" in next_url else "?") + f"msg={quote(msg, safe='')}" if msg else ""))
    return back(_sold_url(ad_id, kind, buyer, msg, anchor))


@app.post("/sold/{ad_id}/willhaben", response_class=HTMLResponse)
async def sold_willhaben(request: Request, ad_id: str, kind: str = Form("sold"), buyer: str = Form(""),
                         undo: str = Form(""), reason_id: str = Form(""), next: str = Form("")):
    """The willhaben step alone, as a background job (lane ads): the sale panel's "Mark on willhaben" when the one-click
    sale could not queue it, and the confirm with the sold reason willhaben asked for (the job leaves the reasons
    on the ad, the panel offers them)."""
    if sold.kind_of(kind) == "sold" and undo:
        return _sale_back(next, ad_id, kind, buyer or None, t("willhaben cannot undo \"sold\"."), "#sale-actions")
    if sold.kind_of(kind) == "sold" and sold.pieces_left(ad_id):     # a stale page: on_willhaben refuses it too
        return _sale_back(next, ad_id, kind, buyer or None,
                          t("Pieces left: record the sale instead – willhaben is marked sold with the last piece"), "#sale-actions")
    queued = sold.enqueue_willhaben(ad_id, kind, undo=bool(undo), reason_id=reason_id or None)
    return _sale_back(next, ad_id, kind, buyer or None, None if queued else t("Nothing queued: these ads are already being processed"),
                      "#sale-actions")


@app.post("/sold/{ad_id}/reserve")
async def sold_reserve(ad_id: str, buyer: str = Form(""), next: str = Form("")):
    """One click: reserved here (automatic answers off, the buyer noted) and on willhaben (background job)."""
    try:
        sold.reserve(ad_id, buyer or None)
    except ValueError as e:
        return _sale_back(next, ad_id, "reserved", buyer or None, str(e), "#sale-actions")
    return _sale_back(next, ad_id, "reserved", buyer or None)


@app.post("/sold/{ad_id}/unreserve")
async def sold_unreserve(ad_id: str, buyer: str = Form(""), next: str = Form("")):
    """One click: the reservation goes here and, when willhaben shows it, there too (background job)."""
    sold.unreserve(ad_id)
    return _sale_back(next, ad_id, "reserved", buyer or None)


@app.post("/sold/{ad_id}/sell")
async def sold_sell(ad_id: str, buyer: str = Form(""), next: str = Form(""), auto_off: str = Form("1"),
                    pieces: str = Form(""), price: str = Form(""), reason_id: str = Form("")):
    """One click: the sale is recorded here and, when that sells the ad out, it is marked sold on willhaben
    (background job with a grace period). `pieces`/`price` come with an ad with a quantity."""
    try:
        amount = min_price_svc.parse_form(price)
    except ValueError:
        return _sale_back(next, ad_id, "sold", buyer or None, t("Price must be a number, e.g. 25 or 25,50"), "#sale-actions")
    try:
        sold.sell(ad_id, buyer or None, pieces=int(pieces) if pieces.strip().isdigit() else (0 if pieces.strip() else 1),
                  price=amount, auto_off=auto_off != "0", reason_id=reason_id or None)
    except ValueError as e:
        return _sale_back(next, ad_id, "sold", buyer or None, str(e), "#sale-actions")
    return _sale_back(next, ad_id, "sold", buyer or None)


@app.post("/sold/{ad_id}/notices")
async def sold_notices(request: Request, ad_id: str):
    form = await request.form()
    kind, buyer = str(form.get("kind") or "sold"), str(form.get("buyer") or "") or None
    convs = [str(x) for x in form.getlist("conv")]
    if form.get("mode") == "claude":      # Claude takes a while per chat: background job (lane claude)
        msg = t("Claude writes {n} drafts in the background – nothing is sent", n=sold.enqueue_notice_drafts(ad_id, kind, convs, buyer=buyer))
    else:
        made, errors = await sold.prepare_notices(ad_id, kind, convs, buyer=buyer)
        msg = t("{n} drafts prepared – nothing sent yet", n=made) + ("\n" + "\n".join(errors) if errors else "")
    return back(_sold_url(ad_id, kind, buyer, msg, "#step2"))


@app.post("/sold/{ad_id}/notices/send")
async def sold_notices_send(request: Request, ad_id: str):
    form = await request.form()
    kind, buyer = str(form.get("kind") or "sold"), str(form.get("buyer") or "") or None
    ids = [int(x) for x in form.getlist("send") if str(x).isdigit()]
    if form.get("action") == "discard":
        msg = t("{n} drafts discarded", n=sold.discard_notices(ad_id, ids))
    elif form.get("action") == "send":    # only an explicit "Send": a form without the button never sends
        msg = t("{n} messages queued – they go out in the background, one after another",
                n=sold.send_notices(ad_id, [(i, str(form.get(f"text_{i}") or "")) for i in ids]))
    else:
        msg = t("Nothing done")
    return back(_sold_url(ad_id, kind, buyer, msg, "#step2"))


@app.post("/sold/{ad_id}/finish")
async def sold_finish(ad_id: str, kind: str = Form("sold"), buyer: str = Form(""), auto_off: str | None = Form(None),
                      pieces: str = Form(""), price: str = Form("")):
    """`pieces` and `price` come from an ad with a quantity (sold.html); a single item sends neither (finish counts
    it as one piece). A blank `pieces` is refused like any number out of range, not taken as one."""
    try:
        amount = min_price_svc.parse_form(price)
    except ValueError:
        return back(_sold_url(ad_id, kind, buyer or None, t("Price must be a number, e.g. 25 or 25,50"), "#step3"))
    try:
        row = sold.finish(ad_id, kind, buyer or None, auto_off=bool(auto_off),
                          pieces=int(pieces) if pieces.strip().isdigit() else 0, price=amount)
    except ValueError as e:
        return back(_sold_url(ad_id, kind, buyer or None, str(e), "#step3"))
    if sold.kind_of(kind) == "sold" and row.sale_status != "sold":     # pieces left: the ad stays online
        msg = row.last_action or ""
    else:
        msg = t("Saved: {status}", status=t("sold") if sold.kind_of(kind) == "sold" else t("reserved"))
    return back(_sold_url(ad_id, kind, buyer or None, msg, "#step3"))


@app.post("/sold/{ad_id}/sales/{sale_id}/undo")
async def sold_undo_sale(ad_id: str, sale_id: int, kind: str = Form("sold"), buyer: str = Form("")):
    row = sold.ad_row(ad_id)      # sold out before: open again here, but what the sell-out switched off stays off
    reopened = row is not None and row.sale_status == "sold"
    if not sold.undo_sale(ad_id, sale_id):
        msg = t("Sale not found")
    elif reopened and not row.auto_reply and not row.auto_renew:
        msg = t("Sale undone here (willhaben unchanged); automatic answers and auto-renew stay off until you switch them on again")
    elif reopened and not row.auto_renew:
        msg = t("Sale undone here (willhaben unchanged); auto-renew stays off until you switch it on again")
    elif reopened and not row.auto_reply:
        msg = t("Sale undone here (willhaben unchanged); automatic answers stay off until you switch them on again")
    else:
        msg = t("Sale undone here (willhaben unchanged)")
    return back(_sold_url(ad_id, kind, buyer or None, msg, "#stock"))


@app.post("/sold/{ad_id}/reset")
async def sold_reset(ad_id: str, kind: str = Form("sold"), buyer: str = Form(""), next: str = Form("")):
    sold.reset(ad_id)
    return _sale_back(next, ad_id, kind, buyer or None, None if next else t("Sale status removed here (willhaben unchanged)"), "#step3")


# ------------------------------------------------------------------ new ad
@app.get("/new-ad", response_class=HTMLResponse)
async def new_ad_page(request: Request):
    with session() as db:
        drafts = list(db.exec(select(AdDraft).order_by(col(AdDraft.created_at).desc()).limit(20)))
        batches = list(db.exec(select(AdBatch).order_by(col(AdBatch.created_at).desc()).limit(10)))
        # a published draft's ad as the account list knows it now (online, in review …), not just "published"
        live = {r.ad_id: r for r in db.exec(select(MyAd).where(col(MyAd.ad_id).in_(
            [d.wh_ad_id for d in drafts if d.status == "published" and d.wh_ad_id])))}
    return render(request, "new_ad.html", drafts=drafts, batches=batches, max_batch=ad_batch.MAX_BATCH_IMAGES,
                  live=live)


@app.post("/new-ad")
async def new_ad_create(notes: str = Form(""), photos: list[UploadFile] = File(default=[])):
    """The draft page opens at once; Claude writes the listing as a background job (lane claude)."""
    files = [(p.filename or "", await p.read()) for p in photos if p.filename]
    if not notes.strip() and not files:
        return back("/new-ad")
    draft = ad_creator.enqueue_create(notes, files)
    return back(f"/new-ad/{draft.id}")


@app.get("/new-ad/{draft_id:int}", response_class=HTMLResponse)
async def ad_draft_page(request: Request, draft_id: int, msg: str | None = None):
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    gh = d.geizhals or {}
    return render(request, "ad_draft.html", d=d, msg=msg, insert_url=ad_creator.INSERT_URL, gh=gh,
                  spec_block=geizhals.spec_block(gh.get("match")))


@app.post("/new-ad/{draft_id:int}")
async def ad_draft_save(request: Request, draft_id: int, title: str = Form(...), description: str = Form(...),
                        price: str = Form("")):
    """The draft's fields; saved as you type (X-Autosave: {"ok", "error"}; ad_creator.autosave: price and pieces also
    while Claude revises, no error line on the draft), a plain form still redirects."""
    form = await request.form()
    if request.headers.get("x-autosave"):
        _draft_or_404(draft_id)
        problem = ad_creator.autosave(draft_id, form)
        return JSONResponse({"ok": problem is None, "error": problem})
    d = ad_creator.save_fields(draft_id, title, description, price, _posted_quantity(form))
    problem = ad_creator.save_problem(d, price)
    return back(f"/new-ad/{draft_id}?msg={quote(problem or t('Changes saved'), safe='')}")


@app.post("/new-ad/{draft_id:int}/refine")
async def ad_draft_refine(draft_id: int, instruction: str = Form(...)):
    """Background job (lane claude): the draft shows "Claude is revising…" and swaps itself in when done."""
    _draft_or_404(draft_id)
    if not ad_creator.enqueue_refine(draft_id, instruction):
        return back(f"/new-ad/{draft_id}?msg={quote(ad_creator.refine_refusal(draft_id), safe='')}")
    return back(f"/new-ad/{draft_id}")


@app.post("/new-ad/{draft_id:int}/answers")
async def ad_draft_answers(request: Request, draft_id: int):
    """The draft's questionnaire: a chosen option or own text per question; "skip" just closes it."""
    _draft_or_404(draft_id)
    form = await request.form()
    if form.get("skip"):
        ad_creator.update_draft(draft_id, questions=None)
        return back(f"/new-ad/{draft_id}")
    answers = {k[2:]: str(v) for k, v in form.items() if k.startswith("q_") and str(v).strip()}
    for k, v in form.items():          # own text wins over a chosen option
        if k.startswith("t_") and str(v).strip():
            answers[k[2:]] = str(v)
    if not ad_creator.enqueue_answers(draft_id, answers):
        return back(f"/new-ad/{draft_id}?msg={quote(t('Choose or write at least one answer.'), safe='')}")
    return back(f"/new-ad/{draft_id}")


@app.post("/new-ad/{draft_id:int}/retry")
async def ad_draft_retry(draft_id: int):
    """A draft Claude could not write: the same notes and photos once more, as a background job."""
    _draft_or_404(draft_id)
    ad_creator.enqueue_retry(draft_id)
    return back(f"/new-ad/{draft_id}")


@app.post("/new-ad/{draft_id:int}/reconcile")
async def ad_draft_reconcile(draft_id: int):
    """Background job (lane claude) like a revision: the draft shows "Claude is revising…" and swaps itself in."""
    _draft_or_404(draft_id)
    if not ad_creator.enqueue_refine(draft_id, ad_creator.RECONCILE_INSTRUCTION,
                                     label=t("Checked against the Geizhals spec sheet")):
        return back(f"/new-ad/{draft_id}?msg={quote(ad_creator.refine_refusal(draft_id), safe='')}#geizhals")
    return back(f"/new-ad/{draft_id}#geizhals")


@app.post("/new-ad/{draft_id:int}/undo")
async def ad_draft_undo(draft_id: int):
    ad_creator.undo_revision(draft_id)
    return back(f"/new-ad/{draft_id}?msg={quote(t('Previous version restored'), safe='')}")


@app.post("/new-ad/{draft_id:int}/geizhals")
async def ad_draft_geizhals(draft_id: int, url: str = Form(""), query: str = Form("")):
    try:
        if url.strip():
            await ad_creator.choose_geizhals(draft_id, url.strip())
        else:
            await ad_creator.search_geizhals(draft_id, query)
    except geizhals.GeizhalsError as e:
        return back(f"/new-ad/{draft_id}?msg={quote(str(e), safe='')}#geizhals")
    return back(f"/new-ad/{draft_id}#geizhals")


def _posted_quantity(form: Any) -> str | None:
    """The piece count of a draft form; None = the form has no such field (leave the saved count alone), blank = one
    item. Read from the raw form: FastAPI's Form() turns a blank field into a missing one."""
    quantity = form.get("quantity")
    return None if quantity is None else str(quantity)


def _save_posted_fields(draft_id: int, form: Any) -> str | None:
    """Unsaved title/description/price/pieces that came along with a willhaben action: saved first. Returns the
    problem (it is on the draft as d.error too) when the edit was not taken over, e.g. a price that could not be read."""
    if form.get("title") is not None and form.get("description") is not None:
        price = str(form.get("price") or "")
        return ad_creator.save_problem(ad_creator.save_fields(draft_id, str(form["title"]), str(form["description"]),
                                                              price, _posted_quantity(form)), price)
    return None


@app.post("/new-ad/{draft_id:int}/willhaben/prepare")
async def ad_draft_wh_prepare(request: Request, draft_id: int):
    """Read-only: categories and required fields from willhaben's insertion form."""
    _draft_or_404(draft_id)
    form = await request.form()
    _save_posted_fields(draft_id, form)
    try:
        await ad_creator.prepare_willhaben(draft_id, str(form.get("category_query") or "").strip() or None)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return back(f"/new-ad/{draft_id}#willhaben")


@app.post("/new-ad/{draft_id:int}/willhaben/category")
async def ad_draft_wh_category(draft_id: int, code: str = Form(...)):
    """A category picked by hand ("Choose another category") becomes a selectable option in wh_plan."""
    _draft_or_404(draft_id)
    try:
        await ad_creator.add_manual_category(draft_id, code.strip())
    except ValueError as e:
        ad_creator.update_draft(draft_id, error=str(e))
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        ad_creator.update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
    return back(f"/new-ad/{draft_id}#willhaben")


@app.post("/new-ad/{draft_id:int}/willhaben/publish")
async def ad_draft_wh_publish(request: Request, draft_id: int):
    """Publish on willhaben after the user's click + confirm, as a background job; never twice (enqueue_publish and
    publish_willhaben check the status)."""
    _draft_or_404(draft_id)
    form = await request.form()
    if _save_posted_fields(draft_id, form):                  # e.g. an unreadable price: not published with the old one
        return back(f"/new-ad/{draft_id}#willhaben")
    try:
        option = int(str(form.get("option", "")))
    except ValueError:
        option = -1
    prefix = f"o{option}_"
    values = {k[len(prefix):]: str(v) for k, v in form.multi_items() if k.startswith(prefix) and v}
    if not ad_creator.enqueue_publish(draft_id, option, values, shipping=ad_creator.parse_shipping(form)):
        return back(f"/new-ad/{draft_id}#willhaben")       # input error (on the draft) or already on its way
    return back(f"/new-ad/{draft_id}?msg={quote(t('Publishing in the background – you can leave this page'), safe='')}#willhaben")


@app.get("/api/willhaben/categories")
async def api_wh_categories(q: str = "", limit: int = 25):
    """Read-only: leaf categories of willhaben's insertion form matching q (form cached ~1 h in memory)."""
    from app.willhaben import insert
    if len(q.strip()) < 2:
        return {"results": []}
    try:
        form = await insert.cached_form(get_session())
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        return JSONResponse({"results": [], "error": str(e)}, status_code=503)
    return {"results": insert.search_categories(form.get("categoryTree") or {}, q, limit=max(1, min(limit, 50)))}


@app.get("/ad-drafts/{draft_id}/image/{idx}")
async def ad_draft_image(draft_id: int, idx: int):
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if not d or idx >= len(d.image_paths):
        raise HTTPException(404)
    return FileResponse(d.image_paths[idx])


# ------------------------------------------------------------------ batch of ads
@app.post("/new-ad/batch")
async def ad_batch_create(notes: str = Form(""), photos: list[UploadFile] = File(default=[])):
    b = ad_batch.new_batch(notes)
    skipped = 0
    for p in photos:
        if p.filename and not ad_batch.add_image(b.id, p.filename, await p.read()):
            skipped += 1
    if not ad_batch.get(b.id).image_paths:
        ad_batch.delete(b.id)
        return back("/new-ad?msg=" + quote(t("No usable photos in the batch"), safe=""))
    ad_batch.spawn(ad_batch.sort(b.id), batch_id=b.id, label=t("Claude sorts {n} photos", n=len(ad_batch.get(b.id).image_paths)))
    msg = f"?msg={quote(t('{n} photos skipped (format, size or more than {max})', n=skipped, max=ad_batch.MAX_BATCH_IMAGES), safe='')}" \
        if skipped else ""
    return back(f"/new-ad/batch/{b.id}{msg}")


@app.get("/new-ad/batch/{batch_id}", response_class=HTMLResponse)
async def ad_batch_page(request: Request, batch_id: int, msg: str | None = None, unticked: str = ""):
    """unticked: drafts the owner unticked (the page's own refresh, ad_batch.html): they stay unticked."""
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    drafts = ad_batch.drafts_of(b)
    off = {int(x) for x in unticked.split(",") if x.strip().isdigit()}
    selected = {d.id for d in drafts if d.status == "draft" and not d.publishing and d.id not in off} if off else None
    return render(request, "ad_batch.html", b=b, msg=msg, drafts=drafts, unassigned=ad_batch.unassigned(b),
                  pending=ad_batch.pending(b), busy=b.status in ("sorting", "creating"), selected=selected,
                  unticked=sorted(off),
                  preparing=_preparing(), max_images=ad_creator.MAX_IMAGES, parallel=ad_batch.CREATE_PARALLEL)


def _preparing() -> set[int]:
    """Drafts with a "prepare for willhaben" job queued or running."""
    return {int(k.split(":")[1]) for k in jobs.current_keys("preparing:")}


@app.post("/new-ad/batch/{batch_id}/drafts", response_class=HTMLResponse)
async def ad_batch_drafts(request: Request, batch_id: int):
    """The drafts table: every editable row is saved (changes save on their own), then act runs on the ticked rows
    (act "verb") or on one row (act "verb:<draft id>"). The answer only swaps what changed (htmx out-of-band)."""
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    form = await request.form()
    verb, _, ref = str(form.get("act") or request.query_params.get("act") or "save").partition(":")
    ticked = [int(v) for v in form.getlist("sel") if str(v).isdigit()]
    rows = ad_batch.table_rows(form)
    if verb == "category" and ref.isdigit() and form.get("code"):   # "Choose another category" search in a row
        rows.setdefault(int(ref), {})["cat"] = str(form["code"])
    redraw, unsaved = await ad_batch.save_table(batch_id, rows)
    msg, tone = t("Saved"), "ok"
    if verb == "category" and ref.isdigit():
        redraw.append(int(ref))
    elif verb != "save":
        ids = [int(ref)] if ref.isdigit() else ticked
        msg, tone = ad_batch.run_action(batch_id, verb, ids, str(form.get(f"{ref}.instruction" if ref else "instruction") or ""),
                                        unsaved)
        redraw += ids
    drafts = ad_batch.drafts_of(ad_batch.get(batch_id))
    return render(request, "_batch_oob.html", b=b, drafts=drafts, redraw=[d for d in drafts if d.id in set(redraw)],
                  selected=set(ticked), preparing=_preparing(), msg=msg, tone=tone)


@app.get("/new-ad/batch/{batch_id}/row/{draft_id}", response_class=HTMLResponse)
async def ad_batch_row(request: Request, batch_id: int, draft_id: int, checked: int | None = None):
    """One row of the drafts table (swapped in when its background job is done). checked: the tick as the page has
    it (an unticked row must not come back ticked and then be published with the others)."""
    b = ad_batch.get(batch_id)
    d = next((d for d in ad_batch.drafts_of(b) if d.id == draft_id), None) if b else None
    if d is None:
        raise HTTPException(404)
    tick = ad_batch.editable(d) if checked is None else bool(checked) and ad_batch.selectable(d)
    return render(request, "_batch_row.html", b=b, d=d, wrap=True, checked=tick, preparing=d.id in _preparing())


@app.post("/new-ad/batch/{batch_id}")
async def ad_batch_save(request: Request, batch_id: int):
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    if b.status in ("sorting", "creating"):
        return back(f"/new-ad/batch/{batch_id}?msg=" + quote(t("Claude is still working on this batch"), safe=""))
    form = await request.form()
    order, fields = [], {}
    for key, value in form.multi_items():   # page order = photo order inside each group
        kind, _, ref = key.partition("_")
        if kind == "img" and ref.isdigit():
            order.append((int(ref), str(value)))
        elif kind in ("product", "comment", "include", "quantity"):
            fields.setdefault(ref, {})[kind] = str(value)
    ad_batch.apply_review(batch_id, order, fields)
    action = form.get("action")
    if action == "resort":
        ad_batch.spawn(ad_batch.sort(batch_id, str(form.get("hint") or "")), batch_id=batch_id,
                       label=t("Claude sorts {n} photos", n=len(ad_batch.get(batch_id).image_paths)))
        return back(f"/new-ad/batch/{batch_id}")
    if action == "create":
        if not ad_batch.pending(ad_batch.get(batch_id)):
            return back(f"/new-ad/batch/{batch_id}?msg=" + quote(t("No group selected"), safe=""))
        ad_batch.spawn(ad_batch.create_drafts(batch_id), batch_id=batch_id,
                       label=t("Claude writes {n} drafts", n=len(ad_batch.pending(ad_batch.get(batch_id)))))
        return back(f"/new-ad/batch/{batch_id}")
    return back(f"/new-ad/batch/{batch_id}?msg={quote(t('Changes saved'), safe='')}")


@app.post("/new-ad/batch/{batch_id}/delete")
async def ad_batch_delete(batch_id: int):
    b = ad_batch.get(batch_id)
    if b and b.status not in ("sorting", "creating"):
        ad_batch.delete(batch_id)
    return back("/new-ad")


@app.get("/new-ad/batch/{batch_id}/progress")
async def ad_batch_progress(batch_id: int):
    b = ad_batch.get(batch_id)
    if b is None:
        raise HTTPException(404)
    return {"status": b.status, "drafts": sum(1 for g in b.groups if g.get("draft_id"))}


def _draft_or_404(draft_id: int) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    return d


@app.get("/ad-drafts/{draft_id}/photos.zip")
async def ad_draft_photos_zip(draft_id: int):
    d = _draft_or_404(draft_id)
    return Response(ad_creator.photos_zip(d), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="draft-{d.id}-photos.zip"'})


@app.get("/ad-batches/{batch_id}/image/{idx}")
async def ad_batch_image(batch_id: int, idx: int):
    b = ad_batch.get(batch_id)
    if not b or not 0 <= idx < len(b.image_paths):
        raise HTTPException(404)
    return FileResponse(b.image_paths[idx])


# ------------------------------------------------------------------ session / capture
def _session_back(next_url: str | None, msg: str | None = None) -> RedirectResponse:
    """Back to where a session form was posted from (/session or Settings → Platforms → willhaben), with a message."""
    url = next_url if next_url and next_url.startswith("/") and not next_url.startswith("//") else "/session"
    path, _, frag = url.partition("#")
    if msg:
        path += ("&" if "?" in path else "?") + "msg=" + quote(msg, safe="")
    return back(path + (f"#{frag}" if frag else ""))


@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request, msg: str | None = None, embed: bool = False, site: str | None = None):
    """embed=1: bare page for the remote-view overlay (base.html opens it in a dialog on any page). site=vinted /
    kleinanzeigen: that site's own tab, with its login state only (no willhaben state, no label checkout)."""
    caps = sorted(get_settings().captures_dir.glob("*.json"), reverse=True)[:30]
    site_mode = site_accounts.get(site) if site else None
    if site_mode is not None:                     # what its tab shows right now counts more than an old check
        tab = get_session().site_pages.get(site_mode.id) if get_session().started else None
        site_accounts.seen_in_tab(site_mode.id, tab.url if tab is not None and not tab.is_closed() else None)
    nxt = "/session?embed=1" + (f"&site={site_mode.id}" if site_mode else "") if embed else "/session"
    return render(request, "session.html", captures=[c.name for c in caps], msg=msg, settings=get_settings(),
                  label_form=None if site_mode else label_fill.active(), session_next=nxt, embed=embed,
                  site_mode=site_mode, site_state=site_accounts.state(site_mode.id) if site_mode else None,
                  site_checked=site_accounts.checked_at(site_mode.id) if site_mode else None)


@app.post("/session/site-check/{pid}")
async def session_site_check(pid: str, next: str = Form("/session")):
    """A site's login check from its own overlay (Vinted, Kleinanzeigen)."""
    site = site_accounts.get(pid)
    if site is None:
        raise HTTPException(404)
    st = await site_accounts.check(pid)
    word = t("logged in") if st["logged_in"] else t("unknown") if st["logged_in"] is None else t("not logged in")
    return _session_back(next, f"{site.name}: {word}" + (f" – {st['error']}" if st.get("error") else ""))


@app.post("/session/start")
async def session_start(next: str = Form("/session")):
    sess = get_session()
    if not sess.started:
        await sess.start()
    return _session_back(next)


@app.post("/session/login")
async def session_login(next: str = Form("/session"), site: str = Form("willhaben")):
    """The login page of willhaben, or of Kleinanzeigen / Vinted (site), in the browser window."""
    try:
        if site_accounts.get(site):
            await site_accounts.open_login(site)
        else:
            await get_session().open_login()
    except SessionUnavailable as e:
        return _session_back(next, str(e))
    return _session_back(next, t('Login page opened in the browser window'))


@app.post("/settings/credentials/{site}")
async def save_credentials(site: str, user: str = Form(""), password: str = Form(""), action: str = Form("save")):
    """The login details of willhaben or Vinted (services/credentials.py): save, or forget them."""
    from app.services import credentials
    if site not in credentials.SITES:
        raise HTTPException(404)
    if action == "forget":
        credentials.forget(site)
        msg = t("Login details deleted")
    else:
        try:
            credentials.save(site, user, password)
            msg = t("Login details saved")
        except ValueError:
            msg = t("E-mail and password are needed")
    return back(f"/settings/{site}?msg={quote(msg, safe='')}#s-credentials")


@app.post("/settings/credentials/{site}/login")
async def credentials_login(site: str):
    """"Log in automatically": the site's login page in the remote view, the stored details typed in and sent; the
    overlay stays open for whatever the site asks next (a captcha, a code)."""
    from app.services import credentials, label_fill
    if site not in credentials.SITES or credentials.get(site) is None:
        raise HTTPException(404)
    sess = get_session()
    remote = "1" if site == "willhaben" else site

    async def logged_in() -> bool:
        if site == "willhaben":
            return await sess.check_login()
        return bool((await site_accounts.check(site))["logged_in"])

    try:
        if site == "willhaben":
            await sess.open_login()
            page = sess.ui_page
        else:
            await sess.open_site(site, credentials.LOGIN_PAGES[site])
            page = sess.site_pages[site]
        try:
            await label_fill.dismiss_consent(page)          # a cookie banner over the form
        except Exception:  # noqa: BLE001 - no banner
            pass
        await credentials.type_in(page, site)
        await page.wait_for_timeout(4000)
        ok = await logged_in()
        msg = t("Logged in") if ok else t("Sent – finish the login in the browser window if the site asks for more")
    except SessionUnavailable as e:
        return back(f"/settings/{site}?msg={quote(str(e), safe='')}")
    except Exception as e:  # noqa: BLE001 - no login form: already logged in, or the page shows a captcha/something else
        try:
            already = await logged_in()
        except Exception:  # noqa: BLE001 - check itself failed
            already = False
        if already:
            log.info("automatic login on %s: already logged in, no form to fill", site)
            msg = t("Already logged in")
        else:
            log.info("automatic login on %s stopped: %s", site, type(e).__name__)
            msg = t("The login form was not found – log in in the browser window")
    return back(f"/settings/{site}?remote={remote}&msg={quote(msg, safe='')}")


@app.post("/settings/native/{action}")
async def native_login(action: str, code: str = Form(""), key: str = Form(""), op: str = Form("save")):
    """willhaben's app API (willhaben/native.py): log in with the stored details, enter the e-mail code willhaben
    sent for it, or log out of it (the browser's login stays)."""
    from app.services import credentials
    from app.willhaben import native
    if action == "login":
        cred = credentials.get("willhaben")
        msg = await native.login_now(cred["user"], cred["password"]) if cred else t("Store the login details first")
    elif action == "code":
        msg = await native.code_now(code)
    elif action == "logout":
        native.get_client().forget()
        msg = t("Logged out of the app API")
    elif action == "key":
        if native.app_key()[1] == "env":
            msg = t("The app key comes from WILLHABEN_APP_KEY in .env – change it there")
        elif op == "forget":
            set_setting("willhaben_app_key", "")
            msg = t("App key deleted – the browser does everything")
        elif key.strip():
            set_setting("willhaben_app_key", key.strip())
            native.get_client().st.app_token = ""      # the next app token is signed with this key
            msg = t("App key saved")
        else:
            msg = t("App key unchanged")
    elif action == "selftest":
        res = await native.selftest(get_session())
        msg = t("Read test: {ok} of {n} worked, {api} through the app API",
                ok=sum(r["ok"] for r in res), n=len(res), api=sum(r["via"] == "api" for r in res))
    else:
        raise HTTPException(404)
    return back(f"/settings/willhaben?msg={quote(msg, safe='')}#s-native")


@app.post("/settings/account/{pid}/login")
async def site_account_login(pid: str):
    """Kleinanzeigen, Vinted: their login page in the remote view (the overlay opens with it)."""
    if not site_accounts.get(pid):
        raise HTTPException(404)
    try:
        await site_accounts.open_login(pid)
    except SessionUnavailable as e:
        return back(f"/settings/{pid}?msg={quote(str(e), safe='')}")
    return back(f"/settings/{pid}?remote={pid}")     # the overlay of this site's own tab, without willhaben's state


@app.post("/settings/account/{pid}/check")
async def site_account_check(pid: str):
    if not site_accounts.get(pid):
        raise HTTPException(404)
    st = await site_accounts.check(pid)
    msg = t("Logged in") if st["logged_in"] else st["error"] or t("Not logged in")
    return back(f"/settings/{pid}?msg={quote(msg, safe='')}")


@app.post("/session/check")
async def session_check(next: str = Form("/session")):
    """willhaben's login, and that of the other sites with an account (switched on): each in a hidden tab."""
    sess = get_session()
    ok = await sess.check_login()          # the app interface's login (no browser needed); else the browser's
    parts = [f"willhaben: {t('logged in') if ok else t('not logged in')}"]
    if sess.started:
        for site in site_accounts.SITES.values():
            if platforms.enabled(site.id):
                st = await site_accounts.check(site.id)
                parts.append(f"{site.name}: {t('logged in') if st['logged_in'] else t('unknown') if st['logged_in'] is None else t('not logged in')}")
    return _session_back(next, " · ".join(parts))


@app.post("/session/open")
async def session_open(url: str = Form(...), next: str = Form("/session")):
    from app.willhaben.session import _other_site

    if not (url.startswith("https://www.willhaben.at/") or (url.startswith("https://") and _other_site(url))):
        return _session_back(next, t('Only willhaben, Kleinanzeigen and Vinted URLs'))
    await get_session().open_url(url)
    return _session_back(next)


@app.get("/session/screenshot.png")
async def session_screenshot():
    try:
        png = await get_session().screenshot()
    except SessionUnavailable:
        raise HTTPException(503) from None
    return Response(png, media_type="image/png", headers={"cache-control": "no-store"})


@app.get("/session/screen.jpg")
async def session_screen():
    """Frame for the remote view on the session page (polled by the browser)."""
    try:
        jpg = await get_session().screenshot("jpeg")
    except SessionUnavailable:
        raise HTTPException(503) from None
    return Response(jpg, media_type="image/jpeg", headers={"cache-control": "no-store"})


@app.post("/session/input", status_code=204)
async def session_input(payload: dict[str, Any]):
    try:
        await get_session().remote_input(payload)
    except SessionUnavailable as e:
        raise HTTPException(503, str(e)) from None
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None


@app.post("/session/capture/start")
async def capture_start(label: str = Form("capture"), next: str = Form("/session")):
    try:
        get_session().start_capture(label.strip() or "capture")
    except SessionUnavailable as e:
        return _session_back(next, str(e))
    return _session_back(next, t('Recording in progress'))


@app.post("/session/vinted-explore")
async def session_vinted_explore(next: str = Form("/session")):
    """Read Vinted's inbox and its newest conversation once, read-only, in hidden tabs (platforms/vinted_inbox.py):
    pressed right after logging in to Vinted in the browser window, no restart in between."""
    from app.platforms import vinted_inbox
    if not get_session().started:
        return _session_back(next, t("Browser session is not running"))
    try:
        res = await vinted_inbox.explore()
    except Exception as e:  # noqa: BLE001 - shown on the page
        return _session_back(next, str(e) or type(e).__name__)
    return _session_back(next, vinted_inbox.summary(res))


@app.post("/session/forget/{pid}")
async def session_forget_site(pid: str, next: str = Form("/session")):
    """Delete a site's cookies in the app's browser (Kleinanzeigen, Vinted): after the site blocked this browser."""
    if not site_accounts.get(pid):
        raise HTTPException(404)
    try:
        n = await site_accounts.forget(pid)
    except SessionUnavailable as e:
        return _session_back(next, str(e))
    return _session_back(next, t("{n} cookies of {site} deleted – log in again", n=n, site=site_accounts.get(pid).name))


@app.post("/session/record-all")
async def session_record_all(on: str = Form("0"), next: str = Form("/session")):
    """Record everything the browser does (willhaben/record_all.py), on or off at once."""
    record_all.set_on(on == "1")
    return _session_back(next, t("Recording everything") if on == "1" else t("Recording everything stopped"))


@app.get("/session/capture/state")
async def capture_state():
    """The running recording's request count (the remote view counts along every 2 s)."""
    st = get_session().status() if get_session().started else {}
    return {"capturing": bool(st.get("capturing")), "n": st.get("capture_count", 0)}


@app.post("/session/capture/stop")
async def capture_stop(next: str = Form("/session")):
    path = await get_session().stop_capture()
    return _session_back(next, t('Saved: {name}', name=path.name if path else '–'))


@app.get("/session/captures/{name}")
async def capture_download(name: str):
    p = get_settings().captures_dir / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json", filename=p.name)


# ------------------------------------------------------------------ settings
from app.services import settings_catalog  # noqa: E402

SETTINGS_SECTIONS = settings_catalog.SECTION_IDS   # the sections with a page of their own, in nav order (settings.html)
templates.env.globals.update(settings_groups=lambda: settings_catalog.build(_site_sections().values()))   # the palette


def _settings_nav() -> dict[str, Any]:
    """settings.html: the groups with their sections (settings_catalog) and a flag per section: bad / warn = a dot next
    to it (not logged in, VPN problem, sender address missing, Claude not signed in, web address unreachable), off = a
    site switched off. `attention`: the flagged sections with a line each, for the overview."""
    sites = _site_sections()
    ss = get_session().status()
    flags = {"willhaben": "bad" if ss["started"] and not ss["logged_in"] else "",
             "vpn": "bad" if vpn.health()[0] == "problem" else "",
             "shipping": "" if shipping.sender_complete() else "warn",
             "claude": "" if claude_token() else "bad",
             "web": "" if _web_address()["reachable"] else "warn",
             "agent": "warn" if agent_settings_routes.attention() else "",
             **{pid: "" if platforms.enabled(pid) else "off" for pid in sites}}
    groups = settings_catalog.build(sites.values())
    attention = [(s, flags[s.id]) for g in groups for s in g.sections if flags.get(s.id) in ("bad", "warn") and s.attention]
    return {"nav_groups": groups, "nav_flags": {k: v for k, v in flags.items() if v}, "attention": attention}


def _last_notifiable() -> datetime | None:
    """Settings → Browser: when the last event the browser would show came (a hit, price drop or saved-ad news that
    was pushed)."""
    with session() as db:
        ts = db.exec(select(func.max(EventLog.ts)).where(col(EventLog.kind).in_(("hit", "price_drop", "saved")),
                                                          col(EventLog.quiet).is_not(True))).one()
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


def _site_sections() -> dict[str, Any]:
    """The other platforms' settings pages (/settings/<platform id>, group "Platforms" next to willhaben)."""
    return {p.id: p for p in platforms.all(enabled_only=False) if p.id != "willhaben"}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str | None = None):
    """The overview: every group with its sections and what they hold, what needs attention, and the search.
    A #section hash (the old links) is sent on to /settings/<section> by the page."""
    return render(request, "settings.html", section=settings_catalog.OVERVIEW, msg=msg, **_settings_nav())


@app.get("/settings/search.json")
async def settings_search_index():
    """The settings search's index (settings.html fetches it on the first keystroke): every section with its fields,
    each text in the UI language and in English (settings_catalog.index)."""
    return settings_catalog.index(_site_sections().values())


INBOX_KINDS = {"hit": "New hit", "price_drop": "Price drop", "saved": "Saved ad", "message": "New message", "auto_reply": "Answered automatically",
               "auto_reply_pending": "Reply waiting for you", "ad_expiring": "Ad expiring soon", "ad_expired": "Ad expired",
               "ad_renewed": "Ad renewed", "paylivery": "Pay now!", "purchase": "Purchase order", "health": "Connections", "test": "Test"}


def event_link(e: EventLog) -> dict[str, Any] | None:
    """Where an activity entry leads in this app: {href, ad, external}. `ad` opens the ad viewer (_ad_panel.html);
    willhaben only when no page here fits. Same mapping as the phone pushes (notify.app_path)."""
    from app.services.notify import app_path

    path = app_path(e.kind, e.url, e.ad_id, e.conversation_id)
    if path is None:
        return {"href": e.url, "ad": None, "external": True} if e.url else None
    ad = path.split("=", 1)[1] if path.startswith("/hits?view=") else None
    return {"href": path, "ad": ad, "external": False}


templates.env.globals.update(event_link=event_link)


def _inbox_items(limit: int = 15) -> list[dict[str, Any]]:
    """The inbox popover (base.html): the events since its last "Clear" (Home's activity keeps them all); unread =
    newer than the last clear."""
    seen = _inbox_seen_at()
    out = []
    for e in _inbox_events(limit):       # newer than the last clear, and not already seen elsewhere in the app
        ts = e.ts if e.ts.tzinfo else e.ts.replace(tzinfo=timezone.utc)
        out.append({"kind": "pay" if e.kind == "paylivery" else e.kind, "title": t(INBOX_KINDS.get(e.kind, "Notification")),
                    "text": e.message, "time": _ago(ts), "link": event_link(e), "unread": ts > seen})
    return out


CONTRIBUTORS_URL = "https://api.github.com/repos/lumascet/secondhandler/contributors"
_contributors_cache: dict[str, Any] = {"at": 0.0, "list": None}


async def _contributors() -> list[dict[str, Any]] | None:
    """The repository's contributors from the GitHub API (public, no token), without the author and bots;
    cached for a day. None when GitHub cannot be reached and nothing is cached."""
    import time

    if _contributors_cache["list"] is not None and time.time() - _contributors_cache["at"] < 86400:
        return _contributors_cache["list"]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(CONTRIBUTORS_URL, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("GitHub contributors not available: %s", e)
        return _contributors_cache["list"]
    people = [{"login": c.get("login") or "", "html_url": c.get("html_url") or "", "avatar_url": c.get("avatar_url") or "",
               "contributions": int(c.get("contributions") or 0)}
              for c in data if isinstance(c, dict) and c.get("type") == "User" and not str(c.get("login", "")).endswith("[bot]")
              and str(c.get("login", "")).lower() != AUTHOR.lower()]
    _contributors_cache.update(at=time.time(), list=people)
    return people


@app.get("/credits", response_class=HTMLResponse)
async def credits_page(request: Request):
    people = await _contributors()
    return render(request, "credits.html", **({"contributors": people} if people is not None else {}))


@app.get("/inbox/items", response_class=HTMLResponse)
async def inbox_items(request: Request):
    """The inbox popover's content alone: an open page swaps it in when new events come (base.html, jobs store)."""
    return templates.TemplateResponse(request, "_inbox.html", {"inbox_items": _inbox_items(),
                                                               "inbox_unread": _nav_counts()["inbox"]})


@app.post("/inbox/read-all")
async def inbox_read_all(request: Request):
    """The inbox's "Clear": all read, and the popover starts empty again."""
    set_setting("inbox_seen_at", utcnow().isoformat())
    ref = request.headers.get("referer") or "/"
    return back(ref if ref.startswith(str(request.base_url)) or ref.startswith("/") else "/")


def _claude_section() -> dict[str, Any]:
    """Context of settings/claude.html (handoff design): models per task, the monthly limit, usage per task and model."""
    from app.llm.base import cost_factors, last_call

    lim = llm_usage.limit()
    rows = []
    fmt = lambda n: "{:,}".format(n or 0).replace(",", ".")   # noqa: E731
    choices = llm_models.choices()                                  # fetched from the API (settings_section)
    fac, names = cost_factors(m for m, _n in choices), {m: name.split()[0] for m, name in choices}   # "Haiku 5.5" -> "Haiku"
    for g in (llm_usage.summary().get("groups") or []):
        rows.append({"label": t(PURPOSES[g["purpose"]][1]) if g["purpose"] in PURPOSES else t("Other (tests, misc.)"),
                     "model": g["model"], "calls": g["calls"], "tokens_in": fmt(g["input"]), "tokens_out": fmt(g["output"]),
                     "cost": g["cost_usd"]})
    return {"models": {p: _alias(model_for(p)) for p in PURPOSES}, "model_choices": choices,   # full model ids
            "model_list": llm_models.last_fetch,                   # {ok, at, error}: where the list comes from
            "model_costs": [(names[m], f) for m, f in fac.items()],   # relative token price, cheapest first
            "claude_usage": {"used": lim["used"], "limit": lim["limit"], "days": lim["days"]},
            "claude_limit_mode": lim["mode"], "token_usage": rows,
            **({"claude_last_call": last_call["at"]} if last_call.get("at") else {})}


@app.post("/settings/claude")
async def settings_claude(request: Request):
    """Model per task and the monthly token limit (Settings → Claude & usage)."""
    form = await request.form()
    await llm_models.refresh()
    ids = llm_models.ids()
    for purpose, (key, _label, _hint) in PURPOSES.items():
        m = llm_models.resolve_alias(str(form.get(f"model_{purpose}") or ""))   # haiku / sonnet / … -> newest of the line
        if m in ids:
            set_setting(key, m)
    try:
        lim = min(max(0.0, float(str(form.get("claude_token_limit_m") or "0").replace(",", "."))), 1000.0)
    except ValueError:
        lim = 0.0
    set_setting("claude_token_limit_m", f"{lim:g}" if lim else "")
    days = str(form.get("claude_limit_days") or "").strip()
    if days.isdigit():
        set_setting(llm_usage.PERIOD_SETTING, str(min(max(int(days), 1), llm_usage.MAX_DAYS)))
    if form.get("claude_limit_mode") in llm_usage.LIMIT_MODES:
        set_setting("claude_limit_mode", str(form.get("claude_limit_mode")))
    return back(f"/settings/claude?msg={quote(t('Changes saved'), safe='')}")


def _apply_limits() -> None:
    """Every site's limiter takes its budget and gap now (own value, else the default of all platforms)."""
    from app.db import platform_min_gap_s, platform_per_hour

    lim = willhaben_limiter()
    lim.per_hour, lim.min_gap_s = platform_per_hour("willhaben"), platform_min_gap_s("willhaben")
    for p in _site_sections().values():
        p.limiter.per_hour, p.limiter.min_gap_s = platform_per_hour(p.id), platform_min_gap_s(p.id)


def _own_number(raw: Any, *, decimal: bool = False) -> str:
    """A site's own value as typed: a number, or "-" when left empty (the default of all platforms applies)."""
    value = str(raw or "").strip().replace(",", ".")
    ok = re.fullmatch(r"\d+(\.\d+)?" if decimal else r"\d+", value)
    return (value if decimal else str(min(int(value), 2000))) if ok else "-"


@app.post("/settings/willhaben")
async def settings_willhaben(request: Request):
    """willhaben's own budget and gap (empty: the default of all platforms) and, from the settings page
    (has_product_search), whether the product search uses willhaben. The usage popover's slider sends
    requests_per_hour (its budget only) and `next`, the page it was moved on."""
    form = await request.form()
    if "requests_per_hour" in form:                  # the popover's slider: willhaben's own budget
        set_setting("platform_willhaben_per_hour", _own_number(form.get("requests_per_hour")))
    if "budget" in form:
        set_setting("platform_willhaben_per_hour", _own_number(form.get("budget")))
    if "gap" in form:
        gap = _own_number(form.get("gap"), decimal=True)
        set_setting("platform_willhaben_min_gap_s", "" if gap == "-" else gap)
    if form.get("has_product_search"):
        set_setting("platform_willhaben_product_search", "1" if form.get("product_search") else "0")
    _apply_limits()
    nxt = str(form.get("next") or "")
    target = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/settings/willhaben"
    sep = "&" if "?" in target else "?"
    return back(f"{target}{sep}msg={quote(t('Changes saved'), safe='')}")


def _platform_defaults() -> dict[str, Any]:
    """Settings → All platforms: the defaults and what each platform uses (own value or the default)."""
    from app.db import default_min_gap_s, platform_min_gap_s, platform_own_per_hour

    rows = []
    for pid, name, on in [("willhaben", "willhaben", True)] + [(p.id, p.name, platforms.enabled(p.id))
                                                             for p in _site_sections().values()]:
        rows.append({"id": pid, "name": name, "enabled": on, "budget": _platform_per_hour(pid),
                     "own_budget": platform_own_per_hour(pid) is not None, "gap": platform_min_gap_s(pid),
                     "own_gap": bool(get_setting(f"platform_{pid}_min_gap_s").strip())})
    return {"budget": requests_per_hour(), "gap": default_min_gap_s(), "sites": rows}


def _wh_own_limits() -> dict[str, Any]:
    """Settings → willhaben: its own budget and gap ("" / None: the defaults of all platforms) and those defaults."""
    from app.db import default_min_gap_s, default_site_gap_s, platform_own_per_hour

    gap = default_min_gap_s()
    return {"budget": platform_own_per_hour("willhaben"), "gap": get_setting("platform_willhaben_min_gap_s").strip(),
            "default_budget": requests_per_hour(), "default_gap": gap if gap is not None else default_site_gap_s("willhaben")}


templates.env.globals.update(platform_defaults=_platform_defaults, wh_own_limits=_wh_own_limits)


@app.post("/settings/platforms")
async def settings_all_platforms(request: Request):
    """Settings → All platforms: the hourly budget and the gap between requests every platform uses unless it has
    its own (0 = no budget; an empty gap keeps each site's built-in pace)."""
    form = await request.form()
    budget = str(form.get("requests_per_hour") or "").strip()
    if budget.isdigit():
        set_setting("requests_per_hour", str(min(int(budget), 2000)))
    gap = _own_number(form.get("gap"), decimal=True)
    set_setting("platforms_min_gap_s", "" if gap == "-" else gap)
    _apply_limits()
    return back(f"/settings/platforms?msg={quote(t('Changes saved'), safe='')}")


@app.get("/settings/{section}", response_class=HTMLResponse)
async def settings_section(request: Request, section: str, msg: str | None = None):
    """One settings section per page (settings.html index + settings/<section>.html); the other platforms each have
    one (settings/platform.html). /settings/platforms, the former page of all sites, leads to the first of them."""
    sites = _site_sections()
    if section not in SETTINGS_SECTIONS and section not in sites:
        raise HTTPException(404)
    keys = ["platform_willhaben_product_search", "reply_rules", "auto_draft", "notify_messages", "weekly_digest", "chat_translate", "chat_auto_finish", "chat_sync_limit", "chat_sync_days", "push_channel", "expiry_warn_days", "myads_expired_days", "auto_reply_rules", "send_verified",
            "reply_humanize", "reply_delay_min_s", "reply_delay_max_s", "reply_fast_min_s", "reply_fast_max_s"]
    if section == "claude":
        await llm_models.refresh()                 # the selectable models, fresh from the API
    return render(request, "settings.html", section=section, ship_sender_ok=shipping.sender_complete(), llm_ok=bool(claude_token()),
                  **_settings_nav(), **(_claude_section() if section == "claude" else {}),
                  **(_timeline_section() if section == "timeline" else {}),
                  **({"ai": {"groups": ai_rules.GROUPS, "rules": ai_rules.rules(), "prompts": ai_rules.prompts(),
                            "built_in": ai_rules.built_in()}}
                     if section == "rules" else {}),
                  **({"sell": selling.config(), "sell_preview": selling.prompt_block(),
                      "contact_rule": selling.CONTACT_RULE} if section == "selling" else {}),
                  **({"site": _platform_row(sites[section])} if section in sites else {}),
                  **({"account": {**site_accounts.state(section), "checked_at": site_accounts.checked_at(section)},
                      "account_site": site_accounts.get(section)}
                     if site_accounts.get(section) else {}),
                  **({"vpn": vpn.status()} if section == "vpn" or section == "willhaben" or section in sites else {}),
                  values={k: get_setting(k) for k in keys}, msg=msg, settings=get_settings(),
                  claude_source=token_source(), claude_login=get_login(),
                  purposes=[(p, label, hint, _alias(model_for(p))) for p, (_, label, hint) in PURPOSES.items()],
                  llm_usage=llm_usage.summary(), purpose_labels={p: label for p, (_, label, _h) in PURPOSES.items()},
                  ha=await ha_auth.status(), ha_url=ha_auth.ha_url(), ha_source=ha_auth.token_source(),
                  ha_service=ha_auth.notify_service(), quiet=quiet_hours.state(), quiet_cfg=quiet_hours.config(),
                  tg=telegram.status(), tg_kinds=telegram.kinds(), tg_qr=telegram.bot_qr_svg(), **(await _gcal_ctx() if section == "calendar" else {}),
                  ship=shipping.config(), pickup_max_km=_geo.pickup_radius(),
                  **({"last_notifiable": _last_notifiable()} if section == "browser" else {}), ship_carriers=shipping.CARRIERS, ship_logins=shipping.login_status(),
                  geizhals_auto_specs=ad_creator.auto_specs(), human=human_pace.config(),
                  **({"adaptive": adaptive_pace.profile(None), "adaptive_on": adaptive_pace.enabled(),
                      "adaptive_max": adaptive_pace.max_factor(), "adaptive_choices": adaptive_pace.MAX_FACTORS,
                      "adaptive_hour": utcnow().astimezone(quiet_hours.VIENNA).hour} if section == "quiet" else {}),
                  captures=[c.name for c in sorted(get_settings().captures_dir.glob("*.json"), reverse=True)[:30]],
                  label_form=label_fill.active(), session_next="/settings/willhaben", backup=backup_routes.panel(),
                  web=_web_address(), **label_buy_routes.panel(), **printing_routes.panel(),
                  **(agent_settings_routes.panel() if section == "agent" else {}))


def _platform_row(p: Any) -> dict[str, Any]:
    """Context of settings/platform.html: the site's switch, pacing, status and (kleinanzeigen) app credentials.
    per_hour / own_gap: the site's own values ("" / None: it follows Settings → All platforms, default_per_hour /
    default_gap, else its built-in pace)."""
    from app.db import default_min_gap_s, default_site_gap_s, platform_min_gap_s, platform_own_per_hour

    st = p.status()
    default_gap = default_min_gap_s()
    return {"p": p, "enabled": platforms.enabled(p.id), "status": st,
            "per_hour": platform_own_per_hour(p.id), "wh_per_hour": requests_per_hour(), "min_gap_s": platform_min_gap_s(p.id),
            "own_gap": get_setting(f"platform_{p.id}_min_gap_s").strip(),
            "default_gap": default_gap if default_gap is not None else default_site_gap_s(p.id),
            "product_search": get_setting(f"platform_{p.id}_product_search") != "0",
            "last_hour": access_stats.last_hour(platform=p.id),
            "auth_source": st.get("auth_source"),
            "auth_env": (get_settings().kleinanzeigen_api_auth.strip() if p.id == "kleinanzeigen"
                         and st.get("auth_source") == "env" else ""),
            "fb_location": _fb_location() if p.id == "facebook" else None}


def _fb_location() -> dict[str, Any] | None:
    from app.platforms import facebook as fb
    return fb.location()


@app.post("/settings/platform/{pid}")
async def settings_platform(request: Request, pid: str):
    """One other platform's switch, pacing, product-search use and (kleinanzeigen) credentials. willhaben's settings
    are posted to /settings/willhaben."""
    from app.db import platform_min_gap_s, platform_per_hour

    p = _site_sections().get(pid)
    if p is None:
        raise HTTPException(404)
    form = await request.form()
    set_setting(f"platform_{p.id}_enabled", "1" if form.get("enabled") else "0")
    set_setting(f"platform_{p.id}_product_search", "1" if form.get("product_search") else "0")
    gap = str(form.get("gap") or "").strip().replace(",", ".")
    budget = str(form.get("budget") or "").strip()
    set_setting(f"platform_{p.id}_min_gap_s", gap if re.fullmatch(r"\d+(\.\d+)?", gap) else "")
    set_setting(f"platform_{p.id}_per_hour", budget if budget.isdigit() else "-")     # empty: all platforms' default
    p.limiter.min_gap_s, p.limiter.per_hour = platform_min_gap_s(p.id), platform_per_hour(p.id)
    if p.id == "facebook":
        from app.platforms import facebook as fb
        link = str(form.get("fb_location") or "").strip()
        if not link:
            fb.clear_location()
        elif link != (fb.location() or {}).get("link"):
            try:
                await fb.save_location(link)       # one test search: does Facebook know the place?
            except (InvalidSearchUrl, PlatformError) as e:
                return back(f"/settings/{p.id}?msg={quote(str(e), safe='')}")
    return back(f"/settings/{p.id}?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/platform/kleinanzeigen/auth")
async def kleinanzeigen_auth(auth: str = Form(""), op: str = Form("save")):
    """The Kleinanzeigen app credentials (user:password): KLEINANZEIGEN_API_AUTH in .env wins over this field, which is
    then read-only. None set: the app API is off and the agents say so."""
    from app.platforms import kleinanzeigen as ka
    auth = auth.strip()
    if ka.api_auth()[1] == "env":
        msg = t("The credentials come from KLEINANZEIGEN_API_AUTH in .env – change them there")
    elif op == "forget":
        set_setting("kleinanzeigen_api_auth", "")
        ka.set_client(None)
        msg = t("Credentials deleted – the Kleinanzeigen agents are off until new ones are set")
    elif not auth:
        msg = t("Credentials unchanged")
    elif re.fullmatch(r"[^:\s]+:[^\s]+", auth):
        set_setting("kleinanzeigen_api_auth", auth)
        ka.set_client(None)           # the next request builds a client with the new credentials
        msg = t("Credentials saved")
    else:
        msg = t("Use the form user:password")
    return back(f"/settings/kleinanzeigen?msg={quote(msg, safe='')}#s-auth")


@app.post("/settings/platform/{pid}/budget")
async def settings_platform_budget(pid: str, budget: str = Form(""), next: str = Form("")):
    """The usage popover's budget of one other site (empty: as willhaben's); nothing else of the site changes."""
    from app.db import platform_per_hour

    p = _site_sections().get(pid)
    if p is None:
        raise HTTPException(404)
    value = budget.strip()
    set_setting(f"platform_{p.id}_per_hour", str(min(int(value), 2000)) if value.isdigit() else "-")
    p.limiter.per_hour = platform_per_hour(p.id)
    target = next if next.startswith("/") and not next.startswith("//") else f"/settings/{p.id}"
    return back(target)


# ------------------------------------------------------------------ VPN for the anonymous requests (app/platforms/vpn.py)
def _vpn_back(msg: str, next_url: str = "") -> RedirectResponse:
    target = next_url if next_url.startswith("/settings/") and not next_url.startswith("//") else "/settings/vpn"
    return back(f"{target}?msg={quote(msg, safe='')}")


from app.services import vpn_check  # noqa: E402

templates.env.globals.update(vpn_test=vpn_check.view)   # Settings → VPN → the connection (settings/_vpn_test.html)
templates.env.globals.update(geizhals_cache=geizhals.cache_view)   # Settings → Search & prices → Geizhals


def _vpn_test_panel(request: Request, note: str | None = None) -> HTMLResponse:
    """The connection panel alone (htmx); `note`: why no search started. While a search runs its body asks for
    itself every few seconds; the first time after the end the whole panel replaces the panel (new header and
    buttons)."""
    r = templates.TemplateResponse(request, "settings/_vpn_test.html", {"vpn_note": note})
    if request.headers.get("hx-target") == "vpn-test-body" and not vpn_check.running():
        r.headers.update({"HX-Retarget": "#vpn-test", "HX-Reswap": "outerHTML", "HX-Reselect": "#vpn-test"})
    return r


@app.post("/settings/vpn/upload")
async def settings_vpn_upload(file: UploadFile | None = File(None)):
    """A .zip of WireGuard configs (the search picks among them) or one .conf (a dedicated profile); replaces the old
    source. The first upload switches the VPN on for willhaben (a later removal then pauses it, never goes direct)."""
    if file is None or not file.filename:
        return _vpn_back(t("Choose a file first"))
    data = await file.read(vpn.MAX_UPLOAD + 1)
    try:
        mode, n, skipped = vpn.import_upload(file.filename, data)
    except ValueError as e:
        return _vpn_back(str(e))
    try:
        await vpn_check.source_changed("uploaded")  # a running search stops before its servers do
    finally:
        await vpn.source_changed()
    if get_setting("platform_willhaben_vpn") == "":
        vpn.set_enabled("willhaben", True)
    if not vpn_check.blocker():
        vpn_check.start("new")                     # a server right away (else the first request asks for one)
    msg = (t("{n} configs uploaded; a search in the background finds the EU server every site with the VPN on "
             "answers", n=n) if mode == "collection" else t("Profile uploaded"))
    if skipped:
        msg += "\n" + t("Skipped {n}: {first}", n=len(skipped), first=skipped[0])
    return _vpn_back(msg)


@app.post("/settings/vpn/forget")
async def settings_vpn_forget():
    vpn.forget()
    return _vpn_back(t("Blocks forgotten; every server gets a new chance and no site pauses"))


@app.post("/settings/vpn/clear")
async def settings_vpn_clear():
    vpn.remove_source()
    try:
        await vpn_check.source_changed("removed")
    finally:
        await vpn.source_changed()
    return _vpn_back(t("WireGuard configs removed; sites with the VPN on pause until you upload new ones"))


@app.post("/settings/geizhals/clear")
async def settings_geizhals_clear():
    """Settings → Search & prices → Geizhals: delete every cached search and product page (the next lookups ask again)."""
    n = geizhals.clear_cache()
    return back(f"/settings/geizhals?msg={quote(t('{n} cached answers deleted', n=n), safe='')}")


@app.post("/settings/vpn/site/{pid}")
async def settings_vpn_site(pid: str, enabled: str = Form(""), next: str = Form("")):
    """One site's VPN switch, or Geizhals' (Settings → VPN or the site's own page via `next`)."""
    if pid != vpn.GEIZHALS:
        try:
            pid = platforms.get(pid).id
        except platforms.UnknownPlatform:
            raise HTTPException(404) from None
    vpn.set_enabled(pid, bool(enabled))
    if enabled:
        vpn_check.site_on(pid)                     # a search asks the site through the server in use first
    else:
        await vpn.site_off(pid)
    return _vpn_back(t("Changes saved"), next)


@app.post("/settings/vpn/interval")
async def settings_vpn_interval(hours: str = Form("")):
    """How often a regular search looks for another server (hours, 1 to 168)."""
    hours = hours.strip()
    if not (hours.isdecimal() and hours.isascii() and 1 <= int(hours) <= 168):   # not "²", not 0 or 999 clamped
        return _vpn_back(t("Enter whole hours, 1 to 168"))
    vpn_check.set_interval(int(hours))
    return _vpn_back(t("Changes saved"))


@app.post("/settings/vpn/parallel")
async def settings_vpn_parallel(n: str = Form("1")):
    """How many VPN servers one site's parallel work (the product search) may use at once (1 = its own only)."""
    if not n.strip().isdecimal():
        return _vpn_back(t("Choose a number of servers"))
    vpn.set_parallel(int(n))
    if vpn.parallel() == 1:
        await vpn.conn.prune_helpers(idle_s=0)          # off: the helper servers stop now
    return _vpn_back(t("Changes saved"))


@app.get("/settings/vpn/test", response_class=HTMLResponse)
async def settings_vpn_test_panel(request: Request):
    """The connection panel (settings/_vpn_test.html); its body is polled while a search runs."""
    return _vpn_test_panel(request)


@app.post("/settings/vpn/search")
@app.post("/settings/vpn/shuffle")                 # the old path, for a page loaded before the update
async def settings_vpn_search(request: Request):
    """"Search now" (a profile: "Test now"): a search for the server(s) every site answers, in the background."""
    why = vpn_check.start("manual")
    return _vpn_test_panel(request, why) if _hx(request) else _vpn_back(why or t("Search started"))


@app.post("/settings/vpn/retest")
async def settings_vpn_retest(request: Request):
    """"Test again": every site asks once through the server it uses now; nothing switches (vpn_check.retest)."""
    why = vpn_check.retest()
    return _vpn_test_panel(request, why) if _hx(request) else _vpn_back(why or t("Test started"))


@app.post("/settings/vpn/stop")
async def settings_vpn_stop(request: Request):
    """Stop the running search; the servers it switched to so far stay."""
    await vpn_check.cancel("stopped")
    return _vpn_test_panel(request) if _hx(request) else _vpn_back(t("Search stopped"))


def _timeline_section() -> dict[str, Any]:
    from app.services import timeline as tl

    return {"tl_cfg": tl.config(), "tl_size": tl.size(), "tl_rec": tl.recording_stats()}


def _web_address() -> dict[str, Any]:
    """Settings → Web address: the address links open, what is stored here and what .env says."""
    url = get_settings().public_url
    return {"url": url, "stored": get_setting(SETTING_PUBLIC_URL).strip(), "env": type(get_settings())().public_url,
            **address_check(url)}


@app.post("/settings/web")
async def settings_web(public_url: str = Form("")):
    url = public_url.strip().rstrip("/")
    if url and not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", url):
        return back(f"/settings/web?msg={quote(t('Enter the address with http:// or https://, e.g. https://willhaben.example.com'), safe='')}")
    set_setting(SETTING_PUBLIC_URL, url)
    apply_public_url()
    if telegram.active():          # an https address: the bot's menu button and its buttons open the Mini App now
        task = asyncio.create_task(telegram.menu_button())
        _background.add(task)
        task.add_done_callback(_background.discard)
    msg = t("Web address saved") if url else t("Web address reset to PUBLIC_URL from .env")
    return back(f"/settings/web?msg={quote(msg, safe='')}")


async def _gcal_ctx() -> dict[str, Any]:
    st, cals, err = gcal.status(), [], None
    if st["connected"]:
        try:
            cals = await gcal.calendars()
        except gcal.CalendarError as e:
            err = str(e)
    return {"gcal": st, "gcal_cfg": gcal.config(), "gcal_cals": cals, "gcal_error": err,
            "gcal_redirect": gcal.REDIRECT_URI}


@app.post("/lang")
async def set_language(request: Request, lang: str = Form("auto"), next: str = Form("")):
    """The app's language (a setting for everyone). Open on the login page, but only a logged-in user changes it;
    before the login the page follows the browser's language."""
    logged_in = not auth.enabled() or auth.valid_token(request.cookies.get(auth.COOKIE)) \
        or auth.valid_basic(request.headers.get("authorization", ""))
    if logged_in:
        set_setting("ui_lang", lang if lang in i18n.LANGS else "auto")
    if next.startswith("/"):                                  # e.g. the language panel in Settings
        r = back(auth.safe_next(next))
    else:
        ref = request.headers.get("referer") or "/"
        r = back(ref if ref.startswith(str(request.base_url)) or ref.startswith("/") else "/")
    if logged_in:
        r.delete_cookie(LANG_COOKIE)
    elif lang in i18n.LANGS:                                  # the login page: this browser only
        r.set_cookie(LANG_COOKIE, lang, max_age=365 * 86400, samesite="lax")
    else:
        r.delete_cookie(LANG_COOKIE)
    return r


@app.post("/settings/quiet")
async def settings_quiet(enabled: str | None = Form(None), start: str = Form("01:00"), end: str = Form("07:00"),
                         mode: str = Form("slow"), interval_min: int = Form(60), jitter: str | None = Form(None),
                         breaks: str | None = Form(None), pace: str | None = Form(None),
                         adaptive: str | None = Form(None), adaptive_max: int = Form(4)):
    quiet_hours.save(bool(enabled), start, end, mode, interval_min)
    if pace:      # the section's "Human-like timing" and adaptive polling switches (Settings → Quiet hours)
        cur = human_pace.config()
        human_pace.save((cur["variance_pct"] or 30) if jitter else 0, bool(breaks))
        adaptive_pace.save(bool(adaptive), adaptive_max)
        scheduler.reschedule_agents()
    return back(f"/settings/quiet?msg={quote(t('Quiet hours saved'), safe='')}")


@app.post("/settings/ai/rule/{key}")
async def settings_ai_rule(key: str, text: str = Form(""), do: str = Form("save")):
    """One of my rules (Settings → AI rules): save it, or reset it to its default."""
    if not ai_rules.is_rule(key):
        raise HTTPException(404)
    if do == "reset":
        ai_rules.reset_rule(key)
    else:
        ai_rules.save_rule(key, text)
    msg = t("Reset to the default") if do == "reset" else t("Changes saved")
    return back(f"/settings/rules?msg={quote(msg, safe='')}#ai-{key}")


@app.post("/settings/ai/prompt/{kind}")
async def settings_ai_prompt(kind: str, text: str = Form(""), do: str = Form("save")):
    """An editable system prompt: save my version (the default itself means the default), or reset it."""
    if kind not in system_prompts.KINDS:
        raise HTTPException(404)
    if do == "reset":
        system_prompts.reset(kind)
    else:
        system_prompts.save(kind, text)
    msg = t("Reset to the default") if do == "reset" else t("Changes saved")
    return back(f"/settings/rules?msg={quote(msg, safe='')}#ai-{kind}")


@app.post("/settings")
async def settings_save(request: Request):
    """The general settings. Each section posts only its own fields (hidden "section"); an unknown or missing section
    saves nothing (it once wrote every key, wiping the fields the form did not send)."""
    form = await request.form()
    groups = {"rules": ("reply_rules", "reply_style"), "selling": selling.KEYS,
              "chats": ("auto_draft", "notify_messages", "weekly_digest", "chat_translate", "chat_auto_finish", "chat_sync_limit", "chat_sync_days", "push_channel", "auto_reply_rules",
                        "reply_humanize", "reply_delay_min_s", "reply_delay_max_s", "reply_fast_min_s", "reply_fast_max_s"),
              "myads": ("expiry_warn_days", "myads_expired_days")}
    section = str(form.get("section") or "")
    if section not in groups:
        return back("/settings")
    todo = set(groups[section])

    def num(key: str, default: int) -> int:
        try:
            return int(str(form.get(key, default)))
        except ValueError:
            return default

    if "reply_rules" in todo:
        if "reply_rules" in form:          # the rules have their own form now (Settings → AI rules); the style card posts here
            set_setting("reply_rules", str(form.get("reply_rules", "")).strip())
        if "reply_style" in form:          # the style card is part of the rules form; a POST without it keeps the profile
            set_setting("reply_style", str(form.get("reply_style", "")).replace("\r\n", "\n").strip()[:style_svc.MAX_PROFILE])
    if "auto_reply_rules" in todo:
        if "auto_reply_rules" in form:     # edited under Settings → AI rules now; a form without it keeps it
            set_setting("auto_reply_rules", str(form.get("auto_reply_rules", "")).strip())
        set_setting("auto_draft", "1" if form.get("auto_draft") else "0")
        set_setting("notify_messages", "1" if form.get("notify_messages") else "0")
        set_setting("weekly_digest", "1" if form.get("weekly_digest") else "0")
        set_setting("chat_translate", "1" if form.get("chat_translate") else "0")
        set_setting("chat_auto_finish", "1" if form.get("chat_auto_finish") else "0")
        set_setting("chat_sync_limit", str(min(max(1, num("chat_sync_limit", 10)), 50)))
        days = str(form.get("chat_sync_days", "")).strip()
        if days == "" or days.isdigit():          # empty: no cutoff; anything else keeps the old value
            set_setting("chat_sync_days", str(min(int(days), 3650)) if days else "")
        if form.get("push_channel") in PUSH_CHANNELS:
            set_setting("push_channel", str(form.get("push_channel")))
        set_setting("reply_humanize", "1" if form.get("reply_humanize") else "0")
        lo = min(max(0, num("reply_delay_min_s", 40)), 3600)
        set_setting("reply_delay_min_s", str(lo))
        set_setting("reply_delay_max_s", str(min(max(lo, num("reply_delay_max_s", 180)), 3 * 3600)))
        fast_lo = min(max(0, num("reply_fast_min_s", 5)), 300)
        set_setting("reply_fast_min_s", str(fast_lo))
        set_setting("reply_fast_max_s", str(min(max(fast_lo, num("reply_fast_max_s", 20)), 600)))
    if "expiry_warn_days" in todo:
        set_setting("expiry_warn_days", str(max(0, num("expiry_warn_days", 3))))
        set_setting("myads_expired_days", str(max(0, num("myads_expired_days", 30))))
    if "sell_handover" in todo:
        selling.save(form)
    if request.headers.get("x-autosave"):          # saved as you change it (x-data="autosave"): no reload
        return JSONResponse({"ok": True})
    return back(f"/settings/{section}?msg={quote(t('Changes saved'), safe='')}")


def _alias(model: str) -> str:
    # LLM_MODEL=opus / sonnet from .env -> the newest model of that line in the selector
    return llm_models.resolve_alias(model)


@app.post("/settings/model")
async def settings_model(purpose: str = Form(...), model: str = Form(...)):
    await llm_models.refresh()
    if purpose not in PURPOSES or model not in llm_models.ids():
        raise HTTPException(400, t("unknown model"))
    set_setting(PURPOSES[purpose][0], model)
    label = llm_models.label(model)
    return back(f"/settings/claude?msg={quote(f'{t(PURPOSES[purpose][1])}: {label}', safe='')}")


@app.post("/settings/claude/test")
@app.post("/settings/test-llm")
async def settings_test_llm():
    try:
        r = await get_backend().complete(
            system="Antworte nur im JSON-Schema.", prompt="Sag 'ok' im Feld status.",
            schema={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"],
                    "additionalProperties": False})
        msg = t("Claude OK ({model}, {ms} ms): {status}", model=r.model, ms=r.duration_ms, status=r.data.get("status"))
    except LLMError as e:
        msg = t("Claude error: {error}", error=e)
    return back(f"/settings/claude?msg={quote(msg, safe='')}")


@app.get("/settings/claude/login")
@app.post("/settings/claude/start")
async def claude_login_start():
    try:
        await get_login().start()
    except RuntimeError as e:
        return back(f"/settings/claude?msg={quote(t('Claude login: {error}', error=e), safe='')}")
    return back("/settings/claude")


@app.post("/settings/claude/code")
async def claude_login_code(code: str = Form(...), attempt: str | None = Form(None)):
    if not await get_login().submit_code(code, attempt):
        return back(f"/settings/claude?msg={quote(t('Claude login failed: {error}', error=get_login().error), safe='')}")
    return await settings_test_llm()


@app.post("/settings/claude/cancel")
async def claude_login_cancel():
    await get_login().cancel()
    return back("/settings/claude")


@app.post("/settings/claude/logout")
async def claude_logout():
    forget_token()
    return back(f"/settings/claude?msg={quote(t('Claude sign-in removed'), safe='')}")


@app.post("/settings/ha/connect")
async def ha_connect(request: Request, ha_url: str = Form(...)):
    try:
        url = ha_auth.authorize_url(ha_url, str(request.base_url))
    except ha_auth.HAError as e:
        return back(f"/settings/ha?msg={quote(str(e), safe='')}")
    return RedirectResponse(url, status_code=303)


@app.get("/settings/ha/callback")
async def ha_callback(code: str | None = None, state: str = "", error: str | None = None):
    if not code:
        reason = error or t("cancelled")
        return back(f"/settings/ha?msg={quote(t('Home Assistant: {reason}', reason=reason), safe='')}")
    try:
        await ha_auth.finish(code, state)
    except ha_auth.HAError as e:
        return back(f"/settings/ha?msg={quote(t('Home Assistant: {reason}', reason=e), safe='')}")
    return back(f"/settings/ha?msg={quote(t('Home Assistant connected – now choose a push target'), safe='')}")


@app.post("/settings/ha/push")
async def ha_push(service: str = Form("")):
    if service and service != ha_auth.NO_PUSH and not re.fullmatch(r"[a-z0-9_]+", service):
        raise HTTPException(400, t("invalid service"))
    set_setting("ha_notify_service", service or ha_auth.NO_PUSH)
    return back(f"/settings/ha?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/ha/disconnect")
async def ha_disconnect():
    ha_auth.disconnect()
    return back(f"/settings/ha?msg={quote(t('Home Assistant disconnected (delete the token in HA under Profile → Security)'), safe='')}")


@app.post("/settings/test-chat")
async def settings_test_chat():
    from app.willhaben.account import AccountClient
    try:
        res = await AccountClient(get_session()).ping_chat()
        msg = t("Chat connection OK") if res.get("status") == "success" else \
            t("Chat connection: {error}", error=f"{res.get('status')} {res.get('code') or ''}")
    except (SessionUnavailable, NotLoggedIn) as e:
        msg = t("Chat connection: {error}", error=e)
    return back(f"/settings/chats?msg={quote(msg.strip(), safe='')}")


# ------------------------------------------------------------------ google calendar
def _gcal_back(msg: str) -> RedirectResponse:
    return back(f"/settings/calendar?msg={quote(msg, safe='')}")


@app.post("/settings/gcal/client")
async def gcal_client(client_id: str = Form(""), client_secret: str = Form(""), client_json: str = Form(""),
                      client_file: UploadFile | None = File(None)):
    if client_file is not None and client_file.filename:
        client_json = (await client_file.read()).decode("utf-8", "replace")
    try:
        gcal.save_client(client_id, client_secret, client_json)
    except gcal.CalendarError as e:
        return _gcal_back(str(e))
    return _gcal_back(t("Client saved – now connect your Google account"))


@app.get("/settings/gcal/authorize")
async def gcal_authorize():
    try:
        return RedirectResponse(gcal.authorize_url(), status_code=303)
    except gcal.CalendarError as e:
        return _gcal_back(str(e))


@app.post("/settings/gcal/code")
async def gcal_code(pasted: str = Form(...)):
    if pasted.strip().startswith("{"):      # the client JSON pasted into the wrong field: take it as the client
        try:
            gcal.save_client(client_json=pasted)
        except gcal.CalendarError as e:
            return _gcal_back(str(e))
        return _gcal_back(t("Client saved – now connect your Google account"))
    try:
        account = await gcal.finish(pasted)
    except gcal.CalendarError as e:
        return _gcal_back(t("Google Calendar: {error}", error=e))
    return _gcal_back(t("Google Calendar connected: {account}", account=account or "ok"))


@app.post("/settings/gcal")
async def gcal_save(request: Request):
    f = await request.form()
    try:
        gcal.save_config([str(c) for c in f.getlist("calendar")], str(f.get("target") or "primary"),
                         int(f.get("days") or 7), str(f.get("window_start") or "09:00"),
                         str(f.get("window_end") or "20:00"), int(f.get("duration") or 30),
                         bool(f.get("in_drafts")), bool(f.get("in_auto")), int(f.get("buffer") or 0),
                         bool(f.get("auto_add")))
    except ValueError:
        return _gcal_back(t("Invalid value"))
    return _gcal_back(t("Changes saved"))


@app.post("/settings/gcal/test")
async def gcal_test():
    if not gcal.connected():
        return _gcal_back(t("Google Calendar is not connected"))
    txt = await gcal.availability_text()
    return _gcal_back(t("Free times Claude sees:") + "\n" + txt if txt else t("Could not read the calendar (see log)"))


@app.post("/settings/gcal/disconnect")
async def gcal_disconnect():
    await gcal.disconnect()
    return _gcal_back(t("Google Calendar disconnected"))


@app.post("/chats/{conversation_id}/calendar")
async def chat_calendar_add(conversation_id: str, start: str = Form(...), minutes: int = Form(30),
                            location: str = Form(""), summary: str = Form("")):
    try:
        await meetups.add(conversation_id, start=start, minutes=minutes, location=location, summary=summary)
    except gcal.CalendarError as e:
        return back(f"/chats/{conversation_id}?msg={quote(str(e), safe='')}")
    return back(f"/chats/{conversation_id}?msg={quote(t('Added to your calendar'), safe='')}")


@app.post("/chats/{conversation_id}/calendar/find")
async def chat_calendar_find(conversation_id: str):
    try:
        m = await meetups.extract(conversation_id)
    except LLMError as e:
        return back(f"/chats/{conversation_id}?msg={quote(t('Draft failed: {error}', error=e), safe='')}")
    msg = "" if m else t("Claude found no agreed appointment in this chat")
    return back(f"/chats/{conversation_id}?msg={quote(msg, safe='')}#meetup")


@app.post("/chats/{conversation_id}/calendar/forget")
async def chat_calendar_forget(conversation_id: str):
    meetups.forget(conversation_id)
    return back(f"/chats/{conversation_id}")


# ------------------------------------------------------------------ shipping (parcel labels)
def _chat_back(conversation_id: str, msg: str = "", request: Request | None = None) -> RedirectResponse:
    """Back to the chat's shipping card. `request` (success paths): sent through htmx, the card itself shows the
    new state, so a message that only restates the click is left out; errors come without it and stay."""
    if request is not None and _hx(request):
        msg = ""
    return back(f"/chats/{conversation_id}?msg={quote(msg, safe='')}#shipping")


def _shipment_or_404(shipment_id: int):
    s = shipping.get(shipment_id)
    if s is None:
        raise HTTPException(404)
    return s


@app.post("/settings/shipping")
async def settings_shipping(request: Request):
    f = await request.form()
    shipping.save_sender({k: str(f.get(k) or "") for k in shipping.ADDRESS_FIELDS})
    if _geo.home_address() and _geo.home_cached() is None:
        # a new address is located now (in the background, paced): the product search can sort by distance at once
        task = asyncio.create_task(_geo.home())
        _background.add(task)
        task.add_done_callback(_background.discard)
    shipping.save_carriers([str(c) for c in f.getlist("carrier")])
    set_setting("ship_detect", "1" if f.get("detect") else "0")
    km = str(f.get("pickup_max_km") or "").strip()
    set_setting("pickup_max_km", str(min(int(km), 2000)) if km.isdigit() and int(km) > 0 else "")
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/auto")
async def settings_shipping_auto(request: Request):
    f = await request.form()
    shipping.save_auto({"on": f.get("on"), "max_price": f.get("max_price"), "need_sold": f.get("need_sold"),
                        "need_parcel": f.get("need_parcel"), "carriers": f.getlist("auto_carrier"),
                        "grace_min": f.get("grace_min"), "daily": f.get("daily")})
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/human-pace")
async def settings_human_pace(variance_pct: int = Form(30), breaks: str | None = Form(None)):
    human_pace.save(variance_pct, bool(breaks))
    scheduler.reschedule_agents()                # new timing applies from the next check on
    return back(f"/settings/quiet?msg={quote(t('Search agents now check irregularly'), safe='')}")


@app.post("/settings/listings")
async def settings_listings(geizhals_auto_specs: str | None = Form(None)):
    set_setting("geizhals_auto_specs", "1" if geizhals_auto_specs else "0")
    return back(f"/settings/rules?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/login/{carrier}")
async def settings_shipping_login(carrier: str, user: str = Form(""), password: str = Form("")):
    try:
        shipping.save_login(carrier, user, password)
    except shipping.ShippingError as e:
        return back(f"/settings/shipping?msg={quote(str(e), safe='')}")
    return back(f"/settings/shipping?msg={quote(t('Changes saved'), safe='')}")


@app.post("/settings/shipping/open/{carrier}")
async def settings_shipping_open(carrier: str):
    try:
        msg = await label_fill.open_carrier(carrier)
    except shipping.ShippingError as e:
        return back(f"/settings/shipping?msg={quote(str(e), safe='')}")
    return back(f"/settings/shipping?remote=1&msg={quote(msg, safe='')}")   # shows the page in the overlay


@app.post("/chats/{conversation_id}/shipping/detect")
async def shipping_detect(request: Request, conversation_id: str):
    """Background job (lane claude): the card shows "Preparing the parcel…" and swaps itself in when it is done."""
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(404)
    shipping.enqueue_detect(conversation_id)
    return _chat_back(conversation_id, request=request)


@app.post("/shipments/{shipment_id}")
async def shipment_save(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    f = await request.form()
    addr = shipping.clean_address({k: str(f.get(k) or "") for k in shipping.ADDRESS_FIELDS})
    if addr:
        shipping.set_recipient(shipment_id, addr)
    try:
        shipping.set_parcel(shipment_id, {k: f.get(k) for k in ("length_cm", "width_cm", "height_cm", "weight_kg", "contents")})
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    if not addr:
        return _chat_back(s.conversation_id, t("Address incomplete: name, street, postcode and city are needed"))
    return _chat_back(s.conversation_id, t("Changes saved"), request=request)


@app.post("/shipments/{shipment_id}/option")
async def shipment_option(shipment_id: int, key: str = Form(...)):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.choose(shipment_id, key)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id)


@app.post("/shipments/{shipment_id}/shop")
async def shipment_shop(request: Request, shipment_id: int, shop: str = Form("")):
    """The owner's parcel shop for a "→ Shop" option (one of the shops the card listed, as JSON)."""
    s = _shipment_or_404(shipment_id)
    try:
        shipping.set_shop(shipment_id, json.loads(shop or "null"))
    except ValueError:
        return _chat_back(s.conversation_id, t("Parcel shop incomplete: name and address are needed"))
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Parcel shop saved"), request=request)


@app.post("/shipments/{shipment_id}/label")
async def shipment_label(shipment_id: int, key: str = Form("")):
    s = _shipment_or_404(shipment_id)
    if s.label_path or s.status in ("buying", "printed", "sent", "discarded"):
        return _chat_back(s.conversation_id, t("This shipment already has a label or a purchase is running"))
    try:
        if key:
            shipping.choose(shipment_id, key)
        msg = await label_fill.start(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    # stay on the chat; ?remote=1 opens the remote-view overlay with the carrier's form
    return back(f"/chats/{s.conversation_id}?remote=1&msg={quote(msg, safe='')}#shipping")


@app.get("/shipments/{shipment_id}/back")
async def shipment_back(shipment_id: int):
    return _chat_back(_shipment_or_404(shipment_id).conversation_id)


@app.post("/session/label-fill")
async def session_label_fill(next: str = Form("/session")):
    try:
        msg = await label_fill.fill_current()
    except shipping.ShippingError as e:
        msg = str(e)
    return _session_back(next, msg)


@app.post("/shipments/{shipment_id}/status")
async def shipment_status(shipment_id: int, status: str = Form(...), tracking: str = Form("")):
    s = _shipment_or_404(shipment_id)
    if status not in ("draft", "labelled", "sent", "discarded"):
        raise HTTPException(400)
    if s.status == "buying" and shipment_id in shipping._running:
        return _chat_back(s.conversation_id, t("A label purchase is running – cancel it in the browser window first"))
    shipping.update(shipment_id, status=status, tracking=tracking.strip() or s.tracking)
    return _chat_back(s.conversation_id)


@app.get("/shipments/{shipment_id}/label.pdf")
async def shipment_label_file(shipment_id: int):
    s = _shipment_or_404(shipment_id)
    if not s.label_path:
        raise HTTPException(404)
    p = get_settings().data_dir / "labels" / Path(s.label_path).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name)


@app.post("/shipments/{shipment_id}/paid")
async def shipment_paid(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.mark_paid(shipment_id, "manual")
        await shipping.after_paid(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Marked as paid"), request=request)


@app.post("/shipments/{shipment_id}/unpaid")
async def shipment_unpaid(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.unmark_paid(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Payment mark removed"), request=request)


@app.post("/shipments/{shipment_id}/approve")
async def shipment_approve(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.approve(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Buying the label – this takes a moment"), request=request)


@app.post("/shipments/{shipment_id}/decline")
async def shipment_decline(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.decline(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Label not bought"), request=request)


@app.post("/shipments/{shipment_id}/retry")
async def shipment_retry(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        shipping.retry(shipment_id, rebuy=(await request.form()).get("rebuy") == "1")
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Trying again"), request=request)


@app.post("/shipments/{shipment_id}/cancel-auto")
async def shipment_cancel_auto(request: Request, shipment_id: int):
    s = _shipment_or_404(shipment_id)
    try:
        await shipping.cancel_auto(shipment_id)
    except shipping.ShippingError as e:
        return _chat_back(s.conversation_id, str(e))
    return _chat_back(s.conversation_id, t("Automatic purchase cancelled – approve the label below"), request=request)


# ------------------------------------------------------------------ telegram
def _tg_back(msg: str) -> RedirectResponse:
    return back(f"/settings/telegram?msg={quote(msg, safe='')}")


@app.post("/settings/telegram/token")
async def settings_telegram_token(token: str = Form(...)):
    try:
        name = await telegram.save_token(token)
    except (telegram.TelegramError, httpx.HTTPError) as e:
        return _tg_back(t("Telegram: {error}", error=str(e)))
    return _tg_back(t("Bot @{name} connected – now link your chat", name=name))


@app.get("/settings/telegram/status")
async def settings_telegram_status():
    st = telegram.status()
    return {"linked": st["linked"], "chat_name": st["chat_name"], "candidates": st["candidates"]}


@app.post("/settings/telegram/link")
async def settings_telegram_link(chat_id: int = Form(...)):
    name = await telegram.link(chat_id)
    return _tg_back(t("linked with {name}", name=name) if name else t("This request has expired – write to the bot again."))


@app.post("/settings/telegram/kinds")
async def settings_telegram_kinds(request: Request):
    form = await request.form()
    set_setting("telegram_enabled", "1" if form.get("enabled") else "0")
    telegram.set_kinds([str(k) for k in form.getlist("kind")])
    return _tg_back(t("Changes saved"))


@app.post("/settings/telegram/relink")
async def settings_telegram_relink():
    telegram.unlink()
    return _tg_back(t("Chat unlinked – write to the bot from the chat that should get the notifications"))


@app.post("/settings/telegram/forget")
async def settings_telegram_forget():
    telegram.forget()
    return _tg_back(t("Telegram bot removed"))


@app.post("/settings/telegram/test")
async def settings_telegram_test():
    ok = await telegram.send("test", "SecondHandler", t("Test notification"))
    return _tg_back(t("Test message sent") if ok else t("Telegram not linked or error (see log)"))


@app.post("/settings/ha/test")
@app.post("/settings/test-notify")
async def settings_test_notify():
    ok = await notify("test", "SecondHandler", t("Test notification"), url=get_settings().public_url)
    msg = t("HA event sent") if ok else t("HA not configured or error (see log)")
    return back(f"/settings/ha?msg={quote(msg, safe='')}")


# ------------------------------------------------------------------ JSON API (for MCP)
def _hit_dict(h: Hit) -> dict[str, Any]:
    return {"id": h.id, "agent_id": h.agent_id, "platform": h.platform, "ad_id": h.ad_id,
            "ref": ad_ref(h.platform, h.ad_id), "title": h.heading, "price": h.price,
            "price_display": h.price_display, "old_price": h.old_price, "location": h.location,
            "published_at": h.published_at.isoformat() if h.published_at else None, "url": h.url,
            "state": h.state, "image_url": h.image_url}


@app.get("/api/status")
async def api_status():
    with session() as db:
        unread = db.exec(select(func.coalesce(func.sum(Conversation.unread_count), 0))).one()
        new_hits = db.exec(select(func.count()).select_from(Hit).where(Hit.state == "new")).one()
    st = get_session().status()
    return {"session": {k: st[k] for k in ("started", "logged_in", "user", "error")}, "unread_messages": unread,
            "new_hits": new_hits}


@app.get("/api/agents")
async def api_agents():
    with session() as db:
        return [a.model_dump(mode="json") for a in db.exec(select(SearchAgent))]


@app.post("/api/agents")
async def api_agents_create(payload: dict[str, Any]):
    """{url} of any supported site, or {platform, keyword, price_from?, price_to?}; {sites: [...]} watches several
    sites with one agent (the url, if any, is the main search; the others get its search term)."""
    sites = payload.get("sites")
    try:
        a = _create_agent(str(payload.get("url") or ""), str(payload.get("platform") or "willhaben"),
                          str(payload.get("keyword") or ""), payload.get("price_from"), payload.get("price_to"),
                          payload.get("name"), payload.get("interval_s"),
                          [str(x) for x in sites] if isinstance(sites, list) else None)
    except (KeyError, InvalidSearchUrl) as e:
        raise HTTPException(400, str(e)) from e
    scheduler.schedule_agent(a)
    return a.model_dump(mode="json")


@app.get("/api/platforms")
async def api_platforms():
    """The sites the app knows, which are switched on and what each can do."""
    return [{"id": p.id, "name": p.name, "host": p.host, "home_country": p.home_country, "enabled": platforms.enabled(p.id),
             "can_filters": p.can("filters"), "can_keyword": p.can("keyword_search"), "can_detail": p.can("detail"),
             "access": p.access} for p in platforms.all(enabled_only=False)]


@app.get("/api/hits")
async def api_hits(agent_id: int | None = None, state: str = "new", limit: int = 50):
    with session() as db:
        q = select(Hit)
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        q = q.where(Hit.state == state) if state != "all" else q.where(Hit.state != search_agents_svc.CLEARED)
        return [_hit_dict(h) for h in db.exec(q.order_by(col(Hit.first_seen_at).desc()).limit(min(limit, 200)))]


_SORT_NAMES = {1: "newest", 3: "price_asc", 4: "price_desc", 7: "relevance"}


@app.get("/api/search")
async def api_search(keyword: str, price_from: float | None = None, price_to: float | None = None,
                     rows: int = 20, sort: int = 1, platform: str = "willhaben"):
    """Live keyword search on one platform (sort: 1 newest, 3 price ascending, 4 price descending, 7 relevance)."""
    try:
        p = platforms.get(platform)
    except platforms.UnknownPlatform as e:
        raise HTTPException(404, f"unknown platform {platform!r}") from e
    if not p.can("keyword_search"):
        raise HTTPException(400, f"{p.name} has no keyword search")
    if p.id == "willhaben":
        try:
            page = await get_public().search_keyword(keyword, min(rows, 50), price_from, price_to, sort)
        except WillhabenError as e:
            raise HTTPException(502, str(e)) from e
    else:
        try:
            page = await p.search(p.keyword_search(keyword, price_from, price_to, sort=_SORT_NAMES.get(sort, "newest")),
                                  rows=min(rows, 50), cache_s=300)
        except PlatformError as e:
            raise HTTPException(502, str(e)) from e
    return {"platform": p.id, "rows_found": page.rows_found, "ads": [a.as_dict() for a in page.ads]}


@app.get("/api/ads/{platform}/{ad_id}")
async def api_ad_platform(platform: str, ad_id: str):
    try:
        p = platforms.get(platform)
    except platforms.UnknownPlatform as e:
        raise HTTPException(404, f"unknown platform {platform!r}") from e
    if not p.can("detail"):
        raise HTTPException(404, f"{p.name} has no ad details")
    try:
        d = await p.detail(ad_id)
    except PlatformError as e:
        raise HTTPException(502, str(e)) from e
    if d is None:
        raise HTTPException(404, "ad is not online (anymore)")
    return d.as_context()


@app.get("/api/ads/{ad_id}")
async def api_ad(ad_id: str):
    return await api_ad_platform("willhaben", ad_id)


@app.get("/api/chats")
async def api_chats():
    with session() as db:
        return [c.model_dump(mode="json", exclude={"ad_context"})
                for c in db.exec(select(Conversation).where(col(Conversation.removed).is_not(True))
                                 .order_by(col(Conversation.last_message_at).desc().nulls_last()))]


@app.get("/api/chats/{conversation_id}")
async def api_chat(conversation_id: str):
    with session() as db:
        c = db.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(404)
    return {"conversation": c.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in chats.messages_of(conversation_id)],
            "drafts": [d.model_dump(mode="json") for d in chats.drafts_of(conversation_id)]}


@app.post("/api/chats/{conversation_id}/draft")
async def api_chat_draft(conversation_id: str, payload: dict[str, Any] | None = None):
    try:
        d = await chats.draft_reply(conversation_id, (payload or {}).get("hint"))
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    return d.model_dump(mode="json")


@app.post("/api/chats/{conversation_id}/drafts")
async def api_chat_save_draft(conversation_id: str, payload: dict[str, Any]):
    """Store a reply written elsewhere (e.g. by Claude Desktop via MCP) as a draft for review."""
    text = (payload.get("text") or "").strip()
    with session() as db:
        if db.get(Conversation, conversation_id) is None:
            raise HTTPException(404)
        if not text:
            raise HTTPException(400, "text is missing")
        d = Draft(conversation_id=conversation_id, text=text, intent=payload.get("intent"), model="mcp")
        db.add(d)
        db.commit()
        db.refresh(d)
    return d.model_dump(mode="json")


@app.post("/api/drafts/{draft_id}/send")
async def api_draft_send(draft_id: int, payload: dict[str, Any]):
    with session() as db:
        current = db.get(Draft, draft_id)
    if current is not None and current.status == "sending":    # queued from the dashboard: never twice
        return JSONResponse({"sent": False, "reason": "already being sent in the background"}, status_code=409)
    try:
        d = await chats.send_draft(draft_id, payload["text"])
    except (EndpointUnknown, SendFailed, ValueError) as e:
        return JSONResponse({"sent": False, "reason": str(e)}, status_code=409)
    except (NotLoggedIn, SessionUnavailable) as e:
        return JSONResponse({"sent": False, "reason": str(e)}, status_code=503)
    return {"sent": True, "draft": d.model_dump(mode="json")}


@app.get("/api/myads")
async def api_myads():
    """Every own ad; `stock`: {quantity, sold, remaining} of an ad with several pieces, None for a single item."""
    with session() as db:
        ads = list(db.exec(select(MyAd)))
    return [{**a.model_dump(mode="json"), "stock": stock.numbers(a)} for a in ads]


@app.post("/api/myads")
async def api_myads_add(payload: dict[str, Any]):
    try:
        row = my_ads.add(payload["ref"], bool(payload.get("auto_renew")))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    row = await my_ads.check(row.ad_id) or row
    return row.model_dump(mode="json")


@app.post("/api/ad-drafts")
async def api_ad_draft(payload: dict[str, Any]):
    d = await ad_creator.create_draft(payload.get("notes", ""), [])
    return d.model_dump(mode="json")


@app.get("/api/myads/{ad_id}/editor-json")
async def api_myad_editor_json(ad_id: str):
    """Read-only: an own ad as willhaben's editor sees it (reference for the insertion format)."""
    from app.willhaben import insert
    with session() as db:
        if db.get(MyAd, ad_id) is None:
            raise HTTPException(404, "not one of my ads")
    try:
        return await insert.get_own_ad(get_session(), ad_id)
    except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        raise HTTPException(503, str(e)) from None


@app.get("/api/ad-drafts/{draft_id}/willhaben-plan")
async def api_ad_draft_plan(draft_id: int):
    """Read-only: what willhaben's insertion form suggests for this draft (category, required fields)."""
    from app.willhaben import insert
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        raise HTTPException(404)
    sess = get_session()
    try:
        form = await insert.load_form(sess)
        raw = await insert.suggest_categories(sess, d.title or d.input_text[:80])
    except (NotLoggedIn, SessionUnavailable) as e:
        raise HTTPException(503, str(e)) from None
    cats = insert.category_index(form.get("categoryTree") or {})
    return {"title": d.title, "suggestions": raw, "categories": len(cats),
            "template": {k: v for k, v in (form.get("ad") or {}).items() if k in ("postCode", "location", "locationId", "countryId", "hidePhoneNo")},
            "prefill": form.get("prefillAttributes")}


@app.post("/api/ad-drafts/{draft_id}/willhaben-publish")
async def api_ad_draft_publish(draft_id: int, payload: dict[str, Any]):
    """Publish (or with stop_after a step-by-step trial up to presave/photos/save/cart) as free private ad.
    Returns the structure of willhaben's responses, without personal data."""
    from app.willhaben.insert import STEPS, InsertError

    _draft_or_404(draft_id)
    sh = payload.get("shipping")
    if sh is not None and not isinstance(sh, dict):
        raise HTTPException(400, "shipping must be an object")
    stop = payload.get("stop_after")
    if stop is not None and stop not in STEPS:
        raise HTTPException(400, f"stop_after must be one of {STEPS}")
    if payload.get("prepare") or not (_draft_or_404(draft_id).wh_plan or {}).get("options"):
        await ad_creator.prepare_willhaben(draft_id)
    try:
        res = await ad_creator.publish_willhaben(draft_id, int(payload.get("option", 0)), payload.get("values") or {},
                                                 stop_after=stop, shipping=ad_creator.parse_shipping(sh or {}))
    except (InsertError, NotLoggedIn, SessionUnavailable, RuntimeError) as e:
        d = ad_creator.update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
        return JSONResponse({"ok": False, "error": str(e), "wh_ad_id": d.wh_ad_id}, status_code=502)
    if isinstance(res, dict):
        return {"ok": True, **res}
    return {"ok": True, "published": res.status == "published", "wh_ad_id": res.wh_ad_id}


@app.get("/api/convert-url")
async def api_convert(url: str):
    try:
        return {"webapi_url": to_webapi_url(url)}
    except InvalidSearchUrl as e:
        raise HTTPException(400, str(e)) from e


from app import deals_web  # noqa: E402  Produktsuche tab (/deals) + price section of the ad viewer
deals_web.install(app, templates)
from app import bugreport_routes  # noqa: E402  footer "Report a bug" (/bugreport), log ring buffer, unhandled-error log
bugreport_routes.install(app)
from app import stats_web  # noqa: E402  willhaben access statistics (/stats/willhaben, footer counter)
stats_web.install(app, templates)
from app import tg_web  # noqa: E402,I001  (the Telegram Mini App, /tg)
tg_web.install(app)
from app import timeline_web  # noqa: E402  timeline (/timeline), Settings → Timeline
timeline_web.install(app, templates)
from app import agent_sim_routes  # noqa: E402  search agent simulator (/agent-sim)
agent_sim_routes.install(app, templates)
