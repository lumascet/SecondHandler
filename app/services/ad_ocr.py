"""Read text that lives inside a listing's photos.

A seller often puts the specs that decide a purchase — the CPU, the RAM, the exact model, a size — only in a
spec-sheet screenshot or on a label in the photos, not in the ad text (seen on a Vinted all-in-one whose Ryzen
5 7520U showed up only in a photographed product-description screen). The judge in services/screening then sees
"HP · Sehr gut · 525 €" and rightly says the specs are missing, when they are right there in picture five.

So, for ads whose text is thin, we OCR a few of their photos locally with tesseract (no model call) and hand the
recognised text to the screening prompt. Missing tesseract, a thin or garbled result, or any error all yield an
empty string — this never raises and never blocks a screening.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

LANGS = "deu+eng"             # the ads are German; English for product names and Windows/driver screenshots
MAX_PHOTOS = 6               # photos read per hit: the specs sit in the spec-sheet shot or the label, not the cover
MIN_CHARS = 14              # a shorter recognised result is noise (a price overlay, a watermark): drop it
MAX_CHARS = 600             # cap what rides along into the prompt
PER_IMAGE_TIMEOUT_S = 20

HAVE_TESSERACT = shutil.which("tesseract") is not None


def _clean(text: str) -> str:
    """Keep the lines that read like real text (mostly letters and digits), deduplicated, and cap the length."""
    seen: set[str] = set()
    lines: list[str] = []
    for raw in text.splitlines():
        ln = " ".join(raw.split())
        if len(ln) < 2:
            continue
        letters = sum(c.isalnum() for c in ln)
        if letters < max(2, len(ln) * 2 // 3):      # mostly symbols: OCR noise, not text
            continue
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(ln)
    return "\n".join(lines).strip()[:MAX_CHARS]


def _ocr_file(path: Path) -> str:
    """Run tesseract on one image file; its stdout (raw, cleaned by the caller). Empty on any failure."""
    try:
        p = subprocess.run(["tesseract", str(path), "stdout", "-l", LANGS],
                           capture_output=True, timeout=PER_IMAGE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001 - binary missing, timeout, crash
        log.info("ocr: tesseract failed (%s)", type(e).__name__)
        return ""
    return p.stdout.decode("utf-8", "replace")


async def read_photos(urls: list[str], folder: Path, *, platform: str | None = None,
                      transport: object | None = None) -> str:
    """Download up to MAX_PHOTOS of `urls` into `folder`, OCR each, and return the combined text ("" when tesseract
    is absent, nothing downloads, or too little is recognised). Downloads are serial so each site's limiter paces
    them; the OCR itself runs off the event loop."""
    if not HAVE_TESSERACT:
        return ""
    picks = [u for u in dict.fromkeys(urls) if u][:MAX_PHOTOS]
    if not picks:
        return ""
    from app.services import ad_images

    chunks: list[str] = []
    for i, url in enumerate(picks):
        try:
            img = await ad_images.download(url, folder, f"ocr{i}", transport=transport, platform=platform)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 - a bad photo URL must not sink the rest
            log.info("ocr: download failed (%s)", type(e).__name__)
            continue
        if img is not None:
            chunks.append(await asyncio.to_thread(_ocr_file, img))
    text = _clean("\n".join(chunks))
    return text if len(text) >= MIN_CHARS else ""
