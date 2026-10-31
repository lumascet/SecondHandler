import re
import time

from sqlmodel import select

from app import db as dbmod
from app.llm.base import LLMResult
from app.services import ad_batch
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.conftest import wait_jobs  # noqa: E402

SORTED = {
    "groups": [
        {"photos": [2, 1], "product": "Bosch GSR 18V-55", "product_query": "Bosch GSR 18V-55",
         "observations": "Akkuschrauber mit Koffer, Kratzer am Griff", "quantity": 1, "confidence": "hoch",
         "question": ""},
        {"photos": [3], "product": "Holzstuhl braun", "product_query": "", "observations": "", "quantity": 4,
         "confidence": "niedrig", "question": "Gehört Foto 4 auch dazu?"},
        {"photos": [3, 99], "product": "Doppelt", "product_query": "", "observations": "", "quantity": 0,
         "confidence": "mittel", "question": ""},          # photo 3 is taken, 99 does not exist -> dropped
    ],
    "unassigned": [4, 5],
    "note": "Foto 5 ist unscharf.",
}


def _wait(batch_id, *states, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        b = ad_batch.get(batch_id)
        if b.status in states:
            return b
        time.sleep(0.02)
    raise AssertionError(f"batch stuck in {ad_batch.get(batch_id).status}")


def _sorting_backend(client, monkeypatch, result=SORTED):  # noqa: F811
    fake = client.fake
    orig = fake.complete

    async def complete(**kw):
        if "groups" in kw["schema"]["properties"]:
            fake.calls.append({**kw, "images": kw.get("images") or []})
            return LLMResult(data=result, model="fake-opus")
        return await orig(**kw)

    monkeypatch.setattr(fake, "complete", complete)
    monkeypatch.setattr("app.services.ad_batch.get_backend", lambda *a: fake)
    return fake


def test_batch_sort_review_and_create(client, monkeypatch):  # noqa: F811
    fake = _sorting_backend(client, monkeypatch)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(5)]
    photos.append(("photos", ("notes.txt", b"x", "text/plain")))          # skipped
    r = client.post("/new-ad/batch", data={"notes": "Abholung Wien 15"}, files=photos, follow_redirects=False)
    assert r.status_code == 303 and "/new-ad/batch/" in r.headers["location"] and "skipped" in r.headers["location"]
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])

    b = _wait(batch_id, "review")
    assert len(b.image_paths) == 5 and len(fake.calls[-1]["images"]) == 5
    assert "Abholung Wien 15" in fake.calls[-1]["prompt"]
    assert [g["photos"] for g in b.groups] == [[1, 0], [2]]              # 1-based -> index, duplicate dropped
    assert ad_batch.unassigned(b) == [3, 4] and b.note == "Foto 5 ist unscharf."
    assert [g["quantity"] for g in b.groups] == [None, 4]        # Claude's count; one piece is nothing to say
    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert "Bosch GSR 18V-55" in page and "Gehört Foto 4 auch dazu?" in page and "do not use" in page
    assert 'name="quantity_g2" value="4"' in page and 'name="quantity_g1" value=""' in page

    # user: photo 0 becomes the cover, photo 3 is a new article, chair is skipped, comment on the drill
    form = [("img_0", "g1"), ("img_1", "g1"), ("product_g1", "Bosch GSR 18V-55 solo"), ("comment_g1", "Akku fehlt"),
            ("include_g1", "1"), ("quantity_g1", "2"), ("img_2", "g2"), ("product_g2", "Holzstuhl"), ("img_3", "new"), ("img_4", ""),
            ("action", "save")]
    client.post(f"/new-ad/batch/{batch_id}", data=dict(form))
    b = ad_batch.get(batch_id)
    assert [(g["id"], g["photos"], g["include"]) for g in b.groups] == [("g1", [0, 1], True), ("g2", [2], False),
                                                                       ("g3", [3], True)]
    assert b.groups[0]["comment"] == "Akku fehlt" and b.groups[0]["product"] == "Bosch GSR 18V-55 solo"
    assert [g["quantity"] for g in b.groups] == [2, 4, None]     # the owner's count; the new group has none

    # re-sort: Claude sees the corrections and the hint; the comment survives
    client.post(f"/new-ad/batch/{batch_id}", data=dict([*form[:-1], ("include_g3", "1"), ("hint", "Foto 4 ist ein Kabel"),
                                                        ("action", "resort")]))
    b = _wait(batch_id, "review")
    prompt = fake.calls[-1]["prompt"]
    assert "Kommentar der Person: Akku fehlt" in prompt and "Foto 4 ist ein Kabel" in prompt
    assert "Bosch GSR 18V-55 solo (2 Stück)" in prompt           # Claude hears the corrected count
    assert b.groups[0]["comment"] == "Akku fehlt" and b.groups[1]["include"] is False  # carried by photo overlap
    assert [g["quantity"] for g in b.groups] == [2, 4]          # Claude's re-sort says 1 for the drill: the owner's 2 stays

    client.post(f"/new-ad/batch/{batch_id}", data=dict([*form[:-1], ("include_g3", "1"), ("action", "create")]))
    b = _wait(batch_id, "review")
    with dbmod.session() as db:
        drafts = list(db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id)))
    assert len(drafts) == 2 and all(d.batch_id == batch_id for d in drafts)
    assert "Akku fehlt" in drafts[0].input_text and "Artikel: Bosch GSR 18V-55 solo" in drafts[0].input_text
    assert [d.quantity for d in drafts] == [2, None]
    ad_prompts = [c["prompt"] for c in client.fake.calls if "Aufgabe: Erstelle das Inserat." in c["prompt"]]
    assert len(ad_prompts) == 2 and sum("Stückzahl: 2 (Preis pro Stück)" in p for p in ad_prompts) == 1
    assert f'name="{drafts[0].id}.quantity" data-key="quantity" min="1" max="999" inputmode="numeric" placeholder="1" value="2"' \
        in client.get(f"/new-ad/batch/{batch_id}?step=review").text
    assert "Gilt für alle meine Artikel: Abholung Wien 15" in drafts[0].input_text
    assert len(drafts[0].image_paths) == 2 and len(drafts[1].image_paths) == 1
    assert [g.get("draft_id") for g in b.groups] == [drafts[0].id, None, drafts[1].id]
    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert f'href="/new-ad/{drafts[0].id}"' in page and "KTM Kinderrad 20 Zoll blau" in page
    assert f'href="/new-ad/batch/{batch_id}"' in client.get(f"/new-ad/{drafts[0].id}").text

    # drafts are locked: a later save cannot move their photos, a re-sort only gets the rest
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g2", "action": "save"})
    assert ad_batch.get(batch_id).groups[0]["photos"] == [0, 1]
    client.post(f"/new-ad/batch/{batch_id}", data={"img_2": "g2", "action": "resort"})
    b = _wait(batch_id, "review")
    assert len(fake.calls[-1]["images"]) == 2                                    # photos 2 and 4
    assert b.groups[0]["draft_id"] == drafts[0].id and b.groups[1]["draft_id"] == drafts[1].id

    client.post(f"/new-ad/batch/{batch_id}/delete")
    assert ad_batch.get(batch_id) is None
    assert client.get(f"/new-ad/{drafts[0].id}").status_code == 200


def test_batch_cleared_count_means_one_item(client, monkeypatch):  # noqa: F811
    """Claude counts 4 chairs, the owner clears the field: one item, through a re-sort and into the draft."""
    fake = _sorting_backend(client, monkeypatch)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(3)]
    r = client.post("/new-ad/batch", files=photos, follow_redirects=False)
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    assert [g["quantity"] for g in _wait(batch_id, "review").groups] == [None, 4]

    form = {"img_0": "g1", "img_1": "g1", "quantity_g1": "", "img_2": "g2", "include_g2": "1", "quantity_g2": ""}
    client.post(f"/new-ad/batch/{batch_id}", data={**form, "action": "save"})
    assert [g["quantity"] for g in ad_batch.get(batch_id).groups] == [None, 1]   # no count stays none; a cleared one is 1
    assert 'name="quantity_g2" value="1"' in client.get(f"/new-ad/batch/{batch_id}").text

    client.post(f"/new-ad/batch/{batch_id}", data={**form, "action": "resort"})  # Claude says 4 again
    assert [g["quantity"] for g in _wait(batch_id, "review").groups] == [None, 1]

    fake.ad_extra = {"quantity": 4}                                    # the draft answer counts 4 as well
    client.post(f"/new-ad/batch/{batch_id}", data={**form, "action": "create"})
    _wait(batch_id, "review")
    with dbmod.session() as db:
        draft = db.exec(select(dbmod.AdDraft)).one()
    assert draft.quantity is None
    assert "Stückzahl: 1 – ein einzelnes Stück" in fake.calls[-1]["prompt"]


def test_batch_without_photos_and_recover(client):  # noqa: F811
    r = client.post("/new-ad/batch", data={"notes": "x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/new-ad?msg=")
    b = ad_batch.new_batch("")
    ad_batch._update(b.id, status="sorting")
    ad_batch.recover()
    assert ad_batch.get(b.id).status == "error"


def test_batch_table_review_refine_and_photos(client, monkeypatch):  # noqa: F811
    _sorting_backend(client, monkeypatch)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(3)]
    r = client.post("/new-ad/batch", files=photos, follow_redirects=False)
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    _wait(batch_id, "review")
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g1", "img_1": "g1", "include_g1": "1",
                                                   "img_2": "g2", "include_g2": "1", "action": "create"})
    _wait(batch_id, "review")
    assert client.get(f"/new-ad/batch/{batch_id}/progress").json() == {"status": "review", "drafts": 2}
    with dbmod.session() as db:
        d1, d2 = db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id)).all()

    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert "Review &amp; publish" in page and page.count('<tbody id="row-') == 2
    assert f'name="{d1.id}.title"' in page and "0</strong> of 2 published" in page

    url = f"/new-ad/batch/{batch_id}/drafts"
    r = client.post(url, data={f"{d1.id}.title": "Bohrschrauber", f"{d1.id}.description": "Top\r\nZustand",
                               f"{d1.id}.price": "45,50", "act": "save"})
    assert "Saved" in r.text and "<html" not in r.text
    with dbmod.session() as db:
        d = db.get(dbmod.AdDraft, d1.id)
    assert (d.title, d.description, d.price_suggestion) == ("Bohrschrauber", "Top\nZustand", 45.5)

    # refine saves unsaved edits first and goes through the normal refine (undo-able)
    r = client.post(url, data={f"{d1.id}.title": "Neu", f"{d1.id}.description": "x", f"{d1.id}.instruction": "kürzer",
                               "act": f"refine:{d1.id}"})
    assert "Claude revises 1 ads" in r.text
    wait_jobs()
    assert "Änderungswünsche: kürzer" in client.fake.calls[-1]["prompt"] and "- Titel: Neu" in client.fake.calls[-1]["prompt"]
    with dbmod.session() as db:
        assert db.get(dbmod.AdDraft, d1.id).revisions[-1]["title"] == "Neu"

    from app.services import ad_creator
    ad_creator.update_draft(d1.id, status="published")
    assert "1 discarded" in client.post(url, data={"sel": str(d2.id), "act": "discard"}).text
    assert "1</strong> of 2 published, 1 discarded" in client.get(f"/new-ad/batch/{batch_id}").text

    import io
    import zipfile
    r = client.get(f"/ad-drafts/{d1.id}/photos.zip")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert r.headers["content-type"] == "application/zip" and len(names) == 2 and names[0].endswith("-01.jpg")


def test_category_queries_order():
    from app.services.ad_creator import category_queries

    q = category_queries("Logitech Rechargeable Speaker S715i, schwarz, iPod-Dock", "Logitech S715i", "logitech s715i",
                         user_query="Lautsprecher")
    assert q == ["Lautsprecher", "Logitech Rechargeable Speaker S715i, schwarz, iPod-Dock",
                 "Logitech Rechargeable Speaker S715i", "Logitech Rechargeable Speaker", "Logitech S715i"]
    assert category_queries("Rad", None, "rad") == ["Rad"]
    assert category_queries("NRGkick 32A", None, None, None, "Auto > Elektromobilität/Ladestationen")[-1] \
        == "Elektromobilität Ladestationen"                          # the end of Claude's category hint, last


def test_failed_draft_is_retried_with_the_same_notes_and_photos(client, monkeypatch):  # noqa: F811
    """Claude down while the batch wrote its drafts: the group keeps its (empty) draft; "Try again" in its table row runs the
    whole pipeline again – text, comparable prices, Geizhals – on that draft, so the batch link stays."""
    from app.llm.base import LLMError
    from app.services import ad_creator

    fake = _sorting_backend(client, monkeypatch)
    sorting_aware = fake.complete
    down = [True]

    async def complete(**kw):
        if down[0] and "title" in kw["schema"]["properties"]:
            raise LLMError("Claude nicht erreichbar (529)")
        return await sorting_aware(**kw)
    monkeypatch.setattr(fake, "complete", complete)

    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(3)]
    r = client.post("/new-ad/batch", data={"notes": "Abholung Wien"}, files=photos, follow_redirects=False)
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    _wait(batch_id, "review")
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g1", "img_1": "g1", "include_g1": "1", "img_2": "",
                                                   "action": "create"})
    end = time.monotonic() + 5
    while not ad_batch.get(batch_id).groups[0].get("draft_id") and time.monotonic() < end:
        time.sleep(0.02)
    b = _wait(batch_id, "review")
    g = b.groups[0]
    with dbmod.session() as db:
        d = db.get(dbmod.AdDraft, g["draft_id"])
    assert (d.status, d.title, len(d.image_paths)) == ("error", None, 2) and "529" in d.error and "529" in g["error"]
    assert ad_batch.locked(g) and ad_batch.pending(b) == []                    # no second draft for the group

    assert f'"act": "retry:{d.id}"' in client.get(f"/new-ad/batch/{batch_id}").text
    assert f'action="/new-ad/{d.id}/retry"' in client.get(f"/new-ad/{d.id}").text

    down[0] = False
    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data={"act": f"retry:{d.id}"})
    assert "Claude writes 1 drafts again" in r.text
    assert f'data-pending-key="refining:{d.id}"' in r.text and "Claude writes…" in r.text
    assert f'"act": "retry:{d.id}"' not in r.text                             # no second click while it runs
    assert ad_creator.enqueue_retry(d.id) is False
    wait_jobs()
    with dbmod.session() as db:
        d = db.get(dbmod.AdDraft, d.id)
    assert (d.status, d.error, d.title) == ("draft", None, "KTM Kinderrad 20 Zoll blau")
    assert d.price_stats["count"] == 3 and len(d.comparables) == 3            # comparable ads were searched
    assert d.search_keywords == "ktm kinderrad 20" and len(d.image_paths) == 2 and d.batch_id == batch_id
    prompt = fake.calls[-1]["prompt"]
    assert "Abholung Wien" in prompt and len(fake.calls[-1]["images"]) == 2  # the same notes and photos
    assert ad_batch.get(batch_id).groups[0]["draft_id"] == d.id               # the batch still points at it
    row = client.get(f"/new-ad/batch/{batch_id}/row/{d.id}").text
    assert '"act": "retry:' not in row and "KTM Kinderrad 20 Zoll blau" in row and "data-pending-key" not in row
    assert ad_creator.enqueue_retry(d.id) is False                            # nothing to retry on a written draft


def _form(pairs):
    """Form pairs (a name may come more than once, like the ticks) as httpx wants them."""
    out = {}
    for k, v in pairs:
        out.setdefault(k, []).append(v)
    return out


def _opt(code, label, fields=()):
    return {"path": [{"label": "Audio", "code": "AUDIO"}, {"label": label, "code": code}], "label": f"Audio › {label}",
            "fields": [{"code": f, "label": f, "values": [["neu", "Neu"], ["gebraucht", "Gebraucht"]],
                        "selected": "gebraucht"} for f in fields]}


def _table_batch():
    """A batch with three written drafts: two prepared for willhaben, one not."""
    from app.services import ad_creator

    with dbmod.session() as db:
        ds = [dbmod.AdDraft(input_text="x", title=f"Artikel {i}", description="Gut.", price_suggestion=10 + i)
              for i in range(3)]
        db.add_all(ds)
        db.commit()
        ids = [d.id for d in ds]
        b = dbmod.AdBatch(status="review", groups=[{"id": f"g{i}", "photos": [], "draft_id": ids[i]} for i in range(3)])
        db.add(b)
        db.commit()
        db.refresh(b)
    ad_creator.update_draft(ids[0], wh_plan={"location": "1150 Wien", "options": [_opt("SPK", "Lautsprecher", ["Zustand"])]})
    ad_creator.update_draft(ids[1], wh_plan={"location": "1150 Wien", "options": [
        _opt("HIFI", "HiFi", ["Zustand"]), _opt("SPK", "Lautsprecher", ["Zustand"])]})
    return b.id, ids


def _row(i, **f):
    base = {"title": f"Artikel {i}", "description": "Gut.", "price": "10", "quantity": ""}
    return [(f"{i}.{k}", v) for k, v in {**base, **f}.items()]


def test_batch_table_saves_rows_and_willhaben_choice(client):  # noqa: F811
    batch_id, (a, b, c) = _table_batch()
    page = client.get(f"/new-ad/batch/{batch_id}").text
    assert page.count('<tbody id="row-') == 3 and 'id="bt"' in page and "data-copy" in page
    assert f'name="{a}.f:SPK:Zustand"' in page and f'name="{b}.f:HIFI:Zustand"' in page and "– no category yet –" in page
    assert '<option value="gebraucht" selected>Zustand: Gebraucht</option>' in page and "Audio › Lautsprecher" in page

    # what ⧉ produces for "Zustand" and "Handover": the same values in the other rows; one save stores all rows
    form = [*_row(a, price="45,50", quantity="2", cat="SPK", **{"f:SPK:Zustand": "neu"}, handover="shipping",
                  paylivery="1", parcel_size="M"),
            *_row(b, cat="SPK", **{"f:SPK:Zustand": "neu", "f:HIFI:Zustand": "bogus"}, handover="shipping",
                  parcel_size="M"),
            *_row(c, title="Neuer Titel", handover="pickup"), ("act", "save")]
    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form(form))
    assert r.status_code == 200 and 'id="bt-status"' in r.text and "Saved" in r.text and "<tbody" not in r.text
    with dbmod.session() as db:
        da, db_, dc = (db.get(dbmod.AdDraft, i) for i in (a, b, c))
    assert (da.price_suggestion, da.quantity) == (45.5, 2)
    assert da.wh_plan["selected"] == 0 and da.wh_plan["options"][0]["fields"][0]["selected"] == "neu"
    assert da.wh_plan["shipping"] == {"handover": "shipping", "paylivery": True, "size": "M", "carrier": None}
    assert db_.wh_plan["selected"] == 1 and db_.wh_plan["options"][1]["fields"][0]["selected"] == "neu"
    assert db_.wh_plan["options"][0]["fields"][0]["selected"] == "gebraucht"          # an unknown value is ignored
    assert db_.wh_plan["shipping"]["paylivery"] is False
    assert dc.title == "Neuer Titel" and dc.wh_plan == {"shipping": {"handover": "pickup"}}

    # an unreadable price: the row says so (out of band), the old price stays
    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*_row(a, price="viel"), ("act", "save")]))
    assert f'<span id="err-{a}"' in r.text and "Price not recognized" in r.text


def test_batch_table_copied_category_the_row_lacks(client, monkeypatch):  # noqa: F811
    """⧉ on the category puts a code into a row that never had it: it becomes a hand-picked option and the row is
    drawn anew (in a <template>, the way htmx swaps table parts out of band)."""
    from app.services import ad_creator

    batch_id, (a, b, c) = _table_batch()
    calls = []

    async def manual(draft_id, code):
        calls.append((draft_id, code))
        if code == "NOPE":
            raise ValueError("This category cannot be chosen")
        return ad_creator.update_draft(draft_id, wh_plan={"location": "1150 Wien", "selected": 0,
                                                           "options": [{**_opt(code, "Lautsprecher", ["Zustand"]), "manual": True}]})
    monkeypatch.setattr(ad_creator, "add_manual_category", manual)
    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*_row(a, cat="SPK"), *_row(c, cat="SPK"), ("act", "save")]))
    assert calls == [(c, "SPK")]                                        # row a has the option already
    assert re.search(rf'<template>\s*<tbody id="row-{c}"[^>]*hx-swap-oob="true"', r.text)
    assert f'name="{c}.f:SPK:Zustand"' in r.text

    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*_row(b, cat="NOPE"), ("act", "save")]))
    assert "This category cannot be chosen" in r.text

    # the row's own "Choose another category" search: the picked code, the row drawn anew
    r = client.post(f"/new-ad/batch/{batch_id}/drafts?act=category:{a}", data=_form([*_row(a, cat="SPK"), ("code", "TV")]))
    assert calls[-1] == (a, "TV") and f'<tbody id="row-{a}"' in r.text


def test_batch_table_publishes_and_prepares_the_ticked_rows(client, monkeypatch):  # noqa: F811
    from app.services import ad_creator

    batch_id, (a, b, c) = _table_batch()
    published = []
    monkeypatch.setattr(ad_creator, "enqueue_publish",
                        lambda draft_id, option, values, shipping=None, code=None: published.append((draft_id, option, shipping)) or True)
    form = [*_row(a, cat="SPK", handover="pickup"), *_row(b, cat="SPK", handover="both"), *_row(c, handover="pickup")]

    async def prepare(draft_id, query=None):
        return ad_creator.update_draft(draft_id, wh_plan={"location": "1150 Wien", "options": [_opt("SPK", "Lautsprecher")]})
    monkeypatch.setattr(ad_creator, "prepare_willhaben", prepare)
    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*form, ("sel", str(a)), ("sel", str(b)), ("sel", str(c)),
                                                              ("act", "publish")]))
    # c has no willhaben category yet: publishing asks willhaben for it first (10-30: it refused and asked nothing)
    assert "2 ads are being published one after another" in r.text
    assert "1 ad: willhaben categories first, then it is published" in r.text and "need attention" not in r.text
    assert len(re.findall(r"<template>\s*<tbody", r.text)) == 3
    assert f'name="sel" value="{a}" form="bt" checked' in r.text                 # the ticks stay
    wait_jobs()
    assert published == [(a, 0, {"handover": "pickup"}),
                         (b, 1, {"handover": "both", "paylivery": False, "size": None, "carrier": None}),
                         (c, 0, None)]

    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*form, ("act", f"prepare:{c}")]))
    assert "1 ad is being prepared" in r.text
    wait_jobs()
    row = client.get(f"/new-ad/batch/{batch_id}/row/{c}").text
    assert row.strip().startswith("<table>") and f'<tbody id="row-{c}"' in row and "data-pending-key" not in row
    assert f'name="{c}.cat"' in row and '<option value="SPK"' in row

    r = client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*form, ("sel", str(c)), ("act", "discard")]))
    assert "1 discarded" in r.text
    with dbmod.session() as db:
        assert db.get(dbmod.AdDraft, c).status == "discarded"
    client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([("act", f"restore:{c}")]))
    with dbmod.session() as db:
        assert db.get(dbmod.AdDraft, c).status == "draft"
    assert "No ad ticked" in client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([("act", "publish")])).text


def test_publishing_without_a_category_asks_willhaben_then_publishes(client, monkeypatch):  # noqa: F811
    """The real chain: the prepare job queues the publish job in the same lane, which runs after it; a draft
    discarded while its categories were fetched does not go live."""
    from app.services import ad_creator

    batch_id, (a, b, c) = _table_batch()
    went_live = []

    async def prepare(draft_id, query=None):
        return ad_creator.update_draft(draft_id, wh_plan={"location": "1150 Wien", "options": [_opt("SPK", "Lautsprecher")]})

    async def publish(did, option, values, stop_after=None, shipping=None, job=None, code=None):
        went_live.append((did, code))
        return ad_creator.update_draft(did, status="published", wh_ad_id="1000000009")
    monkeypatch.setattr(ad_creator, "prepare_willhaben", prepare)
    monkeypatch.setattr(ad_creator, "publish_willhaben", publish)
    client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*_row(c, handover="pickup"), ("sel", str(c)),
                                                              ("act", "publish")]))
    wait_jobs()
    assert went_live == [(c, "SPK")]
    with dbmod.session() as db:
        assert db.get(dbmod.AdDraft, c).status == "published" and not db.get(dbmod.AdDraft, c).error

    ad_creator.update_draft(b, wh_plan=None)

    async def prepare_then_discard(draft_id, query=None):
        await prepare(draft_id)
        return ad_creator.update_draft(draft_id, status="discarded")
    monkeypatch.setattr(ad_creator, "prepare_willhaben", prepare_then_discard)
    client.post(f"/new-ad/batch/{batch_id}/drafts", data=_form([*_row(b, handover="pickup"), ("sel", str(b)),
                                                              ("act", "publish")]))
    wait_jobs()
    assert went_live == [(c, "SPK")]


def test_written_drafts_are_prepared_for_willhaben_right_away(client, monkeypatch):  # noqa: F811
    """With a logged-in willhaben session the categories are asked for each written draft, without a click."""
    from app.services import ad_creator

    _sorting_backend(client, monkeypatch)
    asked = []
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: True)
    monkeypatch.setattr(ad_creator, "enqueue_prepare", lambda draft_id: asked.append(draft_id) or True)
    photos = [("photos", (f"IMG_{i}.jpg", b"jpeg-bytes-%d" % i, "image/jpeg")) for i in range(3)]
    r = client.post("/new-ad/batch", files=photos, follow_redirects=False)
    batch_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    _wait(batch_id, "review")
    client.post(f"/new-ad/batch/{batch_id}", data={"img_0": "g1", "img_1": "g1", "include_g1": "1",
                                                   "img_2": "g2", "include_g2": "1", "action": "create"})
    _wait(batch_id, "review")
    assert sorted(asked) == sorted(g["draft_id"] for g in ad_batch.get(batch_id).groups if g.get("draft_id"))
    assert len(asked) == 2


def test_batch_rows_mark_every_select_and_keep_the_tick(client):  # noqa: F811
    """A <select> without a selected option counts as edited for the page's refreshInPlace, so a row waiting for its
    job would never swap in; and the refreshed row keeps the tick the page sent."""
    from app.services import ad_creator

    batch_id, (a, b, c) = _table_batch()
    plan = ad_creator.update_draft(a).wh_plan
    plan["options"][0]["fields"][0]["selected"] = None                     # no guess for "Zustand"
    ad_creator.update_draft(a, wh_plan=plan)
    for i in (a, c):
        row = client.get(f"/new-ad/batch/{batch_id}/row/{i}").text
        selects = re.findall(r"<select.*?</select>", row, re.S)
        assert selects and all(" selected" in sel for sel in selects), [sel[:80] for sel in selects if " selected" not in sel]
    assert f'value="{c}" form="bt" checked' in client.get(f"/new-ad/batch/{batch_id}/row/{c}").text
    assert f'value="{c}" form="bt" checked' not in client.get(f"/new-ad/batch/{batch_id}/row/{c}?checked=0").text


def test_category_search_of_a_row_without_category_starts_with_claudes_hint(client):  # noqa: F811
    from app.services import ad_creator

    batch_id, (a, b, c) = _table_batch()
    ad_creator.update_draft(c, category_hint="Auto/Motorrad > Autozubehör > Elektromobilität/Ladestationen")
    ad_creator.update_draft(a, category_hint="Audio > Lautsprecher")
    assert 'data-catq autocomplete="off" value="Elektromobilität Ladestationen"' in \
        client.get(f"/new-ad/batch/{batch_id}/row/{c}").text
    assert 'data-catq autocomplete="off" value=""' in client.get(f"/new-ad/batch/{batch_id}/row/{a}").text  # has one
