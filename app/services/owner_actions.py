"""What the owner does to a hit or a draft, the same from every surface (the web pages, the Telegram Mini App, the
bot's buttons): the state change and everything that follows from it – the taste model, the push going off the
phone, the photos kept before the site drops them, the Telegram message about it settled. Each surface calls these
instead of setting the state itself, so none of them forgets a step (a draft discarded in the Mini App came back as a
new auto-draft; a hit kept there never reached the taste model)."""
from __future__ import annotations

from typing import Iterable

from app.config import get_settings
from app.db import Draft, Hit, session
from app.i18n import t

HIT_STATES = ("new", "seen", "saved", "ignored")


def settle_hits(hits: list[Hit], state: str) -> None:
    """A hit kept or ignored here: its Telegram message says so and loses Keep / Ignore (services/telegram.settle)."""
    from app.services import search_agents, telegram
    line = {"saved": "⭐ " + t("Kept in the app"), "ignored": "🚫 " + t("Ignored in the app")}.get(state)
    if not line:
        return
    for h in hits[:50]:
        telegram.settle_later([search_agents.hit_tag(h)], line, h.url)


def settle_draft(d: Draft | None, line: str) -> None:
    """A draft sent or discarded here: its Telegram message (the draft offer, or the chat's message it took over)
    says so and loses Send / Revise / Discard."""
    from app.services import chats, ha_actions, telegram
    if d is None:
        return
    url = f"{get_settings().public_url.rstrip('/')}/chats/{d.conversation_id}"
    telegram.settle_later([ha_actions.tag_for(d.id), chats.message_tag(d.conversation_id)], line, url)


def set_hit_state(hit_ids: Iterable[int], state: str, *, settle: bool = True) -> list[Hit]:
    """Set the hits' state and do what follows: kept / ignored -> the agent's taste (services/taste.py) and, with
    `settle`, their Telegram messages; anything but new -> the push goes off the phone; kept -> its photos are kept.
    `settle` False for the bot's own button (that message already lost its buttons). Returns the hits changed."""
    from app.services import search_agents, taste
    if state not in HIT_STATES:
        raise ValueError(state)
    with session() as db:
        hits = [h for i in dict.fromkeys(hit_ids) if (h := db.get(Hit, i)) is not None]
        for h in hits:
            h.state = state
            db.add(h)
        db.commit()
        for h in hits:
            db.refresh(h)
    if not hits:
        return hits
    if state in ("saved", "ignored"):
        for h in hits:
            taste.record(h, state == "saved")
        if settle:
            settle_hits(hits, state)
    if state != "new":
        search_agents.pushes_seen(hits)
    if state == "saved":
        for h in hits:
            search_agents.keep_image_later(h.id, search_agents.shown_photos(h.id))
    return hits


def discard_draft(draft_id: int) -> Draft | None:
    """The owner throws a draft away: dismissed for good (no new auto-draft for the same message) and its Telegram
    offer settled. None when there is no such draft."""
    from app.services import chats
    with session() as db:
        d = db.get(Draft, draft_id)
    if d is None:
        return None
    chats.discard_draft(draft_id, by_owner=True)
    settle_draft(d, "🗑 " + t("Discarded in the app"))
    return d
