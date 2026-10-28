"""Ad drafting: notes + photos -> Claude writes the listing; price hint from comparable ads."""
from __future__ import annotations

import io
import json
import logging
import re
import statistics
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.db import AdDraft, MyAd, get_setting, session, set_setting, utcnow
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import AD_SCHEMA, AD_SYSTEM, ad_prompt, ad_refine_prompt
from app.services import geizhals, min_price, selling
from app.willhaben.public import WillhabenError, get_public
from app.services import timeline
from app.services.shipping import clean_parcel

log = logging.getLogger(__name__)

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 15 * 1024 * 1024
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
INSERT_URL = "https://www.willhaben.at/iad/anzeigenaufgabe"


def nice_price(v: float) -> float:
    step = 1 if v < 20 else 5 if v < 100 else 10 if v < 1000 else 50
    return max(step, round(v / step) * step)


def price_stats(prices: list[float]) -> dict[str, Any] | None:
    prices = sorted(p for p in prices if p and p > 0)
    if len(prices) < 3:
        return None
    q = statistics.quantiles(prices, n=4)
    return {"count": len(prices), "median": statistics.median(prices), "p25": q[0], "p75": q[2],
            "min": prices[0], "max": prices[-1]}


MAX_QUANTITY = 999


def parse_quantity(value: Any) -> int | None:
    """Pieces from a form field or from Claude: a whole number 1..999; blank, text and zero are None."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return min(n, MAX_QUANTITY) if n >= 1 else None


def pick_quantity(owner: int | None, claude: Any) -> int | None:
    """What the draft offers: the owner's count wins; Claude's only fills in when the owner gave none and it saw more
    than one piece. One piece is no stock (None = a single item, as on My ads)."""
    n = owner if owner is not None else parse_quantity(claude)
    return n if n and n > 1 else None


def claude_parcel(raw: Any, title: str | None) -> dict[str, Any] | None:
    """Claude's packed-size estimate (for one piece) as a shipping parcel; source "claude" keeps it an estimate
    that the owner has to confirm before anything is bought. All zeros = could not estimate = None."""
    if not isinstance(raw, dict):
        return None
    return clean_parcel({**raw, "contents": (title or "")[:60], "source": "claude"})


def save_images(draft_id: int, files: list[tuple[str, bytes]]) -> list[str]:
    folder = get_settings().uploads_dir / str(draft_id)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, (name, data) in enumerate(files[:MAX_IMAGES], 1):
        ext = Path(name or "").suffix.lower()
        if ext not in ALLOWED or not data or len(data) > MAX_IMAGE_BYTES:
            continue
        p = folder / f"{i:02d}{ext}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


@timeline.scoped(batch_id="batch_id")
async def create_draft(notes: str, files: list[tuple[str, bytes]], batch_id: int | None = None,
                       quantity: int | None = None) -> AdDraft:
    """quantity: the owner's piece count (the batch review); it wins over what Claude reads from notes and photos,
    an explicit 1 too (one item: the draft gets no count, the prompt says it is a single piece)."""
    owner_quantity = parse_quantity(quantity)
    with session() as db:
        draft = AdDraft(input_text=notes, batch_id=batch_id, quantity=pick_quantity(owner_quantity, None))
        db.add(draft)
        db.commit()
        db.refresh(draft)
    timeline.update_context(draft_id=draft.id)
    return await _write_draft(draft.id, notes, save_images(draft.id, files), owner_quantity)


def enqueue_create(notes: str, files: list[tuple[str, bytes]], quantity: int | None = None) -> AdDraft:
    """"Create listing with Claude" without waiting: the draft row and its photos exist at once (the page shows
    "Claude is writing the draft…" and swaps itself in when done), Claude writes title, description, price and
    the rest as a background job (lane claude, up to two minutes; the same pipeline as create_draft). Taken back
    while queued, the draft is left as an error with "Try again"."""
    from app.services import jobs

    owner_quantity = parse_quantity(quantity)
    with session() as db:
        draft = AdDraft(input_text=notes, quantity=pick_quantity(owner_quantity, None), refining=True)
        db.add(draft)
        db.commit()
        db.refresh(draft)
    draft_id = draft.id
    paths = save_images(draft_id, files)

    async def run(job) -> str:
        try:
            d = await _write_draft(draft_id, notes, paths, owner_quantity)
        finally:
            update_draft(draft_id, refining=None)
        if d.error:
            raise RuntimeError(d.error)
        return t("Written: {title}", title=(d.title or "")[:60])

    def taken_back() -> None:
        update_draft(draft_id, refining=None, status="error", error=t("Taken back"))

    jobs.submit("refine", t("Claude writes draft #{id}", id=draft_id), run, lane="claude", current=f"refining:{draft_id}",
                on_cancel=taken_back, link=f"/new-ad/{draft_id}")
    return update_draft(draft_id, image_paths=paths)


@timeline.scoped(draft_id="draft_id")
async def retry_draft(draft_id: int) -> AdDraft:
    """A draft Claude could not write (status "error"): the whole pipeline once more from the same notes and photos."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    timeline.update_context(batch_id=d.batch_id)
    return await _write_draft(draft_id, d.input_text, d.image_paths, d.quantity)


async def _write_draft(draft_id: int, notes: str, paths: list[str], owner_quantity: int | None = None) -> AdDraft:
    """Claude writes the listing, then comparable ads and Geizhals. When Claude fails the draft keeps status "error"
    and the message (retry_draft tries again); the photos are kept either way. owner_quantity: the owner's piece
    count, which wins over Claude's (a retry uses the count stored at creation)."""
    rules = selling.append_to(get_setting("reply_rules"), for_ad=True)
    try:
        result = await get_backend("ads").complete(
            system=AD_SYSTEM, prompt=ad_prompt(notes, rules, len(paths), quantity=owner_quantity),
            schema=AD_SCHEMA, images=[Path(p) for p in paths])
    except LLMError as e:
        return update_draft(draft_id, image_paths=paths, status="error", error=str(e))

    data = result.data
    stats, comparables = None, []
    keywords = (data.get("search_keywords") or "").strip()
    if keywords:
        try:
            page = await get_public().search_keyword(keywords, rows=50, sort=7)
            stats = price_stats([a.price for a in page.ads if a.price])
            if stats:
                stats["rows_found"] = page.rows_found
            comparables = [a.as_dict() for a in page.ads if a.price][:12]
        except WillhabenError as e:
            log.info("comparable search failed: %s", e)
    suggestion = nice_price(stats["median"]) if stats else data.get("price_estimate")
    product_query = (data.get("product_query") or "").strip()
    gh = await _geizhals_lookup(product_query)

    with session() as db:
        d = db.get(AdDraft, draft_id)
        d.image_paths, d.error = paths, None
        if d.status == "error":          # a retry succeeded (a card discarded meanwhile stays discarded)
            d.status = "draft"
        d.title, d.description = data.get("title"), clean_text(data.get("description"))
        d.questions = questions_of(data)
        d.category_hint, d.condition = data.get("category_hint"), data.get("condition")
        d.attributes = data.get("attributes") or []
        d.quantity = pick_quantity(owner_quantity, data.get("quantity"))
        d.parcel = claude_parcel(data.get("parcel"), d.title)
        d.search_keywords = keywords
        d.price_suggestion = suggestion
        d.price_stats = {**(stats or {}), "llm_estimate": data.get("price_estimate"), "note": data.get("note")}
        d.comparables = comparables
        d.model = result.model
        d.product_query, d.geizhals = product_query or None, gh
        db.add(d)
        db.commit()
        db.refresh(d)
    if (gh or {}).get("verified") and ((gh or {}).get("match") or {}).get("specs"):
        # only a confirmed product/variant: a guessed one (e.g. the 8GB model for a 4GB ad) would rewrite the text
        d = await reconcile_with_geizhals(d.id)
        d = attach_specs_if_wanted(d.id)
    return d


_CHECK_MARK = re.compile(r"\s*[\(\[](?:bitte\s+)?(?:prüfen|überprüfen|checken|nachprüfen)[\)\]]", re.I)


def clean_text(text: str | None) -> str | None:
    """A "(bitte prüfen)" Claude wrote anyway is dropped: open points are questions on the draft page."""
    return _CHECK_MARK.sub("", text) if text else text


def questions_of(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    from app.services.product_search import normalize_questions

    return normalize_questions(data.get("questions")) or None


def answers_instruction(questions: list[dict[str, Any]], answers: dict[str, str]) -> str | None:
    """The owner's answers to the draft's questions as the revision instruction; None when none was answered."""
    pairs = [(q["text"], (answers.get(q["id"]) or "").strip()[:200]) for q in questions or []]
    pairs = [(q, a) for q, a in pairs if a]
    if not pairs:
        return None
    lines = "\n".join(f"- {q} → {a}" for q, a in pairs)
    return ("Antworten der Person auf deine offenen Fragen. Arbeite sie als normale Angaben in Titel und Beschreibung "
            "ein, wo sie Käufern helfen (nicht als Frage und Antwort zitieren):\n" + lines)


def auto_specs() -> bool:
    """Setting: always attach the Geizhals spec sheet to the description when a product was found."""
    return get_setting("geizhals_auto_specs") == "1"


def attach_specs_if_wanted(draft_id: int, force: bool = False) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    block = geizhals.spec_block((d.geizhals or {}).get("match"))
    if not block or not (force or auto_specs()) or block in (d.description or ""):
        return d
    return update_draft(draft_id, description=f"{(d.description or '').rstrip()}\n\n{block}")


def update_draft(draft_id: int, **fields: Any) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
        for k, v in fields.items():
            setattr(d, k, v)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


_CURRENCY = re.compile(r"€|\bEUR\b|\bEuros?\b|,-$|\.-$", re.I)


def parse_price(text: str | None) -> float | None:
    """The price field as people type it: "12", "12,50", "12.50", "1.200" (thousands), "1.200,50", "12 €", "80 EUR",
    "1 200". Blank -> None (no price). Raises ValueError for anything else (then the old price stays)."""
    s = re.sub(r"\s+", "", _CURRENCY.sub("", text or ""))
    if not s:
        return None
    if not re.fullmatch(r"\d+(?:[.'’]\d{3})*(?:[.,]\d{1,2})?", s):
        raise ValueError(text)
    return min_price.to_float(s)


REFINING_BUSY = "Claude is still revising this draft – save or publish again when it is done"
PUBLISHED_READONLY = "Already published – edit the ad under My ads; here it stays as it was sent"
PRICE_UNREADABLE = "Price not recognized: “{price}” – the old price stays"


def save_fields(draft_id: int, title: str, description: str, price: str | None,
                quantity: str | None = None) -> AdDraft:
    """The user's edits of title, description, price and piece count (form values; quantity None = the form did not
    send it, blank = one item). Refused while Claude revises the draft: its result would overwrite the edit (the
    revision started from the text before it), and once the ad is published (the text lives on willhaben then).
    A price that cannot be read keeps the old one; d.error says so."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is not None and d.refining:
        return update_draft(draft_id, error=t(REFINING_BUSY))
    if d is not None and d.status == "published":
        return update_draft(draft_id, error=t(PUBLISHED_READONLY))
    fields: dict[str, Any] = {"title": title.strip(), "description": description.replace("\r\n", "\n").strip()}
    if quantity is not None:
        fields["quantity"] = pick_quantity(parse_quantity(quantity), None)
    try:
        fields["price_suggestion"] = parse_price(price)
    except ValueError:
        fields["error"] = t(PRICE_UNREADABLE, price=(price or "").strip())
    return update_draft(draft_id, **fields)


def autosave(draft_id: int, form: Any) -> str | None:
    """The draft page's autosave (X-Autosave on POST /new-ad/<id>, the whole form as you type). Only the fields the
    form sent, and only changed ones; price and pieces also while Claude revises (the revision does
    not touch them), title and description not (its result would overwrite them). Returns the problem, else None;
    nothing goes to d.error."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None:
        return None
    if d.status == "published":
        return t(PUBLISHED_READONLY)
    fields: dict[str, Any] = {}
    problem = None
    if form.get("price") is not None:
        try:
            fields["price_suggestion"] = parse_price(str(form["price"]))
        except ValueError:
            problem = t(PRICE_UNREADABLE, price=str(form["price"]).strip())
    if form.get("quantity") is not None:
        fields["quantity"] = pick_quantity(parse_quantity(str(form["quantity"])), None)
    for key, name in (("title", "title"), ("description", "description")):
        if form.get(key) is None:
            continue
        value = str(form[key]).replace("\r\n", "\n").strip()
        if value == (getattr(d, name) or "").strip():
            continue                    # unchanged (the page sends the whole form): nothing to refuse while revising
        if d.refining:
            problem = problem or t(REFINING_BUSY)
            continue
        fields[name] = value
    if fields:
        update_draft(draft_id, **fields)
    return problem


def save_problem(d: AdDraft, price: str | None) -> str | None:
    """After save_fields: why the edit was not (fully) taken over, or None when everything was saved."""
    if d.refining or d.status == "published":
        return d.error
    try:
        parse_price(price)
    except ValueError:
        return d.error
    return None


def photos_zip(d: AdDraft) -> bytes:
    """All photos of a draft, named after the title, for uploading them on willhaben by hand."""
    stem = re.sub(r"[^\w-]+", "-", (d.title or f"draft-{d.id}").lower()).strip("-")[:40] or f"draft-{d.id}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # photos are compressed already
        for i, path in enumerate(d.image_paths, 1):
            if Path(path).exists():
                z.write(path, f"{stem}-{i:02d}{Path(path).suffix}")
    return buf.getvalue()


async def _geizhals_lookup(query: str) -> dict[str, Any] | None:
    if not query:
        return None
    try:
        return await geizhals.lookup(query)
    except geizhals.GeizhalsError as e:
        log.info("geizhals lookup failed for %r: %s", query, e)
        return {"query": query, "match": None, "candidates": [], "error": str(e)}


async def choose_geizhals(draft_id: int, url: str) -> AdDraft:
    """User picked another Geizhals hit (or pasted a product link)."""
    prod = await geizhals.product(url)
    with session() as db:
        d = db.get(AdDraft, draft_id)
        gh = dict(d.geizhals or {"query": d.product_query, "candidates": []})
        old_block = geizhals.spec_block(gh.get("match"))
        had_block = bool(old_block) and old_block in (d.description or "")
        if had_block:                      # the sheet of the previous product goes; the new one replaces it
            d.description = (d.description or "").replace(old_block, "").rstrip()
        gh["match"], gh["verified"] = asdict(prod), True      # the user's own pick counts as confirmed
        gh.pop("error", None)
        d.geizhals = gh
        db.add(d)
        db.commit()
        db.refresh(d)
    return attach_specs_if_wanted(draft_id, force=had_block)


async def search_geizhals(draft_id: int, query: str) -> AdDraft:
    """User typed a product name: confirmed (`verified`) only if a hit matches it, variant included."""
    gh = await _geizhals_lookup(query)
    return update_draft(draft_id, product_query=query.strip() or None, geizhals=gh)


_VERSION_FIELDS = ("title", "description", "category_hint", "condition", "attributes")
_UNDO_EXTRA = ("product_query", "geizhals", "price_suggestion")   # in a revision only when it changed them

RECONCILE_INSTRUCTION = (
    "Gleiche das Inserat mit den Herstellerdaten von Geizhals ab: Korrigiere Angaben, die dem Datenblatt "
    "widersprechen, und übernimm, was das Datenblatt bestätigt. Was es nicht klärt (z. B. Akku, Zustand) und du "
    "nicht sicher weißt, lässt du im Text weg und fragst es unter questions. "
    "Entferne eine selbst geschriebene Liste technischer Daten aus der Beschreibung, das Datenblatt hänge ich an."
)


async def reconcile_with_geizhals(draft_id: int) -> AdDraft:
    return await refine_draft(draft_id, RECONCILE_INSTRUCTION, label=t("Checked against the Geizhals spec sheet"))


@timeline.scoped(draft_id="draft_id")
async def refine_draft(draft_id: int, instruction: str, label: str | None = None) -> AdDraft:
    """Let Claude revise the draft; the previous version goes to `revisions` for undo."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    match = (d.geizhals or {}).get("match") or {}
    block = geizhals.spec_block(match)
    had_block = bool(block) and block in (d.description or "")
    current = {k: getattr(d, k) for k in _VERSION_FIELDS}
    if had_block:   # Claude rewrites the prose only; the spec sheet is re-attached unchanged
        current["description"] = (d.description or "").replace(block, "").rstrip()
    # an unconfirmed variant's sheet is no "reliable" data for a plain revision; the explicit check still uses it
    trusted = (d.geizhals or {}).get("verified") is not False or instruction == RECONCILE_INSTRUCTION
    prompt = ad_refine_prompt(notes=d.input_text, rules=selling.append_to(get_setting("reply_rules"), for_ad=True),
                              n_images=len(d.image_paths),
                              current=current, specs=match.get("specs") if trusted else None,
                              product_name=match.get("name"), instruction=instruction, quantity=d.quantity,
                              product_query=d.product_query or "", price=d.price_suggestion)
    try:
        result = await get_backend("ads").complete(system=AD_SYSTEM, prompt=prompt, schema=AD_SCHEMA,
                                                   images=[Path(p) for p in d.image_paths])
    except LLMError as e:
        return update_draft(draft_id, error=t("Revision failed: {error}", error=e))
    data = result.data
    extra: dict[str, Any] = {}
    # another product (the owner said what it really is): Geizhals looks it up again, the old spec sheet goes
    query = (data.get("product_query") or "").strip()
    if instruction != RECONCILE_INSTRUCTION and query and _norm_query(query) != _norm_query(d.product_query):
        gh = await _geizhals_lookup(query)
        extra.update(product_query=query, geizhals=gh)
        block = geizhals.spec_block((gh or {}).get("match")) if (gh or {}).get("verified") else ""
        had_block = had_block and bool(block)
    # a price the owner named in the instruction (Claude's estimate alone never changes it)
    price = named_price(instruction, data.get("price_estimate"))
    if price is not None and price != d.price_suggestion:
        extra["price_suggestion"] = price
    description = (clean_text(data.get("description")) or "").rstrip()
    if had_block:
        description = f"{description}\n\n{block}"
    revisions = list(d.revisions or [])
    revisions.append({**{k: getattr(d, k) for k in _VERSION_FIELDS}, "instruction": label or instruction,
                      "at": utcnow().isoformat(), **{k: getattr(d, k) for k in _UNDO_EXTRA if k in extra}})
    d = update_draft(draft_id, title=data.get("title"), description=description,
                     category_hint=data.get("category_hint"), condition=data.get("condition"),
                     attributes=data.get("attributes") or [], revisions=revisions[-20:], model=result.model,
                     error=None, price_stats={**(d.price_stats or {}), "note": data.get("note")},
                     questions=questions_of(data), **extra)
    return attach_specs_if_wanted(draft_id) if "geizhals" in extra else d


def _norm_query(q: str | None) -> str:
    return re.sub(r"[^0-9a-z]+", "", (q or "").lower())


_NUMBER = re.compile(r"\d+(?:[.'’]\d{3})*(?:[.,]\d{1,2})?")


def named_price(instruction: str, price: Any) -> float | None:
    """Claude's price_estimate when the instruction names that very amount ("Preis 80 €", "verkaufe um 1.200"),
    else None."""
    try:
        want = round(float(price), 2)
    except (TypeError, ValueError):
        return None
    for m in _NUMBER.finditer(instruction or ""):
        try:
            if parse_price(m.group(0)) == want:
                return want
        except ValueError:
            continue
    return None


def undo_revision(draft_id: int) -> AdDraft:
    with session() as db:
        d = db.get(AdDraft, draft_id)
    revisions = list(d.revisions or [])
    if not revisions:
        return d
    last = revisions.pop()
    return update_draft(draft_id, revisions=revisions, **{k: last.get(k) for k in _VERSION_FIELDS},
                        **{k: last[k] for k in _UNDO_EXTRA if k in last})


# ------------------------------------------------------------------ publish on willhaben (direct, per click)
def category_queries(title: str, product_query: str | None, search_keywords: str | None,
                     user_query: str | None = None) -> list[str]:
    """What to ask willhaben's category suggestion, best first. Long titles often get no suggestion at all,
    and the short search keywords alone can point somewhere odd (live: "Keyboards" for a speaker)."""
    words = [w.strip(",;:·-") for w in (title or "").split()]
    cands = [user_query, title, " ".join(words[:4]), " ".join(words[:3]), product_query, search_keywords]
    out: dict[str, str] = {}
    for q in cands:
        if q and q.strip():
            out.setdefault(q.strip()[:80].lower(), q.strip()[:80])    # same query in other case: ask once
    return list(out.values())


async def prepare_willhaben(draft_id: int, query: str | None = None) -> AdDraft:
    """Read-only: category suggestions and required fields from willhaben's own form, stored for review."""
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    sess = get_session()
    form = await insert.cached_form(sess)
    raw, used = None, None
    for q in category_queries(d.title or d.input_text, d.product_query, d.search_keywords, query):
        raw, used = await insert.suggest_categories(sess, q), q
        if insert.suggestion_paths(raw):
            break
    plan = insert.build_plan(form, raw, condition=d.condition, attributes=d.attributes)
    plan["for_title"], plan["query"] = d.title, used
    if not plan["options"]:          # willhaben suggests nothing (live: a wallbox): the category Claude named, if it exists
        hinted = insert.hint_option(form, d.category_hint, condition=d.condition, attributes=d.attributes)
        if hinted:
            plan["options"] = [hinted]
    old = d.wh_plan or {}
    have = {o["path"][-1]["code"] for o in plan["options"] if o.get("path")}
    plan["options"] += [o for o in old.get("options") or [] if o.get("manual") and o["path"][-1]["code"] not in have]
    if old.get("shipping"):
        plan["shipping"] = old["shipping"]
    _keep_choices(plan, old)
    return update_draft(draft_id, wh_plan=plan, error=None)


def _keep_choices(plan: dict[str, Any], old: dict[str, Any]) -> None:
    """Asking willhaben again keeps what the owner chose: the category (by its code, wherever it lands now) and the
    required fields' values of options that are still there."""
    old_opts = old.get("options") or []
    chosen = {option_code(o): {f["code"]: f.get("selected") for f in o.get("fields") or []} for o in old_opts}
    for o in plan["options"]:
        for f in o["fields"]:
            v = (chosen.get(option_code(o)) or {}).get(f["code"])
            if v is not None and v in {c for c, _ in f["values"]}:
                f["selected"] = v
    sel = old.get("selected")
    if isinstance(sel, int) and 0 <= sel < len(old_opts):
        idx = next((i for i, o in enumerate(plan["options"]) if option_code(o) == option_code(old_opts[sel])), None)
        if idx is not None:
            plan["selected"] = idx


async def add_manual_category(draft_id: int, code: str) -> AdDraft:
    """A category the user picked in "Choose another category": stored as option in wh_plan and pre-selected,
    so publish_willhaben can use it like a suggested one."""
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    form = await insert.cached_form(get_session())
    opt = insert.manual_option(form, code, condition=d.condition, attributes=d.attributes)
    if opt is None:
        raise ValueError(t("This category cannot be chosen (not found or not a final category)"))
    plan = dict(d.wh_plan or {})
    if "location" not in plan:                   # picked before (or without) "Prepare for willhaben"
        base = insert.build_plan(form, None, condition=d.condition, attributes=d.attributes)
        plan.update(location=base["location"], contact=base["contact"], for_title=d.title)
    options = list(plan.get("options") or [])
    idx = next((i for i, o in enumerate(options) if o.get("path") and o["path"][-1]["code"] == code), None)
    if idx is None:
        options.append(opt)
        idx = len(options) - 1
    plan["options"], plan["selected"] = options, idx
    return update_draft(draft_id, wh_plan=plan, error=None)


def option_code(o: dict[str, Any]) -> str:
    """The willhaben category code of a wh_plan option (its leaf)."""
    return str(o["path"][-1]["code"]) if o.get("path") else ""


def save_wh_choice(draft_id: int, code: str | None, values: dict[str, dict[str, str]],
                   shipping: dict[str, Any] | None) -> AdDraft:
    """The batch table's willhaben choice, kept without publishing: the category option (by its code), the required
    fields per option (category code -> field code -> value code; unknown values are ignored) and the handover.
    Publishing takes them from wh_plan later (_publish_inputs falls back to each field's "selected")."""
    import copy

    with session() as db:
        d = db.get(AdDraft, draft_id)
    plan = copy.deepcopy(d.wh_plan or {})
    options = plan.get("options") or []
    for o in options:
        posted = values.get(option_code(o)) or {}
        for f in o["fields"]:
            allowed = {str(c): c for c, _ in f["values"]}
            if posted.get(f["code"]) in allowed:
                f["selected"] = allowed[posted[f["code"]]]
    idx = next((i for i, o in enumerate(options) if option_code(o) == code), None)
    if idx is not None:
        plan["selected"] = idx
    if shipping is not None:
        plan["shipping"] = shipping
    return update_draft(draft_id, wh_plan=plan) if plan != (d.wh_plan or {}) else d


def enqueue_prepare(draft_id: int) -> bool:
    """"Prepare for willhaben" as a background job (lane ads, read-only on willhaben): categories and required fields
    land in wh_plan, the batch row swaps itself in when done. False: not a draft, busy, or already queued."""
    from app.services import jobs
    from app.willhaben.session import NotLoggedIn, SessionUnavailable

    key = f"preparing:{draft_id}"
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None or d.status != "draft" or d.publishing or d.refining or key in jobs.current_keys("preparing:"):
        return False

    async def run(job) -> str:
        try:
            d = await prepare_willhaben(draft_id)
        except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
            update_draft(draft_id, error=t("willhaben form not reachable: {error}", error=str(e)))
            raise
        return t("{n} categories suggested", n=len((d.wh_plan or {}).get("options") or []))

    jobs.submit("prepare", t("Prepare „{title}“ for willhaben", title=(d.title or f"#{draft_id}")[:60]), run,
                current=key)
    return True


def parse_shipping(form: Any) -> dict[str, Any]:
    """Übergabe fields of the publish form (parcel_option "S|Hermes", or parcel_size + parcel_carrier) →
    {"handover", "paylivery", "size", "carrier"}; without a valid handover the
    owner's usual one (Settings → Selling preferences, pickup unless changed there).
    Everything but pickup NEEDS LIVE VERIFICATION (insert.set_delivery)."""
    from app.willhaben import insert

    handover = str(form.get("handover") or "")
    if handover not in insert.HANDOVERS:
        handover = selling.handover()
    if handover == "pickup":
        return {"handover": "pickup"}
    size = str(form.get("parcel_size") or "").upper()
    carrier = str(form.get("parcel_carrier") or "")
    if "|" in str(form.get("parcel_option") or ""):     # one offer of the list: "S|Hermes"
        size, carrier = str(form["parcel_option"]).split("|", 1)
        size = size.upper()
    return {"handover": handover, "paylivery": str(form.get("paylivery") or "").lower() in ("1", "true", "on", "yes"),
            "size": size if size in insert.PARCEL_SIZES else None,
            "carrier": carrier if carrier in insert.PAYLIVERY_CARRIERS else None}


def paylivery_offers() -> list[dict[str, Any]]:
    """The PayLivery offers for the handover form: the list willhaben sent at the last publish with PayLivery, else
    the one in insert.OFFERS."""
    from app.willhaben import insert

    try:
        stored = json.loads(get_setting("paylivery_offers") or "null")
    except ValueError:
        stored = None
    return stored if isinstance(stored, list) and stored else insert.OFFERS


def remember_offers(res: dict[str, Any] | None) -> None:
    offers = (((res or {}).get("trace") or {}).get("delivery") or {}).get("offers")
    if offers:
        set_setting("paylivery_offers", json.dumps(offers, ensure_ascii=False))


_publishing: set[int] = set()     # drafts with a publish in flight (double click, second tab)


async def publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None = None,
                            shipping: dict[str, Any] | None = None, job: Any = None, code: str | None = None):
    """Publish the saved draft as free private ad: the chosen category option and field values from the form.
    shipping: parse_shipping() result; None = the owner's usual handover (Settings → Selling preferences; pickup, the
    live-verified path, unless changed there). job: jobs.Job for progress. code: the chosen category's code, which
    wins over the position `option` (a category check meanwhile may have reordered the options)."""
    from app.willhaben import insert

    if draft_id in _publishing:
        raise insert.InsertError("Wird gerade veröffentlicht – bitte warten")
    _publishing.add(draft_id)
    try:
        return await _publish_willhaben(draft_id, option, values, stop_after, shipping, job, code)
    finally:
        _publishing.discard(draft_id)


def enqueue_publish(draft_id: int, option: int, values: dict[str, str], shipping: dict[str, Any] | None = None,
                    code: str | None = None) -> bool:
    """Check the form right away (errors land on the draft at once), then publish as a background job.

    Returns False when nothing was queued (input error, already queued or published); the reason is in d.error.
    """
    from app.services import jobs
    from app.willhaben import insert

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d.publishing:
        return False
    if d.refining:                        # publishing now would upload the text the revision is about to replace
        update_draft(draft_id, error=t(REFINING_BUSY))
        return False
    if code is None:                      # the category chosen now, by its code (not its place in the list)
        opts = (d.wh_plan or {}).get("options") or []
        code = option_code(opts[option]) if 0 <= option < len(opts) else None
    try:
        _publish_inputs(d, option, values, shipping, code=code)
    except insert.InsertError as e:
        update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
        return False
    d = update_draft(draft_id, publishing=True, error=None)
    n_photos = len(d.image_paths[:insert.MAX_PHOTOS])

    async def run(job) -> str:
        try:
            await publish_willhaben(draft_id, option, values, shipping=shipping, job=job, code=code)
        except Exception as e:
            log.warning("publishing draft %s failed: %s", draft_id, e)
            update_draft(draft_id, error=t("Publishing failed: {error}", error=str(e)))
            raise
        finally:
            update_draft(draft_id, publishing=None)
        return t("Published on willhaben")

    # live on willhaben at once: it waits the grace period in the queue (cancel instead of a confirm)
    jobs.submit("publish", t("Publish „{title}“", title=(d.title or f"#{draft_id}")[:60]), run,
                total=n_photos + 5, current=f"addraft:{draft_id}", grace=True, quiet=True,
                on_cancel=lambda: update_draft(draft_id, publishing=None))
    return True


def enqueue_answers(draft_id: int, answers: dict[str, str]) -> bool:
    """The draft page's questionnaire: the answers go into the text as a background revision; False when none was
    answered (or a revision is already on its way)."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    instruction = answers_instruction((d.questions if d else None) or [], answers)
    if not instruction:
        return False
    if not enqueue_refine(draft_id, instruction, label=t("Your answers")):
        return False
    update_draft(draft_id, questions=None)    # answered; the revision may ask anew
    return True


def enqueue_refine(draft_id: int, instruction: str, label: str | None = None) -> bool:
    """"Revise with Claude" as a background job (lane claude, 10–60 s): the draft shows "Claude is revising…" and
    swaps itself in when done. False: nothing to do or already on its way."""
    from app.services import jobs

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None or d.refining or d.publishing or d.status == "published" or not instruction.strip():
        return False                      # never rewrite a draft that is on its way to willhaben or already there
    update_draft(draft_id, refining=True, error=None)

    async def run(job) -> str:
        try:
            d = await refine_draft(draft_id, instruction, label=label)
        finally:
            update_draft(draft_id, refining=None)
        if d.error:                       # refine_draft keeps Claude's errors on the draft
            raise RuntimeError(d.error)
        return t("Revised: {title}", title=(d.title or "")[:60])

    jobs.submit("refine", t("Claude revises „{title}“", title=(d.title or f"#{draft_id}")[:60]), run, lane="claude",
                current=f"refining:{draft_id}")
    return True


def refine_refusal(draft_id: int) -> str:
    """Why enqueue_refine queued nothing, for the page."""
    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d.status == "published":
        return t(PUBLISHED_READONLY)
    if d.refining or d.publishing:
        return t("Not revised: Claude is already revising or publishing this draft")
    return t("Nothing to revise: say what Claude should change")


def enqueue_retry(draft_id: int) -> bool:
    """"Try again" on a draft Claude could not write (status "error"): the same pipeline as a new ad, as a background
    job (lane claude). Meanwhile the draft counts as refining (no edit or publish, the card shows the spinner) and
    swaps itself in when done. False: not such a draft, or already on its way."""
    from app.services import jobs

    with session() as db:
        d = db.get(AdDraft, draft_id)
    if d is None or d.status != "error" or d.refining or d.publishing:
        return False
    update_draft(draft_id, refining=True, error=None)

    async def run(job) -> str:
        try:
            d = await retry_draft(draft_id)
        finally:
            update_draft(draft_id, refining=None)
        if d.error:
            raise RuntimeError(d.error)
        return t("Written: {title}", title=(d.title or "")[:60])

    jobs.submit("refine", t("Claude writes draft #{id} again", id=draft_id), run, lane="claude",
                current=f"refining:{draft_id}")
    return True


def recover_publishing() -> None:
    """After a restart the queue is gone. The willhaben draft id is remembered, so publishing again continues it."""
    with session() as db:
        for d in db.exec(select(AdDraft).where(AdDraft.publishing == True)):  # noqa: E712
            d.publishing, d.error = None, t("Publishing interrupted by a restart – check on willhaben, then try again")
            db.add(d)
        for d in db.exec(select(AdDraft).where(AdDraft.refining == True)):  # noqa: E712
            d.refining = None             # the revision never happened: the draft is as it was
            db.add(d)
        db.commit()


GONE = ("expired", "sold", "deleted", "replaced")     # MyAd statuses of an ad that is no longer online


def live_on_willhaben(d: AdDraft) -> MyAd | None:
    """The willhaben ad this draft became, as long as My ads has not seen it go offline (a fresh "unknown" counts as
    online: the app itself just published it). None: never published, or no longer online."""
    if not d.wh_ad_id:
        return None
    with session() as db:
        row = db.get(MyAd, d.wh_ad_id)
    return row if row is not None and row.status not in GONE else None


def _publish_inputs(d: AdDraft, option: int, values: dict[str, str], shipping: dict[str, Any] | None,
                    code: str | None = None):
    """Everything that can be checked without willhaben: (chosen option, attrs, labels, shipping) or InsertError.
    code: the chosen category; its option is looked up by it (the list may have changed since the click)."""
    from app.willhaben import insert

    if d.status == "published":
        raise insert.InsertError("Schon veröffentlicht")
    live = live_on_willhaben(d)
    if live is not None:           # the status was reset by hand, but the ad is online: publishing again would
        raise insert.InsertError(  # re-save and re-activate the live ad (or make a public duplicate)
            f"Ist auf willhaben schon online als {live.ad_id} ({live.status}) – unter „Meine Anzeigen“ (/myads) "
            f"ändern oder löschen statt noch einmal veröffentlichen")
    plan = d.wh_plan or {}
    opts = plan.get("options") or []
    if code:
        found = next((i for i, o in enumerate(opts) if option_code(o) == code), None)
        if found is None:
            raise insert.InsertError("Die gewählte Kategorie ist nicht mehr in der Liste – bitte neu wählen")
        option = found
    if not (0 <= option < len(opts)):
        raise insert.InsertError("Bitte zuerst eine Kategorie wählen")
    if not (d.title or "").strip() or not (d.description or "").strip():
        raise insert.InsertError("Titel und Beschreibung dürfen nicht leer sein")
    if hits := selling.contact_hits(f"{d.title}\n{d.description}"):
        raise insert.InsertError(f"willhaben zensiert E-Mail-Adressen und Links – bitte „{hits[0]}“ aus Titel und "
                                 "Beschreibung entfernen")
    chosen = opts[option]
    attrs, labels = {}, {}
    for f in chosen["fields"]:
        code = values.get(f["code"]) or f.get("selected")
        allowed = dict(f["values"])
        if code not in allowed:
            raise insert.InsertError(f"Pflichtfeld „{f['label']}“ fehlt")
        attrs[f["code"]], labels[f["code"]] = (code, allowed[code]), f["label"]

    shipping = shipping if shipping and shipping.get("handover") in insert.HANDOVERS else parse_shipping({})
    if shipping.get("paylivery") and not shipping.get("size"):
        raise insert.InsertError("Bitte eine Paketgröße für PayLivery wählen")
    if shipping != plan.get("shipping") or plan.get("selected") != option:   # remembered for the form
        update_draft(d.id, wh_plan={**plan, "shipping": shipping, "selected": option})

    if d.wh_ad_id and plan.get("delivery_set") and shipping["handover"] == "pickup":
        # A former attempt already set shipping/PayLivery options on this willhaben draft; saving it with pickup
        # would not clear them (a reset call is unverified), so the user changes the handover on willhaben.
        raise insert.InsertError("Versand wurde bei willhaben schon eingerichtet – Übergabe bitte direkt auf "
                                 "willhaben ändern oder wieder Versand wählen")
    return chosen, attrs, labels, shipping


async def _publish_willhaben(draft_id: int, option: int, values: dict[str, str], stop_after: str | None,
                             shipping: dict[str, Any] | None, job: Any = None, code: str | None = None):
    from app.willhaben import insert
    from app.willhaben.session import get_session

    with session() as db:
        d = db.get(AdDraft, draft_id)
    chosen, attrs, labels, shipping = _publish_inputs(d, option, values, shipping, code=code)
    with session() as db:
        d = db.get(AdDraft, draft_id)             # wh_plan may just have been updated
    done = [0]

    def on_step(step: str, i: int = 0, n: int = 0) -> None:
        if job is None:
            return
        done[0] += 1
        text = {"form": t("Loading the willhaben form"), "presave": t("Creating the willhaben draft"),
                "photo": t("Uploading photo {i}/{n}", i=i, n=n), "save": t("Saving the ad"),
                "delivery": t("Setting up shipping"), "activate": t("Activating the free ad")}[step]
        job.progress(min(done[0], job.total), text, f"addraft:{draft_id}")

    on_step("form")
    sess = get_session()
    form = await insert.load_form(sess)          # fresh template (contact, location, context links)
    insert.remember_form(form)
    ad = insert.compose_ad(form.get("ad") or {}, heading=d.title, description=d.description,
                           price=d.price_suggestion, path=chosen["path"], attrs=attrs, attr_labels=labels,
                           handover=shipping["handover"])
    photos = []
    for p in d.image_paths[:insert.MAX_PHOTOS]:
        path = Path(p)
        mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/jpeg")
        photos.append((path.name, path.read_bytes(), mime))

    def remember(wh_id: str) -> None:            # so a retry continues the same willhaben draft
        update_draft(draft_id, wh_ad_id=wh_id)

    def delivery_set() -> None:                  # shipping options now live on the willhaben draft (see guard above)
        with session() as db:
            cur = dict(db.get(AdDraft, draft_id).wh_plan or {})
        update_draft(draft_id, wh_plan={**cur, "delivery_set": True})

    ship = shipping if shipping["handover"] != "pickup" else None
    try:
        res = await insert.publish(sess, ad, photos, existing_ad_id=d.wh_ad_id, on_presaved=remember,
                                   stop_after=stop_after, shipping=ship, on_delivery=delivery_set, on_step=on_step)
    except RuntimeError as e:
        # 409 on the reused willhaben draft: it was deleted (or finished) meanwhile -> start one fresh insertion
        if not d.wh_ad_id or "HTTP 409" not in str(e):
            raise
        # ...unless it still exists: then it may already be live, and a new insertion would be a public duplicate
        from app.willhaben.account import AccountClient
        old = next((a for a in await AccountClient(sess).my_adverts() if a.ad_id == str(d.wh_ad_id)), None)
        if old is not None:
            if old.status == "active":
                update_draft(draft_id, status="published", error=None)
            raise insert.InsertError(f"Existiert auf willhaben schon als {old.ad_id} "
                                     f"({old.status_label or old.status}) – nicht noch einmal angelegt") from e
        log.info("willhaben draft %s of ad draft %s is gone (%s); publishing as a new insertion", d.wh_ad_id, draft_id, e)
        update_draft(draft_id, wh_ad_id=None)
        res = await insert.publish(sess, ad, photos, existing_ad_id=None, on_presaved=remember, stop_after=stop_after,
                                   shipping=ship, on_delivery=delivery_set, on_step=on_step)
    if res.get("stopped"):                       # step-by-step trial: nothing public yet, draft stays a draft
        return res
    remember_offers(res)
    d = update_draft(draft_id, status="published", wh_ad_id=res["ad_id"], error=None)
    from app.services import my_ads
    parcel_estimate = clean_parcel(d.parcel)       # source "claude": the shipping card says "estimated"
    with session() as db:                        # show up under "my ads" right away, status unknown yet
        row = db.get(MyAd, res["ad_id"])
        if row is None:
            row = MyAd(ad_id=res["ad_id"], heading=d.title, url=res["url"], status="unknown", image_url=None,
                       published_at=utcnow(), parcel=parcel_estimate)
        elif row.parcel is None:                 # the account import was faster than we were: fill the gaps only
            row.parcel = parcel_estimate
        if row.quantity is None:                 # the draft's piece count becomes the ad's stock (app/services/stock.py)
            row.quantity = d.quantity
        db.add(row)
        db.commit()
    if d.vinted_ad:                              # copied from an ad only on Vinted: now one ad on both sites
        from app.services import crosspost
        crosspost.link_willhaben(d.vinted_ad, res["ad_id"])
    try:                                         # willhaben's own status (new ads are reviewed before going online)
        await my_ads.import_from_account()
    except Exception as e:  # noqa: BLE001 - the ad is published either way
        log.info("my-ads refresh after publishing failed: %s", e)
    log.info("draft %s published on willhaben as %s", draft_id, res["ad_id"])
    return d
