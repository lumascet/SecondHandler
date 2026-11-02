"""Facebook Marketplace as a platform adapter, through the public (logged-out) website. Never a Facebook account.

Facebook has no public API for searching Marketplace. The logged-out search page
`www.facebook.com/marketplace/<location>/search?query=…` embeds its first ~24 listings as Relay JSON in
`<script type="application/json">` blocks (`marketplace_search.feed_units.edges[].node.listing`): id, title, price
(and the price before a reduction), city, first photo, creation time. Further pages need a login (the GraphQL
pagination answers with empty edges when logged out). A listing page `/marketplace/item/<id>/` carries the whole ad
under `marketplace_product_details_page.target`: description, condition, all photos, category path, seller.

Facebook keeps every page's connection open for ~20 s after the page to stream deferred parts, so FacebookTransport
reads a page only until the data it needs has arrived (2–3 s). The location is a slug (`vienna`) or a numeric id
(`100232032143091`); an unknown one silently lands on `/marketplace/category/search` with Facebook's default place
(San Francisco) and no listings, which is an error here. Logged out, Facebook mixes in a few listings from outside
the chosen radius, and the result list carries no coordinates to filter them out.

Search links carry the location; keyword searches (product search) use the one set in Settings → Facebook
Marketplace (setting `facebook_location`). Facebook's terms do not allow automated access; keep the pacing generous.
Ad ids are numbers; refs are "facebook:<id>". Verified against live pages on 2026-10-15.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from app.i18n import t
from app.platforms import condition
from app.platforms.base import Blocked, InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import TIMEOUT_S, FetchResponse, HttpPlatformClient, Transport, TransportError
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import clean_text, euro, from_epoch, one_line, to_float
from app.platforms.types import AdDetail, Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.facebook.com"
LOCATION_KEY = "facebook_location"      # {"loc", "radius", "place", "link"} for keyword searches
READ_STALL_S = 8.0                      # no new bytes for this long: take what arrived (the tail is deferred parts)
NAV_HEADERS = {                         # a browser opening the page; without the Sec-Fetch-* headers: HTTP 400
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.5",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
SEARCH_MARKERS = (b'"feed_units"',)
ITEM_MARKERS = (b'"redacted_description"', b'"listing_photos"', b'"seo_virtual_category"')
SORTS = {"newest": "creation_time_descend", "price_asc": "price_ascend", "price_desc": "price_descend"}
_SEARCH_PATH = re.compile(r"^/marketplace/(?P<loc>[^/]+)/search/?$")
_ANY_LOC = re.compile(r"^/marketplace/(?P<loc>[^/]+)(?:/|$)")
_NOT_LOCATIONS = {"category", "item", "search", "you", "create", "inbox", "notifications", "buying", "selling",
                  "saved", "profile", "categories", "groups", "stores", "vehicles", "propertyrentals"}
_ITEM_LINK = re.compile(r"/marketplace/item/(\d+)")
_JSON_BLOCK = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


# ------------------------------------------------------------------ parsing
def _dicts(o: Any) -> Iterator[dict]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _dicts(v)


def _blocks(html: str, *markers: str) -> Iterator[Any]:
    for block in _JSON_BLOCK.findall(html or ""):
        if any(m in block for m in markers):
            try:
                yield json.loads(block)
            except ValueError:
                continue


def _amount(price: Any) -> float | None:
    return to_float((price or {}).get("amount")) if isinstance(price, dict) else None


def _display(price: dict[str, Any], amount: float | None) -> str | None:
    text = price.get("formatted_amount_zeros_stripped") or price.get("formatted_amount")
    if text:
        return " ".join(str(text).replace(" ", " ").split())
    return euro(amount) if amount is not None else None


def parse_listing(li: dict[str, Any]) -> Listing | None:
    """One `listing` of the result list; None for hidden or sold ones."""
    lid = str(li.get("id") or "")
    if not lid.isdigit() or li.get("is_hidden") or li.get("is_sold"):
        return None
    price = li.get("listing_price") or {}
    amount = _amount(price)
    before = _amount(li.get("strikethrough_price"))
    geo = ((li.get("location") or {}).get("reverse_geocode")) or {}
    photo = ((li.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
    return Listing(ad_id=lid, heading=one_line(li.get("marketplace_listing_title")) or lid, body=None,
                   price=amount, price_display=_display(price, amount),
                   old_price=before if before and amount is not None and before > amount else None,
                   location=one_line(geo.get("city")), postcode=None, state=geo.get("state"),
                   published_at=from_epoch(li.get("creation_time")), changed_at=None, end_date=None,
                   url=f"{WEB}/marketplace/item/{lid}/", image_url=photo, is_private=None, is_bumped=None,
                   status="reserved" if li.get("is_pending") else "active", platform="facebook",
                   shipping=_delivery(li.get("delivery_types")))


def _delivery(types: Any) -> str | None:
    """Facebook's delivery types: only IN_PERSON is pickup only; any shipping type, shipping (no price in the list)."""
    if not isinstance(types, list) or not types:
        return None
    return "ship" if any(x != "IN_PERSON" for x in types) else "pickup"


def parse_search(html: str) -> SearchPage:
    """A search page -> SearchPage (title: the place Facebook searched around). A page without any search result
    (login wall, changed page) raises PlatformError: that must not read as "no hits" to a search agent."""
    found, place, ads = False, None, {}
    for data in _blocks(html, "marketplace_search", "buy_location"):
        for d in _dicts(data):
            loc = d.get("buy_location")
            if isinstance(loc, dict) and loc.get("display_name"):
                place = loc["display_name"]
            ms = d.get("marketplace_search")
            if not isinstance(ms, dict) or "feed_units" not in ms:
                continue
            found = True
            for edge in (ms.get("feed_units") or {}).get("edges") or []:
                li = ((edge or {}).get("node") or {}).get("listing")
                ad = parse_listing(li) if isinstance(li, dict) else None
                if ad and ad.ad_id not in ads:
                    ads[ad.ad_id] = ad
    if not found:
        raise PlatformError(t("Facebook page without search results (login wall or changed page)"))
    return SearchPage(rows_found=len(ads), ads=list(ads.values()), title=place)


def parse_item_page(html: str, ad_id: str) -> AdDetail | None:
    """A listing page -> AdDetail; None when it has no such listing (sold and removed; logged out a refusal looks
    the same)."""
    target: dict[str, Any] = {}
    photos: list[str] = []
    path: list[str] = []
    for data in _blocks(html, "marketplace_product_details_page", "seo_virtual_category"):
        for d in _dicts(data):
            page = d.get("marketplace_product_details_page")
            tgt = page.get("target") if isinstance(page, dict) else None
            if isinstance(tgt, dict) and str(tgt.get("id")) == ad_id:
                if "marketplace_listing_title" in tgt:
                    target.update(tgt)
                for ph in tgt.get("listing_photos") or []:
                    uri = ((ph or {}).get("image") or {}).get("uri")
                    if uri and uri not in photos:
                        photos.append(uri)
            seo = d.get("seo_virtual_category")
            if isinstance(seo, dict) and not path:
                path = [n["name"] for n in seo.get("taxonomy_path") or [] if isinstance(n, dict) and n.get("name")]
    if not target:
        return None
    price = target.get("listing_price") or {}
    amount = _amount(price)
    attrs = target.get("attribute_data") or []
    extras = [f"{a['attribute_name']}: {a.get('label') or a.get('value')}" for a in attrs
              if isinstance(a, dict) and a.get("attribute_name")]
    text = clean_text((target.get("redacted_description") or {}).get("text")) or ""
    description = (text + ("\n\n" if text and extras else "") + " · ".join(extras)) or None
    sold = target.get("is_sold") or target.get("renderable_listing_status") not in (None, "AVAILABLE")
    seller = target.get("marketplace_listing_seller") or {}
    place = one_line((target.get("location_text") or {}).get("text"))
    posted = from_epoch(target.get("creation_time"))
    item_condition, item_condition_label = condition.facebook(attrs)
    return AdDetail(ad_id=ad_id, heading=one_line(target.get("marketplace_listing_title")) or ad_id,
                    description=description, price=amount, price_display=_display(price, amount),
                    status="sold" if sold else "active", published_at=posted, first_published_at=posted,
                    end_date=None, url=f"{WEB}/marketplace/item/{ad_id}/", images=photos, location=place,
                    place=place, seller_name=one_line(seller.get("name")) if isinstance(seller, dict) else None,
                    is_private=False if target.get("should_show_business_seller_label") else None,
                    chat_enabled=target.get("messaging_enabled"), category_path=path, platform="facebook",
                    item_condition=item_condition, item_condition_label=item_condition_label)


# ------------------------------------------------------------------ links and the stored location
def search_url(query: str, loc: str, *, radius: str | int | None = None, price_from: float | None = None,
               price_to: float | None = None, sort: str = "newest") -> str:
    """A Marketplace search link; relevance order (Facebook's default) unless `sort` says otherwise."""
    params: dict[str, str] = {"query": query.strip()}
    if price_from:
        params["minPrice"] = f"{price_from:.0f}"
    if price_to:
        params["maxPrice"] = f"{price_to:.0f}"
    if radius:
        params["radius"] = str(radius)
    if sort in SORTS:
        params["sortBy"] = SORTS[sort]
    return f"{WEB}/marketplace/{quote(loc, safe='')}/search?{urlencode(params)}"


def location() -> dict[str, Any] | None:
    """The place keyword searches use on Facebook, or None when not set up."""
    from app.db import get_setting

    try:
        data = json.loads(get_setting(LOCATION_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("loc") else None


def location_from_link(link: str) -> tuple[str, str | None]:
    """(location, radius) of a Marketplace link (search or browse page). Raises InvalidSearchUrl."""
    u = urlsplit((link or "").strip())
    host = (u.hostname or "").lower()
    m = _ANY_LOC.match(u.path)
    if not (host == "facebook.com" or host.endswith(".facebook.com")) or not m \
            or m.group("loc").lower() in _NOT_LOCATIONS:
        raise InvalidSearchUrl(t("Not a Facebook Marketplace link with a location (facebook.com/marketplace/<city>/…)"))
    radius = dict(parse_qsl(u.query)).get("radius")
    return m.group("loc"), radius if radius and radius.isdigit() else None


async def save_location(link: str) -> dict[str, Any]:
    """Store the place of a pasted Marketplace link; one test search checks that Facebook knows it."""
    from app.db import set_setting

    loc, radius = location_from_link(link)
    page = await get_client().search_page(search_url("sofa", loc, radius=radius, sort="relevance"))
    data = {"loc": loc, "radius": radius, "place": page.title or loc, "link": link.strip()}
    set_setting(LOCATION_KEY, json.dumps(data))
    return data


def clear_location() -> None:
    from app.db import set_setting

    set_setting(LOCATION_KEY, "")


# ------------------------------------------------------------------ the wire
def _complete(body: bytearray, markers: tuple[bytes, ...]) -> bool:
    """Every marker is in `body` and the script block around its first occurrence is closed."""
    for m in markers:
        i = body.find(m)
        if i < 0 or body.find(b"</script>", i) < 0:
            return False
    return True


def _markers(url: str) -> tuple[bytes, ...]:
    path = urlsplit(url).path
    if path.startswith("/marketplace/item/"):
        return ITEM_MARKERS
    return SEARCH_MARKERS if "/search" in path else ()


class FacebookTransport:
    """httpx that reads a page only until the data it needs has arrived (see the module docstring); `transport`:
    tests' httpx.MockTransport, `proxy`: the VPN tunnel. A fresh cookie jar per request: logged out, Facebook needs
    none, and no visitor is carried from one search to the next."""

    def __init__(self, headers: dict[str, str], *, proxy: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if transport is not None and proxy:
            raise ValueError("a mock transport and a proxy at the same time")
        self._headers, self._transport, self.proxy = headers, transport, proxy
        self.client = self._new()

    def _new(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(headers=self._headers, timeout=httpx.Timeout(TIMEOUT_S, read=READ_STALL_S),
                                 follow_redirects=True, transport=self._transport, proxy=self.proxy)

    async def send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> FetchResponse:
        if self.client.is_closed:
            self.client = self._new()
        self.client.cookies.clear()
        markers = _markers(url)
        body = bytearray()
        try:
            async with self.client.stream(method, url, params=params or None, headers=headers) as r:
                try:
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if markers and _complete(body, markers):
                            break
                except httpx.ReadTimeout:
                    if not body:
                        raise
                return FetchResponse(r.status_code, dict(r.headers), bytes(body), str(r.url))
        except httpx.HTTPError as e:
            raise TransportError(str(e)) from e

    def clear_cookies(self) -> None:
        self.client.cookies.clear()

    async def aclose(self) -> None:
        await self.client.aclose()


def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/marketplace/item/"):
        return "detail"
    return "search" if "/search" in path else "other"


_limiter: RateLimiter | None = None


def facebook_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("facebook"), platform_per_hour("facebook"), name="Facebook", persist="facebook")
    return _limiter


class FacebookClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None, httpx_transport: httpx.AsyncBaseTransport | None = None) -> None:
        if transport is None and httpx_transport is not None:     # tests: the streaming transport over a mock
            transport = FacebookTransport(NAV_HEADERS, transport=httpx_transport)
        super().__init__("facebook", facebook_limiter(), name="Facebook", headers=NAV_HEADERS, transport=transport,
                         transport_factory=None if transport is not None
                         else (lambda proxy: FacebookTransport(NAV_HEADERS, proxy=proxy)))

    def classify(self, url: str) -> str:
        return classify(url)

    @staticmethod
    def _refused(r: FetchResponse) -> bool:
        """400 (incomplete browser headers, or Facebook's bot check), 403/429, or a redirect to the login."""
        path = urlsplit(r.url).path
        return r.status_code in (400, 403, 429) or path.startswith("/login") or path.startswith("/checkpoint")

    def _check_status(self, r: FetchResponse) -> None:
        if self._refused(r):
            self.new_anonymous_session()
            delay = self.limiter.penalize()
            why = f"HTTP {r.status_code}" if r.status_code != 200 else t("login wall")
            blocked = Blocked(t("Facebook refused the request ({why}); pausing {s}s", why=why, s=f"{delay:.0f}"))
            blocked.sent = True
            raise blocked

    def _exit_blocked(self, r: FetchResponse) -> bool:
        return self._refused(r)

    async def search_page(self, url: str) -> SearchPage:
        r = await self._get(url, kind="search")
        if r.status_code != 200:
            raise PlatformError(t("Facebook search returned HTTP {status}", status=r.status_code))
        if urlsplit(r.url).path.startswith("/marketplace/category/"):    # unknown place: Facebook's default instead
            raise PlatformError(t("Facebook does not know this location; open the search on Facebook, pick the "
                                  "location there and copy the link again"))
        try:
            return parse_search(r.text)
        except PlatformError:
            self.limiter.penalize()          # a login wall in place of the page: slow down like after a block
            raise

    async def item(self, ad_id: str) -> AdDetail | None:
        r = await self._get(f"{WEB}/marketplace/item/{ad_id}/", cache_s=300, kind="detail")
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise PlatformError(t("Facebook ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        return parse_item_page(r.text, ad_id)


_client: FacebookClient | None = None


def get_client() -> FacebookClient:
    global _client
    if _client is None:
        _client = FacebookClient()
    return _client


def set_client(client: FacebookClient | None) -> None:
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


class FacebookPlatform(Platform):
    id = "facebook"
    name = "Facebook Marketplace"
    host = "facebook.com"
    api_host = "www.facebook.com"
    home_country = "AT"
    hosts = ("facebook.com",)
    image_hosts = ("fbcdn.net",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})
    PAGE_SIZE_MAX = 24          # what one search page shows without a login; no further pages
    CAN_PAGE = False
    access = "http"
    min_interval_s = 600                 # one whole page per check, and Facebook does not like automated visitors

    @property
    def limiter(self) -> RateLimiter:
        return facebook_limiter()

    @property
    def client(self) -> FacebookClient:
        return get_client()

    def about(self) -> str:
        return t("Reads the public Marketplace pages without a Facebook account: the newest ~24 listings per search "
                 "(more need a login), a few of them from outside the radius. Opening an ad loads its page with the "
                 "description, condition and all photos. Facebook's terms do not allow automated access.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a facebook.com link"))
        if _ITEM_LINK.search(u.path):
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        m = _SEARCH_PATH.match(u.path)
        if not m or m.group("loc").lower() in _NOT_LOCATIONS:
            raise InvalidSearchUrl(t("Not a Facebook Marketplace search with a location "
                                     "(facebook.com/marketplace/<city>/search?query=…)"))
        q = dict(parse_qsl(u.query))
        keyword = one_line(q.get("query"))
        if not keyword:
            raise InvalidSearchUrl(t("The Facebook link has no search term (query=…)"))
        sort = {v: k for k, v in SORTS.items()}.get(q.get("sortBy") or "", "newest")   # agents: newest first
        radius = q.get("radius") if (q.get("radius") or "").isdigit() else None
        spec = SearchSpec(platform=self.id, url="", keyword=keyword, price_from=_num(q.get("minPrice")),
                          price_to=_num(q.get("maxPrice")), location=m.group("loc"), sort=sort,
                          extra={"radius": radius} if radius else {})
        spec.url = self._url(spec)
        return spec

    def _url(self, spec: SearchSpec) -> str:
        stored = location() or {}
        loc = spec.location or stored.get("loc")
        if not loc:
            return f"{WEB}/marketplace/search?{urlencode({'query': spec.keyword or ''})}"
        radius = spec.extra.get("radius") or (stored.get("radius") if not spec.location else None)
        return search_url(spec.keyword or "", loc, radius=radius, price_from=spec.price_from,
                          price_to=spec.price_to, sort=spec.sort)

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        stored = location() or {}
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          location=stored.get("loc"), sort=sort,
                          extra={"radius": stored["radius"]} if stored.get("radius") else {})
        spec.url = self._url(spec)
        return spec

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        if page > 1:                                # no further pages: nothing more to fetch
            return SearchPage(ads=[], rows_found=0)
        if not spec.location:
            raise PlatformError(t("Facebook Marketplace needs a location: set it in Settings → Facebook Marketplace"))
        page = await self.client.search_page(self._url(spec))
        if rows:
            page.ads = page.ads[:rows]
        return page

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        return await self.client.item(ad_id)

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/marketplace/item/{ad_id}/"

    def parse_ad_url(self, url: str) -> str | None:
        m = _ITEM_LINK.search(urlsplit(url).path)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["FacebookClient", "FacebookPlatform", "FacebookTransport", "get_client", "set_client", "parse_search",
           "parse_listing", "parse_item_page", "location", "save_location", "clear_location", "search_url"]
