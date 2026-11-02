"""ebay.at (Austria) as a platform adapter, through the website's result list: Buy It Now offers located in Austria.

A search is the result page www.ebay.at/sch/i.html?_nkw=…&_sop=10 (newest first) with LH_PrefLoc=1 (items located in
Austria) and LH_BIN=1 (Buy It Now). Auctions are left out, also those with a Buy It Now price once they carry bids:
their price is only the current bid. Each result is an `li.s-card` (data-listingid) with title, condition, private or
commercial seller, price, "oder Preisvorschlag", shipping, the listing time (Vienna time, without a year) and the
seller's name and rating. The page has no machine-readable data, so the adapter reads the card texts. Cards name no
place (items from abroad say "aus <country>").

An ad is its item page www.ebay.at/itm/<id>: the canonical link confirms the item (a block page has none), the JSON-LD
Product gives price and photos, the markup title, condition, seller and its kind, shipping and the item specifics, the
page model `"listingStatus"`, `"startDate"` and `"itemLocation"` ("Thalgau, Österreich"). The seller's description is
a second page (the iframe on itm.ebaydesc.com). Words like "beendet" or "Preisvorschlag" are in every page's text
dictionary, so they are never searched for in the raw page; the postcode eBay shows next to pickup and delivery is the
visitor's, not the item's.

eBay sits behind Akamai Bot Manager. Requests go through curl_cffi impersonating Chrome, and a new visitor's first
requests are often refused (403) while the bot manager sets its cookies. Until this visitor fetched a result list or an
item page (not a cached answer, not a bot check's page that comes with 200), a 403 is that warm-up: the client keeps its
cookies and asks again (paced by the limiter, at most WARMUP_RETRIES times); after that a 403 is a block as on every
other site. Through the VPN, every exit is a new visitor with its own warm-up, so a warm-up 403 is no refused exit. The
description host is optional: a refusal there only leaves the ad without its description. No login, no API keys.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from app.i18n import t
from app.platforms import condition, shipping_info
from app.platforms.base import InvalidSearchUrl, Platform, PlatformError
from app.platforms.httpclient import FetchResponse, HttpPlatformClient, Transport
from app.platforms.ratelimit import RateLimiter
from app.platforms.text import clean_text, euro, one_line, parse_iso, to_float
from app.platforms.types import AdDetail, Listing, SearchPage, SearchSpec

log = logging.getLogger(__name__)

WEB = "https://www.ebay.at"
SEARCH = f"{WEB}/sch/i.html"
DESC_HOST = "ebaydesc.com"              # the seller's description (itm.ebaydesc.com)
PAGE_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "de-AT,de;q=0.9", "Referer": f"{WEB}/"}
PAGE_SIZE = "60"
WARMUP_RETRIES = 2
VIENNA = ZoneInfo("Europe/Vienna")
DEFAULT_LOCATION = "1"                  # LH_PrefLoc: 1 = Austria, 3 = EU, 2 = worldwide
SWISS_VAT = 1.081                       # what eBay adds to prices for a visitor in Switzerland
SORTS = {"newest": "10", "relevance": "12", "price_asc": "15", "price_desc": "16"}
_MONTHS = {m: i for i, names in enumerate((("jan",), ("feb",), ("mar", "mär"), ("apr",), ("may", "mai"), ("jun",),
                                           ("jul",), ("aug",), ("sep",), ("oct", "okt"), ("nov",), ("dec", "dez")), 1)
           for m in names}
_LISTING_ID = re.compile(r'data-listingid="?(\d+)')
_CARD = re.compile(r'<li\b[^>]*class="?s-card[ "]')
_LI = re.compile(r"<(/?)li\b", re.I)
_REWRITE = "srp-river-answer--REWRITE_START"          # "results for fewer words": not what was searched
_COUNT = re.compile(r"srp-controls__count-heading[^>]*>(?:\s|<[^>]+>)*([\d.]+)")
_EUR = re.compile(r"(?:EUR|€)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)")
_BIDS = re.compile(r"^\d+ Gebote?$")
_WHEN = re.compile(r"^([A-Za-zä]{3})-(\d{1,2}) (\d{1,2}):(\d{2})$")
_RATING = re.compile(r"^[\d.,]+\s?% positiv")
_SHIPPING = re.compile(r"Versand|Abholung|Lieferung", re.I)
_SELLER_TYPES = {"Privat": True, "Gewerblich": False}
_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "col", "area"}


# ------------------------------------------------------------------ parsing
class _CardParser(HTMLParser):
    """One result card: its texts with the classes of the elements around them, its links and its photo."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str]] = []
        self.texts: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"] or "")
        if tag == "img" and self.image is None and "s-card__image" in (a.get("class") or ""):
            self.image = a.get("data-defer-load") or a.get("src")
        if tag not in _VOID:
            self.stack.append((tag, a.get("class") or ""))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        text = one_line(data)
        if text and not any(tag in ("script", "style") for tag, _ in self.stack):
            self.texts.append((" ".join(c for _, c in self.stack if c), text))


def _cards(page: str) -> list[str]:
    """The result cards in page order, up to eBay's "results for fewer words"."""
    cut = page.find(_REWRITE)
    page = page[:cut] if cut >= 0 else page
    out = []
    for m in _CARD.finditer(page):
        depth = 0
        for li in _LI.finditer(page, m.start()):
            depth += -1 if li.group(1) else 1
            if depth == 0:
                out.append(page[m.start():page.find(">", li.end()) + 1])
                break
    return out


def _amounts(text: str) -> list[float]:
    """"EUR 1.234,50" -> [1234.5]; a range ("EUR 10,00 bis EUR 20,00") gives both ends."""
    return [float(a.replace(".", "").replace(",", ".")) for a in _EUR.findall(text or "")]


def listed_at(text: str, now: datetime | None = None) -> datetime | None:
    """"Oct-9 23:33" (Vienna time, no year) -> aware UTC datetime, like every site's times (the database keeps them
    without the offset); a date after today belongs to last year."""
    m = _WHEN.match(text or "")
    month = _MONTHS.get(m.group(1).lower()) if m else None
    if not m or not month:
        return None
    now = (now or datetime.now(VIENNA)).astimezone(VIENNA)
    try:
        dt = datetime(now.year, month, int(m.group(2)), int(m.group(3)), int(m.group(4)), tzinfo=VIENNA)
        if dt > now + timedelta(days=1):
            dt = dt.replace(year=now.year - 1)
    except ValueError:                      # Feb-30, or a Feb-29 whose year (this one or last) has none
        return None
    return dt.astimezone(timezone.utc)


def parse_card(card: str, now: datetime | None = None) -> Listing | None:
    """One result card -> Listing; None for eBay's own placeholder cards and for auctions."""
    m = _LISTING_ID.search(card)
    if not m:
        return None
    ad_id = m.group(1)
    p = _CardParser()
    p.feed(card)
    if not any(f"/itm/{ad_id}" in href for href in p.links):
        return None                            # "Shop on eBay" and other cards that are no listing
    title = " ".join(x for c, x in p.texts if "s-card__title" in c and "new-listing" not in c and "clipped" not in c)
    subtitle = [x.strip(" |") for c, x in p.texts if "s-card__subtitle" in c]
    rows = [(c, x) for c, x in p.texts if "s-card__attribute-row" in c]
    other = [x for c, x in rows if "s-card__price" not in c]
    if not title or any(_BIDS.match(x) for x in other):
        return None
    current = [v for c, x in rows if "s-card__price" in c and "strikethrough" not in c for v in _amounts(x)]
    before = next((v for c, x in rows if "s-card__price" in c and "strikethrough" in c for v in _amounts(x)), None)
    price = min(current) if current else None
    display = None
    if current:
        display = euro(price) if len(set(current)) == 1 else f"{euro(min(current))} – {euro(max(current))}"
    is_private = next((_SELLER_TYPES[x] for x in subtitle if x in _SELLER_TYPES), None)
    condition_texts = [x for x in subtitle if x and x not in _SELLER_TYPES]
    item_condition, item_condition_label = condition.ebay(next(iter(condition_texts), None))
    abroad = next((x[4:] for x in other if x.startswith("aus ")), None)
    when = next((d for x in other if (d := listed_at(x, now))), None)
    rating_at = next((i for i, x in enumerate(other) if _RATING.match(x)), None)
    seller = other[rating_at - 1] if rating_at else None
    ship_text = next((x for x in other if _SHIPPING.search(x) and "Rückversand" not in x), None)
    ship_price, ship_kind = shipping_info.from_text(ship_text)
    facts = [*condition_texts,
             next((x for x in other if x in ("oder Preisvorschlag", "Preisvorschlag")), None),
             ship_text,
             t("Seller {name}, {rating}", name=seller, rating=other[rating_at]) if seller and rating_at else None]
    image = p.image if p.image and p.image.startswith("https://") else None
    return Listing(ad_id=ad_id, heading=title, body=" · ".join(f for f in facts if f) or None, price=price,
                   price_display=display, old_price=before if before and price is not None and before > price else None,
                   location=abroad, postcode=None, state=None, published_at=when, changed_at=None, end_date=None,
                   url=f"{WEB}/itm/{ad_id}", image_url=re.sub(r"/s-l\d+\.", "/s-l500.", image) if image else None,
                   is_private=is_private, is_bumped=None, status="active", platform="ebay",
                   item_condition=item_condition, item_condition_label=item_condition_label,
                   shipping_price=ship_price, shipping=ship_kind)


def ship_to() -> dict[str, str]:
    """eBay calculates the shipping in the result list for this postcode: the owner's (Settings → Shipping address);
    nothing without one (eBay then guesses from the IP, the VPN server's country)."""
    from app.services import shipping

    zip_code = (shipping.sender().get("zip") or "").strip()
    return {"_stpos": zip_code} if re.fullmatch(r"\d{4}", zip_code) else {}


def _has_results(page: str) -> bool:
    """A result page (also one with "0 Ergebnisse"), not a bot check's or error page."""
    return bool(_COUNT.search(page or "")) or "srp-results" in (page or "")


def parse_search(page: str, now: datetime | None = None) -> SearchPage:
    """The result page -> SearchPage. "0 Ergebnisse" gives an empty page (the cards below are only suggestions)."""
    if not _has_results(page):
        raise PlatformError(t("eBay answered without a result list"))
    m = _COUNT.search(page)
    total = int(m.group(1).replace(".", "")) if m else None
    if total == 0:
        return SearchPage(rows_found=0, ads=[])
    ads, seen = [], set()
    for card in _cards(page):
        ad = parse_card(card, now)
        if ad and ad.ad_id not in seen:
            seen.add(ad.ad_id)
            ads.append(ad)
    return SearchPage(rows_found=total if total is not None else len(ads), ads=ads)


# ------------------------------------------------------------------ the item page
_CANONICAL = re.compile(r'<link\b[^>]*rel="?canonical"?[^>]*href="?https://www\.ebay\.[a-z.]+/itm/(\d+)', re.I)
_LD_JSON = re.compile(r'<script\b[^>]*type="?application/ld\+json"?[^>]*>(.*?)</script>', re.S | re.I)
_STATUS = re.compile(r'"listingStatus":"([A-Z_]+)"')
_START = re.compile(r'"startDate":\{"value":"([^"]+)"')
_ITEM_LOCATION = re.compile(r'"itemLocation":\{"_type":"LabelsValues".{0,300}?"values":\[\{"_type":"TextualDisplay",'
                            r'"textSpans":\[\{"_type":"TextSpan","text":"([^"]+)"', re.S)
_DESC_FRAME = re.compile(r'<iframe\b[^>]*\bid="?desc_ifr"?[^>]*\bsrc="?([^"\s>]+)', re.I)
_PHOTO_ID = re.compile(r"i\.ebayimg\.com/(?:thumbs/)?images/g/([^/\s\"']+)/s-l\d+")
_CODE_LABEL = re.compile(r"EAN|GTIN|UPC|ISBN|MPN|nummer|Nr\b", re.I)   # Herstellernummer, OE/OEM-Referenznummer,
                                                                     # Vergleichsnummer, Art.-Nr.
_NUMBER_CODE = re.compile(r"[\d ,;/-]{7,}[\dX]", re.I)      # 4006381333931, 978-3-16-148410-0, 3-16-148410-X, lists
_HIDDEN = {"clipped", "hide", "ux-bubble-help"}             # screen-reader copies, folded text, tooltips
_SKIP = {"script", "style", "noscript", "svg", "template", "button"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def find_all(self, tag: str | None = None, cls: str | None = None):
        for c in self.children:
            if isinstance(c, _Node):
                if (tag is None or c.tag == tag) and (cls is None or cls in c.classes):
                    yield c
                yield from c.find_all(tag, cls)

    def find(self, tag: str | None = None, cls: str | None = None) -> _Node | None:
        return next(self.find_all(tag, cls), None)

    def text(self) -> str:
        """The visible text, joined without spaces (eBay splits words with <wbr>)."""
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag not in _SKIP and not (_HIDDEN & set(c.classes)) and "hidden" not in c.attrs:
                out.append(c.text())
        return "".join(out)


class _Tree(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        n = _Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        if tag not in self.VOID:
            self.cur = n

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.cur.children.append(_Node(tag, {k: v or "" for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def _text(n: _Node | None) -> str | None:
    return one_line((n.text() if n else "").replace("\xa0", " "))


def _ld_json(page: str, kind: str) -> dict[str, Any]:
    for m in _LD_JSON.finditer(page):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == kind:
            return d
    return {}


def _photos(product: dict[str, Any], root: _Node) -> list[str]:
    """Full-size photos in order: the JSON-LD images, else the gallery's zoom images (lazy slides have no src)."""
    images = product.get("image") or []
    urls = [images] if isinstance(images, str) else [u for u in images if isinstance(u, str)]
    if not urls and (gallery := root.find("div", "ux-image-carousel")):
        urls = [img.attrs.get("data-zoom-src") or img.attrs.get("src") or "" for img in gallery.find_all("img")]
    out: list[str] = []
    for u in urls:
        m = _PHOTO_ID.search(u)
        url = f"https://i.ebayimg.com/images/g/{m.group(1)}/s-l1600.jpg" if m else None
        if url and url not in out:
            out.append(url)
    return out


def _rows(root: _Node) -> dict[str, tuple[str, str]]:
    """The shipping section's rows by kind (shipping, localPickup, deliverto, ...): (label, first value line)."""
    section = root.find("div", "ux-layout-section--shipping")
    rows = {}
    for lv in section.find_all("div", "ux-labels-values") if section else []:
        kind = next((c.split("--", 1)[1] for c in lv.classes if c.startswith("ux-labels-values--")), None)
        values = lv.find("div", "ux-labels-values__values-content")
        first = next((c for c in values.children if isinstance(c, _Node)), None) if values else None
        if kind and kind not in rows:
            rows[kind] = (_text(lv.find("div", "ux-labels-values__labels-content")) or "", _text(first) or "")
    return rows


def _specifics(root: _Node) -> list[tuple[str, str]]:
    """The item specifics (Marke: Lenovo, …) without the condition, which has its own line, and without product codes
    that are bare numbers (EAN 0045496420253): nothing to read, and the scam rules would take them for phone numbers."""
    section = root.find("div", "ux-layout-section--features")
    out = []
    for col in section.find_all("div", "ux-layout-section-evo__col") if section else []:
        label = _text(col.find("div", "ux-labels-values__labels-content"))
        value = _text(col.find("div", "ux-labels-values__values-content"))
        if not label or not value or label.rstrip(":") == "Artikelzustand":
            continue
        if _CODE_LABEL.search(label) and _NUMBER_CODE.fullmatch(value):
            continue
        out.append((label.rstrip(":"), value))
    return out


def parse_description(page: str) -> str | None:
    """The seller's description page (itm.ebaydesc.com) -> plain text."""
    tree = _Tree()
    tree.feed(page or "")
    box = tree.root.find("div", "x-item-description-child")
    if box is None:
        return None
    inner = re.sub(r"<(script|style)\b.*?</\1>", "", page[page.find(">", page.find("x-item-description-child")) + 1:],
                   flags=re.S | re.I)
    end = inner.find("<!--M_")
    return clean_text(inner[:end] if end >= 0 else inner) or None


def parse_item_page(page: str, ad_id: str, description: str | None = None, now: datetime | None = None) -> AdDetail | None:
    """An item page -> AdDetail; None when eBay shows another item (an ended one is redirected). A page without the
    canonical item link is no item page (bot check, error): PlatformError."""
    m = _CANONICAL.search(page or "")
    if not m:
        raise PlatformError(t("eBay answered without the item page"))
    if m.group(1) != ad_id:
        return None
    tree = _Tree()
    tree.feed(page)
    root = tree.root
    product = _ld_json(page, "Product")
    offers = product.get("offers") or {}
    offers = offers[0] if isinstance(offers, list) and offers else offers
    title = _text(root.find("h1", "x-item-title__mainTitle")) or one_line(
        html_lib.unescape(re.sub(r"<wbr\s*/?>", "", product.get("name") or ""))) or ""
    price = to_float(offers.get("price") if isinstance(offers, dict) else None)
    if price is None and (amounts := _amounts(_text(root.find("div", "x-price-primary")) or "")):
        price = amounts[0]
    condition_box = root.find("div", "x-item-condition-text")
    condition_text = _text(condition_box.find("span", "ux-textspans")) if condition_box else None
    note = (_text(root.find("div", "x-item-condition-desc")) or "").strip("“”\" ") or None
    schema = (offers.get("itemCondition") or "").rsplit("/", 1)[-1] if isinstance(offers, dict) else None
    item_condition, item_condition_label = condition.ebay(condition_text, schema)
    about = root.find("div", "x-sellercard-atf__about-seller")
    kinds = [_text(i.find("span", "ux-textspans")) for i in about.find_all("div", "x-sellercard-atf__about-seller-item")] \
        if about else []
    seller_type = next((k for k in kinds if k in _SELLER_TYPES), None)
    rows = _rows(root)
    ship_price, ship_kind = shipping_info.from_text(" ".join(rows["shipping"]) if rows.get("shipping") else None)
    if ship_kind is None and "localPickup" in rows and not rows.get("shipping"):
        ship_kind = shipping_info.PICKUP
    facts = [f"{t('Condition')}: {condition_text}" if condition_text else None, note,
             " ".join(rows["shipping"]) if rows.get("shipping") else None,
             t("Pickup possible") if "localPickup" in rows else None]
    specifics = " · ".join(f"{k}: {v}" for k, v in _specifics(root))
    text = "\n\n".join(x for x in (description, "\n".join(f for f in facts if f), specifics) if x)
    status = (m2.group(1) if (m2 := _STATUS.search(page)) else "ACTIVE")
    start = parse_iso(m3.group(1)) if (m3 := _START.search(page)) else None
    place = html_lib.unescape(m4.group(1)) if (m4 := _ITEM_LOCATION.search(page)) else None
    crumbs = _ld_json(page, "BreadcrumbList").get("itemListElement") or []
    path = [one_line(c.get("name")) for c in crumbs if isinstance(c, dict) and (c.get("position") or 0) > 1 and c.get("name")]
    return AdDetail(ad_id=ad_id, heading=title, description=text or None, price=price,
                    price_display=euro(price) if price is not None else None,
                    status="active" if status == "ACTIVE" else "expired", published_at=start, first_published_at=start,
                    end_date=None, url=f"{WEB}/itm/{ad_id}", images=_photos(product, root), location=place, place=place,
                    seller_name=_text(root.find("div", "x-sellercard-atf__about-seller-item--seller-name")),
                    seller_since=_member_since(page), seller_ratings=_rating_count(page),
                    is_private=_SELLER_TYPES.get(seller_type) if seller_type else None,
                    category_path=[p for p in path if p], platform="ebay", item_condition=item_condition,
                    item_condition_label=item_condition_label, shipping_price=ship_price, shipping=ship_kind)


_MEMBER_SINCE = re.compile(r"(?:Mitglied seit|Member since):?\s*([A-Za-zäÄ]{3,})\.?\s+(\d{4})")
_MONTHS = {m: i for i, ms in enumerate((("jan",), ("feb",), ("mär", "mar", "mae"), ("apr",), ("mai", "may"), ("jun",),
                                        ("jul",), ("aug",), ("sep",), ("okt", "oct"), ("nov",), ("dez", "dec")), 1)
           for m in ms}
# the seller card: the name, then the rating count in brackets ("(0)", "(1.234)")
_RATING_COUNT = re.compile(r'data-testid=x-sellercard-atf__about-seller>(?:<!--[^>]*-->)*<span[^>]*>\((\d[\d.,]*)\)</span>')


def _member_since(page: str) -> datetime | None:
    """ "Mitglied seit Jul 2025" in the seller's box: the first of that month."""
    m = _MEMBER_SINCE.search(page)
    month = _MONTHS.get(m.group(1)[:3].lower()) if m else None
    return datetime(int(m.group(2)), month, 1, tzinfo=timezone.utc) if month else None


def _rating_count(page: str) -> int | None:
    m = _RATING_COUNT.search(page)
    return int(re.sub(r"[.,]", "", m.group(1))) if m else None


# ------------------------------------------------------------------ the client
def classify(url: str) -> str:
    path = urlsplit(url).path
    if path.startswith("/sch/"):
        return "search"
    if path.startswith("/itm/"):
        return "detail"
    return "other"


_limiter: RateLimiter | None = None


def ebay_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        from app.db import platform_min_gap_s, platform_per_hour

        _limiter = RateLimiter(platform_min_gap_s("ebay"), platform_per_hour("ebay"), name="eBay", persist="ebay")
    return _limiter


class _WarmingUp(PlatformError):
    """403 before this visitor's first answer: eBay's bot manager is still setting its cookies."""


def _from_description_host(r: FetchResponse) -> bool:
    """An answer of itm.ebaydesc.com, where the seller's description lives: not www.ebay.at's bot check."""
    return (urlsplit(r.url).hostname or "").endswith(DESC_HOST)


class EbayClient(HttpPlatformClient):
    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__("ebay", ebay_limiter(), name="eBay", headers={"Accept-Language": "de-AT,de;q=0.9"},
                         transport=transport, backend="curl")

    # the warm-up belongs to the visitor: a test of the VPN search warms up its own, the live one stays as it is
    @property
    def _warm(self) -> bool:
        return self._visitor().extra.get("warm", False)

    @_warm.setter
    def _warm(self, value: bool) -> None:
        self._visitor().extra["warm"] = value

    @property
    def _warmups(self) -> int:
        return self._visitor().extra.get("warmups", WARMUP_RETRIES)

    @_warmups.setter
    def _warmups(self, value: int) -> None:
        self._visitor().extra["warmups"] = value

    def classify(self, url: str) -> str:
        return classify(url)

    def new_anonymous_session(self) -> None:
        super().new_anonymous_session()
        self._warm, self._warmups = False, WARMUP_RETRIES

    def _warming_up(self, r: FetchResponse) -> bool:
        return r.status_code == 403 and not self._warm and self._warmups > 0

    def _check_status(self, r: FetchResponse) -> None:
        if _from_description_host(r):
            return                      # optional: item() leaves the ad without it, the visitor and the pace stay
        if self._warming_up(r):
            self._warmups -= 1
            raise _WarmingUp("eBay: warming up")
        super()._check_status(r)

    def _exit_unreachable(self, url: str) -> bool:
        """A network error of the seller's description keeps the server too: another host, and optional."""
        return not (urlsplit(url).hostname or "").endswith(DESC_HOST)

    def _exit_blocked(self, r: FetchResponse) -> bool:
        """Through the VPN a warm-up 403 keeps the exit: _check_status asks again through it. So does a refused
        description: another host, and optional."""
        return not _from_description_host(r) and not self._warming_up(r) and super()._exit_blocked(r)

    async def _use(self, proxy: str | None) -> Transport:
        if self._transport is not None and proxy != self._proxy:      # another exit: a new visitor, a new warm-up
            self._warm, self._warmups = False, WARMUP_RETRIES
        return await super()._use(proxy)

    async def _page(self, url: str, *, params: dict[str, str] | None = None, cache_s: float = 0,
                    kind: str = "search", is_page: Callable[[str], Any] | None = None) -> FetchResponse:
        """GET a page; a new visitor's warm-up refusals are asked again (see the module docstring). `is_page(text)`
        tells the page asked for from a bot check's or error page that came with 200: only the page asked for is
        cached and makes this visitor warm. _get asks about fetched answers only, so a cached or replayed one (no
        news about this visitor) never does."""
        def valid(r: FetchResponse) -> bool:
            if not is_page(r.text):
                return False
            self._warm = True
            return True

        while True:
            try:
                return await self._get(url, params=params, headers=PAGE_HEADERS, cache_s=cache_s, kind=kind,
                                       valid=valid if is_page else None)
            except _WarmingUp:
                log.info("eBay refused a new visitor while its bot check sets the cookies; asking again")

    async def search_page(self, params: dict[str, str], *, cache_s: float = 0) -> SearchPage:
        r = await self._page(SEARCH, params=params, cache_s=cache_s, is_page=_has_results)
        if r.status_code != 200:
            raise PlatformError(t("eBay search returned HTTP {status}", status=r.status_code))
        return parse_search(r.text)

    async def item(self, ad_id: str) -> AdDetail | None:
        """The item page and the seller's description (a second page; without it the ad still shows)."""
        r = await self._page(f"{WEB}/itm/{ad_id}", cache_s=300, kind="detail", is_page=_CANONICAL.search)
        if r.status_code in (404, 410):
            return None
        if r.status_code != 200:
            raise PlatformError(t("eBay ad {id}: HTTP {status}", id=ad_id, status=r.status_code))
        description = None
        if m := _DESC_FRAME.search(r.text):
            try:
                d = await self._page(m.group(1).replace("&amp;", "&"), cache_s=300, kind="detail")
            except PlatformError as e:
                log.info("eBay description of %s: %s", ad_id, e)
            else:
                if d.status_code == 200:
                    description = parse_description(d.text)
                else:
                    log.info("eBay description of %s: HTTP %s, the ad shows without it", ad_id, d.status_code)
        return parse_item_page(r.text, ad_id, description)


_client: EbayClient | None = None


def get_client() -> EbayClient:
    global _client
    if _client is None:
        _client = EbayClient()
    return _client


def set_client(client: EbayClient | None) -> None:
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


class EbayPlatform(Platform):
    id = "ebay"
    name = "eBay"
    host = "ebay.at"
    api_host = "www.ebay.at"
    home_country = "AT"
    hosts = ("ebay.at",)
    image_hosts = ("ebayimg.com", "ebaystatic.com")
    locale = "de-AT"
    capabilities = frozenset({"search_url", "keyword_search", "detail", "images"})
    PAGE_SIZE_MAX = int(PAGE_SIZE)
    CAN_PAGE = True
    access = "http"
    min_interval_s = 300                      # every search is a whole result page (about 1.5 MB)
    vpn_test_requests = 1 + WARMUP_RETRIES    # a new visitor per exit: its warm-up 403s are asked again there

    @property
    def limiter(self) -> RateLimiter:
        return ebay_limiter()

    def price_dropped(self, old: float, new: float) -> bool:
        """Through a Swiss VPN exit eBay showed prices with Swiss VAT added (10-14: 28 of 85 stored hits at exactly
        x1.081); the same price from an EU exit is no price drop, only the stored price is put right."""
        return new < old and round(new * SWISS_VAT, 2) != round(old, 2)

    @property
    def client(self) -> EbayClient:
        return get_client()

    def about(self) -> str:
        return t("Reads the result pages of ebay.at: only Buy It Now offers located in Austria, no auctions. Listings "
                 "show condition, private or commercial seller, shipping and the listing time but no place; opening an "
                 "ad loads its page with the place, all photos, the specifics and the seller's description.")

    def parse_search_url(self, url: str) -> SearchSpec:
        u = urlsplit(url.strip())
        if u.scheme not in ("http", "https") or not self.matches(url):
            raise InvalidSearchUrl(t("Not an ebay.at link"))
        if u.path.startswith(("/itm/", "/p/")):
            raise InvalidSearchUrl(t("That is an ad URL, not a result list"))
        m = re.fullmatch(r"/sch/(?:[^/]+/(\d+)/)?i\.html", u.path)
        if not m:
            raise InvalidSearchUrl(t("Not an eBay result list (copy the link of a search: …/sch/i.html?_nkw=…)"))
        qs = {k: v[0] for k, v in parse_qs(u.query).items() if v}
        keyword = one_line(qs.get("_nkw")) or ""
        category = m.group(1) or (qs.get("_sacat") if qs.get("_sacat", "0") != "0" else None)
        if not keyword and not category:
            raise InvalidSearchUrl(t("The eBay link has no search term (_nkw=…)"))
        extra = {k: qs[k] for k in ("LH_ItemCondition",) if qs.get(k)}
        if qs.get("LH_PrefLoc", DEFAULT_LOCATION) != DEFAULT_LOCATION:
            extra["LH_PrefLoc"] = qs["LH_PrefLoc"]
        # the link's sort (_sop) is dropped: agents diff on newness, a price-sorted window would miss new ads
        spec = SearchSpec(platform=self.id, url="", keyword=keyword or None, price_from=_num(qs.get("_udlo")),
                          price_to=_num(qs.get("_udhi")), category=category, sort="newest", extra=extra)
        spec.url = self._url(spec)
        return spec

    def _params(self, spec: SearchSpec) -> dict[str, str]:
        params = {"_nkw": spec.keyword or "", "_sacat": spec.category or "0", "_sop": SORTS.get(spec.sort, SORTS["newest"]),
                  "LH_BIN": "1", "LH_PrefLoc": spec.extra.get("LH_PrefLoc", DEFAULT_LOCATION)}
        if spec.price_from is not None:
            params["_udlo"] = f"{spec.price_from:g}"
        if spec.price_to is not None:
            params["_udhi"] = f"{spec.price_to:g}"
        if spec.extra.get("LH_ItemCondition"):
            params["LH_ItemCondition"] = spec.extra["LH_ItemCondition"]
        return params

    def _url(self, spec: SearchSpec) -> str:
        return f"{SEARCH}?{urlencode(self._params(spec))}"

    def describe(self, spec: SearchSpec) -> str:
        return spec.keyword or t("search")

    def keyword_search(self, keyword: str, price_from: float | None = None, price_to: float | None = None, *,
                       category: str | None = None, sort: str = "newest") -> SearchSpec:
        spec = SearchSpec(platform=self.id, url="", keyword=keyword.strip(), price_from=price_from, price_to=price_to,
                          category=category, sort=sort)
        spec.url = self._url(spec)
        return spec

    async def search(self, spec: SearchSpec, *, rows: int | None = None, cache_s: float = 0,
                     page: int = 1) -> SearchPage:
        from app.config import get_settings

        params = {**self._params(spec), "_ipg": PAGE_SIZE, **ship_to()}
        if page > 1:                                # the result list's own paging
            params["_pgn"] = str(page)
        result = await self.client.search_page(params, cache_s=cache_s)
        result.ads = result.ads[:rows or get_settings().search_rows]
        result.title = spec.keyword
        return result

    async def detail(self, ad_id: str, *, url: str | None = None) -> AdDetail | None:
        return await self.client.item(ad_id)

    def ad_url(self, ad_id: str) -> str:
        return f"{WEB}/itm/{ad_id}"

    def parse_ad_url(self, url: str) -> str | None:
        m = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,15})(?:[/?#]|$)", url)
        return m.group(1) if m else None

    def classify(self, url: str) -> str:
        return classify(url)

    async def aclose(self) -> None:
        global _client
        if _client is not None:
            await _client.aclose()
            _client = None


__all__ = ["EbayClient", "EbayPlatform", "get_client", "set_client", "parse_search", "parse_card", "listed_at"]
