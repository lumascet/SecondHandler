"""shpock.com (Austria, Germany) as a platform adapter, through the GraphQL API its own website uses.

The website's result list (www.shpock.com/de-at/results?q=…) asks www.shpock.com/graphql for `itemSearch`. The
Apollo server answers GET requests too when the client sends `apollo-require-preflight`, so a search is one plain
GET that the cache, the access log and the offline replay handle like every other request. A listing comes with
title, description, price (and the price before a reduction), place and photos, but without a date. An ad's own
page (/de-at/i/<id>) carries the whole item in __NEXT_DATA__ (Apollo state): posting time, condition, brand, the
seller and whether they sell professionally. No login, no app credentials.

Ad ids are not numbers ("asu6-t2hxq7bDRFz"); refs are "shpock:<id>". Filters travel as a JSON string
(`serializedFilters`): {"q": …, "price": {"from": …, "to": …}, "sort": {"type": "date_listed", "direction": "d"}}.
The website copies unknown link parameters into that object, so the canonical link carries the price as
priceFrom/priceTo, which this adapter reads back (the site itself ignores them).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from app.i18n import t
from app.platforms import condition
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import clean_text, euro, from_epoch, one_line, to_float
from app.platforms.types import AdDetail, Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.shpock.com"
LOCALE = "de-at"
GRAPHQL = f"{WEB}/graphql"
IMAGE = "https://m1.secondhandapp.at/2.0/{id}"
MAX_ROWS = 40
SORTS = {"newest": {"type": "date_listed", "direction": "d"}, "relevance": {"type": "relevance", "direction": "d"},
         "price_asc": {"type": "price", "direction": "a"}, "price_desc": {"type": "price", "direction": "d"}}
# the fields of the website's own ItemSearch query (summaryFragment) this adapter reads
SEARCH_QUERY = (
    "query ItemSearch($serializedFilters: String, $pagination: Pagination, $trackingSource: TrackingSource!) {"
    " itemSearch(serializedFilters: $serializedFilters, pagination: $pagination, trackingSource: $trackingSource) {"
    " __typename od offset limit count total"
    " itemResults { distanceGroup items { __typename ... on ItemSummary { id title description path locality price"
    " originalPrice currency isNew isSold isFree isBoosted isShippable isExpired canonicalURL media { id } } } } } }")
GRAPHQL_HEADERS = {"Accept": "application/graphql-response+json,application/json;q=0.9", "Accept-Language": "de-AT",
                   "apollo-require-preflight": "true", "Referer": f"{WEB}/{LOCALE}/results"}
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-AT"}
_ID = re.compile(r"[A-Za-z0-9_-]{12,24}")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


# ------------------------------------------------------------------ parsing
def _price(item: dict[str, Any]) -> tuple[float | None, str | None]:
    if item.get("isFree"):
        return 0.0, "Zu verschenken"
    price = to_float(item.get("price"))
    return price, euro(price) if price is not None else None


def _postcode(locality: str | None) -> str | None:
    m = re.match(r"(\d{4,5})\b", locality or "")
    return m.group(1) if m else None


def parse_listing(item: dict[str, Any]) -> Listing:
    price, display = _price(item)
    before = to_float(item.get("originalPrice"))
    locality = one_line(item.get("locality"))
    media = [m.get("id") for m in item.get("media") or [] if isinstance(m, dict) and m.get("id")]
    body = clean_text(item.get("description"))
    return Listing(ad_id=str(item.get("id") or ""), heading=one_line(item.get("title")) or "",
                   body=body[:400] if body else None, price=price, price_display=display,
                   old_price=before if before and price is not None and before > price else None,
                   location=locality, postcode=_postcode(locality), state=None, published_at=None, changed_at=None,
                   end_date=None, url=item.get("canonicalURL") or f"{WEB}{item.get('path') or ''}",
                   image_url=IMAGE.format(id=media[0]) if media else None, is_private=None, is_bumped=None,
                   status="active", platform="shpock",
                   shipping=None if item.get("isShippable") is None else ("ship" if item["isShippable"] else "pickup"))


# Shpock's bot trap (since 2026-10-17): a search answered with generated ads, lorem-ipsum titles and places, ids
# "AHfutAAAAAAAAAAA" (a small number padded with A) and photo ids padded with zeros that do not exist (404)
_DECOY_ID = re.compile(r"[A-Za-z0-9_-]{4,8}A{8,}")
_DECOY_MEDIA = re.compile(r"[0-9a-f]{8}0{16}")


def is_decoy(data: Any) -> bool:
    """A search answer of made-up ads: at least half of its items carry a padded id or a padded photo id. A real
    answer's ids are random ("atFLVeGnI_SaCFKQ")."""
    res = ((data.get("data") or {}).get("itemSearch") or {}) if isinstance(data, dict) else {}
    items = [i for g in res.get("itemResults") or [] for i in (g or {}).get("items") or [] if isinstance(i, dict)]

    def fake(i: dict[str, Any]) -> bool:
        media = [m.get("id") for m in i.get("media") or [] if isinstance(m, dict)]
        return bool(_DECOY_ID.fullmatch(str(i.get("id") or ""))
                    or any(_DECOY_MEDIA.fullmatch(str(m or "")) for m in media))

    return bool(items) and sum(map(fake, items)) * 2 >= len(items)


def _real_answer(r: Any) -> bool:
    """`valid` of a search: False for the bot trap's made-up ads (the VPN takes it as a bot check)."""
    try:
        return not is_decoy(r.json())
    except ValueError:
        return True             # no JSON: search_items says so


def parse_search(data: dict[str, Any]) -> SearchPage:
    """The GraphQL answer -> SearchPage; sold and expired items are left out."""
    if not isinstance(data, dict):
        raise PlatformError(t("Shpock search returned no JSON"))
    res = (data.get("data") or {}).get("itemSearch")
    if res is None:
        msg = "; ".join(str(e.get("message")) for e in data.get("errors") or [] if isinstance(e, dict)) or "?"
        raise PlatformError(t("Shpock search failed: {error}", error=msg))
    ads, seen = [], set()
    for group in res.get("itemResults") or []:
        for item in (group or {}).get("items") or []:
            if not isinstance(item, dict) or item.get("__typename") != "ItemSummary" or not item.get("id"):
                continue
            if item.get("isSold") or item.get("isExpired") or item["id"] in seen:
                continue
            seen.add(item["id"])
            ads.append(parse_listing(item))
    total = res.get("total")
    return SearchPage(rows_found=int(total) if isinstance(total, int) else len(ads), ads=ads)


def _apollo(html: str) -> dict[str, Any]:
    m = _NEXT_DATA.search(html or "")
    if not m:
        return {}
    try:
        nd = json.loads(m.group(1))
    except ValueError:
        return {}
    props = nd.get("props") or {}
    return (props.get("pageProps") or {}).get("apolloState") or props.get("apolloState") or {}


def parse_item_page(html: str, ad_id: str) -> AdDetail | None:
    """An ad's page -> AdDetail from its Apollo state; None when the page has no such item (deleted ads)."""
    st = _apollo(html)
    item = st.get(f"Item:{ad_id}")
    if not isinstance(item, dict):
        return None
    ref = lambda r: st.get(r.get("__ref")) if isinstance(r, dict) and r.get("__ref") else r   # noqa: E731
    media = [ref(m) for m in item.get("media") or []]
    images = [IMAGE.format(id=m["id"]) for m in media if isinstance(m, dict) and m.get("id")]
    seller = ref(item.get("user")) or {}
    price, display = _price(item)
    extras = [f"{e.get('label')}: {e.get('valueLabel')}" for e in item.get("extraProperties") or []
              if isinstance(e, dict) and e.get("label") and e.get("valueLabel")]
    description = clean_text(item.get("description")) or ""
    if extras:
        description = (description + "\n\n" if description else "") + " · ".join(extras)
    keywords = item.get("adKeywords") or []
    seller_type = next((k.split(":", 1)[1] for k in keywords if isinstance(k, str) and k.startswith("seller_type:")), None)
    is_private = (not seller.get("isProSeller")) if isinstance(seller, dict) and "isProSeller" in seller else None
    if seller_type in ("private", "professional", "business"):
        is_private = seller_type == "private"
    status = "sold" if item.get("isSold") else ("expired" if item.get("isExpired") else "active")
    posted = from_epoch(item.get("startDate"), ms=True)
    locality = one_line(item.get("locality"))
    item_condition, item_condition_label = condition.shpock(item.get("extraProperties"))   # the list's isNew: not read
    return AdDetail(ad_id=ad_id, heading=one_line(item.get("title")) or "", description=description or None,
                    price=price, price_display=display, status=status, published_at=posted, first_published_at=posted,
                    end_date=from_epoch(item.get("expirationDate"), ms=True),
                    url=item.get("canonicalURL") or f"{WEB}{item.get('path') or f'/{LOCALE}/i/{ad_id}'}",
                    images=images, location=locality, place=f"{locality}, Österreich" if locality else None,
                    seller_name=one_line(seller.get("name")) if isinstance(seller, dict) else None,
                    is_private=is_private, platform="shpock", item_condition=item_condition,
                    item_condition_label=item_condition_label)


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/graphql") or "/results" in path:
        return "search"
    if "/i/" in path:
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def shpock_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("shpock"), platform_per_hour("shpock"), name="Shpock", persist="shpock")
    return _limiter


class ShpockClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("shpock", shpock_limiter(), name="Shpock", headers={"Accept-Language": "de-AT"},
                         transport=transport, backend="curl")

    def classify(self, url: str) -> str:
        return classify(url)

    async def search_items(self, filters: dict[str, Any], limit: int, *, cache_s: float = 0,
                           offset: int = 0) -> SearchPage:
        """One page of the item search; `offset` > 0: the ads after the first `offset` (the answer states its offset,
        limit and total)."""
        pagination: dict[str, int] = {"limit": limit, **({"offset": offset} if offset > 0 else {})}
        variables = {"serializedFilters": json.dumps(filters, separators=(",", ":"), ensure_ascii=False),
                     "pagination": pagination, "trackingSource": "Search"}
        r = await self._get(GRAPHQL, params={"operationName": "ItemSearch", "query": SEARCH_QUERY,
                                             "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False)},
                            headers=GRAPHQL_HEADERS, cache_s=cache_s, kind="search", valid=_real_answer)
        if r.status_code != 200:
            raise PlatformError(t("Shpock search returned HTTP {status}", status=r.status_code))
        try:
            data = r.json()
        except ValueError as e:
            raise PlatformError(t("Shpock search returned no JSON")) from e
        if is_decoy(data):      # never a hit, never a Claude call: the made-up ads of its bot trap
            raise PlatformError(t("Shpock answered with made-up ads (its bot trap); nothing was taken over"))
        return parse_search(data)

    async def item(self, ad_id: str) -> AdDetail | None:
        r = await self._get(f"{WEB}/{LOCALE}/i/{ad_id}", headers=PAGE_HEADERS, cache_s=300, kind="detail")
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise PlatformError(t("Shpock ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        d = parse_item_page(r.text, ad_id)
        if d is None and not _apollo(r.text):   # no page state at all: a changed page or an interstitial, not a
            raise PlatformError(t("Shpock ad {id}: the page could not be read", id=ad_id))   # deleted ad
        return d


_client: ShpockClient | None = None


def get_client() -> ShpockClient:
    global _client
    if _client is None:
        _client = ShpockClient()
    return _client


def set_client(client: ShpockClient | None) -> None:
    """Tests: a client with a fake transport."""
    global _client
    _client = client


# ------------------------------------------------------------------ the adapter
def _num(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        raise InvalidSearchUrl(t("Price in the link is not a number")) from None


class ShpockPlatform(Platform):
    id = "shpock"
    name = "Shpock"
    host = "shpock.com"
    api_host = "www.shpock.com"
    home_country = "AT"
    hosts = ("shpock.com",)
    image_hosts = ("secondhandapp.at",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})
    PAGE_SIZE_MAX = MAX_ROWS
    CAN_PAGE = True
    access = "http"

    @property
    def limiter(self) -> RateLimiter:
        return shpock_limiter()

    @property
    def client(self) -> ShpockClient:
        return get_client()

    def about(self) -> str:
        return t("Searches through the GraphQL API of the Shpock website. Listings come without a date; opening an ad "
                 "loads its page with the posting time, condition and seller.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a shpock.com link"))
        if "/i/" in u.path:
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        if not re.fullmatch(r"(?:/[a-z]{2}-[a-z]{2})?/results/?", u.path):
            raise InvalidSearchUrl(t("Not a Shpock result list (copy the link of a search: …/results?q=…)"))
        qs = parse_qs(u.query)
        keyword = one_line((qs.get("q") or [""])[0])
        if not keyword:
            raise InvalidSearchUrl(t("The Shpock link has no search term (q=…)"))
        return self.keyword_search(keyword, _num((qs.get("priceFrom") or [None])[0]), _num((qs.get("priceTo") or [None])[0]))

    def _url(self, spec: SearchSpec) -> str:
        params = [("q", spec.keyword or "")]
        if spec.price_from is not None:
            params.append(("priceFrom", f"{spec.price_from:g}"))
        if spec.price_to is not None:
            params.append(("priceTo", f"{spec.price_to:g}"))
        return f"{WEB}/{LOCALE}/results?{urlencode(params)}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          sort=sort)
        spec.url = self._url(spec)
        return spec

    def filters_of(self, spec: SearchSpec) -> dict[str, Any]:
        f: dict[str, Any] = {"q": spec.keyword or "", "sort": SORTS.get(spec.sort, SORTS["newest"])}
        if spec.price_from is not None or spec.price_to is not None:
            f["price"] = {k: v for k, v in (("from", spec.price_from), ("to", spec.price_to)) if v is not None}
        return f

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        from app.config import get_settings

        limit = min(rows or get_settings().search_rows, MAX_ROWS)
        result = await self.client.search_items(self.filters_of(spec), limit, cache_s=cache_s,
                                                offset=(max(page, 1) - 1) * limit)
        result.ads = result.ads[:limit]
        result.title = spec.keyword
        return result

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        return await self.client.item(ad_id)

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/{LOCALE}/i/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/i/([A-Za-z0-9_-]{12,24})(?:[/?#]|$)", url)
        return m.group(1) if m else None

    def extract_ad_id(self, text: str) -> str | None:
        text = (text or "").strip()
        if _ID.fullmatch(text) and not text.isdigit():
            return text
        return self.parse_ad_url(text) if self.matches(text) else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["ShpockClient", "ShpockPlatform", "get_client", "set_client", "parse_search", "parse_listing",
           "parse_item_page"]
