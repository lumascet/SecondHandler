"""Own ads: imported from the account, expiry tracked via the public detail page, warnings, reactivation."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import col, select

from app.db import MyAd, current_ad, get_setting, session, utcnow
from app.i18n import t
from app.services import jobs, selling, stock
from app.services.notify import notify
from app.willhaben.account import ACTION_DELETE, AccountClient, AdvertList, EndpointUnknown, has_ended
from app.willhaben.parse import extract_ad_id
from app.willhaben.public import Blocked, WillhabenError, get_public
from app.willhaben.session import NotLoggedIn, SessionUnavailable, get_session
from app.services import timeline

log = logging.getLogger(__name__)

MYADVERTS_URL = "https://www.willhaben.at/iad/myprofile/myadverts"


def is_expired(ad: MyAd, cutoff_days: int | None = None) -> bool:
    """Ran out and can be renewed: not a renewed ad's old copy, and (with a cutoff) ended within the last N days."""
    if ad.status != "expired" or ad.replaced_by or is_sold(ad):
        return False
    if not cutoff_days or ad.end_date is None:
        return True
    end = ad.end_date if ad.end_date.tzinfo else ad.end_date.replace(tzinfo=timezone.utc)   # SQLite: naive UTC
    return end >= utcnow() - timedelta(days=cutoff_days)


def is_sold(ad: MyAd) -> bool:
    """Sold on willhaben, given away ("Verschenkt") or marked sold in the app. An ad with a quantity is marked sold
    only with its last piece (sold.finish), so this stays false while stock is left – also while willhaben shows a
    piece sold with PayLivery as "sold" and the ad waits to go online again (relist)."""
    return (ad.status in ("sold", "Verschenkt") and not ad.relist) or ad.sale_status == "sold"


def add(ref: str, auto_renew: bool = False) -> MyAd:
    ad_id = extract_ad_id(ref)
    if not ad_id:
        raise ValueError(t("Expected an ad ID or willhaben link"))
    with session() as db:
        row = db.get(MyAd, ad_id) or MyAd(ad_id=ad_id)
        row.auto_renew = auto_renew or row.auto_renew
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def remove(ad_id: str) -> None:
    """The ad is gone from willhaben: its row goes too – except the old copy of a renewed ad, which stays (deleted,
    hidden): its buyers' chats still point at it and the renewals are chained through it (stock, minimum price,
    automatic answers, the other buyers' notices)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None:
            return
        if row.replaced_by:
            row.status, row.hidden = "deleted", True
            db.add(row)
        else:
            db.delete(row)
        db.commit()


def _unlink_drafts(ad_id: str) -> None:
    """The willhaben ad is gone: drafts that pointed to it start a fresh insertion when published again."""
    from app.db import AdDraft
    with session() as db:
        for d in db.exec(select(AdDraft).where(AdDraft.wh_ad_id == str(ad_id))):
            d.wh_ad_id = None
            if d.status == "published":
                d.status = "draft"
            db.add(d)
        db.commit()


def set_hidden(ad_id: str, hidden: bool) -> None:
    """Hide from the list only (nothing happens on willhaben); the daily import keeps it hidden."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.hidden = hidden or None
            db.add(row)
            db.commit()


async def deactivate(ad_id: str) -> MyAd | None:
    """Take the ad offline on willhaben (reversible). Auto-renew goes off: offline was intended."""
    await AccountClient(get_session()).deactivate_ad(ad_id)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_renew, row.last_action = False, t("deactivated on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            row.relist = None                 # offline on purpose: not put online again after a PayLivery sale
            db.add(row)
            db.commit()
    await import_from_account()            # status as willhaben shows it now
    with session() as db:
        return db.get(MyAd, ad_id)


async def delete(ad_id: str) -> None:
    """Delete the ad on willhaben for good, confirm it is gone, then drop it here (so auto-renew never sees it)."""
    acct = AccountClient(get_session())
    await acct.delete_ad(ad_id)
    if any(a.ad_id == str(ad_id) for a in await acct.my_adverts()):
        raise EndpointUnknown(t("willhaben still lists the ad after deleting – please check on willhaben"))
    remove(ad_id)
    _unlink_drafts(ad_id)
    log.info("ad %s deleted on willhaben", ad_id)


ACTIONS = ("activate", "deactivate", "delete")


async def bulk(action: str, ad_ids: list[str], job: jobs.Job | None = None) -> dict[str, Any]:
    """Activate, deactivate or delete several ads, one by one with willhaben's per-ad links.

    One list request before and one after (it confirms every delete and refreshes the statuses). Activating a
    passive or expired ad republishes it under a new id (like auto-renew). Errors also land in the ad's
    `last_error`. Returns {"done": [titles], "republished": [titles], "failed": [(title, error)]}.
    """
    if action not in ACTIONS:
        raise ValueError(action)
    acct = AccountClient(get_session())
    ads = await acct.my_adverts()
    titles = {a.ad_id: a.heading or a.ad_id for a in ads}
    done: list[str] = []
    failed: dict[str, str] = {}
    moved: dict[str, str] = {}
    queue = list(dict.fromkeys(ad_ids))
    with session() as db:
        sold_here = {x for x in queue if (r := db.get(MyAd, x)) and is_sold(r)}
    step = {"delete": lambda title: t("Deleting „{title}“", title=title),
            "activate": lambda title: t("Activating „{title}“", title=title),
            "deactivate": lambda title: t("Deactivating „{title}“", title=title)}[action]
    for i, ad_id in enumerate(queue):   # strictly one after another, each request through the shared limiter
        title = titles.get(ad_id, ad_id)
        if job:
            job.progress(i, step(title), f"ad:{ad_id}")
        try:
            if action == "delete":
                await acct.delete_ad(ad_id, ads)
            elif action == "activate":
                if ad_id in sold_here:    # putting a sold ad online again would sell it twice
                    failed[ad_id] = t("sold – not put online again")
                    continue
                if (new_id := await acct.reactivate(ad_id, ads)) != ad_id:
                    moved[ad_id] = new_id
                    _move_to_new_id(ad_id, new_id)    # at once: a later error must not leave it re-activatable
            else:
                await acct.deactivate_ad(ad_id, ads)
            done.append(ad_id)
        except (NotLoggedIn, SessionUnavailable, Blocked) as e:    # logged out / rate limited: stop right here
            failed[ad_id] = str(e)
            failed.update({x: t("not done (stopped after an error)") for x in queue[i + 1:]})
            break
        except (EndpointUnknown, RuntimeError) as e:    # InsertError of a republish is a RuntimeError
            failed[ad_id] = str(e)
    if job:
        job.progress(len(queue), t("Checking the result on willhaben"))
    if action == "delete":
        after = await acct.my_adverts()
        still = {a.ad_id for a in after}
        for ad_id in list(done):
            if ad_id in still:
                done.remove(ad_id)
                failed[ad_id] = t("willhaben still lists the ad after deleting – please check on willhaben")
            else:
                remove(ad_id)
                _unlink_drafts(ad_id)
        await import_from_account(after)    # the list just loaded: statuses up to date without another request
    else:
        with session() as db:
            for ad_id in done:
                row = db.get(MyAd, ad_id)
                if row:
                    when = f"{utcnow():%d.%m. %H:%M}"
                    if action == "deactivate":
                        row.auto_renew = False    # offline on purpose: auto-renew must not undo it
                        row.relist = None         # nor the relisting after a PayLivery sale
                        row.last_action = t("deactivated on willhaben {when}", when=when)
                    else:
                        row.last_action = t("activated on willhaben {when}", when=when)
                    db.add(row)
            db.commit()
        await import_from_account()
    _note_errors(failed)
    log.info("bulk %s: %d done (%d republished), %d failed", action, len(done), len(moved), len(failed))
    return {"done": [titles.get(i, i) for i in done], "republished": [titles.get(i, i) for i in moved],
            "failed": [(titles.get(i, i), e) for i, e in failed.items()]}


async def cleanup_candidates() -> list[dict[str, Any]]:
    """Old ads worth deleting on willhaben: offline without photos (cannot be put online again), sold or given
    away, and the old copy of a republished ad. Never an active or reserved one.

    Read from the live account list: the app keeps the last known photo, willhaben drops them from old ads.
    """
    ads = await AccountClient(get_session()).my_adverts()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    out = []
    for a in ads:
        if a.status in ("active", "reserved"):
            continue
        row = rows.get(a.ad_id)
        reasons = []
        if row and row.replaced_by:
            reasons.append(t("republished as {id}", id=row.replaced_by))
        if a.status in ("sold", "given_away") or (row and is_sold(row)):
            reasons.append(t("sold or given away"))
        if not a.image_url:
            reasons.append(t("no photo"))
        if reasons:
            out.append({"ad_id": a.ad_id, "heading": a.heading or a.ad_id, "url": a.url,
                        "status": a.status_label or a.status, "reasons": reasons,
                        "deletable": ACTION_DELETE in (a.actions or {})})
    return out


async def cleanup(ad_ids: list[str], job: jobs.Job | None = None) -> dict[str, Any]:
    """Delete the picked candidates on willhaben; ones willhaben offers no deleting for are only hidden here.

    The candidates are fetched again, so nothing outside them (an active ad, say) is ever deleted.
    Returns bulk()'s result plus {"hidden": [titles]}.
    """
    if job:
        job.progress(0, t("Loading the ad list from willhaben"))
    cands = {c["ad_id"]: c for c in await cleanup_candidates()}
    picked = [i for i in dict.fromkeys(ad_ids) if i in cands]
    hide = [i for i in picked if not cands[i]["deletable"]]
    for ad_id in hide:
        set_hidden(ad_id, True)
    delete = [i for i in picked if cands[i]["deletable"]]
    if job:
        job.total = len(delete)
    res = await bulk("delete", delete, job) if delete else {"done": [], "failed": []}
    res["hidden"] = [cands[i]["heading"] for i in hide]
    return res


def enqueue_cleanup(ad_ids: list[str]) -> int:
    """"Clean up" as a background job: the picked ads are marked at once (the page returns right away), the job
    checks them against the fresh candidate list and deletes them one after another. Returns how many were queued."""
    ids = []                      # only the ads this call marked: a marker set by another queued job stays its own
    with session() as db:
        for ad_id in dict.fromkeys(ad_ids):
            row = db.get(MyAd, ad_id)
            if row and not row.pending_action and not row.replaced_by:
                row.pending_action, row.last_error = "delete", None
                db.add(row)
                ids.append(ad_id)
        db.commit()
    if not ids:
        return 0

    async def run(job: jobs.Job) -> str:
        try:
            res = await cleanup(ids, job)
        except Exception as e:
            _note_errors({ad_id: str(e) for ad_id in ids})
            raise
        finally:
            _clear_pending(ids)
        job.problems = len(res["failed"])
        line = t("Deleted on willhaben: {n}", n=len(res["done"]))
        if res["hidden"]:
            line += " · " + t("{n} only hidden here", n=len(res["hidden"]))
        return line + (" · " + t("{n} failed (see the ads)", n=len(res["failed"])) if res["failed"] else "")

    jobs.submit("myads-cleanup", t("Clean up {n} old ads", n=len(ids)), run, total=len(ids),
                keys=[f"ad:{i}" for i in ids], on_cancel=lambda: _clear_pending(ids), grace=True, quiet=True)
    return len(ids)


def _note_errors(errors: dict[str, str]) -> None:
    with session() as db:
        for ad_id, err in errors.items():
            row = db.get(MyAd, ad_id)
            if row:
                row.last_error = err
                db.add(row)
        db.commit()


def enqueue(action: str, ad_ids: list[str]) -> int:
    """Mark the ads right away and work through them as a background job. Returns how many were queued.

    Ads already waiting for an action are left out (no double delete).
    """
    if action not in ACTIONS:
        raise ValueError(action)
    queued, titles = [], []
    with session() as db:
        for ad_id in dict.fromkeys(ad_ids):
            row = db.get(MyAd, ad_id)
            if row is None or row.pending_action or row.replaced_by:
                continue
            row.pending_action, row.last_error = action, None
            db.add(row)
            queued.append(ad_id)
            titles.append(row.heading or ad_id)
        db.commit()
    if not queued:
        return 0
    if len(queued) == 1:
        label = {"delete": t("Delete „{title}“ on willhaben", title=titles[0]),
                 "activate": t("Activate „{title}“ on willhaben", title=titles[0]),
                 "deactivate": t("Deactivate „{title}“ on willhaben", title=titles[0])}[action]
    else:
        label = {"delete": t("Delete {n} ads on willhaben", n=len(queued)),
                 "activate": t("Activate {n} ads on willhaben", n=len(queued)),
                 "deactivate": t("Deactivate {n} ads on willhaben", n=len(queued))}[action]
    # delete cannot be undone on willhaben, and activating an expired ad republishes it under a new id: both wait the
    # grace period in the queue (cancel instead of a confirm); deactivate is reversible on the same id
    jobs.submit(f"myads-{action}", label, lambda job: _run_bulk(job, action, queued), total=len(queued),
                keys=[f"ad:{i}" for i in queued], on_cancel=lambda: _clear_pending(queued),
                grace=action in ("delete", "activate"), quiet=True)
    return len(queued)


async def _run_bulk(job: jobs.Job, action: str, ad_ids: list[str]) -> str:
    try:
        res = await bulk(action, ad_ids, job)
    except Exception as e:    # e.g. logged out before the first request: every ad says why
        _note_errors({ad_id: str(e) for ad_id in ad_ids})
        raise
    finally:
        _clear_pending(ad_ids)
    job.problems = len(res["failed"])
    line = {"delete": t("Deleted on willhaben: {n}", n=len(res["done"])),
            "activate": t("Activated on willhaben: {n}", n=len(res["done"])),
            "deactivate": t("Deactivated on willhaben: {n}", n=len(res["done"]))}[action]
    return line + (" · " + t("{n} failed (see the ads)", n=len(res["failed"])) if res["failed"] else "")


def _clear_pending(ad_ids: list[str]) -> None:
    with session() as db:
        for ad_id in ad_ids:
            row = db.get(MyAd, ad_id)
            if row and row.pending_action:
                row.pending_action = None
                db.add(row)
        db.commit()


def recover() -> None:
    """After a restart the queue is gone: ads still marked were not (or not surely) changed on willhaben.
    An interrupted edit keeps its text (the edit page offers it again); photos waiting for upload are dropped."""
    import shutil
    with session() as db:
        for row in db.exec(select(MyAd).where(col(MyAd.pending_action).is_not(None))):
            if row.pending_action == "edit":
                row.pending_payload = {"unsaved": (row.pending_payload or {}).get("edit")}
            elif row.pending_action == "photos":
                row.pending_payload = None
            row.pending_action, row.last_error = None, t("Interrupted by a restart – please try again")
            db.add(row)
        db.commit()
    shutil.rmtree(_photo_dir(), ignore_errors=True)


def _photo_dir(ad_id: str | None = None):
    from app.config import get_settings
    base = get_settings().data_dir / "pending-photos"
    return base / ad_id if ad_id else base


def _mark(ad_id: str, action: str, payload: dict[str, Any] | None = None) -> MyAd | None:
    """Mark one ad for a queued action; None when it is already waiting for one (never two at once)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None or row.pending_action:
            return None
        keep = {} if action == "edit" else {k: v for k, v in (row.pending_payload or {}).items() if k == "unsaved"}
        row.pending_action, row.pending_payload, row.last_error = action, ({**keep, **payload} if payload else keep or None), None
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _unmark(ad_id: str, error: str | None = None, payload: Any = False) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is not None:
            row.pending_action = None
            if payload is not False:
                row.pending_payload = payload
            elif row.pending_payload:          # job done: what it needed goes, an unsaved edit text stays
                row.pending_payload = {k: v for k, v in row.pending_payload.items() if k == "unsaved"} or None
            if error is not None:
                row.last_error = error
            db.add(row)
            db.commit()


def enqueue_edit(ad_id: str, heading: str, description: str, price: float | None) -> bool:
    """Save title / description / price on willhaben as a background job. The text waits in the database, so a
    restart cannot lose it. False: the ad already waits for another action."""
    from app.willhaben.insert import InsertError
    if not heading.strip() or not description.strip():
        raise InsertError(t("Title and description must not be empty"))
    row = _mark(ad_id, "edit", {"edit": {"heading": heading, "description": description, "price": price}})
    if row is None:
        return False

    async def run(job: jobs.Job) -> str:
        try:
            r = await edit(ad_id, heading, description, price)
        except Exception as e:
            _unmark(ad_id, t("Saving failed: {error}", error=e),
                    {"unsaved": {"heading": heading, "description": description, "price": price}})
            raise
        _unmark(ad_id, payload=None)
        return t("Saved on willhaben – status now: {status}", status=(r.status if r else "") or "")

    unsaved = {"unsaved": {"heading": heading, "description": description, "price": price}}
    jobs.submit("myads-edit", t("Save „{title}“ on willhaben", title=(heading or ad_id)[:60]), run,
                current=f"ad:{ad_id}", on_cancel=lambda: _unmark(ad_id, payload=unsaved), quiet=True)   # the text is kept
    return True


def enqueue_price(ad_id: str, price: float) -> bool:
    """Save a new price on willhaben as a background job; title and text stay as willhaben has them (loaded inside
    the job). False: the ad already waits for another action."""
    row = _mark(ad_id, "edit", {"edit": {"price": price}})
    if row is None:
        return False

    async def run(job: jobs.Job) -> str:
        from app.willhaben import insert
        try:
            ad = await insert.get_own_ad(get_session(), ad_id)
            r = await edit(ad_id, ad.get("heading") or "", insert.description_text(ad.get("description")), price)
        except Exception as e:
            _unmark(ad_id, t("Saving failed: {error}", error=e))
            raise
        _unmark(ad_id, payload=None)
        return t("Saved on willhaben – status now: {status}", status=(r.status if r else "") or "")

    jobs.submit("myads-edit", t("Save the price of „{title}“ on willhaben", title=(row.heading or ad_id)[:60]), run,
                current=f"ad:{ad_id}", on_cancel=lambda: _unmark(ad_id), quiet=True)
    return True


def enqueue_photos(ad_id: str, order: list[str], new_photos: list[tuple[str, bytes]]) -> bool:
    """Reorder / remove / add photos as a background job. New photos wait as files in the data folder until the
    upload. False: the ad already waits for another action."""
    from pathlib import Path

    from app.willhaben.insert import InsertError
    for name, _data in new_photos:
        if PHOTO_MIMES.get(Path(name).suffix.lower()) is None:
            raise InsertError(t("Unsupported photo format: {name} (JPG, PNG, WebP)", name=name))
    row = _mark(ad_id, "photos")
    if row is None:
        return False
    folder = _photo_dir(ad_id)
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for i, (name, data) in enumerate(new_photos):
        path = folder / f"{i:02d}{Path(name).suffix.lower()}"
        path.write_bytes(data)
        files.append((name, path))

    async def run(job: jobs.Job) -> str:
        import shutil
        try:
            r = await set_photos(ad_id, order, [(name, path.read_bytes()) for name, path in files])
        except Exception as e:
            _unmark(ad_id, t("Saving the photos failed: {error}", error=e))
            raise
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        _unmark(ad_id)
        return t("Photos saved on willhaben – status now: {status}", status=(r.status if r else "") or "")

    def cancelled() -> None:
        import shutil
        _unmark(ad_id)
        shutil.rmtree(folder, ignore_errors=True)

    jobs.submit("myads-photos", t("Save the photos of „{title}“ on willhaben", title=(row.heading or ad_id)[:60]), run,
                current=f"ad:{ad_id}", on_cancel=cancelled, quiet=True)
    return True


async def load_for_edit(ad_id: str) -> dict[str, Any]:
    """Current title, description (plain text) and price as willhaben has them."""
    from app.willhaben import insert
    s = get_session()
    ad = await insert.get_own_ad(s, ad_id)
    try:
        images = [{"reference": i["reference"], "thumb": insert.image_thumb(i)}
                  for i in (await insert.get_own_images(s, ad))["advertImage"]]
        images_error = None
    except (insert.InsertError, RuntimeError, ValueError) as e:      # text editing still works without photos
        log.info("photos of ad %s not loadable: %s", ad_id, e)
        images, images_error = [], str(e)
    return {"heading": ad.get("heading") or "", "description": insert.description_text(ad.get("description")),
            "price": ad.get("price"), "images": images, "images_error": images_error}


PHOTO_MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".gif": "image/gif"}


async def set_photos(ad_id: str, order: list[str], new_photos: list[tuple[str, bytes]]) -> MyAd | None:
    """Reorder / remove / add photos on willhaben (first = cover), then refresh the status."""
    from pathlib import Path

    from app.willhaben import insert
    files = []
    for name, data in new_photos:
        mime = PHOTO_MIMES.get(Path(name).suffix.lower())
        if mime is None:
            raise insert.InsertError(t("Unsupported photo format: {name} (JPG, PNG, WebP)", name=name))
        files.append((name, data, mime))
    await insert.set_own_images(get_session(), ad_id, order, files)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.last_action = t("photos changed on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            db.add(row)
            db.commit()
    try:
        await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("status refresh after photo change skipped: %s", e)
    with session() as db:
        return db.get(MyAd, ad_id)


@timeline.scoped(ad_id="ad_id", mine=lambda a: True)
async def ai_revise(ad_id: str, heading: str, description: str, price: float | None,
                    instruction: str) -> dict[str, Any]:
    """Claude revises title and description of a live ad by one instruction (nothing is saved).

    Gets the current text from the edit form, the ad's photos (up to 4) and category from the public page.
    Returns {heading, description, price_estimate, note, questions} as a proposal for the form (questions: what
    Claude could not tell, answered in the form and sent back as the next instruction).
    """
    import tempfile
    from pathlib import Path

    from app.llm.base import get_backend
    from app.llm.prompts import AD_SCHEMA, AD_SYSTEM, ad_refine_prompt
    from app.services import ad_images

    instruction = instruction.strip() or "Überarbeite das Inserat: klarer, verkaufsstärker, ehrlich."
    detail = None
    try:
        detail = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        log.info("ad %s detail for AI edit failed: %s", ad_id, e)
    current = {"title": heading, "description": description,
               "category_hint": " > ".join(getattr(detail, "category_path", None) or []),
               "condition": "", "attributes": []}
    with tempfile.TemporaryDirectory(prefix="wh-edit-") as tmp:
        photos: list[Path] = []
        for i, url in enumerate((getattr(detail, "images", None) or [])[:4], 1):
            # through the shared downloader: counted in the access log and recorded like every willhaben request
            if p := await ad_images.download(url, Path(tmp), f"foto{i}"):
                photos.append(p)
        with session() as db:                # the ad's piece count keeps "N Stück verfügbar" in a revised text
            row = db.get(MyAd, ad_id)
        prompt = ad_refine_prompt(notes=f"Aktueller Preis: {price:g} €" if price else "",
                                  rules=selling.append_to(get_setting("reply_rules"), for_ad=True),
                                  n_images=len(photos), current=current, specs=None, product_name=None,
                                  instruction=instruction, quantity=row.quantity if row else None)
        result = await get_backend("ads").complete(system=AD_SYSTEM, prompt=prompt, schema=AD_SCHEMA, images=photos)
    from app.services.ad_creator import clean_text, questions_of

    d = result.data
    return {"heading": (d.get("title") or heading).strip()[:80],
            "description": (clean_text(d.get("description")) or description).strip(),
            "price_estimate": d.get("price_estimate"), "note": (d.get("note") or "").strip(),
            "questions": questions_of(d) or []}


async def edit(ad_id: str, heading: str, description: str, price: float | None) -> MyAd | None:
    """Save title/description/price on willhaben, then refresh the status (an edit may go to review)."""
    from app.willhaben import insert
    if not heading.strip() or not description.strip():
        raise insert.InsertError(t("Title and description must not be empty"))
    await insert.edit_own_ad(get_session(), ad_id, heading=heading, description=description, price=price)
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.heading = heading.strip()[:80]
            row.last_action = t("edited on willhaben {when}", when=f"{utcnow():%d.%m. %H:%M}")
            db.add(row)
            db.commit()
    try:
        await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("status refresh after edit skipped: %s", e)
    with session() as db:
        return db.get(MyAd, ad_id)


def set_auto_renew(ad_id: str, on: bool) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_renew = on
            db.add(row)
            db.commit()


MAX_QUANTITY = 999


def set_quantity(ad_id: str, quantity: int | None) -> None:
    """Pieces offered in the ad (None = one item) and the sale state that follows from them. Raises ValueError
    (message for the page) outside 1..999, below the pieces already sold, and blank while sales count: a single
    item has one state, its next sale or reset would undo every sale in the ledger. 1 without sales is one item,
    as on a new draft. Lowered to the pieces sold, the ad is sold out here as with its last piece (automatic answers
    stay as the owner set them); raised again after that, it is open again here. willhaben's status is not touched."""
    if quantity is not None and not 1 <= quantity <= MAX_QUANTITY:
        raise ValueError(t("Pieces: a number from 1 to {max}", max=MAX_QUANTITY))
    with session() as db:
        row = current_ad(db, ad_id)
        if row is None:
            return
        sales = stock.active_sales(db, row.ad_id)          # newest first
        sold = sum(s.pieces for s in sales)
        if quantity is None and sold:
            raise ValueError(t("{n} piece is sold already – undo the sale first to clear the count", n=sold)
                             if sold == 1 else
                             t("{n} pieces are sold already – undo the sales first to clear the count", n=sold))
        if quantity is not None and quantity < sold:
            raise ValueError(t("{n} pieces are sold already – the number cannot be lower", n=sold))
        if quantity == 1 and not sold:
            quantity = None
        sold_out = quantity is not None and quantity == sold and row.sale_status != "sold"
        restocked = quantity is not None and quantity > sold and row.sale_status == "sold"
        # texts before the first change: t() opens a session of its own
        note = (t("Sold") if sold_out else
                t("{sold} sold · {left} available", sold=sold, left=quantity - sold) if restocked else None)
        row.quantity = quantity
        if sold_out:
            row.sale_status, row.sale_conversation_id, row.sale_at = "sold", sales[0].conversation_id, sales[0].created_at
            row.auto_renew = False    # sold: auto-renew must never reactivate or republish it
        elif restocked:
            row.sale_status = row.sale_conversation_id = row.sale_at = None
        if note:
            row.last_action = note
        db.add(row)
        db.commit()


_KEEP: Any = object()


def set_auto_reply(ad_id: str, on: bool, instructions: str, min_price: float | None = _KEEP) -> None:
    """Overwrites the instructions; `min_price` only changes when passed (None clears the floor)."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row:
            row.auto_reply, row.auto_reply_instructions = on, instructions.replace("\r\n", "\n").strip() or None
            if min_price is not _KEEP:
                row.min_price = min_price
            db.add(row)
            db.commit()


class AccountIds(set):
    """Ids of the account's ads; `complete` = the list had every ad, so an ad missing from it is gone."""
    complete = False


# willhaben statuses that end an ad's time online (or take it offline by hand): anything else an active ad reports
# for a moment is willhaben checking a change
ENDED_STATUSES = ("expired", "passive", "deactivated", "sold", "given_away", "reserved", "deleted")
_ENDED_WORDS = ("verkauft", "verschenkt", "reserviert", "deaktiviert", "passiv", "abgelaufen", "gelöscht", "sold",
                "given away", "reserved", "deactivated", "expired", "deleted")


def _follow_sale_status(row: MyAd, previous: str) -> None:
    """The sale status here follows what willhaben shows (the sold workflow writes both; this covers marks made on
    willhaben itself): sold there = sold here; reserved there = reserved here (buyer unknown) unless a sale is
    recorded; a reservation lifted on willhaben (reserved → active) lifts the local one too. A piece sold with
    PayLivery while more are left is no sell-out: the ad waits to go online again (relist)."""
    if row.status == "sold" and row.sale_status != "sold" and not row.relist:
        row.sale_status, row.sale_at = "sold", utcnow()
        row.auto_reply, row.auto_renew = False, False
    elif row.status == "reserved" and row.sale_status is None:
        row.sale_status, row.sale_at = "reserved", utcnow()
    elif previous == "reserved" and row.status == "active" and row.sale_status == "reserved":
        row.sale_status = row.sale_conversation_id = row.sale_at = None


def ended(ad: Any) -> bool:
    """The status takes the ad offline for good or by hand (its id, or willhaben's word when the id is unknown)."""
    label = (getattr(ad, "status_label", None) or "").lower()
    return ad.status in ENDED_STATUSES or any(w in label for w in _ENDED_WORDS)


async def import_from_account(ads: AdvertList | None = None) -> AccountIds:
    """Add/update every ad of the logged-in account (`ads`: a list just loaded); returns their ids.

    The account list is the better source: it also knows deactivated and sold ads, which the public
    detail page would only report as gone. Sold ads that are no longer in a complete list were deleted on
    willhaben: they become "deleted" (the public page cannot tell, it never shows sold ads).
    """
    if ads is None:
        ads = await AccountClient(get_session()).my_adverts()
    ids = AccountIds(a.ad_id for a in ads)
    ids.complete = getattr(ads, "complete", False)
    if not ids.complete:    # then nothing is concluded from a missing ad: sold ads deleted on willhaben stay listed here
        log.info("account list incomplete: %d ads of %s", len(ads), getattr(ads, "total", None))
    newly_expired, relist = [], []
    with session() as db:
        for ad in ads:
            row = db.get(MyAd, ad.ad_id) or MyAd(ad_id=ad.ad_id)
            previous = row.status
            row.heading = ad.heading or row.heading
            row.url, row.image_url = ad.url, ad.image_url or row.image_url
            if row.replaced_by:
                row.status = "replaced"   # republished under a new id; the old copy stays "Passiv" on willhaben
            elif has_ended(ad):
                row.status = "expired"    # ran out ("Passiv" on willhaben): auto-renew republishes it
            elif previous == "active" and not ended(ad):
                # willhaben checks a changed ad for a few seconds to minutes and reports another status meanwhile:
                # it stays in Active (it did not vanish from the list for a moment), with willhaben's word for it
                row.status = "active"
                if ad.status != "active":
                    row.last_action = t("willhaben is checking the change: {status}",
                                        status=ad.status_label or ad.status)
            else:
                row.status = ad.status if ad.status in ("active", "expired", "sold", "reserved", "deleted") \
                    else ad.status_label or ad.status
            row.published_at, row.end_date = ad.start or row.published_at, ad.end or row.end_date
            row.last_checked_at, row.last_error = utcnow(), None
            if row.relist and not relist_recent(row.relist):
                row.relist = None                # too old: going offline now is not that sale's doing
            _follow_sale_status(row, previous)
            db.add(row)
            if previous == "active" and row.status == "expired":
                newly_expired.append(row)
            if row.relist and not row.replaced_by and offline(ad) and not row.pending_action:
                relist.append(row.ad_id)         # taken offline after a PayLivery sale: online again as a new ad
        if ids.complete:
            for row in db.exec(select(MyAd).where(col(MyAd.ad_id).not_in(ids), MyAd.status != "deleted",
                                                  col(MyAd.replaced_by).is_(None))):
                if is_sold(row):
                    row.status, row.last_checked_at = "deleted", utcnow()
                    db.add(row)
        db.commit()
        for row in newly_expired:
            db.refresh(row)
    for row in newly_expired:
        await notify("ad_expired", t("Ad expired"), row.heading or row.ad_id, url=MYADVERTS_URL,
                     image=row.image_url, data={"ad_id": row.ad_id})
    for ad_id in relist:
        enqueue_relist(ad_id)
    return ids


async def check(ad_id: str, off_account: bool = False) -> MyAd | None:
    """Status from the public detail page. `off_account`: the ad is missing from the complete account list,
    so a vanished page means it was deleted (not just expired)."""
    try:
        detail = await get_public().get_detail(ad_id)
    except WillhabenError as e:
        with session() as db:
            row = db.get(MyAd, ad_id)
            if row:
                row.last_error, row.last_checked_at = str(e), utcnow()
                db.add(row)
                db.commit()
        return None
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None:
            return None
        previous = row.status
        if detail is None and off_account:
            row.status = "deleted"
        elif previous == "sold":
            pass            # sold on willhaben: the public page only says "gone", keep the better status
        elif row.replaced_by:
            row.status = "replaced"   # republished under a new id: the old copy is gone, not expired
        elif detail is None:
            row.status = "expired"
        else:
            row.status = "active" if detail.status == "active" else "expired"
            row.heading, row.url = detail.heading, detail.url
            row.image_url = detail.images[0] if detail.images else row.image_url
            row.published_at, row.end_date = detail.published_at, detail.end_date
        row.last_checked_at, row.last_error = utcnow(), None
        db.add(row)
        db.commit()
        db.refresh(row)
    if previous == "active" and row.status == "expired":
        await notify("ad_expired", t("Ad expired"), row.heading or row.ad_id, url=MYADVERTS_URL,
                     image=row.image_url, data={"ad_id": row.ad_id})
    return row


async def _reactivate(row: MyAd) -> None:
    from app.willhaben.insert import InsertError

    sess = get_session()
    new_id = row.ad_id
    try:
        new_id = await AccountClient(sess).reactivate(row.ad_id)
        action, err = t("reactivated"), None
    except (EndpointUnknown, NotLoggedIn, SessionUnavailable, InsertError, RuntimeError) as e:
        action, err = t("Reactivation needed manually"), str(e)
    with session() as db:
        r = db.get(MyAd, row.ad_id)
        r.last_action, r.last_error = action, err
        db.add(r)
        db.commit()
    if not err and new_id != row.ad_id:
        row = _move_to_new_id(row.ad_id, new_id)
    if err:
        await notify("ad_expired", t("Please reactivate the ad"), f"{row.heading or row.ad_id} ({action})",
                     url=MYADVERTS_URL, image=row.image_url, data={"ad_id": row.ad_id})
    else:
        await notify("ad_renewed", t("Ad renewed automatically"), row.heading or row.ad_id, url=row.url,
                     image=row.image_url, data={"ad_id": row.ad_id})
        try:    # the account list, not the public page: a just republished ad is not public yet ("expired")
            await import_from_account()
        except (NotLoggedIn, SessionUnavailable, ValueError) as e:
            log.info("status refresh after renewal skipped: %s", e)


def _move_to_new_id(old_id: str, new_id: str) -> MyAd:
    """A republished ad has a new willhaben id: copy the per-ad settings over. The old row stays (marked
    replaced, auto-renew off) because buyer chats keep pointing at the old id and still need its auto-reply
    settings."""
    keep = ("heading", "image_url", "auto_renew", "auto_reply", "auto_reply_instructions", "published_at", "min_price",
            "parcel", "quantity")
    with session() as db:
        old = db.get(MyAd, old_id)
        new = db.get(MyAd, new_id) or MyAd(ad_id=new_id)
        for k in keep:
            if hasattr(old, k):
                setattr(new, k, getattr(old, k))
        new.url, new.status = f"https://www.willhaben.at/iad/object?adId={new_id}", "active"
        new.last_action = t("republished (was {id})", id=old_id)
        old.replaced_by, old.auto_renew, old.status = new_id, False, "replaced"
        db.add(new)
        db.add(old)
        db.commit()
        db.refresh(new)
    log.info("ad %s republished as %s", old_id, new_id)
    return new


# ------------------------------------------------------------------ online again after a PayLivery sale
# willhaben takes an ad offline once a piece is bought with PayLivery. An ad with pieces left goes online again: in
# place when willhaben offers "Aktivieren" / "Neu veröffentlichen", otherwise (an ad marked sold offers neither) as a
# new ad copied from it (insert.copy_plan). The marker (MyAd.relist) is set by sold.on_paylivery_stage; the account
# import queues the job once willhaben shows the ad offline.
RELIST_CHECKS = 3       # still online after this many looks: willhaben kept it online, nothing to do
RELIST_TRIES = 3        # failed copies before the owner is asked to do it by hand
RELIST_CHECK_S = 600    # the next look while willhaben still shows the ad online, or after a failed try
# a marker older than this is dropped: an ad that goes offline later (expired, deactivated on willhaben's own site)
# is not this sale's doing. Covers the seller's 24 h to accept a paid purchase (each later stage arms it again).
RELIST_WINDOW = timedelta(hours=48)


def offline(ad: Any) -> bool:
    """willhaben shows the ad offline. A reserved ad is still listed publicly: a copy would be a second live ad."""
    label = (getattr(ad, "status_label", None) or "").lower()
    return (ended(ad) or has_ended(ad)) and ad.status != "reserved" and "reserv" not in label


def relist_recent(plan: dict[str, Any] | None) -> bool:
    """The marker is recent enough to act on (an older one is dropped by its readers)."""
    try:
        since = datetime.fromisoformat((plan or {}).get("since") or "")
    except ValueError:
        return False
    return (since if since.tzinfo else since.replace(tzinfo=timezone.utc)) >= utcnow() - RELIST_WINDOW


def _set_relist(ad_id: str, plan: dict[str, Any] | None) -> None:
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is not None:
            row.relist = plan
            db.add(row)
            db.commit()


def mark_relist(ad_id: str) -> None:
    """A piece was sold with PayLivery and more are left: put the ad online again once willhaben took it offline.
    Again for a later stage of the purchase (willhaben may take it offline only then): the looks start over, the
    failed tries and a willhaben draft of the copy are kept."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None or row.replaced_by:
            return
        old = row.relist or {}
    _set_relist(ad_id, {**old, "since": utcnow().isoformat(), "checks": 0, "tries": int(old.get("tries") or 0)})


def _schedule_relist_check(ad_id: str) -> None:
    from app import scheduler as sched

    if not sched.scheduler.running:
        return
    sched.scheduler.add_job(_relist_check_job, "date", run_date=utcnow() + timedelta(seconds=RELIST_CHECK_S),
                            args=[ad_id], id=f"relist-{ad_id}", replace_existing=True)


async def _relist_check_job(ad_id: str) -> None:
    enqueue_relist(ad_id)


def enqueue_relist(ad_id: str) -> bool:
    """"Online again" as a background job (lane ads). False: no marker, or the ad already waits for another action."""
    with session() as db:
        row = db.get(MyAd, ad_id)
        if row is None or not row.relist:
            return False
    row = _mark(ad_id, "relist")
    if row is None:
        return False

    async def run(job: jobs.Job) -> str:
        try:
            msg = await relist(ad_id, job)
        except Exception as e:
            _unmark(ad_id, t("Not online again yet: {error}", error=e))
            raise
        _unmark(ad_id)
        return msg

    jobs.submit("myads-relist", t("Put „{title}“ online again", title=(row.heading or ad_id)[:60]), run,
                current=f"ad:{ad_id}", on_cancel=lambda: _unmark(ad_id), quiet=True)
    return True


async def relist(ad_id: str, job: jobs.Job | None = None) -> str:
    """Put an ad with pieces left online again after a PayLivery sale (marker MyAd.relist). Waits while willhaben
    still shows it online (a later look is scheduled); never makes a second live copy. Returns the result line."""
    from app.willhaben.account import ACTION_ACTIVATE, ACTION_FINALIZE, ACTION_REPUBLISH, REPUBLISHABLE

    with session() as db:
        row = db.get(MyAd, ad_id)
    plan = dict(row.relist or {}) if row is not None and not row.replaced_by else {}
    if not relist_recent(plan):
        if plan:
            _set_relist(ad_id, None)
        return t("Nothing to put online again")
    title, left = row.heading or ad_id, stock.remaining(ad_id) or 0
    if left <= 0:
        _set_relist(ad_id, None)
        return t("No pieces left – not put online again")
    sess = get_session()
    acct = AccountClient(sess)
    ads = await acct.my_adverts()
    own = next((a for a in ads if a.ad_id == str(ad_id)), None)
    if own is None and getattr(ads, "complete", False):
        _set_relist(ad_id, None)         # deleted on willhaben: gone on purpose
        return t("„{title}“ was deleted on willhaben – not put online again", title=title)
    # missing from an incomplete list: its status is unknown, so it may still be online
    if own is None or not offline(own):
        plan["checks"] = int(plan.get("checks") or 0) + 1
        if plan["checks"] >= RELIST_CHECKS:
            _set_relist(ad_id, None)
            return t("„{title}“ is still online on willhaben – nothing to do", title=title)
        _set_relist(ad_id, plan)
        _schedule_relist_check(ad_id)
        return t("„{title}“ is still online on willhaben – looked at again later", title=title)
    links = (own.actions or {}) if own is not None else {}
    plan["tries"] = int(plan.get("tries") or 0) + 1
    _set_relist(ad_id, plan)
    try:
        if ACTION_ACTIVATE in links or ACTION_FINALIZE in links or (ACTION_REPUBLISH in links and own.status in REPUBLISHABLE):
            new_id = await acct.reactivate(ad_id, ads)
        else:
            new_id = await _copy_as_new(sess, ad_id, plan, job)
    except Exception as e:
        if plan["tries"] >= RELIST_TRIES:
            _set_relist(ad_id, None)
            await notify("ad_expired", t("Please put the ad online again"),
                         t("{title}: {left} pieces left, willhaben took it offline after the PayLivery sale ({error})",
                           title=title, left=left, error=e), url=MYADVERTS_URL, image=row.image_url,
                         data={"ad_id": ad_id})
        else:
            _schedule_relist_check(ad_id)
        raise
    _set_relist(ad_id, None)
    if new_id != str(ad_id):
        new = _move_to_new_id(ad_id, new_id)
    else:
        with session() as db:
            new = db.get(MyAd, ad_id)
            new.last_action = t("online again after the PayLivery sale")
            if new.sale_status == "reserved" and not new.sale_conversation_id:
                new.sale_status = new.sale_at = None     # willhaben's reservation for this purchase (_follow_sale_status)
            db.add(new)
            db.commit()
            db.refresh(new)
    await notify("ad_renewed", t("Ad online again"),
                 t("{title}: {left} pieces left", title=title, left=left), url=new.url, image=new.image_url,
                 data={"ad_id": new.ad_id})
    try:    # the account list: a just placed ad is not public yet
        await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("status refresh after relisting skipped: %s", e)
    log.info("ad %s online again as %s after a PayLivery sale (%d left)", ad_id, new_id, left)
    return t("„{title}“ online again ({left} pieces left)", title=title, left=left)


async def _copy_as_new(sess, ad_id: str, plan: dict[str, Any], job: jobs.Job | None) -> str:
    """The ad placed again as a new ad: text, category, attributes, photos and shipping from the old one. A draft a
    former try left on willhaben (plan["draft"]) is finished instead of starting another one."""
    import tempfile
    from pathlib import Path

    from app.services import ad_images
    from app.willhaben import insert

    copy = await insert.copy_plan(sess, ad_id)        # read-only: nothing is created when this fails
    photos = []
    with tempfile.TemporaryDirectory(prefix="wh-relist-") as tmp:
        for i, url in enumerate(copy["photos"], 1):
            p = await ad_images.download(url, Path(tmp), f"foto{i}")
            if p is None:
                raise insert.InsertError(t("Photo {i} of the ad could not be loaded", i=i))
            photos.append((p.name, p.read_bytes(), PHOTO_MIMES.get(p.suffix.lower(), "image/jpeg")))

    def remember(wh_id: str) -> None:                 # a later try continues this willhaben draft
        _set_relist(ad_id, {**plan, "draft": wh_id})

    def on_step(step: str, i: int = 0, n: int = 0) -> None:
        if job is not None:
            job.progress(0, t("Uploading photo {i}/{n}", i=i, n=n) if step == "photo" else t("Saving the ad"),
                         f"ad:{ad_id}")

    ship = copy["shipping"] if copy["shipping"]["handover"] != "pickup" else None
    res = await insert.publish(sess, copy["ad"], photos, existing_ad_id=plan.get("draft"), on_presaved=remember,
                               shipping=ship, on_step=on_step)
    return str(res["ad_id"])


def start_update(label: str | None = None) -> jobs.Job:
    """"Update from willhaben" as a background job (an update already waiting in the queue is reused)."""
    return jobs.submit("myads-update", label or t("Update my ads from willhaben"), _run_update, reuse_queued=True)


async def scheduled_update() -> None:
    await start_update(t("Daily update of my ads")).task
    from app.services import crosspost
    try:
        await crosspost.refresh()            # the state of the ads also on Vinted (draft, online, sold …)
    except Exception as e:  # noqa: BLE001 - Vinted logged out, slow: tomorrow again
        log.info("vinted state of the linked ads not read: %s", e)


async def _run_update(job: jobs.Job) -> str:
    res = await check_all(job)
    msg = t("{checked} checked, {warned} warnings, {reactivated} reactivations",
            checked=res["checked"], warned=res["warned"], reactivated=res["reactivation"])
    if res["imported"]:
        msg = t("{n} loaded from willhaben", n=res["imported"]) + ", " + msg
    if res["deleted"]:
        msg += ", " + t("{n} deleted on willhaben", n=res["deleted"])
    return msg


async def check_all(job: jobs.Job | None = None) -> dict[str, int]:
    warn_days = int(get_setting("expiry_warn_days") or 3)
    imported: set[str] = set()
    if job:
        job.progress(0, t("Loading the ad list from willhaben"))
    with session() as db:
        known = set(db.exec(select(MyAd.ad_id).where(MyAd.status != "deleted")))
    try:
        imported = await import_from_account()
    except (NotLoggedIn, SessionUnavailable, ValueError) as e:
        log.info("my-ads import skipped: %s", e)
    complete = getattr(imported, "complete", False)
    with session() as db:
        rows = list(db.exec(select(MyAd)))
    ids = [r.ad_id for r in rows if r.status != "deleted"]
    # not in the account list: renewed under a new id, sold (the public page never shows those) and ads being
    # changed right now need no lookup; the rest gets one public detail request each
    lookups = [r.ad_id for r in rows if r.ad_id not in imported and r.status != "deleted" and not r.replaced_by
               and not r.pending_action and not is_sold(r)]
    if job:
        job.total = len(lookups)
    counts = {"imported": len(imported), "checked": 0, "warned": 0, "reactivation": 0, "deleted": 0}
    for ad_id in ids:
        if ad_id in lookups:
            if job:
                job.progress(lookups.index(ad_id), t("Checking „{title}“", title=next(
                    (r.heading or ad_id for r in rows if r.ad_id == ad_id), ad_id)), f"ad:{ad_id}")
            row = await check(ad_id, off_account=complete)   # added by hand / not in the account list
            if row is not None and row.status == "deleted":
                continue
        else:
            with session() as db:
                row = db.get(MyAd, ad_id)
        if row is None:
            continue
        counts["checked"] += 1
        if row.sale_status == "sold":
            continue    # sold (sold workflow): no expiry warnings, never reactivated/republished
        if row.status == "active" and row.end_date and row.end_date - utcnow() < timedelta(days=warn_days) \
                and row.notified_expiry_for != row.end_date:
            await notify("ad_expiring", t("Ad expires soon"),
                         t("{title}: ends {when} UTC", title=row.heading, when=f"{row.end_date:%d.%m. %H:%M}"), url=row.url,
                         image=row.image_url, data={"ad_id": row.ad_id, "end_date": row.end_date.isoformat()})
            with session() as db:
                r = db.get(MyAd, ad_id)
                r.notified_expiry_for = row.end_date
                db.add(r)
                db.commit()
            counts["warned"] += 1
        if row.status == "expired" and row.auto_renew and not row.pending_action:   # a queued delete/sold wins
            await _reactivate(row)
            counts["reactivation"] += 1
    with session() as db:
        counts["deleted"] = len(known & set(db.exec(select(MyAd.ad_id).where(MyAd.status == "deleted"))))
    return counts
