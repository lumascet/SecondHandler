"""The same photo in another ad: a perceptual fingerprint (dHash, 64 bit) of each recent hit's first photo, compared
with those of the last LOOKBACK_DAYS. Another ad on the same site in the same place is most likely the seller
re-listing it ("relisted", shown for information); one on another site or in another place is a classic scam pattern –
a stolen photo ("photo_copy", a warning). A photo in STOCK_MATCHES or more other ads is a manufacturer's picture many
sellers use: nothing is flagged. The flags join the hit's rule-based scam flags (app/services/scam.py)."""
from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlmodel import col, select

from app.db import Hit, session, utcnow

log = logging.getLogger(__name__)

PER_RUN = 150             # photos fingerprinted per hourly run
NEW_DAYS = 7              # hits first seen within this many days get a fingerprint
LOOKBACK_DAYS = 60        # … compared with the hits of this many days
MAX_DISTANCE = 4          # differing bits of 64 that still count as the same photo (re-encoded, slightly cropped)
STOCK_MATCHES = 3         # a photo in this many other ads or more: a manufacturer's picture, no flag


def dhash(data: bytes) -> str | None:
    """The 64-bit difference hash of an image as 16 hex digits: grey, 9×8 pixels, each bit "left brighter than right".
    None when the bytes are no image."""
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    px = img.tobytes()                       # one byte per pixel (mode "L")
    bits = 0
    for row in range(8):
        for c in range(8):
            bits = bits << 1 | (px[row * 9 + c] > px[row * 9 + c + 1])
    return f"{bits:016x}"


def distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


async def _fetch(url: str, platform: str | None) -> bytes | None:
    """The photo through ad_images.download, as every other photo of a hit: the site's VPN when it is on (never the
    home IP then), counted as the site's access, https and an image only, at most MAX_IMAGE_BYTES."""
    import tempfile
    from pathlib import Path

    from app.services import ad_images
    try:
        with tempfile.TemporaryDirectory(prefix="wh-photo-") as tmp:
            path = await ad_images.download(url, Path(tmp), "p", platform=platform)
            return path.read_bytes() if path is not None else None
    except (httpx.HTTPError, OSError) as e:
        log.info("photo %s not loaded: %s", url, e)
        return None


def _flag(h: Hit, other: Hit) -> dict[str, Any]:
    """The flag of hit `h` for the same photo in `other` (an older hit)."""
    # the same site, and the same place or one of them without a place (Vinted and Facebook list none): the seller
    # again – most likely, a dealer showing one picture for several models (10-26: two mini PCs on Vinted) included
    same_place = (h.platform or "willhaben") == (other.platform or "willhaben") and (
        not h.postcode or not other.postcode or h.postcode == other.postcode)
    price = f"{other.price:g} €" if other.price else "–"
    from app.services import scam
    if same_place:
        flag = {"code": "relisted", "severity": "info", "params": {"other": other.id, "price": price}}
    else:
        from app import platforms
        try:
            site = platforms.get(other.platform or "willhaben").name
        except Exception:  # noqa: BLE001 - an unknown site keeps its id
            site = other.platform or "willhaben"
        flag = {"code": "photo_copy", "severity": "warn",
                "params": {"other": other.id, "title": (other.heading or "")[:60], "site": site,
                           "place": other.location or "?", "price": price}}
    return {**flag, "label": scam.label(flag)}


async def photo_job() -> None:
    """Hourly: fingerprints for the recent hits without one, then their matches among the last LOOKBACK_DAYS."""
    now = utcnow()
    with session() as db:
        todo = list(db.exec(select(Hit).where(col(Hit.photo_hash).is_(None), col(Hit.image_url).is_not(None),
                                               Hit.first_seen_at >= now - timedelta(days=NEW_DAYS))
                            .order_by(col(Hit.id).desc()).limit(PER_RUN)))
    flagged = _flagged()
    if not todo:
        match(flagged)                   # re-judged by the current rule
        return
    hashed = []
    for h in todo:
        data = await _fetch(h.image_url, h.platform)
        h.photo_hash = (dhash(data) if data else None) or "-"       # "-": not loadable, not asked again
        hashed.append(h)
    with session() as db:
        for h in hashed:
            row = db.get(Hit, h.id)
            if row is not None:
                row.photo_hash = h.photo_hash
                db.add(row)
        db.commit()
    match([h for h in hashed if h.photo_hash != "-"] + [h for h in flagged if h.id not in {x.id for x in hashed}])


def _flagged() -> list[Hit]:
    """Hits with a photo flag already: judged again each run, so a changed rule or a removed ad corrects them."""
    with session() as db:
        return [h for h in db.exec(select(Hit).where(col(Hit.photo_hash).is_not(None), Hit.photo_hash != "-",
                                                     col(Hit.scam_flags).is_not(None)))
                if any(f.get("code") in ("relisted", "photo_copy") for f in h.scam_flags or [])]


def _drop_flag(h: Hit) -> None:
    with session() as db:
        for row in db.exec(select(Hit).where(Hit.platform == h.platform, Hit.ad_id == h.ad_id)):
            if any(f.get("code") in ("relisted", "photo_copy") for f in row.scam_flags or []):
                row.scam_flags = [f for f in row.scam_flags if f.get("code") not in ("relisted", "photo_copy")]
                db.add(row)
        db.commit()


def match(hits: list[Hit]) -> None:
    """Flag each of `hits` whose photo another ad of the last LOOKBACK_DAYS has (not the same ad of another agent)."""
    if not hits:
        return
    with session() as db:
        known = list(db.exec(select(Hit).where(col(Hit.photo_hash).is_not(None), Hit.photo_hash != "-",
                                               Hit.first_seen_at >= utcnow() - timedelta(days=LOOKBACK_DAYS))))
    for h in hits:
        me = (h.platform or "willhaben", h.ad_id)
        others: dict[tuple[str, str], Hit] = {}
        for o in known:
            key = (o.platform or "willhaben", o.ad_id)
            if key != me and key not in others and distance(h.photo_hash, o.photo_hash) <= MAX_DISTANCE:
                others[key] = o
        if not others or len(others) >= STOCK_MATCHES:
            _drop_flag(h)                 # no (or a manufacturer's) photo match any more
            continue
        other = min(others.values(), key=lambda o: o.first_seen_at or utcnow())      # the earliest
        flag = _flag(h, other)
        with session() as db:
            for row in db.exec(select(Hit).where(Hit.platform == h.platform, Hit.ad_id == h.ad_id)):
                flags = [f for f in row.scam_flags or [] if f.get("code") not in ("relisted", "photo_copy")]
                row.scam_flags = sorted(flags + [flag], key=lambda f: -{"info": 0, "warn": 1, "high": 2}
                                        .get(f.get("severity"), 0))
                db.add(row)
            db.commit()
