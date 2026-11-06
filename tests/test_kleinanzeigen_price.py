"""The own ad's price on Kleinanzeigen (services/kleinanzeigen_price.py): the comparables there, Claude's price and note,
the median when Claude is out, nothing when the platform is off; stored with the draft at write time and by the
Recalculate button as a job; a failing analysis never breaks the draft."""
import pytest

from app import db as dbmod
from app.services import ad_creator, kleinanzeigen_price
from tests.conftest import wait_jobs
from tests.fake_kleinanzeigen import AMOUNTS, AUTH, FakeClaude, disable, enable
from tests.test_dashboard import client  # noqa: F401  (dashboard with the fake Claude backend)

KEYWORDS = "ktm kinderrad 20"


@pytest.fixture(autouse=True)
def _off_again():
    yield
    disable()


def _draft(**kw):
    fields = {"input_text": "x", "title": "KTM Kinderrad 20 Zoll", "description": "Gut erhalten.", "price_suggestion": 120,
              "condition": "gebraucht", "search_keywords": KEYWORDS, "parcel": {"weight_kg": 2.5}, **kw}
    with dbmod.session() as db:
        d = dbmod.AdDraft(**fields)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def _get(draft_id):
    with dbmod.session() as db:
        return db.get(dbmod.AdDraft, draft_id)


# ------------------------------------------------------------------ the analysis
async def test_claude_price_and_note_win_over_the_median(monkeypatch):
    transport = enable(monkeypatch, AMOUNTS + [0, None])            # a giveaway and a "VB" without amount: not compared
    backend = FakeClaude({"price": 95.4, "note": "Auf Kleinanzeigen liegt der Markt etwas tiefer."})
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: backend)
    res = await kleinanzeigen_price.analyse("KTM Kinderrad 20 Zoll", KEYWORDS, condition="gebraucht", shipping=True,
                                            weight_kg=2.5, willhaben_price=120)
    assert res["price"] == 95 and res["note"] == "Auf Kleinanzeigen liegt der Markt etwas tiefer."
    stats = res["price_stats"]
    assert stats["count"] == 7 and stats["median"] == 120 and stats["llm_estimate"] == 95 and stats["rows_found"] == 40
    assert stats["p25"] < stats["median"] < stats["p75"] and stats["min"] == 60 and stats["max"] == 240
    assert len(res["comparables"]) == 7 and res["comparables"][0]["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/")
    assert res["search_keywords"] == KEYWORDS and "16–20" in res["shipping_hint"]          # 2.5 kg: the 4 kg class
    assert res["at"] and transport.calls and "ads.json" in transport.calls[0]
    prompt = backend.prompts[0]
    assert "Preis bei willhaben (Österreich): 120 €" in prompt and "Paketgewicht: 2.5 kg" in prompt
    assert "Versand: angeboten" in prompt and "Zustand: gebraucht" in prompt and "7 mit Preis" in prompt


async def test_without_claude_the_median_is_the_price(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: FakeClaude(error="claude is out"))
    res = await kleinanzeigen_price.analyse("KTM Kinderrad", KEYWORDS, willhaben_price=120)
    assert res["price"] == 120 and res["note"] is None and res["price_stats"]["llm_estimate"] is None


async def test_too_few_comparables_give_no_price_and_no_claude_call(monkeypatch):
    enable(monkeypatch, [60, 90, 0, None])
    backend = FakeClaude({"price": 80, "note": "x"})
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: backend)
    res = await kleinanzeigen_price.analyse("KTM Kinderrad", KEYWORDS, willhaben_price=120)
    assert res["price"] is None and res["price_stats"]["count"] == 2 and res["comparables"][0]["price"] == 60
    assert backend.prompts == []


async def test_nothing_runs_when_the_platform_is_off_or_has_no_credentials(monkeypatch):
    transport = enable(monkeypatch)
    from app.db import set_setting
    set_setting("platform_kleinanzeigen_enabled", "0")
    assert await kleinanzeigen_price.analyse("x", KEYWORDS) is None and transport.calls == []
    set_setting("platform_kleinanzeigen_enabled", "1")
    set_setting("kleinanzeigen_api_auth", "")
    assert await kleinanzeigen_price.analyse("x", KEYWORDS) is None and transport.calls == []
    set_setting("kleinanzeigen_api_auth", AUTH)
    assert await kleinanzeigen_price.analyse("", "") is None                 # nothing to search for


def test_shipping_hint_by_weight_class():
    assert "11–16" in kleinanzeigen_price.shipping_hint(1.5)
    assert "16–20" in kleinanzeigen_price.shipping_hint(4)
    assert "20–25" in kleinanzeigen_price.shipping_hint(9.9)
    assert "over 10 kg" in kleinanzeigen_price.shipping_hint(12) and "25 €" in kleinanzeigen_price.shipping_hint(12)
    assert "11–25" in kleinanzeigen_price.shipping_hint(None)


def test_heading_keywords_leave_numbers_out():
    assert kleinanzeigen_price.heading_keywords("Rennrad 2026 Canyon Endurace CF 8 Größe 56") == "Rennrad Canyon Endurace CF Größe"
    assert kleinanzeigen_price.heading_keywords(None) == ""


# ------------------------------------------------------------------ the draft
def test_writing_a_draft_stores_the_kleinanzeigen_analysis(client, monkeypatch):  # noqa: F811
    enable(monkeypatch)
    backend = FakeClaude({"price": 95, "note": "Etwas günstiger als in Österreich."})
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: backend)
    d = _draft(title=None, description=None, price_suggestion=None, condition=None)
    client.portal.call(ad_creator._write_draft, d.id, "KTM Kinderrad 20 Zoll blau", [])
    wait_jobs()                                  # the analysis is a quiet job the draft does not wait for
    ka = _get(d.id).kleinanzeigen
    assert ka["price"] == 95 and ka["note"] == "Etwas günstiger als in Österreich." and ka["search_keywords"] == "ktm kinderrad 20"
    assert _get(d.id).status == "draft" and _get(d.id).title == "KTM Kinderrad 20 Zoll blau"


def test_no_analysis_is_queued_while_kleinanzeigen_is_off(client, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(ad_creator, "enqueue_kleinanzeigen_price", lambda draft_id: seen.append(draft_id) or True)
    d = _draft(title=None, description=None, price_suggestion=None, condition=None)
    client.portal.call(ad_creator._write_draft, d.id, "KTM Kinderrad 20 Zoll blau", [])
    assert seen == []                            # off (the default): no job, no search, no Claude call
    enable(monkeypatch)
    client.portal.call(ad_creator._write_draft, d.id, "KTM Kinderrad 20 Zoll blau", [])
    assert seen == [d.id]


def test_a_failing_analysis_keeps_the_draft(client, monkeypatch):  # noqa: F811
    async def broken(*a, **k):
        raise RuntimeError("the search is down")

    monkeypatch.setattr(kleinanzeigen_price, "analyse", broken)
    d = _draft(title=None, description=None, price_suggestion=None, condition=None)
    client.portal.call(ad_creator._write_draft, d.id, "KTM Kinderrad 20 Zoll blau", [])
    wait_jobs()
    assert _get(d.id).title == "KTM Kinderrad 20 Zoll blau" and _get(d.id).kleinanzeigen is None
    assert _get(d.id).error is None


def test_recalculate_is_a_job_that_stores_the_price(client, monkeypatch):  # noqa: F811
    enable(monkeypatch)
    backend = FakeClaude({"price": 110, "note": "Neu berechnet."})
    monkeypatch.setattr(kleinanzeigen_price, "get_backend", lambda *a, **k: backend)
    d = _draft()
    r = client.post(f"/new-ad/{d.id}/kleinanzeigen/price", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/new-ad/{d.id}#willhaben"
    wait_jobs()
    assert _get(d.id).kleinanzeigen["price"] == 110 and _get(d.id).kleinanzeigen["note"] == "Neu berechnet."


def test_recalculate_refuses_a_published_draft(client, monkeypatch):  # noqa: F811
    enable(monkeypatch)
    d = _draft(status="published", wh_ad_id="1000000009")
    assert ad_creator.enqueue_kleinanzeigen_price(d.id) is False
    client.post(f"/new-ad/{d.id}/kleinanzeigen/price", follow_redirects=False)
    wait_jobs()
    assert _get(d.id).kleinanzeigen is None
