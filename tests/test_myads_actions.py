"""Deactivate / delete own ads on willhaben via the ad's own action links, and hide-only."""
import copy
import re
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import MyAd, session
from app.services import my_ads
from app.willhaben.account import EndpointUnknown
from app.willhaben.session import ApiResponse
from tests.conftest import finish_jobs, wait_jobs
from tests.test_my_ads import NEXT


def _links(ad_id):
    return {"contextLink": [
        {"id": "2", "description": "Deaktivieren", "relativePath": f"/bap/deactivate/{ad_id}", "serviceName": "myatz"},
        {"id": "3", "description": "Löschen", "relativePath": f"/bap/{ad_id}", "serviceName": "myatz"}]}


class _Account:
    """Stateful fake of the logged-in browser: the list page plus willhaben's PUT/DELETE action links."""

    def __init__(self, delete_works=True):
        self.page = copy.deepcopy(NEXT)
        self.calls, self.delete_works = [], delete_works
        for ad in self._ads():
            self._relink(ad)

    @staticmethod
    def _relink(ad):
        if ad["advertStatus"]["id"] == "active":
            ad["advertEditStatusActionsList"] = {"contextLinkList": _links(ad["id"])}
        else:   # deactivated: willhaben offers "Aktivieren" (and delete)
            ad["advertEditStatusActionsList"] = {"contextLinkList": {"contextLink": [
                {"id": "4", "description": "Aktivieren", "relativePath": f"/bap/activate/{ad['id']}", "serviceName": "myatz"},
                {"id": "3", "description": "Löschen", "relativePath": f"/bap/{ad['id']}", "serviceName": "myatz"}]}}

    async def api_json(self, method, path, body=None):
        r = await self.api(method, path, body)
        if not r.ok:
            raise RuntimeError(f"{method} {path}: HTTP {r.status}")
        return r.json()

    def _ads(self):
        return self.page["props"]["pageProps"]["initialUserAdsList"]["verticalList"][1]["advertDetails"]

    async def api(self, method, path, body=None, accept="application/json"):
        self.calls.append((method, path))
        if method == "GET":
            html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
            return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)
        ad_id = path.rsplit("/", 1)[1]
        ads = self._ads()
        if method == "PUT" and path == f"/webapi/myatz/bap/deactivate/{ad_id}":
            for ad in ads:
                if ad["id"] == ad_id:
                    ad["advertStatus"] = {"id": "deactivated", "description": "Deaktiviert"}
                    self._relink(ad)
            return ApiResponse(status=200, text="", url=path)
        if method == "PUT" and path == f"/webapi/myatz/bap/activate/{ad_id}":
            for ad in ads:
                if ad["id"] == ad_id:
                    ad["advertStatus"] = {"id": "active", "description": "Aktiv"}
                    self._relink(ad)
            return ApiResponse(status=200, text="", url=path)
        if method == "DELETE" and path == f"/webapi/myatz/bap/{ad_id}":
            if self.delete_works:
                ads[:] = [a for a in ads if a["id"] != ad_id]
            return ApiResponse(status=204, text="", url=path)
        return ApiResponse(status=404, text="unexpected", url=path)


@pytest.fixture
def account(monkeypatch):
    acct = _Account()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    return acct


async def test_deactivate_on_willhaben(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    row = await my_ads.deactivate("1000000001")
    assert ("PUT", "/webapi/myatz/bap/deactivate/1000000001") in account.calls
    assert row.status == "Deaktiviert" and row.auto_renew is False and "deactivated" in row.last_action


async def test_delete_on_willhaben_removes_row(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    await my_ads.delete("1000000001")
    assert ("DELETE", "/webapi/myatz/bap/1000000001") in account.calls
    with session() as db:
        assert db.get(MyAd, "1000000001") is None                   # never seen as "expired" by auto-renew
    await my_ads.import_from_account()
    with session() as db:
        assert db.get(MyAd, "1000000001") is None                   # and not re-imported


async def test_delete_not_confirmed_keeps_row(monkeypatch):
    acct = _Account(delete_works=False)
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    await my_ads.import_from_account()
    with pytest.raises(EndpointUnknown, match="still lists"):
        await my_ads.delete("1000000001")
    with session() as db:
        assert db.get(MyAd, "1000000001") is not None


async def test_no_link_no_action(account):
    await my_ads.import_from_account()
    with pytest.raises(EndpointUnknown, match="offers no"):
        await my_ads.deactivate("1000000002")                        # already deactivated: no such link
    assert not any(m in ("PUT", "DELETE") for m, _ in account.calls)


async def test_bulk_deactivate_activate_delete(account):
    await my_ads.import_from_account()
    my_ads.set_auto_renew("1000000001", True)
    res = await my_ads.bulk("deactivate", ["1000000001", "1000000002"])
    assert res["done"] == ["Anzeige 1000000001"] and res["failed"][0][0] == "Anzeige 1000000002"
    with session() as db:
        assert db.get(MyAd, "1000000001").status == "Deaktiviert" and db.get(MyAd, "1000000001").auto_renew is False
    res = await my_ads.bulk("activate", ["1000000001", "1000000002"])
    assert sorted(res["done"]) == ["Anzeige 1000000001", "Anzeige 1000000002"] and not res["failed"]
    with session() as db:
        assert db.get(MyAd, "1000000002").status == "active"
    lists_before = sum(1 for m, p in account.calls if m == "GET")
    res = await my_ads.bulk("delete", ["1000000001", "1000000002"])
    assert len(res["done"]) == 2 and not res["failed"]
    assert sum(1 for m, p in account.calls if m == "GET") - lists_before == 2   # one list before, one to confirm
    with session() as db:
        assert db.exec(select(MyAd)).first() is None


def _make_passive(acct, ad_id):
    """Like a real "Passiv" ad (live 2026-10-09): only Neu veröffentlichen, Rechnung anfordern and Löschen."""
    for ad in acct._ads():
        if ad["id"] == ad_id:
            ad["advertStatus"] = {"id": "passive", "description": "Passiv", "statusId": 60}
            ad["advertEditStatusActionsList"] = {"contextLinkList": {"contextLink": [
                {"id": "6", "description": "Neu veröffentlichen", "relativePath": f"/bap/copy/{ad_id}", "serviceName": "iad"},
                {"id": "requestBillLink", "description": "Rechnung anfordern", "relativePath": f"/payment/all/{ad_id}",
                 "serviceName": "iad"},
                {"id": "3", "description": "Löschen", "relativePath": f"/bap/{ad_id}", "serviceName": "myatz"}]}}


async def test_bulk_activate_republishes_passive_ads(account, monkeypatch):
    from app.willhaben import insert

    _make_passive(account, "1000000002")
    await my_ads.import_from_account()
    with session() as db:
        row = db.get(MyAd, "1000000002")
        row.auto_reply, row.auto_reply_instructions = True, "Preis fix"
        db.add(row)
        db.commit()

    async def get_own_ad(s, ad_id):
        return {"adId": ad_id}

    async def publish(s, base, photos, existing_ad_id=None):
        account._ads().append({**account._ads()[0], "id": existing_ad_id, "description": "Neu",
                               "advertStatus": {"id": "active", "description": "Aktiv"}})
        return {"ad_id": existing_ad_id}

    real_api = account.api

    async def api(method, path, body=None, accept="application/json"):
        if method == "POST" and path == "/webapi/iad/bap/copy/1000000002":
            account.calls.append((method, path))
            return ApiResponse(status=200, text='{"adId": 1000000009}', url=path)
        return await real_api(method, path, body, accept)

    monkeypatch.setattr(account, "api", api)
    monkeypatch.setattr(insert, "get_own_ad", get_own_ad)
    monkeypatch.setattr(insert, "publish", publish)
    res = await my_ads.bulk("activate", ["1000000002"])
    assert res["done"] == res["republished"] == ["Anzeige 1000000002"] and not res["failed"]
    with session() as db:
        old, new = db.get(MyAd, "1000000002"), db.get(MyAd, "1000000009")
    assert old.replaced_by == "1000000009" and new.status == "active"
    assert (new.auto_reply, new.auto_reply_instructions) == (True, "Preis fix")   # settings carried over


async def test_bulk_activate_never_republishes_sold_ads(account):
    _make_passive(account, "1000000002")
    await my_ads.import_from_account()
    with session() as db:
        row = db.get(MyAd, "1000000002")
        row.sale_status = "sold"
        db.add(row)
        db.commit()
    res = await my_ads.bulk("activate", ["1000000002"])
    assert not res["done"] and res["failed"][0][0] == "Anzeige 1000000002"
    assert not any(m == "POST" for m, _ in account.calls)


async def test_bulk_route_keeps_list_filters(account):
    from app.main import app

    await my_ads.import_from_account()
    with TestClient(app) as c:
        r = c.post("/myads/bulk/activate", data={"ids": ["1000000002"], "show_inactive": "1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("&show_inactive=1")


async def test_bulk_route_queues_and_returns_at_once(account, monkeypatch):
    import asyncio

    from app.main import app

    await my_ads.import_from_account()
    gate = asyncio.Event()
    real = account.api

    async def slow_api(method, path, body=None, accept="application/json"):
        if method == "DELETE":
            await gate.wait()       # willhaben is "slow": the page must not wait for it
        return await real(method, path, body, accept)

    monkeypatch.setattr(account, "api", slow_api)
    with TestClient(app) as c:
        r = c.post("/myads/bulk/delete", data={"ids": ["1000000001"]}, follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        with session() as db:
            assert db.get(MyAd, "1000000001").pending_action == "delete"     # marked before willhaben is asked
        page = c.get("/myads?show_active=1&show_inactive=1").text
        assert 'data-pending-key="ad:1000000001"' in page and "Deleting…" in page and 'value="1000000001" form="bulk"' not in page
        state = c.get("/jobs").json()
        assert state["pending"] == ["ad:1000000001"] and state["active"][0]["label"].startswith("Delete „")
        r = c.post("/myads/1000000001/delete", follow_redirects=False)       # already queued: not twice
        assert "Nothing%20queued" in r.headers["location"]
        c.portal.call(gate.set)
        wait_jobs()
        with session() as db:
            assert db.get(MyAd, "1000000001") is None
        state = c.get("/jobs").json()
        assert not state["active"] and not state["pending"] and state["recent"][0]["result"] == "Deleted on willhaben: 1"
        assert c.post("/myads/bulk/nuke", data={"ids": ["1"]}).status_code == 404
        page = c.get("/myads?show_inactive=1").text
        assert 'data-act="delete"' in page and 'name="ids"' in page and ">Activate<" in page
        assert 'id="bg-jobs"' in page and "Deleted on willhaben: 1" in page    # header: last result


async def test_queued_job_failure_lands_on_the_ads(account, monkeypatch):
    from app.services import jobs
    from app.willhaben.session import NotLoggedIn

    await my_ads.import_from_account()

    async def logged_out(method, path, body=None, accept="application/json"):
        raise NotLoggedIn("HTTP 401")

    monkeypatch.setattr(account, "api", logged_out)
    assert my_ads.enqueue("deactivate", ["1000000001", "1000000002", "1000000001"]) == 2
    assert my_ads.enqueue("delete", ["1000000001"]) == 0                    # still waiting: no second action
    job = jobs._active[0]
    assert job.status in ("queued", "running") and job.total == 2
    await job.task
    assert job.status == "failed" and "401" in job.result
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert all(r.pending_action is None and "401" in r.last_error for r in rows.values())


async def test_jobs_run_one_after_another(account):
    from app.services import jobs

    await my_ads.import_from_account()
    my_ads.enqueue("deactivate", ["1000000001"])
    upd = my_ads.start_update()
    assert my_ads.start_update() is upd                                      # a waiting update is not doubled
    assert [j["status"] for j in jobs.snapshot()["active"]] == ["queued", "queued"]
    await upd.task
    snap = jobs.snapshot()
    assert not snap["active"] and [j["status"] for j in snap["recent"]] == ["done", "done"]
    assert "loaded from willhaben" in snap["recent"][0]["result"]
    with session() as db:
        assert db.get(MyAd, "1000000001").status == "Deaktiviert"


def test_recover_after_restart():
    with session() as db:
        db.add(MyAd(ad_id="7", heading="Halb", status="active", pending_action="delete"))
        db.commit()
    my_ads.recover()
    with session() as db:
        row = db.get(MyAd, "7")
    assert row.pending_action is None and "restart" in row.last_error


async def test_hide_only_survives_import_and_routes(account):
    from app.main import app

    await my_ads.import_from_account()
    with TestClient(app) as c:
        assert c.post("/myads/bulk/hide", data={"ids": ["1000000001"]}, follow_redirects=False).status_code == 303
        page = c.get("/myads?show_inactive=1").text
        assert "Anzeige 1000000001" not in page and "Show 1 hidden ads" in page
        assert "Anzeige 1000000001" in c.get("/myads?show_hidden=1").text   # an active ad: in the Active view
        assert "/delete" in page and ">Activate<" in page   # the visible ad is deactivated
    await my_ads.import_from_account()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert rows["1000000001"].hidden is True                          # the import keeps it hidden
    assert not any(m in ("PUT", "DELETE") for m, _ in account.calls)  # hiding never touches willhaben


async def test_bulk_stops_at_logout(account, monkeypatch):
    from app.willhaben.session import NotLoggedIn

    await my_ads.import_from_account()
    real = account.api

    async def api(method, path, body=None, accept="application/json"):
        if method == "DELETE":
            raise NotLoggedIn("HTTP 401")
        return await real(method, path, body, accept)

    monkeypatch.setattr(account, "api", api)
    res = await my_ads.bulk("delete", ["1000000001", "1000000002"])
    assert res["done"] == [] and "401" in res["failed"][0][1] and "not done" in res["failed"][1][1]
    with session() as db:
        assert len(db.exec(select(MyAd)).all()) == 2


async def test_delete_unlinks_draft_and_publish_restarts_after_409(account, monkeypatch):
    from app.db import AdDraft
    from app.services import ad_creator
    from app.willhaben import insert

    await my_ads.import_from_account()
    plan = {"options": [{"path": [{"label": "X", "code": "X"}], "label": "X", "fields": []}]}
    with session() as db:
        db.add(AdDraft(id=50, input_text="", title="Testartikel", description="Text", image_paths=[],
                       wh_plan=plan, wh_ad_id="1000000001"))
        db.add(AdDraft(id=51, input_text="", title="Zweiter", description="Text", image_paths=[],
                       wh_plan=plan, wh_ad_id="999"))       # points to an ad that was deleted elsewhere
        db.commit()
    await my_ads.delete("1000000001")
    with session() as db:
        assert db.get(AdDraft, 50).wh_ad_id is None            # unlinked: next publish starts fresh

    calls = []

    async def fake_publish(sess, ad, photos, existing_ad_id=None, on_presaved=None, stop_after=None, **_kw):
        calls.append(existing_ad_id)
        if existing_ad_id:
            raise RuntimeError("POST /webapi/iad/products/privates/atz/u/carts: HTTP 409 ")
        return {"ad_id": "2000000001", "url": "https://www.willhaben.at/iad/object?adId=2000000001"}

    async def fake_form(sess):
        return {"ad": {}}

    monkeypatch.setattr(insert, "publish", fake_publish)
    monkeypatch.setattr(insert, "load_form", fake_form)
    monkeypatch.setattr(insert, "compose_ad", lambda *a, **k: {})
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: account)
    await ad_creator.publish_willhaben(51, 0, {})
    assert calls == ["999", None]                               # stale willhaben draft -> one fresh insertion
    with session() as db:
        d = db.get(AdDraft, 51)
    assert d.status == "published" and d.wh_ad_id == "2000000001"


async def test_publish_409_never_duplicates_a_live_ad(account, monkeypatch):
    from app.db import AdDraft
    from app.services import ad_creator
    from app.willhaben import insert

    plan = {"options": [{"path": [{"label": "X", "code": "X"}], "label": "X", "fields": []}]}
    with session() as db:   # its willhaben ad 1000000001 is still active in the account list
        db.add(AdDraft(id=52, input_text="", title="Live", description="Text", image_paths=[], wh_plan=plan,
                       wh_ad_id="1000000001"))
        db.commit()
    calls = []

    async def fake_publish(sess, ad, photos, existing_ad_id=None, on_presaved=None, stop_after=None, **_kw):
        calls.append(existing_ad_id)
        raise RuntimeError("POST /webapi/iad/products/privates/atz/u/carts: HTTP 409 ")

    async def fake_form(sess):
        return {"ad": {}}

    monkeypatch.setattr(insert, "publish", fake_publish)
    monkeypatch.setattr(insert, "load_form", fake_form)
    monkeypatch.setattr(insert, "compose_ad", lambda *a, **k: {})
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: account)
    with pytest.raises(insert.InsertError, match="schon als 1000000001"):
        await ad_creator.publish_willhaben(52, 0, {})
    assert calls == ["1000000001"]                              # no second, fresh insertion
    with session() as db:
        assert db.get(AdDraft, 52).status == "published"


class _Editor(_Account):
    """Fake willhaben editor: GET /webapi/iad/bap/<id> returns the full ad, PUT bap/edit replaces it."""

    def __init__(self, applies=True):
        super().__init__()
        self.applies, self.put_body = applies, None
        self.ad = {"adId": 1000000001, "heading": "Alter Titel", "price": 40, "categoryPath": {"path": ["X"]},
                   "description": "<p>Zeile eins<br>Zeile zwei</p><ul><li>Netzteil</li><li>Tasche</li></ul>",
                   "advertImageList": {"advertImage": [{"id": 1}]}, "contextLinkList": {"contextLink": []}}

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/webapi/iad/bap/1000000001":
            return dict(self.ad)
        if method == "PUT" and path == "/webapi/iad/bap/edit":
            self.put_body = body
            if self.applies:
                self.ad = dict(body)
            return body
        raise AssertionError((method, path))


def test_description_text_roundtrip():
    from app.willhaben import insert
    text = "Zeile eins\nZeile zwei\n\n• Netzteil\n• Tasche"
    assert insert.description_text(insert.description_html(text)) == text
    assert insert.description_text("<p>A &amp; B</p>") == "A & B"


async def test_edit_changes_only_three_fields(monkeypatch):
    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    await my_ads.import_from_account()
    cur = await my_ads.load_for_edit("1000000001")
    assert {k: cur[k] for k in ("heading", "description", "price")} == {
        "heading": "Alter Titel", "description": "Zeile eins\nZeile zwei\n• Netzteil\n• Tasche", "price": 40}  # no gap on willhaben either
    assert cur["images"] == [] and cur["images_error"]        # no imageList link: text editing still works
    row = await my_ads.edit("1000000001", "Neuer Titel", "Neu\n\n• Netzteil", 35.0)
    body = acct.put_body
    assert body["heading"] == "Neuer Titel" and body["price"] == 35 and body["description"] == "<p>Neu</p><p><br></p><ul><li>Netzteil</li></ul>"
    assert body["advertImageList"] == {"advertImage": [{"id": 1}]} and body["categoryPath"] == {"path": ["X"]}   # full object
    assert "edited" in row.last_action                       # heading comes back from the account list
    assert not any("cart" in p or "checkout" in p for _, p in acct.calls)


async def test_edit_not_applied_is_reported(monkeypatch):
    from app.willhaben.insert import InsertError
    acct = _Editor(applies=False)
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    with pytest.raises(InsertError, match="nicht übernommen"):
        await my_ads.edit("1000000001", "Neuer Titel", "Text", 10.0)


def test_edit_page_and_route(monkeypatch):
    from app.main import app

    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads/1000000001/edit").text
        assert 'value="Alter Titel"' in page and "• Netzteil" in page and 'value="40"' in page
        r = c.post("/myads/1000000001/edit", data={"heading": "Neu", "description": "Text", "price": "12,50"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/myads?ad=1000000001&msg=")   # back to the ad
        wait_jobs()                                                          # saved by the background job
        assert acct.put_body["price"] == 12.5
        with session() as db:
            row = db.get(MyAd, "1000000001")
        assert not row.pending_action and not row.pending_payload
        r = c.post("/myads/1000000001/edit", data={"heading": "Neu", "description": "Text", "price": "abc"},
                   follow_redirects=False)
        assert "/edit?msg=" in r.headers["location"]


def test_ai_revision_fills_the_form_without_saving(monkeypatch):
    from app.llm.base import LLMResult
    from app.main import app

    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)

    class Detail:
        images, category_path = [], ["Audio", "Lautsprecher"]

    async def fake_detail(ad_id):
        return Detail()

    class Backend:
        prompts = []

        async def complete(self, *, system, prompt, schema, images=None):
            Backend.prompts.append(prompt)
            return LLMResult(data={"title": "Kürzerer Titel", "description": "Neuer Text.", "price_estimate": 30,
                                   "note": "Titel gekürzt."}, model="fake")

    monkeypatch.setattr(my_ads, "get_public", lambda: type("P", (), {"get_detail": staticmethod(fake_detail)})())
    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: Backend())
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        page = c.post("/myads/1000000001/edit/ai", data={"heading": "Alter Titel", "description": "Alt", "price": "40",
                                                         "instruction": "kürzer"}).text
    assert 'value="Kürzerer Titel"' in page and "Neuer Text." in page and "Titel gekürzt." in page
    assert 'value="40"' in page and "Claude estimates" in page                   # price stays, estimate offered
    assert "kürzer" in Backend.prompts[0] and "Audio > Lautsprecher" in Backend.prompts[0]
    assert acct.put_body is None                                                 # nothing saved on willhaben

    class Failing:
        async def complete(self, *, system, prompt, schema, images=None):
            from app.llm.base import LLMError
            raise LLMError("Limit erreicht")

    monkeypatch.setattr("app.llm.base.get_backend", lambda *a: Failing())
    with TestClient(app) as c:                       # htmx keeps only #edit-main: the error must be inside it
        page = c.post("/myads/1000000001/edit/ai", data={"heading": "Alter Titel", "description": "Alt", "price": "40",
                                                         "instruction": "kürzer"}, headers={"HX-Request": "true"}).text
    main = page.split('id="edit-main"', 1)[1]
    assert "Limit erreicht" in main and 'value="Alter Titel"' in main


def test_myads_filters_and_order():
    from datetime import timedelta

    from app.db import MyAd, session, utcnow
    from app.main import app
    from fastapi.testclient import TestClient
    now = utcnow()
    with session() as db:
        db.add(MyAd(ad_id="1", heading="Bald", status="active", end_date=now + timedelta(days=2)))
        db.add(MyAd(ad_id="2", heading="Lange", status="active", end_date=now + timedelta(days=40)))
        db.add(MyAd(ad_id="3", heading="Deaktiviert", status="Passiv", end_date=now))
        db.add(MyAd(ad_id="4", heading="Weg", status="sold", end_date=now))
        db.add(MyAd(ad_id="5", heading="Geschenkt", status="Verschenkt", end_date=now))
        db.add(MyAd(ad_id="6", heading="Reserviert", status="active", sale_status="sold", end_date=now))
        db.add(MyAd(ad_id="7", heading="Ausgelaufen", status="expired", end_date=now))
        db.add(MyAd(ad_id="8", heading="Uralt", status="expired", end_date=now - timedelta(days=400)))
        db.add(MyAd(ad_id="9", heading="Erneuert", status="expired", replaced_by="2", end_date=now,
                    url="https://www.willhaben.at/iad/object?adId=9"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads").text
        assert page.index("Lange") < page.index("Bald")       # most days left first
        # long expired (past the 30-day cutoff) and the old copy of a renewed ad are not "expired"
        for gone in ("Deaktiviert", "Weg", "Geschenkt", "Reserviert", "Uralt", "Erneuert"):
            assert gone not in page
        assert re.search(r"Deactivated<small[^>]*>2</small>", page) and re.search(r"Sold / reserved<small[^>]*>3</small>", page)
        # expired ads always show, in their own section above the others
        assert page.index("Active (2)") < page.index("Lange") < page.index("Bald") < page.index("Expired (1)") \
            < page.index("Ausgelaufen")    # active ads on top, then the expired ones
        assert ">Activate<" in c.get("/myads?ad=7").text    # an expired ad can be put online again by hand (republished)
        # the chips are exclusive views: only that group, nothing of the active list
        page = c.get("/myads?show_inactive=1").text
        assert "Deaktiviert" in page and "Uralt" in page and "Weg" not in page
        assert "Erneuert" not in page     # a renewed ad's old copy is never listed: its chats use the new copy
        assert "Bald" not in page and "Ausgelaufen" not in page
        page = c.get("/myads?show_sold=1").text
        assert "Weg" in page and "Geschenkt" in page and "Deaktiviert" not in page


def test_list_stays_on_the_page(monkeypatch):
    """htmx asks in the background: the update answers without a redirect, actions keep the filters and the open
    ad, and the page carries what swaps it in place (list, detail, lane refresh)."""
    from app.main import app

    async def no_public_check(ad_id, off_account=False):     # the update must not reach willhaben from a test
        return None

    monkeypatch.setattr(my_ads, "check", no_public_check)
    with session() as db:
        db.add(MyAd(ad_id="11", heading="Rad", status="active"))
        db.commit()
    hx = {"HX-Request": "true", "HX-Current-URL": "http://testserver/myads?show_inactive=1&ad=11&msg=alt"}
    with TestClient(app) as c:
        page = c.get("/myads?ad=11").text
        assert c.post("/myads/check", headers=hx, follow_redirects=False).status_code == 204
        loc = c.post("/myads/bulk/hide", data={"ids": ["11"]}, headers=hx, follow_redirects=False).headers["location"]
        assert "msg=Hidden" in loc and "show_inactive=1" in loc and "ad=11" in loc and "alt" not in loc
        r = c.post("/myads/check", headers={"referer": "http://testserver/myads?show_sold=1"}, follow_redirects=False)
        assert r.status_code == 303 and "show_sold=1" in r.headers["location"]
        wait_jobs()
    for part in ('id="myads-rows"', 'data-refresh-on-lane="ads"', 'hx-post="/myads/bulk/delete"', 'id="myads-detail"',
                 'hx-post="/myads/11/delete"', 'hx-post="/myads/check"', 'id="bg-jobs"'):
        assert part in page, part


def _cleanup_account(account):
    """1000000002 deactivated without a photo (deletable), 1000000003 sold with no delete link (only hidden here)."""
    ads = account._ads()
    ads[1]["advertImageList"] = {"advertImage": []}
    sold = copy.deepcopy(ads[0])
    sold.update(id="1000000003", description="Anzeige 1000000003", advertStatus={"id": "sold", "description": "Verkauft"},
                advertEditStatusActionsList={"contextLinkList": {"contextLink": []}})
    ads.append(sold)


async def test_cleanup_is_queued_and_marks_at_once(account):
    _cleanup_account(account)
    await my_ads.import_from_account()
    assert my_ads.enqueue_cleanup(["1000000002", "1000000003", "1000000001"]) == 3
    with session() as db:                                                   # marked before willhaben is asked
        assert {r.ad_id for r in db.exec(select(MyAd)) if r.pending_action == "delete"} == {"1000000001", "1000000002", "1000000003"}
    await finish_jobs()
    with session() as db:
        rows = {r.ad_id: r for r in db.exec(select(MyAd))}
    assert "1000000002" not in rows and rows["1000000003"].hidden is True
    assert "1000000001" in rows                                             # active: never a candidate, never deleted
    assert not any(r.pending_action for r in rows.values())
    assert [p for m, p in account.calls if m == "DELETE"] == ["/webapi/myatz/bap/1000000002"]
    from app.services import jobs
    assert jobs.snapshot()["recent"][0]["result"] == "Deleted on willhaben: 1 · 1 only hidden here"


def test_cleanup_route_returns_at_once(account):
    from app.main import app

    _cleanup_account(account)
    with TestClient(app) as c:
        c.portal.call(my_ads.import_from_account)
        r = c.post("/myads/cleanup", data={"ids": ["1000000002"]}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/myads?msg=")
        wait_jobs()
        page = c.get("/myads/cleanup").text
    with session() as db:
        assert db.get(MyAd, "1000000002") is None
    assert "Runs in the background" in page


async def test_cleanup_logged_out_leaves_the_reason_on_the_ads(account, monkeypatch):
    from app.services import jobs
    from app.willhaben.session import NotLoggedIn

    await my_ads.import_from_account()

    async def logged_out(method, path, body=None, accept="application/json"):
        raise NotLoggedIn("HTTP 401")

    monkeypatch.setattr(account, "api", logged_out)
    my_ads.enqueue_cleanup(["1000000002"])
    await finish_jobs()
    with session() as db:
        row = db.get(MyAd, "1000000002")
    assert row.pending_action is None and "401" in row.last_error
    assert jobs.snapshot()["recent"][0]["status"] == "failed"


def test_bulk_hide_and_show_again_without_willhaben():
    from app.db import MyAd, session
    from app.main import app
    from fastapi.testclient import TestClient
    with session() as db:
        for i in ("11", "12", "13"):
            db.add(MyAd(ad_id=i, heading=f"Ad {i}", status="active"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/myads").text
        assert 'formaction="/myads/bulk/hide"' in page and "Hide from this list" not in page   # no link in the detail any more
        r = c.post("/myads/bulk/hide", data={"ids": ["11", "12"]}, follow_redirects=False)
        assert r.status_code == 303
        assert 'formaction="/myads/bulk/unhide"' in c.get("/myads?show_hidden=1").text
        c.post("/myads/bulk/unhide", data={"ids": ["12"]})
    with session() as db:
        assert db.get(MyAd, "11").hidden and not db.get(MyAd, "12").hidden and not db.get(MyAd, "13").hidden


def test_cleanup_marks_only_its_own_ads():
    from app.db import MyAd, session
    from app.services import jobs, my_ads
    with session() as db:
        db.add(MyAd(ad_id="21", heading="Alt", status="Passiv"))
        db.add(MyAd(ad_id="22", heading="Wird bearbeitet", status="Passiv", pending_action="edit"))
        db.commit()
    seen = []
    orig = jobs.submit
    jobs.submit = lambda kind, label, run, **kw: seen.append((kind, label))   # no job run here
    try:
        assert my_ads.enqueue_cleanup(["21", "22", "21"]) == 1                  # 22 belongs to another queued job
    finally:
        jobs.submit = orig
    with session() as db:
        assert db.get(MyAd, "21").pending_action == "delete" and db.get(MyAd, "22").pending_action == "edit"
    assert seen and "1" in seen[0][1]



def test_the_panel_loads_the_ads_text(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import MyAd, session
    from app.main import app
    from app.willhaben import public

    refreshed = []

    class _Pub:
        async def get_detail(self, ad_id, refresh=False):
            refreshed.append(refresh)
            if ad_id == "2":
                raise public.WillhabenError("HTTP 500")
            if ad_id == "3":
                return None
            from app.platforms.types import AdDetail
            return AdDetail(ad_id=ad_id, heading="Kamera", description="Erste Zeile\nZweite <Zeile>", price=40.0,
                            price_display="€ 40", status="active", published_at=None, first_published_at=None,
                            end_date=None, url="u", images=["https://img.example/1.jpg", "https://img.example/2.jpg"],
                            location="1040 Wien", paylivery_enabled=True,
                            shipping_options=[{"name": "Post", "home": 5.7, "point": 3.49}])

        async def aclose(self):
            pass

    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_public", lambda: _Pub())
    with session() as db:
        for i in "123":
            db.add(MyAd(ad_id=i, heading=f"Ad {i}", status="active"))
        db.commit()
    with TestClient(app) as c:
        panel = c.get("/myads?ad=1").text
        assert 'hx-get="/myads/1/description"' in panel
        frag = c.get("/myads/1/description").text
        assert "Erste Zeile\nZweite &lt;Zeile&gt;" in frag 
        assert frag.count('<img src="https://img.example/') == 2 and 'x-text="(i + 1)' in frag and "€ 40" in frag and "1040 Wien" in frag
        assert "PayLivery Post" in frag and "€ 3,49" in frag and "€ 5,70" in frag
        assert "/deal" not in frag and "/hits/ad/willhaben/1/deal" in panel     # one price check: the ad's own, above
        assert "HTTP 500" in c.get("/myads/2/description").text
        assert "shows no text" in c.get("/myads/3/description").text
        assert c.get("/myads/9/description").status_code == 404
        # "Reload" fetches anew; the link opens willhaben's own editor
        assert 'hx-get="/myads/1/description?refresh=1"' in panel and "/edit\"" not in frag   # in the header
        assert 'href="https://www.willhaben.at/iad/anzeige-bearbeiten/marktplatz?adId=1&amp;adTypeId=67"' in panel
        refreshed.clear()
        c.get("/myads/1/description?refresh=1")
        assert refreshed == [True]


async def test_an_active_ad_under_review_stays_active():
    """After an edit willhaben checks the ad for a while and reports another status: it stays in Active (with
    willhaben's word for it) instead of jumping to Deactivated for a moment; real end states still move it."""
    from app.willhaben.account import AdvertList, OwnAdvert

    def ad(status, label=None):
        return OwnAdvert(ad_id="5", heading="Kamera", status=status, status_label=label, price_display=None,
                         start=None, end=None, url="u", image_url=None, actions={"2": {"relativePath": "x"}})

    with session() as db:
        db.add(MyAd(ad_id="5", heading="Kamera", status="active"))
        db.commit()
    for status, label, want in (("checking", "In Prüfung", "active"), ("active", None, "active"),
                                ("unknownid", "Verschenkt", "Verschenkt"), ("active", None, "active"),
                                ("deactivated", "Deaktiviert", "Deaktiviert")):
        await my_ads.import_from_account(AdvertList([ad(status, label)]))
        with session() as db:
            row = db.get(MyAd, "5")
        assert row.status == want, (status, label)
        if status == "checking":
            assert "In Prüfung" in row.last_action


def test_price_changes_from_the_panel_and_decimal_points_are_read_right(monkeypatch):
    """POST /myads/<id>/price saves the price alone (title and text as willhaben has them) as a background job;
    "12.5" means twelve and a half, not 125."""
    from app.main import app

    acct = _Editor()
    monkeypatch.setattr(my_ads, "get_session", lambda: acct)
    with session() as db:
        db.add(MyAd(ad_id="1000000001", heading="Alter Titel", url="https://www.willhaben.at/iad/object?adId=1000000001"))
        db.commit()
    with TestClient(app) as c:
        r = c.post("/myads/1000000001/price", data={"price": "12.5"}, follow_redirects=False)
        assert r.status_code == 303 and "ad=1000000001" in r.headers["location"] and "msg=&" in r.headers["location"]
        wait_jobs()                                                  # the job may already have run (no grace in tests)
        assert acct.put_body["price"] == 12.5 and acct.put_body["heading"] == "Alter Titel"
        with session() as db:
            assert db.get(MyAd, "1000000001").pending_action is None
        r = c.post("/myads/1000000001/price", data={"price": "abc"}, follow_redirects=False)
        assert "Invalid" in r.headers["location"]
        r = c.post("/myads/1000000001/edit", data={"heading": "Neu", "description": "Text", "price": "12.5"},
                   follow_redirects=False)
        wait_jobs()
        assert acct.put_body["price"] == 12.5                        # the edit page reads a decimal point too
        page = c.get("/myads/1000000001/edit").text                  # and renders without %g surprises
        assert ('value="12.5"' in page or 'value="40"' in page) and "e+0" not in page


async def test_deleting_a_renewed_ads_old_copy_keeps_its_row(account):
    """The old copy of a renewed ad: deleted on willhaben, but its row stays (hidden) – chats and sales chain through it."""
    await my_ads.import_from_account()
    with session() as db:
        row = db.get(MyAd, "1000000001")
        row.replaced_by = "1000000099"
        db.add(row)
        db.commit()
    await my_ads.delete("1000000001")
    with session() as db:
        row = db.get(MyAd, "1000000001")
        assert row is not None and row.status == "deleted" and row.hidden and row.replaced_by == "1000000099"
