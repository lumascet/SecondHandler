"""Parsing of willhaben JSON (search results, ad detail) and URL conversion.

All shapes verified against live responses on 2026-10-09 (see tests/fixtures).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.i18n import t
from app.platforms import condition
from app.platforms.base import InvalidSearchUrl  # noqa: F401 - re-exported: the app's search-URL error lives here too
from app.platforms.text import clean_text, parse_iso  # noqa: F401 - clean_text is imported from here by other modules
from app.platforms.types import (  # noqa: F401 - the neutral shapes; AdSummary is the old name of Listing
    AdDetail,
    FilterValue,
    Listing,
    SearchFilter,
    SearchFilters,
    SearchPage,
)

AdSummary = Listing
_iso = parse_iso

BASE = "https://www.willhaben.at"
SEARCH_PREFIX = "/webapi/ad-search/search/atz/seo/"
# Query params the web app adds for its own bookkeeping; dropped so the agent URL is stable.
_VOLATILE_PARAMS = {"page", "sfId", "isNavigation", "publishedDateTo", "rows", "sort", "isLog"}


def attrs(obj: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten willhaben's `attributes.attribute[{name, values}]` list into a dict."""
    out: dict[str, list[str]] = {}
    for a in (obj.get("attributes") or {}).get("attribute") or []:
        out[a.get("name", "")] = list(a.get("values") or [])
    return out


def _first(a: dict[str, list[str]], key: str) -> str | None:
    v = a.get(key)
    return v[0] if v else None


def _float(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _ms(v: str | None) -> datetime | None:
    """willhaben epoch-millisecond strings -> aware UTC datetime."""
    f = _float(v)
    if f is None:
        return None
    return datetime.fromtimestamp(f / 1000, tz=timezone.utc)


def ad_url(seo_url: str | None, ad_id: str) -> str:
    if seo_url:
        return f"{BASE}/iad/{seo_url.lstrip('/')}"
    return f"{BASE}/iad/object?adId={ad_id}"


def parse_summary(ad: dict[str, Any]) -> AdSummary:
    a = attrs(ad)
    ad_id = str(ad.get("id") or _first(a, "ADID") or "")
    images = (ad.get("advertImageList") or {}).get("advertImage") or []
    image = images[0].get("mainImageUrl") if images else None
    if not image and _first(a, "MMO"):
        image = f"https://cache.willhaben.at/mmo/{_first(a, 'MMO')}"
    price_raw = _first(a, "PRICE/AMOUNT") or _first(a, "PRICE")
    private = _first(a, "ISPRIVATE")
    item_condition, item_condition_label = condition.willhaben_list(_first(a, "categorytreeattributeids"))
    return AdSummary(
        ad_id=ad_id,
        heading=_first(a, "HEADING") or ad.get("description") or "",
        body=clean_text(_first(a, "BODY_DYN")),
        price=_float(price_raw),
        price_display=_first(a, "PRICE_FOR_DISPLAY"),
        old_price=_float(_first(a, "OLD_PRICE")),
        location=_first(a, "LOCATION"),
        postcode=_first(a, "POSTCODE"),
        state=_first(a, "STATE"),
        published_at=_ms(_first(a, "PUBLISHED")),
        changed_at=_ms(_first(a, "CHANGED")),
        end_date=_ms(_first(a, "ENDDATE")),
        url=ad_url(_first(a, "SEO_URL"), ad_id),
        image_url=image,
        is_private=None if private is None else private == "1",
        is_bumped=_first(a, "IS_BUMPED") == "1",
        status=((ad.get("advertStatus") or {}).get("id")) or "unknown",
        category_ids=[c for c in (_first(a, "categorytreeids") or "").split(";") if c],
        platform="willhaben",
        item_condition=item_condition,
        item_condition_label=item_condition_label,
        # PayLivery on: the seller ships (its price is on the ad page only); off says nothing (shipping by arrangement)
        shipping="ship" if (_first(a, "p2penabled") or "").lower() == "true" else None,
        paylivery={"true": True, "false": False}.get((_first(a, "p2penabled") or "").lower()),
        **_coordinates(_first(a, "COORDINATES")),
    )


def _coordinates(raw: str | None) -> dict[str, float]:
    """COORDINATES "48.2,16.37" -> {lat, lon}; nothing for junk or an empty value."""
    try:
        lat, lon = (float(x) for x in (raw or "").split(","))
    except ValueError:
        return {}
    return {"lat": lat, "lon": lon} if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon) else {}


def parse_search(data: dict[str, Any]) -> SearchPage:
    if not isinstance(data, dict) or ("advertSummaryList" not in data and "rowsFound" not in data):
        # not a result list at all (a changed answer, an error page as JSON): an error, never "nothing new"
        from app.platforms.base import PlatformError
        raise PlatformError(t("willhaben answered the search without a result list"))
    items = ((data.get("advertSummaryList") or {}).get("advertSummary")) or []
    return SearchPage(
        rows_found=int(data.get("rowsFound") or 0),
        ads=[parse_summary(x) for x in items],
        title=data.get("searchTitle") or data.get("heading"),
    )


def _flag(*values: Any) -> bool:
    """First set value of a willhaben flag ("true"/"false" as text, or a one-item list of it) is true."""
    for v in values:
        v = v[0] if isinstance(v, list) and v else v
        if v is not None:
            return str(v).lower() == "true"
    return False


def _price_from_display(v: str | None) -> float | None:
    """'€ 2.400' / '€ 1.234,50' -> number, for detail pages that carry the price only as display text."""
    if not v:
        return None
    m = re.search(r"\d[\d.]*(?:,\d{1,2})?", v)
    return _float(m.group(0).replace(".", "").replace(",", ".")) if m else None


def _money_value(v: Any) -> float | None:
    """A price as willhaben sends it: a number, or a text such as "4,90", "4,90 €" or "1.234,50"."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        s = re.sub(r"[€\s]|EUR", "", v)
        if "," in s:                                  # German decimal comma (dots are thousands separators)
            s = s.replace(".", "").replace(",", ".")
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _handover(ad: dict[str, Any]) -> str | None:
    """The ad page's "Übergabe" (advertisingParameters "Uebergabe": ["Selbstabholung", "Versand"]): "ship" when it
    names shipping or PayLivery is on, "pickup" when it names pickup only, else None (not stated)."""
    params = ad.get("advertisingParameters") or {}
    raw = params.get("Uebergabe") or []
    words = {str(w).strip().lower() for w in (raw if isinstance(raw, list) else [raw])}
    if "versand" in words or _flag(params.get("payliveryEnabled")):
        return "ship"
    if "selbstabholung" in words:
        return "pickup"
    return None


def shipping_options(p2pp: Any) -> list[dict[str, Any]]:
    """PayLivery's delivery options: [{name, home, point}]: `home` the price to the door (deliveryOptions[].price),
    `point` the one to a pickup point or locker (depositoryPrice, often lower: willhaben's "Versand ab € 3,49")."""
    if not isinstance(p2pp, dict):
        return []
    opts = p2pp.get("deliveryOptions")
    if isinstance(opts, dict):
        opts = [opts]
    out = []
    for o in opts or []:
        if not isinstance(o, dict):
            continue
        home, point = _money_value(o.get("price")), _money_value(o.get("depositoryPrice"))
        home = home if home is not None and home >= 0 else None
        point = point if point is not None and point >= 0 else None
        if home is None and point is None:
            continue
        out.append({"name": str(o.get("optionName") or (o.get("carrier") or {}).get("description") or ""),
                    "home": home, "point": point})
    return out


def _shipping_price(p2pp: Any) -> float | None:
    """Cheapest PayLivery delivery price (to a pickup point or the door, as willhaben's "Versand ab"); None if none."""
    prices = [v for o in shipping_options(p2pp) for v in (o["home"], o["point"]) if v is not None]
    return min(prices) if prices else None


def parse_detail(page_props: dict[str, Any]) -> AdDetail | None:
    ad = page_props.get("advertDetails")
    if not ad:
        return None
    a = attrs(ad)
    images = [
        i.get("mainImageUrl")
        for i in ((ad.get("advertImageList") or {}).get("advertImage") or [])
        if i.get("mainImageUrl")
    ]
    addr = ad.get("advertAddressDetails") or {}
    seller = ad.get("sellerProfileUserData") or {}
    crumbs = [c.get("displayName") for c in (ad.get("breadcrumbs") or [])][2:]
    price_raw = _first(a, "PRICE/AMOUNT") or _first(a, "PRICE")
    canonical = (ad.get("seoMetaData") or {}).get("canonicalUrl")
    private = _first(a, "ISPRIVATE")
    location = ", ".join(x for x in [addr.get("postCode"), addr.get("postalName"), addr.get("province")] if x)
    # postalName can be a part of the town ("Bergerviertel"), municipality is the town itself ("Sankt Gallen")
    town = " ".join(x for x in [addr.get("postCode"), addr.get("municipality") or addr.get("postalName")] if x)
    place = ", ".join(x for x in [town, addr.get("country") or ("Österreich" if town else None)] if x)
    item_condition, item_condition_label = condition.willhaben_detail(ad)
    return AdDetail(
        ad_id=str(ad.get("id")),
        heading=ad.get("description") or "",
        description=clean_text(_first(a, "DESCRIPTION")),
        price=_float(price_raw) if _float(price_raw) is not None else _price_from_display(_first(a, "PRICE_FOR_DISPLAY")),
        price_display=_first(a, "PRICE_FOR_DISPLAY"),
        status=((ad.get("advertStatus") or {}).get("id")) or "unknown",
        published_at=_iso(ad.get("publishedDate")),
        first_published_at=_iso(ad.get("firstPublishedDate")),
        end_date=_iso(ad.get("endDate")),
        url=canonical or ad_url(None, str(ad.get("id"))),
        images=images,
        location=location or None,
        seller_name=seller.get("name"),
        seller_login_id=str(ad.get("loginId")) if ad.get("loginId") is not None else None,
        seller_uuid=ad.get("loginUUid"),
        ad_uuid=ad.get("uuid"),
        seller_since=_iso(seller.get("registerDate")),
        is_private=None if private is None else private == "1",
        chat_enabled=ad.get("chatEnabled"),
        category_path=[c for c in crumbs if c],
        edit_actions=ad.get("advertEditStatusActionsList"),
        category_id=str(ad["categoryTreeId"]) if ad.get("categoryTreeId") is not None else None,
        paylivery_enabled=_flag((ad.get("advertisingParameters") or {}).get("payliveryEnabled"),
                                _first(a, "payliveryEnabled")),
        buy_now=bool(((ad.get("p2ppOptions") or {}).get("buyNowInformationDto") or {}).get("isBuyNowAd")),
        shipping_price=_shipping_price(ad.get("p2ppOptions")),
        shipping_options=shipping_options(ad.get("p2ppOptions")) or None,
        shipping=_handover(ad),
        place=place or None,
        item_condition=item_condition,
        item_condition_label=item_condition_label,
    )


_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def extract_next_data(html_text: str) -> dict[str, Any] | None:
    m = _NEXT_DATA_RE.search(html_text)
    return json.loads(m.group(1)) if m else None


def to_webapi_url(user_url: str, rows: int = 30) -> str:
    """Turn a willhaben result-list URL from the browser into the JSON search URL.

    https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300
    -> https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz/fahrraeder-4552?PRICE_TO=300&sort=1&rows=30
    """
    u = urlsplit(user_url.strip())
    if u.scheme not in ("http", "https") or not u.netloc.endswith("willhaben.at"):
        raise InvalidSearchUrl(t("Not a willhaben.at URL"))
    path = u.path
    if path.startswith(SEARCH_PREFIX):
        rest = path[len(SEARCH_PREFIX):]
    elif path.startswith("/iad/"):
        rest = path[len("/iad/"):]
    else:
        raise InvalidSearchUrl(t("Path must start with /iad/ (copy a result list from willhaben)"))
    rest = rest.strip("/")
    if "/d/" in f"/{rest}/" or rest.startswith("object"):
        raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
    if not rest:
        raise InvalidSearchUrl(t("Empty search path"))
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in _VOLATILE_PARAMS]
    params += [("sort", "1"), ("rows", str(rows))]
    return f"{BASE}{SEARCH_PREFIX}{rest}?{urlencode(params)}"


def keyword_search_url(keyword: str, rows: int = 30, price_from: float | None = None,
                       price_to: float | None = None, sort: int = 1,
                       path: str = "kaufen-und-verkaufen/marktplatz") -> str:
    params: list[tuple[str, str]] = [("keyword", keyword)]
    if price_from is not None:
        params.append(("PRICE_FROM", f"{price_from:g}"))
    if price_to is not None:
        params.append(("PRICE_TO", f"{price_to:g}"))
    params += [("sort", str(sort)), ("rows", str(rows))]
    return f"{BASE}{SEARCH_PREFIX}{path.strip('/')}?{urlencode(params)}"


def web_url(path_or_url: str) -> str:
    """Browser result-list URL (https://www.willhaben.at/iad/...) for any search link willhaben hands out.

    Accepts restapi links (`/search/atz/seo/...`, `ad-search.willhaben.at/restapi/v2/...`), webapi links,
    SEO links without `/iad` (`/kaufen-und-verkaufen/...`) and browser links; drops bookkeeping params.
    """
    u = urlsplit(path_or_url.strip())
    path = u.path
    for prefix in ("/restapi/v2/search/atz/seo/", "/search/atz/seo/", SEARCH_PREFIX, "/iad/", "/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in _VOLATILE_PARAMS]
    return f"{BASE}/iad/{path.strip('/')}" + (f"?{urlencode(params)}" if params else "")


def with_params(url: str, changes: dict[str, str | None]) -> str:
    """Set (or, for empty values, remove) single-valued query params of a browser search URL."""
    u = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(u.query) if k not in changes and k not in _VOLATILE_PARAMS]
    params += [(k, v) for k, v in changes.items() if v]
    return f"{BASE}{u.path}" + (f"?{urlencode(params)}" if params else "")


def url_param(url: str, name: str) -> str | None:
    return dict(parse_qsl(urlsplit(url).query)).get(name)


def _link(d: Any) -> str | None:
    if not isinstance(d, dict):
        return None
    return d.get("relativePath") or d.get("uri")


def parse_filters(data: dict[str, Any], fallback_url: str) -> SearchFilters:
    links = {c.get("id"): c for c in ((data.get("searchContextLinks") or {}).get("contextLink") or [])}
    seo = _link(links.get("resultListSeoNavigatorLink"))
    groups: list[tuple[str, list[SearchFilter]]] = []
    keyword = None
    for g in data.get("navigatorGroups") or []:
        filters = []
        for n in g.get("navigatorList") or []:
            kind = n.get("navigatorType") or "STANDARD"
            selected = []
            for v in n.get("selectedValues") or []:
                reset = _link(v.get("resetLink"))
                if reset:
                    selected.append(FilterValue(label=v.get("label") or "", url=web_url(reset)))
            if kind == "TEXT_SEARCH":   # the keyword has its own input field
                keyword = (n.get("selectedValues") or [{}])[0].get("label") if n.get("selectedValues") else None
                continue
            values = []
            for grp in n.get("groupedPossibleValues") or []:
                for v in grp.get("possibleValues") or []:
                    link = _link(v.get("webLink")) or _link(v.get("searchLink"))
                    if link:
                        values.append(FilterValue(label=v.get("label") or "", url=web_url(link), hits=v.get("hits")))
            info = n.get("urlConstructionInformation") or {}
            range_params = [p["urlParameterName"] for p in info.get("urlParams") or [] if p.get("urlParameterName")]
            if kind == "RANGE" and not range_params:
                continue
            if kind != "RANGE" and not values and not selected:
                continue
            filters.append(SearchFilter(
                id=str(n.get("id")), label=n.get("label") or str(n.get("id")), kind=kind,
                multi=n.get("navigatorSelectionType") == "MULTI_SELECT", values=values, selected=selected,
                range_params=range_params if kind == "RANGE" else []))
        if filters:
            groups.append((g.get("label") or "", filters))
    page = parse_search(data)
    return SearchFilters(url=web_url(seo) if seo else web_url(fallback_url), rows_found=page.rows_found,
                         groups=groups, ads=page.ads, keyword=keyword)


def describe_search(user_url: str) -> str:
    """Best-effort human name for a search URL (keyword or last path segment)."""
    u = urlsplit(user_url)
    q = dict(parse_qsl(u.query))
    if q.get("keyword"):
        return q["keyword"]
    seg = [s for s in u.path.split("/") if s][-1:] or [t("search")]
    return re.sub(r"-\d+$", "", seg[0]).replace("-", " ").title()


def extract_ad_id(text: str) -> str | None:
    """Accept a bare id, an /iad/object?adId= URL or an SEO URL ending in -<id>/."""
    text = text.strip()
    if text.isdigit():
        return text
    m = re.search(r"adId=(\d+)", text) or re.search(r"-(\d{6,})/?(?:\?|$)", text)
    return m.group(1) if m else None
