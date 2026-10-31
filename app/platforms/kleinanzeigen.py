"""kleinanzeigen.de (Germany) as a platform adapter, through the JSON API the Android app uses.

The website sits behind bot protection that rejects plain HTTP clients at the TLS layer; the app's API
(api.kleinanzeigen.de) answers a browser-impersonating client (curl_cffi, app/platforms/httpclient.py) with clean
JSON: price with its type (fixed, negotiable "VB", free), private or commercial poster, pictures, posting time and,
in categories that have one, the condition (app/platforms/condition.py).
It needs the app's HTTP Basic credentials: distribution values baked into the Android app and published in
open-source clients, not personal secrets, but they can rotate. KLEINANZEIGEN_API_AUTH="user:pw" (or the setting
in Settings → Platforms) overrides the bundled default; a 401/403 says so in the agent's error.

Search links from the website are understood (/s-preis:10:100/berlin/fahrrad/k0), with the place resolved to the
API's location id on first use. Answers come wrapped in {"value": …} nodes (eBay Classifieds "capi").
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from app.config import get_settings
from app.i18n import t
from app.platforms import condition
from app.platforms.base import InvalidSearchUrl, NotTheExit, Platform, PlatformError
from app.platforms.httpclient import FetchResponse, HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import clean_text, euro, one_line, parse_iso, to_float
from app.platforms.types import AdDetail, Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

API = "https://api.kleinanzeigen.de/api"
WEB = "https://www.kleinanzeigen.de"
APP_VERSION = "2026.25.0"
ADS_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads"
AD_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad"
LOCATIONS_NS = "{http://www.ebayclassifiedsgroup.com/schema/location/v1}locations"
SORTS = {"newest": "DATE_DESCENDING", "relevance": "DATE_DESCENDING", "price_asc": "PRICE_ASCENDING",
         "price_desc": "PRICE_DESCENDING"}
MAX_ROWS = 50
LOCATION_CACHE_S = 24 * 3600
PICTURE_RELS = ("XXL", "large", "teaser")

_locations: dict[str, tuple[float, int | None]] = {}


# ------------------------------------------------------------------ JSON helpers
def _val(node: Any) -> Any:
    """The scalar inside a capi node: {"value": x} (also nested) -> x."""
    if isinstance(node, dict):
        if "value" in node:
            return _val(node["value"])
        return node
    return node


def _many(x: Any) -> list[Any]:
    """A list of nodes; one item comes back as a dict, not a list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _text(node: Any) -> str | None:
    v = _val(node)
    return html.unescape(str(v)) if v not in (None, "") else None


def _links(node: Any) -> list[dict[str, Any]]:
    return [ln for ln in _many(node) if isinstance(ln, dict)]


def _pictures(ad: dict[str, Any]) -> list[str]:
    out = []
    for pic in _many((ad.get("pictures") or {}).get("picture")):
        if not isinstance(pic, dict):
            continue
        by_rel = {ln.get("rel"): ln.get("href") for ln in _links(pic.get("link")) if ln.get("href")}
        best = next((by_rel[r] for r in PICTURE_RELS if r in by_rel), next(iter(by_rel.values()), None))
        if best:
            out.append(best)
    return out


def _price(ad: dict[str, Any]) -> tuple[float | None, str | None, bool | None]:
    """(price, display, negotiable): FREE -> 0 "Zu verschenken"; no amount -> None "VB"; "120 € VB" when negotiable.
    Seen live (2026-10): SPECIFIED_AMOUNT and PLEASE_CONTACT with an amount (shown as the plain price)."""
    price = ad.get("price") or {}
    kind = (str(_val(price.get("price-type")) or "")).upper()
    amount = to_float(_val(price.get("amount")))
    if kind == "FREE":
        return 0.0, "Zu verschenken", False
    vb = kind == "NEGOTIABLE"
    if amount is None:
        return None, "VB" if vb or kind in ("SEE_DESCRIPTION", "PLEASE_CONTACT") else None, vb or None
    return amount, euro(amount) + (" VB" if vb else ""), vb


def _shipping(ad: dict[str, Any]) -> str | None:
    """The category's shipping attribute (<category>.versand): "nein" = pickup only ("Nur Abholung"), "ja" = shipping
    possible; None where the category has none (the seller's price for it is not in the answer)."""
    for a in _many((ad.get("attributes") or {}).get("attribute")):
        if isinstance(a, dict) and str(a.get("name") or "").endswith(".versand"):
            v = next((str(x.get("value") or "").lower() for x in _many(a.get("value")) if isinstance(x, dict)), "")
            return "pickup" if v == "nein" else "ship" if v == "ja" else None
    return None


def _common(ad: dict[str, Any]) -> dict[str, Any]:
    addr = ad.get("ad-address") or {}
    postcode = _text(addr.get("zip-code"))
    city = one_line((_text(addr.get("state")) or "").replace("\t", ", "))   # "Hamburg\tWaltershof": city, district
    price, display, vb = _price(ad)
    poster = (str(_val(ad.get("poster-type")) or "")).upper()
    url = next((ln.get("href") for ln in _links(ad.get("link")) if ln.get("rel") == "self-public-website"), None)
    ad_id = str(_val(ad.get("id")) or "")
    category = ad.get("category") or {}
    item_condition, item_condition_label = condition.kleinanzeigen(ad)
    return {"ad_id": ad_id, "status": _status(ad), "heading": _text(ad.get("title")) or "", "price": price, "price_display": display, "vb": vb,
            "location": " ".join(x for x in (postcode, city) if x) or None, "postcode": postcode,
            "published_at": parse_iso(_val(ad.get("start-date-time"))),
            "url": url or f"{WEB}/s-anzeige/{ad_id}", "images": _pictures(ad),
            "is_private": None if not poster else poster == "PRIVATE",
            "category_id": str(_val(category.get("id"))) if _val(category.get("id")) not in (None, "") else None,
            "category_name": _text(category.get("localized-name")) or _text(category.get("localized_name")),
            "description": clean_text(_text(ad.get("description"))),
            "lat": to_float(_val(addr.get("latitude"))), "lon": to_float(_val(addr.get("longitude"))),
            "item_condition": item_condition, "item_condition_label": item_condition_label, "shipping": _shipping(ad)}


# ad-status: ACTIVE, PAUSED (the seller marked it reserved: "Reserviert" on the site), DELETED; seen in the app's
# recordings 2026-10-20
_AD_STATUS = {"ACTIVE": "active", "PAUSED": "reserved", "DELETED": "deleted", "SOLD": "sold"}


def _status(ad: dict[str, Any]) -> str:
    raw = str(_val(ad.get("ad-status")) or "ACTIVE").upper()
    return _AD_STATUS.get(raw, raw.lower())


def parse_listing(ad: dict[str, Any]) -> Listing:
    c = _common(ad)
    body = c["description"]
    return Listing(ad_id=c["ad_id"], heading=c["heading"], body=body[:400] if body else None, price=c["price"],
                   price_display=c["price_display"], old_price=None, location=c["location"], postcode=c["postcode"],
                   state=None, published_at=c["published_at"], changed_at=None, end_date=None, url=c["url"],
                   image_url=c["images"][0] if c["images"] else None, is_private=c["is_private"], is_bumped=None,
                   status=c["status"], category_ids=[c["category_id"]] if c["category_id"] else [], platform="kleinanzeigen",
                   lat=c["lat"], lon=c["lon"], item_condition=c["item_condition"],
                   item_condition_label=c["item_condition_label"], shipping=c["shipping"])


def parse_ads(data: dict[str, Any]) -> SearchPage:
    block = _val((data or {}).get(ADS_NS)) or {}
    if not isinstance(block, dict):
        block = {}
    found = to_float(_val((block.get("paging") or {}).get("numFound")))
    ads = [parse_listing(a) for a in _many(block.get("ad")) if isinstance(a, dict)]
    rows = int(found) if found is not None else len(ads)
    return SearchPage(rows_found=rows, ads=ads)


def parse_ad(data: dict[str, Any]) -> AdDetail | None:
    ad = _val((data or {}).get(AD_NS, data))
    if not isinstance(ad, dict) or not _val(ad.get("id")):
        return None
    c = _common(ad)
    seller = _text(ad.get("contact-name")) or _text((ad.get("contact") or {}).get("name"))
    return AdDetail(ad_id=c["ad_id"], heading=c["heading"], description=c["description"], price=c["price"],
                    price_display=c["price_display"], status=c["status"], published_at=c["published_at"],
                    first_published_at=c["published_at"], end_date=None, url=c["url"], images=c["images"],
                    location=c["location"], seller_name=seller, is_private=c["is_private"],
                    category_path=[c["category_name"]] if c["category_name"] else [], category_id=c["category_id"],
                    is_vb=c["vb"], platform="kleinanzeigen", item_condition=c["item_condition"],
                    item_condition_label=c["item_condition_label"], shipping=c["shipping"])


def parse_locations(data: dict[str, Any]) -> list[tuple[int, str]]:
    """(id, name) of every place in the answer, bigger areas first (Berlin before its districts)."""
    root = _val((data or {}).get(LOCATIONS_NS)) or {}
    out: list[tuple[int, str]] = []

    def walk(nodes: Any) -> None:
        for n in _many(nodes):
            if not isinstance(n, dict):
                continue
            lid, name = _val(n.get("id")), _text(n.get("localized-name")) or _text(n.get("id-name"))
            if lid not in (None, "") and name:
                try:
                    out.append((int(lid), name))
                except ValueError:
                    pass
            walk(n.get("location"))
    walk(root.get("location") if isinstance(root, dict) else None)
    return out


# ------------------------------------------------------------------ the client
def api_auth() -> tuple[str, str]:
    """("user:pw", where it comes from: "env" | "settings" | "") for the API. KLEINANZEIGEN_API_AUTH in .env wins
    over Settings → Platforms. The credentials are the Kleinanzeigen app's own (public, distribution values, not
    personal secrets); none are bundled, so one must be set or the agents cannot run."""
    if env := get_settings().kleinanzeigen_api_auth.strip():
        return env, "env"
    try:
        from app.db import get_setting
        stored = (get_setting("kleinanzeigen_api_auth") or "").strip()
    except Exception:  # noqa: BLE001 - no database yet
        stored = ""
    return (stored, "settings") if stored else ("", "")


def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.endswith("/ads.json"):
        return "search"
    if re.search(r"/ads/\d+\.json$", path):
        return "detail"
    if "/s-anzeige/" in path:
        return "detail"
    if path.startswith("/s-"):
        return "search"
    return "other"


_limiter: RateLimiter | None = None


def kleinanzeigen_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("kleinanzeigen"), platform_per_hour("kleinanzeigen"), name="Kleinanzeigen")
    return _limiter


class KleinanzeigenClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None, auth: str | None = None) -> None:
        self.app_id = f"{uuid.uuid4().hex}{int(time.time() * 1000)}"     # the app's install id, once per client
        token = base64.b64encode((auth or api_auth()[0]).encode()).decode()
        headers = {"Authorization": f"Basic {token}", "X-EBAYK-APP": self.app_id,
                   "X-ECG-USER-AGENT": f"ebayk-android-app-{APP_VERSION}", "X-ECG-USER-VERSION": APP_VERSION,
                   "User-Agent": f"Kleinanzeigen/{APP_VERSION} (Android 13; Pixel 7)",
                   "Accept": "application/json", "Accept-Language": "de-DE"}
        super().__init__("kleinanzeigen", kleinanzeigen_limiter(), name="Kleinanzeigen", headers=headers,
                         transport=transport, backend="curl")
        self.headers = headers

    def classify(self, url: str) -> str:
        return classify(url)

    def _exit_blocked(self, r: FetchResponse) -> bool:
        """Through the VPN: Akamai's HTML 403 or a 429 refuse the exit; the API's own 401/403 (JSON) means the app
        credentials and stays an error (_check_status). Not seen live yet."""
        if r.status_code == 429:
            return True
        return r.status_code == 403 and ("text/html" in r.header("content-type").lower()
                                         or "akamai" in r.header("server").lower())

    def _check_status(self, r: FetchResponse) -> None:
        if r.status_code in (401, 403):          # the app credentials were rotated, not a block of this IP
            raise NotTheExit(t("Kleinanzeigen refused the app credentials (HTTP {status}); enter current ones in "
                               "Settings → Platforms", status=r.status_code))
        super()._check_status(r)

    async def search_ads(self, params: dict[str, Any], *, cache_s: float = 0) -> SearchPage:
        r = await self._get(f"{API}/ads.json", params={k: v for k, v in params.items() if v not in (None, "")},
                            headers=self.headers, cache_s=cache_s)
        if r.status_code != 200:
            raise PlatformError(t("Kleinanzeigen search returned HTTP {status}", status=r.status_code))
        try:
            return parse_ads(r.json())
        except ValueError as e:
            raise PlatformError(t("Kleinanzeigen search returned no JSON")) from e

    async def get_ad(self, ad_id: str) -> AdDetail | None:
        r = await self._get(f"{API}/ads/{ad_id}.json", headers=self.headers, cache_s=300)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise PlatformError(t("Kleinanzeigen ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        try:
            return parse_ad(r.json())
        except ValueError as e:
            raise PlatformError(t("Kleinanzeigen ad {id}: no JSON", id=ad_id)) from e

    async def location_id(self, place: str) -> int | None:
        """The API's id of a place name ("berlin"), the biggest match first; None when unknown. Cached for a day."""
        key = place.strip().lower()
        hit = _locations.get(key)
        if hit and time.monotonic() - hit[0] < LOCATION_CACHE_S:
            return hit[1]
        r = await self._get(f"{API}/locations.json", params={"q": key.replace("-", " ")}, headers=self.headers,
                            kind="other", cache_s=3600)
        found = parse_locations(r.json()) if r.status_code == 200 else []
        lid = found[0][0] if found else None
        _locations[key] = (time.monotonic(), lid)
        return lid


_client: KleinanzeigenClient | None = None


def get_client() -> KleinanzeigenClient:
    global _client
    if _client is None:
        if not api_auth()[0]:
            raise PlatformError(t("No Kleinanzeigen app credentials – set KLEINANZEIGEN_API_AUTH in .env or enter "
                                  "them in Settings → Platforms"))
        _client = KleinanzeigenClient()
    return _client


def set_client(client: KleinanzeigenClient | None) -> None:
    """Tests: a client with a fake transport. Settings: None after new credentials, so the next call rebuilds it."""
    global _client
    _client = client


# ------------------------------------------------------------------ the adapter
# the last path segment: k0 (any category) and/or c<category>, then the place l<id> and the radius r<km>
_FINAL = re.compile(r"^(?P<k>k0)?(?:c(?P<cat>\d+))?(?:l(?P<loc>\d+))?(?:r(?P<radius>\d+))?$")


def _slug(text: str) -> str:
    s = text.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return quote(s) or "anzeigen"


def _num(v: str) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        raise InvalidSearchUrl(t("Price in the link is not a number")) from None


class KleinanzeigenPlatform(Platform):
    id = "kleinanzeigen"
    name = "Kleinanzeigen"
    host = "kleinanzeigen.de"
    api_host = "api.kleinanzeigen.de"
    home_country = "DE"
    hosts = ("kleinanzeigen.de",)
    image_hosts = ("kleinanzeigen.de", "ebayimg.com")
    locale = "de-DE"
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})
    PAGE_SIZE_MAX = MAX_ROWS
    CAN_PAGE = True
    access = "http"
    min_interval_s = 180                      # the API caches searches for about two minutes anyway

    @property
    def limiter(self) -> RateLimiter:
        return kleinanzeigen_limiter()

    @property
    def client(self) -> KleinanzeigenClient:
        return get_client()

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a kleinanzeigen.de link"))
        segs = [unquote(s) for s in u.path.split("/") if s]
        if not segs or not segs[0].startswith("s-"):
            raise InvalidSearchUrl(t("Path must start with /s- (copy a result list from kleinanzeigen.de)"))
        if segs[0] == "s-anzeige":
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        # every option segment carries its own "s-" (/s-preis:10:100/s-seite:2/…), the words in between do not
        segs = [seg[2:] if seg.startswith("s-") and (i == 0 or ":" in seg) else seg for i, seg in enumerate(segs)]
        m = _FINAL.match(segs[-1]) if len(segs) >= 1 else None
        if not m or not (m.group("k") or m.group("cat")):
            raise InvalidSearchUrl(t("Not a kleinanzeigen.de result list (the link should end in k0 or c<number>)"))
        price_from = price_to = None
        plain: list[str] = []
        for seg in segs[:-1]:
            key, _, rest = seg.partition(":")
            if key == "preis":
                a, _, b = rest.partition(":")
                price_from, price_to = _num(a), _num(b)
            elif key in ("seite", "sortierung", "anzeige"):
                continue                                             # page, sort and offers/wanted: not the search
            elif ":" in seg:
                continue                                             # other option segments the site may add
            elif seg:
                plain.append(seg)
        category = m.group("cat")
        location_id = int(m.group("loc")) if m.group("loc") else None
        radius = int(m.group("radius")) if m.group("radius") else None
        # the site writes /s-<category>/<place>/<keyword>/…: the category slug comes with c<id>, a place with l<id>
        # (or, typed by hand, as the first of two words)
        category_slug = plain.pop(0) if category and plain else None
        location = plain.pop(0) if plain and (location_id or len(plain) >= 2) else None
        keyword = plain[-1].replace("-", " ") if plain else None
        extra: dict[str, Any] = {}
        if category_slug:
            extra["category_slug"] = category_slug
        if location_id:
            extra["location_id"] = location_id
        if radius:
            extra["radius_km"] = radius
        spec = SearchSpec(platform=self.id, url="", keyword=keyword, price_from=price_from, price_to=price_to,
                          category=category, location=location, extra=extra)
        spec.url = self._url(spec)
        return spec

    def _url(self, spec: SearchSpec) -> str:
        parts = []
        if spec.price_from is not None or spec.price_to is not None:
            parts.append(f"preis:{spec.price_from:g}" if spec.price_from is not None else "preis:")
            parts[-1] += f":{spec.price_to:g}" if spec.price_to is not None else ":"
        if spec.extra.get("category_slug"):
            parts.append(_slug(spec.extra["category_slug"]))
        if spec.location:
            parts.append(_slug(spec.location))
        if spec.keyword:
            parts.append(_slug(spec.keyword))
        final = ("k0" if spec.keyword or not spec.category else "") + (f"c{spec.category}" if spec.category else "")
        if spec.extra.get("location_id"):
            final += f"l{spec.extra['location_id']}"
        if spec.extra.get("radius_km"):
            final += f"r{spec.extra['radius_km']}"
        parts.append(final)
        return f"{WEB}/s-" + "/".join(parts)

    def describe(self, spec: SearchSpec) -> str:
        if spec.keyword:
            return spec.keyword
        if spec.extra.get("category_slug"):
            return spec.extra["category_slug"].replace("-", " ").title()
        return spec.location.replace("-", " ").title() if spec.location else t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          category=category, sort=sort)
        spec.url = self._url(spec)
        return spec

    async def _params(self, spec: SearchSpec, rows: int) -> dict[str, Any]:
        location_id = spec.extra.get("location_id")
        if spec.location and not location_id:
            location_id = await self.client.location_id(spec.location)
        return {"q": spec.keyword, "minPrice": f"{spec.price_from:g}" if spec.price_from is not None else None,
                "maxPrice": f"{spec.price_to:g}" if spec.price_to is not None else None,
                "categoryId": spec.category, "locationId": location_id,
                "distance": spec.extra.get("radius_km") if location_id else None,
                "sortType": SORTS.get(spec.sort, "DATE_DESCENDING"), "adType": "OFFERED",
                "page": 0, "size": min(rows, MAX_ROWS), "pictureRequired": "false"}

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        params = await self._params(spec, rows or get_settings().search_rows)
        params["page"] = max(page, 1) - 1          # the API's pages count from 0
        page = await self.client.search_ads(params, cache_s=cache_s)
        page.title = spec.keyword
        return page

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        return await self.client.get_ad(ad_id)

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/s-anzeige/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/s-anzeige/(?:[^/?#]+/)?(\d{6,})(?:-\d+-\d+)?/?(?:[?#]|$)", url) or re.search(r"/api/ads/(\d+)\.json", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None

    def status(self) -> dict[str, Any]:
        return {**super().status(), "auth_source": api_auth()[1]}


__all__ = ["KleinanzeigenClient", "KleinanzeigenPlatform", "get_client", "set_client", "parse_ads", "parse_ad",
           "parse_listing", "parse_locations", "json"]
