"""An ad that is no longer online (sold, deleted, removed as a scam): the viewer shows the copy the app kept – from a
chat about it (text, photos, seller as Claude saw them), else from its hit – with a note, not just "no longer
online"."""
from fastapi.testclient import TestClient

from app import platforms
from app.db import Conversation, session, utcnow


def test_a_deleted_ad_shows_the_kept_copy(monkeypatch):
    from app.main import app

    async def gone(ad_id, url=None):
        return None
    monkeypatch.setattr(platforms.get("kleinanzeigen"), "detail", gone)
    monkeypatch.setattr(platforms, "enabled", lambda pid: True)
    with session() as db:
        db.add(Conversation(id="ka:off:1", platform="kleinanzeigen", ad_id="3100000099", ad_context_at=utcnow(),
                            ad_context={"title": "UniFi Gateway, neu", "price": "160 €", "description": "Originalverpackt.",
                                        "location": "04275 Leipzig", "seller_name": "Verkäufer", "platform": "kleinanzeigen",
                                        "images": ["https://img.example/a.jpg"]}))
        db.commit()
    with TestClient(app) as c:
        html = c.get("/hits/ad/kleinanzeigen/3100000099").text
    assert "data-ad-offline" in html and "UniFi Gateway, neu" in html and "Originalverpackt." in html
    assert "https://img.example/a.jpg" in html


def test_without_a_copy_it_still_says_no_longer_online(monkeypatch):
    from app.main import app

    async def gone(ad_id, url=None):
        return None
    monkeypatch.setattr(platforms.get("kleinanzeigen"), "detail", gone)
    monkeypatch.setattr(platforms, "enabled", lambda pid: True)
    with TestClient(app) as c:
        html = c.get("/hits/ad/kleinanzeigen/3100000098").text
    assert "no longer online" in html.lower() and "data-ad-offline" not in html


def test_from_context_reads_the_price_text():
    d = platforms.AdDetail.from_context({"title": "x", "price": "1.250 €"}, "1", "kleinanzeigen")
    assert d.price == 1250.0 and d.status == "gone"
