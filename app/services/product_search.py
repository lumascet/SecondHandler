"""Produktsuche: a free description -> Claude plans willhaben searches -> the app runs them -> Claude picks the best.

Flow of one search (a background task, the page polls its progress):
1. plan   – ONE Claude call (purpose "search"): summary, budget, criteria and up to N keyword searches. The depth
            setting bounds N (3 / 6 / 12, hard cap MAX_QUERIES); two toggles widen the plan (unusual names, other
            categories), they never raise the cap. The same call may add up to MAX_QUESTIONS short questions
            ("Rückfragen") about things the description leaves open that matter for judging the offers (setting
            "ask", on by default).
2. search – the queries run strictly one after another through the anonymous client (get_public(), the shared
            willhaben RateLimiter + hourly budget): one request per query, nothing else. Ads are de-duplicated by
            ad id; "Suche"/"Tausch" ads, ads far above the budget and ads whose title has an exclusion word are
            dropped in code; at most MAX_CANDIDATES go on, taken round-robin so every query is represented.
            A block stops the searching; what was found so far is still ranked. A used-up hourly budget only
            delays it: the search waits until the site has room again (the page says until when), then goes on.
            If the plan has questions, the cards are stored and the search waits in status "asking" (no task, no
            Claude call) until the person answers or skips them (continue_run); the answers go into the ranking and
            photo prompts and into the screening brief of a search agent made from the search.
            Facebook (BAND_SITES, DETAIL_SITES) serves only ~24 listings per search without a login and no ad text:
            there each query also runs in the lower and upper half of its budget (at most MAX_BANDS by depth,
            round-robin over the queries, after all whole-range searches), and before the ranking the listing pages
            of up to MAX_DETAILS shortlisted Facebook candidates fill in their description (load_details).
3. rank   – ONE more Claude call: the best TOP_N with a fit score, a short reason and red flags. The app adds the
            price position (deals.position against the other ads of the same query, no extra request) and the
            rule-based scam flags (scam.flags, pure function).
4. photos – optional (setting "photos", on by default), ONE more Claude call: the thumbnails of the ranked results
            (at most TOP_N, downloaded from the willhaben CDN) go to Claude with the text-stage fit and reason; it says
            whether each photo shows the item and its condition and gives the new fit, weighed against what the
            person cares about (no fixed caps; apply_photos only limits a raise). If it fails, the text-only fits
            stay.

Every search is stored in the ProductSearch table; a stored search can be re-run with its plan (no new plan call,
no new questions; the stored answers are kept).
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import nullcontext
import logging
import re
import tempfile
import time
from types import SimpleNamespace
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from app.db import ProductSearch, session, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import (
    SEARCH_PHOTO_SCHEMA,
    SEARCH_PHOTO_SYSTEM,
    SEARCH_PLAN_SCHEMA,
    SEARCH_PLAN_SYSTEM,
    SEARCH_RANK_SCHEMA,
    SEARCH_RANK_SYSTEM,
    search_photo_prompt,
    search_plan_prompt,
    search_rank_context,
    search_rank_list,
)
from app.services import ad_images, deals, geo, price_zones, scam
from app import platforms
from app.platforms import Blocked, Listing, PlatformError, SearchPage, SearchSpec, httpclient, ratelimit, shipping_info
from app.platforms.text import mentions
from app.services import timeline

log = logging.getLogger(__name__)

DEPTHS = {"normal": 3, "thorough": 6, "deep": 12}   # max. queries (= willhaben requests) per depth
MAX_QUERIES = 12
ROWS = 30
SORTS = {"relevance": 7, "newest": 1}        # willhaben's codes; the adapters map the names
MAX_CANDIDATES = 40          # offers per ranking call (a batch); every candidate is ranked, batch by batch
TOP_N = 10                   # the best few that get the photo check and their shipping loaded
RANK_PARALLEL = 5            # ranking calls at the same time (a 600-offer search: 3 rounds, not 15 calls in a row)
MAX_RANKED = 600             # offers ranked in one search (15 calls at most); the rest waits for "load more"
PER_SITE = (30, 100, 200, 500)   # ads per search and site the form offers (page after page up to it)
PER_SITE_DEFAULT = 100
OVER_BUDGET_CHOICES = {"1.5": 1.5, "2": 2.0, "none": 0.0}   # ads above factor × budget are left out; 0 = no limit
MAX_PAGES = 25               # pages of one search on one site at most (a safety stop)
MAX_POOL = 160               # offers kept after a search for "load more" (the next candidates, then the passed-over)
OVER_BUDGET = 1.5            # default: ads above 1.5 × the budget are not worth a look
TEXT_CHARS = 160             # ad text per candidate in the ranking prompt (the token cost of every batch)
MAX_DESCRIPTION = 2000
RECENT = 10
RUNNING = ("planning", "searching", "ranking", "photos", "prices", "more")   # "asking" waits for the person, no task runs
CLAUDE_CALLS = 2             # plan + ranking; the templates add one when settings["photos"] is on
MAX_PHOTOS = TOP_N           # photos sent in the photo check
PHOTO_RAISE = 10             # the photo check may raise the text fit by at most this much
PHOTO_VERDICTS = ("match", "unclear", "mismatch")
CONDITIONS = ("good", "worn", "damaged", "unclear")
MAX_QUESTIONS = 3            # clarifying questions from the plan call
MAX_OPTIONS = 4
MAX_ANSWER = 200             # chars of one answer (free text included)
# Facebook serves only ~24 listings per search without a login, and its lists carry no ad text. So each query there also
# runs in two price bands inside the budget, and the shortlist's listing pages are loaded before the ranking. Facebook
# paces at 8 s per request and 60 per hour: the extras stay a few per search.
BAND_SITES = ("facebook",)
MAX_BANDS = {"normal": 4, "thorough": 6, "deep": 8}   # band searches per site and product search, by depth
DETAIL_SITES = ("facebook",)
MAX_DETAILS = 8              # shortlisted candidates per site whose listing page (description) is loaded
_ID_RE = re.compile(r"[^a-z0-9_]+")
DEFAULT_PATH = "kaufen-und-verkaufen/marktplatz"
_PATH_RE = re.compile(r"^kaufen-und-verkaufen/marktplatz/[a-z0-9-]+-\d+$")

_transport = None             # tests inject a mock transport for the photo downloads
_tasks: set[asyncio.Task] = set()
_active: set[int] = set()     # searches with a live background task in this process
_waits: dict[int, ratelimit.BudgetWait] = {}    # per search: the sites whose hourly budget it waits for right now


class Busy(RuntimeError):
    """Another product search is still running."""


# ------------------------------------------------------------------ settings + plan
def search_platforms() -> list[platforms.Platform]:
    """Sites the product search may use: switched on, with a keyword search, "Use for the product search" on."""
    from app.db import get_setting

    out = [p for p in platforms.all() if p.can("keyword_search")
           and get_setting(f"platform_{p.id}_product_search") != "0"]
    return out or [platforms.get("willhaben")]


CONDITIONS_WANTED = ("any", "new", "used")


def condition_wanted(new: bool, used: bool) -> str:
    """The form's two boxes: one ticked = only that, both or none = any."""
    return "new" if new and not used else "used" if used and not new else "any"


def make_settings(depth: str | None = "normal", unusual: bool = False, categories: bool = False,
                  sort: str | None = "relevance", photos: bool = True, ask: bool = True,
                  sites: list[str] | None = None, condition: str = "any", per_site: int | None = None,
                  over_budget: float | None = None, gh_level: str | None = None,
                  gh_pct: int | None = None) -> dict[str, Any]:
    """The search's options; `sites` = platform ids to search on (default: search_platforms()); `condition`: any,
    new or used (the person said it in the form: Claude does not ask, offers stating the other one are left out);
    `per_site`: ads per search and site (PER_SITE, paged); `over_budget`: factor of the budget above which ads are
    left out (OVER_BUDGET_CHOICES; 0 = no limit); `gh_level` / `gh_pct`: the search agents' Geizhals rule (a price zone
    or cheaper, or at least this many % below the new price; stored only when set, the level wins)."""
    from app.services.search_agents import LEVELS
    usable = {p.id for p in platforms.all() if p.can("keyword_search")}
    if gh_level in LEVELS:
        rule: dict[str, Any] = {"gh_level": gh_level}
    elif gh_pct:
        rule = {"gh_pct": min(max(int(gh_pct), 1), 95)}
    else:
        rule = {}
    chosen = [s for s in dict.fromkeys(sites or []) if s in usable] or [p.id for p in search_platforms()]
    return {"depth": depth if depth in DEPTHS else "normal", "unusual": bool(unusual), "categories": bool(categories),
            "sort": sort if sort in SORTS else "relevance", "photos": bool(photos), "ask": bool(ask),
            "platforms": chosen, "condition": condition if condition in CONDITIONS_WANTED else "any",
            "per_site": per_site if per_site in PER_SITE else PER_SITE_DEFAULT,
            "over_budget": over_budget if over_budget in OVER_BUDGET_CHOICES.values() else OVER_BUDGET, **rule}


def condition_note(settings: dict[str, Any] | None) -> tuple[str, str] | None:
    """The form's new/used choice as a question with its answer for Claude's prompts; None for any."""
    c = (settings or {}).get("condition") or "any"
    return None if c == "any" else ("Neu oder gebraucht?", "nur neu" if c == "new" else "nur gebraucht")


def condition_fits(ad: Listing, settings: dict[str, Any] | None) -> bool:
    """An offer whose stated condition is the other one is no candidate; one stating none stays."""
    c = (settings or {}).get("condition") or "any"
    if c == "any" or not ad.item_condition:
        return True
    return price_zones.group(ad.item_condition) == c


def max_queries(settings: dict[str, Any] | None) -> int:
    return min(DEPTHS.get((settings or {}).get("depth") or "normal", DEPTHS["normal"]), MAX_QUERIES)


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v >= 0 else None


def _texts(v: Any, limit: int = 10, chars: int = 120) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [re.sub(r"\s+", " ", x).strip()[:chars] for x in v if isinstance(x, str)]
    return [x for x in out if x][:limit]


def normalize_questions(v: Any) -> list[dict[str, Any]]:
    """Claude's clarifying questions, cleaned: slug ids (a-z0-9_, unique), one-line text (≤ 120 chars), 2 to 4
    unique options; malformed ones (no text, < 2 or > 4 options, empty or repeated id) are dropped; at most
    MAX_QUESTIONS."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for q in v if isinstance(v, list) else []:
        if not isinstance(q, dict) or not isinstance(q.get("id"), str) or not isinstance(q.get("text"), str):
            continue
        qid = _ID_RE.sub("_", q["id"].strip().lower()).strip("_")[:30]
        text = _one_line(q["text"])[:120]
        raw = q.get("options")
        if not qid or qid in seen or not text or not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
            continue
        options: list[str] = []
        for o in raw:
            o = _one_line(o)[:60] if isinstance(o, str) else ""
            if o and o.lower() not in {x.lower() for x in options}:
                options.append(o)
        if len(options) < 2:
            continue
        seen.add(qid)
        out.append({"id": qid, "text": text, "options": options})
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def normalize_plan(data: dict[str, Any], limit: int) -> dict[str, Any]:
    """Claude's plan, cleaned: valid numbers, short strings, unique keywords, at most `limit` (≤ 12) queries, at
    most MAX_QUESTIONS questions."""
    limit = max(1, min(limit, MAX_QUERIES))
    max_price = _num(data.get("max_price"))
    queries, seen = [], set()
    for q in data.get("queries") or []:
        if not isinstance(q, dict) or not isinstance(q.get("keyword"), str):
            continue
        kw = re.sub(r"\s+", " ", q["keyword"]).strip()[:60]
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        pf, pt = _num(q.get("price_from")), _num(q.get("price_to"))
        if pf is not None and pt is not None and pf > pt:
            pf = None
        path = q.get("category_path") if isinstance(q.get("category_path"), str) else None
        path = path.strip().strip("/") if path else None
        hint = q.get("category_hint") if isinstance(q.get("category_hint"), str) else None
        queries.append({"keyword": kw, "price_from": pf, "price_to": pt or None,
                        "category_hint": (hint or "").strip()[:80] or None,
                        "category_path": path if path and _PATH_RE.match(path) else None,
                        "why": str(q.get("why") or "").strip()[:200]})
        if len(queries) >= limit:
            break
    return {"summary": str(data.get("summary") or "").strip()[:500], "max_price": max_price or None,
            "must_have": _texts(data.get("must_have")), "nice_to_have": _texts(data.get("nice_to_have")),
            "exclude": _texts(data.get("exclude")), "queries": queries,
            "questions": normalize_questions(data.get("questions"))}


async def make_plan(description: str, settings: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """(plan, model). Raises LLMError."""
    n = max_queries(settings)
    ask = bool(settings.get("ask"))
    prompt = search_plan_prompt(description=description, max_queries=n, unusual_names=bool(settings.get("unusual")),
                                other_categories=bool(settings.get("categories")), ask=ask,
                                condition=(condition_note(settings) or (None, None))[1])
    out = await get_backend("search").complete(system=SEARCH_PLAN_SYSTEM, prompt=prompt, schema=SEARCH_PLAN_SCHEMA)
    plan = normalize_plan(out.data or {}, n)
    if not ask:
        plan["questions"] = []
    return plan, out.model


# ------------------------------------------------------------------ questions + answers
def clean_answers(plan: dict[str, Any] | None, raw: dict[str, Any] | None) -> dict[str, str]:
    """{question id: answer} for the plan's questions only, each answer on one line (it goes into prompts) and
    short; empty answers are left out."""
    ids = {q.get("id") for q in (plan or {}).get("questions") or [] if isinstance(q, dict)}
    out = {}
    for k, v in (raw or {}).items():
        a = _one_line(v)[:MAX_ANSWER] if isinstance(v, str) else ""
        if k in ids and a:
            out[k] = a
    return out


def answer_pairs(plan: dict[str, Any] | None, answers: dict[str, Any] | None,
                 settings: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """(question text, answer) in question order; unanswered questions are left out. With `settings` the form's
    new/used choice comes first (condition_note)."""
    out = [note] if (note := condition_note(settings)) else []
    for q in (plan or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        a = (answers or {}).get(q.get("id"))
        if isinstance(a, str) and _one_line(a):
            out.append((_one_line(q.get("text"))[:120], _one_line(a)[:MAX_ANSWER]))
    return out


def preferences_text(plan: dict[str, Any] | None, answers: dict[str, Any] | None) -> str:
    """The answers as plain German lines ("Rückfragen und Antworten:" + "– question: answer"), "" without any."""
    pairs = answer_pairs(plan, answers)
    if not pairs:
        return ""
    return "\n".join(["Rückfragen und Antworten:", *[f"– {q}: {a}" for q, a in pairs]])


# ------------------------------------------------------------------ searching
def drop_reason(ad: Listing, plan: dict[str, Any], q: dict[str, Any] | None = None,
                settings: dict[str, Any] | None = None) -> str | None:
    """Why an ad is not a candidate (cheap checks in code), or None. The query's price_to is applied here,
    not sent to willhaben, so pricier ads of the same search still count as price comparables. The form's budget
    factor (settings["over_budget"], 0 = no limit) decides both price drops."""
    if deals._wanted(ad.heading):
        return "wanted"
    factor = (settings or {}).get("over_budget", OVER_BUDGET)
    mp = plan.get("max_price")
    if factor and mp and ad.price and ad.price > mp * factor:
        return "over_budget"
    pt = (q or {}).get("price_to")
    if factor and pt and ad.price and ad.price > pt * factor / OVER_BUDGET:
        return "over_budget"
    title = (ad.heading or "").lower()
    for word in plan.get("exclude") or []:
        w = word.lower().strip()
        if w and " " not in w and re.search(rf"(?<!\w){re.escape(w)}", title):
            return "excluded"
    return None


LOOSE_SITES = ("shpock",)   # a page full of anything near one word (10/2026: "UCG Fiber" -> 30 ads, none of them one);
                            # Vinted filters its own results, Facebook's search is semantic ("Mountainbike" finds MTB)


def _on_topic(p: platforms.Platform, ad: Listing, q: dict[str, Any]) -> bool:
    """On a loose site an offer is a candidate only when its title and text mention every word of the search."""
    return p.id not in LOOSE_SITES or mentions(f"{ad.heading} {ad.body or ''}", q.get("keyword") or "")


def _remaining_budget(p: platforms.Platform) -> int | None:
    lim = p.limiter
    return max(0, lim.per_hour - lim.last_hour) if lim.per_hour else None


def run_sites(settings: dict[str, Any] | None) -> list[platforms.Platform]:
    """The sites a run of the search uses: its chosen ones that are known, switched on and can search, else willhaben
    (no settings: the defaults, as run() uses them)."""
    out = []
    for pid in (settings or make_settings()).get("platforms") or ["willhaben"]:
        try:
            p = platforms.get(pid)
        except platforms.UnknownPlatform:
            continue
        if platforms.enabled(p.id) and p.can("keyword_search"):
            out.append(p)
    return out or [platforms.get("willhaben")]


def query_spec(p: platforms.Platform, q: dict[str, Any], settings: dict[str, Any] | None) -> SearchSpec:
    """One planned query on one site as run_queries sends it: the search's sort, the willhaben category only on
    willhaben, no price_to (drop_reason applies it, so the pricier ads stay comparables)."""
    return p.keyword_search(q.get("keyword") or "", q.get("price_from"), None,
                            sort=(settings or make_settings()).get("sort") or "relevance",
                            category=q.get("category_path") if p.id == "willhaben" else None)


def planned(plan: dict[str, Any] | None, settings: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The plan's searches before they ran: each query once per site the run will use, query-major like per_query."""
    sites = run_sites(settings)
    return [{**q, "platform": p.id} for q in (plan or {}).get("queries") or [] for p in sites]


_TERM_FIELDS = ("keyword", "price_from", "price_to", "why", "category_hint", "category_path")


def by_term(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The searches table: each search term once (its plan fields) with `sites` = its rows in run order (per_query
    entries or planned() rows: one per site, query-major), found/kept = the sums over the sites that answered (None:
    none did, or not run yet) and, when none answered, error/skipped = whether one failed / all were skipped (a
    per_query entry is found, failed or skipped). A site that comes round again starts the next term, even with the
    same keyword."""
    terms: list[dict[str, Any]] = []
    for q in rows or []:
        term = terms[-1] if terms else None
        if (term is None or term["keyword"] != q.get("keyword")
                or any(s.get("platform") == q.get("platform") for s in term["sites"])):
            term = {**{k: q.get(k) for k in _TERM_FIELDS}, "sites": []}
            terms.append(term)
        term["sites"].append(q)
    for term in terms:
        ran = [s for s in term["sites"] if s.get("found") is not None]
        term["found"] = sum(s["found"] for s in ran) if ran else None
        term["kept"] = sum(s.get("kept") or 0 for s in ran) if ran else None
        term["error"] = not ran and any(s.get("error") for s in term["sites"])
        term["skipped"] = not ran and not term["error"] and any(s.get("skipped") for s in term["sites"])
    return terms


def price_bands(q: dict[str, Any], plan: dict[str, Any]) -> list[tuple[float | None, float]]:
    """The price bands a query also runs in on a BAND_SITES site, (from, to): the lower and the upper half of its budget
    (the query's price_to, else the plan's max_price) above its price_from. No budget, no bands: there are no halves to
    search then, and the whole-range search already ran."""
    budget = q.get("price_to") or plan.get("max_price")
    low = q.get("price_from") or 0
    if not budget or budget - low < 2:
        return []
    mid = float(round(low + (budget - low) / 2))
    return [(low or None, mid), (mid, float(budget))]


def max_bands(settings: dict[str, Any] | None) -> int:
    return MAX_BANDS.get((settings or {}).get("depth") or "normal", MAX_BANDS["normal"])


def band_runs(plan: dict[str, Any] | None, settings: dict[str, Any] | None) -> list[tuple[int, float | None, float]]:
    """(run index, price from, price to) of the band searches run_queries sends after the whole-range ones: on each
    BAND_SITES site every query's lower half, then every query's upper half, at most max_bands per site."""
    queries = (plan or {}).get("queries") or []
    sites = run_sites(settings)
    runs = [(q, p) for q in queries for p in sites]
    out: list[tuple[int, float | None, float]] = []
    for p in sites:
        if p.id not in BAND_SITES:
            continue
        mine = [(i, price_bands(q, plan or {})) for i, (q, rp) in enumerate(runs) if rp.id == p.id]
        out += [(i, *bs[k]) for k in range(2) for i, bs in mine if k < len(bs)][:max_bands(settings)]
    return out


async def run_queries(plan: dict[str, Any], settings: dict[str, Any],
                      on_progress: Callable[[int, int, int], None] | None = None) -> dict[str, Any]:
    """Run each of the plan's queries on every site of the search, one after another, then the price bands.

    Returns {per_query, pools, prices, query_of, requests, warning}: one entry per (query, site) run, query-major;
    pools[i] = the new, kept ads of run i (relevance order), prices[i] = all prices of run i (comparables from the
    same site), query_of[i] = the plan query's index. requests counts only requests that actually went out (not those
    a limiter refused); on_progress(done, total, requests). A block stops only that site's remaining searches.

    On a BAND_SITES site the runs come back once more in price_bands, after every whole-range search: each query's
    lower half, then each one's upper half, at most max_bands per site. A band belongs to its run: its new ads go
    into that run's pool, its count into the run's found, and the run's entry lists it under `bands` ({price_from,
    price_to, url, found, kept, error, skipped}). Band prices are no comparables (a band would skew the median); a run
    that did not answer gets no bands, and the hourly budget only ever goes to bands when the whole-range searches
    are done."""
    queries = plan.get("queries") or []
    sites = run_sites(settings)
    sort = (settings or make_settings()).get("sort") or "relevance"
    runs = [(qi, q, p) for qi, q in enumerate(queries) for p in sites]
    per_query = []
    for _qi, q, p in runs:
        spec = query_spec(p, q, settings)
        per_query.append({**q, "platform": p.id, "url": spec.url, "spec": spec.to_json(), "found": None, "kept": 0,
                          "error": None, "skipped": False})
    jobs: list[tuple[int, int | None]] = [(i, None) for i in range(len(runs))]     # (run, band index or None)
    for i, lo, hi in band_runs(plan, settings):
        q, p = runs[i][1], runs[i][2]
        spec = p.keyword_search(q.get("keyword") or "", lo, hi, sort=sort)
        per_query[i].setdefault("bands", []).append({"price_from": lo, "price_to": hi, "url": spec.url,
                                                     "spec": spec.to_json(), "found": None, "kept": 0,
                                                     "error": None, "skipped": False})
        jobs.append((i, len(per_query[i]["bands"]) - 1))
    pools: list[list[Listing]] = [[] for _ in runs]
    prices: list[list[float]] = [[] for _ in runs]
    seen: set[str] = set()
    requests, warnings = 0, []
    left: dict[str, int | None] = {}
    for p in sites:
        budget = None if ratelimit.waits() else _remaining_budget(p)   # a background search waits for the budget
        wanted = sum(1 for _, _, rp in runs if rp.id == p.id)
        left[p.id] = budget
        if budget is not None and budget < wanted:
            warnings.append(t("Hourly request limit of {platform} almost used up: only {n} of {total} searches run.",
                              platform=p.name, n=budget, total=wanted))
    stopped: set[str] = set()
    count = {"done": 0, "requests": 0}

    deep: dict[int, dict[str, Any]] = {}      # runs with further result pages to fetch: {spec, got, nr, want, size}

    def take(i: int, k: int | None, page: SearchPage) -> int:
        """A page's ads into run i's pool (new, kept ones) and its prices (comparables); returns how many were kept."""
        _qi, q, p = runs[i]
        new = 0
        for ad in page.ads:
            if k is None and ad.price and ad.price > 0 and not deals._wanted(ad.heading):
                prices[i].append(ad.price)
            if not ad.ad_id or ad.ref in seen:
                continue
            seen.add(ad.ref)
            if drop_reason(ad, plan, q, settings) is None and _on_topic(p, ad, q) and condition_fits(ad, settings):
                pools[i].append(ad)
                new += 1
        return new

    def more_pages(p: platforms.Platform, st: dict[str, Any], page: SearchPage) -> bool:
        """Is there a next page worth a request: not enough yet, not the last page, the site not empty – and, where
        the site's total is exact (TOTAL_KNOWN), not all of it seen."""
        if not page.ads or st["got"] >= st["want"] or st["nr"] >= min(MAX_PAGES, -(-st["want"] // st["size"])):
            return False
        return not (getattr(p, "TOTAL_KNOWN", False) and page.rows_found and st["got"] >= page.rows_found)

    async def one(i: int, k: int | None) -> None:
        """The first page of run i (or its band k); further pages come later, after every run's first (deeper())."""
        _qi, q, p = runs[i]
        entry = per_query[i] if k is None else per_query[i]["bands"][k]
        if p.id in stopped or (left[p.id] is not None and left[p.id] <= 0) \
                or (k is not None and per_query[i]["found"] is None):
            entry["skipped"] = True
            return
        if left[p.id] is not None:
            left[p.id] -= 1
        want = int((settings or {}).get("per_site") or PER_SITE_DEFAULT) if k is None else ROWS
        size = max(1, int(getattr(p, "PAGE_SIZE_MAX", ROWS) or ROWS))
        paging = k is None and getattr(p, "CAN_PAGE", False) and want > size
        spec = SearchSpec.from_json(entry["spec"])
        handed_back = False
        try:
            page = await p.search(spec, rows=size if paging else want)   # uncached: real requests
            count["requests"] += 1
        except httpclient.LaneLost as e:            # this lane's server is out: another lane runs the search
            count["requests"] += int(getattr(e, "sent", False))
            if left[p.id] is not None:
                left[p.id] += 1
            handed_back = True
            raise
        except Blocked as e:
            count["requests"] += int(getattr(e, "sent", False))   # False: refused by the limiter, nothing was sent
            entry["error"] = str(e)
            stopped.add(p.id)
            done = sum(1 for e2 in per_query if e2["platform"] == p.id and e2["found"] is not None)
            warnings.append(t("{platform} paused the search ({error}): stopped after {n} searches there, the offers "
                              "found so far are ranked.", platform=p.name, error=e, n=done))
            return
        except PlatformError as e:
            count["requests"] += 1                          # went through the limiter (network error / HTTP status)
            entry["error"] = str(e)
            return
        finally:
            if not handed_back:
                count["done"] += 1
                if on_progress:
                    on_progress(count["done"], len(jobs), count["requests"])
        entry["found"] = page.rows_found
        entry["kept"] = take(i, k, page)
        if k is not None:
            per_query[i]["found"] += page.rows_found
        per_query[i]["kept"] = len(pools[i])
        if paging:
            st = {"spec": spec, "got": len(page.ads), "nr": 1, "want": want, "size": size}
            if more_pages(p, st, page):
                deep[i] = st

    async def deeper(i: int) -> None:
        """The next result page of run i (a request of its own); the run leaves `deep` when it has no more."""
        _qi, q, p = runs[i]
        st = deep[i]
        if left[p.id] is not None:
            left[p.id] -= 1
        st["nr"] += 1
        try:
            page = await p.search(st["spec"], rows=st["size"], page=st["nr"])
            count["requests"] += 1
        except httpclient.LaneLost as e:            # this lane's server is out: the page comes in another round
            count["requests"] += int(getattr(e, "sent", False))
            st["nr"] -= 1
            if left[p.id] is not None:
                left[p.id] += 1
            raise
        except Blocked as e:                               # the pages so far stay counted; this site stops paging
            count["requests"] += int(getattr(e, "sent", False))
            stopped.add(p.id)
            warnings.append(t("{platform} paused the search ({error}): no further result pages there, the offers "
                              "found so far are ranked.", platform=p.name, error=e))
            deep.pop(i, None)
            return
        except PlatformError as e:
            count["requests"] += 1
            log.info("page %s of a %s search failed: %s", st["nr"], p.id, e)
            deep.pop(i, None)
            return
        if on_progress:
            on_progress(count["done"], len(jobs), count["requests"])
        st["got"] += len(page.ads)
        take(i, None, page)
        per_query[i]["kept"] = len(pools[i])
        per_query[i]["pages"] = st["nr"]
        if not more_pages(p, st, page):
            deep.pop(i, None)

    async def site(pid: str) -> None:
        """One site's searches: every search's first page in their order (its limiter paces them), then the price
        bands, then the further pages round by round, so the hourly budget reaches every search. With parallel VPN
        servers (Settings → VPN) each phase is spread over the site's lanes: one worker per server, each paced by
        its own limiter; a lane whose server the site refuses hands its item back and stops."""
        p = next(x for x in sites if x.id == pid)
        mine = [(i, k) for i, k in jobs if runs[i][2].id == pid]
        lanes = await _site_lanes(p) if len(mine) > 1 else []
        for ln in lanes:                             # each helper server has a budget of its own
            if left[pid] is not None:
                free = (ln.limiter.per_hour - ln.limiter.last_hour) if ln.limiter.per_hour else None
                left[pid] = None if free is None else left[pid] + free
        workers: list[httpclient.Lane | None] = [None, *lanes]
        if lanes:
            lanes_used[pid] = len(workers)

        async def phase(items: list[Any], fn: Callable[..., Any]) -> None:
            todo = deque(items)

            async def work(ln: httpclient.Lane | None) -> None:
                with httpclient.lane(ln) if ln is not None else nullcontext():
                    # a stopped site's first pages still pass one(): it marks them skipped without a request
                    while todo and (pid not in stopped or fn is one):
                        item = todo.popleft()
                        try:
                            await fn(*item) if isinstance(item, tuple) else await fn(item)
                        except httpclient.LaneLost:
                            todo.appendleft(item)
                            workers.remove(ln)
                            return
            await asyncio.gather(*(work(ln) for ln in list(workers)))

        try:
            await phase([(i, k) for i, k in mine if k is None], one)
            await phase([(i, k) for i, k in mine if k is not None], one)
            while runs_deep := [i for i in deep if runs[i][2].id == pid]:
                if pid in stopped:
                    for i in runs_deep:
                        deep.pop(i, None)
                    break
                if left[pid] is not None and left[pid] <= 0:
                    for i in runs_deep:
                        deep.pop(i, None)
                    if not any(w.startswith(f"[{pid}]") for w in short):
                        short.append(f"[{pid}]")
                        warnings.append(t("Hourly request limit of {platform} reached: further result pages were "
                                          "left out.", platform=p.name))
                    break
                await phase(runs_deep[:max(1, left[pid])] if left[pid] is not None else runs_deep, deeper)
        finally:
            for ln in lanes:
                ln.close()

    short: list[str] = []
    lanes_used: dict[str, int] = {}
    # the sites at once: each answers at its own pace, so a search takes as long as its slowest site, not their sum
    try:
        await asyncio.gather(*(site(p.id) for p in sites))
    finally:
        if lanes_used:
            from app.platforms import vpn
            vpn.conn.release_helpers()
    requests = count["requests"]
    for e in per_query:
        e.pop("spec", None)
        for b in e.get("bands") or []:
            b.pop("spec", None)
    return {"per_query": per_query, "pools": pools, "prices": prices, "query_of": [qi for qi, _, _ in runs],
            "requests": requests, "warning": " ".join(warnings) or None, "lanes": lanes_used}


async def _site_lanes(p: platforms.Platform) -> list[httpclient.Lane]:
    """The helper lanes of one site (beside its own server) when parallel VPN servers are set up and its VPN is on;
    [] otherwise or when none starts (the search then runs on the site's own server, as before)."""
    from app.platforms import vpn
    if vpn.parallel() <= 1:
        return []
    try:
        routes = await vpn.lanes(p.id)
    except PlatformError as e:
        log.info("no parallel lanes for %s: %s", p.id, e)
        return []
    return [httpclient.Lane(p.id, r, httpclient.lane_limiter(p.id, r.name, p.limiter)) for r in routes[1:]]


def detail_todo(picked: list[tuple[int, Listing]]) -> list[tuple[int, Listing]]:
    """The candidates load_details opens: among the first MAX_RANKED (those Claude ranks), per DETAIL_SITES site
    the first MAX_DETAILS without a text, in pick order."""
    out: list[tuple[int, Listing]] = []
    for pi, ad in picked[:MAX_RANKED]:
        if ad.platform in DETAIL_SITES and not ad.body \
                and sum(1 for _, a in out if a.platform == ad.platform) < MAX_DETAILS:
            out.append((pi, ad))
    return out


async def load_details(picked: list[tuple[int, Listing]], per_query: list[dict[str, Any]],
                       on_progress: Callable[[int, int], None] | None = None
                       ) -> tuple[list[tuple[int, Listing]], int, str | None]:
    """(picked, requests, warning): the listing pages of detail_todo(picked) fill in the description, the place, the
    dealer flag and the condition, so Claude ranks them by more than the title; a listing the site no longer shows
    (gone, sold) leaves the candidates. per_query[pool]["details"] / ["gone"] count them per search. A block stops
    that site's loading (the rest keep their title only), an error skips one ad. on_progress(done, total)."""
    todo = detail_todo(picked)
    gone: set[str] = set()
    stopped: dict[str, str] = {}
    requests = 0
    for n, (pi, ad) in enumerate(todo, 1):
        if ad.platform in stopped:
            continue
        site = platforms.get(ad.platform)
        before = site.limiter.last_hour       # a page the client still has cached, or a refusal, sends nothing
        try:
            d = await site.detail(ad.ad_id, url=ad.url)
        except Blocked as e:
            stopped[ad.platform] = str(e)
            continue
        except PlatformError:
            continue
        finally:
            requests += max(0, site.limiter.last_hour - before)
            if on_progress:
                on_progress(n, len(todo))
        row = per_query[pi]
        if d is None or d.status == "sold":
            gone.add(ad.ref)
            row["gone"] = row.get("gone", 0) + 1
            continue
        ad.body = d.description or ad.body
        ad.location = d.location or ad.location
        if d.is_private is not None:
            ad.is_private = d.is_private
        if d.item_condition:
            ad.item_condition, ad.item_condition_label = d.item_condition, d.item_condition_label
        row["details"] = row.get("details", 0) + 1
    warning = " ".join(t("{platform} paused loading the descriptions ({error}); the other offers are ranked by their "
                         "title.", platform=platforms.get(pid).name, error=err) for pid, err in stopped.items())
    return [(pi, ad) for pi, ad in picked if ad.ref not in gone], requests, warning or None


def pick_candidates(pools: list[list[Listing]], cap: int = MAX_CANDIDATES) -> list[tuple[int, Listing]]:
    """Round-robin over the queries (each keeps its relevance order) so every query gets a say; (query idx, ad)."""
    out: list[tuple[int, Listing]] = []
    depth = max((len(p) for p in pools), default=0)
    for k in range(depth):
        for i, pool in enumerate(pools):
            if k < len(pool):
                out.append((i, pool[k]))
                if len(out) >= cap:
                    return out
    return out


def _price_without(prices: list[float], price: float | None) -> list[float]:
    rest = list(prices)
    if price in rest:
        rest.remove(price)       # the ad itself is not its own comparable
    return rest


def _one_line(text: str | None) -> str:
    """Seller text collapsed to one line, so it cannot fake a new candidate entry or task line in the prompt."""
    return re.sub(r"\s+", " ", text or "").strip()


def card(qi: int, ad: Listing, plan: dict[str, Any], prices: list[float]) -> dict[str, Any]:
    """Everything the result list shows about one ad, price position and scam flags included (no request)."""
    comp = _price_without(prices, ad.price)
    st = deals.stats(comp)
    pos = deals.position(ad.price, st, None, ad.item_condition)    # lists know no new price: comparables only
    item = {"heading": ad.heading, "body": ad.body, "price": ad.price, "is_private": ad.is_private}
    return {"ad_id": ad.ad_id, "platform": ad.platform, "ref": ad.ref, "title": ad.heading, "price": ad.price,
            "price_display": ad.price_display,
            "location": ad.location, "postcode": ad.postcode, "lat": ad.lat, "lon": ad.lon,   # distance from home
            "url": ad.url, "image_url": ad.image_url, "is_private": ad.is_private,
            "published_at": ad.published_at.isoformat() if ad.published_at else None,
            "text": _one_line(ad.body)[:TEXT_CHARS], "query": (plan.get("queries") or [{}])[qi].get("keyword"),
            "position": pos, "median": st["median"] if st else None, "min": st["min"] if st else None,
            "p25": st["p25"] if st else None, "comparables": st["count"] if st else 0, "stats": st,
            "item_condition": ad.item_condition, "item_condition_label": ad.item_condition_label,
            "shipping_price": ad.shipping_price, "shipping": ad.shipping,
            "scam_flags": scam.flags(item, scam.median_context(comp), home_country=scam.home_country_of(ad.platform))}


# ------------------------------------------------------------------ new prices (Geizhals)
MAX_NEW_PRICES = 30           # Geizhals lookups per search (one per product name, 3 s apart): the best offers first


def new_price(r: dict[str, Any]) -> float | None:
    return (r.get("geizhals") or {}).get("price_from")


def discount(r: dict[str, Any]) -> int | None:
    """How far below the Geizhals new price the offer is, in % (negative: above it); None without both prices, and
    when the comparable ads contradict the new price as the price check sees it (their median outside
    deals.MISMATCH_LOW..MISMATCH_HIGH × new: most likely another product's price)."""
    price, new = r.get("price"), new_price(r)
    if not price or not new or price <= 0 or new <= 0:
        return None
    median = (r.get("stats") or {}).get("median")
    if median and not deals.MISMATCH_LOW <= median / new <= deals.MISMATCH_HIGH:
        return None
    return round((1 - price / new) * 100)


def _lookups(results: list[dict[str, Any]]) -> list[tuple[str | tuple[str, ...], list[dict[str, Any]]]]:
    """The Geizhals lookups the offers not asked yet need, one per product name (whatever its case) or set of parts,
    best offers first, at most MAX_NEW_PRICES. The name is the product Claude named while ranking ("" there: a
    generic offer, no lookup); the parts those of an offer made of several products (their prices are summed); only
    an offer Claude did not rank takes the name its title gives (deals.geizhals_query), which for generic titles
    ("Mini PC Ryzen 7") finds some other product. What needs no request is filled in place: Claude's pick from an
    earlier price check of the ad, {} without a product name (asked, none)."""
    groups: dict[Any, tuple[str | tuple[str, ...], list[dict[str, Any]]]] = {}
    for r in results:
        if "geizhals" in r:
            continue
        if gh := deals.stored_geizhals(str(r.get("ref") or r.get("ad_id"))):
            r["geizhals"] = {k: gh.get(k) for k in ("name", "url", "price_from", "by")}
            continue
        if len(r.get("parts") or []) >= 2:
            key = tuple(r["parts"])
            groups.setdefault(tuple(x.lower() for x in key), (key, []))[1].append(r)
            continue
        q = r.get("product") if "product" in r else deals.geizhals_query(r.get("title"))
        if q:
            groups.setdefault(q.lower(), (q, []))[1].append(r)
        else:
            r["geizhals"] = {}
    return list(groups.values())[:MAX_NEW_PRICES]


def geizhals_rule(settings: dict[str, Any] | None) -> SimpleNamespace | None:
    """The search's Geizhals rule as a search agent holds it (geizhals_level / geizhals_pct), None without one."""
    st = settings or {}
    if not (st.get("gh_level") or st.get("gh_pct")):
        return None
    return SimpleNamespace(geizhals_level=st.get("gh_level"), geizhals_pct=st.get("gh_pct"))


def geizhals_passes(r: dict[str, Any], settings: dict[str, Any] | None) -> bool:
    """Does the offer keep the search's Geizhals rule, judged as a search agent judges a hit
    (search_agents.geizhals_passes, the price zones of today)? Without the rule or a new price: it passes."""
    from app.services import search_agents
    rule = geizhals_rule(settings)
    if rule is None:
        return True
    gh = r.get("geizhals") if discount(r) is not None else None      # a new price the comparables contradict counts not
    hit = SimpleNamespace(price=r.get("price"), geizhals=gh, item_condition=r.get("item_condition"), screen_deal=None)
    return search_agents.geizhals_passes(rule, hit)


async def geizhals_prices(search_id: int, results: list[dict[str, Any]]) -> None:
    """The Geizhals product of every offer not asked yet, as the price check finds it (_lookups; the lookup is
    cached 6 h), in place and stored after each lookup so the list can sort by discount while it runs. When
    Geizhals fails the rest stay not asked."""
    from app.services import geizhals

    todo = _lookups(results)
    row = get(search_id)
    progress = dict((row.progress if row else None) or {})
    for i, (q, rows) in enumerate(todo):
        _update(search_id, results=results, progress={**progress, "prices": {"done": i, "total": len(todo)}})
        try:
            gh = await (deals.parts_new_price(list(q)) if isinstance(q, tuple) else deals.geizhals_match(q, raise_errors=True))
        except geizhals.GeizhalsError:
            break
        for r in rows:
            r["geizhals"] = {k: gh[k] for k in ("name", "url", "price_from", "parts", "missing") if k in gh} if gh else {}
    progress.pop("prices", None)
    _update(search_id, results=results, progress=progress)


_backfill: set[int] = set()      # finished searches whose new prices are being looked up (fill_new_prices)
_backfill_tried: dict[int, float] = {}   # ... and when one ended with offers still not asked (Geizhals refusing)
BACKFILL_RETRY_S = 15 * 60
BACKFILL_ROUNDS = 10             # × MAX_NEW_PRICES lookups at most per opening


def backfilling(search_id: int) -> bool:
    return search_id in _backfill


def fill_new_prices(search_id: int) -> bool:
    """A finished search whose offers were not looked up on Geizhals yet (stored before the step existed, or Geizhals
    failed then, or more than one step's MAX_NEW_PRICES): look them up in the background now, so its list can sort by
    discount. The search stays "done" and blocks nothing – no new search, no other page; only "load more" and a re-run
    of this search wait (backfilling). False when nothing is to do (what needs no request is filled in at once)."""
    row = get(search_id)
    if row is None or row.status != "done" or search_id in _backfill \
            or time.monotonic() - _backfill_tried.get(search_id, -1e9) < BACKFILL_RETRY_S \
            or not any("geizhals" not in r for r in row.results or []):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    results = list(row.results or [])
    if not _lookups(results):                       # nothing that needs a request: stored at once
        _update(search_id, results=results)
        return False
    _backfill.add(search_id)
    _spawn(_new_prices(search_id), search_id)
    return True


async def _new_prices(search_id: int) -> None:
    """Round after round until every offer is asked (or a round gets no further: Geizhals refusing)."""
    try:
        for _ in range(BACKFILL_ROUNDS):
            row = get(search_id)
            if row is None or row.status != "done":
                break
            results = list(row.results or [])
            todo = len(_lookups(list(dict(r) for r in results)))
            if not todo:
                break
            await geizhals_prices(search_id, results)
            if len(_lookups(list(dict(r) for r in results))) >= todo:
                _backfill_tried[search_id] = time.monotonic()        # no further: not again for a while
                break
    except Exception:  # noqa: BLE001 - the offers stay as they were
        log.exception("new prices of product search %s failed", search_id)
        _backfill_tried[search_id] = time.monotonic()
    finally:
        _backfill.discard(search_id)


# ------------------------------------------------------------------ ranking
def _rank_line(nr: int, c: dict[str, Any]) -> dict[str, Any]:
    return {"nr": nr, "title": _one_line(c["title"])[:120],
            "price": shipping_info.with_price(c["price_display"] or (f"{c['price']:g} €" if c["price"] is not None else None),
                                              c.get("shipping_price"), c.get("shipping")),
            "location": c["location"], "private": c["is_private"], "text": c["text"],
            "level": deals.LEVEL_DE.get((c.get("position") or {}).get("level") or ""),
            "scam": [f["label"] for f in scam.warnings(c["scam_flags"])]}


_SHIP_UNKNOWN = re.compile(r"versand\w*\W.*(unklar|unbekannt|nicht angegeben)|(unklar|unbekannt)\w*\W.*versand", re.I)


async def fill_shipping(results: list[dict[str, Any]]) -> None:
    """The best offers that ship but whose list named no price (willhaben, Vinted): their ad page, one request each
    through the site's limiter, so the photo check and the cards know the shipping (in place; failures stay open).
    The sites at once, each at its own pace."""
    todo: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        if r.get("shipping_price") is None and r.get("shipping") == shipping_info.SHIP:   # else: no request
            todo.setdefault(r.get("platform") or "willhaben", []).append(r)
    await asyncio.gather(*(_fill_site(rows) for rows in todo.values()))


async def _fill_site(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        try:
            p = platforms.get(r.get("platform") or "willhaben")
            if not p.can("detail"):
                continue
            d = await p.detail(str(r["ad_id"]), url=r.get("url"))
        except Exception as e:  # noqa: BLE001 - the card loads it later (ship_lazy) or shows none
            log.info("shipping of %s %s not loaded: %s", r.get("platform"), r.get("ad_id"), e)
            continue
        if d is not None and (d.shipping_price is not None or d.shipping):
            r["shipping_price"], r["shipping"] = d.shipping_price, d.shipping or r.get("shipping")
            if getattr(d, "shipping_options", None):
                r["shipping_options"] = d.shipping_options


def red_flags(flags: Any) -> list[str]:
    """Claude's concerns about an offer, without "shipping costs unclear": the app loads them itself (also for
    results stored before the prompt said so)."""
    return [f for f in flags or [] if isinstance(f, str) and not _SHIP_UNKNOWN.search(f)]


def merge_ranking(data: dict[str, Any], cards: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Claude's picks joined with the app's cards (unknown or repeated nr ignored), best fit first."""
    out, used = [], set()
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        nr = r.get("nr")
        if not isinstance(nr, int) or isinstance(nr, bool) or not 1 <= nr <= len(cards) or nr in used:
            continue
        used.add(nr)
        fit = r.get("fit")
        fit = max(0, min(100, int(fit))) if isinstance(fit, (int, float)) and not isinstance(fit, bool) else None
        out.append({**cards[nr - 1], "fit": fit, "reason": str(r.get("reason") or "").strip()[:500],
                    "red_flags": red_flags(_texts(r.get("red_flags"), limit=5, chars=80)),
                    "product": _one_line(str(r.get("product") or ""))[:80],
                    "parts": [_one_line(x)[:80] for x in r.get("parts") or [] if isinstance(x, str) and x.strip()][:deals.MAX_PARTS]})
    out.sort(key=lambda c: -(c["fit"] if c["fit"] is not None else -1))
    return out[:top_n]


async def rank(description: str, plan: dict[str, Any], cards: list[dict[str, Any]], top_n: int | None = None,
               answers: list[tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], str, str]:
    """(results, note, model) of one batch: every offer Claude finds fitting, best first; answers = answer_pairs(...).
    The search's own part (description, criteria, answers) goes with the system prompt, which the backend caches:
    the batches after the first pay for little more than their offers. Raises LLMError."""
    system = SEARCH_RANK_SYSTEM + search_rank_context(description=description, plan=plan, answers=answers)
    prompt = search_rank_list([_rank_line(nr, c) for nr, c in enumerate(cards, 1)])
    out = await get_backend("search").complete(system=system, prompt=prompt, schema=SEARCH_RANK_SCHEMA)
    return (merge_ranking(out.data or {}, cards, top_n or len(cards)),
            str((out.data or {}).get("note") or "").strip()[:300], out.model)


def _by_fit(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(results, key=lambda c: -(c["fit"] if c.get("fit") is not None else -1))


async def _rank_all(search_id: int, description: str, plan: dict[str, Any], cards: list[dict[str, Any]],
                    pairs: list[tuple[str, str]] | None, results: list[dict[str, Any]], passed: list[dict[str, Any]],
                    progress: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                                       list[dict[str, Any]], str, str | None, dict[str, Any]]:
    """Every card in batches of MAX_CANDIDATES, RANK_PARALLEL batches at a time; after each batch the results so far
    (the ones shown before, then the new ones best first across the batches) and the passed-over offers are stored,
    so the page shows them at once. Returns (results, passed, unseen, note, warning, progress): unseen = cards of
    batches Claude could not rank; progress with the ranking calls counted (progress["calls"])."""
    batches = [cards[i:i + MAX_CANDIDATES] for i in range(0, len(cards), MAX_CANDIDATES)]
    state = {"before": list(results), "new": [], "passed": list(passed), "unseen": [], "note": "", "warning": None,
             "done": 0, "progress": dict(progress)}
    gate = asyncio.Semaphore(RANK_PARALLEL)

    async def one(batch: list[dict[str, Any]]) -> None:
        async with gate:
            try:
                got, batch_note, _model = await rank(description, plan, batch, answers=pairs)
            except LLMError as e:
                got, batch_note = None, ""
                state["unseen"] += batch
                state["warning"] = t("Claude could not rank {n} offers ({error}).", n=len(batch), error=e)
        # no await from here on: the batches running at the same time do not interleave their changes
        state["done"] += 1
        pr = state["progress"]
        state["progress"] = {**pr, "rank": {"done": state["done"], "total": len(batches)},
                             "calls": int(pr.get("calls") or 0) + 1}      # ranking calls so far (the page counts them)
        if got is not None:
            fit_keys = {_key(c) for c in got}
            state["passed"] += [{**c, "fit": None, "reason": "", "red_flags": []} for c in batch if _key(c) not in fit_keys]
            state["new"] = _by_fit(state["new"] + got)
            state["note"] = state["note"] or batch_note
        _update(search_id, results=state["before"] + state["new"], passed=state["passed"] or None,
                progress=state["progress"])

    await asyncio.gather(*(one(b) for b in batches))
    return (state["before"] + state["new"], state["passed"], state["unseen"], state["note"], state["warning"],
            state["progress"])


def unranked(cards: list[dict[str, Any]], top_n: int = TOP_N) -> list[dict[str, Any]]:
    """Fallback when Claude fails: cheapest relative to its own search first, no fit score."""
    def key(c: dict[str, Any]) -> float:
        p = (c.get("position") or {}).get("pos")
        return p if p is not None else 2.0
    return [{**c, "fit": None, "reason": "", "red_flags": []} for c in sorted(cards, key=key)[:top_n]]


# ------------------------------------------------------------------ photo check
def _fit(v: Any) -> int | None:
    return max(0, min(100, int(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def apply_photos(results: list[dict[str, Any]], data: dict[str, Any],
                 nrs: set[int] | None = None) -> list[dict[str, Any]]:
    """Claude's photo verdicts merged into the ranked results (nr = 1-based position; unknown, repeated or not sent
    nr ignored), best fit first. The text fit is kept as fit_text; the photo may lower the fit freely but raise it
    by at most PHOTO_RAISE (a guard against inflation). There are no fixed caps for a mismatch or damage: how much
    that matters depends on the person (the prompt weighs it against the description and the answers). Results
    without a verdict keep their fit and get photo=None."""
    out = [{**r, "photo": None} for r in results]
    used: set[int] = set()
    for v in data.get("results") or []:
        if not isinstance(v, dict):
            continue
        nr = v.get("nr")
        if not isinstance(nr, int) or isinstance(nr, bool) or not 1 <= nr <= len(out) or nr in used \
                or (nrs is not None and nr not in nrs):
            continue
        used.add(nr)
        r = out[nr - 1]
        text = r.get("fit")
        photo = v.get("photo") if v.get("photo") in PHOTO_VERDICTS else "unclear"
        condition = v.get("condition") if v.get("condition") in CONDITIONS else "unclear"
        fit = _fit(v.get("fit"))
        if fit is None:
            fit = text
        elif text is not None:
            fit = min(fit, text + PHOTO_RAISE)
        r.update(fit_text=text, fit=fit, photo=photo, condition=condition,
                 photo_note=_one_line(str(v.get("photo_note") or ""))[:200])
    out.sort(key=lambda c: -(c["fit"] if c["fit"] is not None else -1))
    return out


async def check_photos(description: str, plan: dict[str, Any], results: list[dict[str, Any]],
                       answers: list[tuple[str, str]] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """(results, warning): one Claude call with the thumbnails of the ranked results. On failure the results come
    back unchanged with a warning."""
    todo = [(nr, r) for nr, r in enumerate(results, 1) if r.get("image_url")][:MAX_PHOTOS]
    if not todo:
        return results, None
    with tempfile.TemporaryDirectory(prefix="wh-search-") as tmp:
        images: list[Path] = []
        items = []
        for nr, r in todo:
            img = await ad_images.download(r["image_url"], Path(tmp), f"r{nr}", transport=_transport,
                                           platform=r.get("platform"))
            if img is None:
                continue
            images.append(img)
            items.append({"nr": nr, "title": _one_line(r.get("title"))[:120],
                          "price": shipping_info.with_price(
                              r.get("price_display") or (f"{r['price']:g} €" if r.get("price") is not None else None),
                              r.get("shipping_price"), r.get("shipping")),
                          "level": deals.LEVEL_DE.get((r.get("position") or {}).get("level") or ""),
                          "fit": r.get("fit"), "reason": _one_line(r.get("reason"))[:300],
                          # the CLI wrapper copies photos in order as foto1, foto2, ...
                          "photo": f"foto{len(images)}{img.suffix}"})
        if not images:
            return results, t("Claude could not check the photos ({error}); the fit scores are text-only.",
                              error=t("no photo could be downloaded"))
        prompt = search_photo_prompt(description=description, plan=plan, items=items, answers=answers)
        try:
            out = await get_backend("search").complete(system=SEARCH_PHOTO_SYSTEM, prompt=prompt,
                                                       schema=SEARCH_PHOTO_SCHEMA, images=images)
        except LLMError as e:
            return results, t("Claude could not check the photos ({error}); the fit scores are text-only.", error=e)
    return apply_photos(results, out.data or {}, {it["nr"] for it in items}), None


# ------------------------------------------------------------------ stored searches + background task
def _update(search_id: int, **fields: Any) -> None:
    with session() as db:
        row = db.get(ProductSearch, search_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        db.add(row)
        db.commit()


def get(search_id: int) -> ProductSearch | None:
    with session() as db:
        return db.get(ProductSearch, search_id)


def recent(limit: int = RECENT) -> list[ProductSearch]:
    """The latest searches, each text once (its newest run; one running or waiting for answers always shows):
    the same search run again listed every run (10-28)."""
    with session() as db:
        rows = list(db.exec(select(ProductSearch).order_by(col(ProductSearch.started_at).desc(),
                                                           col(ProductSearch.id).desc()).limit(limit * 4)))
    out, seen = [], set()
    for row in rows:
        key = " ".join((row.description or "").lower().split())
        if key in seen and not (is_running(row) or row.status == "asking"):
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def is_running(row: ProductSearch | None) -> bool:
    return bool(row and row.status in RUNNING and row.id in _active)


def running() -> ProductSearch | None:
    for sid in sorted(_active):
        row = get(sid)
        if is_running(row):
            return row
    return None


def _spawn(coro: Coroutine[Any, Any, Any], search_id: int) -> None:
    """Run a search step in the background; a site's used-up hourly budget makes it wait, not stop (waiting_for)."""
    bw = _waits[search_id] = ratelimit.BudgetWait()
    with ratelimit.waiting(bw):         # the task takes the setting along
        task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)

    def done(task: asyncio.Task) -> None:
        _tasks.discard(task)
        if _waits.get(search_id) is bw:
            del _waits[search_id]

    task.add_done_callback(done)


def waiting_for(search_id: int) -> dict[str, Any]:
    """The sites a running search waits for right now (their hourly budget is used up) and when it goes on."""
    bw = _waits.get(search_id)
    return bw.free_at() if bw else {}


def start(description: str, settings: dict[str, Any]) -> int:
    """Store a new search and run it in the background. Raises Busy while another one runs."""
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    desc = (description or "").strip()[:MAX_DESCRIPTION]
    with session() as db:
        row = ProductSearch(description=desc, settings=settings, status="planning", started_at=utcnow(),
                            progress={"done": 0, "total": max_queries(settings)})
        db.add(row)
        db.commit()
        db.refresh(row)
    _active.add(row.id)
    _spawn(run(row.id), row.id)
    return row.id


def rerun(search_id: int) -> None:
    """Run a stored search again with its plan (only the ranking and the photo check go to Claude); without a plan,
    re-plan."""
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    if search_id in _backfill:
        raise Busy(t("The new prices of this search are still being looked up – try again in a moment."))
    row = get(search_id)
    if row is None:
        raise KeyError(search_id)
    replan = not (row.plan and row.plan.get("queries"))
    # a re-run never asks again: the stored answers stay, a search that has none gets {} (= questions skipped)
    _update(search_id, status="planning" if replan else "searching", results=None, queries=None, requests=None,
            candidates=None, note=None, warning=None, error=None, started_at=utcnow(), finished_at=None, cards=None, pool=None,
            passed=None,
            answers=row.answers if row.answers is not None else {},
            progress={"done": 0, "total": len((row.plan or {}).get("queries") or []) or max_queries(row.settings)})
    _active.add(search_id)
    _spawn(run(search_id, replan=replan), search_id)


def continue_run(search_id: int, answers: dict[str, Any] | None) -> bool:
    """Continue a search waiting in "asking" with the person's answers ({} = skipped): ranking and photo check in
    the background, from the stored cards (no willhaben request). False (nothing done) when the search is not
    waiting for answers. Raises KeyError for an unknown search, Busy while another search runs."""
    row = get(search_id)
    if row is None:
        raise KeyError(search_id)
    if row.status != "asking":
        return False
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    _update(search_id, status="ranking", answers=clean_answers(row.plan, answers))
    _active.add(search_id)
    _spawn(_continue(search_id), search_id)
    return True


def recover() -> None:
    """After a restart no search task is running any more (a search in "asking" has none: it stays)."""
    with session() as db:
        for row in db.exec(select(ProductSearch).where(col(ProductSearch.status).in_(RUNNING))):
            if row.status in ("more", "prices"):  # loading more or new prices: the offers shown before stay
                row.status, row.finished_at = "done", row.finished_at or utcnow()
            elif row.status in ("ranking", "photos") and row.results:   # ranked in part: those offers stay
                msg = t("Interrupted by a restart – the offers ranked until then are shown.")
                row.status, row.finished_at, row.cards = "done", utcnow(), None
                row.warning = f"{row.warning} {msg}" if row.warning else msg
            else:
                row.status, row.error, row.finished_at = "error", t("Interrupted by a restart."), utcnow()
            db.add(row)
        db.commit()


@timeline.scoped(search_id="search_id")
async def run(search_id: int, replan: bool = True) -> None:
    """The whole search for one stored row; every step is written back so the page can show progress."""
    _active.add(search_id)
    try:
        row = get(search_id)
        if row is None:
            return
        settings = row.settings or make_settings()
        plan = row.plan
        if replan or not plan:
            _update(search_id, status="planning")
            try:
                plan, _model = await make_plan(row.description, settings)
            except LLMError as e:
                _update(search_id, status="error", error=t("Claude could not plan the search: {error}", error=e),
                        finished_at=utcnow())
                return
            if not plan["queries"]:
                _update(search_id, status="error", plan=plan, finished_at=utcnow(),
                        error=t("Claude proposed no searches – describe the item a bit more concretely."))
                return
        n = len(plan["queries"])
        _update(search_id, status="searching", plan=plan, progress={"done": 0, "total": n})
        bands = {"bands": n_bands} if (n_bands := len(band_runs(plan, settings))) else {}
        found = await run_queries(plan, settings,
                                  on_progress=lambda d, total, sent: _update(
                                      search_id, requests=sent, progress={"done": d, "total": total, **bands}))
        picked = pick_candidates(found["pools"], cap=MAX_RANKED + MAX_POOL)
        if detail_todo(picked):
            # the page loads are no searches: the header counts searches, the table the descriptions per search
            picked, _sent, msg = await load_details(picked, found["per_query"], on_progress=lambda d, total: _update(
                search_id, progress={"done": d, "total": total, "step": "details"}))
            if msg:
                found["warning"] = f"{found['warning']} {msg}" if found["warning"] else msg
        cards = [card(found["query_of"][pi], ad, plan, found["prices"][pi]) for pi, ad in picked]
        if radius := geo.pickup_radius():   # Settings → Shipping & pickup: far pickup-only offers never reach Claude
            await geo.locate(cards)
            cards = [c for c in cards if not geo.too_far(c, radius)]
        cards, reserve = cards[:MAX_RANKED], cards[MAX_RANKED:]      # the reserve is for "load more"
        searched = {"queries": found["per_query"], "requests": found["requests"], "candidates": len(cards),
                    "warning": found["warning"]}
        if cards and plan.get("questions") and row.answers is None and settings.get("ask"):
            # wait for the answers: the cards are kept, nothing more runs until continue_run
            _update(search_id, status="asking", cards=cards, pool=reserve or None, **searched)
            return
        _update(search_id, status="ranking", **searched)
        await _rank_and_finish(search_id, row.description, plan, settings, cards, row.answers, found["warning"],
                               reserve)
    except Exception as e:  # noqa: BLE001 - a background task must leave a readable state behind
        log.exception("product search %s failed", search_id)
        _update(search_id, status="error", error=str(e) or e.__class__.__name__, finished_at=utcnow())
    finally:
        _active.discard(search_id)


async def _continue(search_id: int) -> None:
    """The rest of a search that waited for answers: ranking + photos from the stored cards."""
    _active.add(search_id)
    try:
        row = get(search_id)
        if row is None:
            return
        await _rank_and_finish(search_id, row.description, row.plan or {}, row.settings or make_settings(),
                               list(row.cards or []), row.answers, row.warning, list(row.pool or []))
    except Exception as e:  # noqa: BLE001 - a background task must leave a readable state behind
        log.exception("product search %s failed", search_id)
        _update(search_id, status="error", error=str(e) or e.__class__.__name__, finished_at=utcnow())
    finally:
        _active.discard(search_id)


@timeline.scoped(search_id="search_id")
async def _rank_and_finish(search_id: int, description: str, plan: dict[str, Any], settings: dict[str, Any],
                           cards: list[dict[str, Any]], answers: dict[str, Any] | None, warning: str | None,
                           reserve: list[dict[str, Any]] | None = None) -> None:
    """Ranking (+ photo check) of the cards, then status done; the stored cards are dropped. The offers not ranked stay
    in `pool` for "load more": the cards of a batch Claude could not rank first (they came earlier in the order),
    then the unseen reserve."""
    pairs = answer_pairs(plan, answers, settings)
    note, passed = "", []
    unseen: list[dict[str, Any]] = []     # cards Claude never ranked (its call failed): they keep their turn
    results: list[dict[str, Any]] = []
    if cards:
        _update(search_id, results=None, passed=None)
        row = get(search_id)
        results, passed, unseen, note, msg, _progress = await _rank_all(
            search_id, description, plan, cards, pairs, [], [], dict((row.progress if row else None) or {}))
        if msg:
            warning = f"{warning} {msg}" if warning else msg
        if not results and unseen and len(unseen) == len(cards):     # Claude ranked nothing: the cheapest instead
            results = unranked(cards)
            msg = t("Claude could not rank the offers; showing the cheapest ones instead.")
            warning = f"{warning} {msg}" if warning else msg
        elif results:
            best, rest = results[:TOP_N], results[TOP_N:]
            await fill_shipping(best)           # before the photo check: Claude then knows what shipping costs
            if settings.get("photos"):
                _update(search_id, status="photos")
                best, msg = await check_photos(description, plan, best, answers=pairs)
                if msg:
                    warning = f"{warning} {msg}" if warning else msg
            results = _by_fit(best + rest)
    pool = _pool(unseen, reserve or [], results + passed)
    if results:      # the ranking's outcome stored first: a restart during the lookups loses none of it (recover)
        _update(search_id, status="prices", results=results, passed=passed or None, note=note or None, warning=warning,
                cards=None, pool=pool)
        await geizhals_prices(search_id, results)
    await geo.locate(results)           # coordinates from postcodes, home geocoded once: the list can sort by distance
    _update(search_id, status="done", results=results, passed=passed or None, note=note or None, warning=warning,
            finished_at=utcnow(), cards=None, pool=pool)


def _key(c: dict[str, Any]) -> tuple[str, str]:
    return c.get("platform") or "willhaben", str(c.get("ad_id"))


def _pool(fresh: list[dict[str, Any]], unranked_: list[dict[str, Any]], shown: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """What "load more" can still show: offers Claude has not ranked yet (the unseen reserve, and a batch whose
    ranking failed). Those it saw and passed over are not offered again: ranking them twice only costs time."""
    taken = {_key(c) for c in shown}
    out = [c for c in fresh + unranked_ if _key(c) not in taken]
    return out[:MAX_POOL] or None


def load_more(search_id: int) -> bool:
    """Rank the next offers of a finished search in the background and add the best below the others. False when
    there is nothing more (not done, pool empty). Raises KeyError, Busy while a search runs."""
    row = get(search_id)
    if row is None:
        raise KeyError(search_id)
    if row.status != "done" or not row.pool:
        return False
    if running():
        raise Busy(t("A product search is still running – wait for it to finish."))
    if search_id in _backfill:
        raise Busy(t("The new prices of this search are still being looked up – try again in a moment."))
    _update(search_id, status="more")
    _active.add(search_id)
    _spawn(_more(search_id), search_id)
    return True


@timeline.scoped(search_id="search_id")
async def _more(search_id: int) -> None:
    """One "load more": Claude ranks the next batch of the pool. An offer Claude ranked leaves the pool whether it
    was picked or passed over, so the pool runs out. A problem goes to progress["more_warning"], apart from the
    search's own warning; the next load replaces it (None: cleared)."""
    _active.add(search_id)
    progress: dict[str, Any] = {}
    try:
        row = get(search_id)
        if row is None:
            return
        progress = dict(row.progress or {})
        plan, settings = row.plan or {}, row.settings or make_settings()
        # what an interrupted load stored already (results and passed are written per batch): not ranked twice
        shown = {_key(c) for c in (row.results or []) + (row.passed or [])}
        pool = [c for c in row.pool or [] if _key(c) not in shown]
        batch, rest = pool[:MAX_RANKED], pool[MAX_RANKED:]
        pairs = answer_pairs(plan, row.answers, settings)
        results, passed, unseen, _note, warning, progress = await _rank_all(
            search_id, row.description, plan, batch, pairs, list(row.results or []), list(row.passed or []), progress)
        if unseen and len(unseen) == len(batch):        # Claude ranked none of them: the cheapest instead
            results = results + unranked(batch)
            warning = t("Claude could not rank the offers; showing the cheapest ones instead.")
        await geizhals_prices(search_id, results)
        await geo.locate(results)
        _update(search_id, status="done", results=results, passed=passed or None, pool=_pool(unseen, rest, results + passed),
                candidates=(row.candidates or 0) + len(batch),
                progress={**progress, "rank": None, "more": int(progress.get("more") or 0) + 1, "more_warning": warning})
    except Exception as e:  # noqa: BLE001 - the results shown so far stay; the failure is a warning
        log.exception("loading more offers for product search %s failed", search_id)
        _update(search_id, status="done", progress={**progress, "more_warning": t("Loading more offers failed: {error}",
                                                                                  error=str(e) or type(e).__name__)})
    finally:
        _active.discard(search_id)
