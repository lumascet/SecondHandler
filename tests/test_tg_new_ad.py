"""The Telegram Mini App's ad creation (/tg/new-ad): the start page, a draft with its willhaben step, a batch – the
dashboard's endpoints, answered back into the Mini App (app/tg_bridge.py)."""
import pytest
from fastapi.testclient import TestClient

from app.db import AdBatch, AdDraft, session
from app.services import ad_creator

TG = {"X-TG-Back": "/tg/new-ad"}


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _draft(**kw):
    with session() as db:
        d = AdDraft(input_text="Bohrschrauber", title="Bosch GSR 18V-55", description="Wie neu.", price_suggestion=80.0,
                    **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
        return d


def test_the_start_page_creates_a_draft_in_the_mini_app(client, monkeypatch):
    page = client.get("/tg/new-ad").text
    assert 'action="/new-ad"' in page and 'name="photos"' in page and "data-main-form=\"new-ad-form\"" in page
    assert 'action="/new-ad/batch"' in client.get("/tg/new-ad?mode=batch").text
    made = _draft()
    monkeypatch.setattr(ad_creator, "enqueue_create", lambda notes, files, *a, **k: made)
    r = client.post("/new-ad", data={"notes": "Bohrschrauber"}, files={"photos": ("a.jpg", b"x", "image/jpeg")},
                    headers=TG, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/tg/new-ad/{made.id}"     # the draft opens there
    assert f'href="/tg/new-ad/{made.id}"' in client.get("/tg/new-ad").text               # among the recent drafts


def test_a_draft_saves_revises_and_publishes_through_the_dashboard(client, monkeypatch):
    d = _draft(geizhals={"query": "Bosch GSR 18V-55", "match": {"name": "Bosch GSR 18V-55", "url": "https://geizhals.at/a1",
                                                               "price_from": 99.0, "specs": []}},
               wh_plan={"query": "Bohrschrauber", "options": [{"label": "Werkzeug › Akkuschrauber",
                                                               "path": [{"code": "1", "label": "Werkzeug"}],
                                                               "fields": [{"code": "c", "label": "Zustand", "selected": "",
                                                                           "values": [["n", "Neu"], ["g", "Gebraucht"]]}]}]})
    page = client.get(f"/tg/new-ad/{d.id}").text
    assert 'id="draft-form"' in page and f'action="/new-ad/{d.id}/refine"' in page
    assert f'action="/new-ad/{d.id}/willhaben/publish"' in page and 'name="o0_c"' in page and 'name="handover"' in page
    assert "Bosch GSR 18V-55" in page and "data-geizhals" in page
    r = client.post(f"/new-ad/{d.id}", data={"title": "Bosch GSR 18V-55 Akkuschrauber", "description": "Wie neu.",
                                              "price": "75"}, headers={"X-TG-Back": f"/tg/new-ad/{d.id}"},
                    follow_redirects=False)
    assert r.headers["location"].startswith(f"/tg/new-ad/{d.id}?msg=")
    with session() as db:
        assert db.get(AdDraft, d.id).price_suggestion == 75
    monkeypatch.setattr(ad_creator, "enqueue_refine", lambda *a, **k: True)
    r = client.post(f"/new-ad/{d.id}/refine", data={"instruction": "kürzer"}, headers={"X-TG-Back": f"/tg/new-ad/{d.id}"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/tg/new-ad/{d.id}"
    with session() as db:                                     # Claude at work: the page asks again until it is done
        row = db.get(AdDraft, d.id)
        row.refining = True
        db.add(row)
        db.commit()
    assert 'hx-trigger="every 4s"' in client.get(f"/tg/new-ad/{d.id}").text


def test_a_batch_its_groups_and_its_drafts(client):
    d = _draft()
    with session() as db:
        b = AdBatch(image_paths=["/x/1.jpg", "/x/2.jpg", "/x/3.jpg"], status="review",
                    groups=[{"id": "g1", "photos": [0, 1], "product": "Bohrschrauber", "include": True, "comment": ""},
                            {"id": "g2", "photos": [], "product": "Lampe", "include": False, "comment": "",
                             "draft_id": d.id}])
        db.add(b)
        db.commit()
        db.refresh(b)
    sort = client.get(f"/tg/new-ad/batch/{b.id}?step=sort").text
    assert f'action="/new-ad/batch/{b.id}"' in sort and 'name="img_0"' in sort and 'name="img_2"' in sort   # 2: unused
    assert 'name="product_g1"' in sort and 'value="create"' in sort
    review = client.get(f"/tg/new-ad/batch/{b.id}").text                  # drafts there: the review step first
    assert f'action="/tg/new-ad/batch/{b.id}/drafts"' in review and f'href="/tg/new-ad/{d.id}"' in review
    r = client.post(f"/new-ad/batch/{b.id}", data={"product_g1": "Akkuschrauber", "include_g1": "1", "img_0": "g1",
                                                   "img_1": "g1", "action": "save"},
                    headers={"X-TG-Back": f"/tg/new-ad/batch/{b.id}"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/tg/new-ad/batch/{b.id}?msg=")


def test_the_batch_review_edits_inline_and_brings_the_tables_message_back(client):
    """The drafts as editable cards (title, price, pieces, handover; ⧉ on the values); the table's save and its
    actions answer the dashboard's fragment, so the Mini App's route brings its message back to the batch page."""
    with session() as db:
        b = AdBatch(image_paths=["/x/1.jpg"], status="review", groups=[])
        db.add(b)
        db.commit()
        db.refresh(b)
    d = _draft(batch_id=b.id, status="draft")
    with session() as db:
        row = db.get(AdBatch, b.id)
        row.groups = [{"id": "g1", "photos": [0], "product": "Bohrschrauber", "include": True, "comment": "",
                       "draft_id": d.id}]
        db.add(row)
        db.commit()
    review = client.get(f"/tg/new-ad/batch/{b.id}?step=review").text
    assert f'name="{d.id}.title"' in review and f'name="{d.id}.price"' in review and f'name="{d.id}.quantity"' in review
    assert f'name="{d.id}.handover"' in review and "data-copy" in review and f'value="refine:{d.id}"' in review
    r = client.post(f"/tg/new-ad/batch/{b.id}/drafts", data={f"{d.id}.title": "Bosch Akkuschrauber",
                                                             f"{d.id}.description": "Wie neu.", f"{d.id}.price": "70",
                                                             "sel": str(d.id), "all": str(d.id)},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/tg/new-ad/batch/{b.id}?step=review&msg=")
    with session() as db:
        assert db.get(AdDraft, d.id).title == "Bosch Akkuschrauber" and db.get(AdDraft, d.id).price_suggestion == 70
    r = client.post(f"/tg/new-ad/batch/{b.id}/drafts", data={"act": "discard", "all": str(d.id)},   # nothing ticked
                    follow_redirects=False)
    assert f"unticked={d.id}" in r.headers["location"] and "msg=" in r.headers["location"]
    with session() as db:
        assert db.get(AdDraft, d.id).status == "draft"


def test_the_sort_step_drags_photos_and_has_a_new_item(client):
    with session() as db:
        b = AdBatch(image_paths=["/x/1.jpg", "/x/2.jpg"], status="review",
                    groups=[{"id": "g1", "photos": [0, 1], "product": "Lampe", "include": True, "comment": ""}])
        db.add(b)
        db.commit()
        db.refresh(b)
    sort = client.get(f"/tg/new-ad/batch/{b.id}?step=sort").text
    assert 'data-gid="new"' in sort and "data-handle" in sort and "data-include-all" in sort and "pointerdown" in sort


def test_the_mini_app_keeps_the_kleinanzeigen_tick_and_asks_nothing_before_publishing(client, monkeypatch):
    from app.services import crosspost_kleinanzeigen
    monkeypatch.setattr(crosspost_kleinanzeigen, "available", lambda: True)
    d = _draft(wh_plan={"options": [{"label": "Audio › Lautsprecher", "fields": []}], "selected": 0})
    page = client.get(f"/tg/new-ad/{d.id}").text
    assert f'hx-post="/new-ad/{d.id}/also"' in page and 'id="wh-publish"' in page
    assert "hx-confirm" not in page.split('id="wh-publish"')[1].split("</form>")[0]     # no popup: a grace job with Cancel
    assert client.post(f"/new-ad/{d.id}/also", data={"kleinanzeigen": "1"}).status_code == 204
    assert 'name="kleinanzeigen" value="1" checked' in client.get(f"/tg/new-ad/{d.id}").text


def test_the_mini_app_batch_publishes_and_discards_without_a_popup(client, monkeypatch):
    from app.services import ad_batch, crosspost_kleinanzeigen
    monkeypatch.setattr(crosspost_kleinanzeigen, "available", lambda: True)
    d = _draft(wh_plan={"options": [], "selected": 0})
    b = ad_batch.new_batch("ein Stuhl")
    ad_batch._update(b.id, status="review", groups=[{"draft_id": d.id, "photos": []}])
    page = client.get(f"/tg/new-ad/batch/{b.id}").text
    for act in ("publish", "discard"):                       # the button's own tag: no popup in front of the grace job
        end = page.index(f'value="{act}"')
        tag = page[page.rfind("<button", 0, end):page.index(">", end)]
        assert "<button" in tag and "hx-confirm" not in tag
    assert f'hx-post="/new-ad/batch/{b.id}/also"' in page
