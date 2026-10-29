"""Own search agents: run their searches on the sites, diff against stored hits, notify.

An agent watches one or several sites (app/platforms): either one pasted search link (`spec`), or several searches
(`queries`, each with its platform and spec) run one after another per check: the same search term on several
sites, a pasted link plus its term on other sites, or a product search's keyword searches, with the search plan's
cheap code filters (`plan`) applied to new ads. Every hit keeps the site it came from. Nothing here knows a site's
URLs or JSON; the adapters do.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlmodel import col, select

from app.config import get_settings
from app.db import Hit, ProductSearch, SearchAgent, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.services import auto_buy, product_search, scam, screening
from app import platforms
from app.platforms import Blocked, Listing, PlatformError, SearchSpec, ad_ref
from app.platforms import vpn
from app.platforms import shipping_info
from app.platforms.shipping_info import apply_shipping
from app.services.notify import notify

log = logging.getLogger(__name__)

MAX_PUSH_PER_RUN = 5
NAME_CHARS = 60
SETTING_REFILED = "hits_filtered_refiled"    # refile_screened() ran
# cleared hits keep a bare row (state "cleared", shown nowhere) so the ad never comes back as new while a search still
# lists it; that row goes once no search has seen the ad for GONE_DAYS. KEEP_DAYS_KEY: hits first seen more than that
# many days ago are cleared by themselves, all but saved ones ("0" = never; unset = KEEP_DAYS_DEFAULT)
CLEARED = "cleared"
PENDING = "pending"      # a new hit while the Geizhals rule and the screening judge it: shown only in its own tab
GONE_DAYS = 30
KEEP_DAYS_KEY = "hits_keep_days"
KEEP_DAYS_DEFAULT = 30
OLD_KEEP_DAYS_KEY = "hits_filtered_days"     # the same setting while it cleared filtered hits only: still read
EXPIRING = ("new", "seen", "ignored", "filtered", PENDING)     # the states the retention clears: never "saved"


@dataclass
class RunResult:
    agent_id: int
    rows_found: int = 0
    new: list[Hit] = field(default_factory=list)
    price_drops: list[Hit] = field(default_factory=list)
    bumped: int = 0
    baseline: bool = False
    error: str | None = None
    pending_left: int = 0       # hits still pending after the check (more than one check screens): a follow-up comes
    # the first check's hits (and a new search's) of an agent that judges: checked like new ones, but never pushed;
    # they end in "seen" or "filtered"
    quiet: list[Hit] = field(default_factory=list)


def _interval(platform: platforms.Platform, interval_s: int | None) -> int:
    s = get_settings()
    return max(interval_s or s.default_agent_interval_s, s.min_agent_interval_s, platform.min_interval_s)


def create_agent(source_url: str, name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    """An agent from a pasted result-list link of any supported site. Raises InvalidSearchUrl."""
    p = platforms.for_url(source_url)
    return create_agent_from_spec(p.parse_search_url(source_url), name, interval_s)


def create_agent_from_spec(spec: SearchSpec, name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    p = platforms.get(spec.platform)
    agent = SearchAgent(name=name or p.describe(spec), platform=p.id, spec=spec.to_json(), source_url=spec.url,
                        webapi_url=spec.api or spec.url, interval_s=_interval(p, interval_s))
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def _query_of(spec: SearchSpec) -> dict[str, Any]:
    """One entry of SearchAgent.queries for a search on any site."""
    return {"keyword": spec.keyword or "", "platform": spec.platform, "spec": spec.to_json(), "webapi_url": spec.api,
            "source_url": spec.url, "price_from": spec.price_from, "price_to": spec.price_to}


def _quiet(q: dict[str, Any] | None) -> bool:
    """A search added in the settings drawer that has not had its first check: what it finds then counts as seen."""
    return isinstance(q, dict) and bool(q.get("quiet"))


def _unquiet(q: dict[str, Any]) -> dict[str, Any]:
    """A search without its quiet flag: the same search before and after its first check."""
    return {k: v for k, v in q.items() if k != "quiet"}


def create_agent_on_sites(specs: list[SearchSpec], name: str | None = None, interval_s: int | None = None) -> SearchAgent:
    """One agent for several sites: one search per site, checked together (each site paced by its own limiter).
    The first search is the main one (name, "on <site>" link). One spec makes a plain agent."""
    if not specs:
        raise ValueError("no search")
    if len(specs) == 1:
        return create_agent_from_spec(specs[0], name, interval_s)
    first = specs[0]
    sites = [platforms.get(x.platform) for x in specs]
    agent = SearchAgent(name=name or sites[0].describe(first), platform=first.platform, spec=first.to_json(),
                        source_url=first.url, webapi_url=first.api or first.url,
                        interval_s=max(_interval(x, interval_s) for x in sites), queries=[_query_of(x) for x in specs])
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def agent_platforms(agent: SearchAgent) -> list[str]:
    """The sites an agent watches, in the order of its searches (one for a plain agent)."""
    ids = [str(q.get("platform") or agent.platform or "willhaben") for q in agent.queries or [] if isinstance(q, dict)]
    return list(dict.fromkeys(ids)) or [agent.platform or "willhaben"]


def agent_spec(agent: SearchAgent) -> SearchSpec:
    """The agent's search. Rows from before `spec` existed are willhaben searches with their stored URLs: the search
    term and prices are read back from the link, the stored URLs stay what is requested."""
    if agent.spec:
        return SearchSpec.from_json(agent.spec)
    pid = agent.platform or "willhaben"
    try:
        parsed = platforms.get(pid).parse_search_url(agent.source_url)
    except (platforms.PlatformError, platforms.InvalidSearchUrl, ValueError):
        parsed = SearchSpec(platform=pid, url=agent.source_url)
    return replace(parsed, platform=pid, url=agent.source_url, api=agent.webapi_url)


def agent_searches(agent: SearchAgent) -> list[dict[str, Any]]:
    """The agent's searches as `queries` entries: its list, or its one search (a plain agent)."""
    if agent.queries:
        return [dict(q) for q in agent.queries if isinstance(q, dict)]
    return [_query_of(agent_spec(agent))]


def set_searches(agent: SearchAgent, searches: list[dict[str, Any]]) -> None:
    """Store an agent's searches; the first is the main one (its site and the "on <site>" link). A plain agent keeps
    a single search without `queries`; an agent from a product search keeps its list."""
    first = searches[0]
    agent.platform, agent.spec = first.get("platform") or agent.platform, first.get("spec")
    agent.source_url, agent.webapi_url = first["source_url"], first.get("webapi_url") or first["source_url"]
    agent.queries = list(searches) if len(searches) > 1 or agent.search_id else None   # new list: JSON sees no edits


def keyword_sites(agent: SearchAgent | None = None) -> list[platforms.Platform]:
    """Sites a search term can be sent to (switched on, keyword search), plus those the agent watches already."""
    mine = agent_platforms(agent) if agent else []
    out = []
    for p in platforms.all(enabled_only=False):
        if p.can("keyword_search") and (platforms.enabled(p.id) or p.id in mine):
            out.append(p)
    return out


def _keyword_site(pid: str) -> platforms.Platform:
    try:
        p = platforms.get(pid)
    except platforms.UnknownPlatform as e:
        raise ValueError(t("Unknown platform: {id}", id=pid)) from e
    if not platforms.enabled(p.id):
        raise ValueError(t("{name} is switched off in Settings → Platforms", name=p.name))
    if not p.can("keyword_search"):
        raise ValueError(t("{name} has no keyword search.", name=p.name))
    return p


def _fillable(pid: str) -> bool:
    """A site that takes every search term of its agent: known, switched on, with a keyword search."""
    try:
        return platforms.enabled(pid) and platforms.get(pid).can("keyword_search")
    except platforms.UnknownPlatform:
        return False


def _term(q: dict[str, Any]) -> tuple[str, Any, Any] | None:
    """A search's term: its keyword (spaces normalised) with its price range, the same on every site. None for a
    search without a term (a pasted filter link), which belongs to its one site."""
    kw = " ".join(str(q.get("keyword") or "").split())
    return (kw, q.get("price_from"), q.get("price_to")) if kw else None


def term_key(term: tuple[str, Any, Any]) -> str:
    """A term as the settings drawer sends it back to remove it (drop_term): JSON [keyword, price_from, price_to]."""
    return json.dumps(list(term), ensure_ascii=False)


def parse_term_key(raw: str) -> tuple[str, float | None, float | None] | None:
    """term_key() read back; None for anything else (a stale or hand-made value is ignored, not an error)."""
    try:
        kw, pf, pt = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(kw, str) or any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)))
                                      for v in (pf, pt)):
        return None
    kw = " ".join(kw.split())
    return (kw, pf, pt) if kw else None


def edit_searches(agent: SearchAgent, drop: Iterable[int] = (), sites: list[str] | None = None, add: str = "",
                  price_from: float | None = None, price_to: float | None = None, *,
                  drop_terms: Iterable[tuple[str, Any, Any]] = (),
                  add_terms: Iterable[tuple[str, float | None, float | None]] = ()) -> bool:
    """Change an agent's searches in one go. A search term (keyword + price range) runs on every site the agent
    watches: `drop_terms` removes terms on every site; `add` (with `price_from`/`price_to`) and `add_terms` add terms;
    every chosen site that is switched on ends up with every term (also rows from before this rule that lack one),
    a watched site that is switched off or gone keeps what it has. `drop` removes single searches by index into
    agent_searches (the drawer sends it for searches without a term: a pasted filter link, one site each). `sites`:
    the sites to watch (None = as now); a site no longer chosen loses its searches, a watched one the drawer has no
    chip for (keyword_sites: gone) keeps them until its last one is dropped. Raises ValueError and changes nothing on
    a mistake. True when searches were added: they start quietly ("quiet" in their entry, see _run_agent), what their
    first check finds counts as seen; a plain agent whose only search is new has no entry for it, so its whole next
    check is quiet."""

    def site(q: dict[str, Any]) -> str:
        return str(q.get("platform") or agent.platform or "willhaben")

    every = agent_searches(agent)
    drop, gone = set(drop), {x for x in drop_terms if x}
    kept = [q for i, q in enumerate(every) if i not in drop and _term(q) not in gone]
    offered = {p.id for p in keyword_sites(agent)}      # the drawer's chips
    if sites is None:      # as now: the sites that keep a search; all of them when none does (a new term replaces it)
        wanted = list(dict.fromkeys(site(q) for q in kept or every))
    else:                  # the ticked chips, and the watched sites without a chip that keep a search (no box unticks)
        ticked = [s.strip().lower() for s in sites if s.strip()]
        if not ticked and any(site(q) in offered for q in every):
            raise ValueError(t("Choose at least one site."))     # a site without a chip cannot be the only one left
        wanted = list(dict.fromkeys(ticked + [site(q) for q in kept if site(q) not in offered]))
    if not wanted:
        raise ValueError(t("Choose at least one site."))
    new = [(" ".join((kw or "").split()), pf, pt) for kw, pf, pt in [(add, price_from, price_to), *add_terms]]
    terms = list(dict.fromkeys([x for x in map(_term, kept) if x] + [x for x in new if x[0]]))
    searches = [q for q in kept if site(q) in wanted]
    have = {(site(q), _term(q)) for q in searches}
    watched = {site(q) for q in every}
    added = False
    for pid in wanted:
        if not _fillable(pid):
            if pid not in watched:
                _keyword_site(pid)      # a newly chosen site must take the terms: raises why it cannot
            continue                    # a watched site switched off (or gone) keeps what it has
        p = platforms.get(pid)
        for term in terms:
            if (pid, term) in have:
                continue
            q = _query_of(p.keyword_search(*term))
            if (pid, _term(q)) not in have:     # the site may word the term the way a search it has does
                searches.append({**q, "quiet": True})
                added = True
            have |= {(pid, term), (pid, _term(q))}
    if not searches:     # checked after adding: the only term can be swapped in one Save
        raise ValueError(t("Keep at least one search, or delete the agent."))
    for pid in wanted:     # nothing to search on a chip's site: say why (one without a chip just goes with its last)
        if pid in offered and not any(site(q) == pid for q in searches):
            raise ValueError(t("The searches have no search term to use on {name}.", name=_keyword_site(pid).name))
    set_searches(agent, searches)
    if agent.queries is None and searches[0].get("quiet"):
        agent.baseline_done = False     # a plain agent's only search is new: no entry keeps its flag
    return added


def _site_name(pid: str) -> str:
    try:
        return platforms.get(pid).name
    except platforms.UnknownPlatform:
        return pid


def agent_search_items(agent: SearchAgent) -> dict[str, Any]:
    """An agent's searches as its settings drawer shows them.
    terms: each search term once (keyword + price range; it runs on every site the agent watches): `k` (its key in the
      page's Alpine component), `key` (sent back as drop_term), the sites it runs on, a link to its search on the
      first of them, the product search's `why`, and `missing`/`missing_names`: ticked sites that are switched on and
      lack it (rows from before this rule), which the next Save fills in.
    filters: the searches without a term (a pasted filter link, one site each): `index` into agent_searches (sent back
      as drop_query), `label` in words from the site's describe() ("Search link" when it has nothing better), its
      site, prices and link.
    per_check: searches per check after Save as shown; choices: the site chips (keyword_sites); js: for agentSearches."""
    searches = agent_searches(agent)
    watched = agent_platforms(agent)
    sites = keyword_sites(agent)
    choices = [p.id for p in sites]
    ticked = watched        # a watched site without a chip (gone) stays too: Save keeps its searches
    fixed = [pid for pid in watched if pid not in choices]     # those without a chip: nothing can untick them
    fill = [pid for pid in dict.fromkeys(watched + choices) if _fillable(pid)]
    terms: dict[tuple[str, Any, Any], dict[str, Any]] = {}
    filters: list[dict[str, Any]] = []
    for i, q in enumerate(searches):
        pid = str(q.get("platform") or agent.platform or "willhaben")
        term = _term(q)
        if term:
            row = terms.setdefault(term, {"k": f"t{len(terms)}", "key": term_key(term), "keyword": term[0],
                                          "price_from": term[1], "price_to": term[2], "sites": [],
                                          "url": q.get("source_url") or "", "why": ""})
            if pid not in row["sites"]:
                row["sites"].append(pid)
            row["why"] = row["why"] or str(q.get("why") or "")
            continue
        try:
            label = platforms.get(pid).describe(_query_spec(q, pid))
        except Exception:  # noqa: BLE001 - a site that no longer exists, or a stored search it cannot read
            label = ""
        if not label or label == t("search"):
            label = t("Search link")
        filters.append({"k": f"f{i}", "index": i, "platform": pid, "label": label, "url": q.get("source_url") or "",
                        "price_from": q.get("price_from"), "price_to": q.get("price_to")})
    rows = list(terms.values())
    for row in rows:
        row["missing"] = [pid for pid in ticked if pid in fill and pid not in row["sites"]]
        row["missing_names"] = [_site_name(pid) for pid in row["missing"]]
    per_check = sum((len(rows) if pid in fill else sum(pid in r["sites"] for r in rows))
                    + sum(f["platform"] == pid for f in filters) for pid in ticked)
    return {"terms": rows, "filters": filters, "per_check": per_check, "choices": sites,
            "js": {"terms": {r["k"]: {"t": [r["keyword"], r["price_from"], r["price_to"]], "sites": r["sites"],
                                      "missing": r["missing"]} for r in rows},
                   "filters": {f["k"]: f["platform"] for f in filters}, "sites": ticked, "fixed": fixed, "fill": fill}}


def agent_interval(agent: SearchAgent, interval_s: int) -> int:
    """`interval_s` raised to the floors: the settings' minimum and the slowest site the agent watches."""
    floors = [get_settings().min_agent_interval_s]
    for pid in agent_platforms(agent):
        try:
            floors.append(platforms.get(pid).min_interval_s)
        except platforms.UnknownPlatform:
            continue
    return max(interval_s, *floors)


def _newest_first(spec: SearchSpec) -> SearchSpec:
    """Agents diff on newness: an eBay search saved from a sorted link (before parse_search_url dropped its _sop)
    still asks for the newest ads first."""
    return replace(spec, sort="newest") if spec.platform == "ebay" and spec.sort not in (None, "newest") else spec


def _query_spec(q: dict[str, Any], platform: str = "willhaben") -> SearchSpec:
    if q.get("spec"):
        return SearchSpec.from_json(q["spec"])
    return SearchSpec(platform=q.get("platform") or platform, url=q.get("source_url") or q.get("webapi_url") or "",
                      api=q.get("webapi_url"), keyword=q.get("keyword"), price_from=q.get("price_from"))


def agent_queries(plan: dict[str, Any] | None, platform_id: str = "willhaben") -> list[dict[str, Any]]:
    """The agent's searches for a product-search plan on one platform: newest first (agents diff on newness), no
    price_to in the search (drop_reason applies it, like the product search does)."""
    p = platforms.get(platform_id)
    out = []
    for q in (plan or {}).get("queries") or []:
        if not isinstance(q, dict) or not q.get("keyword"):
            continue
        category = q.get("category_path") if p.id == "willhaben" else None
        spec = p.keyword_search(q["keyword"], q.get("price_from"), None, category=category, sort="newest")
        out.append({"keyword": q["keyword"], "platform": p.id, "spec": spec.to_json(), "webapi_url": spec.api,
                    "source_url": spec.url, "price_from": q.get("price_from"), "price_to": q.get("price_to"),
                    "category_path": q.get("category_path"), "why": q.get("why") or ""})
    return out


def default_interval_s(n_queries: int) -> int:
    """An agent sends one request per search and check, so it checks n times less often by default."""
    s = get_settings()
    return max(s.default_agent_interval_s * max(n_queries, 1), s.min_agent_interval_s)


def agent_name(row: ProductSearch) -> str:
    text = ((row.plan or {}).get("summary") or row.description or "").strip() or t("search")
    text = " ".join(text.split())
    if len(text) <= NAME_CHARS:
        return text
    cut = text[:NAME_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:–-")
    return (cut or text[:NAME_CHARS]) + "…"


def screen_brief(row: ProductSearch) -> str:
    """Screening brief from the search (goes into Claude's prompt, so German is fine; editable later)."""
    plan = row.plan or {}
    lines = [(row.description or "").strip()]
    if plan.get("max_price"):
        lines.append(f"Budget: bis {plan['max_price']:g} €")
    for label, key in (("Muss", "must_have"), ("Wünsche", "nice_to_have"), ("Ausschluss", "exclude")):
        if plan.get(key):
            lines.append(f"{label}: {', '.join(plan[key])}")
    lines.append(product_search.preferences_text(plan, row.answers))   # the person's answers to the questions
    return "\n".join(x for x in lines if x)


def agent_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    """What product_search.drop_reason needs at check time."""
    plan = plan or {}
    return {"summary": plan.get("summary") or "", "max_price": plan.get("max_price"),
            "must_have": list(plan.get("must_have") or []), "nice_to_have": list(plan.get("nice_to_have") or []),
            "exclude": list(plan.get("exclude") or [])}


def create_agent_from_search(search_id: int, name: str | None = None, interval_s: int | None = None,
                             screen: bool = True, platform: str = "willhaben", sites: list[str] | None = None) -> SearchAgent:
    """A search agent that keeps running a finished product search's keyword searches, on one platform or on
    several (`sites`: one agent, every planned search on each site). Raises KeyError for an unknown search,
    ValueError when it has no planned searches or no chosen site can search by keyword. Does not run the baseline
    (the caller schedules)."""
    with session() as db:
        row = db.get(ProductSearch, search_id)
    if row is None:
        raise KeyError(search_id)
    chosen = [platforms.get(pid) for pid in dict.fromkeys(sites or [platform])]
    usable = [x for x in chosen if x.can("keyword_search")]
    if not usable:
        raise ValueError(t("{name} has no keyword search.", name=chosen[0].name))
    per_site = [agent_queries(row.plan, x.id) for x in usable]
    queries = [q for qs in per_site for q in qs]
    if not queries:
        raise ValueError(t("This search has no planned searches yet."))
    # each site has its own limiter: the interval follows the searches per site, not their sum
    interval = max(_interval(x, interval_s or default_interval_s(max(len(qs) for qs in per_site))) for x in usable)
    p = usable[0]
    first = queries[0]
    agent = SearchAgent(name=(name or "").strip() or agent_name(row), platform=p.id, spec=first["spec"],
                        source_url=first["source_url"], webapi_url=first["webapi_url"] or first["source_url"],
                        interval_s=interval, queries=queries, plan=agent_plan(row.plan), search_id=row.id)
    if screen:
        agent.screen, agent.screen_brief, agent.screen_min = True, screen_brief(row) or None, "match"
    st = row.settings or {}              # the search's Geizhals rule goes along
    agent.geizhals_level = st.get("gh_level") if st.get("gh_level") in LEVELS else None
    agent.geizhals_pct = None if agent.geizhals_level else st.get("gh_pct")
    with session() as db:
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return agent


def _wh_rows_key(agent_id: int) -> str:
    """Set once the agent's first check with the sites' larger pages (Platform.former_rows) is done."""
    return f"wh_rows_agent_{agent_id}"


def _old_rows(spec: SearchSpec) -> int | None:
    return _site(spec.platform).former_rows(spec)


async def _search_all(specs: list[tuple[SearchSpec, dict[str, Any] | None]], widen: bool = False,
                      older: set[tuple[str, str]] | None = None) -> tuple[
        list[tuple[Listing, dict[str, Any] | None]], int, list[str], Exception | None, list[dict[str, Any] | None],
        list[str]]:
    """Run an agent's searches one after another (uncached), each on its platform. Returns (ads with their query,
    de-duplicated by ad id, first occurrence wins; summed rows_found; error lines; first exception; the queries of
    the searches that succeeded; the searches the VPN held: a site waiting for the search to find it a server, no
    error of the search). A block stops the remaining searches of this run. `widen`: the agent's first check with
    willhaben's largest page; the ads beyond the rows it asked for before go into `older` (they are not new, the
    smaller page just never showed them)."""
    ads: list[tuple[Listing, dict[str, Any] | None]] = []
    seen: set[tuple[str, str]] = set()
    rows, errors, first_exc, ok, held = 0, [], None, [], []
    failed: list[tuple[int, str]] = []
    skipped = 0
    n = len(specs)
    for i, (spec, q) in enumerate(specs, 1):
        try:
            page = await platforms.get(spec.platform).search(spec)
        except vpn.VpnWait as e:
            log.info("agent search %s/%s (%s) waits for the VPN: %s", i, n, spec.keyword, e)
            held.append(str(e))
            continue
        except PlatformError as e:
            log.warning("agent search %s/%s (%s) failed: %s", i, n, spec.keyword, e)
            failed.append((i, str(e)))
            first_exc = first_exc or e
            if isinstance(e, Blocked):
                if i < n:
                    skipped = n - i
                break
            continue
        ok.append(q)
        rows += page.rows_found or 0
        if widen and older is not None and (old := _old_rows(spec)) is not None:
            older.update((spec.platform, ad.ad_id) for ad in page.ads[old:] if ad.ad_id)
        for ad in page.ads:
            ad.platform = spec.platform               # the hit belongs to the site that was searched
            if not ad.ad_id or (spec.platform, ad.ad_id) in seen:
                continue
            seen.add((spec.platform, ad.ad_id))
            ads.append((ad, q))
    errors = failure_lines(failed, n)
    if skipped:
        errors.append(t("remaining searches skipped: {k}", k=skipped))
    return ads, rows, errors, first_exc, ok, held


def failure_lines(failed: list[tuple[int, str]], n: int) -> list[str]:
    """The failed searches as lines, one per kind of error: "3 of 12 searches failed: eBay …" rather than the same
    sentence for each search (10-28: an agent card listed eBay's answer three times)."""
    if n <= 1:
        return [err for _, err in failed]
    by_error: dict[str, list[int]] = {}
    for i, err in failed:
        by_error.setdefault(err, []).append(i)
    return [t("search {i}/{n} failed: {error}", i=idx[0], n=n, error=err) if len(idx) == 1
            else t("{k} of {n} searches failed: {error}", k=len(idx), n=n, error=err) for err, idx in by_error.items()]


def _apply(hit: Hit, ad: Listing) -> None:
    hit.heading = ad.heading
    hit.body = ad.body
    hit.price = ad.price
    hit.price_display = ad.price_display
    hit.old_price = ad.old_price
    hit.location = ad.location
    hit.postcode = ad.postcode
    hit.published_at = ad.published_at or hit.published_at   # Vinted's list has no date: the item page's stays
    hit.end_date = ad.end_date
    hit.url = ad.url
    hit.image_url = ad.image_url
    hit.is_private = ad.is_private
    if ad.item_condition:           # a list without one keeps what the ad page told (Shpock states it only there)
        hit.item_condition, hit.item_condition_label = ad.item_condition, ad.item_condition_label
    apply_shipping(hit, ad)         # the same: a list without shipping keeps what the ad page told
    if ad.lat is not None and ad.lon is not None:
        hit.lat, hit.lon = ad.lat, ad.lon
    _list_status(hit, ad)
    hit.last_seen_at = utcnow()


# sites whose search list states an ad's real status (willhaben's advertStatus, Kleinanzeigen's ad-status, Facebook's
# is_pending); the others' lists say "active" for every ad, so only their ad page tells
LIST_STATUS = {"willhaben", "kleinanzeigen", "facebook"}


def _list_status(hit: Hit, ad: Listing) -> None:
    """A reserved (or sold) ad in a search list is marked on the hit at once; one listed as active again clears a
    reservation the hit had. Sites whose lists do not state it keep what their ad page said."""
    raw = (getattr(ad, "status", None) or "").lower()
    status = STATUS_WORDS.get(raw)
    if status in ("reserved", "sold"):
        hit.site_status, hit.status_checked_at = status, utcnow()
    elif raw == "active" and hit.site_status == "reserved" and (hit.platform or "willhaben") in LIST_STATUS:
        hit.site_status, hit.status_checked_at = "active", utcnow()


_checking: dict[int, asyncio.Future] = {}     # agent id -> resolved when its current check ends


def is_checking(agent_id: int) -> bool:
    """A check of this agent is running right now (the scheduler then skips its turn)."""
    return agent_id in _checking


async def run_agent(agent_id: int, job: Any = None) -> RunResult:
    """One check of a search agent, as one timeline run (its searches, screening and pushes are the steps). Never two
    checks of one agent at once: they send every search twice (eBay answered the second with 403, 10-13). A check
    asked for while one runs ("Run now" during a scheduled one) waits for it, then runs with the current settings.
    `job`: the background job this check runs as (enqueue_run), so it is not shown a second time."""
    from app.services import timeline

    while (running := _checking.get(agent_id)) is not None:
        await asyncio.shield(running)
    done = asyncio.get_running_loop().create_future()
    _checking[agent_id] = done
    try:
        with session() as db:
            agent = db.get(SearchAgent, agent_id)
            title = agent.name if agent else t("Agent {id}", id=agent_id)
        from app.services import jobs

        # in the header's background work while it runs (no notice when done: the hits and pushes tell); a check
        # queued by hand is a job of its own already (enqueue_run sets its result)
        shown = contextlib.nullcontext(job) if job is not None else jobs.track(
            "agent-run", t("Search agent „{name}“", name=title), lane="agents", link=f"/hits?agent_id={agent_id}",
            quiet=True, current=f"agent:{agent_id}")
        async with shown as tracked, timeline.run("agent_run", title, name=f"agent-{agent_id}", agent_id=agent_id) as tl:
            res = await _run_agent(agent_id)
            tl.finish(not res.error, res.error, payload={k: v for k, v in {
                "rows_found": res.rows_found, "new": len(res.new), "price_drops": len(res.price_drops),
                "bumped": res.bumped, "baseline": res.baseline or None}.items() if v is not None})
            if job is None:
                tracked.result = res.error or _run_summary(res)
                if res.error:
                    tracked.status = "failed"
    finally:
        del _checking[agent_id]
        done.set_result(None)
    return res


def enqueue_run(agent_id: int) -> bool:
    """"Check now": one check of the agent as a background job (lane agents; cancellable while it waits). The card
    carries the marker "agent:<id>" meanwhile and refreshes itself when the check is done. False: a check of this
    agent is queued or running already (a scheduled one shows the same marker), or no such agent."""
    from app.services import jobs

    key = f"agent:{agent_id}"
    if key in jobs.current_keys("agent:"):
        return False
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None:
        return False

    async def run(job) -> str:
        res = await run_agent(agent_id, job=job)
        if res.error:
            raise RuntimeError(res.error)
        return _run_summary(res)

    jobs.submit("agent-run", t("Check „{name}“ now", name=agent.name), run, lane="agents", current=key,
                on_cancel=lambda: None, link=f"/hits?agent_id={agent_id}")
    return True


def _run_summary(res: RunResult) -> str:
    parts = [t("{n} new", n=len(res.new))] if res.new else []
    if res.price_drops:
        parts.append(t("{n} price drops", n=len(res.price_drops)))
    return " · ".join(parts) or (t("first check: everything counts as seen") if res.baseline else t("nothing new"))


async def _run_agent(agent_id: int) -> RunResult:
    res = RunResult(agent_id=agent_id)
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        if agent is None:
            res.error = t("Agent not found")
            return res
        name, queries, plan = agent.name, agent.queries, agent.plan
        specs = [(_newest_first(_query_spec(q, agent.platform)), q) for q in queries] if queries else [
            (_newest_first(agent_spec(agent)), None)]
        # as the check starts: a Save while it runs changes the next check, not this one
        baseline, before = not agent.baseline_done, [_unquiet(q) for q in agent_searches(agent)]

    def _fail(error: str) -> RunResult:
        with session() as db:
            row = db.get(SearchAgent, agent_id)
            row.last_run_at, row.last_error = utcnow(), error
            db.add(row)
            db.commit()
        res.error = error
        return res

    usable, skipped = [], []                 # searches on unknown or switched-off sites are skipped (and said so)
    for spec, q in specs:
        try:
            site = platforms.get(spec.platform)
        except platforms.UnknownPlatform:
            skipped.append(t("Unknown platform: {id}", id=spec.platform))
            continue
        if not platforms.enabled(site.id):
            skipped.append(t("{name} is switched off in Settings → Platforms", name=site.name))
            continue
        usable.append((spec, q))
    skipped = list(dict.fromkeys(skipped))
    if not usable:
        return _fail("; ".join(skipped))
    # quiet searches last: an ad an older search finds too counts as that one's (first occurrence wins) and pushes
    usable.sort(key=lambda x: _quiet(x[1]))
    widen = get_setting(_wh_rows_key(agent_id)) != "1"
    older: set[tuple[str, str]] = set()
    found, rows_found, errors, first_exc, ok, held = await _search_all(usable, widen, older)
    if not ok and not errors:                # the VPN held every search: no error, the next check runs as planned
        log.info("agent %s: every search waits for the VPN (%s)", agent_id, held[0])
        return res
    if not ok:
        return _fail(str(first_exc))         # every search failed (one search: its error)
    partial_error = "; ".join(skipped + errors) or None

    res.rows_found = rows_found
    res.baseline = baseline
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
        existing = {((h.platform or "willhaben"), h.ad_id): h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id))}
        id_site = {ad_id: pid for pid, ad_id in existing}     # Hit is unique per (agent, ad id), whatever the site
        scam_ctx = _scam_context(existing.values(), [ad for ad, _ in found])
        known = _known_conditions(db, [ad for ad, _ in found if ad.ad_id and not ad.item_condition
                                       and (ad.platform, ad.ad_id) not in existing])
        for ad, query in found:
            if not ad.ad_id:
                continue
            hit = existing.get((ad.platform, ad.ad_id))
            if hit is None and id_site.get(ad.ad_id, ad.platform) != ad.platform:
                log.warning("agent %s: %s ad %s has the id of a %s hit, skipped", agent_id, ad.platform, ad.ad_id,
                            id_site[ad.ad_id])
                continue
            if hit is None:
                if plan and product_search.drop_reason(ad, plan, query):
                    continue        # "Suche" ad, far over budget or an exclusion word: never stored or notified
                # the agent's or this search's first check: seen, no push; so are the older ads the larger page shows
                quiet = res.baseline or _quiet(query) or (ad.platform, ad.ad_id) in older
                # with a screening or a Geizhals rule a new hit is pending until it passed both (Geizhals, Claude:
                # seconds to minutes): New shows only hits that passed; _file_screened files it
                # a quiet hit of an agent that judges (screening, Geizhals rule, distance) is checked too, without a
                # push: it ends in "seen" or "filtered", not unjudged in "seen"
                judge_quiet = quiet and _judges(agent)
                hit = Hit(agent_id=agent_id, platform=ad.platform, ad_id=ad.ad_id, heading=ad.heading, url=ad.url,
                          state=PENDING if judge_quiet else "seen" if quiet else PENDING if _holds(agent) else "new",
                          quiet=True if judge_quiet else None)
                _apply(hit, ad)
                if negotiable_only(hit):     # Kleinanzeigen "VB": never shown or pushed (the hits page leaves it out)
                    hit.state, hit.quiet = "filtered", None
                if not hit.item_condition and (cond := known.get((ad.platform, ad.ad_id))):
                    hit.item_condition, hit.item_condition_label = cond      # what another agent's hit learned
                hit.scam_flags = scam.flags(hit, scam_ctx, home_country=_site(ad.platform).home_country) or None   # before screening
                _mark_scam_issue(hit)
                db.add(hit)
                existing[(ad.platform, ad.ad_id)] = hit
                id_site[ad.ad_id] = ad.platform
                if hit.state == "filtered":
                    pass
                elif judge_quiet:
                    res.quiet.append(hit)
                elif not quiet:
                    res.new.append(hit)
                continue
            prev_price, prev_published = hit.price, hit.published_at
            _apply(hit, ad)
            if prev_price is not None and ad.price is not None and _site(ad.platform).price_dropped(prev_price, ad.price):
                hit.price_dropped_at = utcnow()
                hit.old_price = prev_price
                hit.scam_flags = scam.flags(hit, scam_ctx, home_country=_site(hit.platform).home_country) or None   # a new red flag?
                _mark_scam_issue(hit)
                if hit.state not in ("ignored", CLEARED):
                    res.price_drops.append(hit)
            if ad.is_bumped is True and prev_published and ad.published_at and ad.published_at > prev_published:
                hit.bumped_count += 1
                res.bumped += 1
            db.add(hit)
        agent.last_run_at = agent.last_ok_at = utcnow()
        agent.last_error = partial_error
        agent.last_rows_found = rows_found
        # on the row as it is now, so a Save during this check stays. The agent's first check is done, unless a Save
        # meanwhile swapped a plain agent's only search: that one has not run (one added to a list is quiet itself)
        if baseline and all(_quiet(q) or _unquiet(q) in before for q in agent_searches(agent)):
            agent.baseline_done = True
        ran = [_unquiet(q) for q in ok if _quiet(q)]     # quiet searches that ran: their first check is done
        if ran and agent.queries:
            agent.queries = [_unquiet(q) if _quiet(q) and _unquiet(q) in ran else q for q in agent.queries]   # new list
        elif ran and not agent.baseline_done and agent_searches(agent)[0] in ran:
            agent.baseline_done = True      # a Save meanwhile left a plain agent with one of them (the agent's flag)
        notify_new, notify_drops = agent.notify, agent.notify and agent.notify_price_drops
        db.add(agent)
        db.commit()
        for h in res.new + res.price_drops + res.quiet:
            db.refresh(h)
        db.refresh(agent)
        fresh = {h.id for h in res.new}
        # pending hits an earlier check did not finish (Claude got only MAX_PER_RUN, a crash, a restart): judged now,
        # before this check's own; with the rule and the screening switched off meanwhile they are simply new
        drops = {h.id for h in res.price_drops}
        fresh |= {h.id for h in res.quiet}
        backlog = [h for h in db.exec(select(Hit).where(Hit.agent_id == agent_id, Hit.state == PENDING))
                   if h.id not in fresh and h.id not in drops]
        quiet_backlog = [h for h in backlog if h.quiet]
        backlog = [h for h in backlog if not h.quiet]
        if backlog and not _holds(agent):
            for h in backlog:
                h.state = "new"
                db.add(h)
            db.commit()
            backlog = []
        if quiet_backlog and not _judges(agent):
            for h in quiet_backlog:
                h.state = "seen"
                db.add(h)
            db.commit()
            quiet_backlog = []
        res.new = backlog + res.new
        res.quiet = quiet_backlog + res.quiet

    if widen and any(_old_rows(s) is not None and q in ok for s, q in usable):
        set_setting(_wh_rows_key(agent_id), "1")                # its searches ran with the larger page
    to_notify = (res.new if notify_new else []) + (res.price_drops if notify_drops else [])
    to_notify = to_notify + [h for h in res.quiet if h not in to_notify]    # judged like them, never pushed
    if pickup_limit(agent) and to_notify:
        await _locate(to_notify)
        from app.services import geo

        # a list that says nothing about shipping (Kleinanzeigen) is no "pickup only": the ad page tells, before
        # the distance holds a far hit back
        await _shipping_pages([h for h in to_notify if h.shipping is None and geo.too_far(h, pickup_limit(agent))])
    gh_check = bool(agent.screen and agent.geizhals_check)
    if (geizhals_rule(agent) or agent.screen) and to_notify:   # the screening rates against the new price too
        await _geizhals_prices(to_notify)
    if agent.geizhals_level in LEVELS and to_notify:          # no new price: their similar ads rate them
        await _similar_prices([h for h in to_notify if _geizhals_cents(h) is None and distance_passes(agent, h)])
    # held back anyway: no Claude call – unless Claude is to check the product the rule judged by (the screening's call
    # does that too: geizhals_check.SCREEN_RULES)
    from app.services import geizhals_check

    to_screen = [h for h in to_notify if distance_passes(agent, h)
                 and (geizhals_passes(agent, h) or gh_check and geizhals_check.unchecked(h.geizhals))]
    # first those held back before (filtered or pending, no verdict yet), which get into New only judged. A check
    # sends at most screening.MAX_PER_RUN; a hit beyond that stays unjudged (pending) and gets no push (10-14 they
    # were pushed unjudged: 30 off-topic Vinted ads in a day)
    to_screen.sort(key=lambda h: (bool(h.quiet), not (h.state in ("filtered", PENDING) and h.screen_relevant is None
                                                       and h.id not in fresh)))   # quiet ones last
    asked: set[int] = set()
    if agent.screen and to_screen:      # Claude: right item? good deal? right Geizhals product?
        asked = await screening.screen_all(agent_id, to_screen)
    if gh_check and not asked:          # no screening call: the products alone (the held back, older unchecked hits)
        await geizhals_check.check(agent, to_notify)
    if _holds(agent) and to_notify:
        _file_screened(agent, res.new if notify_new else [], res.price_drops, asked)
    if res.quiet:
        _file_quiet(agent, res.quiet, asked)
    if agent.screen and (_holds(agent) or res.quiet):
        res.pending_left = sum(1 for h in res.new + res.quiet if h.state == PENDING)
        if res.pending_left:            # judged in a follow-up check soon, not only at the next regular one
            from app import scheduler

            scheduler.follow_up(agent_id)

    # the site in the title once agents watch more than one; always in the payload (HA automations, app links)
    multi = platforms.multi()

    def label(pid: str) -> str:
        return f"{_site(pid).name} · {name}" if multi else name

    if notify_new and res.new:
        good = [h.id for h in res.new if passes(agent, h) and not _unjudged(agent, h, asked)]
        pushed = set(good[:MAX_PUSH_PER_RUN])
        await _shipping_pages([h for h in res.new if h.id in pushed])   # the push names the shipping
        for h in res.new:
            if h.id in pushed or h.id not in good:   # rejected hits: HA event only, no push
                await notify("hit", _scam_prefix(h) + f"{label(h.platform)}: {h.price_display or ''}{_ship_suffix(h)}".strip(" :"),
                             _hit_text(h), url=h.url, image=h.image_url, push=h.id in pushed, tag=hit_tag(h),
                             data={"agent": name, "platform": h.platform, "ad_id": h.ad_id, "ref": ad_ref(h.platform, h.ad_id),
                                   "price": h.price, "location": h.location, "shipping_price": h.shipping_price,
                                   "shipping": h.shipping, **_screen_data(h), **_scam_data(h), **_geizhals_data(h)})
        if auto_buy.enabled(agent):       # the passing hits: bought after a countdown (Telegram can cancel it)
            await auto_buy.consider(agent, [h for h in res.new if h.id in good])
        if len(good) > MAX_PUSH_PER_RUN:
            sites = list(dict.fromkeys(h.platform for h in res.new))
            await notify("hit", label(sites[0]) if len(sites) == 1 else name,
                         t("+{n} more new hits", n=len(good) - MAX_PUSH_PER_RUN),
                         data={"agent": name, "platform": sites[0] if len(sites) == 1 else None, "count": len(good)})
    if notify_drops and res.price_drops and auto_buy.enabled(agent):   # a price drop into the maximum buys too
        await auto_buy.consider(agent, [h for h in res.price_drops if passes(agent, h) and not _unjudged(agent, h, asked)])
    if notify_drops:
        for h in res.price_drops[:MAX_PUSH_PER_RUN]:
            await notify("price_drop", _scam_prefix(h) + t("{name}: price dropped", name=label(h.platform)),
                         f"{h.heading}: {h.old_price:g} → {h.price:g} {_site(h.platform).currency}{_ship_suffix(h)}"
                         + _scam_line(h)
                         + _geizhals_line(h) + _screen_line(h), url=h.url, image=h.image_url, tag=hit_tag(h),
                         push=passes(agent, h) and not _unjudged(agent, h, asked),
                         data={"agent": name, "platform": h.platform, "ad_id": h.ad_id, "ref": ad_ref(h.platform, h.ad_id),
                               "price": h.price, "old_price": h.old_price, **_screen_data(h), **_scam_data(h),
                               **_geizhals_data(h)})
    return res


def _site(pid: str | None) -> platforms.Platform:
    """The adapter of a hit's site; willhaben's when that site is gone (wording only: name, currency, country)."""
    try:
        return platforms.get(pid or "willhaben")
    except platforms.UnknownPlatform:
        return platforms.get("willhaben")


def _geizhals_cents(hit: Hit) -> tuple[int, int] | None:
    """(ad price, Geizhals new price) in whole cents, None without both: whole numbers compare exactly (in floats
    700 € · 0.7 is 489.99999… €, so an ad exactly 30 % below would miss a 30 % rule)."""
    new_price = round(((hit.geizhals or {}).get("price_from") or 0) * 100)
    return None if new_price <= 0 or hit.price is None else (round(hit.price * 100), new_price)


LEVELS = ("bargain", "cheap", "fair")     # the zones an agent's Geizhals rule can ask for (price_zones.NAMES order)
LEVEL_DEAL = {"bargain": "great", "cheap": "good", "fair": "fair"}   # the same in Claude's price verdict (screening)


def level_choices() -> list[tuple[str, str]]:
    """The price zones the agent drawer offers, with where each ends now: [("cheap", "cheap (used below 75 %, new
    below 100 % of the new price)"), ...]. The limits are Settings → Search & prices → Price rating."""
    from app.services import deals, price_zones

    z = price_zones.config()
    def cap(s: str) -> str:
        return s[:1].upper() + s[1:]

    return [(lvl, t("{level} (used below {used} %, new below {new} % of the new price)", level=cap(deals.level_label(lvl)),
                    used=z["used"][i], new=z["new"][i])) for i, lvl in enumerate(LEVELS)]


def geizhals_rule(agent: SearchAgent) -> bool:
    """Does the agent hold back ads by their Geizhals new price (a % or a price zone)?"""
    return bool(agent.geizhals_pct or agent.geizhals_level in LEVELS)


def geizhals_passes(agent: SearchAgent, hit: Hit) -> bool:
    """The agent's Geizhals rule: the price is at least `geizhals_pct` % below the Geizhals new price, or its price zone
    (Settings → Search & prices → Price rating, used or new by the item's condition) is `geizhals_level` or cheaper. Without a
    Geizhals price (not found, Geizhals unreachable) a zone takes Claude's price verdict (LEVEL_DEAL) when the
    screening gave one. Without the rule, any price to compare or, for a zone, with a defective item: it passes."""
    cents = _geizhals_cents(hit)
    if not geizhals_rule(agent):
        return True
    if cents is None and agent.geizhals_level in LEVELS and (hit.similar or {}).get("level"):
        from app.services import price_zones           # no Geizhals price: the similar ads' rating (_similar_prices)
        return price_zones.NAMES.index(hit.similar["level"]) <= price_zones.NAMES.index(agent.geizhals_level)
    if cents is None:            # nor a rating by similar ads: a price zone asks Claude's price verdict, if there is one
        need = LEVEL_DEAL.get(agent.geizhals_level or "")
        return need is None or hit.screen_deal in (None, "unknown") or \
            screening.DEAL_RANK.get(hit.screen_deal, 1) >= screening.DEAL_RANK[need]
    if agent.geizhals_level in LEVELS:
        from app.services import deals, price_zones

        lvl = deals.hit_level(hit)
        return lvl is None or price_zones.NAMES.index(lvl) <= price_zones.NAMES.index(agent.geizhals_level)
    price, new_price = cents
    return price * 100 <= new_price * (100 - agent.geizhals_pct)


def passes(agent: SearchAgent, hit: Hit) -> bool:
    """Should this hit push: the screening lets it through, it keeps the Geizhals rule and the distance limit."""
    return screening.passes(agent, hit) and geizhals_passes(agent, hit) and distance_passes(agent, hit)


def ships(hit: Hit) -> bool:
    return hit.shipping == shipping_info.SHIP or hit.shipping_price is not None


def pickup_limit(agent: SearchAgent) -> int | None:
    """The agent's own "pickup within N km", else the one of Settings → Shipping & pickup."""
    from app.services import geo

    return agent.max_km or geo.pickup_radius()


def distance_passes(agent: SearchAgent, hit: Hit) -> bool:
    """Pickup only within the limit: an ad that ships always passes, so does one whose place is not known."""
    from app.services import geo

    return not geo.too_far(hit, pickup_limit(agent))


async def _locate(hits: list[Hit]) -> None:
    """Coordinates for the hits a distance limit judges (their postcode's centre, cached), stored."""
    from app.services import geo

    located = await geo.locate_hits([h for h in hits if not ships(h)])
    if not located:
        return
    with session() as db:
        for h in located:
            row = db.get(Hit, h.id)
            if row is not None:
                row.lat, row.lon = h.lat, h.lon
                db.add(row)
        db.commit()


async def locate_job() -> None:
    """Hourly: coordinates for the hits shown without any (the cards' distance), a few new postcodes at a time."""
    from app.services import geo

    if geo.home_address() is None:
        return
    with session() as db:
        hits = list(db.exec(select(Hit).where(col(Hit.lat).is_(None), Hit.state != CLEARED)
                            .order_by(col(Hit.id).desc()).limit(2000)))
    located = await geo.locate_hits(hits)
    if located:
        with session() as db:
            for h in located:
                row = db.get(Hit, h.id)
                if row is not None:
                    row.lat, row.lon = h.lat, h.lon
                    db.add(row)
            db.commit()


SIMILAR_PER_RUN = 8              # hits rated by their similar ads per check (each: the sites' searches, Claude's pick)
SIMILAR_TTL = timedelta(days=3)


async def _similar_prices(hits: list[Hit]) -> None:
    """For hits without a Geizhals new price: their rating among similar ads (the price check: comparable searches,
    Claude's choice; the user's limits for similar ads), stored as hit.similar – the price-zone rule goes by it. At
    most SIMILAR_PER_RUN per check, again after SIMILAR_TTL; a failing check leaves the hit to Claude's verdict."""
    from app.platforms.types import AdDetail
    from app.services import deals

    todo = [h for h in hits if not h.similar or utcnow() - _aware_dt(h.similar.get("at")) > SIMILAR_TTL][:SIMILAR_PER_RUN]
    for h in todo:
        try:
            res = await deals.analyze(AdDetail.from_hit(h))
        except Exception as e:  # noqa: BLE001 - one hit's searches must not stop the check
            log.info("similar ads of hit %s not rated: %s", h.id, e)
            continue
        pos, comp = res.get("position") or {}, res.get("comparables") or {}
        h.similar = {"level": pos.get("level") if pos.get("basis") == "comparables" else None,
                     "median": pos.get("median"), "count": comp.get("count") or 0, "at": utcnow().isoformat()}
        with session() as db:
            row = db.get(Hit, h.id)
            if row is not None:
                row.similar = h.similar
                db.add(row)
                db.commit()


def _aware_dt(raw: str | None) -> datetime:
    try:
        at = datetime.fromisoformat(raw) if raw else datetime.min.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


async def _geizhals_prices(hits: list[Hit]) -> None:
    """The Geizhals product of every hit not asked yet (the price check's lookup, cached 6 h per product name). When
    Geizhals fails (blocked, down) the hit stays not asked: it passes now and a later run asks again."""
    from app.services import deals, geizhals

    answered = []
    for h in hits:
        if h.geizhals is not None:
            continue
        try:
            gh = await deals.title_match(h.heading, raise_errors=True)
        except geizhals.GeizhalsError:
            continue
        h.geizhals = {k: gh[k] for k in ("name", "url", "price_from")} if gh else {}
        answered.append(h)
    if not answered:
        return
    with session() as db:
        for h in answered:
            row = db.get(Hit, h.id)
            if row is not None:
                row.geizhals = h.geizhals
                db.add(row)
        db.commit()


def replace_geizhals(hit_id: int, gh: dict[str, Any] | None) -> None:
    """Claude called the hit's Geizhals product wrong (price check): the product Claude named instead, or none – final
    ("checked"), so neither the card nor the agent's Geizhals rule uses the wrong price again."""
    with session() as db:
        row = db.get(Hit, hit_id)
        if row is None:
            return
        row.geizhals = ({k: gh.get(k) for k in ("name", "url", "price_from", "by", "parts", "missing") if k in gh}
                        | {"checked": True}) if gh else {"checked": True}
        db.add(row)
        db.commit()


def remember_geizhals(hit_id: int, gh: dict[str, Any]) -> None:
    """The product Claude found on Geizhals for a hit (price check in the viewer). The agent's rule has run already;
    an automatic match the hit has is kept."""
    with session() as db:
        row = db.get(Hit, hit_id)
        if row is None or row.geizhals:
            return
        row.geizhals = {k: gh.get(k) for k in ("name", "url", "price_from", "by")}
        db.add(row)
        db.commit()


def _unjudged(agent: SearchAgent, h: Hit, asked: set[int]) -> bool:
    """With screening on: Claude has no verdict on the hit and was not sent it in this run (`asked`; a failed call lets
    it pass, as it does the push). A hit the Geizhals rule held back has none, and neither has a drop that is not
    reported or did not fit into the call."""
    return bool(agent.screen) and h.screen_relevant is None and h.id not in asked


def _holds(agent: SearchAgent) -> bool:
    """New hits of this agent are pending until judged: it pushes, and screens, has a Geizhals rule or a distance
    limit."""
    return bool(agent.notify and (agent.screen or geizhals_rule(agent) or pickup_limit(agent)))


def _judges(agent: SearchAgent) -> bool:
    """The agent judges its new hits (it pushes, and screens or has a Geizhals rule or a distance limit): then it
    judges its quiet ones too. An agent without pushes judges neither (its hits are simply new or seen)."""
    return _holds(agent)


def _file_quiet(agent: SearchAgent, quiet: list[Hit], asked: set[int]) -> None:
    """The quiet hits (first check, new search) once judged: "seen" when they pass, "filtered" when not; one Claude was
    not sent stays pending for the next check. Never pushed."""
    moves = [(h, "filtered" if not passes(agent, h) else PENDING if _unjudged(agent, h, asked) else "seen")
             for h in quiet if h.state == PENDING]
    moves = [m for m in moves if m[1] != PENDING]
    if not moves:
        return
    with session() as db:
        for h, state in moves:
            row = db.get(Hit, h.id)
            if row is not None and row.state == PENDING:      # unless it was saved or ignored meanwhile
                row.state = h.state = state
                db.add(row)
        db.commit()


def _file_screened(agent: SearchAgent, new: list[Hit], drops: list[Hit], asked: set[int]) -> None:
    """A pending hit (new, or left from an earlier check) that the screening or the Geizhals rule holds back gets no
    push and goes to "filtered"; one Claude was not sent (more than a check screens) stays pending for the next check;
    one that passed both becomes new. A filtered or pending hit that passes after a price drop is pushed and becomes
    new, with screening on only once Claude has judged it or was sent it in this run."""
    moves = [(h, h.state, "filtered" if not passes(agent, h) else PENDING if _unjudged(agent, h, asked) else "new")
             for h in new if h.state in ("new", PENDING)]
    moves += [(h, h.state, "new") for h in drops
              if h.state in ("filtered", PENDING) and passes(agent, h) and not _unjudged(agent, h, asked)]
    moves = [m for m in moves if m[1] != m[2]]
    if not moves:
        return
    with session() as db:
        for h, old, state in moves:
            row = db.get(Hit, h.id)
            if row is not None and row.state == old:      # unless it was saved, seen or ignored meanwhile
                row.state = h.state = state
                db.add(row)
        db.commit()


NEGOTIABLE_HIDDEN = ("kleinanzeigen",)      # sites whose "VB" ads are not shown among the hits


def negotiable_only(h: Any) -> bool:
    """A Kleinanzeigen ad with "VB" ("VB" alone or "120 € VB", price negotiable): not shown among the hits."""
    return getattr(h, "platform", None) in NEGOTIABLE_HIDDEN and "VB" in (getattr(h, "price_display", None) or "")


SETTING_VB_REFILED = "hits_vb_refiled"       # refile_negotiable() ran


def refile_negotiable() -> int:
    """Once, at startup: Kleinanzeigen "VB" hits that are new, pending or seen go to "filtered" (they were counted as
    new in the sidebar while the hits page left them out). Returns how many moved."""
    if get_setting(SETTING_VB_REFILED):
        return 0
    n = 0
    with session() as db:
        for h in db.exec(select(Hit).where(col(Hit.platform).in_(NEGOTIABLE_HIDDEN),
                                           col(Hit.state).in_(("new", PENDING, "seen")))):
            if negotiable_only(h):
                h.state = "filtered"
                db.add(h)
                n += 1
        db.commit()
    set_setting(SETTING_VB_REFILED, "1")
    return n


def refile_screened() -> None:
    """Once, at startup: new hits the screening held back before the "filtered" state existed move there."""
    if get_setting(SETTING_REFILED):
        return
    with session() as db:
        agents = {a.id: a for a in db.exec(select(SearchAgent).where(SearchAgent.screen == True))}  # noqa: E712
        for h in db.exec(select(Hit).where(Hit.state == "new", col(Hit.screen_relevant).is_not(None))):
            if h.agent_id in agents and not screening.passes(agents[h.agent_id], h):
                h.state = "filtered"
                db.add(h)
        db.commit()
    set_setting(SETTING_REFILED, "1")


def _screen_line(h: Hit) -> str:
    line = screening.summary(h)
    return f"\n{line}" if line else ""


def _ship_suffix(h: Hit) -> str:
    """" · + € 4,90 shipping" after a pushed price; "" when the shipping is not known."""
    from app.platforms import shipping_info

    text = shipping_info.label(h.shipping_price, h.shipping, "€")
    return f" · {text}" if text else ""


def _hit_text(h: Hit) -> str:
    return f"{h.heading} ({h.location or '?'})" + _scam_line(h) + _geizhals_line(h) + _screen_line(h)


def geizhals_below_pct(h: Hit) -> int | None:
    """How many % the hit is below its Geizhals new price (negative: above); None without both prices. Whole % rounded
    down, so it shows the rule's X % exactly when the hit keeps the rule (29.6 % below is 29, not 30) and an ad dearer
    than new is at least 1 % more, never 0 % less."""
    cents = _geizhals_cents(h)
    if cents is None:
        return None
    price, new_price = cents
    return (new_price - price) * 100 // new_price


def _geizhals_line(h: Hit) -> str:
    pct = geizhals_below_pct(h)
    if pct is None:
        return ""
    price = f"{h.geizhals['price_from']:g} €"
    if pct < 0:
        return "\n" + t("Geizhals: new from {price}, {pct} % more", price=price, pct=-pct)
    return "\n" + t("Geizhals: new from {price}, {pct} % less", price=price, pct=pct)


def _geizhals_data(h: Hit) -> dict:
    """The Geizhals comparison in the HA event."""
    pct = geizhals_below_pct(h)
    return {} if pct is None else {"geizhals_price": h.geizhals["price_from"], "geizhals_below_pct": pct}


def _screen_data(h: Hit) -> dict:
    """Screening result in the HA event, so automations can filter on it too."""
    if h.screen_relevant is None:
        return {}
    return {"screen_relevant": h.screen_relevant, "screen_deal": h.screen_deal, "screen_reason": h.screen_reason,
            "screen_issues": h.screen_issues or []}


def _known_conditions(db, ads: list[Listing]) -> dict[tuple[str, str], tuple[str, str | None]]:
    """The condition other agents' hits of these ads already know, by (platform, ad id): a list without one (Shpock
    states it only on the ad page) then gives a new hit what the viewer learned there. One query."""
    if not ads:
        return {}
    rows = db.exec(select(Hit.platform, Hit.ad_id, Hit.item_condition, Hit.item_condition_label)
                   .where(col(Hit.ad_id).in_(sorted({ad.ad_id for ad in ads})), col(Hit.item_condition).is_not(None)))
    return {((pid or "willhaben"), ad_id): (cond, label) for pid, ad_id, cond, label in rows}


def _scam_context(hits, ads) -> dict:
    """Agent price median for the scam check: recent stored hits plus this page (newest price per ad)."""
    since = (utcnow() - timedelta(days=scam.CONTEXT_DAYS)).replace(tzinfo=None)   # SQLite gives naive UTC
    prices = {h.ad_id: h.price for h in hits if h.screen_relevant is not False
              and (h.first_seen_at is None or h.first_seen_at.replace(tzinfo=None) >= since)}
    rejected = {h.ad_id for h in hits if h.screen_relevant is False}
    prices.update({a.ad_id: a.price for a in ads if a.ad_id and a.ad_id not in rejected})
    return scam.median_context(list(prices.values()))


def _mark_scam_issue(h: Hit) -> None:
    """A severe rule-based flag counts as screening reason "scam_suspect" (also for agents without screening)."""
    if scam.level(h.scam_flags) == "high":
        h.screen_issues = list(dict.fromkeys([*(h.screen_issues or []), "scam_suspect"]))


def _scam_prefix(h: Hit) -> str:
    return "! " if scam.warnings(h.scam_flags) else ""


def _scam_line(h: Hit) -> str:
    top = scam.headline(h.scam_flags)
    return f"\n! {top}" if top else ""


def _scam_data(h: Hit) -> dict:
    """Scam flag codes in the HA event (warn/high only), so automations can filter on them."""
    w = scam.warnings(h.scam_flags)
    return {"scam_flags": [f["code"] for f in w], "scam_level": scam.level(w)} if w else {}


def keep_days() -> int:
    """Settings: after how many days a hit (all but saved ones) is cleared by itself; 0 = never."""
    raw = (get_setting(KEEP_DAYS_KEY) or get_setting(OLD_KEEP_DAYS_KEY) or "").strip()
    return int(raw) if raw.isdigit() else KEEP_DAYS_DEFAULT


def set_keep_days(days: int) -> int:
    days = min(max(int(days), 0), 3650)
    set_setting(KEEP_DAYS_KEY, str(days))
    return days


def clear_hits(states: Iterable[str] = ("filtered",), agent_id: int | None = None,
               older_than_days: int | None = None, platform: str | None = None,
               ids: Iterable[int] | None = None) -> int:
    """Hits in `states` (of one agent or site, first seen more than `older_than_days` ago, or just those `ids`) become
    cleared: shown nowhere, their description dropped (Claude's short reason stays with its verdict: the ad viewer
    shows both when the ad comes up again). The row stays so a search that still lists the ad does not find it new
    (and screen it again)."""
    states = [x for x in states if x in EXPIRING]       # never saved ones
    with session() as db:
        q = select(Hit).where(col(Hit.state).in_(states))
        if agent_id:
            q = q.where(Hit.agent_id == agent_id)
        if platform:
            q = q.where(Hit.platform == platform)
        if ids is not None:
            q = q.where(col(Hit.id).in_(list(ids)))
        if older_than_days is not None:
            q = q.where(Hit.first_seen_at < utcnow() - timedelta(days=older_than_days))
        n = 0
        for h in db.exec(q):
            h.state, h.body = CLEARED, None
            db.add(h)
            n += 1
        db.commit()
    return n


def prune_cleared() -> int:
    """Cleared hits no search has listed for GONE_DAYS: the ad is gone, so is its row."""
    with session() as db:
        rows = list(db.exec(select(Hit).where(Hit.state == CLEARED,
                                              Hit.last_seen_at < utcnow() - timedelta(days=GONE_DAYS))))
        for h in rows:
            db.delete(h)
        db.commit()
    return len(rows)


async def retention_job() -> None:
    """Scheduler (every 6 h): the hits' retention (all but saved ones), then the rows of ads gone for good."""
    try:
        days = keep_days()
        cleared = clear_hits(EXPIRING, older_than_days=days) if days else 0
        gone = prune_cleared()
    except Exception as e:  # noqa: BLE001 - keep the scheduler job alive
        log.warning("hits cleanup failed: %s", e)
        return
    if cleared or gone:
        log.info("hits: %d cleared, %d rows of gone ads deleted", cleared, gone)


STATUS_WORDS = {"sold": "sold", "verkauft": "sold", "reserved": "reserved", "reserviert": "reserved",
                "expired": "expired", "inactive": "expired", "deleted": "gone"}


async def _shipping_pages(hits: list[Hit]) -> None:
    """Hits about to be pushed whose list did not state the shipping price: their ad page (one request each, through
    the site's limiter and VPN) tells it, and their status. A failure only leaves it unknown."""
    for h in hits:
        p = _site(h.platform)
        if h.shipping_price is not None or h.shipping == "pickup" or not p.can("detail"):
            continue
        try:
            d = await p.detail(h.ad_id, url=h.url)
        except (PlatformError, Exception) as e:  # noqa: BLE001 - the push goes out without it
            log.info("shipping: ad page of %s %s not loaded: %s", h.platform, h.ad_id, e)
            continue
        record_status(h.platform or "willhaben", h.ad_id, site_status(d), d)
        if d is not None:
            apply_shipping(h, d)
        h.site_status = site_status(d)


def site_status(detail: Any) -> str:
    """The ad's status as the hit keeps it: gone when the site no longer has the ad, else its status word."""
    if detail is None:
        return "gone"
    raw = (getattr(detail, "status", None) or "unknown").lower()
    return "active" if raw == "active" else STATUS_WORDS.get(raw, raw)


def hit_tag(h: Hit) -> str:
    """Tag of a hit's push (new hit, price drop): the same tag replaces it, and seeing the hit takes it off the phone."""
    return f"wh-hit-{h.platform or 'willhaben'}-{h.ad_id}"


def pushes_seen(hits: list[Hit]) -> None:
    """The owner has seen these hits (opened, saved, ignored, marked seen): their pushes go off the phone (Home
    Assistant); Telegram's messages keep their buttons. Only hits that were pushed (an inbox entry) are asked for."""
    from app.db import EventLog
    refs = {(h.platform or "willhaben", h.ad_id) for h in hits if h.ad_id}
    if not refs:
        return
    keys = {ad for _p, ad in refs} | {f"{p}:{ad}" for p, ad in refs}
    with session() as db:
        pushed = set(db.exec(select(EventLog.ad_id).where(col(EventLog.ad_id).in_(keys),
                                                          col(EventLog.quiet).is_not(True))).all())
    tags = [f"wh-hit-{p}-{ad}" for p, ad in refs if ad in pushed or f"{p}:{ad}" in pushed][:50]
    if not tags:
        return
    from app.services.notify import clear_push

    async def run() -> None:
        for tag in tags:                  # Telegram's message keeps its buttons (keep, ignore, open)
            await clear_push(tag, telegram_buttons=False)
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:           # no loop (a sync caller in tests): nothing to clear
        return
    _image_tasks.add(task)
    task.add_done_callback(_image_tasks.discard)


_image_tasks: set[asyncio.Task] = set()
MAX_KEPT_PHOTOS = 12                       # a saved hit's photos kept locally (the viewer shows as many thumbnails)
_shown_photos: dict[int, list[str]] = {}   # hit id -> the photos its ad page showed last (the viewer), for a save


def remember_photos(hit_id: int, photos: list[str] | None) -> None:
    """The photos the ad viewer just showed for a hit: saving it then keeps them all, without asking the site again."""
    if photos:
        _shown_photos.pop(hit_id, None)
        _shown_photos[hit_id] = list(photos)
        while len(_shown_photos) > 200:
            _shown_photos.pop(next(iter(_shown_photos)))


def shown_photos(hit_id: int) -> list[str]:
    return _shown_photos.get(hit_id, [])


def kept_photos(h: Hit) -> list[str]:
    """The file names of a hit's kept photos, the first (image_path) first."""
    if not h.image_path or h.id is None:
        return []
    folder = get_settings().hit_images_dir
    more = sorted(folder.glob(f"hit-{h.id}-*"), key=lambda p: int(p.stem.rsplit("-", 1)[1]) if p.stem.rsplit("-", 1)[1].isdigit() else 0)
    return [h.image_path] + [p.name for p in more]


async def keep_image(hit_id: int, url: str | list[str] | None = None) -> bool:
    """A saved hit's photos into data/hit-images (url: the ad page's photos, fresh, else the list's thumbnail), so
    they still show once the site drops them (sold, deleted). True when the first one is kept (also when it already
    was)."""
    from app.services import ad_images

    with session() as db:
        h = db.get(Hit, hit_id)
    if h is None:
        return False
    photos = [url] if isinstance(url, str) else list(url or [])
    folder = get_settings().hit_images_dir
    folder.mkdir(parents=True, exist_ok=True)
    if not h.image_path:
        for src in dict.fromkeys(u for u in (photos[:1] + [h.image_url]) if u):
            p = await ad_images.download(src, folder, f"hit-{hit_id}", platform=h.platform)
            if p is not None:
                with session() as db:
                    row = db.get(Hit, hit_id)
                    if row is not None:
                        row.image_path = p.name
                        db.add(row)
                        db.commit()
                h.image_path = p.name
                break
        else:
            return False
    for i, src in enumerate(photos[1:MAX_KEPT_PHOTOS], 1):     # the others, each once
        if src and not any(folder.glob(f"hit-{hit_id}-{i}.*")):
            await ad_images.download(src, folder, f"hit-{hit_id}-{i}", platform=h.platform)
    return True


def keep_image_later(hit_id: int, url: str | list[str] | None = None) -> None:
    try:
        task = asyncio.get_running_loop().create_task(keep_image(hit_id, url))
    except RuntimeError:           # no loop (a sync caller in tests): nothing to keep
        return
    _image_tasks.add(task)
    task.add_done_callback(_image_tasks.discard)


def record_status(platform_id: str, ad_id: str, status: str, detail: Any = None) -> None:
    """The status the site just gave for an ad, and the shipping its page states, on every hit of it (several agents
    may hold it). A hit held back only for its distance that turns out to ship goes back to New."""
    with session() as db:
        agents = {}
        for h in db.exec(select(Hit).where(Hit.platform == platform_id, Hit.ad_id == ad_id)):
            h.site_status, h.status_checked_at = status, utcnow()
            if detail is not None:
                was_far = h.state == "filtered" and not ships(h)
                apply_shipping(h, detail)
                if not h.published_at:      # Vinted: only its item page says when it went up ("vor 6 Min.")
                    h.published_at = getattr(detail, "first_published_at", None) or getattr(detail, "published_at", None)
                if was_far and ships(h) and status == "active":
                    agent = agents.get(h.agent_id) or db.get(SearchAgent, h.agent_id)
                    agents[h.agent_id] = agent
                    # never judged by Claude (held back before the screening): not into New unscreened
                    if agent is not None and passes(agent, h) and not (agent.screen and h.screen_relevant is None):
                        h.state = "new"
            db.add(h)
            photos = getattr(detail, "images", None) or []
            if h.state == "saved" and (not h.image_path or len(kept_photos(h)) < min(len(photos), MAX_KEPT_PHOTOS)):
                keep_image_later(h.id, photos)              # still online: keep its photos before they are gone
        db.commit()


def held_back_reasons(agent: SearchAgent | None, h: Hit) -> list[str]:
    """Why a hit is under Filtered out (its card says it): Claude's screening, the Geizhals rule, the distance."""
    if agent is None:
        return []
    out = []
    if not screening.passes(agent, h):
        out.append(t("Claude's screening"))
    if not geizhals_passes(agent, h):
        from app.services.deals import level_label

        rule = t("{level} or cheaper", level=level_label(agent.geizhals_level)) if agent.geizhals_level \
            else t("{pct} % below", pct=agent.geizhals_pct)
        out.append(t("Geizhals rule: {rule}", rule=rule))
    if not distance_passes(agent, h):
        out.append(t("pickup only, farther than {n} km", n=pickup_limit(agent)))
    return out


WATCH_EVERY_H = 20            # a saved hit's ad is loaded about once a day
WATCH_PER_RUN = 15            # the job runs every 2 h: a few ad pages each time, not a burst
_STATUS_NEWS = {"sold": "Saved ad sold", "reserved": "Saved ad reserved", "gone": "Saved ad no longer online",
                "expired": "Saved ad expired"}


def watch_saved_on() -> bool:
    return get_setting("watch_saved") != "0"


async def watch_saved_job() -> None:
    """Saved hits: their ad pages, about once a day, for a price drop or a new status (sold, reserved, gone, available
    again), each told with a notification of the kind "saved". Ads that are gone are no longer asked."""
    if not watch_saved_on():
        return
    due = utcnow() - timedelta(hours=WATCH_EVERY_H)
    with session() as db:
        rows = list(db.exec(select(Hit).where(Hit.state == "saved", or_(col(Hit.site_status).is_(None),
                                                                         Hit.site_status != "gone"),
                                              or_(col(Hit.status_checked_at).is_(None), Hit.status_checked_at < due))
                            .order_by(col(Hit.status_checked_at).asc().nulls_first()).limit(WATCH_PER_RUN * 3)))
    with session() as db:                    # saved before photos were kept: try the list's photo once more
        for h in db.exec(select(Hit).where(Hit.state == "saved", col(Hit.image_path).is_(None),
                                           col(Hit.image_url).is_not(None)).limit(WATCH_PER_RUN)):
            await keep_image(h.id)
    seen: set[tuple[str, str]] = set()
    asked = 0
    for h in rows:
        key = (h.platform or "willhaben", h.ad_id)
        if key in seen or not _site(h.platform).can("detail"):
            continue
        seen.add(key)
        if asked >= WATCH_PER_RUN:
            break
        asked += 1
        try:
            await _watch_one(h)
        except Exception as e:  # noqa: BLE001 - one ad must not stop the others; the next run asks again
            log.info("saved hit %s: ad page not loaded: %s", h.id, e)


async def _watch_one(h: Hit) -> None:
    p = _site(h.platform)
    d = await p.detail(h.ad_id, url=h.url)
    status, before = site_status(d), h.site_status
    record_status(h.platform or "willhaben", h.ad_id, status, d)
    news: list[tuple[str, str]] = []
    price = getattr(d, "price", None) if d is not None else None
    if price is not None and h.price is not None and p.price_dropped(h.price, price):
        with session() as db:
            row = db.get(Hit, h.id)
            if row is not None:
                row.old_price, row.price, row.price_dropped_at = row.price, price, utcnow()
                row.price_display = getattr(d, "price_display", None) or row.price_display
                db.add(row)
                db.commit()
        news.append((t("Saved ad: price dropped"), f"{h.heading}: {h.price:g} → {price:g} {p.currency}"))
    elif price is not None and h.price != price:
        with session() as db:
            row = db.get(Hit, h.id)
            if row is not None:
                row.price = price
                row.price_display = getattr(d, "price_display", None) or row.price_display
                db.add(row)
                db.commit()
    if before is not None and status != before:
        if status in _STATUS_NEWS:
            news.append((t(_STATUS_NEWS[status]), h.heading))
        elif status == "active" and before in ("reserved", "sold"):
            news.append((t("Saved ad available again"), h.heading))
    for title, text in news:
        await notify("saved", f"{p.name} · {title}" if platforms.multi() else title, text + _ship_suffix(h),
                     url=h.url, image=h.image_url,
                     data={"platform": h.platform, "ad_id": h.ad_id, "ref": ad_ref(h.platform, h.ad_id),
                           "price": price if price is not None else h.price, "status": status})


SWEEP_PER_RUN = 20             # recent hits whose ad page is read per hourly run (sold prices for the price check)
SWEEP_DAYS = 30                # first seen within this many days
SWEEP_EVERY = timedelta(days=2)
SWEEP_SKIP = {"facebook"}      # its ad pages trip its login block (10-27): the agents' own checks only


async def status_sweep_job() -> None:
    """Hourly: the ad pages of a few recent hits (not cleared, not ruled out, still active as far as known, checked
    longest ago) – their status kept quietly. A hit that went sold or gone keeps its last price: the price
    check's "sold or taken down recently" (deals.sold_ads). Off with the setting "status_sweep" = "0"."""
    if get_setting("status_sweep") == "0":
        return
    now = utcnow()
    with session() as db:
        rows = list(db.exec(select(Hit).where(
            Hit.state != CLEARED, or_(col(Hit.screen_relevant).is_(None), Hit.screen_relevant == True),  # noqa: E712
            or_(col(Hit.site_status).is_(None), col(Hit.site_status).in_(("active", "reserved"))),
            Hit.first_seen_at >= now - timedelta(days=SWEEP_DAYS),
            or_(col(Hit.status_checked_at).is_(None), Hit.status_checked_at < now - SWEEP_EVERY))
            .order_by(col(Hit.status_checked_at).asc().nulls_first(), col(Hit.id).desc()).limit(SWEEP_PER_RUN * 3)))
    seen: set[tuple[str, str]] = set()
    for h in rows:
        key = (h.platform or "willhaben", h.ad_id)
        if key in seen or key[0] in SWEEP_SKIP or not _site(h.platform).can("detail"):
            continue
        seen.add(key)
        if len(seen) > SWEEP_PER_RUN:
            break
        try:
            d = await _site(h.platform).detail(h.ad_id, url=h.url)
        except Exception as e:  # noqa: BLE001 - one ad must not stop the others; the next run asks again
            log.info("status of hit %s not loaded: %s", h.id, e)
            continue
        # the status only: the price stays the list's (a drop is the agent's to notice and push)
        record_status(h.platform or "willhaben", h.ad_id, site_status(d), d)


CHECK_MAX = 100                # hits a "Check availability" reads at most (one ad page each, through the limiter)


async def check_hits(hit_ids: list[int], job: Any = None) -> dict[str, int]:
    """"Check availability" on the hits page: the ad page of each hit shown (each ad once, Facebook left out like the
    hourly sweep) – still online, reserved, sold or gone. Returns {checked, online, gone, failed}."""
    with session() as db:
        rows = list(db.exec(select(Hit).where(col(Hit.id).in_(hit_ids[:CHECK_MAX]))))
    todo, seen = [], set()
    for h in rows:
        key = (h.platform or "willhaben", h.ad_id)
        if key in seen or key[0] in SWEEP_SKIP or not _site(h.platform).can("detail"):
            continue
        seen.add(key)
        todo.append(h)
    out = {"checked": 0, "online": 0, "gone": 0, "failed": 0}
    for i, h in enumerate(todo, 1):
        if job is not None:
            job.progress(i - 1, t("Checking {title}", title=(h.heading or h.ad_id)[:50]), None)
        try:
            d = await _site(h.platform).detail(h.ad_id, url=h.url)
        except Exception as e:  # noqa: BLE001 - one ad must not stop the others
            log.info("availability of hit %s not loaded: %s", h.id, e)
            out["failed"] += 1
            continue
        status = site_status(d)
        record_status(h.platform or "willhaben", h.ad_id, status, d)
        out["checked"] += 1
        out["online" if status == "active" else "gone"] += 1
    return out


async def check_hit(hit_id: int) -> Hit | None:
    """The sync button of a hit: load the ad from its site (one request through its limiter and VPN) and keep its
    status. PlatformError when the site cannot be asked; None when the hit is gone."""
    with session() as db:
        h = db.get(Hit, hit_id)
    if h is None:
        return None
    p = _site(h.platform)
    if not p.can("detail"):
        raise PlatformError(t("{site} cannot load a single ad", site=p.name))
    d = await p.detail(h.ad_id, url=h.url)
    record_status(h.platform or "willhaben", h.ad_id, site_status(d), d)
    with session() as db:
        return db.get(Hit, hit_id)
