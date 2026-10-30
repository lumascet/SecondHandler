"""Optional Claude screening of search-agent hits: right product? good deal? Only hits that pass get a push.

Rejected hits stay in the dashboard and still fire the Home Assistant event (automations), just no push or
Telegram message. If Claude fails, hits are notified as before (fail open: no hit gets lost).
"""
from __future__ import annotations

import logging
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import col, select

from app.db import Hit, SearchAgent, session, utcnow
from app.platforms import condition, shipping_info
from app.services import ad_images, scam
from app.services.ad_images import MAX_IMAGE_BYTES  # noqa: F401  (re-exported)
from app.i18n import LANGS, background_lang, t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import SCREEN_GH_SCHEMA, SCREEN_SCHEMA, screen_prompt, screen_system
from app.services.ad_creator import price_stats
from app.services import timeline

log = logging.getLogger(__name__)

MAX_PER_CALL = 10
GH_PICK_MAX = 8                    # Geizhals results per hit Claude may pick from in the screening's call
MAX_PER_RUN = 3 * MAX_PER_CALL     # hits one agent check sends to Claude at most, in calls of MAX_PER_CALL
DEAL_RANK = {"great": 3, "good": 2, "fair": 1, "unknown": 1, "overpriced": 0}
# agent threshold -> lowest deal that still notifies ("unknown" passes "fair" but not "good")
MIN_RANK = {"match": 0, "fair": 1, "good": 2}
_transport: httpx.AsyncBaseTransport | None = None   # tests inject a mock transport


def thresholds() -> dict[str, str]:
    return {"match": t("every matching item"), "fair": t("only fair or better prices"),
            "good": t("only good deals")}


def deal_label(deal: str | None) -> str:
    return {"great": t("top deal"), "good": t("good price"), "fair": t("fair price"),
            "overpriced": t("too expensive"), "unknown": t("price unclear")}.get(deal or "", "")


# Predefined reasons Claude picks from (several per hit). The price ones only label: the deal threshold filters price.
# A hit that misses a limit the description sets (a price cap, a must-have feature, the generation) never pushes: it
# is the user's own rule, no option to switch off (10-14 Echo Shows over "only below 50€" were pushed as "too expensive").
ISSUES = ["wrong_product", "accessory_only", "wrong_variant", "misses_requirement", "defective", "search_or_swap",
          "incomplete", "bundle_only", "dealer", "too_far", "scam_suspect", "too_expensive", "price_unclear"]
PRICE_ISSUES = {"too_expensive", "price_unclear"}
ALWAYS_HIDE = {"misses_requirement"}
APP_ISSUES = {"dealer"}            # set by the app from the site's data, not Claude's reason against the hit
HIDEABLE = [i for i in ISSUES if i not in PRICE_ISSUES and i not in ALWAYS_HIDE]
DEFAULT_HIDE = ["wrong_product", "accessory_only", "wrong_variant", "search_or_swap", "scam_suspect"]


def issue_labels() -> dict[str, str]:
    return {"wrong_product": t("wrong product"), "accessory_only": t("accessory / spare part only"),
            "wrong_variant": t("wrong size or variant"), "misses_requirement": t("misses a requirement"),
            "defective": t("defective / damaged"),
            "search_or_swap": t("search or swap ad"), "incomplete": t("incomplete, parts missing"),
            "bundle_only": t("only as a bundle"), "dealer": t("dealer"), "too_far": t("too far away"),
            "scam_suspect": t("suspected scam"), "too_expensive": t("too expensive"),
            "price_unclear": t("price unclear")}


def hide_set(agent: SearchAgent) -> set[str]:
    """Reasons that keep a hit of this agent from pushing (None = defaults, "-" = none)."""
    if agent.screen_hide is None:
        return set(DEFAULT_HIDE)
    return {i for i in agent.screen_hide.split(",") if i in HIDEABLE}


def verdict_labels(hit: Hit) -> list[str]:
    """Badges for a screened hit: the price verdict if it is the item, then every named reason."""
    if hit.screen_relevant is None:
        return []
    names = issue_labels()
    reasons = [names[i] for i in (hit.screen_issues or []) if i in names and i not in PRICE_ISSUES]
    if hit.screen_relevant:
        return [deal_label(hit.screen_deal)] + reasons
    return reasons or [t("wrong item")]          # hits screened before reasons existed


def passes(agent: SearchAgent, hit: Hit) -> bool:
    """Should this hit push? Unscreened hits (screening off, failed, not yet run) always do."""
    if not agent.screen or hit.screen_relevant is None:
        return True
    issues = set(hit.screen_issues or [])
    if issues & (hide_set(agent) | ALWAYS_HIDE):
        return False
    if not hit.screen_relevant and not (issues - PRICE_ISSUES - APP_ISSUES):
        return False                           # not the item and no reason the user chose to allow
    return DEAL_RANK.get(hit.screen_deal or "unknown", 1) >= MIN_RANK.get(agent.screen_min or "match", 0)


def summary(hit: Hit) -> str:
    """One line for the push text, e.g. "Claude: good price – 30 % under the usual used price"."""
    if hit.screen_relevant is None:
        return ""
    head = " · ".join(verdict_labels(hit))
    return f"Claude: {head}: {short_reason(hit.screen_reason)}" if hit.screen_reason else f"Claude: {head}"


def short_reason(reason: str | None, limit: int = 140) -> str:
    """First sentence of Claude's reason for push texts; the ad viewer shows the whole text."""
    text = (reason or "").strip()
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def _stats(agent_id: int, exclude: set[int]) -> dict[str, Any] | None:
    since = utcnow() - timedelta(days=60)
    with session() as db:
        prices = db.exec(select(Hit.price).where(Hit.agent_id == agent_id, col(Hit.first_seen_at) >= since,
                                                 col(Hit.id).not_in(exclude or {0}), Hit.screen_relevant != False)  # noqa: E712
                         ).all()
    return price_stats([p for p in prices if p])


def _site_name(pid: str | None) -> str:
    from app import platforms

    try:
        return platforms.get(pid or "willhaben").name
    except platforms.UnknownPlatform:
        return pid or "willhaben"


def _system(sites: list[str], check_geizhals: bool = False) -> str:
    """The system prompt for the sites of this batch: one site with its country, several named together (a
    multi-site agent's hits are not all from its main site); with `check_geizhals` also the Geizhals check's rules."""
    from app.services import geizhals_check

    base = _site_system(sites)
    return base + geizhals_check.SCREEN_RULES if check_geizhals else base


def _site_system(sites: list[str]) -> str:
    from app import platforms

    known = []
    for pid in sites:
        try:
            known.append(platforms.get(pid))
        except platforms.UnknownPlatform:
            continue
    if not known:
        return screen_system()                    # an unknown platform: willhaben's wording
    countries = {p.home_country for p in known}
    return screen_system(", ".join(p.host for p in known), countries.pop() if len(countries) == 1 else "")


def _new_price(h: Hit) -> dict[str, Any] | None:
    """The hit's Geizhals product for the prompt: {name, price, pct, level} with the price in % of the lowest new price
    and the label of the user's zones (German, as the prompt), the one the hit card shows; None without one."""
    from app.services import deals, price_zones

    gh = h.geizhals or {}
    if not gh.get("price_from") or not h.price or h.price <= 0 or price_zones._cents(gh["price_from"]) <= 0:
        return None
    lvl = deals.hit_level(h)
    return {"name": gh.get("name") or "?", "price": gh["price_from"], "pct": price_zones.share(h.price, gh["price_from"]),
            "level": deals.LEVEL_DE.get(lvl or "")}


def _gh_candidates(h: Hit) -> list[dict[str, Any]]:
    """The Geizhals results of the hit's automatic lookup, for Claude to pick from (no request; the product found
    first)."""
    from app.services import deals

    return deals.geizhals_candidates(deals.geizhals_query(h.heading))[:GH_PICK_MAX]


async def _download(url: str | None, folder: Path, name: str, platform: str | None = None) -> Path | None:
    return await ad_images.download(url, folder, name, transport=_transport, platform=platform)   # _transport: tests


async def ask(agent: SearchAgent, hits: list[Hit], check: list[int],
              stats: dict[str, Any] | None) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Claude's verdicts on `hits` (the agent's brief, the photos, the comparables `stats`); `check`: the numbers
    (from 1) whose Geizhals product Claude checks in the same call. ({nr: answer}, {nr: the lookup's results Claude
    picked from}). Stores nothing (the screening and the agent simulator, app/services/agent_sim.py, both use it);
    raises LLMError."""
    cands: dict[int, list[dict[str, Any]]] = {}     # the results Claude may pick from, per hit to check
    lang = LANGS.get(background_lang(), "Deutsch")
    sites = list(dict.fromkeys(h.platform or "willhaben" for h in hits))
    with tempfile.TemporaryDirectory(prefix="wh-screen-") as tmp:
        images: list[Path] = []
        items = []
        for nr, h in enumerate(hits, 1):
            img = await _download(h.image_url, Path(tmp), f"hit{nr}", h.platform)
            if img:
                images.append(img)
            items.append({"nr": nr, "title": h.heading, "price": h.price_display or (f"{h.price:g} €" if h.price else None),
                          "location": h.location, "private": h.is_private, "description": h.body,
                          "condition": condition.shown(h.item_condition, h.item_condition_label, h.platform) or None,
                          "new_price": _new_price(h),
                          **({"check_geizhals": (h.geizhals or {}).get("name") or "",
                              "geizhals_results": cands.setdefault(nr, _gh_candidates(h))} if nr in check else {}),
                          "shipping": shipping_info.prompt_line(h.shipping_price, h.shipping)
                          if h.shipping_price is not None or h.shipping else None,
                          **({"site": _site_name(h.platform)} if len(sites) > 1 else {}),
                          "scam_flags": [f.get("label") for f in h.scam_flags or [] if f.get("severity") in ("warn", "high")],
                          # the CLI wrapper copies photos in order as foto1, foto2, ...
                          "photo": f"foto{len(images)}{img.suffix}" if img else None})
        prompt = screen_prompt(agent_name=agent.name, brief=agent.screen_brief or "", hits=items, stats=stats,
                               language=lang)
        result = await get_backend("screen").complete(system=_system(sites, bool(check)), prompt=prompt,
                                                      schema=SCREEN_GH_SCHEMA if check else SCREEN_SCHEMA,
                                                      images=images)
    return {r.get("nr"): r for r in result.data.get("results") or [] if isinstance(r, dict)}, cands


def verdict_fields(r: dict[str, Any], hit: Hit) -> dict[str, Any]:
    """One answer of `ask` as the hit's screen_* fields (the app's own issues added: dealer, the scam check)."""
    out: dict[str, Any] = {"screen_relevant": bool(r.get("relevant")),
                           "screen_deal": r.get("deal") if r.get("deal") in DEAL_RANK else "unknown",
                           "screen_reason": (r.get("reason") or "").strip()[:1500] or None}   # full text for the viewer
    if isinstance(r.get("issues"), list):
        issues = [i for i in r["issues"] if i in ISSUES and i != "dealer"]
        if hit.is_private is False:          # willhaben says so; Claude does not have to guess
            issues.append("dealer")
        if scam.level(hit.scam_flags) == "high":   # the rule-based scam check counts like Claude's verdict
            issues.append("scam_suspect")
        out["screen_issues"] = list(dict.fromkeys(issues))
    return out


@timeline.scoped(agent_id="agent_id")
async def screen(agent_id: int, hits: list[Hit]) -> int:
    """Ask Claude about up to MAX_PER_CALL hits and store the verdicts on them. Returns how many were judged."""
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None or not agent.screen or not hits:
        return 0
    from app.services import geizhals_check

    hits = hits[:MAX_PER_CALL]
    timeline.update_context(hit_ids=[h.id for h in hits])
    # with "Check the Geizhals product" the same call checks the product the app found (one call instead of two)
    # always, also without the switch: a product the app found by itself is checked in the same call (no request
    # more) – right ones kept, wrong ones not used (10-30: a Captiva at € 2,019 rated a Nigopi a bargain while the
    # screening's own note said "another, stronger model"); looking for the right one stays the switch's
    check = [nr for nr, h in enumerate(hits, 1) if geizhals_check.unchecked(h.geizhals)]
    try:
        by_nr, cands = await ask(agent, hits, check, _stats(agent_id, {h.id for h in hits}))
    except LLMError as e:
        log.warning("screening failed for agent %s: %s", agent_id, e)
        return 0
    judged = 0
    with session() as db:
        for nr, h in enumerate(hits, 1):
            r = by_nr.get(nr)
            if r is None:
                continue
            row = db.get(Hit, h.id)
            for k, v in verdict_fields(r, row).items():
                setattr(row, k, v)
            row.screen_at = utcnow()
            db.add(row)
            judged += 1
        db.commit()
    if check:               # the product check's answers: right ones kept, wrong ones looked for
        nrs = [nr for nr in check if nr in by_nr]
        verdicts = {i: {"ok": by_nr[nr].get("geizhals_ok"), "product": by_nr[nr].get("product"),
                        "queries": by_nr[nr].get("queries"), "parts": by_nr[nr].get("parts"),
                        "pick": by_nr[nr].get("geizhals_pick"), "candidates": cands.get(nr) or []}
                    for i, nr in enumerate(nrs, 1)}
        await geizhals_check.settle(agent, [hits[nr - 1] for nr in nrs], verdicts, search=bool(agent.geizhals_check))
    for h in hits:          # callers keep using their objects
        with session() as db:
            fresh = db.get(Hit, h.id)
        h.screen_relevant, h.screen_deal, h.screen_reason = fresh.screen_relevant, fresh.screen_deal, fresh.screen_reason
        h.screen_issues = fresh.screen_issues
    return judged


async def screen_all(agent_id: int, hits: list[Hit]) -> set[int]:
    """Screen up to MAX_PER_RUN hits, MAX_PER_CALL per Claude call. Returns the ids Claude was sent: a call that judged
    none (failed) counts, its hits fail open, but ends the round; the hits after it are not sent."""
    asked: set[int] = set()
    hits = hits[:MAX_PER_RUN]
    for i in range(0, len(hits), MAX_PER_CALL):
        part = hits[i:i + MAX_PER_CALL]
        asked |= {h.id for h in part}
        if not await screen(agent_id, part):
            break
    return asked
