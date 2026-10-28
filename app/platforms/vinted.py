"""vinted.at / vinted.de as a platform adapter, through the React Server Components answer of the website.

Vinted's old JSON catalog API (/api/v2/catalog/items) is gone (404 since the website moved to Next.js), and its
item API refuses anonymous clients. The website itself renders the catalog on the server: asking a catalog or an
item page with the header `RSC: 1` returns the "flight" data (text/x-component) instead of HTML, and that holds the
result list as plain JSON ("items":{"items":[…],"pagination":{…}}) and, for an item, the page's "plugins" (summary,
attributes, description, photos, seller). The answer is large (about 3 MB unpacked, mostly translations), so the
site has a higher minimum agent interval. DataDome guards the site: requests go through curl_cffi impersonating
Chrome (app/platforms/httpclient.py), a 403 is a block.

A search link keeps every filter of the website (category path, brand, size, condition, …) and drops page and
session values; the agents always ask for the newest items first. Vinted's search is loose: with the newest first,
most of a page names none of the search words (shoes and make-up for "Raspberry pi 5", 10-14), so a result is kept
only when its title and brand/size line hold every word. Listings come without date and place; opening an ad reads
"uploaded 6 min. ago" from its page, and the shipping to Austria ("pricingServices": the cheapest option, as the
site's "Versand ab € 5,95", or pickup only).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.i18n import t
from app.platforms import condition, shipping_info
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import clean_text, euro, mentions, one_line, to_float
from app.platforms.types import AdDetail, Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

HOST = "www.vinted.at"
HOSTS = ("vinted.at", "vinted.de")
ORDERS = {"newest": "newest_first", "relevance": "relevance", "price_asc": "price_low_to_high",
          "price_desc": "price_high_to_low"}
VOLATILE = {"page", "time", "search_id", "search_session_id", "search_by_image_uuid", "order", "currency", "per_page",
            "referrer", "disabled_personalization", "search_text", "price_from", "price_to"}
RSC_HEADERS = {"RSC": "1", "Accept": "*/*", "Accept-Language": "de-AT"}
_ITEMS = '"items":{"items":['
_PAGINATION = '"pagination":{'
_PLUGINS = re.compile(r'\{"plugins":\s*\[')
_PRICE = re.compile(r'"price":\s*\{"amount":\s*"([\d.]+)",\s*"currency_code":\s*"([A-Z]{3})"\}')
_PRICING = '"pricingServices":{'
_dec = json.JSONDecoder()

# "6 Min.", "2 Std.", "3 Tage", "1 Woche", "vor 2 Monaten", "Hochgeladen 6 min.", "just now" -> an age
# written out ("2 Std.", "3 Tage") or short ("9 h", "2 d", "1 T"; Vinted switched to these, seen 2026-10-23)
_UNITS = [(r"s$|sek|sec", 1), (r"min|m$", 60), (r"h$|std|stunde|hour|hr", 3600), (r"d$|t$|tag|day", 86400),
          (r"w$|wo|week", 7 * 86400), (r"mon|month", 30 * 86400), (r"j$|y$|jahr|year", 365 * 86400)]


def parse_age(text: str | None, now: datetime | None = None) -> datetime | None:
    """A relative upload time as Vinted shows it -> an aware UTC datetime (approximate); None when unreadable."""
    s = (text or "").strip().lower()
    if not s:
        return None
    now = now or datetime.now(timezone.utc)
    if "gerade" in s or "just now" in s or "soeben" in s:
        return now
    m = re.search(r"(\d+)\s*([a-zä]+)", s) or (re.search(r"\b(ein|eine|einem|einer|a|an)\s+([a-zä]+)", s))
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else 1
    for pattern, seconds in _UNITS:
        if re.match(pattern, m.group(2)):
            return now - timedelta(seconds=n * seconds)
    return None


# ------------------------------------------------------------------ parsing
def _money(node: Any) -> float | None:
    return to_float((node or {}).get("amount")) if isinstance(node, dict) else None


def parse_listing(item: dict[str, Any], host: str = HOST) -> Listing:
    p = item.get("productItem") or item
    price = _money(p.get("price"))
    discounted = _money(p.get("priceWithDiscount"))
    total = _money(p.get("totalItemPrice"))
    fee = _money(p.get("serviceFee"))
    box = p.get("itemBox") or {}
    lines = [one_line(box.get("firstLine")), one_line(box.get("secondLine"))]
    if total is not None:
        lines.append(t("{price} incl. buyer protection", price=euro(total)))
    photos = [ph.get("url") for ph in p.get("photos") or [] if isinstance(ph, dict) and ph.get("url")]
    user = p.get("user") or {}
    shown = discounted if discounted is not None and price is not None and discounted < price else price
    url = p.get("url") or f"/items/{p.get('id')}"
    item_condition, item_condition_label = condition.vinted_listing(box)
    return Listing(ad_id=str(p.get("id") or item.get("id") or ""), heading=one_line(p.get("title")) or "",
                   body=" · ".join(x for x in lines if x) or None, price=shown,
                   price_display=euro(shown) if shown is not None else None,
                   old_price=price if shown is not None and price is not None and shown < price else None,
                   location=None, postcode=None, state=None, published_at=None, changed_at=None, end_date=None,
                   url=url if url.startswith("http") else f"https://{host}{url}",
                   image_url=p.get("thumbnailUrl") or (photos[0] if photos else None),
                   is_private=(not user.get("isBusiness")) if "isBusiness" in user else None, is_bumped=None,
                   status="reserved" if (p.get("is_reserved") or p.get("isReserved") or item.get("is_reserved"))
                   else "active", platform="vinted", item_condition=item_condition,
                   item_condition_label=item_condition_label, buyer_fee=fee)


_RESERVED = re.compile(r'"is_reserved"\s*:\s*true')     # the item page's own flag (the label: "Reserviert")


# the item page's "buyer_item_status" plugin: {"theme": "SUCCESS", "title": "Verkauft"} on a sold item (seen
# 2026-10-21, the one sold to the owner); "Reserviert" on a reserved one
_STATUS_TITLES = {"verkauft": "sold", "sold": "sold", "reserviert": "reserved", "reserved": "reserved"}


def _item_status(plugins: dict[str, dict[str, Any]], text: str) -> str:
    title = str((plugins.get("buyer_item_status") or {}).get("title") or "").strip().lower()
    if title in _STATUS_TITLES:
        return _STATUS_TITLES[title]
    return "reserved" if _RESERVED.search(text or "") else "active"


# the profile page's own user: {"country_title_local": "Deutschland", "country_iso_code": "DE", …} (seen 2026-10-22,
# fetched anonymously, so the only user on the page is the member)
_COUNTRY = re.compile(r'"country_iso_code"\s*:\s*"([A-Z]{2})"|"country_code"\s*:\s*"([A-Z]{2})"')


_ONLINE = re.compile(r'"is_online"\s*:\s*(true|false)')
_LAST_SEEN = re.compile(r'"last_loged_on_ts"\s*:\s*"([^"]+)"')      # sic: Vinted's spelling
_COUNTRY_TITLE = re.compile(r'"country_title"\s*:\s*"([^"]{2,60})"')   # in the page's language (de-AT asked)
_CITY = re.compile(r'"expose_location"\s*:\s*true\s*,\s*"city"\s*:\s*"([^"]{1,60})"')   # only when shown


def parse_member_country(text: str) -> str | None:
    m = _COUNTRY.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


def parse_member(text: str) -> dict[str, Any]:
    """The profile page's own user: {country, online (bool or None), last_seen (ISO time or None)}."""
    on, seen = _ONLINE.search(text or ""), _LAST_SEEN.search(text or "")
    title, city = _COUNTRY_TITLE.search(text or ""), _CITY.search(text or "")
    return {"country": parse_member_country(text), "online": (on.group(1) == "true") if on else None,
            "last_seen": seen.group(1) if seen else None, "country_title": title.group(1) if title else None,
            "city": city.group(1) if city else None}


def parse_catalog(text: str, host: str = HOST) -> SearchPage:
    """The flight data of a catalog page -> SearchPage. PlatformError when the answer holds no result list."""
    i = (text or "").find(_ITEMS)
    if i < 0:
        raise PlatformError(t("Vinted answered without a result list"))
    try:
        items, end = _dec.raw_decode(text, i + len(_ITEMS) - 1)
    except ValueError as e:
        raise PlatformError(t("Vinted answered without a result list")) from e
    total = None
    j = text.find(_PAGINATION, end, end + 200)
    if j >= 0:
        try:
            pagination, _ = _dec.raw_decode(text, j + len(_PAGINATION) - 1)
            total = pagination.get("total_entries")
        except ValueError:
            pass
    ads = [parse_listing(x, host) for x in items if isinstance(x, dict) and (x.get("productItem") or x.get("id"))]
    return SearchPage(rows_found=int(total) if isinstance(total, int) else len(ads), ads=ads)


# Vinted's pages are React Server Components: a long text is a row of its own ("103:T436,<text>", the length in hex
# bytes) and the object holds only "$103" (10-28: a camera's description went to willhaben as "$103"). A string that
# really starts with "$" is written "$$…".
_REF = re.compile(r"^\$([0-9a-f]+)$")


def is_ref(value: Any) -> bool:
    """An unresolved reference ("$103") instead of a text: never to be shown or sent anywhere."""
    return isinstance(value, str) and bool(_REF.match(value.strip()))


def rsc_text(payload: str, value: Any) -> Any:
    """`value` with a reference resolved to its text row of `payload` (unchanged when it is none or not found)."""
    if not isinstance(value, str):
        return value
    if value.startswith("$$"):
        return value[1:]
    m = _REF.match(value)
    if not m:
        return value
    row = re.search(r"(?:^|\n)%s:T([0-9a-f]+)," % re.escape(m.group(1)), payload or "")
    if not row:
        return value
    size, out, used = int(row.group(1), 16), [], 0
    for ch in payload[row.end():]:
        n = len(ch.encode("utf-8"))
        if used + n > size:
            break
        out.append(ch)
        used += n
    return "".join(out)


def _plugins(text: str) -> dict[str, dict[str, Any]]:
    """The item page's plugins by name (the first block that has them)."""
    for m in _PLUGINS.finditer(text or ""):
        try:
            obj, _ = _dec.raw_decode(text, m.start())
        except ValueError:
            continue
        found = {p.get("name"): (p.get("data") or {}) for p in obj.get("plugins") or [] if isinstance(p, dict)}
        if "description" in found or "attributes" in found:
            return found
    return {}


def _shipping(text: str) -> tuple[float | None, str | None]:
    """The item's own pricing block -> (price, kind); (None, None) when the page has none."""
    i = (text or "").find(_PRICING)
    if i < 0:
        return None, None
    try:
        pricing, _ = _dec.raw_decode(text, i + len(_PRICING) - 1)
    except ValueError:
        return None, None
    services = pricing.get("services") or {}
    if services.get("isPickUpOnly"):
        return None, shipping_info.PICKUP
    ship = services.get("shipping")
    if not isinstance(ship, dict):
        return None, None
    if ship.get("isFree"):
        return 0.0, shipping_info.SHIP
    return _money(ship.get("finalPrice")), shipping_info.SHIP


def parse_item(text: str, ad_id: str, host: str = HOST, now: datetime | None = None) -> AdDetail | None:
    plugins = _plugins(text)
    if not plugins:
        return None
    summary = plugins.get("summary") or {}
    title = rsc_text(text, next((e.get("value") for ln in summary.get("lines") or [] for e in ln.get("elements") or []
                                 if e.get("style") == "title"), None))
    attrs = {a.get("code"): (a.get("data") or {}) for a in (plugins.get("attributes") or {}).get("attributes") or []
             if isinstance(a, dict)}
    description = rsc_text(text, (plugins.get("description") or {}).get("description"))
    description = "" if is_ref(description) else (clean_text(description) or "")
    facts = [f"{a.get('title')}: {a.get('value')}" for code, a in attrs.items()
             if code != "upload_date" and a.get("title") and a.get("value")]
    if facts:
        description = (description + "\n\n" if description else "") + " · ".join(facts)
    offer = plugins.get("make_offer") or {}
    photos = [ph.get("url") for ph in offer.get("photos") or [] if isinstance(ph, dict) and ph.get("url")]
    seller = plugins.get("user_info_header") or {}
    m = _PRICE.search(text)
    price = to_float(m.group(1)) if m else None
    business = seller.get("business", offer.get("business"))
    uploaded = parse_age((attrs.get("upload_date") or {}).get("value"), now)
    item_condition, item_condition_label = condition.vinted_item(attrs)
    shipping_price, shipping = _shipping(text)
    return AdDetail(ad_id=ad_id, heading=one_line(title) or "", description=description or None, price=price,
                    price_display=euro(price) if price is not None else None,
                    status=_item_status(plugins, text), published_at=uploaded,
                    first_published_at=uploaded, end_date=None, url=f"https://{host}/items/{ad_id}", images=photos,
                    seller_name=one_line(seller.get("name")),
                    seller_ratings=seller["feedback_count"] if isinstance(seller.get("feedback_count"), int) else None,
                    is_private=(not business) if isinstance(business, bool) else None, platform="vinted",
                    item_condition=item_condition, item_condition_label=item_condition_label,
                    shipping_price=shipping_price, shipping=shipping)


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/catalog"):
        return "search"
    if path.startswith("/items/"):
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def vinted_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("vinted"), platform_per_hour("vinted"), name="Vinted")
    return _limiter


class VintedClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("vinted", vinted_limiter(), name="Vinted", headers={"Accept-Language": "de-AT"},
                         transport=transport, backend="curl")

    def classify(self, url: str) -> str:
        return classify(url)

    async def catalog(self, url: str, host: str, *, cache_s: float = 0) -> SearchPage:
        r = await self._get(url, headers=RSC_HEADERS, cache_s=cache_s, kind="search")
        if r.status_code != 200:
            raise PlatformError(t("Vinted search returned HTTP {status}", status=r.status_code))
        return parse_catalog(r.text, host)

    async def member(self, user_id: str, host: str = HOST, cache_s: float = 86400) -> dict[str, Any]:
        """A member's public profile: {country, online, last_seen} (parse_member)."""
        r = await self._get(f"https://{host}/member/{user_id}", headers=RSC_HEADERS, cache_s=cache_s, kind="detail")
        if r.status_code != 200:
            raise PlatformError(t("Vinted member {id}: HTTP {status}", id=user_id, status=r.status_code))
        return parse_member(r.text)

    async def member_country(self, user_id: str, host: str = HOST) -> str | None:
        """A member's country (ISO code, "DE") from their public profile page; None when it says none."""
        return (await self.member(user_id, host))["country"]

    async def item(self, ad_id: str, host: str = HOST) -> AdDetail | None:
        r = await self._get(f"https://{host}/items/{ad_id}", headers=RSC_HEADERS, cache_s=300, kind="detail")
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise PlatformError(t("Vinted ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        return parse_item(r.text, ad_id, host)


_client: VintedClient | None = None


def get_client() -> VintedClient:
    global _client
    if _client is None:
        _client = VintedClient()
    return _client


def set_client(client: VintedClient | None) -> None:
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


class VintedPlatform(Platform):
    id = "vinted"
    name = "Vinted"
    host = "vinted.at"
    api_host = HOST
    home_country = "AT"
    hosts = HOSTS
    image_hosts = ("vinted.net",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})
    PAGE_SIZE_MAX = 96          # the catalog's page (its per_page default on the web)
    CAN_PAGE = True
    access = "http"
    min_interval_s = 300                      # every search loads the catalog page's data (about 3 MB unpacked)

    @property
    def limiter(self) -> RateLimiter:
        return vinted_limiter()

    @property
    def client(self) -> VintedClient:
        return get_client()

    def about(self) -> str:
        return t("Reads the catalog the Vinted website renders for a browser (no API, no login). Every search loads "
                 "about 3 MB, so agents on Vinted run at most every 5 minutes. Listings come without date and place; "
                 "opening an ad loads its details.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a vinted.at or vinted.de link"))
        if u.path.startswith("/items/"):
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        m = re.fullmatch(r"/catalog(?:/(\d+)(?:-[^/]*)?)?/?", u.path)
        if not m:
            raise InvalidSearchUrl(t("Not a Vinted result list (copy the link of a search: …/catalog?search_text=…)"))
        pairs = parse_qsl(u.query, keep_blank_values=False)
        q = dict(pairs)
        keyword = one_line(q.get("search_text"))
        extra_params = sorted([k, v] for k, v in pairs if k not in VOLATILE)
        if not keyword and not m.group(1) and not extra_params:
            raise InvalidSearchUrl(t("The Vinted link has no search term or filter"))
        extra: dict[str, Any] = {}
        if extra_params:
            extra["params"] = extra_params
        host = (u.hostname or HOST).lower()
        if host != HOST:
            extra["host"] = host
        spec = SearchSpec(platform=self.id, url="", keyword=keyword, price_from=_num(q.get("price_from")),
                          price_to=_num(q.get("price_to")), category=m.group(1), extra=extra)
        spec.url = self._url(spec)
        return spec

    def _url(self, spec: SearchSpec, order: str = "newest") -> str:
        params: list[tuple[str, str]] = []
        if spec.keyword:
            params.append(("search_text", spec.keyword))
        params += [(k, v) for k, v in spec.extra.get("params") or []]
        if spec.price_from is not None:
            params.append(("price_from", f"{spec.price_from:g}"))
        if spec.price_to is not None:
            params.append(("price_to", f"{spec.price_to:g}"))
        if spec.price_from is not None or spec.price_to is not None:
            params.append(("currency", "EUR"))
        params.append(("order", ORDERS.get(order, "newest_first")))
        path = f"/catalog/{spec.category}" if spec.category else "/catalog"
        return f"https://{spec.extra.get('host') or HOST}{path}?{urlencode(params)}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          sort=sort)
        spec.url = self._url(spec)
        return spec

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        from app.config import get_settings

        host = spec.extra.get("host") or HOST
        url = self._url(spec, spec.sort)
        if page > 1:                                # the catalog's own paging
            url += f"&page={page}"
        result = await self.client.catalog(url, host, cache_s=cache_s)
        if spec.keyword:     # Vinted also finds ads that name none of the words (make-up for "Unifi"): left out
            result.ads = [ad for ad in result.ads if mentions(f"{ad.heading} {ad.body or ''}", spec.keyword)]
        result.ads = result.ads[:rows or get_settings().search_rows]
        result.title = spec.keyword
        return result

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        host = (urlsplit(url).hostname or HOST) if url and self.matches(url) else HOST
        return await self.client.item(ad_id, host)

    def ad_url(self, ad_id: str) -> str:
        return f"https://{HOST}/items/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/items/(\d{5,})(?:-[^/?#]*)?/?(?:[?#]|$)", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["VintedClient", "VintedPlatform", "get_client", "set_client", "parse_catalog", "parse_item", "parse_listing",
           "parse_age", "parse_member", "parse_member_country"]
