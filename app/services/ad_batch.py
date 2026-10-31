"""Batch ad creation: a dump of photos of many articles -> Claude sorts them into products -> user corrects
-> one AdDraft per group through the normal pipeline (ad_creator.create_draft)."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Coroutine

from sqlmodel import select

from app.config import get_settings
from app.db import AdBatch, AdDraft, session
from app.i18n import t
from app.llm.base import LLMError, get_backend
from app.llm.prompts import SORT_SCHEMA, SORT_SYSTEM, sort_prompt
from app.services import ad_creator
from app.services import timeline

log = logging.getLogger(__name__)

MAX_BATCH_IMAGES = 60
CREATE_PARALLEL = 2          # drafts written at the same time (each is 1-2 Claude calls + a willhaben search)
_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, batch_id: int | None = None, label: str | None = None) -> None:
    """Run a slow job in the background; the page polls the batch status. With a label (and the batch) it also shows
    among the background jobs, with a notice when it is done. A used-up hourly willhaben budget (the drafts' price
    searches) makes it wait instead of giving up."""
    from app.platforms import ratelimit

    if label and batch_id is not None:
        coro = _tracked(coro, batch_id, label)
    with ratelimit.waiting():           # the task takes the setting along (_tracked shows the wait in the header)
        task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _tracked(coro: Coroutine[Any, Any, Any], batch_id: int, label: str) -> None:
    from app.platforms import ratelimit
    from app.services import jobs

    async with jobs.track("ad-batch", label, link=f"/new-ad/batch/{batch_id}") as job:
        before = {g["id"] for g in (get(batch_id) or AdBatch()).groups if g.get("draft_id")}
        with ratelimit.waiting(jobs.budget_wait(job)):
            await coro
        b = get(batch_id)
        if b is None:
            return
        if b.status == "error":
            job.status, job.result = "failed", b.error or ""
            return
        made = [g for g in b.groups if g.get("draft_id") and g["id"] not in before]
        job.problems = sum(1 for g in b.groups if g.get("error") and g["id"] not in before)
        job.result = (t("{n} drafts written", n=len(made)) if made or job.problems
                      else t("{n} articles found – check the groups", n=len(b.groups)))


def get(batch_id: int) -> AdBatch | None:
    with session() as db:
        return db.get(AdBatch, batch_id)


def _update(batch_id: int, **fields: Any) -> AdBatch:
    with session() as db:
        b = db.get(AdBatch, batch_id)
        for k, v in fields.items():
            setattr(b, k, v)
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


def _folder(batch_id: int) -> Path:
    return get_settings().uploads_dir / f"batch-{batch_id}"


def new_batch(notes: str) -> AdBatch:
    with session() as db:
        b = AdBatch(notes=notes.strip())
        db.add(b)
        db.commit()
        db.refresh(b)
    return b


def add_image(batch_id: int, name: str, data: bytes) -> bool:
    """Store one uploaded photo (called per file so a big upload never sits in memory at once)."""
    b = get(batch_id)
    ext = Path(name or "").suffix.lower()
    if len(b.image_paths) >= MAX_BATCH_IMAGES or ext not in ad_creator.ALLOWED or not data \
            or len(data) > ad_creator.MAX_IMAGE_BYTES:
        return False
    folder = _folder(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{len(b.image_paths) + 1:02d}{ext}"
    p.write_bytes(data)
    _update(batch_id, image_paths=[*b.image_paths, str(p)])
    return True


def locked(g: dict[str, Any]) -> bool:
    """A group that already became a draft is no longer changed by sorting or review."""
    return bool(g.get("draft_id"))


def unassigned(b: AdBatch) -> list[int]:
    used = {i for g in b.groups for i in g["photos"]}
    return [i for i in range(len(b.image_paths)) if i not in used]


def _next_gid(groups: list[dict[str, Any]]) -> str:
    n = max((int(g["id"][1:]) for g in groups if g["id"][1:].isdigit()), default=0)
    return f"g{n + 1}"


def _new_group(gid: str, photos: list[int], **fields: Any) -> dict[str, Any]:
    return {"id": gid, "photos": photos, "product": "", "product_query": "", "observations": "", "quantity": None,
            "confidence": "", "question": "", "comment": "", "include": True, "draft_id": None, "error": None,
            **fields}


def groups_from_result(data: dict[str, Any], pool: list[int], keep: list[dict[str, Any]],
                       previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Claude's 1-based photo numbers (over `pool`) -> groups of image indices; each photo at most once.
    The user's comment, include flag and piece count move to the new group sharing the most photos with the old one."""
    groups, seen, carried = list(keep), set(), set()
    previous = [g for g in previous or [] if not locked(g)]
    for raw in data.get("groups") or []:
        photos = []
        for n in raw.get("photos") or []:
            if isinstance(n, int) and 1 <= n <= len(pool) and pool[n - 1] not in seen:
                seen.add(pool[n - 1])
                photos.append(pool[n - 1])
        if not photos:
            continue
        old = max((g for g in previous if g["id"] not in carried), default=None,
                  key=lambda g: len(set(g["photos"]) & set(photos)))
        user = {"quantity": ad_creator.pick_quantity(None, raw.get("quantity"))}    # Claude's; one piece = nothing to say
        if old and set(old["photos"]) & set(photos):
            carried.add(old["id"])
            user |= {"comment": old.get("comment") or "", "include": old.get("include", True)}
            if old.get("quantity"):         # the count the owner reviewed is theirs, as the comment is
                user["quantity"] = old["quantity"]
        groups.append(_new_group(
            _next_gid(groups), photos, product=(raw.get("product") or "").strip(),
            product_query=(raw.get("product_query") or "").strip(),
            observations=(raw.get("observations") or "").strip(), confidence=raw.get("confidence") or "",
            question=(raw.get("question") or "").strip(), **user))
    return groups


@timeline.scoped(batch_id="batch_id")
async def sort(batch_id: int, hint: str | None = None) -> AdBatch:
    """Let Claude (re-)group the photos. Groups that are drafts already stay as they are."""
    b = get(batch_id)
    keep = [g for g in b.groups if locked(g)]
    taken = {i for g in keep for i in g["photos"]}
    pool = [i for i in range(len(b.image_paths)) if i not in taken]
    if not pool:
        return _update(batch_id, status="review", error=None)
    pos = {img: n for n, img in enumerate(pool, 1)}
    current = [{"photos": [pos[i] for i in g["photos"]], "product": g.get("product"), "comment": g.get("comment"),
                "quantity": g.get("quantity")}
               for g in b.groups if not locked(g) and g["photos"]]
    _update(batch_id, status="sorting", error=None)
    backend = get_backend("ads")
    if hasattr(backend, "timeout_s"):   # Claude looks at every photo; give it time
        backend.timeout_s = max(backend.timeout_s, 120 + 10 * len(pool))
    try:
        result = await backend.complete(
            system=SORT_SYSTEM, prompt=sort_prompt(notes=b.notes, n_images=len(pool), current=current, hint=hint),
            schema=SORT_SCHEMA, images=[Path(b.image_paths[i]) for i in pool])
    except LLMError as e:
        return _update(batch_id, status="error", error=t("Sorting failed: {error}", error=e))
    except Exception as e:  # noqa: BLE001  (background task: never leave the batch stuck in "sorting")
        log.exception("batch %s: sorting crashed", batch_id)
        return _update(batch_id, status="error", error=t("Sorting failed: {error}", error=e))
    groups = groups_from_result(result.data, pool, keep, b.groups)
    return _update(batch_id, groups=groups, status="review", note=(result.data.get("note") or "").strip() or None,
                   model=result.model)


def apply_review(batch_id: int, order: list[tuple[int, str]], fields: dict[str, dict[str, Any]]) -> AdBatch:
    """Save the user's corrections.

    order:  (image index, target) in page order; target is a group id, "new" (all such photos form one new group)
            or "" (not used). Photo order inside a group follows page order, so the first one is the cover.
    fields: group id -> {product, comment, include, quantity}; quantity only when the form sent it. Blank = one item:
            a count the owner clears is kept as 1, so Claude's count (re-sort, draft) does not come back
    """
    b = get(batch_id)
    groups = {g["id"]: dict(g, photos=list(g["photos"]) if locked(g) else []) for g in b.groups}
    taken = {i for g in groups.values() if locked(g) for i in g["photos"]}
    new_gid = None
    for idx, target in order:
        if idx in taken or not 0 <= idx < len(b.image_paths):
            continue
        if target == "new":
            if new_gid is None:
                new_gid = _next_gid(list(groups.values()))
                groups[new_gid] = _new_group(new_gid, [])
            target = new_gid
        g = groups.get(target)
        if g is None or locked(g):
            continue
        g["photos"].append(idx)
        taken.add(idx)
    for gid, f in fields.items():
        g = groups.get(gid)
        if g is None or locked(g):
            continue
        g["product"] = (f.get("product") or "").strip()
        g["comment"] = (f.get("comment") or "").replace("\r\n", "\n").strip()
        g["include"] = bool(f.get("include"))
        if "quantity" in f:
            q = ad_creator.parse_quantity(f["quantity"])
            g["quantity"] = q if q is not None else (1 if g.get("quantity") else None)
    kept = [g for g in groups.values() if g["photos"] or locked(g)]
    return _update(batch_id, groups=kept)


def group_notes(b: AdBatch, g: dict[str, Any]) -> str:
    """Input text for the normal draft pipeline: the user's words first, Claude's sorting result as hints."""
    parts = []
    if g.get("comment"):
        parts.append(g["comment"])
    if g.get("product"):
        parts.append(f"Artikel: {g['product']}")
    if g.get("observations"):
        parts.append(f"Auf den Fotos erkannt (automatisch, bitte prüfen): {g['observations']}")
    if b.notes:
        parts.append(f"Gilt für alle meine Artikel: {b.notes}")
    return "\n".join(parts)


def pending(b: AdBatch) -> list[dict[str, Any]]:
    return [g for g in b.groups if g.get("include") and not locked(g) and g["photos"]]


async def create_drafts(batch_id: int) -> AdBatch:
    """One AdDraft per included group, through the same pipeline as a single new ad."""
    b = _update(batch_id, status="creating", error=None)
    sem = asyncio.Semaphore(CREATE_PARALLEL)

    async def one(g: dict[str, Any]) -> None:
        async with sem:
            files = [(Path(b.image_paths[i]).name, Path(b.image_paths[i]).read_bytes())
                     for i in g["photos"][:ad_creator.MAX_IMAGES]]
            try:
                d = await ad_creator.create_draft(group_notes(b, g), files, batch_id=batch_id, quantity=g.get("quantity"))
                _set_group(batch_id, g["id"], draft_id=d.id, error=d.error)
                if d.status == "draft" and not d.error and _willhaben_ready():
                    ad_creator.enqueue_prepare(d.id)    # categories ready in the table without a click (read-only)
            except Exception as e:  # noqa: BLE001
                log.exception("batch %s group %s: draft failed", batch_id, g["id"])
                _set_group(batch_id, g["id"], error=str(e))

    await asyncio.gather(*(one(g) for g in pending(b)))
    return _update(batch_id, status="review")


def _willhaben_ready() -> bool:
    from app.willhaben.session import get_session

    return bool(get_session().logged_in)


def _set_group(batch_id: int, gid: str, **fields: Any) -> None:
    b = get(batch_id)
    _update(batch_id, groups=[dict(g, **fields) if g["id"] == gid else g for g in b.groups])


def recover() -> None:
    """After a restart no background job is running any more."""
    with session() as db:
        for b in db.exec(select(AdBatch).where(AdBatch.status.in_(["sorting", "creating"]))):
            if b.status == "sorting":
                b.status, b.error = "error", t("Sorting was interrupted by a restart.")
            else:
                b.status = "review"
            db.add(b)
        db.commit()


def delete(batch_id: int) -> None:
    """Drafts keep their own copies of the photos."""
    with session() as db:
        b = db.get(AdBatch, batch_id)
        if b:
            db.delete(b)
            db.commit()
    shutil.rmtree(_folder(batch_id), ignore_errors=True)


# ------------------------------------------------------------------ the drafts table (review & publish)
def drafts_of(b: AdBatch) -> list[AdDraft]:
    """The batch's drafts in group order."""
    ids = [g["draft_id"] for g in b.groups if g.get("draft_id")]
    if not ids:
        return []
    with session() as db:
        found = {d.id: d for d in db.exec(select(AdDraft).where(AdDraft.id.in_(ids)))}
    return [found[i] for i in ids if i in found]


def editable(d: AdDraft) -> bool:
    """A row whose fields the table saves: a written draft that is not on its way to willhaben or being revised."""
    return d.status == "draft" and not d.publishing and not d.refining


def selectable(d: AdDraft) -> bool:
    return d.status in ("draft", "error", "discarded") and not d.publishing


def table_rows(form: Any) -> dict[int, dict[str, str]]:
    """The table's inputs are named "<draft id>.<field>" -> {draft id: {field: value}}."""
    rows: dict[int, dict[str, str]] = {}
    for key, value in form.multi_items():
        ref, dot, rest = key.partition(".")
        if dot and ref.isdigit():
            rows.setdefault(int(ref), {})[rest] = str(value)
    return rows


async def save_table(batch_id: int, rows: dict[int, dict[str, str]]) -> tuple[list[int], set[int]]:
    """Save every editable row of the table (unticked ones too: nothing typed gets lost). Returns the drafts whose row
    has to be drawn anew (a category copied from another row that this draft did not have yet: it becomes a
    hand-picked option, fields included) and the drafts whose edit was not taken over (e.g. an unreadable price)."""
    from app.willhaben.session import NotLoggedIn, SessionUnavailable

    redraw, unsaved = [], set()
    for d in drafts_of(get(batch_id)):
        f = rows.get(d.id)
        if f is None or not editable(d):
            continue
        if "title" in f and "description" in f:
            d = ad_creator.save_fields(d.id, f["title"], f["description"], f.get("price", ""), f.get("quantity"))
            if ad_creator.save_problem(d, f.get("price", "")):
                unsaved.add(d.id)
        values: dict[str, dict[str, str]] = {}
        for k, v in f.items():
            kind, _, rest = k.partition(":")
            cat, _, field = rest.partition(":")
            if kind == "f" and cat and field and v:
                values.setdefault(cat, {})[field] = v
        shipping = ad_creator.parse_shipping({k: f.get(k) for k in ("handover", "paylivery", "parcel_size",
                                                                    "parcel_carrier")}) if f.get("handover") else None
        code = f.get("cat") or None
        if code and code not in {ad_creator.option_code(o) for o in (d.wh_plan or {}).get("options") or []}:
            try:
                await ad_creator.add_manual_category(d.id, code)
                redraw.append(d.id)
            except ValueError as e:
                ad_creator.update_draft(d.id, error=str(e))
                redraw.append(d.id)
                unsaved.add(d.id)          # not published in the category it had before
            except (NotLoggedIn, SessionUnavailable, RuntimeError) as e:
                ad_creator.update_draft(d.id, error=t("willhaben form not reachable: {error}", error=str(e)))
                redraw.append(d.id)
                unsaved.add(d.id)
        ad_creator.save_wh_choice(d.id, code, values, shipping)
    return redraw, unsaved


def _publish_chosen(d: AdDraft) -> bool:
    """Publish a draft with the category and the fields chosen in its row (the first suggestion if none was)."""
    plan = d.wh_plan or {}
    sel = plan.get("selected") or 0
    code = ad_creator.option_code(plan["options"][sel]) if 0 <= sel < len(plan["options"]) else None
    return ad_creator.enqueue_publish(d.id, sel, {}, plan.get("shipping"), code=code)


def run_action(batch_id: int, verb: str, ids: list[int], instruction: str = "",
               unsaved: set[int] = frozenset()) -> tuple[str, str]:
    """One button of the table for all ticked drafts (or the one row it belongs to): (message, tone). unsaved: drafts
    whose edit save_table could not take over; they are not published with the old values (the row says why)."""
    drafts = {d.id: d for d in drafts_of(get(batch_id))}
    picked = [drafts[i] for i in ids if i in drafts]
    if not picked:
        return t("No ad ticked"), "warn"
    if verb == "publish":
        n = asking = 0
        for d in picked:
            if d.status != "draft" or d.id in unsaved:
                continue
            if (d.wh_plan or {}).get("options"):
                n += _publish_chosen(d)
            elif ad_creator.enqueue_prepare(d.id, then=_publish_chosen):   # no category yet: ask willhaben first
                ad_creator.update_draft(d.id, error=None)
                asking += 1
            elif d.refining:
                ad_creator.update_draft(d.id, error=t(ad_creator.REFINING_BUSY))
            else:                       # its categories are being asked for already
                ad_creator.update_draft(d.id, error=t("willhaben categories are still being fetched – publish once "
                                                      "they are there"))
        rest = len(picked) - n - asking
        if not n and not asking:
            return t("Nothing published – see the notes in the rows"), "warn"
        parts = ([t("{n} ads are being published one after another", n=n)] if n else []) \
            + ([t("{n} ads: willhaben categories first, then they are published", n=asking)] if asking else []) \
            + ([t("{n} need attention (see the rows)", n=rest)] if rest else [])
        return " · ".join(parts), "ok" if not rest else "warn"
    if verb == "prepare":
        n = sum(ad_creator.enqueue_prepare(d.id) for d in picked)
        return t("{n} ads are being prepared for willhaben", n=n), "ok"
    if verb == "refine":
        if not instruction.strip():
            return t("Nothing to revise: say what Claude should change"), "warn"
        n = sum(ad_creator.enqueue_refine(d.id, instruction) for d in picked if d.status == "draft")
        return t("Claude revises {n} ads", n=n), "ok"
    if verb == "retry":
        n = sum(ad_creator.enqueue_retry(d.id) for d in picked)
        return t("Claude writes {n} drafts again", n=n), "ok"
    if verb in ("discard", "restore"):
        n = 0
        for d in picked:
            if (verb == "discard" and d.status in ("draft", "error")) or (verb == "restore" and d.status == "discarded"):
                ad_creator.update_draft(d.id, status="discarded" if verb == "discard" else "draft")
                n += 1
        return (t("{n} discarded", n=n) if verb == "discard" else t("{n} restored", n=n)), "ok"
    return t("Changes saved"), "ok"
