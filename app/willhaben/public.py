"""Anonymous willhaben access: search JSON and ad detail pages.

Every request goes through one shared RateLimiter so all jobs together stay polite.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings
from app.i18n import t
from app.platforms import httpclient
from app.platforms.base import Blocked as PlatformBlocked
from app.platforms.base import PlatformError
from app.platforms.ratelimit import RateLimiter  # noqa: F401 - re-exported, tests and geizhals import it from here
from app.willhaben.parse import (
    AdDetail,
    SearchFilters,
    SearchPage,
    extract_next_data,
    keyword_search_url,
    parse_detail,
    parse_filters,
    parse_search,
    to_webapi_url,
)

log = logging.getLogger(__name__)

WEB_CLIENT = "api@willhaben.at;responsive_web;server;1.0.0;desktop"


# The errors are the neutral platform errors (app/platforms/base.py): every `except WillhabenError` also catches
# what the shared limiter and the other adapters raise.
WillhabenError = PlatformError
Blocked = PlatformBlocked


_limiter: RateLimiter | None = None


def willhaben_limiter() -> RateLimiter:
    """The one limiter for every willhaben request, anonymous or via the logged-in browser (same home IP)."""
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour   # changed in Settings (own or all platforms)

        _limiter = RateLimiter(platform_min_gap_s("willhaben"), platform_per_hour("willhaben"), name="willhaben")
    return _limiter


NOT_FOUND_CACHE_S = httpclient.NOT_FOUND_CACHE_S
ANON_SESSION_S = httpclient.ANON_SESSION_S


class PublicClient(httpclient.HttpPlatformClient):
    """The anonymous willhaben visitor: search JSON, filters and ad detail pages (app/platforms/httpclient.py does
    the pacing, caching, recording and block handling).

    Deliberately separate from the logged-in browser session (own cookie jar, never the account's cookies): search
    agents, screening, price checks and the ad viewer must not be linkable to the owner's willhaben account. The jar
    is reset regularly and after a block -> a fresh anonymous visitor.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        s = get_settings()
        super().__init__("willhaben", willhaben_limiter(), name="willhaben",
                         headers={"user-agent": s.user_agent, "accept-language": "de-AT,de;q=0.9,en;q=0.5"},
                         httpx_transport=transport)

    async def _get(self, url: str, *, json_api: bool, cache_s: float = 0, kind: str | None = None,  # type: ignore[override]
                   refresh: bool = False) -> httpclient.FetchResponse:
        """GET through the shared limiter; `cache_s` > 0 reuses a recent 200/404 answer for the same URL (not with
        `refresh`)."""
        tunnel = self._tunnel()
        route = tunnel.current() if tunnel is not None else None
        if json_api and self._lane() is None and (tunnel is None or route is not None):
            # the app API's search: same answer, an anonymous app identity of its own – one per VPN server, its app
            # token fetched through that server (never at home for a server). Parallel lanes: the web search.
            from app.willhaben import native

            if alt := await native.anon_route(url, route):
                app_url, app_headers = alt
                r = await super()._get(app_url, headers=app_headers, cache_s=cache_s, kind=kind or self.classify(url),
                                       refresh=refresh)
                if r.status_code == 200:
                    return r
                log.info("willhaben app search: HTTP %s, the web answers", r.status_code)
        headers = {"accept": "application/json", "x-wh-client": WEB_CLIENT} if json_api else {"accept": "text/html"}
        return await super()._get(url, headers=headers, cache_s=cache_s, kind=kind, refresh=refresh)

    async def search(self, webapi_url: str, cache_s: float = 0) -> SearchPage:
        """Search results; agents poll uncached, one-off lookups may reuse a recent answer."""
        r = await self._get(webapi_url, json_api=True, cache_s=cache_s)
        if r.status_code != 200:
            raise WillhabenError(t("Search returned HTTP {status}", status=r.status_code))
        try:
            return parse_search(r.json())
        except ValueError as e:
            raise WillhabenError(t("Search returned no JSON")) from e

    async def search_filters(self, browser_url: str, rows: int = 6) -> SearchFilters:
        """Current search plus its available filters (willhaben "navigators") for the agent builder."""
        r = await self._get(to_webapi_url(browser_url, rows=rows), json_api=True, cache_s=120, kind="filters")
        if r.status_code != 200:
            raise WillhabenError(t("Search returned HTTP {status}", status=r.status_code))
        try:
            return parse_filters(r.json(), browser_url)
        except ValueError as e:
            raise WillhabenError(t("Search returned no JSON")) from e

    async def search_keyword(self, keyword: str, rows: int = 30, price_from: float | None = None,
                             price_to: float | None = None, sort: int = 1) -> SearchPage:
        return await self.search(keyword_search_url(keyword, rows, price_from, price_to, sort), cache_s=300)

    async def get_detail(self, ad_id: str, refresh: bool = False) -> AdDetail | None:
        """Full ad detail; None if the ad is gone (expired, deleted, sold). refresh: not the copy kept for 5 minutes."""
        r = await self._get(f"https://www.willhaben.at/iad/object?adId={ad_id}", json_api=False, cache_s=300,
                            refresh=refresh)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise WillhabenError(t("Ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        nd = extract_next_data(r.text)
        if not nd:
            raise WillhabenError(t("Ad {id}: no __NEXT_DATA__ found", id=ad_id))
        if nd.get("page") == "/404":
            return None
        return parse_detail((nd.get("props") or {}).get("pageProps") or {})


    async def member_place(self, ad_uuid: str, user_uuid: str) -> str | None:
        """Where someone is, from their public profile page (the one the chat links, /iad/advert/<ad uuid>/seller/
        <user uuid>/profile): its location, else the place most of their ads are in; None when it says nothing."""
        return (await self.member_profile(ad_uuid, user_uuid))["place"]

    async def member_profile(self, ad_uuid: str, user_uuid: str) -> dict[str, Any]:
        """Someone's public profile page (cached a day): parse_member_profile."""
        r = await self._get(f"https://www.willhaben.at/iad/advert/{ad_uuid}/seller/{user_uuid}/profile", json_api=False,
                            cache_s=86400, kind="detail")
        if r.status_code != 200:
            raise WillhabenError(t("Profile: HTTP {status}", status=r.status_code))
        return parse_member_profile(extract_next_data(r.text) or {})


def parse_member_profile(nd: dict) -> dict[str, Any]:
    """{place, since: when the account was created, ratings: PayLivery ratings received, score: their overall score
    (scale not documented: 1 seen with 9 ratings), reply_time: "Antwortet meistens innerhalb von 10 Std."} of a
    profile page (pageProps.sellerProfileWrapper.sellerProfile, pageProps.trustSignals); None where it says nothing."""
    page = (nd.get("props") or {}).get("pageProps") or {}
    profile = ((page.get("sellerProfileWrapper") or {}).get("sellerProfile")) or {}
    trust = page.get("trustSignals") or {}
    feedback = ((trust.get("reputation") or {}).get("feedback")) or {}
    count = feedback.get("receivedCount")
    score = feedback.get("overallScore")
    since = profile.get("registerDate")
    return {"place": parse_member_place(nd),
            "since": datetime.fromisoformat(since) if isinstance(since, str) and since else None,
            # a profile without any rating shows trust signals without a count: none received (no trust signals at all:
            # the page changed, unknown)
            "ratings": count if isinstance(count, int) else (0 if isinstance(page.get("trustSignals"), dict) else None),
            "score": float(score) if isinstance(score, (int, float)) else None,
            "reply_time": trust.get("replyTime") if isinstance(trust.get("replyTime"), str) else None}


def parse_member_place(nd: dict) -> str | None:
    """sellerProfile.location ("8530 Deutschlandsberg"), else its district, else their ads' most common place."""
    page = (nd.get("props") or {}).get("pageProps") or {}
    profile = ((page.get("sellerProfileWrapper") or {}).get("sellerProfile")) or {}
    for key in ("location", "district"):
        if isinstance(profile.get(key), str) and profile[key].strip():
            return profile[key].strip()
    places: dict[str, int] = {}
    ads = (((page.get("searchResult") or {}).get("advertSummaryList") or {}).get("advertSummary")) or []
    for ad in ads:
        attrs = {a.get("name"): (a.get("values") or [None])[0] for a in ((ad.get("attributes") or {}).get("attribute") or [])
                 if isinstance(a, dict)}
        place = " ".join(x for x in (attrs.get("POSTCODE"), attrs.get("LOCATION")) if x)
        if place:
            places[place] = places.get(place, 0) + 1
    return max(places, key=places.get) if places else None


_client: PublicClient | None = None


def get_public() -> PublicClient:
    global _client
    if _client is None:
        _client = PublicClient()
    return _client


def set_public(client: PublicClient | None) -> None:
    global _client
    _client = client
