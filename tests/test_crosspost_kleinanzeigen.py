"""An own willhaben ad posted on Kleinanzeigen (services/crosspost_kleinanzeigen.py): the source (a published draft or
willhaben's ad), the category (kept when Claude says it fits, else walked), the submit marker and the uncertain
posting, the sync with willhaben's sale state, the routes and the pages. The browser is faked (the listing module's
Upload and OwnAds); nothing here has run against the live site."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import AdDraft, MyAd, get_setting, session, set_setting
from app.llm.base import LLMResult
from app.platforms import kleinanzeigen_listing as kl
from app.services import chats, crosspost_kleinanzeigen as ka, jobs, my_ads
from app.willhaben import insert
from sqlmodel import select

from app.services import kleinanzeigen_price
from tests.fake_kleinanzeigen import AMOUNTS, FakeClaude, disable, enable

ROOT = ["Elektronik", "Haus & Garten"]
NEXT = {"Elektronik": ["Computer", "Handys"], "Computer": ["Laptops", "Desktop-PCs"], "Laptops": None,
        "Desktop-PCs": None, "Handys": None, "Haus & Garten": None}


class FakeUpload:
    """The posting form: records what the service sets; `mode` decides how the final click goes."""
    detected: list = []
    mode = "ok"
    new_id = "7001"
    calls: list = []
    chosen: list = []
    applied: list = []
    submitted: list = []
    seen_pending: list = []
    photo_count: list = []

    def __init__(self):
        self.page = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def describe(self, text):
        FakeUpload.calls.append(("describe", text))

    async def detected_category(self):
        return list(FakeUpload.detected) if FakeUpload.detected else None

    async def open_category_dialog(self):
        FakeUpload.calls.append(("open",))
        return list(ROOT)

    async def choose_category(self, label):
        FakeUpload.chosen.append(label)
        return NEXT[label]

    async def apply_category(self):
        FakeUpload.applied.append(True)

    async def set_condition(self, code):
        FakeUpload.calls.append(("condition", code))
        return True

    async def set_price(self, price_type, amount):
        FakeUpload.calls.append(("price", price_type, amount))

    async def set_shipping(self, possible):
        FakeUpload.calls.append(("shipping", possible))
        return True

    async def set_zip(self, zip_code):
        FakeUpload.calls.append(("zip", zip_code))

    async def photos(self, files):
        FakeUpload.photo_count.append(len(files))
        return len(files)

    async def submit(self, title, *, on_submitting=None):
        FakeUpload.submitted.append(title)
        if FakeUpload.mode == "captcha":            # as the module: before the marker, nothing was clicked
            raise kl.Captcha("captcha")
        if FakeUpload.mode == "early":              # an error before the marker: nothing was clicked either
            raise RuntimeError("no submit button")
        if on_submitting is not None:
            on_submitting()
        with session() as db:                       # the marker is on the ad before the click
            FakeUpload.seen_pending.append((db.get(MyAd, "601").kleinanzeigen or {}).get("pending"))
        if FakeUpload.mode == "uncertain":
            raise kl.Uncertain("no confirmation")
        if FakeUpload.mode == "boom":               # an unexpected error after the click
            raise RuntimeError("Target page, context or browser has been closed")
        if FakeUpload.mode == "rejected":           # the form is still on screen: nothing was posted
            raise kl.ListingError("Kleinanzeigen rejected the ad: Titel zu kurz")
        if FakeUpload.mode == "cancelled":          # the job's timeout or a cancel while the page redirects
            raise asyncio.CancelledError()
        return FakeUpload.new_id


class FakeOwnAds:
    listed: list = []
    done: list = []
    fail_reserve = False

    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list(self):
        return [dict(a) for a in FakeOwnAds.listed]

    async def reserve(self, ad_id):
        if FakeOwnAds.fail_reserve:
            raise kl.ListingError("ad has no reserve button")
        FakeOwnAds.done.append(("reserve", ad_id))

    async def activate(self, ad_id):
        FakeOwnAds.done.append(("activate", ad_id))

    async def delete(self, ad_id):
        FakeOwnAds.done.append(("delete", ad_id))
        return True


class FakeBackend:
    answers: list = []
    prompts: list = []

    async def complete(self, *, system, prompt, schema, images=None):
        FakeBackend.prompts.append(prompt)
        return LLMResult(data={"index": FakeBackend.answers.pop(0), "reason": ""}, model="fake")


def _setup(monkeypatch, tmp_path, *, draft=True, price=120, copied=None):
    """One own ad (601). With `draft` a published willhaben draft is its source; without, copy_plan is."""
    with session() as db:
        db.add(MyAd(ad_id="601", heading="Lenovo ThinkPad T14 i5", status="active", parcel={"weight_kg": 2}))
        db.commit()
    img = tmp_path / "01.jpg"
    img.write_bytes(b"\xff\xd8x")
    if draft:
        with session() as db:
            db.add(AdDraft(input_text="notes", title="Lenovo ThinkPad T14 i5", description="i5, 16 GB, 14 Zoll",
                           price_suggestion=price, condition="Gebraucht", image_paths=[str(img)], wh_ad_id="601",
                           status="published", wh_plan={"shipping": {"handover": "both"}}))
            db.commit()

    async def plan(s, ad_id):
        if copied is not None:
            copied()
        return {"ad": {"heading": "Lenovo ThinkPad T14 i5", "description": "<p>i5, 16 GB</p>", "price": price,
                       "treeAttributes": [{"attributeNode": {"label": "Zustand"},
                                           "selectedAttributeValueNodes": [{"label": "Neu"}]}]},
                "photos": ["https://cache.willhaben.at/mmo/a.jpg"], "shipping": {"handover": "shipping"}}

    async def download(url, folder, name, **kw):
        p = tmp_path / f"{name}.jpg"
        p.write_bytes(b"\xff\xd8x")
        return p

    monkeypatch.setattr(insert, "copy_plan", plan)
    monkeypatch.setattr(insert, "description_text", lambda html: html.replace("<p>", "").replace("</p>", ""))
    from app.services import ad_images
    monkeypatch.setattr(ad_images, "download", download)
    monkeypatch.setattr(ka, "get_backend", lambda purpose=None: FakeBackend())
    monkeypatch.setattr(kl, "Upload", FakeUpload)
    monkeypatch.setattr(kl, "OwnAds", FakeOwnAds)
    FakeUpload.detected, FakeUpload.mode, FakeUpload.calls, FakeUpload.chosen = [], "ok", [], []
    FakeUpload.applied, FakeUpload.submitted, FakeUpload.seen_pending, FakeUpload.photo_count = [], [], [], []
    FakeBackend.answers, FakeBackend.prompts = [], []
    FakeOwnAds.listed, FakeOwnAds.done, FakeOwnAds.fail_reserve = [], [], False
    return img


def _ka(ad_id="601"):
    with session() as db:
        return dict(db.get(MyAd, ad_id).kleinanzeigen or {})


def _set_ka(ad_id="601", **fields):
    with session() as db:
        row = db.get(MyAd, ad_id)
        row.kleinanzeigen = {**(row.kleinanzeigen or {}), **fields}
        db.add(row)
        db.commit()


async def test_the_published_draft_is_the_source(monkeypatch, tmp_path):
    asked = []
    _setup(monkeypatch, tmp_path, copied=lambda: asked.append(1))
    FakeUpload.detected = ["Elektronik", "Computer", "Laptops"]
    FakeBackend.answers = [0]                                         # the detected category fits
    msg = await ka.to_kleinanzeigen("601")
    assert asked == []                                                 # copy_plan never read: the draft is the source
    assert FakeUpload.submitted == ["Lenovo ThinkPad T14 i5"]
    assert ("price", "FIXED", 120.0) in FakeUpload.calls and ("condition", "ok") in FakeUpload.calls   # "Gebraucht"
    assert ("shipping", True) in FakeUpload.calls and FakeUpload.photo_count == [1]
    assert FakeUpload.chosen == [] and FakeUpload.applied == []        # kept as the site detected it
    assert _ka()["id"] == "7001" and _ka()["state"] == "active" and _ka()["category"] == "Elektronik > Computer > Laptops"
    assert _ka()["pending"] is None and _ka()["url"].endswith("/s-anzeige/7001")
    assert "7001" not in msg and "Lenovo" in msg


async def test_without_a_draft_willhabens_ad_is_read_and_its_photos_downloaded(monkeypatch, tmp_path):
    asked = []
    _setup(monkeypatch, tmp_path, draft=False, copied=lambda: asked.append(1))
    set_setting("kleinanzeigen_zip", "1010")
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    await ka.to_kleinanzeigen("601")
    assert asked == [1]
    assert ("condition", "new") in FakeUpload.calls and ("shipping", True) in FakeUpload.calls
    assert ("zip", "1010") in FakeUpload.calls and FakeUpload.photo_count == [1]


async def test_a_category_that_does_not_fit_is_walked_level_by_level(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Haus & Garten"]
    FakeBackend.answers = [1, 0, 0, 0]                    # not a fit, then Elektronik > Computer > Laptops (a leaf)
    await ka.to_kleinanzeigen("601")
    assert FakeUpload.chosen == ["Elektronik", "Computer", "Laptops"] and FakeUpload.applied == [True]
    assert "Haus & Garten" in FakeBackend.prompts[0] and "Ebene" in FakeBackend.prompts[1]
    assert _ka()["category"] == "Elektronik > Computer > Laptops"


async def test_no_fitting_category_posts_nothing_and_says_so(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Haus & Garten"]
    FakeBackend.answers = [1, -1]                         # not a fit; no label of the first level fits
    with pytest.raises(kl.ListingError, match="no fitting Kleinanzeigen category"):
        await ka.to_kleinanzeigen("601")
    assert FakeUpload.submitted == []


async def test_a_price_is_needed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, price=None)
    with pytest.raises(kl.ListingError, match="no price"):
        await ka.to_kleinanzeigen("601")
    assert FakeUpload.submitted == []


async def test_a_free_ad_is_a_giveaway(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, price=0)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    await ka.to_kleinanzeigen("601")
    assert ("price", "GIVE_AWAY", None) in FakeUpload.calls


async def test_the_marker_is_set_before_the_click_and_an_uncertain_posting_is_checked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "uncertain"
    msg = await ka.to_kleinanzeigen("601")
    assert FakeUpload.seen_pending == ["submitting"]
    row = _ka()
    assert row["state"] == "check" and row["pending"] is None and row["title"] == "Lenovo ThinkPad T14 i5"
    assert not row.get("id") and "no confirmation" in msg


async def test_an_unexpected_error_after_the_marker_leaves_a_check(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "boom"
    msg = await ka.to_kleinanzeigen("601")
    assert FakeUpload.seen_pending == ["submitting"]
    row = _ka()
    assert row["state"] == "check" and row["pending"] is None and "no confirmation" in msg
    assert not row.get("id")


async def test_a_rejected_form_after_the_marker_is_a_plain_failure_not_a_check(monkeypatch, tmp_path):
    """The listing module raises a plain ListingError after the click only when the form is still on screen (nothing
    posted): the ad may be listed again at once instead of waiting for a Forget."""
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "rejected"
    with pytest.raises(kl.ListingError, match="rejected"):
        await ka.to_kleinanzeigen("601")
    assert FakeUpload.seen_pending == ["submitting"]
    assert _ka().get("state") is None and not _ka().get("id")


async def test_a_cancel_after_the_marker_leaves_a_check_and_goes_on(monkeypatch, tmp_path):
    """The job's timeout cancels the coroutine mid-redirect: the record must say "check" (the ad may be live) before
    the cancel travels on, or a second List would post the ad again."""
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await ka.to_kleinanzeigen("601")
    row = _ka()
    assert row["state"] == "check" and row["pending"] is None and not row.get("id")


async def test_an_error_before_the_marker_is_a_plain_failure(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "early"
    with pytest.raises(RuntimeError, match="no submit button"):
        await ka.to_kleinanzeigen("601")
    assert _ka().get("state") is None and FakeUpload.seen_pending == []


async def test_a_captcha_posts_nothing_and_the_job_says_so(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    FakeUpload.mode = "captcha"
    run = _capture_job(monkeypatch)
    assert ka.enqueue("601") is True
    assert _ka()["pending"] == "listing"
    with pytest.raises(kl.Captcha):
        await run(_Job())
    assert _ka()["pending"] is None and "captcha" in _ka()["error"]


class _Job:
    def progress(self, *a, **kw):
        pass


def _capture_job(monkeypatch):
    holder = {}

    def submit(kind, label, run, **kw):
        holder.update(run=run, kw=kw, kind=kind)
        return SimpleNamespace(task=None)
    monkeypatch.setattr(jobs, "submit", submit)
    return lambda job: holder["run"](job)


async def test_a_logged_out_account_says_where_to_log_in(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    run = _capture_job(monkeypatch)
    ka.enqueue("601")

    class LoggedOutUpload(FakeUpload):
        async def __aenter__(self):
            raise kl.LoggedOut("not logged in to Kleinanzeigen")
    monkeypatch.setattr(kl, "Upload", LoggedOutUpload)
    with pytest.raises(kl.LoggedOut):
        await run(_Job())
    assert "Settings → Platforms → Kleinanzeigen" in _ka()["error"] and _ka()["pending"] is None
    from app.services import site_accounts
    assert site_accounts.state("kleinanzeigen")["logged_in"] is False


async def test_enqueue_refuses_a_second_listing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _capture_job(monkeypatch)
    _set_ka(id="7001", state="active")
    assert ka.enqueue("601") is False                                 # already listed
    _set_ka(id=None, state=None, pending="listing")
    monkeypatch.setattr(jobs, "current_keys", lambda prefix: ["ka:601"] if prefix == "ka:" else [])
    assert ka.enqueue("601") is False                                 # on its way
    assert ka.enqueue("vinted-42") is False and ka.enqueue("999") is False
    _set_ka(pending=None, state="check", title="x")
    assert ka.enqueue("601") is False                                 # waits for a check, never posted again


async def test_sync_reserves_when_sold_and_activates_again(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active", url="https://www.kleinanzeigen.de/s-anzeige/9001")
    with session() as db:
        db.get(MyAd, "601").status = "sold"
        db.commit()
    FakeOwnAds.listed = [{"id": "9001", "state": "active", "title": "x"}]
    assert (await ka.sync())["reserved"] == 1
    assert FakeOwnAds.done == [("reserve", "9001")]
    assert _ka()["state"] == "reserved" and _ka()["hidden_by"] == "app"
    FakeOwnAds.listed = [{"id": "9001", "state": "reserved", "title": "x"}]
    assert await ka.sync() == {}                                        # nothing changes twice
    with session() as db:
        db.get(MyAd, "601").status = "active"
        db.commit()
    FakeOwnAds.listed = [{"id": "9001", "state": "reserved", "title": "x"}]
    assert (await ka.sync())["activated"] == 1
    assert FakeOwnAds.done[-1] == ("activate", "9001") and _ka()["state"] == "active" and _ka()["hidden_by"] is None


async def test_an_owner_reservation_is_kept_and_an_unlisted_copy_is_gone(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    FakeOwnAds.listed = [{"id": "9001", "state": "reserved", "title": "x"}]
    await ka.sync()
    assert _ka()["state"] == "reserved" and _ka()["hidden_by"] is None and FakeOwnAds.done == []
    FakeOwnAds.listed = [{"id": "9002", "state": "active", "title": "other"}]   # the list answers, 9001 is not in it
    assert await ka.sync() == {} and _ka()["state"] == "reserved" and _ka()["missing"] == 1
    assert (await ka.sync())["gone"] == 1                                      # missing on a second sync in a row
    assert _ka()["state"] == "gone" and _ka()["missing"] == 0


async def test_an_empty_own_ads_list_marks_nothing_gone(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    FakeOwnAds.listed = []                                                     # no answer, not "no ads"
    assert await ka.sync() == {} and await ka.sync() == {}
    assert _ka()["state"] == "active" and not _ka().get("missing")


async def test_a_missing_ad_seen_again_starts_over(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    FakeOwnAds.listed = [{"id": "9002", "state": "active", "title": "other"}]
    await ka.sync()
    assert _ka()["missing"] == 1
    FakeOwnAds.listed = [{"id": "9001", "state": "active", "title": "x"}, {"id": "9002", "state": "active", "title": "y"}]
    await ka.sync()
    assert _ka()["missing"] == 0 and _ka()["state"] == "active"
    FakeOwnAds.listed = [{"id": "9002", "state": "active", "title": "y"}]
    await ka.sync()
    assert _ka()["state"] == "active" and _ka()["missing"] == 1


async def test_a_check_is_linked_when_its_title_is_online(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(state="check", title="Lenovo ThinkPad T14 i5", pending=None)
    FakeOwnAds.listed = [{"id": "9100", "state": "active", "title": "lenovo thinkpad T14 i5"}]
    assert (await ka.sync())["linked"] == 1
    assert _ka()["id"] == "9100" and _ka()["state"] == "active" and _ka()["title"] is None


def _second_ad_linked(ad_id="602", kid="9100"):
    with session() as db:
        db.add(MyAd(ad_id=ad_id, heading="Lenovo ThinkPad T14 i5", status="active",
                    kleinanzeigen={"id": kid, "state": "active"}))
        db.commit()


async def test_a_check_is_not_linked_to_an_ad_another_row_has(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _second_ad_linked()
    _set_ka(state="check", title="Lenovo ThinkPad T14 i5", pending=None)
    FakeOwnAds.listed = [{"id": "9100", "state": "active", "title": "Lenovo ThinkPad T14 i5"}]
    assert await ka.sync() == {}
    row = _ka()
    assert row["state"] == "check" and not row.get("id") and "1 own ads" in row["error"]


async def test_a_check_with_several_candidates_is_left_for_the_owner(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(state="check", title="Lenovo ThinkPad T14 i5", pending=None)
    FakeOwnAds.listed = [{"id": "9100", "state": "active", "title": "Lenovo ThinkPad T14 i5"},
                         {"id": "9101", "state": "active", "title": "lenovo thinkpad t14 i5"}]
    assert await ka.sync() == {}
    assert _ka()["state"] == "check" and not _ka().get("id") and "2 own ads" in _ka()["error"]


async def test_a_deleted_willhaben_ad_with_a_live_copy_is_reserved_there(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    with session() as db:
        db.get(MyAd, "601").status = "deleted"
        db.commit()
    FakeOwnAds.listed = [{"id": "9001", "state": "active", "title": "x"}]
    assert (await ka.sync())["reserved"] == 1
    assert FakeOwnAds.done == [("reserve", "9001")] and _ka()["state"] == "reserved"


def test_a_willhaben_ad_gone_keeps_its_row_only_with_a_live_copy(monkeypatch, tmp_path):
    from app.services import my_ads
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    my_ads.remove("601")
    with session() as db:
        row = db.get(MyAd, "601")
        assert row is not None and row.status == "deleted" and row.kleinanzeigen["id"] == "9001"
    with session() as db:
        db.add(MyAd(ad_id="602", heading="ohne Kopie", status="active"))
        db.commit()
    my_ads.remove("602")
    with session() as db:
        assert db.get(MyAd, "602") is None


def test_a_deleted_ad_with_a_copy_is_listed_in_my_ads_with_its_copy_buttons(monkeypatch, tmp_path):
    from app.main import app
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active", url="https://www.kleinanzeigen.de/s-anzeige/9001")
    from app.services import my_ads
    my_ads.remove("601")
    monkeypatch.setattr(ka, "available", lambda: True)
    with TestClient(app) as c:
        page = c.get("/myads?ad=601").text
    assert 'data-listing="kleinanzeigen"' in page and "/myads/601/kleinanzeigen/reserve" in page
    assert "/myads/601/delete" not in page                                     # no willhaben actions on a gone ad


async def test_a_sync_with_no_linked_ad_opens_nothing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(kl, "OwnAds", lambda: pytest.fail("opened"))
    assert await ka.sync() == {}
    ka.schedule_sync("601")                                               # no copy: a no-op, no job
    assert await ka.scheduled_sync() is None


async def test_a_restart_turns_a_submit_that_may_have_gone_through_into_a_check(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(pending="submitting", title="Lenovo ThinkPad T14 i5")
    ka.recover()
    assert _ka()["state"] == "check" and _ka()["pending"] is None and _ka()["title"] == "Lenovo ThinkPad T14 i5"


async def test_reserve_and_delete_from_the_ad(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    run = _capture_job(monkeypatch)
    assert ka.enqueue_reserve("601") is True and _ka()["pending"] == "reserving"
    await run(_Job())
    assert FakeOwnAds.done == [("reserve", "9001")] and _ka()["state"] == "reserved" and _ka()["pending"] is None
    assert ka.enqueue_delete("601") is True
    await run(_Job())
    assert ("delete", "9001") in FakeOwnAds.done and _ka() == {}     # the link goes with the ad on Kleinanzeigen


async def test_a_failed_reserve_says_why(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active")
    run = _capture_job(monkeypatch)
    FakeOwnAds.fail_reserve = True
    ka.enqueue_reserve("601")
    with pytest.raises(kl.ListingError):
        await run(_Job())
    assert "has no reserve button" in _ka()["error"] and _ka()["pending"] is None and _ka()["state"] == "active"


def test_by_kleinanzeigen_id_and_the_chat_about_it(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_ka(id="777")
    assert my_ads.by_kleinanzeigen_id("777").ad_id == "601"
    assert my_ads.by_kleinanzeigen_id("778") is None
    assert chats.own_ad(SimpleNamespace(ad_id="777", platform="kleinanzeigen")).ad_id == "601"
    assert chats.own_ad(SimpleNamespace(ad_id="601", platform=None)).ad_id == "601"   # willhaben ids unchanged
    assert chats.own_ad(SimpleNamespace(ad_id="777", platform=None)) is None         # never a willhaben id match


def test_routes_queue_the_jobs(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from app.main import app
    queued, bulk = [], []
    monkeypatch.setattr(ka, "enqueue", lambda ad_id: queued.append(ad_id) or True)
    monkeypatch.setattr(ka, "enqueue_reserve", lambda ad_id: queued.append(("reserve", ad_id)) or True)
    monkeypatch.setattr(ka, "enqueue_delete", lambda ad_id: queued.append(("delete", ad_id)) or True)
    monkeypatch.setattr(ka, "enqueue_many", lambda ids: bulk.append(list(ids)) or len(ids))
    with TestClient(app) as c:
        assert c.post("/myads/601/kleinanzeigen").status_code == 200
        c.post("/myads/601/kleinanzeigen/reserve")
        c.post("/myads/601/kleinanzeigen/delete")
        assert c.post("/myads/601/kleinanzeigen/unknown").status_code == 404
        c.post("/myads/bulk/kleinanzeigen", data={"ids": ["601"]})
    assert queued == ["601", ("reserve", "601"), ("delete", "601")] and bulk == [["601"]]


def test_the_my_ads_page_offers_the_copy_only_when_the_account_can_post(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from app.main import app
    with TestClient(app) as c:
        monkeypatch.setattr(ka, "available", lambda: False)
        page = c.get("/myads?ad=601").text
        assert 'data-listing="kleinanzeigen"' not in page and "/myads/bulk/kleinanzeigen" not in page
        monkeypatch.setattr(ka, "available", lambda: True)
        page = c.get("/myads?ad=601").text
        assert 'action="/myads/601/kleinanzeigen" hx-post' in page and "/myads/bulk/kleinanzeigen" in page
        _set_ka(id="9001", state="active", url="https://www.kleinanzeigen.de/s-anzeige/9001")
        page = c.get("/myads?ad=601").text
        assert 'action="/myads/601/kleinanzeigen/reserve"' in page and 'data-row-site="kleinanzeigen"' in page
        monkeypatch.setattr(ka, "available", lambda: False)
        page = c.get("/myads?ad=601").text
        assert 'data-listing="kleinanzeigen"' in page and "/kleinanzeigen/reserve" not in page   # state, no buttons
        _set_ka(pending="submitting")
        assert 'data-pending-key="ka:601"' in c.get("/myads?ad=601").text


def test_the_settings_save_the_posting_card_and_keep_the_switch(monkeypatch, tmp_path):
    from app.main import app
    set_setting("platform_kleinanzeigen_enabled", "1")
    with TestClient(app) as c:
        page = c.get("/settings/kleinanzeigen").text
        assert 'id="s-ka-post-default"' in page and 'id="s-ka-zip"' in page
        c.post("/settings/platform/kleinanzeigen", data={"part": "posting", "post_default": "on", "zip": "1010"})
        assert get_setting("kleinanzeigen_post_default") == "1"
        assert get_setting("platform_kleinanzeigen_enabled") == "1"       # the posting card leaves the switch alone
        c.post("/settings/platform/kleinanzeigen", data={"part": "posting", "zip": "12a"})
        assert get_setting("kleinanzeigen_zip") == "1010" and get_setting("kleinanzeigen_post_default") == "1"  # refused whole
        c.post("/settings/platform/kleinanzeigen", data={"part": "posting", "zip": ""})
        assert get_setting("kleinanzeigen_zip") == ""


def test_forget_drops_a_check_and_lets_the_ad_be_listed_again(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _capture_job(monkeypatch)
    assert ka.forget("601") is False                                  # nothing to forget
    _set_ka(state="check", title="Lenovo ThinkPad T14 i5", pending=None)
    assert ka.enqueue("601") is False                                 # a check is never posted again on its own
    assert ka.forget("601") is True and _ka() == {}
    assert ka.enqueue("601") is True                                  # after the forget the ad is listed again
    assert _ka()["pending"] == "listing"


def test_the_forget_route_and_its_button(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from app.main import app
    _set_ka(state="check", title="Lenovo ThinkPad T14 i5", pending=None)
    monkeypatch.setattr(ka, "available", lambda: True)
    with TestClient(app) as c:
        page = c.get("/myads?ad=601").text
        assert 'action="/myads/601/kleinanzeigen/forget"' in page and "Drops the record here only" in page
        c.post("/myads/601/kleinanzeigen/forget")
        assert _ka() == {}
        assert c.post("/myads/601/kleinanzeigen/forget").status_code in (200, 303)   # nothing left: said, not an error
        assert 'action="/myads/601/kleinanzeigen/forget"' not in c.get("/myads?ad=601").text


def test_the_my_ads_detail_keeps_both_pending_keys(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from app.main import app
    monkeypatch.setattr(ka, "available", lambda: True)
    with TestClient(app) as c:                       # the app's start clears markers without a job: set them after it
        with session() as db:
            row = db.get(MyAd, "601")
            row.pending_action = "deactivate"
            db.add(row)
            db.commit()
        _set_ka(pending="listing")
        assert 'data-pending-key="ad:601 ka:601"' in c.get("/myads?ad=601").text
        _set_ka(pending=None)
        assert 'data-pending-key="ad:601"' in c.get("/myads?ad=601").text


# ------------------------------------------------------------------ the price on Kleinanzeigen
@pytest.fixture
def ka_on(monkeypatch):
    yield lambda amounts=AMOUNTS: enable(monkeypatch, amounts)
    disable()


def _draft_set(**fields):
    with session() as db:
        d = db.exec(select(AdDraft).where(AdDraft.wh_ad_id == "601")).one()
        for k, v in fields.items():
            setattr(d, k, v)
        db.add(d)
        db.commit()


async def test_the_posted_price_is_the_typed_one_then_the_analysis_then_willhabens(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, price=120)
    plan = await ka._source("601", ka._row("601"), tmp_path, lambda s: None)
    assert plan.price == 120 and plan.price_note is None and plan.wh_price == 120
    _draft_set(kleinanzeigen={"price": 95, "note": "Am Markt tiefer."})
    plan = await ka._source("601", ka._row("601"), tmp_path, lambda s: None)
    assert plan.price == 95 and plan.price_note == "Am Markt tiefer." and plan.wh_price == 120
    _draft_set(also={"kleinanzeigen": True, "kleinanzeigen_price": 77.0})
    plan = await ka._source("601", ka._row("601"), tmp_path, lambda s: None)
    assert plan.price == 77 and plan.price_note is None and plan.wh_price == 120


async def test_a_copy_without_a_draft_is_posted_at_the_analysed_price_and_it_is_kept(monkeypatch, tmp_path, ka_on):
    _setup(monkeypatch, tmp_path, draft=False, price=120)
    ka_on()
    backend = FakeClaude({"price": 150, "note": "Hier teurer."})
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: backend)
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    await ka.to_kleinanzeigen("601")
    assert ("price", "FIXED", 150.0) in FakeUpload.calls
    assert _ka()["price"] == 150 and _ka()["price_note"] == "Hier teurer." and _ka()["wh_price"] == 120
    assert "Lenovo ThinkPad" in backend.prompts[0]                              # the heading is what is compared


async def test_without_comparables_the_willhaben_price_is_posted(monkeypatch, tmp_path, ka_on):
    _setup(monkeypatch, tmp_path, draft=False, price=120)
    ka_on([60, 90])
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: FakeClaude({"price": 80, "note": "x"}))
    FakeUpload.detected = ["Elektronik"]
    FakeBackend.answers = [0]
    await ka.to_kleinanzeigen("601")
    assert ("price", "FIXED", 120.0) in FakeUpload.calls and _ka()["price"] == 120 and _ka().get("price_note") is None


def test_my_ads_shows_the_price_there_with_the_difference_to_willhaben(monkeypatch, tmp_path):
    from app.main import app
    _setup(monkeypatch, tmp_path)
    _set_ka(id="9001", state="active", url="https://www.kleinanzeigen.de/s-anzeige/9001", price=130.0,
            price_note="Hohe Nachfrage", wh_price=120.0)
    monkeypatch.setattr(ka, "available", lambda: True)
    with TestClient(app) as c:
        page = c.get("/myads?ad=601").text
    assert "data-ka-price" in page and "Price there: € 130" in page and "8 % above willhaben" in page
    assert 'title="Hohe Nachfrage"' in page


async def test_a_free_willhaben_ad_stays_free_on_kleinanzeigen(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, price=0)
    _draft_set(kleinanzeigen={"price": 90, "note": "x"})
    plan = await ka._source("601", ka._row("601"), tmp_path, lambda s: None)
    assert plan.price is None and plan.price_type == "GIVE_AWAY"     # a giveaway carries no amount
