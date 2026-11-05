"""Platform-neutral shapes: a listing in a result list, a result page, an ad's details, a search's definition.

Every adapter (app/platforms/<site>.py) turns its site's answers into these. Fields only one site knows are optional
(willhaben: category ids, PayLivery, seller login; kleinanzeigen: "VB"). `platform` names the adapter that produced
the object; `ref` is the id as it travels in links: the bare id for willhaben, "<platform>:<id>" otherwise.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from app.i18n import t

DEFAULT_PLATFORM = "willhaben"


def ad_ref(platform: str | None, ad_id: str) -> str:
    """The id as it travels in links and pushes: bare for willhaben, prefixed for every other platform."""
    return str(ad_id) if not platform or platform == DEFAULT_PLATFORM else f"{platform}:{ad_id}"


@dataclass
class Listing:
    """One ad in a result list (what a search agent stores as a Hit)."""
    ad_id: str
    heading: str
    body: str | None
    price: float | None
    price_display: str | None
    old_price: float | None
    location: str | None
    postcode: str | None
    state: str | None
    published_at: datetime | None
    changed_at: datetime | None
    end_date: datetime | None
    url: str
    image_url: str | None
    is_private: bool | None
    is_bumped: bool | None = None       # None: the site has no bumps (or does not say)
    status: str = "active"
    category_ids: list[str] = field(default_factory=list)   # willhaben: category tree root -> leaf
    platform: str = DEFAULT_PLATFORM
    lat: float | None = None            # the ad's place where the site sends coordinates (willhaben, Kleinanzeigen)
    lon: float | None = None
    item_condition: str | None = None   # new | like_new | refurbished | used | defect (app/platforms/condition.py)
    item_condition_label: str | None = None   # the site's words for it; None with a value: inferred (Preisjäger)
    # shipping to the owner (app/platforms/shipping_info.py): the price, 0 = free, None = not stated; and whether the
    # seller ships at all: "ship" | "pickup" (pickup only) | None (not stated)
    shipping_price: float | None = None
    shipping: str | None = None
    buyer_fee: float | None = None      # the buyer fee the site states for this ad (Vinted: buyer protection), app/platforms/fees.py
    paylivery: bool | None = None       # willhaben: PayLivery on (the list's p2penabled); None = not stated

    @property
    def ref(self) -> str:
        return ad_ref(self.platform, self.ad_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ad_id": self.ad_id,
            "ref": self.ref,
            "platform": self.platform,
            "title": self.heading,
            "category_ids": self.category_ids,
            "price": self.price,
            "price_display": self.price_display,
            "old_price": self.old_price,
            "location": self.location,
            "postcode": self.postcode,
            "lat": self.lat,
            "lon": self.lon,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "url": self.url,
            "image_url": self.image_url,
            "is_private": self.is_private,
            "item_condition": self.item_condition,
            "item_condition_label": self.item_condition_label,
            "shipping_price": self.shipping_price,
            "shipping": self.shipping,
            "buyer_fee": self.buyer_fee,
            "paylivery": self.paylivery,
        }


@dataclass
class SearchPage:
    rows_found: int
    ads: list[Listing]
    title: str | None = None


@dataclass
class AdDetail:
    ad_id: str
    heading: str
    description: str | None
    price: float | None
    price_display: str | None
    status: str
    published_at: datetime | None
    first_published_at: datetime | None
    end_date: datetime | None
    url: str
    images: list[str] = field(default_factory=list)
    location: str | None = None
    seller_name: str | None = None
    seller_login_id: str | None = None
    seller_uuid: str | None = None
    ad_uuid: str | None = None         # willhaben's internal ad uuid: with seller_uuid, the seller's profile page
    seller_since: datetime | None = None   # when the seller's account was created (willhaben registerDate, eBay)
    seller_ratings: int | None = None  # ratings the seller received (willhaben PayLivery, Vinted, eBay); None = unknown
    is_private: bool | None = None
    chat_enabled: bool | None = None
    category_path: list[str] = field(default_factory=list)
    edit_actions: Any = None
    category_id: str | None = None     # leaf category (same id space as Listing.category_ids)
    paylivery_enabled: bool = False    # the ad offers willhaben PayLivery (attribute payliveryEnabled)
    buy_now: bool = False              # "Sofort kaufen" (p2ppOptions.buyNowInformationDto.isBuyNowAd)
    shipping_price: float | None = None  # cheapest PayLivery delivery price for the buyer (pickup point or door)
    shipping_options: list[dict[str, Any]] | None = None   # willhaben: [{name, home, point}] (parse.shipping_options)
    place: str | None = None           # pickup place for a map search: "postcode municipality, country" (advertAddressDetails)
    is_vb: bool | None = None          # kleinanzeigen: price is negotiable ("VB")
    platform: str = DEFAULT_PLATFORM
    item_condition: str | None = None  # as on Listing (app/platforms/condition.py)
    item_condition_label: str | None = None
    shipping: str | None = None        # "ship" | "pickup" | None, as on Listing; shipping_price above: 0 = free
    buyer_fee: float | None = None     # as on Listing

    @property
    def ref(self) -> str:
        return ad_ref(self.platform, self.ad_id)

    @property
    def maps_url(self) -> str | None:
        """Google Maps search for the place of the ad (postcode level, private sellers show no street)."""
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(self.place)}" if self.place else None

    @classmethod
    def from_hit(cls, h: Any) -> AdDetail:
        """What a stored hit knows, for sites without an ad-detail call (the reduced ad viewer)."""
        return cls(ad_id=h.ad_id, heading=h.heading, description=h.body, price=h.price, price_display=h.price_display,
                   status="active", published_at=h.published_at, first_published_at=h.published_at, end_date=h.end_date,
                   url=h.url, images=[h.image_url] if h.image_url else [], location=h.location,
                   is_private=h.is_private, platform=getattr(h, "platform", None) or DEFAULT_PLATFORM,
                   item_condition=getattr(h, "item_condition", None),
                   item_condition_label=getattr(h, "item_condition_label", None),
                   shipping_price=getattr(h, "shipping_price", None), shipping=getattr(h, "shipping", None),
                   buyer_fee=getattr(h, "buyer_fee", None), paylivery_enabled=bool(getattr(h, "paylivery", None)))

    @classmethod
    def from_result(cls, r: dict[str, Any]) -> AdDetail:
        """What a search result showed (direct search, product search: Listing.as_dict plus product-search keys),
        for sites without an ad-detail call or when the site cannot be reached."""
        from app.platforms.text import parse_iso

        published = r.get("published_at")
        published = parse_iso(published) if isinstance(published, str) else published
        return cls(ad_id=str(r.get("ad_id") or ""), heading=r.get("title") or "", description=r.get("text") or r.get("body"),
                   price=r.get("price"), price_display=r.get("price_display"), status="active", published_at=published,
                   first_published_at=published, end_date=None, url=r.get("url") or "",
                   images=[r["image_url"]] if r.get("image_url") else [], location=r.get("location"),
                   is_private=r.get("is_private"), platform=r.get("platform") or DEFAULT_PLATFORM,
                   item_condition=r.get("item_condition"), item_condition_label=r.get("item_condition_label"),
                   shipping_price=r.get("shipping_price"), shipping=r.get("shipping"), buyer_fee=r.get("buyer_fee"))

    @classmethod
    def from_context(cls, ctx: dict[str, Any], ad_id: str, platform: str) -> AdDetail:
        """The copy a chat kept of its ad (as_context()), for an ad no longer online: title, text, price, place,
        seller, photos as they were the last time the ad was read."""
        import re
        raw = ctx.get("price")
        price_display = raw if isinstance(raw, str) else None
        if isinstance(raw, (int, float)):
            price = float(raw)
        else:                                          # "160 €", "€ 1.250", "12,50 €"
            m = re.search(r"\d[\d.]*(?:,\d+)?", price_display or "")
            price = float(m.group(0).replace(".", "").replace(",", ".")) if m else None
        return cls(ad_id=str(ctx.get("ad_id") or ad_id), heading=ctx.get("title") or "", description=ctx.get("description"),
                   price=price, price_display=price_display, status="gone", published_at=None, first_published_at=None,
                   end_date=None, url=ctx.get("url") or "", images=list(ctx.get("images") or []),
                   location=ctx.get("location"), seller_name=ctx.get("seller_name"),
                   category_path=[c for c in (ctx.get("category") or "").split(" > ") if c],
                   platform=ctx.get("platform") or platform, item_condition=ctx.get("item_condition"),
                   item_condition_label=ctx.get("item_condition_label"), shipping=ctx.get("shipping"),
                   shipping_price=ctx.get("shipping_price"))

    def as_context(self) -> dict[str, Any]:
        return {
            "ad_id": self.ad_id,
            "ref": self.ref,
            "platform": self.platform,
            "title": self.heading,
            "price": self.price_display or (f"€ {self.price:g}" if self.price is not None else None),
            "description": self.description,
            "location": self.location,
            "category": " > ".join(self.category_path),
            "status": self.status,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "seller_name": self.seller_name,
            "seller_login_id": self.seller_login_id,
            "seller_uuid": self.seller_uuid,
            "url": self.url,
            "images": self.images[:5],
            "paylivery": self.paylivery_enabled,
            "buy_now": self.buy_now,
            "shipping_price": self.shipping_price,
            "shipping": self.shipping,
            "item_condition": self.item_condition,
            "item_condition_label": self.item_condition_label,
        }


@dataclass
class SearchSpec:
    """One search as a search agent stores it: `url` is the canonical browser link (what "on <site>" opens, volatile
    parameters dropped), `api` the platform's opaque request target (willhaben: the JSON search URL; kleinanzeigen:
    nothing, the API parameters are rebuilt from the fields), the rest is what the search means."""
    platform: str
    url: str
    api: str | None = None
    keyword: str | None = None
    price_from: float | None = None
    price_to: float | None = None
    category: str | None = None        # willhaben: category path; kleinanzeigen: category id
    location: str | None = None        # kleinanzeigen: the place slug from a pasted link
    sort: str = "newest"               # newest | relevance | price_asc | price_desc
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, {}, "") or k in ("platform", "url")}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SearchSpec:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class FilterValue:
    label: str
    url: str                 # browser URL with this value applied (selected values: with it removed)
    hits: int | None = None


@dataclass
class SearchFilter:
    id: str
    label: str
    kind: str                # HIERARCHICAL | RANGE | STANDARD
    multi: bool
    values: list[FilterValue]
    selected: list[FilterValue]
    range_params: list[str] = field(default_factory=list)   # e.g. [PRICE_FROM, PRICE_TO]


@dataclass
class SearchFilters:
    """One step of the search-agent builder: the current search plus everything that can be added to it."""
    url: str
    rows_found: int
    groups: list[tuple[str, list[SearchFilter]]]
    ads: list[Listing]
    keyword: str | None = None

    @property
    def selected(self) -> list[tuple[SearchFilter, FilterValue]]:
        return [(f, v) for _, fs in self.groups for f in fs for v in f.selected]

    def suggested_name(self) -> str:
        parts = [self.keyword] if self.keyword else []
        for f, v in self.selected:
            # Category shows its whole path; the deepest level is enough for a name.
            if f.kind == "HIERARCHICAL" and v is not f.selected[-1]:
                continue
            parts.append(v.label)
        return " · ".join(parts)[:80] or t("All ads")
