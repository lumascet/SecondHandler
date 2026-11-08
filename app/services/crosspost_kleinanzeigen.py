"""An own willhaben ad also on Kleinanzeigen (kleinanzeigen.de): posted through the site's own ad form in a hidden tab
of the logged-in browser (app/platforms/kleinanzeigen_listing.py), the state kept on the ad (MyAd.kleinanzeigen).

What is posted: the willhaben draft the ad was published from (its text, price, condition and local photos, the
handover from its shipping) – or, for an ad without such a draft, willhaben's own ad read through the editor
(insert.copy_plan, read-only) with its photos downloaded. The category is the one the site detects from the text when
Claude says it fits; otherwise Claude picks it level by level from the site's category dialog. Nothing is posted
without a price: a free ad is price 0 on willhaben.

Once posted, the ad's Kleinanzeigen copy follows the willhaben ad: reserved while it is not for sale there, active
again when it is (sync, hourly and right after the app changes the ad). "Delete" removes the copy on Kleinanzeigen
only after a grace period.

A posting that may have gone through (the final click made, no confirmation seen) is state "check": it is never
posted again on its own. The next sync links it to the one own ad with that title that no other willhaben ad is
linked to; with none or several, it stays "check" and says how many were found. "Forget" drops the record (the
owner decides that the posting is not online, or deletes the copy on Kleinanzeigen first). An own ad missing from the
own-ads list is "gone" only after two syncs in a row, and never on an empty list (no answer is not "no ads").

Interface used by the rest of the app (ad_creator after a willhaben publish, My ads, the batch table):
- available() -> bool: the Kleinanzeigen account is logged in (site_accounts) and the platform is switched on
- enqueue(ad_id) -> bool: post the willhaben ad `ad_id` on Kleinanzeigen as a background job
- enqueue_many(ad_ids) -> int
- enqueue_reserve / enqueue_activate / enqueue_delete(ad_id) -> bool: the copy on Kleinanzeigen, as a job
- schedule_sync(ad_id): the copy follows the willhaben ad's state, quietly; a no-op for an ad not on Kleinanzeigen
- scheduled_sync(): the hourly sync of every linked ad (app/scheduler.py)
- forget(ad_id) -> bool: drop a "check" record (My ads → Forget)
- recover(): after a restart, a step that was on its way is over (a submit that may have gone through: "check")

Not verified against the live site: every selector and text of the posting form and the own-ads page is the
listing module's (see its docstring); the category walk and the reserve/activate clicks are untested with a real page.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlmodel import col, select

from app.db import AdDraft, MyAd, get_setting, session, utcnow
from app.i18n import t
from app.llm.base import get_backend
from app.platforms import condition
from app.platforms import kleinanzeigen_listing as kl
from app.services import jobs
from app.services import crosspost, kleinanzeigen_price

log = logging.getLogger(__name__)

PLATFORM = "kleinanzeigen"
VINTED_ONLY = "vinted-"           # the ids of ads only on Vinted: never listed here
MAX_LEVELS = 6                    # category dialog levels Claude walks at most (the site's tree is deeper only rarely)
JOB_LANE_KIND = "myads-kleinanzeigen"
SYNC_KIND = "kleinanzeigen-sync"
NO_CATEGORY = "no fitting Kleinanzeigen category was found"
SUBMIT_UNCERTAIN = "Submitted – no confirmation came: check on Kleinanzeigen"


def available() -> bool:
    """The account is logged in in the app's browser and the platform is on. Unknown login state counts as not."""
    from app.services import site_accounts
    if get_setting(f"platform_{PLATFORM}_enabled") != "1":
        return False
    return site_accounts.state(PLATFORM)["logged_in"] is True


# ------------------------------------------------------------------ the state on the ad
def _row(ad_id: str) -> MyAd | None:
    with session() as db:
        return db.get(MyAd, str(ad_id))


def _ka(row: MyAd | None) -> dict[str, Any]:
    return dict(row.kleinanzeigen or {}) if row is not None else {}


def _set(ad_id: str, **fields: Any) -> None:
    with session() as db:
        row = db.get(MyAd, str(ad_id))
        if row is not None:
            row.kleinanzeigen = {**(row.kleinanzeigen or {}), **fields}
            db.add(row)
            db.commit()


def _unset(ad_id: str) -> None:
    with session() as db:
        row = db.get(MyAd, str(ad_id))
        if row is not None:
            row.kleinanzeigen = None
            db.add(row)
            db.commit()


def _busy(ad_id: str, ka: dict[str, Any]) -> bool:
    """A step on the ad's Kleinanzeigen copy is queued or running. A marker without a job is stale (recover())."""
    return bool(ka.get("pending")) and f"ka:{ad_id}" in jobs.current_keys("ka:")


def _fail(ad_id: str, e: BaseException) -> None:
    """A step that failed: the marker goes, the ad says why (login, captcha or the error itself)."""
    if isinstance(e, kl.LoggedOut):
        from app.services import site_accounts
        site_accounts._save(PLATFORM, False, str(e))
        text = t("Not logged in to Kleinanzeigen: log in under Settings → Platforms → Kleinanzeigen")
    elif isinstance(e, kl.Captcha):
        text = t("A captcha stands in the way on Kleinanzeigen: solve it in the app's browser, then try again")
    else:
        text = t("Not on Kleinanzeigen: {error}", error=e)
    _set(ad_id, pending=None, error=text[:300])


def _summary(done: dict[str, int]) -> str:
    return t("Kleinanzeigen: {changes}", changes=", ".join(f"{k} {n}" for k, n in done.items())) if done \
        else t("Kleinanzeigen: nothing to change")


# ------------------------------------------------------------------ what is posted
def _condition_value(raw: str | None) -> str | None:
    """A draft's condition as the normalised value: the value itself, or the German label Claude wrote."""
    return raw if raw in condition.VALUES else condition.from_text(raw)


def _about(plan: kl.Plan) -> str:
    return f"Titel: {plan.title}\nText:\n{plan.description[:2000]}"


def _draft_source(ad_id: str) -> dict[str, Any] | None:
    """The published willhaben draft the ad came from (newest first), its fields read while the session is open."""
    with session() as db:
        d = db.exec(select(AdDraft).where(AdDraft.wh_ad_id == str(ad_id), AdDraft.status == "published")
                    .order_by(col(AdDraft.id).desc())).first()
        if d is None:
            return None
        return {"title": d.title, "description": d.description, "price": d.price_suggestion, "condition": d.condition,
                "photos": [p for p in d.image_paths or [] if Path(p).is_file()], "wh_plan": dict(d.wh_plan or {}),
                "also": dict(d.also or {}), "kleinanzeigen": dict(d.kleinanzeigen or {})}


PRICE_FLOOR = 0.7     # an analysed price below this share of willhaben's is not posted: willhaben's price instead


def _guard(price: float | None, note: str | None, wh_price: float | None,
           min_price: float | None) -> tuple[float | None, str | None]:
    """An analysed price the owner did not confirm: never below the ad's own lowest price, nor below PRICE_FLOOR of
    willhaben's price (a bad set of comparable ads must not post a 500 € item at 50 €) – willhaben's price then."""
    if price is None or not wh_price:
        return price, note
    floor = max(float(min_price or 0), PRICE_FLOOR * float(wh_price))
    if price < floor:
        log.info("kleinanzeigen price %.2f below %.2f (willhaben %.2f): willhaben's price is posted", price, floor,
                 wh_price)
        return float(wh_price), t("the comparable ads were far cheaper – willhaben's price is kept")
    return price, note


def _draft_price(draft: dict[str, Any], min_price: float | None = None) -> tuple[float | None, str | None]:
    """The price the Kleinanzeigen copy is posted at, in order: the price typed for it on the draft ("also"), a free
    willhaben ad stays free (0: the analysis does not change a giveaway), the analysis of the comparable ads there
    (services/kleinanzeigen_price.py), willhaben's price. With its note when the analysis gave the price."""
    typed = (draft["also"] or {}).get("kleinanzeigen_price")
    if typed is not None:
        return float(typed), None
    if draft["price"] == 0:
        return 0.0, None
    ka = draft["kleinanzeigen"] or {}
    if ka.get("price"):                  # the analysis: within the guard (not typed by the owner)
        return _guard(float(ka["price"]), ka.get("note"), draft["price"], min_price)
    return (float(draft["price"]) if draft["price"] is not None else None), None


async def _analysed_price(heading: str | None, condition: str | None, shipping: bool | None,
                          wh_price: float | None) -> tuple[float | None, str | None]:
    """The price of a copy whose ad has no draft: the comparable ads on Kleinanzeigen decide when they give one, else
    willhaben's price stays. A failure there is logged and never stops the posting."""
    try:
        ka = await kleinanzeigen_price.analyse(heading, kleinanzeigen_price.heading_keywords(heading),
                                               condition=condition, shipping=shipping, willhaben_price=wh_price)
    except Exception as e:  # noqa: BLE001 - the willhaben price is posted instead
        log.info("kleinanzeigen price not analysed for %r: %s", heading, e)
        return None, None
    if not ka or not ka.get("price"):
        return None, None
    return float(ka["price"]), ka.get("note")


async def _source(ad_id: str, row: MyAd, tmp: Path, step: Callable[[str], None]) -> kl.Plan:
    """The ad as the posting form takes it: the draft it was published from, else willhaben's own ad (read-only)
    with its photos downloaded into `tmp`. ListingError when there is no price or no photo; copy_plan's error
    (the ad is not in the editor) goes up as it is."""
    zip_code = get_setting("kleinanzeigen_zip") or None
    draft = _draft_source(ad_id)
    if draft is not None:
        price, note = _draft_price(draft, row.min_price)
        if price is None:
            raise kl.ListingError(t("the ad has no price on willhaben (a free ad is price 0)"))
        ship = draft["wh_plan"].get("shipping") or {}
        shipping = ship.get("handover") in ("shipping", "both") or ship.get("shipping") is True
        plan = kl.build_plan(title=draft["title"] or row.heading or "", description=draft["description"] or "",
                             price=price, condition_value=_condition_value(draft["condition"]),
                             shipping=shipping, zip_code=zip_code, photos=draft["photos"])
        plan.price_note, plan.wh_price = note, draft["price"]
        return plan
    from app.services import ad_images
    from app.willhaben import insert
    from app.willhaben.session import get_session

    step(t("Reading the ad on willhaben"))
    src = await insert.copy_plan(get_session(), ad_id)           # read-only on willhaben
    if src["ad"].get("price") in (None, ""):
        raise kl.ListingError(t("the ad has no price on willhaben (a free ad is price 0)"))
    files = []
    for i, url in enumerate(src["photos"][:kl.MAX_PHOTOS], 1):
        p = await ad_images.download(url, tmp, f"foto{i}", platform=PLATFORM)
        if p is None:
            raise kl.ListingError(t("Photo {i} of the ad could not be loaded", i=i))
        files.append(str(p))
    plan = kl.plan_from_willhaben(src["ad"], src["shipping"], files, zip_code)
    plan.wh_price = plan.price
    if plan.price:                       # a giveaway stays one (its price type is set by build_plan from the price)
        step(t("Comparing prices on Kleinanzeigen"))
        price, note = await _analysed_price(plan.title, plan.condition, plan.shipping, plan.price)
        price, note = _guard(price, note, plan.wh_price, row.min_price)
        if price is not None:
            plan.price, plan.price_note = price, note
    return plan


# ------------------------------------------------------------------ the category
async def _ask(prompt: str, n: int) -> int | None:
    res = await get_backend("ads").complete(system=kl.SYSTEM, schema=kl.PICK_SCHEMA, prompt=prompt)
    return kl.pick_index(res.data, n)


async def _category(up: kl.Upload, about: str, detected: list[str] | None, step: Callable[[str], None]) -> list[str]:
    """The site's detected category when Claude says it fits the ad; else the dialog walked level by level, Claude
    picking one label per level (-1 or a label out of range: no fitting category, nothing is posted)."""
    if detected:
        fits = await _ask(f"{about}\n\nDie Plattform hat diese Kategorie erkannt: {' > '.join(detected)}\n"
                          "Antworte 0, wenn sie zu dem passt, was verkauft wird; 1, wenn nicht.\n0: passt\n1: etwas anderes",
                          2)
        if fits == 0:
            return detected
    step(t("Claude picks the Kleinanzeigen category"))
    labels = await up.open_category_dialog()
    path: list[str] = []
    for _ in range(MAX_LEVELS):
        idx = await _ask(kl.pick_category_prompt(about, labels, path), len(labels))
        if idx is None:
            raise kl.ListingError(t(NO_CATEGORY))
        path.append(labels[idx])
        nxt = await up.choose_category(labels[idx])
        if nxt is None:                                 # a leaf: the category is chosen
            await up.apply_category()
            return path
        labels = nxt
    raise kl.ListingError(t(NO_CATEGORY))               # deeper than the levels Claude may walk: not guessed


# ------------------------------------------------------------------ posting
async def to_kleinanzeigen(ad_id: str, job: jobs.Job | None = None) -> str:
    """Post the willhaben ad `ad_id` on Kleinanzeigen and keep the result on the ad. Returns the result line; raises
    with the reason (Uncertain never raises: the ad is set to "check" and the line says so)."""
    row = _row(ad_id)
    if row is None:
        raise kl.ListingError(t("Ad not found"))
    if _ka(row).get("id") and _ka(row).get("state") != "gone":
        return t("„{title}“ is already on Kleinanzeigen", title=row.heading or ad_id)

    def step(text: str) -> None:
        if job is not None:
            job.progress(0, text, f"ka:{ad_id}")

    with tempfile.TemporaryDirectory(prefix="wh-kleinanzeigen-") as tmp:
        plan = await _source(ad_id, row, Path(tmp), step)
        about = _about(plan)
        step(t("Opening Kleinanzeigen's ad form"))
        async with kl.Upload() as up:
            await up.describe(plan.description)            # the site detects its category from the text
            path = await _category(up, about, await up.detected_category(), step)
            await up.set_condition(plan.condition)         # False: this category has no condition to set
            await up.set_price(plan.price_type, plan.price)
            await up.set_shipping(plan.shipping)
            await up.set_zip(plan.zip)
            step(t("Uploading {n} photos to Kleinanzeigen", n=len(plan.photos)))
            await up.photos(plan.photos)
            step(t("Posting on Kleinanzeigen"))
            marked = []

            def mark() -> None:
                marked.append(True)
                _set(ad_id, pending="submitting", title=plan.title)
            try:
                aid = await up.submit(plan.title, on_submitting=mark)
            except BaseException as e:              # noqa: BLE001 - routed below, never retried
                # Once the marker is set the final click may have been made: any error from here on is a posting to
                # check, not a failure to retry - also a cancel or the job's timeout (CancelledError), and also the
                # listing module's own ListingError after the click: its signs of "nothing posted" (a rejected form
                # still on screen, a paid category) come from selectors not yet seen live, and a false one would
                # let a second List post the ad twice. Until they are, the owner looks first.
                if not marked:
                    raise
                _set(ad_id, state="check", pending=None, title=plan.title, checked=utcnow().isoformat(),
                     error=t(SUBMIT_UNCERTAIN))
                log.warning("kleinanzeigen: ad %s left to check after %s", ad_id, type(e).__name__)
                if not isinstance(e, Exception):
                    raise                           # a cancel goes on, the record is safe
                return t(SUBMIT_UNCERTAIN)
    now = utcnow().isoformat()
    cat = " > ".join(path)
    _set(ad_id, id=aid, url=kl.ad_url(aid), state="active", category=cat, at=now, checked=now, pending=None,
         error=None, title=None, hidden_by=None, price=plan.price, price_note=plan.price_note, wh_price=plan.wh_price)
    return t("„{title}“ is on Kleinanzeigen: {path}", title=plan.title[:60], path=cat)


def after_review(ad_id: str) -> None:
    """A fresh willhaben ad with "also on Kleinanzeigen": it is posted once willhaben's review let it online
    (my_ads.review_check_job), not before – enqueue() lists only an ad that is for sale."""
    _set(ad_id, wish="after_review")


def review_done(ad_ids: list[str]) -> int:
    """Ads willhaben let online: the ones waiting for it (after_review) go on Kleinanzeigen now. How many."""
    n = 0
    for i in ad_ids:
        row = _row(i)
        if (row is not None and _ka(row).get("wish") == "after_review") and enqueue(i):
            _set(i, wish=None)
            n += 1
    return n


def enqueue(ad_id: str) -> bool:
    """"List" on the Kleinanzeigen line as a background job (grace: it goes live). False: not an ad of this app, already
    listed or on its way, or waiting for a check."""
    row = _row(ad_id)
    ka = _ka(row)
    if row is None or row.replaced_by or str(ad_id).startswith(VINTED_ONLY) or row.status != "active":
        return False                                # only an ad that is for sale (not sold, expired or deleted)
    if (ka.get("id") and ka.get("state") != "gone") or ka.get("state") == "check" or _busy(ad_id, ka):
        return False
    _set(ad_id, pending="listing", error=None)

    async def run(job: jobs.Job) -> str:
        try:
            return await to_kleinanzeigen(ad_id, job)
        except Exception as e:                      # shown on the ad's Kleinanzeigen line
            _fail(ad_id, e)
            raise

    jobs.submit(JOB_LANE_KIND, t("„{title}“ on Kleinanzeigen", title=_title(ad_id)), run,
                current=f"ka:{ad_id}", on_cancel=lambda: _set(ad_id, pending=None), grace=True, quiet=True)
    return True


def forget(ad_id: str) -> bool:
    """Drop a "check" record: this app stops waiting for the posting, and the ad may be listed again. A posting that
    went live stays on Kleinanzeigen (the owner deletes it there). False for any other state."""
    row = _row(ad_id)
    if row is None or _ka(row).get("state") != "check":
        return False
    _unset(ad_id)
    return True


def enqueue_many(ad_ids: list[str]) -> int:
    return sum(1 for i in dict.fromkeys(ad_ids) if enqueue(i))


# ------------------------------------------------------------------ the copy on Kleinanzeigen
def _listed_copy(ad_id: str, pending: str, job_title: str, done_text: str,
                 op: Callable[[kl.OwnAds, str], Awaitable[Any]], done: dict[str, Any]) -> bool:
    row = _row(ad_id)
    ka = _ka(row)
    if row is None or not ka.get("id") or ka.get("state") == "gone" or _busy(ad_id, ka):
        return False
    _set(ad_id, pending=pending, error=None)

    async def run(job: jobs.Job) -> str:
        try:
            async with kl.OwnAds() as own:
                await op(own, str(ka["id"]))
        except Exception as e:
            _fail(ad_id, e)
            raise
        _set(ad_id, pending=None, error=None, checked=utcnow().isoformat(), **done)
        return done_text

    jobs.submit(JOB_LANE_KIND, job_title, run, current=f"ka:{ad_id}", on_cancel=lambda: _set(ad_id, pending=None),
                quiet=True)
    return True


def _title(ad_id: str) -> str:
    row = _row(ad_id)
    return ((row.heading if row else None) or ad_id)[:60]


def enqueue_reserve(ad_id: str) -> bool:
    return _listed_copy(ad_id, "reserving", t("Reserve „{title}“ on Kleinanzeigen", title=_title(ad_id)),
                        t("Reserved on Kleinanzeigen"), lambda own, aid: own.reserve(aid),
                        {"state": "reserved", "hidden_by": None})


def enqueue_activate(ad_id: str) -> bool:
    return _listed_copy(ad_id, "activating", t("Activate „{title}“ on Kleinanzeigen", title=_title(ad_id)),
                        t("Activated on Kleinanzeigen"), lambda own, aid: own.activate(aid),
                        {"state": "active", "hidden_by": None})


def enqueue_delete(ad_id: str) -> bool:
    """Delete the copy on Kleinanzeigen (irreversible: grace first, Cancel while it waits). The ad keeps its willhaben
    side; the link to the Kleinanzeigen ad goes."""
    row = _row(ad_id)
    ka = _ka(row)
    if row is None or not ka.get("id") or ka.get("state") == "gone" or _busy(ad_id, ka):
        return False
    _set(ad_id, pending="deleting", error=None)

    async def run(job: jobs.Job) -> str:
        try:
            async with kl.OwnAds() as own:
                await own.delete(str(ka["id"]))          # False: it was gone already, the same result
        except Exception as e:
            _fail(ad_id, e)
            raise
        _unset(ad_id)
        return t("Deleted on Kleinanzeigen")

    jobs.submit(JOB_LANE_KIND, t("Delete „{title}“ on Kleinanzeigen", title=_title(ad_id)), run,
                current=f"ka:{ad_id}", on_cancel=lambda: _set(ad_id, pending=None), grace=True, quiet=True)
    return True


# ------------------------------------------------------------------ the sync
def _tracked(only: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """The ads with a Kleinanzeigen ad (not gone) or a posting to check: {ad_id: its MyAd.kleinanzeigen}."""
    with session() as db:
        rows = db.exec(select(MyAd).where(col(MyAd.kleinanzeigen).is_not(None))).all()
        out = {}
        for r in rows:
            v = dict(r.kleinanzeigen or {})
            if (only is not None and r.ad_id not in only) or r.replaced_by or v.get("state") == "gone":
                continue
            if v.get("id") or v.get("state") == "check":
                out[r.ad_id] = v
    return out


def _listed_state(item: dict[str, str]) -> str:
    return item.get("state") or "unknown"


def _linked_elsewhere(ad_id: str) -> set[str]:
    """The Kleinanzeigen ad ids already on another willhaben ad's row: never linked a second time."""
    with session() as db:
        rows = db.exec(select(MyAd).where(col(MyAd.kleinanzeigen).is_not(None))).all()
        return {str(r.kleinanzeigen["id"]) for r in rows
                if r.ad_id != str(ad_id) and (r.kleinanzeigen or {}).get("id")}


# an ad missing from the own-ads list is "gone" only when it was missing on this many syncs in a row
MISSING_SYNCS = 2


async def _sync_one(own: kl.OwnAds, ad_id: str, v: dict[str, Any], listed: list[dict[str, str]],
                    by_id: dict[str, dict[str, str]], counts: dict[str, int]) -> None:
    now = utcnow().isoformat()
    if not v.get("id"):                                 # "check": the posting may be live under its title
        title = kl.norm_label(v.get("title"))
        taken = _linked_elsewhere(ad_id)
        same = [a for a in listed if title and kl.norm_label(a["title"]) == title]
        free = [a for a in same if a["id"] not in taken]          # an ad another row is linked to is not this one
        if len(free) == 1:                              # exactly one free ad with that title: that is the posting
            _set(ad_id, id=free[0]["id"], url=kl.ad_url(free[0]["id"]), state=_listed_state(free[0]), title=None,
                 pending=None, error=None, at=now, checked=now, missing=0)
            counts["linked"] += 1
        else:                                           # none or several: left as "check", the owner decides
            _set(ad_id, error=t("{n} own ads on Kleinanzeigen have this title: not linked, check them by hand",
                                n=len(same)), checked=now)
        return
    aid = str(v["id"])
    row = _row(ad_id)
    if row is None:
        return
    if aid not in by_id:
        if not listed:                                  # an empty list is no answer: nothing is taken for gone
            return
        missing = int(v.get("missing") or 0) + 1
        if missing < MISSING_SYNCS:
            _set(ad_id, missing=missing, checked=now)
            return
        _set(ad_id, state="gone", missing=0, checked=now, error=None)
        counts["gone"] += 1
        return
    if v.get("missing"):
        _set(ad_id, missing=0)
    shown = _listed_state(by_id[aid])
    why = crosspost.not_for_sale(row)
    if why and shown == "active":
        await own.reserve(aid)
        _set(ad_id, state="reserved", hidden_by="app", checked=now, error=None)
        counts["reserved"] += 1
    elif not why and shown == "reserved" and v.get("hidden_by") == "app":
        await own.activate(aid)
        _set(ad_id, state="active", hidden_by=None, checked=now, error=None)
        counts["activated"] += 1
    elif shown in ("active", "reserved") and shown != v.get("state"):
        _set(ad_id, state=shown, hidden_by=None, checked=now, error=None)     # the owner did it on Kleinanzeigen
    else:
        _set(ad_id, checked=now)


async def sync(only: list[str] | None = None) -> dict[str, int]:
    """Bring the copies on Kleinanzeigen in step with willhaben (see the module text). Returns the counts of what
    changed; the site is not opened without a linked ad, nor when the account is known to be logged out."""
    from app.services import site_accounts
    tracked = _tracked(only)
    if not tracked or site_accounts.state(PLATFORM)["logged_in"] is False:
        return {}
    counts = {"linked": 0, "reserved": 0, "activated": 0, "gone": 0, "failed": 0}
    try:
        async with kl.OwnAds() as own:
            listed = await own.list()
            by_id = {a["id"]: a for a in listed}
            for ad_id, v in tracked.items():
                try:
                    await _sync_one(own, ad_id, v, listed, by_id, counts)
                except kl.LoggedOut:
                    raise
                except Exception as e:                  # one ad must not stop the others; tried again next time
                    log.warning("kleinanzeigen sync of %s failed: %s", ad_id, e)
                    counts["failed"] += 1
                    _set(ad_id, error=str(e)[:300], checked=utcnow().isoformat())
    except kl.LoggedOut as e:
        site_accounts._save(PLATFORM, False, str(e))
        return {}
    return {k: n for k, n in counts.items() if n}


def schedule_sync(ad_id: str) -> None:
    """Sync one linked ad soon, as a quiet background job; a cheap no-op for an ad not on Kleinanzeigen."""
    if str(ad_id) not in _tracked([str(ad_id)]):
        return

    async def run(job: jobs.Job) -> str:
        return _summary(await sync([str(ad_id)]))

    jobs.submit(SYNC_KIND, t("Sync with Kleinanzeigen"), run, current=f"ka:{ad_id}", quiet=True, reuse_queued=True)


async def _run_all() -> str:
    return _summary(await sync())


async def scheduled_sync() -> None:
    """The hourly sync (a quiet job only with linked ads and an account that is not known to be logged out)."""
    from app.services import site_accounts
    with session() as db:                 # online by another way than the review check (the daily update): due now
        waiting = [r.ad_id for r in db.exec(select(MyAd).where(col(MyAd.kleinanzeigen).is_not(None),
                                                                MyAd.status == "active"))
                   if (r.kleinanzeigen or {}).get("wish") == "after_review"]
    if waiting and site_accounts.state(PLATFORM)["logged_in"] is not False:
        review_done(waiting)
    if not _tracked() or site_accounts.state(PLATFORM)["logged_in"] is False:
        return
    await jobs.submit(SYNC_KIND, t("Sync with Kleinanzeigen"), lambda job: _run_all(), quiet=True,
                      reuse_queued=True).task


def recover() -> None:
    """After a restart the queue is gone: a step that was on its way is over. A posting whose final click may have been
    made is "check" (never posted again on its own); other steps are dropped with a note."""
    with session() as db:
        for row in db.exec(select(MyAd).where(col(MyAd.kleinanzeigen).is_not(None))):
            ka = dict(row.kleinanzeigen or {})
            if not ka.get("pending"):
                continue
            if ka["pending"] == "submitting":
                ka.update(state="check", error=t(SUBMIT_UNCERTAIN))
            else:
                ka["error"] = t("Interrupted by a restart – please try again")
            ka["pending"] = None
            row.kleinanzeigen = ka
            db.add(row)
        db.commit()
