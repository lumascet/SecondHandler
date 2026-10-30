"""Placing a marketplace ad (free private ad, "bap", adTypeId/productId 67) like willhaben's own form.

Taken from the web app bundle /bbx-adin (2026-10-09); every call runs as fetch() in the logged-in page.

  form      GET  /iad/anzeigenaufgabe/marktplatz?adTypeId=67&productId=67   (HTML, __NEXT_DATA__)
            -> pageProps.ad (template incl. location/contact), categoryTree, prefillAttributes
  category  GET  /webapi/iad/bap/categorysuggestion/attributetree?title=…&adTypeId=67&productId=67
  presave   POST /webapi/iad/bap/presave           ad JSON -> ad with adId, adUuid, contextLinkList (draft)
  images    POST /webapi/iad-upload/atimage/temp   multipart "file" -> {reference, …}
            PUT  /webapi/iad-upload<imageList>/order-and-associate  {advertImage:[{reference}], floorPlans:[]}
  save      POST /webapi/iad/bap/save              ad JSON (with adId)
  delivery  (only with Versand, NEEDS LIVE VERIFICATION - read from the bundle, never run against willhaben)
            PUT  /webapi/iad/bap/edit              ad JSON with treeAttributes Uebergabe = [PICKUP?, Versand]
            GET  /webapi/<p2ppUserOptions link>    PayLivery options (deliveryOptions[] per parcel size/carrier)
            PUT  /webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery  {adId, deliveryOptions: [uuid…]}
  cart      POST /webapi/iad/products/privates/atz/<adUuid>/carts  {currency:"EUR"} -> {shoppingCartId, status:"Open"}
            PUT  /webapi/iad/products/privates/carts/<id>/items     {productReferences: []} -> {totalPrice}
            POST /webapi/iad/products/privates/carts/<id>/checkout  {billingData: null} -> {status:"ACTIVATED"}

This module stops if anything would cost money (totalPrice > 0 or a paymentUrl) - nothing is ever paid.
"""
from __future__ import annotations

import re

import logging
import time
from typing import Any
from urllib.parse import quote

from app.willhaben.parse import extract_next_data
from app.willhaben.session import BrowserSession, NotLoggedIn

log = logging.getLogger(__name__)

AD_TYPE = PRODUCT = 67          # "Marktplatz – kostenlose Anzeige" (private)
MAX_PHOTOS = 15
FORM_PATH = f"/iad/anzeigenaufgabe/marktplatz?adTypeId={AD_TYPE}&productId={PRODUCT}"


class InsertError(RuntimeError):
    pass


async def load_form(s: BrowserSession) -> dict[str, Any]:
    """pageProps of the empty insertion form: ad template, category tree with required attributes. From the app API
    when it is logged in (the same template and tree), else from the web page."""
    from app.willhaben import native

    try:
        if (form := await native.insertion_form()) is not None:
            return form
    except native.NativeError as e:
        log.info("insertion form through the app API failed, the web page gives it: %s", e)
    r = await s.api("GET", FORM_PATH, accept="text/html")
    nd = extract_next_data(r.text) if r.ok else None
    if not nd or nd.get("page") != "/iad/anzeigenaufgabe/marktplatz":
        raise NotLoggedIn(f"Anzeigenformular nicht erreichbar (HTTP {r.status}, eingeloggt?)")
    return (nd.get("props") or {}).get("pageProps") or {}


async def suggest_categories(s: BrowserSession, title: str) -> Any:
    path = (f"/webapi/iad/bap/categorysuggestion/attributetree?title={quote(title)}"
            f"&adTypeId={AD_TYPE}&productId={PRODUCT}")
    return await s.api_json("GET", path)


def category_index(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """code -> {label, path (labels root..node), codes, required attribute codes, leaf}."""
    out: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], labels: list[str], codes: list[str], inherited: list[dict[str, Any]]):
        refs = inherited + [r for r in node.get("attributeReferences") or [] if isinstance(r, dict)]
        here_labels, here_codes = labels + [node.get("label") or ""], codes + [node.get("code") or ""]
        children = node.get("children") or []
        if node.get("code"):
            out[node["code"]] = {"label": node.get("label"), "path": here_labels[1:], "codes": here_codes[1:],
                                 "required": [r["code"] for r in refs if r.get("required")],
                                 "attributes": refs, "leaf": not children}
        for ch in children:
            walk(ch, here_labels, here_codes, refs)

    walk(tree.get("categoryNode") or {}, [], [], [])
    return out


def attribute_values(tree: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """attribute code -> {label, values: {code: label}} from the category tree."""
    out = {}
    for a in tree.get("attributes") or []:
        el = a.get("treeAttributeElement") or {}
        if el.get("code"):
            out[el["code"]] = {"label": el.get("label"),
                               "values": {v.get("code"): v.get("label") for v in a.get("values") or [] if v.get("code")}}
    return out


FORM_CACHE_S = 3600.0
_form_cache: dict[str, Any] = {}


async def cached_form(s: BrowserSession, max_age: float = FORM_CACHE_S) -> dict[str, Any]:
    """load_form() kept in memory for about an hour: the category tree barely changes and each load is a
    logged-in GET. Publishing itself always loads a fresh form (context links, contact)."""
    hit = _form_cache.get("form")
    if hit and time.monotonic() - _form_cache.get("at", 0) < max_age:
        return hit
    form = await load_form(s)
    _form_cache.update(form=form, at=time.monotonic())
    return form


def remember_form(form: dict[str, Any]) -> None:
    _form_cache.update(form=form, at=time.monotonic())


def clear_form_cache() -> None:
    _form_cache.clear()


def search_categories(tree: dict[str, Any], query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Leaf categories whose name or path contains every word of the query, best (name match, short path) first."""
    words = [_norm(w) for w in (query or "").split() if _norm(w)]
    if not words:
        return []
    hits = []
    for code, info in category_index(tree).items():
        if not info.get("leaf"):
            continue
        label, hay = _norm(info.get("label")), _norm(" ".join(info.get("path") or []))
        if all(w in hay for w in words):
            hits.append((0 if all(w in label for w in words) else 1, len(info["path"]), " › ".join(info["path"]), code))
    hits.sort()
    return [{"code": code, "label": path.split(" › ")[-1], "path": path} for _, _, path, code in hits[:limit]]


async def get_own_ad(s: BrowserSession, ad_id: str) -> dict[str, Any]:
    """An own ad in the editor's JSON format (read-only), the same shape presave/save take."""
    return await s.api_json("GET", f"/webapi/iad/bap/{int(ad_id)}")


def description_text(html_text: str | None) -> str:
    """The editor's HTML description → plain text for editing (inverse of description_html). Each <p> is a line, an
    empty one (<p><br></p>) a blank line; older ads with one <p> per paragraph read as lines too, as willhaben shows them."""
    import html as _html
    t_ = html_text or ""
    t_ = re.sub(r"(?i)<p[^>]*>\s*(?:<br\s*/?>\s*)?</p>", "\n", t_)
    t_ = re.sub(r"(?i)<li[^>]*>\s*", "• ", t_)
    t_ = re.sub(r"(?i)</li>|<br\s*/?>|</p>|</div>", "\n", t_)        # </ul>: its last </li> ended the line
    t_ = re.sub(r"<[^>]+>", "", t_)
    t_ = _html.unescape(t_)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in t_.split("\n"))).strip()


async def edit_own_ad(s: BrowserSession, ad_id: str, *, heading: str, description: str,
                      price: float | None) -> dict[str, Any]:
    """Change title, description and price of an own ad like willhaben's edit page.

    Loads the complete editor JSON, changes only these three fields and PUTs the whole object back to
    bap/edit (a partial body would wipe the other fields). Never creates a cart or checkout. Afterwards the ad
    is read again; raises InsertError if willhaben did not take the change. NEEDS LIVE VERIFICATION whether an
    edit of an active ad goes live at once or into willhaben's review first (the status shows it).
    """
    ad = await get_own_ad(s, ad_id)
    if not isinstance(ad, dict) or not ad.get("adId"):
        raise InsertError(f"Anzeige {ad_id} nicht im Editor gefunden")
    body = dict(ad)
    body["heading"] = heading.strip()[:80]
    body["description"] = description_html(description)
    body["price"] = int(price) if price is not None and float(price).is_integer() else price
    await s.api_json("PUT", "/webapi/iad/bap/edit", body)
    after = await get_own_ad(s, ad_id)
    got = (after.get("heading"), description_text(after.get("description")), after.get("price"))
    want = (body["heading"], description_text(body["description"]), body["price"])
    if got[0] != want[0] or got[1] != want[1] or (want[2] is not None and float(got[2] or 0) != float(want[2])):
        raise InsertError("willhaben hat die Änderung nicht übernommen")
    return after


# ------------------------------------------------------------------ photos of an own ad
# Like the edit page's image hook (bbx-adin buildId 1058731-20d7e775, chunk 9792 module 83070): existing photos are
# kept by their `reference`, new ones uploaded to atimage/temp first, then ONE call sets the whole list in order:
#   PUT /webapi/iad-upload<imageList link>/order-and-associate  {advertImage: [{reference}…], floorPlans: […]}
# Photos left out are removed, the first photo is the cover. The edit page sends it after PUT bap/edit; the photo
# call stands on its own (nothing else is sent). NEEDS LIVE VERIFICATION: the GET shape of the imageList link.
def _images(raw: Any) -> dict[str, list[dict[str, Any]]]:
    if isinstance(raw, dict) and "advertImageList" in raw and "advertImage" not in raw:
        raw = raw.get("advertImageList") or {}
    raw = raw if isinstance(raw, dict) else {"advertImage": raw if isinstance(raw, list) else []}
    return {"advertImage": [i for i in raw.get("advertImage") or [] if isinstance(i, dict) and i.get("reference")],
            "floorPlans": [i for i in raw.get("floorPlans") or [] if isinstance(i, dict) and i.get("reference")]}


def image_thumb(img: dict[str, Any]) -> str:
    """A small preview URL of an existing photo (willhaben's public image cache)."""
    url = img.get("thumbnailImageUrl") or img.get("mainImageUrl") or img.get("referenceImageUrl")
    if url:
        return url
    ref = str(img.get("reference") or "")
    stem, dot, ext = ref.rpartition(".")
    return f"https://cache.willhaben.at/mmo/{stem}_thumb.{ext}" if dot else f"https://cache.willhaben.at/mmo/{ref}"


async def get_own_images(s: BrowserSession, ad: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """{advertImage: [...], floorPlans: [...]} of an own ad (editor JSON from get_own_ad), read-only."""
    link = _link(ad, "imageList")
    if not link:
        raise InsertError("kein imageList-Link in der Anzeige")
    return _images(await s.api_json("GET", _webapi(link)))


async def set_own_images(s: BrowserSession, ad_id: str, order: list[str],
                         new_photos: list[tuple[str, bytes, str]]) -> list[dict[str, Any]]:
    """Set the photos of an own ad: `order` lists existing references and "new:<i>" (index into new_photos).

    Existing references must belong to the ad (nothing foreign can be attached); photos not named are removed.
    Returns willhaben's new photo list; raises InsertError when the result does not match.
    """
    ad = await get_own_ad(s, ad_id)
    if not isinstance(ad, dict) or not ad.get("adId"):
        raise InsertError(f"Anzeige {ad_id} nicht im Editor gefunden")
    link = _link(ad, "imageList")
    if not link:
        raise InsertError("kein imageList-Link in der Anzeige")
    current = _images(await s.api_json("GET", _webapi(link)))
    known = {i["reference"] for i in current["advertImage"]}
    used_new: set[int] = set()
    for item in order:
        if item.startswith("new:"):
            idx = int(item[4:]) if item[4:].isdigit() else -1
            if not 0 <= idx < len(new_photos) or idx in used_new:
                raise InsertError(f"unbekanntes neues Foto: {item}")
            used_new.add(idx)
        elif item not in known:
            raise InsertError("Foto gehört nicht zu dieser Anzeige")
    if not order:
        raise InsertError("Eine Anzeige braucht mindestens ein Foto")
    if len(order) > MAX_PHOTOS:
        raise InsertError(f"höchstens {MAX_PHOTOS} Fotos")
    if len(set(order)) != len(order):
        raise InsertError("Foto doppelt in der Reihenfolge")
    if order == [i["reference"] for i in current["advertImage"]]:
        return current["advertImage"]                       # unchanged: like the web app, send nothing

    temp: dict[int, str] = {}
    for idx in sorted(used_new):
        name, data, mime = new_photos[idx]
        r = await s.upload("/webapi/iad-upload/atimage/temp", "file", name, data, mime)
        if not r.ok:
            raise InsertError(f"Foto-Upload {name}: HTTP {r.status} {r.text[:150]}")
        ref = (r.json() or {}).get("reference")
        if not ref:
            raise InsertError(f"Foto-Upload {name}: keine Referenz")
        temp[idx] = ref
    refs = [temp[int(i[4:])] if i.startswith("new:") else i for i in order]
    body = {"advertImage": [{"reference": r} for r in refs],
            "floorPlans": [{"reference": f["reference"]} for f in current["floorPlans"]]}
    res = await s.api_json("PUT", _webapi(link, upload=True) + "/order-and-associate", body)
    after = _images(res)["advertImage"] if isinstance(res, dict) and res.get("advertImage") is not None else \
        _images(await s.api_json("GET", _webapi(link)))["advertImage"]
    kept = [(pos, r) for pos, r in enumerate(refs) if r in known]
    if len(after) != len(refs) or any(after[pos].get("reference") != r for pos, r in kept):
        raise InsertError("willhaben hat die Fotos nicht wie gewünscht übernommen")
    return after


# ------------------------------------------------------------------ plan (read-only)
PICKUP = {"label": "Selbstabholung", "code": "PICKUP"}
SHIPPING = {"label": "Versand", "code": "Versand"}     # bundle module 76697: I2="Uebergabe", z5="Versand", $9="PICKUP"
HANDOVER = "Uebergabe"
# Übergabe choice in the UI -> value nodes. Everything except "pickup" NEEDS LIVE VERIFICATION.
HANDOVERS = {"pickup": [PICKUP], "shipping": [SHIPPING], "both": [PICKUP, SHIPPING]}
PARCEL_SIZES = ("XS", "S", "M", "L")                  # PayLivery sizes offered by willhaben's form (bundle: eT)
PAYLIVERY_CARRIERS = ("Post", "dpd", "Hermes")         # carrier.description values seen in the bundle (eC)

# The PayLivery offers (what the buyer pays for shipping, and what fits) as willhaben listed them for an ad on
# 2026-10-19 (deliveryOptions of the p2ppUserOptions link). Each publish with PayLivery stores the live list
# (setting "paylivery_offers", offers_of()), which then replaces this one in the form.
OFFERS_SEEN = "2026-10-19"
OFFERS: list[dict[str, Any]] = [
    {"size": "S", "carrier": "Post", "price": 5.7, "max_kg": 3.0, "limits": "max. L 100cm × B 60cm × H 60cm, bis 3kg",
     "liability": "Haftung bis € 510"},
    {"size": "S", "carrier": "Hermes", "price": 5.7, "max_kg": 31.5,
     "limits": "Längste + kürzeste Seite = max. 50cm, bis 31,5kg", "liability": "Haftung bis € 500"},
    {"size": "S", "carrier": "dpd", "price": 3.49, "original_price": 5.6, "max_kg": 3.0,
     "limits": "(Höhe + Breite) × 2 + Länge = max. 3m, Länge max. 1,75m, bis 3kg", "liability": "Haftung bis € 520"},
    {"size": "M", "carrier": "Post", "price": 7.9, "max_kg": 10.0, "limits": "max. L 100cm × B 60cm × H 60cm, bis 10kg",
     "liability": "Haftung bis € 510"},
    {"size": "M", "carrier": "Hermes", "price": 6.4, "max_kg": 31.5,
     "limits": "Längste + kürzeste Seite = max. 80cm, bis 31,5kg", "liability": "Haftung bis € 500"},
    {"size": "M", "carrier": "dpd", "price": 7.5, "max_kg": 10.0,
     "limits": "(Höhe + Breite) × 2 + Länge = max. 3m, Länge max. 1,75m, bis 10kg", "liability": "Haftung bis € 520"},
    {"size": "L", "carrier": "Post", "price": 9.9, "max_kg": 31.5,
     "limits": "max. L 100cm × B 60cm × H 60cm, bis 31,5kg", "liability": "Haftung bis € 510"},
    {"size": "L", "carrier": "Hermes", "price": 6.9, "max_kg": 31.5,
     "limits": "Längste + kürzeste Seite = max. 120cm, bis 31,5kg", "liability": "Haftung bis € 500"},
    {"size": "L", "carrier": "dpd", "price": 9.7, "max_kg": 31.5,
     "limits": "(Höhe + Breite) × 2 + Länge = max. 3m, Länge max. 1,75m, bis 31,5kg", "liability": "Haftung bis € 520"},
]


def _kg(text: str) -> float | None:
    m = re.search(r"bis\s*(\d+(?:[.,]\d+)?)\s*kg", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


def offers_of(options: dict[str, Any]) -> list[dict[str, Any]]:
    """willhaben's deliveryOptions (p2ppUserOptions) in the shape of OFFERS, smallest size first."""
    order = {s: i for i, s in enumerate(PARCEL_SIZES)}
    out = []
    for o in options.get("deliveryOptions") or []:
        size = ((o.get("parcelSize") or {}).get("size") or "").upper()
        carrier = (o.get("carrier") or {}).get("description") or ""
        try:
            price = float(o.get("price"))
        except (TypeError, ValueError):
            continue
        if not size or not carrier:
            continue
        limits = re.sub(r",\s*quaderförmiges Paket$", "", str(o.get("description") or "").strip())
        offer = {"size": size, "carrier": carrier, "price": price, "max_kg": _kg(limits), "limits": limits,
                 "liability": str(o.get("parcelInsurance") or "")}
        try:
            if float(o.get("originalPrice")) > price:
                offer["original_price"] = float(o["originalPrice"])
        except (TypeError, ValueError):
            pass
        out.append(offer)
    return sorted(out, key=lambda x: (order.get(x["size"], 9), x["price"]))


def preselect_offer(offers: list[dict[str, Any]], weight_kg: float | None, size: str | None = None,
                    carrier: str | None = None) -> dict[str, Any] | None:
    """The offer the form shows selected: the one chosen before (size and carrier), else the cheapest that takes the
    parcel's weight."""
    chosen = next((o for o in offers if o["size"] == (size or "").upper() and carrier
                   and o["carrier"].lower() == carrier.lower()), None)
    if chosen is None and size:          # a size chosen with "cheapest carrier"
        same = [o for o in offers if o["size"] == size.upper()]
        chosen = min(same, key=lambda o: o["price"]) if same else None
    return chosen or pick_offer(offers, weight_kg)


def pick_offer(offers: list[dict[str, Any]], weight_kg: float | None) -> dict[str, Any] | None:
    """The cheapest offer whose weight limit takes the parcel (smaller size first on a tie); None without offers."""
    order = {s: i for i, s in enumerate(PARCEL_SIZES)}
    fits = [o for o in offers if not weight_kg or not o.get("max_kg") or weight_kg <= o["max_kg"]] or offers
    return min(fits, key=lambda o: (o["price"], order.get(o["size"], 9))) if fits else None


def suggestion_paths(raw: Any) -> list[list[dict[str, str]]]:
    """Category suggestions as categoryPath lists [{label, code}, …] root → leaf, without duplicates."""
    out, seen = [], set()
    for top in (raw or {}).get("categories") or []:
        path, node = [], top
        while isinstance(node, dict):
            path.append({"label": node.get("name") or node.get("label") or "", "code": node.get("code") or ""})
            kids = node.get("categories") or []
            node = kids[0] if kids else None
        if path and path[-1]["code"] not in seen:
            seen.add(path[-1]["code"])
            out.append(path)
    return out


def _norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def guess_value(attr_label: str, values: dict[str, str], draft_attrs: list[dict[str, Any]], condition: str | None) -> str | None:
    """Pre-select a value code from what Claude wrote (condition, attributes like Marke/Farbe)."""
    by_label = {_norm(lbl): code for code, lbl in values.items()}
    wanted = []
    if _norm(attr_label) == "zustand" and condition:
        wanted.append(condition)
    wanted += [a.get("value") for a in draft_attrs or [] if _norm(a.get("name")) == _norm(attr_label)]
    for w in wanted:
        n = _norm(w)
        if n in by_label:
            return by_label[n]
        hit = next((code for lbl, code in by_label.items() if n and (n in lbl or lbl in n)), None)
        if hit:
            return hit
    return None


def _option(cats: dict[str, Any], vals: dict[str, Any], path: list[dict[str, str]], condition: str | None,
            attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """One category option: path root → leaf plus its required fields with a guessed value."""
    info = cats.get(path[-1]["code"]) or {}
    fields = []
    for code in dict.fromkeys(info.get("required") or []):
        if code == HANDOVER:
            continue                       # chosen separately ("Übergabe": pickup / shipping)
        a = vals.get(code) or {"label": code, "values": {}}
        fields.append({"code": code, "label": a["label"], "values": list(a["values"].items()),
                       "selected": guess_value(a["label"], a["values"], attributes or [], condition)})
    return {"path": path, "label": " › ".join(p["label"] for p in path), "fields": fields}


def manual_option(form: dict[str, Any], code: str, *, condition: str | None,
                  attributes: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """A category the user picked from the whole tree (search box), in the same shape as build_plan's options."""
    tree = form.get("categoryTree") or {}
    cats = category_index(tree)
    info = cats.get(code)
    if not info or not info.get("leaf"):
        return None
    path = [{"label": lbl, "code": c} for lbl, c in zip(info["path"], info["codes"], strict=False)]
    return {**_option(cats, attribute_values(tree), path, condition, attributes), "manual": True}


def hint_option(form: dict[str, Any], hint: str | None, *, condition: str | None,
                attributes: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Claude's category_hint ("KFZ-Zubehör/Motorradteile > E-Mobilität > Ladestationen/Wallboxen") as an option, when
    willhaben's own suggestion finds nothing: the one leaf whose path ends in the hint's segments, else the one leaf of
    that name; several ("Zubehör") give none.
    Split only on > and ›, a slash is part of willhaben's labels."""
    want = [_norm(p) for p in re.split(r"[>›]", hint or "") if _norm(p)]
    if not want:
        return None
    tree = form.get("categoryTree") or {}
    cats = category_index(tree)
    leaves = [(code, info) for code, info in cats.items() if info.get("leaf")]
    hits = [c for c, info in leaves if [_norm(p) for p in info["path"]][-len(want):] == want]
    if not hits:
        hits = [c for c, info in leaves if _norm(info.get("label")) == want[-1]]
    if len(hits) != 1:              # none, or several ("Zubehör"): no guess – the owner picks one
        return None
    info = cats[hits[0]]
    path = [{"label": lbl, "code": c} for lbl, c in zip(info["path"], info["codes"], strict=False)]
    return _option(cats, attribute_values(tree), path, condition, attributes)


def build_plan(form: dict[str, Any], raw_suggestions: Any, *, condition: str | None,
               attributes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Everything the user confirms before publishing: category options and required fields per option."""
    tree = form.get("categoryTree") or {}
    cats, vals = category_index(tree), attribute_values(tree)
    options = [_option(cats, vals, path, condition, attributes) for path in suggestion_paths(raw_suggestions)[:5]]
    ad = form.get("ad") or {}
    return {"options": options, "location": " ".join(x for x in (ad.get("postCode"), ad.get("location")) if x),
            "contact": " ".join(x for x in (ad.get("firstName"), ad.get("surName")) if x)}


# ------------------------------------------------------------------ publish (writes, only on the user's click)
def description_html(text: str) -> str:
    """Plain text → the editor's HTML as willhaben's own editor writes it: one <p> per line, a blank line as
    <p><br></p> (willhaben shows paragraphs without a gap, so "<p>a</p><p>b</p>" loses the blank line), runs of
    "• "/"- " lines as a list."""
    import html as _html

    blocks: list[str] = []
    items: list[str] = []
    blank = False
    for ln in (text or "").replace("\r\n", "\n").strip().split("\n"):
        ln = ln.strip()
        if ln.startswith(("• ", "- ")):
            items.append(f"<li>{_html.escape(ln[2:].strip())}</li>")
            continue
        if items:
            blocks.append(f"<ul>{''.join(items)}</ul>")
            items = []
        if not ln:
            if not blank:             # several blank lines in a row: one gap
                blocks.append("<p><br></p>")
            blank = True
            continue
        blank = False
        blocks.append(f"<p>{_html.escape(ln)}</p>")
    if items:
        blocks.append(f"<ul>{''.join(items)}</ul>")
    return "".join(blocks)


def _link(ad: dict[str, Any], *ids: str) -> dict[str, Any] | None:
    links = {link.get("id"): link for link in (ad.get("contextLinkList") or {}).get("contextLink") or []}
    return next((links[i] for i in ids if links.get(i, {}).get("relativePath")), None)


def _webapi(link: dict[str, Any], upload: bool = False) -> str:
    svc = link.get("serviceName") or "iad"
    if upload and svc == "iad":
        svc = "iad-upload"
    return f"/webapi/{svc}{link['relativePath']}"


def compose_ad(template: dict[str, Any], *, heading: str, description: str, price: float | None,
               path: list[dict[str, str]], attrs: dict[str, tuple[str, str]], attr_labels: dict[str, str],
               handover: str = "pickup") -> dict[str, Any]:
    import copy

    ad = copy.deepcopy(template)
    ad["heading"] = heading.strip()[:80]
    ad["description"] = description_html(description)
    ad["price"] = (int(price) if price is not None and float(price).is_integer() else price)
    ad["categoryPath"] = {"path": path}
    tree_attrs = [{"attributeNode": {"label": attr_labels.get(code, code), "code": code},
                   "selectedAttributeValueNodes": [{"label": label, "code": value}]}
                  for code, (value, label) in attrs.items()]
    tree_attrs.append({"attributeNode": {"label": "Übergabe", "code": HANDOVER},
                       "selectedAttributeValueNodes": [dict(n) for n in HANDOVERS.get(handover, [PICKUP])]})
    ad["treeAttributes"] = tree_attrs
    ad.pop("treeAttributeIds", None)
    return ad


STEPS = ("presave", "photos", "save", "delivery", "cart")   # stop_after: halt before the ad goes public (checkout)


def _shape(obj: Any) -> Any:
    """Structure of a response without personal data: keys, link ids, a few status fields."""
    if not isinstance(obj, dict):
        return type(obj).__name__
    links = [link.get("id") for link in (obj.get("contextLinkList") or {}).get("contextLink") or []]
    keep = {k: obj.get(k) for k in ("adId", "adUuid", "status", "totalPrice", "shoppingCartId", "productId",
                                      "paymentUrl", "advertStatus") if k in obj}
    return {"keys": sorted(obj), "links": links, **keep}


def _with_handover(ad: dict[str, Any], nodes: list[dict[str, str]]) -> dict[str, Any]:
    """The ad with its Uebergabe attribute replaced, like the bundle's handleFormSubmit does."""
    keep = [a for a in ad.get("treeAttributes") or [] if (a.get("attributeNode") or {}).get("code") != HANDOVER]
    return {**ad, "treeAttributes": keep + [{"attributeNode": {"label": "Übergabe", "code": HANDOVER},
                                             "selectedAttributeValueNodes": [dict(n) for n in nodes]}]}


def pick_delivery_option(options: dict[str, Any], size: str | None, carrier: str | None) -> dict[str, Any] | None:
    """The PayLivery deliveryOption for a parcel size (and carrier, else the cheapest)."""
    cands = [o for o in options.get("deliveryOptions") or []
             if ((o.get("parcelSize") or {}).get("size") or "").upper() == (size or "").upper()]
    if carrier:
        cands = [o for o in cands if ((o.get("carrier") or {}).get("description") or "").lower() == carrier.lower()]

    def price(o: dict[str, Any]) -> float:
        try:
            return float(o.get("price") or 0)
        except (TypeError, ValueError):
            return 0.0
    return min(cands, key=price) if cands else None


async def set_delivery(s: BrowserSession, ad: dict[str, Any], shipping: dict[str, Any]) -> dict[str, Any]:
    """Shipping step of willhaben's "Übergabe & Versand" page (/iad/anzeigenaufgabe/marktplatz/versandoptionen).

    NEEDS LIVE VERIFICATION: read from the bbx-adin bundle (buildId 1058731-20d7e775, chunk 9691 modules 41117/54527/
    35688), never run against willhaben. The page does:
      1. PUT /webapi/iad/bap/edit  with the ad, treeAttributes Uebergabe = [{code:PICKUP}?, {code:Versand}]
      2. GET /webapi/<serviceName><relativePath> of the ad's contextLink "p2ppUserOptions"   (PayLivery offers)
      3. on "Weiter": PUT bap/edit again (same body; skipped here) + PUT /webapi/p2p-aggregator/restapi/v2/p2pp/
         options/delivery {adId, deliveryOptions: [deliveryOption.uuid, …add-on uuids]}  ([] = Versand without PayLivery)
    Add-ons (oversize, "fragile") are not offered here.
    The ad stays a non-public draft throughout; the cart afterwards keeps the money guard."""
    nodes = HANDOVERS.get(shipping.get("handover") or "", [SHIPPING])
    body = _with_handover(ad, nodes)
    edited = await s.api_json("PUT", "/webapi/iad/bap/edit", body)
    out: dict[str, Any] = {"handover": [n["code"] for n in nodes], "edit": _shape(edited)}
    chosen: list[str] = []
    if shipping.get("paylivery"):
        link = _link(edited if isinstance(edited, dict) else {}, "p2ppUserOptions") or _link(body, "p2ppUserOptions")
        if not link:
            raise InsertError("PayLivery: willhaben bietet für diese Anzeige keine Versandoptionen an")
        opts = await s.api_json("GET", _webapi(link))
        opts = opts if isinstance(opts, dict) else {}
        if opts.get("sellerOnboardingDTO"):
            raise InsertError("PayLivery ist für dein Konto noch nicht eingerichtet – bitte einmal auf willhaben "
                              "erledigen (Bankdaten), dann erneut versuchen")
        opt = pick_delivery_option(opts, shipping.get("size"), shipping.get("carrier"))
        if not opt or not opt.get("uuid"):
            sizes = sorted({(o.get("parcelSize") or {}).get("size") or "?" for o in opts.get("deliveryOptions") or []})
            raise InsertError(f"PayLivery: keine Versandoption für Größe {shipping.get('size') or '?'}"
                              f"{' / ' + shipping['carrier'] if shipping.get('carrier') else ''}"
                              f" (angeboten: {', '.join(sizes) or 'keine'})")
        chosen = [opt["uuid"]]
        out["offers"] = offers_of(opts)
        out["option"] = {"size": (opt.get("parcelSize") or {}).get("size"),
                         "carrier": (opt.get("carrier") or {}).get("description"), "price": opt.get("price")}
    res = await s.api_json("PUT", "/webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery",
                           {"adId": ad.get("adId"), "deliveryOptions": chosen})
    out["delivery"] = _shape(res)
    return out


async def publish(s: BrowserSession, ad: dict[str, Any], photos: list[tuple[str, bytes, str]],
                  existing_ad_id: str | None = None, on_presaved=None, stop_after: str | None = None,
                  shipping: dict[str, Any] | None = None, on_delivery=None, on_step=None) -> dict[str, Any]:
    """presave → photos → save → [delivery] → empty cart → checkout. Returns {ad_id, ad_uuid, url}.

    shipping {"handover": "pickup"|"shipping"|"both", "paylivery": bool, "size", "carrier"}: anything but pickup
    runs set_delivery() between save and cart (NEEDS LIVE VERIFICATION). None/pickup = the live-verified path.
    stop_after ("presave" | "photos" | "save" | "delivery" | "cart") returns {"stopped": step, ...} with the response
    structures instead; everything before checkout stays a non-public willhaben draft.
    on_delivery() is called once set_delivery() went through (a retry switching to pickup must not leave
    PayLivery options behind, see ad_creator). on_step(step, i, n) is called before each stage for progress:
    "presave", "photo" (i of n), "save", "delivery", "activate"."""
    trace: dict[str, Any] = {}
    step = on_step or (lambda *_a: None)
    step("presave", 0, 0)
    if existing_ad_id:          # a former attempt already created the willhaben draft: continue with it
        base = await get_own_ad(s, existing_ad_id)
        ad = {**ad, "adId": base.get("adId"), "adUuid": base.get("adUuid"), "contextLinkList": base.get("contextLinkList")}
    else:
        presave = _link(ad, "presave")
        if not presave:
            raise InsertError("Formular ohne presave-Link")
        pre = await s.api_json("POST", _webapi(presave), ad)
        ad = {**ad, "adId": pre.get("adId"), "adUuid": pre.get("adUuid"), "contextLinkList": pre.get("contextLinkList"),
              "productId": pre.get("productId") or ad.get("productId"), "taggingData": pre.get("taggingData") or ad.get("taggingData")}
        trace["presave"] = _shape(pre)
        if on_presaved and ad.get("adId"):
            on_presaved(str(ad["adId"]))
    if not ad.get("adId"):
        raise InsertError("willhaben hat keine Anzeigen-ID vergeben")
    if stop_after == "presave":
        return {"stopped": "presave", "ad_id": str(ad["adId"]), "trace": trace}

    if photos:
        refs = []
        for i, (name, data, mime) in enumerate(photos, 1):
            step("photo", i, len(photos))
            r = await s.upload("/webapi/iad-upload/atimage/temp", "file", name, data, mime)
            if not r.ok:
                raise InsertError(f"Foto-Upload {name}: HTTP {r.status} {r.text[:150]}")
            refs.append({"reference": (r.json() or {}).get("reference")})
        img = _link(ad, "imageList")
        if not img:
            raise InsertError("kein imageList-Link nach dem Vorspeichern")
        assoc = await s.api_json("PUT", _webapi(img, upload=True) + "/order-and-associate",
                                 {"advertImage": refs, "floorPlans": []})
        trace["photos"] = {"uploaded": len(refs), "associate": _shape(assoc)}
    if stop_after == "photos":
        return {"stopped": "photos", "ad_id": str(ad["adId"]), "trace": trace}

    save = _link(ad, "selfLinkFinish", "selfLinkSave")
    if not save:
        raise InsertError("kein Speichern-Link")
    step("save", 0, 0)
    saved = await s.api_json("POST", _webapi(save), ad)
    ad_id, ad_uuid = str(saved.get("adId") or ad["adId"]), saved.get("adUuid") or ad.get("adUuid")
    trace["save"] = _shape(saved)
    if stop_after == "save":
        return {"stopped": "save", "ad_id": ad_id, "trace": trace}

    if shipping and shipping.get("handover") in ("shipping", "both"):
        # Like the bundle's versandoptionen page (module 41117: {...e.ad, treeAttributes: …Uebergabe}), edit the ad
        # willhaben holds after saving - not our composed template, which lacks the photo association and other
        # server-side state a full-ad PUT bap/edit could wipe. NEEDS LIVE VERIFICATION.
        step("delivery", 0, 0)
        base = await get_own_ad(s, ad_id)
        if not isinstance(base, dict) or not base.get("adId"):
            raise InsertError("Versand: gespeicherte Anzeige nicht lesbar – abgebrochen, nichts veröffentlicht")
        trace["delivery"] = await set_delivery(s, base, shipping)
        if on_delivery:
            on_delivery()
    if stop_after == "delivery":
        return {"stopped": "delivery", "ad_id": ad_id, "trace": trace}

    step("activate", 0, 0)
    cart = await s.api_json("POST", f"/webapi/iad/products/privates/atz/{ad_uuid}/carts", {"currency": "EUR"})
    cart_id = cart.get("shoppingCartId")
    items = await s.api_json("PUT", f"/webapi/iad/products/privates/carts/{cart_id}/items", {"productReferences": []})
    trace["cart"] = {"cart": _shape(cart), "items": _shape(items)}
    if (items.get("totalPrice") or 0) > 0 or items.get("paymentUrl"):
        raise InsertError(f"willhaben verlangt {items.get('totalPrice')} € – abgebrochen, nichts bezahlt")
    if stop_after == "cart":
        return {"stopped": "cart", "ad_id": ad_id, "trace": trace}
    done = await s.api_json("POST", f"/webapi/iad/products/privates/carts/{cart_id}/checkout", {"billingData": None})
    if (done or {}).get("status") != "ACTIVATED":
        raise InsertError(f"Anzeige nicht aktiviert (Status {(done or {}).get('status')}"
                          f"{', Zahlung verlangt' if (done or {}).get('paymentUrl') else ''}) – nichts bezahlt")
    trace["checkout"] = _shape(done)
    return {"ad_id": ad_id, "ad_uuid": ad_uuid, "url": f"https://www.willhaben.at/iad/object?adId={ad_id}", "trace": trace}


# ------------------------------------------------------------------ an own ad again as a new insertion
# A sold ad offers neither "Aktivieren" nor "Neu veröffentlichen" ("Auf verkauft gesetzte Anzeigen können nicht wieder
# aktiviert oder neu veröffentlicht werden"): it is placed again like a new ad from its editor JSON. NEEDS LIVE
# VERIFICATION: whether a sold ad's editor JSON, photo list and p2ppUserOptions can still be read.
COPY_KEYS = ("heading", "description", "price", "categoryPath", "treeAttributes")


def handover_of(ad: dict[str, Any]) -> str:
    """pickup | shipping | both from the ad's Uebergabe attribute (pickup when it has none)."""
    nodes = next((a.get("selectedAttributeValueNodes") or [] for a in ad.get("treeAttributes") or []
                  if (a.get("attributeNode") or {}).get("code") == HANDOVER), [])
    codes = {n.get("code") for n in nodes}
    if SHIPPING["code"] not in codes:
        return "pickup"
    return "both" if PICKUP["code"] in codes else "shipping"


async def shipping_of(s: BrowserSession, ad: dict[str, Any]) -> dict[str, Any]:
    """The handover of an own ad and, with Versand, its chosen PayLivery option ({size, carrier}), in the shape
    publish() takes. Raises InsertError when the ad ships but its PayLivery option cannot be read (a copy without
    PayLivery would quietly sell differently)."""
    handover = handover_of(ad)
    if handover == "pickup":
        return {"handover": "pickup"}
    link = _link(ad, "p2ppUserOptions")
    opts = await s.api_json("GET", _webapi(link)) if link else None
    if not isinstance(opts, dict):
        raise InsertError("Versand: die PayLivery-Optionen der Anzeige sind nicht lesbar")
    chosen = {o.get("deliveryOptionUuid") for o in opts.get("selectedDeliveryOptions") or []}
    if not chosen:
        return {"handover": handover, "paylivery": False}
    opt = next((o for o in opts.get("deliveryOptions") or [] if o.get("uuid") in chosen), None)
    if opt is None:
        raise InsertError("Versand: die gewählte PayLivery-Option der Anzeige ist nicht lesbar")
    return {"handover": handover, "paylivery": True, "size": (opt.get("parcelSize") or {}).get("size"),
            "carrier": (opt.get("carrier") or {}).get("description")}


def image_url(img: dict[str, Any]) -> str:
    """The full-size URL of an existing photo."""
    url = img.get("mainImageUrl") or img.get("referenceImageUrl")
    return url or f"https://cache.willhaben.at/mmo/{img.get('reference')}"


async def copy_plan(s: BrowserSession, ad_id: str) -> dict[str, Any]:
    """Everything a copy of an own ad needs, read before anything is created: the ad in the form's template (fresh
    contact, location and context links; heading, text, price, category and attributes from the ad), its photo URLs
    and its shipping. Read-only."""
    import copy

    old = await get_own_ad(s, ad_id)
    if not isinstance(old, dict) or not old.get("heading"):
        raise InsertError(f"Anzeige {ad_id} nicht im Editor gefunden")
    shipping = await shipping_of(s, old)
    photos = [image_url(i) for i in (await get_own_images(s, old))["advertImage"]][:MAX_PHOTOS]
    if not photos:
        raise InsertError("Die Anzeige hat keine Fotos mehr – bitte auf willhaben neu einstellen")
    form = await load_form(s)
    ad = copy.deepcopy(form.get("ad") or {})
    for k in COPY_KEYS:
        if k in old:
            ad[k] = copy.deepcopy(old[k])
    ad.pop("treeAttributeIds", None)
    return {"ad": ad, "photos": photos, "shipping": shipping}
