"""Reading specs a seller hid in a photo: the OCR text cleaner, the thin-text gate, and that screening fills and
stores hit.photo_text and carries it into the judge's prompt."""
import pytest

from app import db as dbmod
from app.db import session, set_setting
from app.llm.prompts import screen_prompt
from app.services import ad_ocr, screening


def test_clean_keeps_text_drops_noise_and_dedupes():
    raw = "AMD Ryzen 5 7520U\n|||  ~~~  |||\n16 GB LPDDR5\n16 GB LPDDR5\n\n.\n512 GB SSD"
    out = ad_ocr._clean(raw)
    assert "AMD Ryzen 5 7520U" in out
    assert "16 GB LPDDR5" in out and out.count("16 GB LPDDR5") == 1   # deduped
    assert "512 GB SSD" in out
    assert "|||" not in out and "~~~" not in out                      # symbol noise dropped


def test_clean_caps_length():
    assert len(ad_ocr._clean("Ryzen 7520U " * 200)) <= ad_ocr.MAX_CHARS


def test_thin_text_gate():
    assert screening._thin_text(dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u",
                                          body="HP · Sehr gut · 525,70 € inkl. Käuferschutz"))
    assert not screening._thin_text(dbmod.Hit(agent_id=1, ad_id="1", heading="x", url="u",
                                              body="Acer Aspire C24. Technische Daten: Modell, Prozessor Intel i5, "
                                                   "16 GB RAM, 512 GB SSD, Windows 11, kaum benutzt, Rechnung dabei."))


class _FakeDetail:
    images = ["https://cdn/p1.jpg", "https://cdn/p5.jpg", "https://cdn/p8.jpg"]


class _FakePlatform:
    def can(self, cap):
        return cap == "detail"

    async def detail(self, ad_id, *, url=None):
        return _FakeDetail()


def _store(hit):
    with session() as db:
        db.add(hit)
        db.commit()
        db.refresh(hit)
    return hit


@pytest.mark.asyncio
async def test_read_photo_text_fills_and_persists(monkeypatch):
    monkeypatch.setattr(ad_ocr, "HAVE_TESSERACT", True)
    monkeypatch.setattr(screening.registry, "get", lambda pid: _FakePlatform())

    async def fake_read(urls, folder, *, platform=None, transport=None):
        assert urls == _FakeDetail.images        # the whole photo set reaches the OCR, not just the cover
        return "AMD Ryzen 5 7520U\n16 GB LPDDR5\n512 GB SSD"

    monkeypatch.setattr(ad_ocr, "read_photos", fake_read)
    hit = _store(dbmod.Hit(agent_id=1, platform="vinted", ad_id="900001", heading="HP All in one",
                           url="u", body="HP · Sehr gut · 525 €"))
    await screening.read_photo_text([hit])
    assert "Ryzen 5 7520U" in (hit.photo_text or "")
    with session() as db:                        # persisted, so a re-screen does not OCR again
        assert "Ryzen 5 7520U" in (db.get(dbmod.Hit, hit.id).photo_text or "")
    assert hit.photo_text in screen_prompt(agent_name="x", brief="PC", language="Deutsch", stats=None,
                                           hits=[{"nr": 1, "title": "HP", "price": "525 €", "location": "Wien",
                                                  "private": True, "photo_text": hit.photo_text}])


@pytest.mark.asyncio
async def test_read_photo_text_respects_toggle_and_tesseract(monkeypatch):
    monkeypatch.setattr(screening.registry, "get", lambda pid: _FakePlatform())
    monkeypatch.setattr(ad_ocr, "read_photos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should skip")))
    hit = _store(dbmod.Hit(agent_id=1, platform="vinted", ad_id="900002", heading="x", url="u", body="HP · 1 €"))

    monkeypatch.setattr(ad_ocr, "HAVE_TESSERACT", False)      # no OCR binary: skip, leave it untried
    await screening.read_photo_text([hit])
    assert hit.photo_text is None

    monkeypatch.setattr(ad_ocr, "HAVE_TESSERACT", True)
    set_setting("read_photo_text", "0")                      # turned off by the owner
    await screening.read_photo_text([hit])
    assert hit.photo_text is None


@pytest.mark.asyncio
async def test_rich_text_is_not_ocred(monkeypatch):
    monkeypatch.setattr(ad_ocr, "HAVE_TESSERACT", True)
    monkeypatch.setattr(screening.registry, "get", lambda pid: _FakePlatform())
    monkeypatch.setattr(ad_ocr, "read_photos", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rich: skip")))
    hit = _store(dbmod.Hit(agent_id=1, platform="willhaben", ad_id="900003", heading="x", url="u",
                           body="Acer Aspire C24 All-in-One. Prozessor Intel i5-1335U, 16 GB RAM, 512 GB SSD, "
                                "Windows 11 Home, kaum benutzt, Rechnung und Garantie dabei, Abholung in Wien."))
    await screening.read_photo_text([hit])
    assert hit.photo_text is None
