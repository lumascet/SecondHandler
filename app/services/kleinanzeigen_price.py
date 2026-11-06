"""The own ad's price on Kleinanzeigen (kleinanzeigen.de), worked out from the comparable ads there.

Why its own price: the German market prices differently from Austria's, and a parcel from Austria to Germany costs
more than one sent at home. So the Kleinanzeigen price is not simply the willhaben price. analyse() searches
Kleinanzeigen for the ad's words through the app's API (the same statistics as the willhaben comparables,
ad_creator.price_stats), and one Claude call weighs those prices against the willhaben price, the condition and the
shipping to Germany. It returns the price to ask and a one-line note. Without Claude the median of the comparable ads
is the suggestion; with fewer than three priced comparables there is none.

analyse() does nothing (None) unless the platform is switched on and its app credentials are set: the search needs
them, and the rest of the app works without. Errors of the search are not caught here: the callers (the draft's
write, its Recalculate job, the crosspost job) decide what a failure means for them.

Interface: analyse(...) -> dict | None, shipping_hint(weight_kg) -> str, heading_keywords(heading) -> str.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app import platforms
from app.db import utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.platforms import kleinanzeigen as kleinanzeigen_api

log = logging.getLogger(__name__)

# Rough 2026 prices of a parcel from Austria to Germany (Post AT -> DE) by weight class: (up to kg, low €, high €).
# Used only for the hint on the page and for the prompt. These are not quotes: the carrier's price decides.
SHIPPING_AT_DE = ((2.0, 11, 16), (4.0, 16, 20), (10.0, 20, 25))
SEARCH_ROWS = 50
MAX_COMPARABLES = 12
MAX_HEADING_WORDS = 5

PRICE_SCHEMA = {"type": "object", "properties": {"price": {"type": "number"}, "note": {"type": "string"}},
                "required": ["price", "note"]}
PRICE_SYSTEM = (
    "Du bist Preisberater für Gebrauchtwaren auf kleinanzeigen.de (Deutschland). Du bekommst den Preis, zu dem derselbe "
    "Artikel in Österreich auf willhaben steht, und die Preise vergleichbarer Anzeigen auf Kleinanzeigen. Schlage den "
    "Preis vor, zu dem der Artikel dort realistisch verkauft wird: Der deutsche Markt hat eigene Preise, nicht einfach "
    "die österreichischen. Der Versand nach Deutschland kostet mehr als innerhalb Österreichs; der Preis darf den "
    "höheren Versand enthalten, wenn der Artikel sonst kaum ginge, muss es aber nicht. Antworte mit dem Preis in ganzen "
    "Euro und einem kurzen Satz auf Deutsch: wie der Markt im Vergleich zu willhaben liegt und, wenn es passt, ein "
    "Hinweis zum Versand. Keine Anrede, keine Floskeln."
)


def available() -> bool:
    """The analysis can run: the platform is switched on and its app credentials are set."""
    return platforms.enabled("kleinanzeigen") and bool(kleinanzeigen_api.api_auth()[0])


def _kg(value: float) -> str:
    return f"{value:g}"


def shipping_hint(weight_kg: float | None) -> str:
    """One German sentence: what a parcel from Austria to Germany costs for its weight class (SHIPPING_AT_DE)."""
    if weight_kg is None or weight_kg <= 0:
        low, high = SHIPPING_AT_DE[0][1], SHIPPING_AT_DE[-1][2]
        return t("Shipping to Germany costs more than at home: about {low}–{high} € for a parcel up to {kg} kg (rough).",
                 low=low, high=high, kg=_kg(SHIPPING_AT_DE[-1][0]))
    for limit, low, high in SHIPPING_AT_DE:
        if weight_kg <= limit:
            return t("Shipping to Germany costs more than at home: a parcel up to {kg} kg is about {low}–{high} € (rough).",
                     kg=_kg(limit), low=low, high=high)
    return t("Shipping to Germany costs more than at home: a parcel over {kg} kg costs more than {high} € (rough).",
             kg=_kg(SHIPPING_AT_DE[-1][0]), high=SHIPPING_AT_DE[-1][2])


def heading_keywords(heading: str | None) -> str:
    """Search words for an ad without a draft: the first words of its heading, numbers alone left out."""
    words = [w for w in (heading or "").split() if not re.fullmatch(r"[\d.,/x×-]+", w)]
    return " ".join(words[:MAX_HEADING_WORDS])


def _priced(ads: list[Any]) -> list[Any]:
    """The comparables that carry a price: no giveaways (price 0, "Zu verschenken") and no "VB" without an amount."""
    return [a for a in ads if a.price and a.price > 0 and "verschenk" not in (a.price_display or "").lower()]


def _positive(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _eur(value: float | None) -> str:
    return "–" if value is None else f"{value:g} €"


def _prompt(title: str, query: str, stats: dict[str, Any], comparables: list[dict[str, Any]], condition: str | None,
            shipping: bool | None, weight_kg: float | None, willhaben_price: float | None) -> str:
    lines = [f"Artikel: {title or query}", f"Suchbegriffe: {query}"]
    lines.append(f"Preis bei willhaben (Österreich): {_eur(willhaben_price)}")
    if condition:
        lines.append(f"Zustand: {condition}")
    lines.append("Versand: " + ("angeboten" if shipping else "nur Abholung" if shipping is False else "unbekannt"))
    lines.append(f"Paketgewicht: {_kg(weight_kg)} kg" if weight_kg and weight_kg > 0 else "Paketgewicht: unbekannt")
    lines.append("Versand nach Deutschland (grobe Richtwerte, Post AT → DE): bis 2 kg etwa 11–16 €, bis 4 kg etwa "
                 "16–20 €, bis 10 kg etwa 20–25 €.")
    lines.append(f"Vergleichbare Anzeigen auf Kleinanzeigen: {stats['count']} mit Preis, Median {_eur(stats['median'])}, "
                 f"mittlere 50 % {_eur(stats['p25'])} – {_eur(stats['p75'])}, Spanne {_eur(stats['min'])} – "
                 f"{_eur(stats['max'])}.")
    lines += [f"- {c['title']}: {c['price_display'] or _eur(c['price'])}" for c in comparables]
    return "\n".join(lines)


async def _claude(prompt: str) -> tuple[float | None, str | None]:
    """Claude's price (whole euros) and note; (None, None) when Claude cannot answer (the median then stands)."""
    try:
        res = await get_backend("ads").complete(system=PRICE_SYSTEM, prompt=prompt, schema=PRICE_SCHEMA)
    except LLMError as e:
        log.info("kleinanzeigen price: no Claude estimate: %s", e)
        return None, None
    data = res.data or {}
    price = _positive(data.get("price"))
    note = " ".join(str(data.get("note") or "").split()) or None
    return (float(round(price)) if price else None), note


async def analyse(title: str | None, keywords: str | None, *, condition: str | None = None,
                  shipping: bool | None = None, weight_kg: float | None = None,
                  willhaben_price: float | None = None) -> dict[str, Any] | None:
    """The Kleinanzeigen price of an ad: a keyword search there (the keywords, else the title), the statistics of the
    priced comparables, and Claude's price and note (see the module text). None when the platform is off or has no
    app credentials. The dict: price (None without comparables), price_stats (count, median, p25, p75, min, max,
    rows_found, llm_estimate), comparables (title, price, price_display, url, location), search_keywords, note,
    shipping_hint, at (ISO time)."""
    from app.services.ad_creator import nice_price, price_stats   # imports this module: looked up when called

    if not available():
        return None
    query = " ".join((keywords or title or "").split())
    if not query:
        return None
    plat = platforms.get("kleinanzeigen")
    page = await plat.search(plat.keyword_search(query), rows=SEARCH_ROWS)
    priced = _priced(page.ads)
    stats = price_stats([a.price for a in priced])
    comparables = [{"title": a.heading, "price": a.price, "price_display": a.price_display, "url": a.url,
                    "location": a.location} for a in priced[:MAX_COMPARABLES]]
    llm_price, note = None, None
    if stats:                       # no comparables to weigh: no Claude call, and no price
        llm_price, note = await _claude(_prompt(title or "", query, stats, comparables, condition, shipping,
                                                weight_kg, willhaben_price))
    price = nice_price(llm_price) if llm_price else (nice_price(stats["median"]) if stats else None)
    return {"price": price,
            "price_stats": {**(stats or {"count": len(priced)}), "rows_found": page.rows_found,
                            "llm_estimate": llm_price},
            "comparables": comparables, "search_keywords": query, "note": note,
            "shipping_hint": shipping_hint(weight_kg), "at": utcnow().isoformat()}
