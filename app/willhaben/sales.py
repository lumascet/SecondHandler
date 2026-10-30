"""Mark own ads as sold / reserved on willhaben, the way the my-adverts page does it.

Read in willhaben's public bundle /bbx-adin, page chunk pages/iad/myprofile/myadverts (buildId 1058731-20d7e775,
2026-10-09), helper module 60775 + fetch wrapper 43396 (browser prefix "/webapi"):

  Every action is a context link of the ad in the my-adverts list
  (advertEditStatusActionsList.contextLinkList.contextLink[] = {id, serviceName, relativePath}); the page calls
  fetch("/webapi/" + serviceName + relativePath) with credentials, x-wh-client and the CSRF header:

    setAdSold             PUT, no body   e.g. /webapi/iad/bap/sold/{id}               "Auf verkauft setzen"
    markAdReserved        PUT, no body   e.g. /webapi/iad/bap/updatesalesinfo/{id}/5  "Als reserviert markieren"
    removeMarkAdReserved  PUT, no body   (undo "reserved")

  Before "sold", only when the ad has a non-empty `deleteReasonQueryParameter`, the dialog loads
    GET  /webapi/iad/soldreasons?adId={id}  -> {soldReasons: [{soldReasonId, description, playAnimation}],
                                                header, actionHeader, acceptButtonText, footer, banner}
  and the user has to pick one (required field); it is posted before the PUT, as a string (radio value):
    POST /webapi/iad/soldreasons?adId={id}  {"soldReasonId": "<id>"}

  "Sold" cannot be undone on willhaben (dialog footer: "Auf verkauft gesetzte Anzeigen können nicht wieder
  aktiviert oder neu veröffentlicht werden."). There is no "unsold" link.

NEEDS LIVE VERIFICATION: methods and bodies are unambiguous in the bundle and the paths are taken from the
ad's own links at call time (never built by hand), but no call has been made against willhaben yet, so the
response shapes (expected: empty or JSON) and the sold-reason list are unconfirmed.
Every function here that writes runs only after an explicit click + confirm in the dashboard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.i18n import t
from app.willhaben import native
from app.willhaben.account import AccountClient, OwnAdvert, _link_path
from app.willhaben.session import BrowserSession, NotLoggedIn

log = logging.getLogger(__name__)

SOLD, RESERVE, UNRESERVE = "setAdSold", "markAdReserved", "removeMarkAdReserved"
REASONS = "/webapi/iad/soldreasons?adId={id}"


class NotOffered(RuntimeError):
    """willhaben does not offer this action for the ad right now (no such link in the my-adverts list)."""


class SaleFailed(RuntimeError):
    """willhaben refused the status change."""


@dataclass
class SoldReason:
    id: str
    label: str


class ReasonRequired(Exception):
    """willhaben wants a sold reason first; nothing has been written yet."""

    def __init__(self, reasons: list[SoldReason]) -> None:
        super().__init__("sold reason required")
        self.reasons = reasons


def parse_sold_reasons(data: Any) -> list[SoldReason]:
    out = []
    items = data.get("soldReasons") if isinstance(data, dict) else None
    for r in items if isinstance(items, list) else []:
        if isinstance(r, dict) and r.get("soldReasonId") is not None:
            out.append(SoldReason(id=str(r["soldReasonId"]), label=str(r.get("description") or r["soldReasonId"])))
    return out


async def own_advert(s: BrowserSession, ad_id: str) -> OwnAdvert:
    """The ad as the my-adverts list shows it (read-only), with its action links."""
    ads = await AccountClient(s).my_adverts()
    ad = next((a for a in ads if a.ad_id == str(ad_id)), None)
    if ad is None:
        raise NotOffered(t("Ad {id} is not in your willhaben list (only the 50 most recently changed ads are loaded).",
                           id=ad_id))
    return ad


def action_path(ad: OwnAdvert, link_id: str) -> str:
    """/webapi path of one of the ad's action links (account._link_path, the way the web app builds it),
    with extra checks because these writes are irreversible: absolute path, no "..", plain service name."""
    link = (ad.actions or {}).get(link_id) or {}
    rel, svc = link.get("relativePath"), link.get("serviceName") or "iad"
    if not isinstance(rel, str) or not rel.startswith("/") or ".." in rel or not isinstance(svc, str) or "/" in svc:
        raise NotOffered(t("willhaben does not offer this for the ad right now (status: {status}).",
                           status=ad.status_label or ad.status))
    return _link_path(link)


def offers(ad: OwnAdvert) -> dict[str, bool]:
    acts = ad.actions or {}
    return {"sold": SOLD in acts, "reserve": RESERVE in acts, "unreserve": UNRESERVE in acts}


async def _call(s: BrowserSession, method: str, path: str, body: Any = None, *, need_json: bool = False) -> Any:
    """One willhaben call, decided on the HTTP status alone. The response bodies of the sold/reserve calls are
    not known (needs live verification), so a 2xx write counts as done whatever it returns: parsing the body
    must never turn a change willhaben already made into an error. A read (need_json) must return JSON."""
    r = await s.api(method, path, body)
    if r.status in (401, 403) and native.active() is None and await s.refresh_sso():   # as BrowserSession.api_json
        r = await s.api(method, path, body)
    if r.status in (401, 403):
        s.logged_in = False
        raise NotLoggedIn(f"{path}: HTTP {r.status}")
    if not r.ok:
        raise SaleFailed(t("willhaben refused the change: {detail}", detail=f"{method} {path}: HTTP {r.status} {r.text[:200]}"))
    try:
        return r.json()
    except ValueError as e:
        if need_json:
            raise SaleFailed(t("willhaben refused the change: {detail}", detail=f"{method} {path}: no JSON")) from e
        log.info("%s %s: HTTP %s, body is not JSON (ignored)", method, path, r.status)
        return None


async def sold_reasons(s: BrowserSession, ad_id: str) -> list[SoldReason]:
    return parse_sold_reasons(await _call(s, "GET", REASONS.format(id=int(ad_id)), need_json=True))


async def mark_sold(s: BrowserSession, ad_id: str, reason_id: str | None = None) -> OwnAdvert:
    """Set the ad to sold (irreversible on willhaben). Raises ReasonRequired before writing anything."""
    ad = await own_advert(s, ad_id)
    path = action_path(ad, SOLD)
    if ad.reasons_query:
        reasons = await sold_reasons(s, ad_id)
        if reasons:
            if reason_id is None or str(reason_id) not in {r.id for r in reasons}:
                raise ReasonRequired(reasons)
            await _call(s, "POST", REASONS.format(id=int(ad_id)), {"soldReasonId": str(reason_id)})
    await _call(s, "PUT", path)
    log.info("ad %s marked as sold on willhaben", ad_id)
    return ad


async def mark_reserved(s: BrowserSession, ad_id: str, on: bool = True) -> OwnAdvert:
    """Mark as reserved (on=True) or remove the mark (on=False)."""
    ad = await own_advert(s, ad_id)
    await _call(s, "PUT", action_path(ad, RESERVE if on else UNRESERVE))
    log.info("ad %s reserved=%s on willhaben", ad_id, on)
    return ad
