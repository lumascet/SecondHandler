"""willhaben's exact PayLivery buyer fee for one ad, read from the chat's PayLivery window.

The fee is not in the ad's data: it varies by category (4.5 % generally, 7 % seen for CPUs) and is only shown in the
window the chat opens for "Sofort kaufen" / "Anfrage senden". That window is an iframe whose address comes from
`GET /webapi/convoad/v1/chat/ad_list/<own user uuid>?ids=<adId>` → `ads[0].integrations[].button.baseHref` plus
`?itemId=<ad uuid>&partnerId=<seller uuid>&activeUserId=<own uuid>` (bbx-lead bundle, chat page).

The app opens that address in a hidden tab of the logged-in browser that blocks every request but GET/HEAD/OPTIONS
(session.read_hidden), so nothing is ever sent, offered or bought. The window goes /p2p/buynow/init → a payment
preview with an id of its own, whose data call (`/p2p/payment/preview/data/<id>`) has
paymentPreviewPriceBreakdown.buyerProtectionPrice; the page text ("willhaben-Käuferschutz € x") is the fallback.
Seen 2026-10-19: € 15 → € 2,49, € 325 → € 14,62 (4.5 %, cut off), € 420 (CPUs) → € 29,40.
Each read is put on the timeline (kind "wh", name "paylivery-fee") with the window's calls, so the window's own data
call can be wired in directly later. Results are kept for an hour per ad and price.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.services import timeline
from app.willhaben.parse import extract_next_data
from app.willhaben.public import get_public

log = logging.getLogger(__name__)

AD_LIST = "/webapi/convoad/v1/chat/ad_list/{me}?ids={ad_id}"
FEE_TEXT = r"K(?:ä|ae)uferschutz"
_AMOUNT = r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+(?:\.\d{2})?)"
_FEE_LINE = re.compile(r"K(?:ä|ae)uferschutz[^\n€]{0,60}?(?:\n\s*)?€\s*" + _AMOUNT, re.I)
_FEE_LINE_AFTER = re.compile(_AMOUNT + r"\s*€[^\n]{0,10}\n?[^\n]{0,40}K(?:ä|ae)uferschutz", re.I)
CACHE_S = 3600
_cache: dict[tuple[str, float | None], tuple[float, dict[str, Any]]] = {}


class FeeError(Exception):
    pass


def _euro(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", ".")) if "," in raw else float(raw)


def fee_from_text(text: str) -> float | None:
    """The fee from the window's text: "Käuferschutzgebühr € 29,40" (label, then amount, maybe on the next line)."""
    for line in (_FEE_LINE, _FEE_LINE_AFTER):
        if m := line.search(text or ""):
            return _euro(m.group(1))
    return None


def fee_from_bodies(bodies: dict[str, str]) -> float | None:
    """The fee from the window's data call `GET mint.willhaben.at/p2p/payment/preview/data/<preview id>`:
    paymentPreviewPriceBreakdown.buyerProtectionPrice (the preview id is made by /p2p/buynow/init when the window
    opens, so the call cannot be made without it)."""
    for url, text in (bodies or {}).items():
        if "/p2p/payment/preview/data/" not in url:
            continue
        try:
            price = (json.loads(text).get("paymentPreviewPriceBreakdown") or {}).get("buyerProtectionPrice")
            return round(float(price), 2) if price is not None else None
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def price_section(text: str) -> str:
    """The window's price part only ("Preis … willhaben-Käuferschutz € x"), without the delivery address above it."""
    i = (text or "").find("Artikelpreis")
    return text[i:i + 600] if i >= 0 else ""


def pick_integration(convoad: Any) -> dict[str, Any] | None:
    """The PayLivery window among the ad's chat integrations: "Sofort kaufen" first, else the offer."""
    ads = (convoad or {}).get("ads") if isinstance(convoad, dict) else None
    items = (ads[0].get("integrations") if ads and isinstance(ads[0], dict) else None) or []
    p2p = [i for i in items if isinstance(i, dict) and "p2p" in str(i.get("initName") or "")
           and (i.get("button") or {}).get("baseHref")]
    return next((i for i in p2p if "buynow" in str(i.get("initName"))), p2p[0] if p2p else None)


async def _ids(ad_id: str) -> tuple[str, str]:
    """(ad uuid, seller uuid) from the public ad page."""
    r = await get_public()._get(f"https://www.willhaben.at/iad/object?adId={ad_id}", json_api=False, cache_s=300)
    ad = (((extract_next_data(r.text) or {}).get("props") or {}).get("pageProps") or {}).get("advertDetails") or {}
    if not ad.get("uuid") or not ad.get("loginUUid"):
        raise FeeError("Anzeige ohne IDs für PayLivery (offline?)")
    return str(ad["uuid"]), str(ad["loginUUid"])


async def fetch(sess: Any, ad_id: str, price: float | None = None) -> dict[str, Any]:
    """{"fee": float | None, "integration": initName, "found": bool}; raises FeeError when the window cannot be
    opened (no PayLivery, not logged in)."""
    key = (str(ad_id), price)
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    me = (getattr(sess, "user", None) or {}).get("uuid")
    if not me and await sess.check_login():
        me = (sess.user or {}).get("uuid")
    if not me:
        raise FeeError("Nicht bei willhaben eingeloggt")
    item, seller = await _ids(ad_id)
    convoad = await sess.api_json("GET", AD_LIST.format(me=me, ad_id=ad_id))
    integ = pick_integration(convoad)
    if integ is None:
        timeline.record("wh", f"PayLivery-Gebühr {ad_id}: kein PayLivery-Fenster", name="paylivery-fee", ok=False,
                        blobs={"convoad": json.dumps(convoad, ensure_ascii=False)[:100_000]})
        raise FeeError("willhaben bietet für diese Anzeige kein PayLivery-Fenster an")
    url = f"{integ['button']['baseHref']}?itemId={item}&partnerId={seller}&activeUserId={me}"
    if (fee_app := await _through_app(url)) is not None:
        page = fee_app
    else:
        page = await sess.read_hidden(url, FEE_TEXT + r"[^\n€]{0,60}€\s*\d")
    fee = fee_from_bodies(page["bodies"])
    if fee is None:
        fee = fee_from_text(price_section(page["text"]) or page["text"])
    res = {"fee": fee, "integration": integ.get("initName"), "found": fee is not None}
    timeline.record("wh", f"PayLivery-Gebühr {ad_id}: " + (f"€ {fee:.2f}" if fee is not None else "nicht gefunden"),
                    name="paylivery-fee", ok=fee is not None,
                    payload={"integration": integ.get("initName"), "fee": fee, "price": price,
                             "requests": [{k: v for k, v in r.items()} for r in page["requests"]][:200]},
                    # the price part and the price data only: the window also shows the buyer's address
                    blobs={"text": price_section(page["text"]),
                           "preview": next((b for u, b in page["bodies"].items() if "/p2p/payment/preview/data/" in u),
                                           None)})
    if fee is not None:
        _cache[key] = (time.monotonic(), res)
    return res


_PREVIEW = re.compile(r"(https://[^/]+)/p2p/payment/preview/([0-9a-f-]{36})")


async def _through_app(url: str) -> dict[str, Any] | None:
    """The window read without the browser, as willhaben's app opens it in a web view: logged in with the app's
    one-time web login (native.web_pages), the redirects to the payment preview followed (GET only), then the
    preview's data call. The same shape as session.read_hidden gives; None when the app API is not there or the
    window did not end at a preview (the browser reads it then)."""
    from app.willhaben import native

    c = native.active()
    if c is None:
        return None

    def data_call(final: str) -> str | None:
        m = _PREVIEW.match(final)
        return f"{m.group(1)}/p2p/payment/preview/data/{m.group(2)}" if m else None

    try:
        bodies = await c.web_pages(url, data_call)
    except (native.NativeError, Exception) as e:  # noqa: BLE001 - the browser reads the window then
        log.info("PayLivery window through the app login failed: %s", e)
        return None
    if not any("/p2p/payment/preview/data/" in u for u in bodies):
        log.info("PayLivery window through the app login ended elsewhere: %s", ", ".join(u.split("?")[0] for u in bodies))
        return None
    page = next((b for u, b in bodies.items() if "/p2p/payment/preview/data/" not in u), "")
    return {"bodies": bodies, "text": re.sub(r"<[^>]+>", "\n", page),
            "requests": [{"method": "GET", "url": u.split("?")[0], "via": "app"} for u in bodies]}


def cached(ad_id: str, price: float | None) -> float | None:
    """The fee read for this ad and price within the last hour (the ad viewer reads it when the ad is opened)."""
    hit = _cache.get((str(ad_id), price))
    return hit[1]["fee"] if hit and time.monotonic() - hit[0] < CACHE_S else None
