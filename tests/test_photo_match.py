"""The same photo in another ad (services/photo_match): a re-listing on the same site and place is information, a
copy on another site or elsewhere a warning, a photo in many ads (a manufacturer's) nothing."""
import io
from datetime import timedelta

import pytest
from PIL import Image

from app.db import Hit, session, utcnow
from app.services import photo_match, scam


def _png(seed: int, shift: int = 0) -> bytes:
    """A photo-like picture (soft shapes) per seed; shift != 0: the same picture re-encoded as JPEG and scaled down,
    as a site does with an upload."""
    from PIL import ImageDraw, ImageFilter
    img = Image.new("RGB", (400, 300), (40 + seed * 9 % 200, 90, 160))
    d = ImageDraw.Draw(img)
    for i in range(6):
        x, y = (seed * 53 + i * 71) % 330, (seed * 29 + i * 47) % 230
        d.ellipse((x, y, x + 70, y + 60), fill=((seed * 31 + i * 40) % 255, (i * 60) % 255, (seed * 17) % 255))
    img = img.filter(ImageFilter.GaussianBlur(3))
    out = io.BytesIO()
    if shift:
        img.resize((372, 279)).save(out, format="JPEG", quality=55)
    else:
        img.save(out, format="PNG")
    return out.getvalue()


def test_dhash_tells_the_same_photo_apart_from_another():
    a, a2, b = photo_match.dhash(_png(3)), photo_match.dhash(_png(3, shift=1)), photo_match.dhash(_png(11))
    assert len(a) == 16 and photo_match.distance(a, a2) <= photo_match.MAX_DISTANCE
    assert photo_match.distance(a, b) > photo_match.MAX_DISTANCE
    assert photo_match.dhash(b"no image") is None


@pytest.fixture
def photos(monkeypatch):
    pics = {}

    async def fetch(url, platform):
        return pics.get(url)
    monkeypatch.setattr(photo_match, "_fetch", fetch)
    return pics


def _hit(ad_id, url, platform="willhaben", postcode="1010", days=0, **kw):
    return Hit(agent_id=1, ad_id=ad_id, heading=f"Kamera {ad_id}", url="x", image_url=url, platform=platform,
               postcode=postcode, location=f"{postcode} Wien", price=100.0,
               first_seen_at=utcnow() - timedelta(days=days), **kw)


async def test_relisted_and_copied_photos(photos):
    photos.update({"p1": _png(3), "p2": _png(3, shift=1), "p3": _png(3), "p4": _png(11)})
    with session() as db:
        db.add(_hit("1", "p1", days=20))                                     # the original, fingerprinted earlier
        db.add(_hit("2", "p2"))                                              # the same seller, same place: re-listed
        db.add(_hit("3", "p3", platform="kleinanzeigen", postcode="10115"))  # another site, another city: a copy
        db.add(_hit("4", "p4"))                                              # another photo
        db.commit()
    with session() as db:
        h1 = db.exec(photo_match.select(Hit).where(Hit.ad_id == "1")).one()
        h1.photo_hash = photo_match.dhash(_png(3))
        db.add(h1)
        db.commit()
    await photo_match.photo_job()
    with session() as db:
        hits = {h.ad_id: h for h in db.exec(photo_match.select(Hit))}
    assert [f["code"] for f in hits["2"].scam_flags] == ["relisted"]
    assert [f["code"] for f in hits["3"].scam_flags] == ["photo_copy"]
    assert scam.warnings(hits["3"].scam_flags)[0]["label"].startswith("Photo also in another ad: Kamera 1 (willhaben")
    assert scam.warnings(hits["2"].scam_flags) == []                       # information only
    assert not hits["4"].scam_flags and hits["4"].photo_hash


async def test_a_photo_many_ads_share_is_no_sign(photos):
    photos.update({f"s{i}": _png(5) for i in range(5)})
    with session() as db:
        for i in range(5):
            db.add(_hit(f"5{i}", f"s{i}", platform="kleinanzeigen", postcode=f"1{i}000"))
        db.commit()
    await photo_match.photo_job()
    with session() as db:
        assert all(not h.scam_flags for h in db.exec(photo_match.select(Hit)))


async def test_one_site_without_places_is_a_relisting_and_old_flags_are_judged_again(photos):
    """10-26: two models of one dealer on Vinted (no postcodes) with the same picture were called a copy."""
    photos.update({"v1": _png(7), "v2": _png(7, shift=1)})
    with session() as db:
        db.add(_hit("71", "v1", platform="vinted", postcode=None, days=3))
        db.add(_hit("72", "v2", platform="vinted", postcode=None,
                    scam_flags=[{"code": "photo_copy", "severity": "warn", "params": {}}]))     # by the old rule
        db.commit()
    await photo_match.photo_job()
    with session() as db:
        hits = {h.ad_id: h for h in db.exec(photo_match.select(Hit))}
    assert [f["code"] for f in hits["72"].scam_flags] == ["relisted"]
    assert scam.warnings(hits["72"].scam_flags) == []
