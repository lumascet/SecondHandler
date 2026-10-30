"""Geizhals.at lookup for ad drafts: find the product, read its spec sheet and lowest new price.

Plain HTML pages (no API): search results are `galleryview__item` cards, an article page
(`...-a<id>.html`) lists its properties as `<dl class="specs-grid">` with dt/dd pairs and the
lowest offer as `lowPrice: <n>`. Requests are few (a search or up to three, then one product page, which a mere
price lookup skips) and 3 s apart (bursts of searches got 403 at 2 s); a 403/429 pauses Geizhals like any other site.
With its switch in Settings → VPN on, the requests go through its VPN server (app/platforms/vpn.py), fail
closed; there a 403/429, a bot check or no answer refuses the server (a search for another) instead of pausing Geizhals.

Matching (checked against real searches, 2026-10-13): the first search result is often another variant ("iPhone 16"
→ "16 Pro Max", "UniFi Switch 24" → "Switch Pro 24"), so a result counts only when every number of the query is in
its name, an order code ("USW-24-POE") in its name or link, and it has no variant word (Pro, Max, Lite …), pack or
accessory word the query lacks. Spelling is forgiven ("I-Phone" = "iPhone", "Rasperry" ≈ "Raspberry"). A query
that finds nothing fitting is retried with its order code alone, and with its key words when nothing was found.
"""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from sqlmodel import col, delete, func, select

from app import db
from app.config import get_settings
from app.i18n import t
from app.platforms import vpn
from app.platforms.base import PlatformError
from app.platforms.httpclient import TRIAL_TIMEOUT_S, current_trial
from app.willhaben.public import Blocked, RateLimiter

log = logging.getLogger(__name__)

BASE = "https://geizhals.at"
_limiter = RateLimiter(3.0, name="Geizhals")
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport
CACHE_DAYS = 7                # a cached search or product page is used this long, then fetched again; unused this
                              # long, it is deleted (prune_cache())
CHALLENGED_TTL_S = 6 * 3600   # a search Cloudflare challenges (not the server) is not asked again for this long
_challenged: dict[str, float] = {}   # url -> until (_get_vpn_once())

_CARD_RE = re.compile(r'<article class="galleryview__item[^"]*"(.*?)</article>', re.S)
_NAME_RE = re.compile(r'class="galleryview__name-link"[^>]*>(.*?)</a>', re.S)
_HREF_RE = re.compile(r'href="(https://geizhals\.at/[^"#]+-a\d+\.html)"')
_PRICE_RE = re.compile(r'<span class="price">\s*€\s*([\d.]+,\d{2})')
_IMG_RE = re.compile(r'<img src="(https://gzhls\.at/[^"]+)"')
_SPECS_RE = re.compile(r'<dl\s+class="specs-grid[^"]*"[^>]*>(.*?)</dl>', re.S)
_ITEM_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_LOW_RE = re.compile(r"lowPrice:\s*([\d.]+)")
_UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(gb|tb|mb|mah)\b", re.I)
CAPACITY_RE = re.compile(r"\d+(?:[.,]\d+)?(?:gb|tb|mb|mah)", re.I)   # a variant-defining size: "4GB", "1.5TB"
_QTOK_RE = re.compile(r"[^\s,;:()\[\]{}\"'!?/|]+")


class GeizhalsError(RuntimeError):
    pass


@dataclass
class Hit:
    name: str
    url: str
    price: float | None = None
    image: str | None = None


@dataclass
class Product:
    name: str
    url: str
    price_from: float | None
    specs: list[tuple[str, str]] = field(default_factory=list)


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = html.unescape(t).replace("​", "")
    return re.sub(r"\s+", " ", t).strip()


def _eur(v: str) -> float | None:
    try:
        return float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_search(page: str) -> list[Hit]:
    hits = []
    for card in _CARD_RE.findall(page):
        href, name = _HREF_RE.search(card), _NAME_RE.search(card)
        if not href or not name:      # variant families (-v<id>) have no single spec sheet
            continue
        price, img = _PRICE_RE.search(card), _IMG_RE.search(card)
        hits.append(Hit(name=_text(name.group(1)), url=href.group(1),
                        price=_eur(price.group(1)) if price else None, image=img.group(1) if img else None))
    seen, out = set(), []
    for h in hits:
        if h.url not in seen:
            seen.add(h.url)
            out.append(h)
    return out


def parse_product(page: str, url: str) -> Product:
    h1 = _H1_RE.search(page)
    specs: list[tuple[str, str]] = []
    grid = _SPECS_RE.search(page)
    if grid:
        specs = [(_text(k), _text(v)) for k, v in _ITEM_RE.findall(grid.group(1)) if _text(k) and _text(v)]
    low = _LOW_RE.search(page)
    return Product(name=_text(h1.group(1)) if h1 else "", url=url,
                   price_from=float(low.group(1)) if low else None, specs=specs)


async def _get(url: str) -> str:
    headers = {"user-agent": get_settings().user_agent, "accept": "text/html", "accept-language": "de-AT,de;q=0.9"}
    if (tunnel := vpn.tunnel_for(vpn.GEIZHALS)) is not None:
        return await _get_vpn(tunnel, url, headers)
    if current_trial() is not None:   # a test of the VPN search: never from the home IP, even after the switch went off
        raise GeizhalsError(t("Geizhals: the VPN is off, the test sends nothing directly"))
    r = None
    try:
        async with _limiter:
            if vpn.enabled(vpn.GEIZHALS):     # switched on while it waited: through the VPN after all
                _limiter.refund()
            else:
                try:
                    async with _client(None) as c:
                        r = await c.get(url, headers=headers)
                except httpx.HTTPError as e:
                    raise GeizhalsError(t("Geizhals unreachable: {error}", error=e)) from e
    except Blocked as e:              # still pausing after a block: no request at all
        raise GeizhalsError(str(e)) from e
    if r is None:
        return await _get(url)
    if r.status_code in (403, 429):   # blocked: pause (60 s, doubling) instead of asking again every 2 s
        _limiter.penalize()
    elif r.status_code == 200:
        _limiter.reward()
    if r.status_code != 200:
        raise GeizhalsError(t("Geizhals answered HTTP {status}", status=r.status_code))
    return r.text


def _client(proxy: str | None) -> httpx.AsyncClient:
    """One request's client: direct, or through the VPN's local proxy (never the environment's proxy settings)."""
    if proxy is None:
        return httpx.AsyncClient(timeout=15, follow_redirects=True, transport=_transport)
    if _transport is not None:        # httpx would send through the proxy and quietly skip the mock
        raise GeizhalsError("a mock transport and a proxy at the same time")
    return httpx.AsyncClient(timeout=15, follow_redirects=True, proxy=proxy, trust_env=False)


class _AskAgain(GeizhalsError):
    """A refusal through the live server: asked again (through the same server until it counts, then the next)."""


async def _get_vpn(tunnel: vpn.Tunnel, url: str, headers: dict[str, str]) -> str:
    """Through Geizhals' VPN server; no route: GeizhalsError, never a direct request. A 403/429, Cloudflare's
    challenge or no answer is a refusal (Tunnel.refused()), except a challenge for this search only (the front page
    passes through the same server: the lookup fails, CHALLENGED_TTL_S long): asked again through the same server until the refusals
    in a row count (vpn.CONFIRM), then Geizhals moves to the other live server or waits for the search to find it
    another one. A refusal does not penalize the limiter, whose pause is for blocks of the home IP. Under a Trial
    (the VPN search) once, through its server only, and what it met is noted there."""
    last: GeizhalsError | None = None
    for _ in range(vpn.CONFIRM + 2):
        try:
            return await _get_vpn_once(tunnel, url, headers)
        except _AskAgain as e:
            last = e
    assert last is not None
    raise GeizhalsError(str(last))


async def _get_vpn_once(tunnel: vpn.Tunnel, url: str, headers: dict[str, str]) -> str:
    tr = current_trial()
    if tr is None and _challenged.get(url, 0) > time.time():
        raise GeizhalsError(_challenged_text())
    try:
        route = tr.route if tr is not None else await tunnel.ensure()
    except PlatformError as e:
        raise GeizhalsError(str(e)) from e
    error: httpx.HTTPError | None = None
    try:
        async with _limiter:
            # switched over (or the search's server stopped) while it waited: nothing goes out
            if not (tunnel.alive(route) if tr is not None else tunnel.current() == route):
                _limiter.refund()
                raise GeizhalsError(t("Geizhals: the VPN server changed before the request went out"))
            try:
                if tr is not None:
                    tr.wire += 1
                with tunnel.hold(route):
                    async with _client(route.proxy) as c:   # a test of the VPN search waits TRIAL_TIMEOUT_S at most
                        r = await c.get(url, headers=headers,
                                        timeout=TRIAL_TIMEOUT_S if tr is not None else httpx.USE_CLIENT_DEFAULT)
            except httpx.HTTPError as e:
                error = e
    except Blocked as e:              # still pausing after a block: no request at all
        raise GeizhalsError(str(e)) from e
    if error is not None:
        detail = str(error)[:160] or type(error).__name__
        if tr is not None:
            tr.sent, tr.refused, tr.detail = tr.sent + 1, "unreachable", detail
            raise GeizhalsError(t("Geizhals unreachable through the VPN: {error}", error=detail)) from error
        await tunnel.refused(route, "unreachable", detail)
        raise _AskAgain(t("Geizhals unreachable through the VPN: {error}", error=detail)) from error
    challenge = r.headers.get("cf-mitigated", "").lower() == "challenge"
    if tr is not None:
        tr.sent, tr.status = tr.sent + 1, r.status_code
        tr.ctype, tr.challenge = r.headers.get("content-type", "").lower(), challenge
    if r.status_code in (403, 429) or challenge:
        reason = "rate" if r.status_code == 429 else "blocked" if r.status_code == 403 else "botcheck"
        if tr is not None:
            tr.refused, tr.detail = reason, f"HTTP {r.status_code}"
            raise GeizhalsError(t("Geizhals refused the VPN server (HTTP {status})", status=r.status_code))
        if challenge and await _front_page_passes(tunnel, route, headers):
            # Cloudflare challenges some searches (model numbers, mostly) through every server while the others
            # pass: the search, not the server. Not asked again for a while, no strike against the server.
            _challenged[url] = time.time() + CHALLENGED_TTL_S
            log.info("Geizhals: a challenge for %s only, the front page passes through %s", url, route.name)
            raise GeizhalsError(_challenged_text())
        await tunnel.refused(route, reason, f"HTTP {r.status_code}")
        raise _AskAgain(t("Geizhals refused the VPN server (HTTP {status})", status=r.status_code))
    if tr is None:
        tunnel.answered(route)
    if r.status_code == 200:
        _limiter.reward()
    if r.status_code != 200:
        raise GeizhalsError(t("Geizhals answered HTTP {status}", status=r.status_code))
    return r.text


def _challenged_text() -> str:
    return t("Geizhals shows a browser check for this search (not because of the VPN server); it is tried again in a "
             "few hours")


async def _front_page_passes(tunnel: vpn.Tunnel, route: vpn.Route, headers: dict[str, str]) -> bool:
    """After a challenge: does Geizhals' front page answer through the same server? Then the search was challenged,
    not the server."""
    try:
        async with _limiter:
            if tunnel.current() != route:
                _limiter.refund()
                return False
            with tunnel.hold(route):
                async with _client(route.proxy) as c:
                    r = await c.get(f"{BASE}/", headers=headers)
    except (Blocked, httpx.HTTPError):
        return False
    return r.status_code == 200 and r.headers.get("cf-mitigated", "").lower() != "challenge"


_DIGITS_RE = re.compile(r"\d{4,}")


def search_term(query: str) -> str:
    """The search as sent: Cloudflare challenges every Geizhals search with four digits in a row ("RTX 4090",
    "Gira 211100"), through every server, so a long number goes in thousands ("RTX 4.090", which Geizhals reads as
    4090); one with a leading zero (no thousands) as its first three digits and a wildcard, the rest of that word
    dropped ("JAL-0810D.02" -> "JAL-081*")."""
    words = []
    for w in query.split():
        m = _DIGITS_RE.search(w)
        while m is not None:
            run = m.group()
            if run.startswith("0"):
                w = w[:m.start()] + run[:3] + "*"
                break
            dotted = f"{int(run):,}".replace(",", ".")
            w = w[:m.start()] + dotted + w[m.end():]
            m = _DIGITS_RE.search(w, m.start() + len(dotted))
        words.append(w)
    return " ".join(words)


STATS_KEY = "geizhals_cache_stats"   # setting: the cache's counts since `since` (cache_view), per day for STATS_DAYS
STATS_DAYS = 30


def _count(what: str) -> None:
    """One more lookup answered from the cache ("hits"), sent to Geizhals ("fetches") or answered by an old entry
    because Geizhals failed ("stale"); per day too."""
    import json
    try:
        st = json.loads(db.get_setting(STATS_KEY) or "{}") or {}
    except ValueError:
        st = {}
    st.setdefault("since", datetime.now(timezone.utc).isoformat())
    st[what] = int(st.get(what) or 0) + 1
    days = st.setdefault("days", {})
    day = days.setdefault(datetime.now(timezone.utc).date().isoformat(), {})
    day[what] = int(day.get(what) or 0) + 1
    st["days"] = dict(sorted(days.items())[-STATS_DAYS:])
    db.set_setting(STATS_KEY, json.dumps(st))


async def _cached(url: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """`fetch()`'s answer for `url` (JSON-able), from the local cache while it is younger than CACHE_DAYS; an older
    one is fetched again (and still used when that fails). Never under a Trial (the VPN search tests the server).
    Counted per entry (hits, fetches) and in all (_count)."""
    if current_trial() is not None:
        return await fetch()
    now = time.time()
    with db.session() as ses:
        row = ses.get(db.GeizhalsCache, url)
        if row is not None:
            row.used_at = now
            fresh = now - row.fetched_at < CACHE_DAYS * 86400
            if fresh:
                row.hits = (row.hits or 0) + 1
            ses.add(row)
            ses.commit()
            if fresh:
                _count("hits")
                return row.data
    try:
        data = await fetch()
    except GeizhalsError:
        if row is not None:
            log.info("Geizhals: %s could not be refreshed, the cached answer from %s stays", url,
                     time.strftime("%Y-%m-%d", time.localtime(row.fetched_at)))
            _count("stale")
            return row.data
        raise
    with db.session() as ses:
        old = ses.get(db.GeizhalsCache, url)
        ses.merge(db.GeizhalsCache(url=url, data=data, fetched_at=now, used_at=now, hits=(old.hits if old else 0),
                                   fetches=((old.fetches or 0) if old else 0) + 1))
        ses.commit()
    _count("fetches")
    return data


def prune_cache() -> int:
    """Delete the cached answers no lookup used for CACHE_DAYS; returns how many."""
    with db.session() as ses:
        n = ses.exec(delete(db.GeizhalsCache).where(
            col(db.GeizhalsCache.used_at) < time.time() - CACHE_DAYS * 86400)).rowcount
        ses.commit()
    return n or 0


def cache_view(limit: int = 300) -> dict[str, Any]:
    """Settings → Search & prices → Geizhals: the cached answers, the last used first: {count, rows: [{kind, label, href,
    result, fetched, used, stale}]}; `label`: the search term as sent, or the product's name."""
    from urllib.parse import parse_qs, urlparse

    with db.session() as ses:
        count = ses.exec(select(func.count()).select_from(db.GeizhalsCache)).one()
        found = ses.exec(select(db.GeizhalsCache).order_by(col(db.GeizhalsCache.used_at).desc()).limit(limit)).all()
    now, rows = time.time(), []
    for r in found:
        d = r.data
        if isinstance(d, list):          # a search: its hits
            top = d[0] if d else None
            rows.append({"kind": "search", "label": parse_qs(urlparse(r.url).query).get("fs", [""])[0], "href": r.url,
                         "result": t("{n} hits", n=len(d)) if d else t("nothing found"),
                         "top": top.get("name") if top else "", "price": top.get("price") if top else None})
        else:                            # a product page
            rows.append({"kind": "product", "label": d.get("name") or r.url, "href": r.url,
                         "result": t("{n} specs", n=len(d.get("specs") or [])), "top": "", "price": d.get("price_from")})
        rows[-1].update(fetched=datetime.fromtimestamp(r.fetched_at, timezone.utc),
                        used=datetime.fromtimestamp(r.used_at, timezone.utc), stale=now - r.fetched_at >= CACHE_DAYS * 86400,
                        hits=r.hits or 0, fetches=r.fetches or 0)
    return {"count": count, "rows": rows, "days": CACHE_DAYS, "more": max(0, count - len(rows)), "stats": cache_stats()}


def cache_stats() -> dict[str, Any]:
    """The cache page's figures: entries (searches, product pages, fresh, outdated), their size in bytes, the oldest
    fetch; since `since`: lookups answered from the cache (hits), requests sent (fetches), old entries used because
    Geizhals failed (stale), the hit rate; per day for the last 7 days (date, hits, fetches); the most used entries."""
    import json
    from sqlalchemy import case

    G = db.GeizhalsCache
    cut = time.time() - CACHE_DAYS * 86400
    with db.session() as ses:
        n, size, fresh, oldest = ses.exec(select(
            func.count(), func.coalesce(func.sum(func.length(G.data)), 0),
            func.coalesce(func.sum(case((col(G.fetched_at) >= cut, 1), else_=0)), 0), func.min(G.fetched_at))).one()
        searches = ses.exec(select(func.count()).select_from(G).where(col(G.url).contains("?fs="))).one()
        top = ses.exec(select(G).where(col(G.hits) > 0).order_by(col(G.hits).desc()).limit(5)).all()
    try:
        st = json.loads(db.get_setting(STATS_KEY) or "{}") or {}
    except ValueError:
        st = {}
    hits, fetches = int(st.get("hits") or 0), int(st.get("fetches") or 0)
    today = datetime.now(timezone.utc).date()
    week = []
    for back in range(6, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        d = (st.get("days") or {}).get(day) or {}
        week.append({"date": day, "hits": int(d.get("hits") or 0), "fetches": int(d.get("fetches") or 0)})
    return {"entries": n, "searches": searches, "products": n - searches, "size": int(size), "fresh": int(fresh),
            "stale_entries": n - int(fresh),
            "oldest": datetime.fromtimestamp(oldest, timezone.utc) if oldest else None,
            "since": datetime.fromisoformat(st["since"]) if st.get("since") else None,
            "hits": hits, "fetches": fetches, "stale": int(st.get("stale") or 0),
            "rate": round(hits * 100 / (hits + fetches)) if hits + fetches else None,
            "week": week, "week_max": max([d["hits"] + d["fetches"] for d in week] + [1]),
            "top": [{"label": parse_qs_label(r.url) if isinstance(r.data, list) else (r.data or {}).get("name") or r.url,
                     "href": r.url, "hits": r.hits or 0, "kind": "search" if isinstance(r.data, list) else "product"}
                    for r in top]}


def parse_qs_label(url: str) -> str:
    """The search term of a cached search URL (?fs=…)."""
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(url).query).get("fs", [""])[0]


def clear_cache() -> int:
    """Delete every cached answer ("Clear cache"); returns how many."""
    with db.session() as ses:
        n = ses.exec(delete(db.GeizhalsCache)).rowcount
        ses.commit()
    return n or 0


async def prune_job() -> None:
    if n := prune_cache():
        log.info("Geizhals: %d cached answers unused for %d days deleted", n, CACHE_DAYS)


async def search(query: str, limit: int = 5) -> list[Hit]:
    url = f"{BASE}/?{urlencode({'fs': search_term(query), 'hloc': 'at'})}"

    async def fetch() -> list[dict[str, Any]]:
        return [asdict(h) for h in parse_search(await _get(url))]

    return [Hit(**h) for h in await _cached(url, fetch)][:limit]


async def product(url: str) -> Product:
    if not re.fullmatch(r"https://geizhals\.at/[^\s\"<>]+-a\d+\.html", url):
        raise GeizhalsError(t("Not a Geizhals product page"))

    async def fetch() -> dict[str, Any]:
        return asdict(parse_product(await _get(url), url))

    d = await _cached(url, fetch)
    return Product(name=d["name"], url=d["url"], price_from=d["price_from"],
                   specs=[(k, v) for k, v in d.get("specs") or []])


_FR_UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(Go|To)\b")     # French "512 Go", "1 To" (case kept: "to" is a word)


def glue_units(s: str) -> str:
    """Number and capacity unit as one word, case kept: "4 GB" -> "4GB", "1,5 TB" -> "1.5TB"; French "512 Go" ->
    "512GB"."""
    s = _FR_UNIT_RE.sub(lambda m: m.group(1) + {"Go": "GB", "To": "TB"}[m.group(2)], s or "")
    return _UNIT_RE.sub(lambda m: m.group(1).replace(",", ".") + m.group(2), s)


_WATT_RE = re.compile(r"(\d)\s*w\b", re.I)
_SLUG_RE = re.compile(r"https://geizhals\.at/(.+?)(?:-a\d+)?\.html")
# words that make another product of the same line: "iPhone 16" is not the "16 Pro Max", "Switch 8" not "Enterprise 8"
VARIANT_WORDS = {"max", "plus", "mini", "ultra", "lite", "pro", "se", "air", "fe", "neo", "enterprise", "flex", "xg",
                 "compute", "mesh", "lr",
                 "xt", "xtx", "ti", "super"}       # graphics cards: "RX 6600" is not the dearer "RX 6600 XT"
# brands and lines a seller or Geizhals may leave out ("UniFi UNAS 2" is "Ubiquiti UNAS 2"; "U6 Mesh" sells without
# "Ubiquiti UniFi"): a match need not have them, and the price check's comparable search drops them
BRANDS = frozenset({"ubiquiti", "unifi", "apple", "amazon", "samsung", "sony", "google", "microsoft"})
# a result with one of these the query lacks is something else: an accessory, a multi-pack or a bundle
OTHER_THING_WORDS = {"kit", "halterung", "mount", "hülle", "case", "cover", "adapter", "kabel", "netzteil", "upgrade",
                     "ersatzteil", "ersatzteile", "zubehör", "pack", "bundle", "bundles"}


# words that name a kind of thing or its interface, not a product: shared by any two SSDs – and by any two mini PCs
# (10-30: "Captiva Mini PC Business/Office …" passed for a "Mini pc Nigopi Ryzen 7" ad on its word "mini")
GENERIC_WORDS = frozenset({"ssd", "nvme", "hdd", "pcie", "sata", "usb", "m2", "m.2", "intern", "extern", "internal",
                           "external", "gen3", "gen4", "gen5", "neu", "new",
                           "mini", "pc", "pcs", "computer", "desktop", "laptop", "notebook", "tablet", "business",
                           "office", "gaming", "home", "smart", "set", "kit", "bundle", "system"})


def _gb(cap: str) -> float:
    v, unit = float(re.match(r"[\d.]+", cap.replace(",", ".")).group()), cap[-2:].lower()
    return {"tb": v * 1000, "gb": v, "mb": v / 1000}.get(unit, -1)


def title_conflict(name: str, title: str) -> str | None:
    """Why a product found by an automatic lookup cannot be the ad's (its full title), or None: storage sizes that
    differ ("1TB" for a "512 Go" ad) or none of the product's brand and model words in the title (the first two
    words that are no GENERIC_WORDS, and those mixing letters and digits: "Kingston NV3 NVMe …" for a "Western
    Digital … 512 Go" ad, found by a query that kept only "SSD NVMe M.2")."""
    t_caps = {_gb(c) for c in CAPACITY_RE.findall(glue_units(title)) if not c.lower().endswith("mah")}
    n_caps = {_gb(c) for c in CAPACITY_RE.findall(glue_units(name)) if not c.lower().endswith("mah")}
    if t_caps and n_caps and not any(abs(a - b) <= 0.03 * max(a, b) for a in t_caps for b in n_caps):
        return "capacity"
    flat = _flat(glue_units(title))
    words = [w for w in (_flat(x) for x in _QTOK_RE.findall(glue_units(name)))
             if re.search(r"[a-zäöüß]", w) and w not in GENERIC_WORDS and not CAPACITY_RE.fullmatch(w)]
    key = words[:2] + [w for w in words if re.search(r"\d", w)]
    if key and not any(w in flat if len(w) >= 3 else re.search(rf"(?<![a-z0-9]){w}(?![a-z0-9])", glue_units(title).lower())
                       for w in key):
        return "product"
    return None


def _norm(s: str) -> str:
    return _WATT_RE.sub(r"\1w", glue_units(s)).lower()


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())


def _parts(s: str) -> list[str]:
    return [x for x in re.split(r"[^a-z0-9äöüß]+", (s or "").lower()) if x]


def _has_run(parts: list[str], within: list[str]) -> bool:
    """The code's parts appear one after another: "USW-24-POE" in "…-switch-usw-24-poe", not "18V-55" in "18V-550"."""
    k = len(parts)
    return any(within[i:i + k] == parts for i in range(len(within) - k + 1))


def _has_word(name: str, w: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(w)}(?!\w)", name) is not None


def is_code(token: str) -> bool:
    """An order code such as "USW-24-POE", "UAP-AC-PRO", "BE-02001.02" or "UXG-Lite": parts joined by "-", each in
    capitals or digits (or a variant word), at least five letters/digits; not "RJ-45", "Wi-Fi" or "WLAN-Repeater"."""
    parts = token.split("-")
    if len(parts) < 2 or len(_flat(token)) < 5 or not all(parts):
        return False
    return all(re.fullmatch(r"[A-Z0-9.]+", p) or p.lower() in VARIANT_WORDS for p in parts) \
        and any(re.fullmatch(r"[A-Z]{2,}|[0-9.]+[A-Z]*", p) for p in parts)


def strong_code(token: str) -> bool:
    """A code that names one product ("USW-24-POE", "BE-02001.02"), worth a search of its own – unlike "18V-55"."""
    return is_code(token) and (token.count("-") >= 2 or len(_flat(token)) >= 7)


def _tokens(query: str) -> list[str]:
    return [w for w in (x.strip(".-") for x in _QTOK_RE.findall(glue_units(query))) if w]


def _similar(w: str, words: set[str]) -> bool:
    """Typos in longer words: "rasperry" ≈ "raspberry"."""
    return len(w) >= 5 and any(abs(len(x) - len(w)) <= 2 and SequenceMatcher(None, w, x).ratio() >= 0.85 for x in words)


_ORDINAL_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th|ª|º)")                          # "3rd", "5ª": the generation number
_GLUED_RE = re.compile(r"([a-zäöü]+)(\d+)(\+?)")                                   # "Pi4", "Show8", "U6", "U6+"


def _model_found(w: str, n: str, parts: list[str]) -> bool:
    """A query word with a digit in the (normalised) name: as a whole word ("13" is not "113" or "230cm"), a bare
    number from 5 on also as a port count ("24" in "24x RJ-45"), glued or abbreviated to its line: "Pi4" is "Pi 4",
    "Show8" "Show 8", "U6" "UniFi 6" – and "U6+" only "UniFi 6+", "U6" not."""
    if _has_word(n, w) or w.isdigit() and int(w) >= 5 and _has_word(n, w + "x"):
        return True
    m = _GLUED_RE.fullmatch(w)
    if not m:
        return False
    letters, digits, plus = m.groups()
    for p, nxt in zip(parts, parts[1:], strict=False):
        if nxt == digits and (p == letters or len(letters) <= 2 and p.startswith(letters)):
            return bool(plus) == bool(re.search(rf"(?<!\w){re.escape(p)}\s*{digits}\+", n))
    return False


def matches(name: str, query: str, url: str = "") -> bool:
    """Is this product really the one asked for (the variant too)? Every word with a digit – also a bare "4" as in
    "Pi 4", and capacities like "4GB" – appears in the name (_model_found: a whole word, "13" is not "113" or "230cm",
    "4" not "400" or "4GB"; glued or abbreviated, "Pi4", "U6" for "UniFi 6"; "3rd" as "3"); an order code in the name
    or the product link; most other words of three letters or more appear as words (hyphens, compounds and small typos
    forgiven; "Lot" is not in "Merlotglas"; a brand of BRANDS the name lacks does not count), all of them when the
    query has no number or code ("Ubiquiti UniFi Cloud Gateway Max"); and query and name have
    the same variant words (Pro, Max, Lite …), the name no pack or accessory word the query lacks. "4 GB" equals "4GB",
    "150 W" "150W"; a capacity only in the name is fine."""
    n = _norm(name)
    slug = _SLUG_RE.match(url or "")
    flat = _flat(name)
    raw = _tokens(query)
    if not raw:
        return False
    codes = [w for w in raw if is_code(w)]
    toks = [_ORDINAL_RE.sub(r"\1", _norm(w)) if _ORDINAL_RE.fullmatch(_norm(w)) else _norm(w) for w in raw if not is_code(w)]
    words = [w for w in toks if not re.search(r"\d", w) and len(w) >= 3]     # not "de", "of": they are in any name
    model = [w + "+" if "plus" in words and _GLUED_RE.fullmatch(w + "+") else w     # "U6 Plus" is the "U6+"
             for w in toks if re.search(r"\d", w)]
    where = (_parts(name), _parts(slug.group(1) if slug else ""))
    if not all(any(_has_run(_parts(c), w) for w in where) for c in codes) \
            or not all(_model_found(w, n, where[0]) for w in model):
        return False
    asked = {x for w in toks + codes for x in _parts(w)} | {m.group(1) for w in model if (m := _GLUED_RE.fullmatch(w))}
    name_words = set(re.findall(r"[a-z0-9äöüß]+", n)) | ({"plus"} if re.search(r"\d\+(?![\w+])", n) else set())
    asked |= {"plus"} if any(w.endswith("+") for w in model) else set()       # "U6+" asks for the "UniFi 6+"
    if (name_words & (VARIANT_WORDS | OTHER_THING_WORDS)) - asked or {w for w in words if w in VARIANT_WORDS} - name_words:
        return False

    def found(w: str) -> bool:      # a whole word, part of a compound ("UniFi" in "UniFiSwitch", "i-phone"), a typo
        return w in name_words or len(_flat(w)) >= 4 and _flat(w) in flat or _similar(w, name_words)

    joined = {x for a, b in zip(words, words[1:], strict=False) if found(a + b) for x in (a, b)}   # "ESY LUX": "Esylux"
    counted = [w for w in words if w not in BRANDS or found(w)]       # a brand Geizhals leaves out does not count
    need = 0.6 if model or codes else 1.0       # no number or code to go by: every word ("Dream Machine UDM" is no
    return not counted or sum(w in joined or found(w) for w in counted) / len(counted) >= need    # "… Beast")


def best_hit(hits: list[Hit], query: str) -> Hit | None:
    """The first hit (Geizhals' order) that is really this product and variant."""
    return next((h for h in hits if matches(h.name, query, h.url)), None)


def search_queries(query: str) -> list[str]:
    """What to search for, most promising first, at most three: an order code that names one product alone (Geizhals
    finds "USW-24-POE" but often nothing for the code among other words), the query, then its key words – the first
    two, variant words and numbers ("Ubiquiti UniFi Pro PoE 48" -> "Ubiquiti UniFi Pro 48"); lookup() searches the
    key words only when nothing was found at all."""
    raw = _tokens(query)
    codes = [w for w in raw if strong_code(w)]
    key = raw[:2] + [w for w in raw[2:] if not is_code(w) and (re.search(r"\d", w) or w.lower() in VARIANT_WORDS)]
    out = codes[:1] + [query] + ([" ".join(key)] if len(key) >= 2 else [])
    return list(dict.fromkeys(q for q in out if q))[:3]


async def lookup(query: str, page: bool = True) -> dict[str, Any] | None:
    """Best match for a product name, as stored on the draft; None if nothing usable was found. `verified`: the
    product (search card and page title) matches the query, variant included; False = Geizhals' first hit as a guess.
    Searches again (search_queries) while nothing found fits; a failing search raises GeizhalsError as before.
    page=False: no product page (no spec sheet; the card's price is the lowest offer too) – one request less for a
    mere price lookup."""
    query = query.strip()
    if not query:
        return None
    hits: list[Hit] = []
    best = None
    queries = search_queries(query)
    for i, q in enumerate(queries):
        if i == 2 and hits:          # the key words only when the code and the query found nothing at all
            break
        hits += [h for h in await search(q, limit=12) if h.url not in {x.url for x in hits}]
        best = best_hit(hits, query)
        if best is not None:
            break
    if not hits:
        return {"query": query, "match": None, "candidates": [], "verified": False}
    if not page:
        top = best or hits[0]
        return {"query": query, "match": {"name": top.name, "url": top.url, "price_from": top.price, "specs": []},
                "candidates": [asdict(h) for h in hits[:12]], "verified": best is not None}
    prod = await product((best or hits[0]).url)
    verified = best is not None and (not prod.name or matches(prod.name, query, prod.url))   # the h1 can be more precise
    return {"query": query, "match": asdict(prod), "candidates": [asdict(h) for h in hits[:12]], "verified": verified}


_SPEC_HEAD = re.compile(r"^Technische Daten \((?P<name>.+), laut Hersteller/Geizhals\):[ \t]*$", re.M)


def find_spec_block(text: str | None) -> tuple[int, int, str] | None:
    """The spec sheet (spec_block) in a description: (start, end, product name), None without one. Its lines may come
    back from willhaben as "• " list items."""
    m = _SPEC_HEAD.search(text or "")
    if m is None:
        return None
    end = m.end()
    for line in re.finditer(r"\n([^\n]*)", text[m.end():]):
        if not line.group(1).lstrip().startswith(("- ", "• ")):
            break
        end = m.end() + line.end()
    return m.start(), end, m.group("name")


def with_spec_block(text: str | None, block: str) -> str:
    """The description with its spec sheet replaced by `block` (appended when it had none; "" removes it)."""
    text = text or ""
    if (found := find_spec_block(text)) is not None:
        start, end, _ = found
        text = (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip()
    text = text.rstrip()
    return f"{text}\n\n{block}" if block else text


def spec_block(match: dict[str, Any] | None) -> str:
    """Text appended to the description when the user ticks "add the spec sheet" (ad content: stays German)."""
    if not match or not match.get("specs"):
        return ""
    lines = [f"Technische Daten ({match['name']}, laut Hersteller/Geizhals):"]
    lines += [f"- {k}: {v}" for k, v in match["specs"]]
    return "\n".join(lines)
