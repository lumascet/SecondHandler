"""The posting flow: "also on Kleinanzeigen" on the single draft and in the batch table, the publish choices kept on
the draft, the categories asked right after Claude wrote a draft, and "publish as soon as Claude is done"."""
import pytest
from sqlmodel import select

from app import db as dbmod
from app.db import set_setting
from app.services import ad_batch, ad_creator, crosspost_kleinanzeigen
from tests.conftest import wait_jobs
from tests.test_ad_batch import _form, _opt, _row, _table_batch  # noqa: F401  (helpers of the batch table tests)
from tests.test_dashboard import client  # noqa: F401  (dashboard with the fake Claude backend)
from tests.test_publish_page import _draft, _get, wh  # noqa: F401  (fake willhaben)

PUBLISH = {"option": "0", "handover": "pickup", "title": "Logitech S125i", "description": "Gut erhalten.",
           "price": "12"}


def _kleinanzeigen(monkeypatch, available: bool, queued: list | None = None):
    monkeypatch.setattr(crosspost_kleinanzeigen, "available", lambda: available)
    if queued is not None:
        monkeypatch.setattr(crosspost_kleinanzeigen, "enqueue", lambda ad_id: queued.append(ad_id) or True)


def _prepared_draft(client, **kw):  # noqa: F811
    d = _draft(**kw)
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    return d


def _head(page: str) -> str:
    """The draft page's head: the buttons above the error line."""
    return page.split('id="draft-error"')[0]


# ------------------------------------------------------------------ the single draft
def test_kleinanzeigen_box_on_the_publish_card_follows_login_and_setting(client, wh, monkeypatch):  # noqa: F811
    d = _prepared_draft(client)
    _kleinanzeigen(monkeypatch, available=False)
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="kleinanzeigen"' not in page and "Publish on willhaben now" in page

    _kleinanzeigen(monkeypatch, available=True)
    set_setting("kleinanzeigen_post_default", "0")
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="kleinanzeigen" value="1" checked' not in page and page.count('name="kleinanzeigen"') == 1
    assert "Also post on Kleinanzeigen" in page and "Claude picks the category" in page
    set_setting("kleinanzeigen_post_default", "1")
    assert 'name="kleinanzeigen" value="1" checked' in client.get(f"/new-ad/{d.id}").text
    ad_creator.update_draft(d.id, also={"kleinanzeigen": False})     # the draft's own choice wins over the setting
    assert 'name="kleinanzeigen" value="1" checked' not in client.get(f"/new-ad/{d.id}").text


def test_publish_with_the_box_stores_the_choice_and_queues_the_copy(client, wh, monkeypatch):  # noqa: F811
    queued = []
    _kleinanzeigen(monkeypatch, available=True, queued=queued)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen": "1", "handover": "pickup"})
    wait_jobs()
    assert _get(d.id).status == "published" and _get(d.id).also == {"kleinanzeigen": True}
    assert queued == ["1000000001"]                       # the willhaben ad id, once the willhaben publish is done

    d2 = _prepared_draft(client)
    client.post(f"/new-ad/{d2.id}/willhaben/publish", data=PUBLISH)          # box not ticked
    wait_jobs()
    assert _get(d2.id).status == "published" and _get(d2.id).also == {"kleinanzeigen": False}
    assert queued == ["1000000001"]


def test_a_failing_kleinanzeigen_copy_never_fails_the_willhaben_publish(client, wh, monkeypatch):  # noqa: F811
    def broken(ad_id):
        raise RuntimeError("Kleinanzeigen not reachable")
    _kleinanzeigen(monkeypatch, available=True)
    monkeypatch.setattr(crosspost_kleinanzeigen, "enqueue", broken)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen": "1"})
    wait_jobs()
    assert _get(d.id).status == "published" and _get(d.id).error is None


def test_draft_head_has_no_save_and_puts_the_publish_first(client, wh, monkeypatch):  # noqa: F811
    _kleinanzeigen(monkeypatch, available=False)
    d = _prepared_draft(client)
    ad_creator.update_draft(d.id, status="error", error="Claude broke")
    head = _head(client.get(f"/new-ad/{d.id}").text)
    assert ">Save</button>" not in head
    assert head.index('form="wh-publish"') < head.index("Try again")

    ad_creator.update_draft(d.id, status="published", wh_ad_id="1000000001", error=None)
    head = _head(client.get(f"/new-ad/{d.id}").text)
    assert head.index('href="/myads?ad=1000000001"') < head.index("View on willhaben") < head.index("Edit on willhaben")


# ------------------------------------------------------------------ the batch table
def test_batch_bar_order_and_box(client, monkeypatch):  # noqa: F811
    from pathlib import Path
    _kleinanzeigen(monkeypatch, available=True)
    batch_id, _ = _table_batch()
    page = client.get(f"/new-ad/batch/{batch_id}").text
    bar = page[page.index('id="bt"'):]
    bar = bar[:bar.index("</form>")]
    order = [bar.index(s) for s in ('data-selall', "Publish ticked", 'name="kleinanzeigen"', "Check categories",
                                    "Revise", "Discard ticked")]
    assert order == sorted(order)
    _kleinanzeigen(monkeypatch, available=False)
    assert 'name="kleinanzeigen"' not in client.get(f"/new-ad/batch/{batch_id}").text
    source = Path("app/templates/_batch_table.html").read_text(encoding="utf-8")
    assert "hx-confirm" not in source                    # the grace job and Restore make the confirms unneeded


def test_batch_publish_passes_the_box_on_to_each_publish(client, monkeypatch):  # noqa: F811
    batch_id, (a, b, c) = _table_batch()
    calls = []
    monkeypatch.setattr(ad_creator, "enqueue_publish",
                        lambda draft_id, option, values, shipping=None, code=None, also=None:
                        calls.append((draft_id, also)) or True)
    rows = [*_row(a, cat="SPK", handover="pickup"), *_row(b, cat="SPK", handover="pickup")]
    url = f"/new-ad/batch/{batch_id}/drafts"
    client.post(url, data=_form([*rows, ("sel", str(a)), ("sel", str(b)), ("act", "publish"), ("kleinanzeigen", "1")]))
    assert calls == [(a, {"kleinanzeigen": True}), (b, {"kleinanzeigen": True})]
    client.post(url, data=_form([*rows, ("sel", str(a)), ("act", "publish")]))
    assert calls[-1] == (a, {"kleinanzeigen": False})


# ------------------------------------------------------------------ Claude's draft: categories and publishing
def _prepare_fake(monkeypatch, mode: str = "plain", calls: list | None = None):
    async def prepare(draft_id, query=None):
        if calls is not None:
            calls.append(draft_id)
        if mode == "question":
            ad_creator.update_draft(draft_id, questions=[{"id": "q1", "text": "Farbe?", "options": ["rot", "blau"]}])
        if mode == "bitte":
            d = ad_creator.update_draft(draft_id)
            ad_creator.update_draft(draft_id, description=f"{d.description} (bitte prüfen)")
        options = [] if mode == "nocat" else [_opt("SPK", "Lautsprecher")]
        return ad_creator.update_draft(draft_id, wh_plan={"location": "1150 Wien", "options": options})
    monkeypatch.setattr(ad_creator, "prepare_willhaben", prepare)


def _went_live(monkeypatch) -> list:
    live = []

    async def publish(did, option, values, stop_after=None, shipping=None, job=None, code=None):
        live.append((did, code))
        return ad_creator.update_draft(did, status="published", wh_ad_id="1000000009")
    monkeypatch.setattr(ad_creator, "publish_willhaben", publish)
    return live


def test_a_written_draft_gets_its_categories_at_once(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: True)
    asked = []
    _prepare_fake(monkeypatch, calls=asked)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [], None, {"kleinanzeigen": False, "auto_publish": False})
    wait_jobs()
    assert asked == [d.id] and _get(d.id).wh_plan["options"][0]["label"] == "Audio › Lautsprecher"
    assert _get(d.id).status == "draft" and _get(d.id).also == {"kleinanzeigen": False, "auto_publish": False}


def test_not_logged_in_to_willhaben_nothing_is_asked_after_writing(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: False)
    asked = []
    _prepare_fake(monkeypatch, calls=asked)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [], None, {"auto_publish": True})
    wait_jobs()
    assert asked == [] and _get(d.id).wh_plan is None
    assert _get(d.id).also == {"auto_publish": False, "auto_publish_skipped": "willhaben_not_logged_in"}   # said, not silent
    assert "willhaben is not logged in" in client.get(f"/new-ad/{d.id}").text


def test_auto_publish_goes_live_with_the_first_category(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: True)
    _prepare_fake(monkeypatch)
    live = _went_live(monkeypatch)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [], None, {"kleinanzeigen": False, "auto_publish": True})
    wait_jobs()
    assert live == [(d.id, "SPK")]
    assert _get(d.id).status == "published" and _get(d.id).error is None
    assert _get(d.id).also == {"kleinanzeigen": False, "auto_publish": True}      # kept: it documents what was asked


@pytest.mark.parametrize("mode,reason", [("question", "questions"), ("bitte", "bitte_pruefen"),
                                         ("nocat", "no_category")])
def test_auto_publish_is_skipped_and_says_why(client, monkeypatch, mode, reason):  # noqa: F811
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: True)
    _prepare_fake(monkeypatch, mode=mode)
    live = _went_live(monkeypatch)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [], None, {"kleinanzeigen": False, "auto_publish": True})
    wait_jobs()
    assert live == []
    row = _get(d.id)
    assert row.status == "draft" and row.error is None and row.publishing is None
    assert row.also == {"kleinanzeigen": False, "auto_publish": False, "auto_publish_skipped": reason}
    page = client.get(f"/new-ad/{d.id}").text
    assert page.index("Not published by itself") < page.index('id="wh-publish"')    # above the publish card


def test_a_draft_needing_a_required_value_is_left_for_review(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ad_batch, "_willhaben_ready", lambda: True)

    async def prepare(draft_id, query=None):
        opt = _opt("SPK", "Lautsprecher", fields=["Zustand"])
        opt["fields"][0]["selected"] = None                 # no default: a value is needed
        return ad_creator.update_draft(draft_id, wh_plan={"location": "1150 Wien", "options": [opt]})
    monkeypatch.setattr(ad_creator, "prepare_willhaben", prepare)
    live = _went_live(monkeypatch)
    d = client.portal.call(ad_creator.enqueue_create, "Stuhl", [], None, {"auto_publish": True})
    wait_jobs()
    assert live == [] and _get(d.id).also["auto_publish_skipped"] == "required"


def test_new_ad_form_stores_the_choices_and_offers_them(client, monkeypatch):  # noqa: F811
    _kleinanzeigen(monkeypatch, available=True)
    set_setting("kleinanzeigen_post_default", "1")
    page = client.get("/new-ad").text
    assert 'name="auto_publish" value="1"' in page and 'name="kleinanzeigen" value="1" checked' in page
    assert 'name="auto_publish" value="1" checked' not in page
    _kleinanzeigen(monkeypatch, available=False)
    assert 'name="kleinanzeigen"' not in client.get("/new-ad").text and 'name="auto_publish"' in client.get("/new-ad").text

    client.post("/new-ad", data={"notes": "Stuhl", "auto_publish": "1", "kleinanzeigen": "1"}, follow_redirects=False)
    wait_jobs()
    with dbmod.session() as db:
        d = db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id.desc())).first()
    # not logged in to willhaben here: the auto-publish is not done, and the draft says why
    assert d.also == {"kleinanzeigen": True, "auto_publish": False, "auto_publish_skipped": "willhaben_not_logged_in"}
    client.post("/new-ad", data={"notes": "Tisch"}, follow_redirects=False)
    wait_jobs()
    with dbmod.session() as db:
        d = db.exec(select(dbmod.AdDraft).order_by(dbmod.AdDraft.id.desc())).first()
    assert d.also == {"kleinanzeigen": False, "auto_publish": False}


def test_mini_app_shows_the_same_boxes(client, wh, monkeypatch):  # noqa: F811
    _kleinanzeigen(monkeypatch, available=True)
    assert 'name="auto_publish"' in client.get("/tg/new-ad").text and 'name="kleinanzeigen"' in client.get("/tg/new-ad").text
    d = _prepared_draft(client)
    assert 'name="kleinanzeigen"' in client.get(f"/tg/new-ad/{d.id}").text
    _kleinanzeigen(monkeypatch, available=False)
    assert 'name="kleinanzeigen"' not in client.get(f"/tg/new-ad/{d.id}").text


# ------------------------------------------------------------------ the Kleinanzeigen price on the card
KA_STATS = {"count": 7, "median": 120.0, "p25": 90.0, "p75": 180.0, "min": 60.0, "max": 240.0, "rows_found": 40,
            "llm_estimate": 95}
KA_PRICE = {"price": 95, "price_stats": KA_STATS, "search_keywords": "logitech s125i", "note": "Am Markt etwas tiefer.",
            "comparables": [], "shipping_hint": "Shipping to Germany costs more.", "at": "2026-11-01T10:00:00"}


def test_the_card_shows_the_kleinanzeigen_price_with_its_comparables(client, wh, monkeypatch):  # noqa: F811
    d = _prepared_draft(client, kleinanzeigen=KA_PRICE)
    _kleinanzeigen(monkeypatch, available=True)
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="kleinanzeigen_price" id="ka-price-field" value="95"' in page
    assert "7 ads on Kleinanzeigen for “logitech s125i”" in page and "Median € 120" in page
    assert "Am Markt etwas tiefer." in page and "Shipping to Germany costs more than at home: about 11–25 €" in page
    assert 'style="left:' in page and "7 comparable ads" in page                  # the gauge among the comparables
    assert 'id="wh-kaprice"' in page and 'form="wh-kaprice"' in page and "Recalculate" in page
    assert page.index('name="kleinanzeigen"') < page.index('name="kleinanzeigen_price"')    # under the box


def test_without_an_analysis_the_field_starts_at_the_willhaben_price(client, wh, monkeypatch):  # noqa: F811
    d = _prepared_draft(client, also={"kleinanzeigen": True, "kleinanzeigen_price": 88.5})
    _kleinanzeigen(monkeypatch, available=True)
    assert 'name="kleinanzeigen_price" id="ka-price-field" value="88.5"' in client.get(f"/new-ad/{d.id}").text
    d2 = _prepared_draft(client)
    page = client.get(f"/new-ad/{d2.id}").text
    assert 'name="kleinanzeigen_price" id="ka-price-field" value="12"' in page        # the willhaben price
    assert "Too few comparable ads" not in page


def test_the_mini_app_shows_the_kleinanzeigen_price_field(client, wh, monkeypatch):  # noqa: F811
    d = _prepared_draft(client, kleinanzeigen=KA_PRICE)
    _kleinanzeigen(monkeypatch, available=True)
    page = client.get(f"/tg/new-ad/{d.id}").text
    assert 'name="kleinanzeigen_price"' in page and 'value="95"' in page and 'id="wh-kaprice"' in page
    assert "Recalculate" in page


def test_publish_stores_the_kleinanzeigen_price_typed_on_the_card(client, wh, monkeypatch):  # noqa: F811
    queued = []
    _kleinanzeigen(monkeypatch, available=True, queued=queued)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen": "1", "kleinanzeigen_price": "99,5"})
    wait_jobs()
    assert _get(d.id).also == {"kleinanzeigen": True, "kleinanzeigen_price": 99.5} and queued == ["1000000001"]


def test_a_blank_kleinanzeigen_price_leaves_the_analysis_price_to_the_posting(client, wh, monkeypatch):  # noqa: F811
    queued = []
    _kleinanzeigen(monkeypatch, available=True, queued=queued)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen": "1", "kleinanzeigen_price": ""})
    wait_jobs()
    assert _get(d.id).also == {"kleinanzeigen": True, "kleinanzeigen_price": None} and queued == ["1000000001"]


def test_an_unreadable_kleinanzeigen_price_publishes_nothing(client, wh, monkeypatch):  # noqa: F811
    queued = []
    _kleinanzeigen(monkeypatch, available=True, queued=queued)
    d = _prepared_draft(client)
    r = client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen": "1", "kleinanzeigen_price": "zwölf"},
                    follow_redirects=False)
    wait_jobs()
    assert r.status_code == 303 and r.headers["location"].endswith("#willhaben")
    assert _get(d.id).status == "draft" and queued == [] and "not recognized" in (_get(d.id).error or "")


def test_an_unticked_box_ignores_a_stray_kleinanzeigen_price(client, wh, monkeypatch):  # noqa: F811
    """The Mini App sends the price field whether or not the box is ticked: unticked, a text there must not hold up
    the willhaben publish."""
    queued = []
    _kleinanzeigen(monkeypatch, available=True, queued=queued)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={**PUBLISH, "kleinanzeigen_price": "zwölf"})
    wait_jobs()
    assert _get(d.id).status == "published" and queued == [] and not _get(d.id).error
    assert _get(d.id).also == {"kleinanzeigen": False}


def test_recalculate_keeps_the_typed_kleinanzeigen_price(client, wh, monkeypatch):  # noqa: F811
    _kleinanzeigen(monkeypatch, available=True)
    monkeypatch.setattr(ad_creator, "enqueue_kleinanzeigen_price", lambda draft_id: True)
    d = _prepared_draft(client)
    client.post(f"/new-ad/{d.id}/kleinanzeigen/price", data={**PUBLISH, "kleinanzeigen_price": "77"})
    assert _get(d.id).also.get("kleinanzeigen_price") == 77.0
    page = client.get(f"/new-ad/{d.id}").text
    assert 'id="wh-kaprice"' in page and 'hx-include="#title, #desc, #price, #quantity, #ka-price-field"' in page
    client.post(f"/new-ad/{d.id}/kleinanzeigen/price", data={**PUBLISH, "kleinanzeigen_price": "zwölf"})
    assert _get(d.id).also.get("kleinanzeigen_price") == 77.0        # unreadable: the typed price before it stays
