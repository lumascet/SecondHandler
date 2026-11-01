"""preisjaeger.at (Austria), the deal community, as a platform adapter: deals from shops, not private listings.

preisjaeger.at and mydealz.de are white-label builds of the same Pepper ("tippingcanoe") backend, so the Android
app talks to a shared mobile REST API (https://www.preisjaeger.at/rest_api/v2/…) that returns clean JSON — no
Cloudflare-fronted HTML to scrape. `thread` lists deals for a search (one JSON object per deal: title, price, the
comparison price `next_best_price`, merchant, temperature, voucher `code`, posting time and image). As on the
website, there is no details page; the viewer shows the listing.

The app signs every request OAuth-1.0 style (HMAC-SHA1) with a consumer key/secret baked into it — group-wide
distribution values, not personal secrets (like the Kleinanzeigen app credentials the project already uses). The
bundled pair works out of the box; PREISJAEGER_OAUTH="key:secret" (.env or Settings → Platforms) overrides it if the
app rotates it. No user login. We send the oauth parameters in the Authorization header only, so the request URL
stays stable and `_get`'s response cache still works.

A search without results answers with an empty `data` list. The REST payload carries no shipping price, so shipping
stays unknown. The website ignores price limits in the link; the adapter keeps them (priceFrom/priceTo) and filters
itself.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import time
from base64 import encodebytes
from hashlib import sha1
from random import randint
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit

from app.config import get_settings
from app.i18n import t
from app.platforms import condition
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import euro, from_epoch, one_line, to_float
from app.platforms.types import Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.preisjaeger.at"
API = f"{WEB}/rest_api/v2"
STATIC = "https://static.preisjaeger.at"

# The Pepper Android app's bundled OAuth credentials (distribution values, not personal secrets) and user-agent.
OAUTH_CONSUMER_KEY = "83561944886fdab16690e78a1f5f5559846dd86d"
OAUTH_SECRET = "f0561bf4d666c1cfe942390bc2761a9ce02cedb8"
APP_UA = "com.tippingcanoe.preisjaeger ANDROID [v7.05.02] [30 | sdk_gphone_x86_64] [@2.625x]"


# ------------------------------------------------------------------ signing
def oauth_creds() -> tuple[str, str, str]:
    """(consumer_key, secret, source): the bundled app credentials, or an override from PREISJAEGER_OAUTH in .env
    ("key:secret"), or the same setting in Settings → Platforms, for when the app rotates them."""
    raw = get_settings().preisjaeger_oauth.strip()
    src = "env"
    if not raw:
        try:
            from app.db import get_setting

            raw = (get_setting("preisjaeger_oauth") or "").strip()
            src = "settings"
        except Exception:  # noqa: BLE001 - no database yet
            raw = ""
    if raw and ":" in raw:
        key, _, secret = raw.partition(":")
        return key.strip(), secret.strip(), src
    return OAUTH_CONSUMER_KEY, OAUTH_SECRET, "bundled"


def _sign(method: str, url: str, params: dict[str, str]) -> tuple[str, str]:
    """Sign `url` with the ordered query `params` and return (Authorization header, URL query string). `criteria`
    is percent-encoded once; the oauth parameters go into the signature base string and the header, never the URL,
    so the same search always yields the same URL (and cache key). Replicates the app's byte construction."""
    key, secret, _ = oauth_creds()
    hmac_key = (secret + "&").encode()                      # consumer secret + "&" + (empty) token secret
    auth = {
        "oauth_consumer_key": key,
        "oauth_nonce": str(randint(-2**63, 2**63 - 1)),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    sent = dict(params)
    if sent.get("criteria"):
        sent["criteria"] = quote(sent["criteria"])
    base_params = {**sent, **auth}
    norm = "&".join(f"{k}={v}" for k, v in sorted(base_params.items()))
    message = "&".join([method, quote_plus(url), quote_plus(norm)]).encode()
    auth["oauth_signature"] = quote_plus(encodebytes(hmac.new(hmac_key, message, sha1).digest()).decode().strip())
    header = "OAuth " + ",".join(f'{k}="{v}"' for k, v in auth.items())
    return header, "&".join(f"{k}={v}" for k, v in sent.items())


# ------------------------------------------------------------------ parsing
def _image(th: dict[str, Any]) -> str | None:
    img = th.get("image")
    if isinstance(img, dict) and (img.get("uri") or img.get("large_uri")):
        return img.get("uri") or img.get("large_uri")
    return th.get("icon_list_url") or None


def parse_listing(th: dict[str, Any]) -> Listing:
    price = to_float(th.get("price")) or None          # 0: a voucher or percentage deal without a price
    before = to_float(th.get("next_best_price"))
    pct = to_float(th.get("price_discount"))
    merchant = (th.get("merchant") or {}).get("name") or th.get("merchant_name_display")
    temp = to_float(th.get("temperature_rating"))
    group = th.get("group_display_summary") or (th.get("main_group") or {}).get("name")
    facts = [f"{temp:.0f}°" if temp is not None else None,
             t("Voucher code {code}", code=th["code"]) if th.get("code") else None,
             one_line(group),
             t("local deal") if th.get("local") else None]
    tid = str(th.get("thread_id"))
    title = one_line(th.get("title")) or ""
    return Listing(ad_id=tid, heading=title, body=" · ".join(f for f in facts if f) or None,
                   price=price, price_display=euro(price) if price else (f"−{pct:g} %" if pct else None),
                   old_price=before if before and price is not None and before > price else None,
                   location=one_line(merchant), postcode=None, state=None, published_at=from_epoch(th.get("submitted")),
                   changed_at=None, end_date=from_epoch(th.get("expiry_date")),
                   url=th.get("deal_uri") or f"{WEB}/share-deal/{tid}",
                   image_url=_image(th), is_private=None, is_bumped=None, status="active",
                   platform="preisjaeger", item_condition=condition.preisjaeger(title)[0],   # inferred: no label
                   shipping_price=None, shipping=None)                                        # the API states none


def _threads(body: str | dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    try:
        doc = json.loads(body) if isinstance(body, (str, bytes)) else body
    except ValueError:
        raise PlatformError(t("Preisjäger answered without a result list")) from None
    data = doc.get("data") if isinstance(doc, dict) else doc
    if not isinstance(data, list):
        raise PlatformError(t("Preisjäger answered without a result list"))
    return [th for th in data if isinstance(th, dict) and th.get("thread_id")]


def parse_search(body: str | dict[str, Any], price_from: float | None = None,
                 price_to: float | None = None) -> SearchPage:
    """The /rest_api/v2/thread answer -> SearchPage: live deals only, within the price limits (deals without a price
    drop out of a limited search). An empty `data` list gives an empty page."""
    ads = []
    for th in _threads(body):
        if th.get("expired") or (th.get("type") or {}).get("type_universal_id") == "discussions":
            continue
        ad = parse_listing(th)
        if price_from is not None and (ad.price is None or ad.price < price_from):
            continue
        if price_to is not None and (ad.price is None or ad.price > price_to):
            continue
        ads.append(ad)
    return SearchPage(rows_found=len(ads), ads=ads)


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/search"):
        return "search"
    if path.startswith(("/deals/", "/share-deal/")):
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def preisjaeger_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("preisjaeger"), platform_per_hour("preisjaeger"), name="Preisjäger")
    return _limiter


class PreisjaegerClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("preisjaeger", preisjaeger_limiter(), name="Preisjäger", headers={"Accept-Language": "de-AT"},
                         transport=transport, backend="curl")

    def classify(self, url: str) -> str:
        return classify(url)

    async def _signed_get(self, path: str, params: dict[str, str], *, kind: str, cache_s: float = 0):
        url = f"{API}{path}"
        header, qs = _sign("GET", url, params)
        headers = {"Authorization": header, "Accept": "application/json", "User-Agent": APP_UA}
        return await self._get(f"{url}?{qs}" if qs else url, headers=headers, cache_s=cache_s, kind=kind)

    async def search_page(self, criteria: dict[str, Any], price_from: float | None, price_to: float | None, *,
                          limit: int = 30, cache_s: float = 0) -> SearchPage:
        params = {"criteria": json.dumps(criteria, separators=(",", ":")),
                  "history_item_needed": "false", "limit": str(limit)}
        r = await self._signed_get("/thread", params, kind="search", cache_s=cache_s)
        if r.status_code != 200:
            raise PlatformError(t("Preisjäger search returned HTTP {status}", status=r.status_code))
        return parse_search(r.text, price_from, price_to)


_client: PreisjaegerClient | None = None


def get_client() -> PreisjaegerClient:
    global _client
    if _client is None:
        _client = PreisjaegerClient()
    return _client


def set_client(client: PreisjaegerClient | None) -> None:
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


class PreisjaegerPlatform(Platform):
    id = "preisjaeger"
    name = "Preisjäger"
    host = "preisjaeger.at"
    api_host = "www.preisjaeger.at"
    home_country = "AT"
    hosts = ("preisjaeger.at",)
    image_hosts = ("static.preisjaeger.at",)
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "images"})
    PAGE_SIZE_MAX = 30          # one result page of the deal list
    CAN_PAGE = False            # the mobile API pages by cursor, not page number; one page is plenty for a deal feed
    access = "http"
    min_interval_s = 300                      # deals come in slowly; the community needs minutes to post them

    @property
    def limiter(self) -> RateLimiter:
        return preisjaeger_limiter()

    @property
    def client(self) -> PreisjaegerClient:
        return get_client()

    def about(self) -> str:
        return t("Deals from shops that the Preisjäger community posts: new goods with price, comparison price, shop "
                 "and temperature. No private sellers and no details page; the shop is shown where other sites show "
                 "the place.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not a preisjaeger.at link"))
        if u.path.startswith(("/deals/", "/share-deal/")):
            raise InvalidSearchUrl(t("That is a deal link, not a search"))
        if u.path.rstrip("/") != "/search":
            raise InvalidSearchUrl(t("Not a Preisjäger search (copy the link of a search: …/search?q=…)"))
        qs = parse_qs(u.query)
        keyword = one_line((qs.get("q") or [""])[0])
        if not keyword:
            raise InvalidSearchUrl(t("The Preisjäger link has no search term (q=…)"))
        return self.keyword_search(keyword, _num((qs.get("priceFrom") or [None])[0]), _num((qs.get("priceTo") or [None])[0]))

    def _params(self, spec: SearchSpec, sort: str = "newest") -> dict[str, str]:
        params = {"q": spec.keyword or ""}
        if sort != "relevance":
            params["sortBy"] = "new"
        params["hide_expired"] = "true"
        return params

    def _url(self, spec: SearchSpec) -> str:
        params = list(self._params(spec).items())
        if spec.price_from is not None:
            params.append(("priceFrom", f"{spec.price_from:g}"))
        if spec.price_to is not None:
            params.append(("priceTo", f"{spec.price_to:g}"))
        return f"{WEB}/search?{urlencode(params)}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          sort=sort)
        spec.url = self._url(spec)
        return spec

    def _criteria(self, spec: SearchSpec) -> dict[str, Any]:
        return {"query": spec.keyword or "", "tab": "new", "only_discussions": False, "only_vouchers": False,
                "only_non_expired": True, "whereabouts": "search"}

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        want = rows or get_settings().search_rows
        if page > 1:                                     # CAN_PAGE is False: the deal feed is a single page
            return SearchPage(rows_found=0, ads=[], title=spec.keyword)
        limit = min(want, self.PAGE_SIZE_MAX)
        result = await self.client.search_page(self._criteria(spec), spec.price_from, spec.price_to,
                                               limit=limit, cache_s=cache_s)
        result.ads = result.ads[:want]
        result.title = spec.keyword
        return result

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/share-deal/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/deals/[^/?#]*?-(\d{3,})/?(?:[?#]|$)", url) or re.search(r"/share-deal/(\d{3,})", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    def status(self) -> dict[str, Any]:
        return {**super().status(), "auth_source": oauth_creds()[2]}

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["PreisjaegerClient", "PreisjaegerPlatform", "get_client", "set_client", "parse_search", "parse_listing",
           "oauth_creds"]
