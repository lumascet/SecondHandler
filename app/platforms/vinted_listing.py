"""Vinted listings: an own ad saved as a draft in the Vinted account, through the calls Vinted's own upload page makes.

Seen 2026-10-28 (a capture of saving a draft on www.vinted.at/items/new, logged in with Chrome):
  GET  /api/v2/item_upload/catalogs           {catalogs: [{id, title, path, catalogs: [...]}]} – the category tree
  POST /api/v2/item_upload/attributes         {attributes: [{code: "category", value: [<catalog id>]}]}
       -> {attributes: [{code, configuration: {title, required, options: [{options: [{id, title}]}]}}]}
       (condition, the department of accessories …; "brand" without options)
  GET  /api/v2/item_upload/brands?category_id=<id>   {brands: [{id, title}]} – the category's usual brands
  GET  api.vinted.at/shipping-estimation/external/catalogs/<id>/package_sizes   {package_sizes: [{id, code, title}]}
  POST /api/v2/item_upload/drafts   {draft: {id: null, currency, temp_uuid, title (5 characters at least),
       description, brand_id, brand, catalog_id, isbn, ai_photo, price (a string), package_size_id, shipment_prices,
       color_ids, assigned_photos, measurement_length, measurement_width, item_attributes, manufacturer,
       manufacturer_labelling}, parcel, upload_session_id}  -> 200; 400 {message_code: "validation_error",
       errors: [{field, value}]}
  GET  /api/v2/wardrobe/<user id>/items?page=1&per_page=20   {items: [{id, title, is_draft, is_closed, is_reserved,
       is_hidden, price.amount, url}]}
Seen 2026-10-28 on a draft and a live item (clicked through in the app's browser):
  PUT  /api/v2/item_upload/drafts/<id>             {draft: {...the draft, id set}, parcel, upload_session_id}
  POST /api/v2/item_upload/drafts/<id>/completion  the same + push_up: false – publishes it (the item keeps the id);
       400 errors: [{field: "color", value: "Wähle ein Farbe …"}] – a colour is required in some categories
  PUT  /api/v2/item_upload/items/<id>              {item: {...the same fields}, push_up: false, parcel, upload_session_id}
  PUT  /api/v2/items/<id>/is_hidden                {is_hidden: true|false}
  POST /api/v2/item_upload/suggestions/attributes  {title, description, category_id, …} -> {prefill_brand {id, title},
       colors [{id, title}], …} – what the page prefills; brand_id 1 is "no brand"
  POST api.vinted.at/shipping-estimation/external/package_sizes/suggestion {catalog_id, title, …} -> {package_size_id}
  The edit page (/items/<id>/edit) carries the item in its page data: {id, title, description, colorIds, catalogId,
  packageSizeId, brand {id, title}, brandId, price, itemAttributes [{ids, code}], isDraft, …} and its photos [{id}].
  Reserving needs another member to reserve for; marking sold and deleting were not tried.
They need Vinted's cookie, CSRF token and anon id, which only its own web app has: a tab of the app's browser opens
the upload page and makes the calls from inside it, with the token the page carries. Only a draft is saved – the
owner looks at it on Vinted and publishes it there. Not seen yet: the photo upload's answer (the page's own upload
of the files is used, its answer is logged), the values of item_attributes (the shape of the page's suggestions call
is used: [{code, ids}]; Vinted refusing them saves the draft without), editing, hiding and deleting an item.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

log = logging.getLogger(__name__)

ORIGIN = "https://www.vinted.at"
NEW_URL = f"{ORIGIN}/items/new"
API = f"{ORIGIN}/api/v2"
PHOTO_TIMEOUT_MS = 45000
MAX_PHOTOS = 20
# Vinted's sizes as seen for a category (ids 1–3); the category's own list wins when it can be read
PACKAGE_LIMITS = ((0.5, "SMALL"), (1.0, "MEDIUM"), (2.0, "LARGE"))

# the call from inside Vinted's page: its CSRF token and anon id ride along as its own web app sends them
_JS_CALL = r"""async ([method, url, body]) => {
  const html = document.documentElement.innerHTML;
  const tok = html.match(/\\?"CSRF_TOKEN\\?"\s*:\s*\\?"([^"\\]+)/);
  const anon = (document.cookie.match(/(?:^|; )anon_id=([^;]+)/) || [])[1]
            || (html.match(/\\?"anon_id\\?"\s*:\s*\\?"([0-9a-f-]{20,})/) || [])[1];
  const headers = {'accept': 'application/json, text/plain, */*', 'locale': document.documentElement.lang || 'de-AT'};
  if (tok) headers['x-csrf-token'] = tok[1];
  if (anon) headers['x-anon-id'] = decodeURIComponent(anon);
  if (body !== null) headers['content-type'] = 'application/json';
  const r = await fetch(url, {method, headers, credentials: 'include',
                              body: body === null ? undefined : JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""
_JS_USER = r"""() => (document.documentElement.innerHTML.match(/\\?"userId\\?"\s*:\s*\\?"?(\d+)/) || [])[1] || null"""


class ListingError(RuntimeError):
    pass


# ------------------------------------------------------------------ pure helpers (tested without a browser)
def leaves(tree: list[dict[str, Any]], path: tuple[str, ...] = ()) -> list[tuple[int, str]]:
    """Every category one can list in: (id, "Elektronik > Computer > Laptops")."""
    out: list[tuple[int, str]] = []
    for node in tree or []:
        p = (*path, str(node.get("title") or ""))
        kids = node.get("catalogs") or []
        if kids:
            out += leaves(kids, p)
        elif node.get("id"):
            out.append((int(node["id"]), " > ".join(p)))
    return out


def groups(tree: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """The first two levels of the tree (id, path): Claude chooses among these first, then among the leaves below –
    the whole tree, thousands of leaves, is too long for one question."""
    out: list[tuple[int, str]] = []
    for top in tree or []:
        kids = top.get("catalogs") or []
        if not kids:
            out.append((int(top["id"]), str(top.get("title") or "")))
        for k in kids:
            out.append((int(k["id"]), f"{top.get('title')} > {k.get('title')}"))
    return out


def subtree(tree: list[dict[str, Any]], node_id: int) -> dict[str, Any] | None:
    for node in tree or []:
        if int(node.get("id") or 0) == node_id:
            return node
        found = subtree(node.get("catalogs") or [], node_id)
        if found is not None:
            return found
    return None


def path_of(tree: list[dict[str, Any]], node_id: int) -> str | None:
    return next((p for i, p in leaves(tree) if i == node_id), None)


def attribute_choices(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The attributes of a category one picks from a list ({code, title, required, options: [(id, title, text)]});
    brand and free-text ones are left out."""
    out = []
    for a in (raw or {}).get("attributes") or []:
        conf = a.get("configuration") or {}
        opts = [(int(o["id"]), str(o.get("title") or ""), str(o.get("description") or ""))
                for g in conf.get("options") or [] for o in (g.get("options") or [g]) if o.get("id") is not None
                and o.get("type") != "group"]
        if a.get("code") in (None, "brand", "category") or not opts:
            continue
        out.append({"code": a["code"], "title": conf.get("title") or a["code"], "required": bool(conf.get("required")),
                    "multi": conf.get("selection_type") == "multiple", "options": opts})
    return out


def clean_attributes(chosen: Any, choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude's picks held to the offered options: [{code, ids}] in the shape of the page's own calls."""
    allowed = {c["code"]: ({o[0] for o in c["options"]}, c["multi"]) for c in choices}
    out = []
    for item in chosen or []:
        code = (item or {}).get("code")
        if code not in allowed:
            continue
        ok, multi = allowed[code]
        ids = [int(i) for i in (item.get("ids") or []) if str(i).lstrip("-").isdigit() and int(i) in ok]
        if ids:
            out.append({"code": code, "ids": ids if multi else ids[:1]})
    return out


def match_brand(brands: list[dict[str, Any]], *texts: str) -> dict[str, Any] | None:
    """A brand of the category's list named in the title (whole word, the longest name first: "Apple TV" before "Apple")."""
    hay = " ".join(t or "" for t in texts).lower()
    for b in sorted(brands or [], key=lambda b: -len(str(b.get("title") or ""))):
        name = str(b.get("title") or "").strip().lower()
        if len(name) >= 2 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", hay):
            return b
    return None


def package_size(sizes: list[dict[str, Any]] | None, weight_kg: float | None) -> int | None:
    """The smallest of Vinted's sizes the parcel fits by weight; the largest standard one above 2 kg; None without a
    weight (Vinted then asks on publishing)."""
    if not weight_kg:
        return None
    std = [s for s in sizes or [] if s.get("standard", True) and s.get("id")]
    by_code = {s.get("code"): int(s["id"]) for s in std}
    for limit, code in PACKAGE_LIMITS:
        if weight_kg <= limit and code in by_code:
            return by_code[code]
    return int(std[-1]["id"]) if std else None


def draft_body(*, title: str, description: str, price: float | None, catalog_id: int, session_id: str,
               brand: dict[str, Any] | None = None, package_size_id: int | None = None,
               photo_ids: list[int] | None = None, attributes: list[dict[str, Any]] | None = None,
               color_ids: list[int] | None = None) -> dict[str, Any]:
    """The draft as Vinted's upload page sends it (seen 2026-10-28)."""
    return {"draft": {
        "id": None, "currency": "EUR", "temp_uuid": session_id, "title": title.strip()[:100],
        "description": description.strip()[:2000], "brand_id": int(brand["id"]) if brand else None,
        "brand": str(brand["title"]) if brand else None, "catalog_id": int(catalog_id), "isbn": None, "ai_photo": False,
        "price": price_text(price), "package_size_id": package_size_id,
        "shipment_prices": {"domestic": None, "international": None}, "color_ids": list(color_ids or [])[:2],
        "assigned_photos": [{"id": int(i), "orientation": 0} for i in photo_ids or []],
        "measurement_length": None, "measurement_width": None, "item_attributes": attributes or [],
        "manufacturer": None, "manufacturer_labelling": None},
        "parcel": None, "upload_session_id": session_id}


def errors_of(text: str | None) -> str:
    """Vinted's validation messages as one line ("title: Bitte nutze … mindestens 5 Zeichen")."""
    try:
        d = json.loads(text or "")
    except ValueError:
        return (text or "").strip()[:200]
    errs = [f"{e.get('field')}: {e.get('value')}" for e in d.get("errors") or [] if isinstance(e, dict)]
    return "; ".join(errs) or str(d.get("message") or d.get("message_code") or "")[:200]


def photo_id(body: Any) -> int | None:
    """The id of an uploaded photo in the upload's answer ({id} or {photo: {id}})."""
    if not isinstance(body, dict):
        return None
    for v in (body.get("id"), (body.get("photo") or {}).get("id") if isinstance(body.get("photo"), dict) else None):
        if v is not None and str(v).isdigit():
            return int(v)
    return None


def upload_name(body: bytes | None) -> str | None:
    """The file name in a photo upload's multipart body (photo[file]; filename="foto2.jpg")."""
    m = re.search(rb'filename="([^"]+)"', body or b"")
    return m.group(1).decode("utf-8", "replace") if m else None


def ordered_photo_ids(files: list[str], got: dict[str, int]) -> list[int]:
    """The uploaded photos' ids in the order of the files; raises when one is missing (a draft without its cover or
    with a photo short is not what the ad shows)."""
    from pathlib import PurePath
    ids = [got.get(PurePath(f).name) for f in files]
    if None in ids:
        raise ListingError(f"{sum(i is not None for i in ids)} of {len(files)} photos uploaded to Vinted")
    return [int(i) for i in ids]


def price_text(price: float | None) -> str | None:
    return f"{price:.2f}".rstrip("0").rstrip(".") if price else None


_NEXT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def next_payload(html: str) -> str:
    """The page data of Vinted's Next.js page, read from its HTML (self.__next_f.push([1, "…"]) chunks). From the
    HTML, not the page's variable: the app's browser runs its scripts in a world of their own, where the page's
    variables are not visible (10-28: the item was "not found")."""
    out = []
    for chunk in _NEXT_CHUNK.findall(html or ""):
        try:
            out.append(json.loads(f'"{chunk}"'))
        except ValueError:
            continue
    return "".join(out)


def item_from_payload(payload: str, item_id: int | str) -> dict[str, Any] | None:
    """The item and its photo ids out of the edit page's data ({…, "photos": [{id}…]})."""
    dec = json.JSONDecoder()
    i = payload.find('{"id":%s,"title":' % int(item_id))
    if i < 0:
        return None
    try:
        item, end = dec.raw_decode(payload, i)
    except ValueError:
        return None
    j = payload.find('"photos":[', end)
    photos: list[int] = []
    if 0 <= j < end + 400:
        try:
            raw, _ = dec.raw_decode(payload, j + len('"photos":'))
            raw = [ph for ph in raw if isinstance(ph, dict) and ph.get("id")]
            photos = [int(ph["id"]) for ph in raw]
            urls = [str(ph.get("fullSizeUrl") or ph.get("full_size_url") or ph.get("url") or "") for ph in raw]
        except ValueError:
            urls = []
    else:
        urls = []
    return {**item, "photo_ids": photos, "photo_urls": [u for u in urls if u]}


def edit_body(item: dict[str, Any], session_id: str, *, title: str | None = None, description: str | None = None,
              price: float | None = None) -> dict[str, Any]:
    """The whole item as the edit page saves it, with title, text and price changed where given (a partial body is
    not what the page sends). Draft or item, by the item's isDraft."""
    brand = item.get("brand") or {}
    body = {
        "id": int(item["id"]), "currency": item.get("currency") or "EUR", "temp_uuid": session_id,
        "title": (title if title is not None else item.get("title") or "").strip()[:100],
        "description": (description if description is not None else item.get("description") or "").strip()[:2000],
        "brand_id": item.get("brandId"), "brand": (brand.get("title") or None), "catalog_id": item.get("catalogId"),
        "isbn": item.get("isbn"), "ai_photo": bool(item.get("isAiPhoto")),
        "price": price_text(price if price is not None else float(item.get("price") or 0)),
        "package_size_id": item.get("packageSizeId"),
        "shipment_prices": {"domestic": item.get("domesticShipmentPrice"),
                            "international": item.get("internationalShipmentPrice")},
        "color_ids": list(item.get("colorIds") or []),
        "assigned_photos": [{"id": i, "orientation": 0} for i in item.get("photo_ids") or []],
        "measurement_length": item.get("measurementLength"), "measurement_width": item.get("measurementWidth"),
        "item_attributes": [{"code": a.get("code"), "ids": list(a.get("ids") or [])}
                            for a in item.get("itemAttributes") or [] if a.get("code")],
        "manufacturer": item.get("manufacturer"), "manufacturer_labelling": item.get("manufacturerLabel")}
    key = "draft" if item.get("isDraft") else "item"
    return {key: body, "push_up": False, "parcel": None, "upload_session_id": session_id}


def find_draft(items: list[dict[str, Any]], title: str, known: set[int]) -> dict[str, Any] | None:
    """The draft just saved in the wardrobe list: a draft with this title that was not there before."""
    want = title.strip()[:100].lower()
    return next((i for i in items or [] if i.get("is_draft") and int(i.get("id") or 0) not in known
                 and str(i.get("title") or "").strip().lower() == want), None)


def item_state(item: dict[str, Any]) -> str:
    """draft | sold | reserved | hidden | active, from a wardrobe entry."""
    if item.get("is_draft"):
        return "draft"
    if item.get("is_closed"):
        return "sold"
    if item.get("is_reserved"):
        return "reserved"
    if item.get("is_hidden"):
        return "hidden"
    return "active"


# ------------------------------------------------------------------ the browser
class Upload:
    """One open upload page of Vinted in a tab of the app's browser (async with)."""

    def __init__(self, url: str = NEW_URL) -> None:
        self.url = url
        self.page = None
        self.session_id: str | None = None
        self.user_id: str | None = None

    async def __aenter__(self) -> Upload:
        from app.willhaben.session import get_session

        sess = get_session()
        sess._require()
        ui = sess.ui_page
        sess._creating_internal = True
        try:
            self.page = await sess.context.new_page()
        finally:
            sess._creating_internal = False
            sess.ui_page = ui
        sess.working_pages.add(self.page)

        def seen(request) -> None:       # the page's own upload session, for the photos and the draft alike
            if self.session_id:
                return
            try:
                text = f"{request.url} {request.post_data or ''}"
            except Exception:  # noqa: BLE001 - a binary body (the photos)
                text = request.url
            if m := re.search(r"upload_session_id[=\"':\s]+([0-9a-f-]{36})", text):
                self.session_id = m.group(1)
        self.page.on("request", seen)
        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
        if "/member/" in self.page.url or "signup" in self.page.url or "login" in self.page.url:
            from app.services import site_accounts
            site_accounts._save("vinted", False)
            await self.close()
            raise ListingError("not logged in to Vinted")
        self.user_id = await self.page.evaluate(_JS_USER)
        if self.user_id:                 # the upload page opened logged in: that is the login check too
            from app.services import site_accounts
            if site_accounts.state("vinted")["logged_in"] is not True:
                site_accounts._save("vinted", True)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.page is not None:
            from app.willhaben.session import get_session
            get_session().working_pages.discard(self.page)
            try:
                await self.page.close()
            except Exception:  # noqa: BLE001 - already gone
                pass
            self.page = None

    async def call(self, method: str, url: str, body: Any = None) -> tuple[int, Any, str]:
        res = await self.page.evaluate(_JS_CALL, [method, url, body])
        text = res.get("text") or ""
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        return int(res.get("status") or 0), data, text

    async def get(self, url: str) -> Any:
        status, data, _ = await self.call("GET", url)
        if status != 200:
            raise ListingError(f"Vinted answered HTTP {status} for {url.split('?')[0].rsplit('/', 1)[-1]}")
        return data

    async def catalogs(self) -> list[dict[str, Any]]:
        return (await self.get(f"{API}/item_upload/catalogs") or {}).get("catalogs") or []

    async def attributes(self, catalog_id: int) -> dict[str, Any]:
        status, data, _ = await self.call("POST", f"{API}/item_upload/attributes",
                                          {"attributes": [{"code": "category", "value": [catalog_id]}]})
        return data if status == 200 and isinstance(data, dict) else {}

    async def brands(self, catalog_id: int) -> list[dict[str, Any]]:
        try:
            return (await self.get(f"{API}/item_upload/brands?category_id={catalog_id}") or {}).get("brands") or []
        except ListingError:
            return []

    async def package_sizes(self, catalog_id: int) -> list[dict[str, Any]]:
        sid = self.session_id or ""
        try:
            data = await self.get(f"https://api.vinted.at/shipping-estimation/external/catalogs/{catalog_id}"
                                  f"/package_sizes?upload_session_id={sid}")
        except Exception as e:  # noqa: BLE001 - the fixed sizes then
            log.info("vinted: package sizes not read (%s)", e)
            return []
        return (data or {}).get("package_sizes") or []

    async def photos(self, files: list[str]) -> list[int]:
        """Upload the photos through the page's own file field and return their ids in the order of `files` (the
        first is the cover). The page uploads them side by side and the answers come in any order (10-28: the cover
        came second), so each answer is matched to its file by the file name its request carried."""
        files = files[:MAX_PHOTOS]
        if not files:
            return []
        got: dict[str, int] = {}
        failed: list[int] = []

        def is_upload(r) -> bool:
            return r.request.method == "POST" and "/api/v2/photos" in r.url

        async def on_response(r) -> None:
            if not is_upload(r):
                return
            name = upload_name(r.request.post_data_buffer)
            try:
                body = json.loads(await r.text())
            except Exception:  # noqa: BLE001 - no JSON
                body = None
            if (pid := photo_id(body)) is not None and name:
                got[name] = pid
            else:
                failed.append(r.status)
        self.page.on("response", on_response)
        field = self.page.locator("input[type=file]").first
        await field.wait_for(state="attached", timeout=PHOTO_TIMEOUT_MS)
        await field.set_input_files(files)
        for _ in range(PHOTO_TIMEOUT_MS // 500):
            if len(got) + len(failed) >= len(files):
                break
            await self.page.wait_for_timeout(500)
        return ordered_photo_ids(files, got)

    async def wardrobe(self) -> list[dict[str, Any]]:
        if not self.user_id:
            raise ListingError("Vinted user not found on the page")
        data = await self.get(f"{API}/wardrobe/{self.user_id}/items?page=1&per_page=96&order=newest_first")
        return (data or {}).get("items") or []

    async def wardrobe_all(self, max_pages: int = 10) -> tuple[list[dict[str, Any]], bool]:
        """Every item of the own wardrobe, page by page; complete=False when there were more pages than read."""
        if not self.user_id:
            raise ListingError("Vinted user not found on the page")
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = await self.get(f"{API}/wardrobe/{self.user_id}/items?page={page}&per_page=96&order=newest_first") or {}
            out += data.get("items") or []
            if page >= int((data.get("pagination") or {}).get("total_pages") or 1):
                return out, True
        return out, False

    async def save_draft(self, body: dict[str, Any]) -> tuple[int, Any, str]:
        return await self.call("POST", f"{API}/item_upload/drafts", body)

    async def suggestions(self, catalog_id: int, title: str, description: str) -> dict[str, Any]:
        """What the page prefills for this text: prefill_brand, colors (best first)."""
        status, data, _ = await self.call("POST", f"{API}/item_upload/suggestions/attributes", {
            "title": title, "description": description, "category_id": catalog_id,
            "suggested_category_id": catalog_id, "image_metadata": [], "upload_session_id": self.session_id or "",
            "attributes": [{"code": "category", "ids": [str(catalog_id)]}], "suggested_legacy_category_id": catalog_id})
        return data if status == 200 and isinstance(data, dict) else {}

    async def suggested_package(self, catalog_id: int, title: str, description: str) -> int | None:
        try:
            status, data, _ = await self.call(
                "POST", "https://api.vinted.at/shipping-estimation/external/package_sizes/suggestion",
                {"catalog_id": catalog_id, "upload_session_id": self.session_id or "", "title": title,
                 "description": description})
        except Exception:  # noqa: BLE001 - the parcel size is then asked on publishing
            return None
        pid = (data or {}).get("package_size_id") if status == 200 and isinstance(data, dict) else None
        return int(pid) if pid else None

    async def open_edit(self, item_id: int | str) -> dict[str, Any]:
        """Go to the item's edit page and return the item from its page data (with photo_ids)."""
        await self.page.goto(f"{ORIGIN}/items/{int(item_id)}/edit", wait_until="domcontentloaded", timeout=30000)
        await self.page.wait_for_timeout(1500)
        item = item_from_payload(next_payload(await self.page.content()), item_id)
        if item is None:
            raise ListingError(f"Vinted item {item_id} not found on its edit page")
        return item

    async def save_item(self, item_id: int | str, body: dict[str, Any]) -> None:
        kind = "drafts" if "draft" in body else "items"
        status, _, text = await self.call("PUT", f"{API}/item_upload/{kind}/{int(item_id)}", body)
        if status not in (200, 201):
            raise ListingError(f"Vinted did not save the item: {errors_of(text) or status}")

    async def set_hidden(self, item_id: int | str, hidden: bool) -> None:
        status, _, text = await self.call("PUT", f"{API}/items/{int(item_id)}/is_hidden", {"is_hidden": hidden})
        if status not in (200, 201):
            raise ListingError(f"Vinted did not {'hide' if hidden else 'show'} the item: {errors_of(text) or status}")

    async def publish(self, draft_id: int | str, body: dict[str, Any]) -> None:
        status, _, text = await self.call("POST", f"{API}/item_upload/drafts/{int(draft_id)}/completion", body)
        if status not in (200, 201):
            raise ListingError(errors_of(text) or f"HTTP {status}")


def new_session_id() -> str:
    return str(uuid.uuid4())
