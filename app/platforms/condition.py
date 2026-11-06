"""Item condition as the sites state it: one normalised value (it picks the price zones, price_zones.group) and the
site's own words for display. Unknown text stays unknown (None): it is never guessed as used.

Values: new | like_new | refurbished | used | defect. A value with a label means the site stated it (the label is
the site's words); a value without a label was inferred (Preisjäger only: its deals are shop offers).

Checked against live answers: willhaben (list attribute 21 and the detail's attributeInformation), eBay (card
subtitle, item page, JSON-LD), Vinted (catalog and item), Kleinanzeigen (`<category>.condition` in list and ad,
2026-10-14). Shpock states it only on the item page (extraProperties "condition", "good" seen) and only when the
seller picked one; the list's `isNew` was false even for an ad titled "Neu" without a condition, so it is not read.
"""
from __future__ import annotations

import html
import re
from typing import Any

from app.i18n import t

VALUES = ("new", "like_new", "refurbished", "used", "defect")
_RULES = [  # order matters: "Sehr gut – Refurbished" is refurbished, "Neu ohne Etikett" like new
    ("defect", re.compile(r"defekt|ersatzteil|bastler|for parts|not working|nicht (?:voll )?funktionsf")),
    ("refurbished", re.compile(r"refurb|generalüberholt|überholt|renewed|b-ware")),
    ("like_new", re.compile(r"neuwertig|wie neu|like new|as good as new|ohne etikett|ausstellungsst|unbenutzt")),
    ("new", re.compile(r"^(?:brand)?neu\b|^new\b|^brand new|mit etikett")),
    ("used", re.compile(r"gebraucht|^used\b|sehr gut|^gut\b|\bgood\b|akzeptabel|in ordnung|zufriedenstellend")),
]
GERMAN = {"new": "neu", "like_new": "neuwertig", "refurbished": "generalüberholt", "used": "gebraucht",
          "defect": "defekt"}


def from_text(raw: str | None) -> str | None:
    """The value a site's condition text names (German or English), None for text it does not know ("Top")."""
    s = str(raw or "").strip().lower()
    return next((value for value, rx in _RULES if s and rx.search(s)), None)


def pair(raw: str | None) -> tuple[str | None, str | None]:
    """(value, the site's words) of a condition text; (None, None) when the text names none."""
    value = from_text(raw)
    return (value, " ".join(str(raw).split())) if value else (None, None)


def _coded(value: str | None, text: str | None) -> tuple[str | None, str | None]:
    """A value a site sent as a code, with its text; a code without text gets the German name, so it still reads as
    stated by the site (a value without a label means inferred)."""
    if not value:
        return None, None
    return value, " ".join((text or "").split()) or GERMAN[value].capitalize()


# ------------------------------------------------------------------ willhaben
# value ids of the tree attribute 21 "Zustand" (the navigator of a Marktplatz search names all six)
WILLHABEN = {"22": ("new", "Neu"), "2546": ("like_new", "Neuwertig"), "2539": ("like_new", "Ausstellungsstück"),
             "5013256": ("refurbished", "Generalüberholt"), "23": ("used", "Gebraucht"), "24": ("defect", "Defekt")}
_WH_PAIRS = re.compile(r"(\d+);(\d+)")


def willhaben_list(raw: str | None) -> tuple[str | None, str | None]:
    """categorytreeattributeids "[21;23, 2535;2536, …]" of a list ad: the value of attribute 21 (Zustand)."""
    return next((WILLHABEN[v] for k, v in _WH_PAIRS.findall(raw or "") if k == "21" and v in WILLHABEN), (None, None))


def willhaben_detail(ad: dict[str, Any]) -> tuple[str | None, str | None]:
    """advertDetails of an ad page: attributeInformation[treeId 21] by value id, else its label; else the
    advertisingParameters "Zustand" text."""
    for info in ad.get("attributeInformation") or []:
        el = (info or {}).get("treeAttributeElement") or {}
        if el.get("treeId") != 21 and el.get("code") != "Zustand":
            continue
        v = next(iter(info.get("values") or []), None) or {}
        if known := WILLHABEN.get(str(v.get("treeId"))):
            return known[0], v.get("label") or known[1]
        if (found := pair(v.get("label")))[0]:
            return found
    raw = (ad.get("advertisingParameters") or {}).get("Zustand")
    return pair(next(iter(raw), None) if isinstance(raw, list) else raw)


# ------------------------------------------------------------------ eBay
SCHEMA_ORG = {"NewCondition": "new", "RefurbishedCondition": "refurbished", "UsedCondition": "used",
              "DamagedCondition": "defect"}


def ebay(text: str | None, schema: str | None = None) -> tuple[str | None, str | None]:
    """A card's or item page's condition text; else the item's JSON-LD itemCondition ("UsedCondition")."""
    found = pair(text)
    return found if found[0] else _coded(SCHEMA_ORG.get(schema or ""), None)


# ------------------------------------------------------------------ Shpock
SHPOCK = {"good": "used",                                         # seen live (shpock_item.html)
          "new": "new", "like_new": "like_new", "as_good_as_new": "like_new", "acceptable": "used",
          "fair": "used", "defective": "defect"}                  # not seen yet: the label decides too


def shpock(extra_properties: list[Any] | None) -> tuple[str | None, str | None]:
    """Item.extraProperties of an item page: the entry "condition" by its value, else its label."""
    for e in extra_properties or []:
        if isinstance(e, dict) and e.get("name") == "condition":
            text = str(e.get("valueLabel") or "")
            return _coded(SHPOCK.get(str(e.get("value") or "")) or from_text(text), text)
    return None, None


# ------------------------------------------------------------------ Facebook Marketplace
# attribute_data entry "Zustand"/"Condition" of an item page (seen live: used_good "Gebraucht – guter Zustand")
FACEBOOK = {"new": "new", "used_like_new": "like_new", "used_good": "used", "used_fair": "used",
            "refurbished": "refurbished", "used_poor": "used"}


def facebook(attributes: list[Any] | None) -> tuple[str | None, str | None]:
    """An item page's attribute_data: the condition entry by its value, else its label."""
    for a in attributes or []:
        if isinstance(a, dict) and str(a.get("value") or "") in FACEBOOK or \
                isinstance(a, dict) and str(a.get("attribute_name") or "").lower() in ("zustand", "condition"):
            text = str(a.get("label") or "")
            return _coded(FACEBOOK.get(str(a.get("value") or "")) or from_text(text), text or None)
    return None, None


# ------------------------------------------------------------------ Kleinanzeigen
# attribute "<category>.condition" (seen live in Handy & Telefon, list and ad: new "Neu", like_new "Sehr Gut",
# ok "Gut", alright "In Ordnung", defect "Defekt"); <category>.battery_condition is another thing
KLEINANZEIGEN = {"new": "new", "like_new": "like_new", "ok": "used", "alright": "used", "defect": "defect"}
# the reverse, for posting an own ad: the form takes these codes; it has no "refurbished", the nearest is
# like_new (the ad text says the rest). A value without a code is left out of the form: None.
CONDITION_TO_KLEINANZEIGEN = {"new": "new", "like_new": "like_new", "refurbished": "like_new",
                              "used": "ok", "defect": "defect"}


def _ka_text(node: Any) -> str | None:
    while isinstance(node, dict) and "value" in node:          # capi nodes: {"value": x}
        node = node["value"]
    if node is None or isinstance(node, (dict, list)):
        return None
    return html.unescape(str(node)).strip() or None


def kleinanzeigen(ad: dict[str, Any]) -> tuple[str | None, str | None]:
    """An ad of the app API (list or detail): its condition attribute; (None, None) in categories without one."""
    attrs = (ad.get("attributes") or {}).get("attribute") or []
    for at in attrs if isinstance(attrs, list) else [attrs]:
        if not isinstance(at, dict):
            continue
        name = str(at.get("name") or "").rsplit(".", 1)[-1]
        if name not in ("condition", "condition_s") and _ka_text(at.get("localized-label")) != "Zustand":
            continue
        values = at.get("value")
        v = (values[0] if values else {}) if isinstance(values, list) else values
        v = v if isinstance(v, dict) else {}
        text = _ka_text(v.get("localized-label"))
        return _coded(KLEINANZEIGEN.get(_ka_text(v.get("value")) or "") or from_text(text), text)
    return None, None


# ------------------------------------------------------------------ Vinted
_VINTED = re.compile(r"Zustand: (Neu, (?:mit|ohne) Etikett|[^,]+)")


def vinted_listing(box: dict[str, Any]) -> tuple[str | None, str | None]:
    """A catalog item's itemBox: "…, Zustand: Sehr gut, …" in its accessibility label (the label itself may hold a
    comma), else the second line when it is a condition; there clothes put the size first ("M · Neu, mit Etikett"),
    so only its last part counts when that names one."""
    m = _VINTED.search(str(box.get("accessibilityLabel") or ""))
    if m and (found := pair(m.group(1)))[0]:
        return found
    line = str(box.get("secondLine") or "")
    if (last := pair(line.rsplit(" · ", 1)[-1]))[0]:
        return last
    return pair(line)


def vinted_item(attrs: dict[str, Any]) -> tuple[str | None, str | None]:
    """The item page's attributes by code: "status" holds the condition ("Neu, mit Etikett")."""
    return pair((attrs.get("status") or {}).get("value"))


# ------------------------------------------------------------------ Preisjäger
def preisjaeger(title: str | None) -> tuple[str | None, None]:
    """Inferred from a deal's title (no label: the site states nothing); a shop deal is new unless the title says
    otherwise. No defect rule: "Ersatzteil-Set" is a product there, not a condition."""
    s = (title or "").lower()
    if re.search(r"refurb|generalüberholt|renewed|b-ware", s):
        return "refurbished", None
    if re.search(r"vorführ|ausstellungsst|wie neu", s):
        return "like_new", None
    if re.search(r"\bgebraucht\b", s):
        return "used", None
    return "new", None


# ------------------------------------------------------------------ display
def label(value: str | None) -> str:
    """The value in the UI language ("" for None)."""
    return {"new": t("New"), "like_new": t("Like new"), "refurbished": t("Refurbished"), "used": t("Used"),
            "defect": t("Defective")}.get(value or "", "")


def shown(value: str | None, label_: str | None = None, platform: str | None = None) -> str | None:
    """What a pill or details line shows: the site's words, else the value's name; None when unknown."""
    if not value:
        return None
    if label_:
        return label_
    if platform == "preisjaeger" and value == "new":
        return t("New (shop deal)")
    return label(value)


def tone(value: str | None) -> str:
    """ui.pill tone: new stands out, defect is red, the rest is plain."""
    return {"new": "brand", "defect": "bad"}.get(value or "", "")
