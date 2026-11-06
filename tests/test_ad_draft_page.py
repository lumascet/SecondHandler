"""The ad draft page and the batch page without page loads: the fields save themselves, every other form swaps its
section, Claude's work is a background job the page waits for."""
from app import db as dbmod
from app.services import ad_batch, ad_creator
from tests.conftest import wait_jobs
from tests.test_dashboard import client  # noqa: F401  (dashboard with the fake Claude backend)

GH = {"match": {"url": "https://geizhals.at/x-a1.html", "name": "Bosch GSR 18V-55", "price_from": 99.0,
                "specs": [["Typ", "Bohrschrauber"]]}, "verified": True, "candidates": []}


def _draft(**kw):
    with dbmod.session() as db:
        d = dbmod.AdDraft(input_text="x", title="Logitech S125i", description="Gut erhalten.", price_suggestion=12,
                          condition="Gebraucht", **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def _get(draft_id):
    with dbmod.session() as db:
        return db.get(dbmod.AdDraft, draft_id)


def test_fields_save_themselves_and_the_forms_swap_their_sections(client):  # noqa: F811
    d = _draft(questions=[{"id": "q1", "text": "Farbe?", "options": ["rot", "blau"]}],
               revisions=[{"instruction": "kürzer", "title": "a", "description": "b"}], geizhals=GH)
    page = client.get(f"/new-ad/{d.id}").text
    assert 'id="draft-form" x-data="autosave"' in page and 'x-text="state"' in page
    assert f'hx-post="/new-ad/{d.id}/answers" hx-target="#draft-refine" hx-select="#draft-refine"' in page
    assert f'hx-post="/new-ad/{d.id}/undo" hx-target="#draft-refine"' in page and "#draft-aside" in page
    assert f'hx-post="/new-ad/{d.id}/willhaben/prepare" hx-include="#title, #desc, #price, #quantity" hx-target="#willhaben" hx-select="#willhaben"' in page
    assert f'hx-post="/new-ad/{d.id}/reconcile" hx-target="#geizhals" hx-select="#geizhals"' in page
    assert page.count(f'hx-post="/new-ad/{d.id}/geizhals" hx-target="#geizhals"') == 1       # the search (no other candidates)
    assert 'id="draft-aside"' in page
    # saved as you type: JSON with the problem, if any; a plain form still comes back with a message
    auto = {"X-Autosave": "1"}
    assert client.post(f"/new-ad/{d.id}", data={"title": "Neu", "description": "Text", "price": "12,50"}, headers=auto).json() == {"ok": True, "error": None}
    assert _get(d.id).title == "Neu" and _get(d.id).price_suggestion == 12.5
    r = client.post(f"/new-ad/{d.id}", data={"title": "Neu", "description": "Text", "price": "zwölf"}, headers=auto).json()
    assert r["ok"] is False and r["error"]
    r = client.post(f"/new-ad/{d.id}", data={"title": "Neu", "description": "Text", "price": "13"}, follow_redirects=False)
    assert r.status_code == 303 and "Changes%20saved" in r.headers["location"]


def test_geizhals_check_is_a_revision_job(client):  # noqa: F811
    d = _draft(geizhals=GH)
    r = client.post(f"/new-ad/{d.id}/reconcile", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/new-ad/{d.id}#geizhals"
    wait_jobs()
    assert _get(d.id).refining is None and _get(d.id).revisions[-1]["instruction"] == "Checked against the Geizhals spec sheet"
    with dbmod.session() as db:
        row = db.get(dbmod.AdDraft, d.id)
        row.status = "published"
        db.add(row)
        db.commit()
    r = client.post(f"/new-ad/{d.id}/reconcile", follow_redirects=False)       # refused: a message, nothing queued
    assert "msg=" in r.headers["location"] and r.headers["location"].endswith("#geizhals")
    assert "Check text against the spec sheet" not in client.get(f"/new-ad/{d.id}").text


def test_new_draft_is_written_by_a_job_the_page_waits_for(client, monkeypatch):  # noqa: F811
    import asyncio

    from app.services import jobs
    real = ad_creator._write_draft
    gate = client.portal.call(asyncio.Event)                               # Claude waits until the page was looked at

    async def held(*a, **kw):
        await gate.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(ad_creator, "_write_draft", held)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [])        # needs the app's event loop
    assert d.refining and d.image_paths == []
    page = client.get(f"/new-ad/{d.id}").text
    assert "Claude is writing the draft…" in page and 'data-pending-key="refining:' in page
    job = next(j for j in jobs.snapshot()["active"] if j["current"] == f"refining:{d.id}")
    assert job["lane"] == "claude" and job["link"] == f"/new-ad/{d.id}"
    client.portal.call(gate.set)
    wait_jobs()
    assert _get(d.id).title and _get(d.id).refining is None


def test_batch_page_waits_for_the_batch_instead_of_reloading(client):  # noqa: F811
    b = ad_batch.new_batch("alles aus dem Keller")
    ad_batch._update(b.id, status="sorting")
    page = client.get(f"/new-ad/batch/{b.id}").text
    assert f'id="batch-main" data-pending-key="batch:{b.id}"' in page and "http-equiv" not in page
    assert "location.reload" not in page.split('id="batch-main"')[1].split("</main>")[0]   # (the shell's pull-to-refresh aside)
    assert 'hx-target="#batch-main" hx-select="#batch-main"' in page and "refreshInPlace([document.getElementById('batch-main')])" in page
    assert f"batch:{b.id}" in client.get("/jobs", headers={"accept": "application/json"}).json()["pending"]
    ad_batch._update(b.id, status="review")
    page = client.get(f"/new-ad/batch/{b.id}").text
    assert 'id="batch-main" data-refresh=' in page and 'data-pending-key="batch:' not in page
    assert f"batch:{b.id}" not in client.get("/jobs", headers={"accept": "application/json"}).json()["pending"]


def _offer_on(monkeypatch):
    from app.services import crosspost_kleinanzeigen
    monkeypatch.setattr(crosspost_kleinanzeigen, "available", lambda: True)


def _ticked_box(page):
    return 'name="kleinanzeigen" value="1" checked' in page


def test_the_kleinanzeigen_tick_is_kept_when_the_willhaben_card_is_drawn_anew(client, monkeypatch):  # noqa: F811
    _offer_on(monkeypatch)
    d = _draft(wh_plan={"options": [{"label": "Audio › Lautsprecher", "fields": []}], "selected": 0})
    page = client.get(f"/new-ad/{d.id}").text
    assert f'hx-post="/new-ad/{d.id}/also"' in page and not _ticked_box(page)     # the Settings default: off
    r = client.post(f"/new-ad/{d.id}/also", data={"kleinanzeigen": "1"})
    assert r.status_code == 204 and _get(d.id).also == {"kleinanzeigen": True}
    assert _ticked_box(client.get(f"/new-ad/{d.id}").text)                        # the card drawn again: still ticked
    client.post(f"/new-ad/{d.id}/also", data={})                                  # an unticked box sends nothing
    assert _get(d.id).also == {"kleinanzeigen": False}
    assert not _ticked_box(client.get(f"/new-ad/{d.id}").text)


def test_the_batch_bar_keeps_its_tick_on_every_draft_of_the_batch(client, monkeypatch):  # noqa: F811
    _offer_on(monkeypatch)
    d1 = _draft(wh_plan={"options": [], "selected": 0})
    d2 = _draft(wh_plan={"options": [], "selected": 0})
    b = ad_batch.new_batch("zwei Stühle")
    ad_batch._update(b.id, status="review", groups=[{"draft_id": d1.id, "photos": []}, {"draft_id": d2.id, "photos": []}])
    assert not _ticked_box(client.get(f"/new-ad/batch/{b.id}").text)
    r = client.post(f"/new-ad/batch/{b.id}/also", data={"kleinanzeigen": "1"})
    assert r.status_code == 204
    assert _get(d1.id).also == {"kleinanzeigen": True} and _get(d2.id).also == {"kleinanzeigen": True}
    assert _ticked_box(client.get(f"/new-ad/batch/{b.id}").text)                  # a refresh of the page keeps it
    assert f'hx-post="/new-ad/batch/{b.id}/also"' in client.get(f"/new-ad/batch/{b.id}").text
    assert client.post(f"/new-ad/batch/{b.id + 999}/also", data={}).status_code == 404
