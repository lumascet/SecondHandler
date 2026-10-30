"""Human-like auto replies: timing plan, read receipts, and re-checks before sending."""
import random
from datetime import timedelta

import pytest

from app.db import Conversation, Draft, Message, MyAd, session, set_setting, utcnow
from app.llm.base import LLMError, LLMResult
from app.services import chats, humanize
from app.willhaben.account import SendFailed, parse_messages


def test_plan_stays_in_window_and_reads_before_sending():
    set_setting("reply_delay_min_s", "60")
    set_setting("reply_delay_max_s", "300")
    now = utcnow()
    for seed in range(200):
        p = humanize.plan(now, now, random.Random(seed))
        read_s = (p.read_at - now).total_seconds()
        send_s = (p.send_after - now).total_seconds()
        assert 60 * 0.3 <= read_s <= 300 * 0.6
        assert send_s >= read_s + humanize.THINK_S[0] and send_s >= 60
    # message noticed late (app was offline): read soon, not in the past
    old = now - timedelta(hours=1)
    p = humanize.plan(old, now, random.Random(1))
    assert now < p.read_at <= now + timedelta(seconds=humanize.LATE_READ_S[1])


def test_typing_time_grows_with_text():
    r = random.Random(3)
    assert humanize.typing_seconds("x" * 400, r) > humanize.typing_seconds("x" * 20, random.Random(3))


def test_parse_messages_keeps_read_flag():
    msgs = parse_messages({"messages": [
        {"message_id": "m1", "text": "Hallo", "is_mine": False, "read": False, "created_at": "2026-10-09T10:00:00Z"},
        {"message_id": "m2", "text": "Hi", "is_mine": True, "read": True, "created_at": "2026-10-09T10:01:00Z"},
    ]}, set())
    assert [(m.ext_id, m.read) for m in msgs] == [("m1", False), ("m2", True)]


class FakeSession:
    def __init__(self):
        self.frames = []

    async def ws_send(self, url, frames, settle_ms=1500):
        self.frames += frames
        return {"status": "ok", "sent": len(frames)}


@pytest.fixture
def seller_chat():
    with session() as db:
        db.add(MyAd(ad_id="111", heading="Lautsprecher", auto_reply=True))
        db.add(Conversation(id="c1", ad_id="111", ad_title="Lautsprecher", partner_name="Anna", role="seller",
                            unread_count=1))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Noch da?",
                       created_at=utcnow() - timedelta(seconds=5)))
        db.commit()


async def test_humanized_flow_reads_first_and_rechecks_before_sending(seller_chat, monkeypatch):
    events = []
    set_setting("reply_delay_min_s", "0")
    set_setting("reply_delay_max_s", "0")
    monkeypatch.setattr(humanize, "THINK_S", (0.0, 0.0))
    monkeypatch.setattr(humanize, "typing_seconds", lambda text, rng=None, warm=False: 0.0)

    async def fake_sync(cid):
        events.append("sync")
        return "full"

    async def fake_mark(cid):
        events.append("read")
        return 1

    async def fake_auto(cid, before_send=None):
        events.append("draft")
        d = Draft(id=1, conversation_id=cid, text="Ja, ist noch da.")
        ok = await before_send(d)
        events.append("send" if ok else "drop")
        return d if ok else None

    monkeypatch.setattr(chats, "sync_messages", fake_sync)
    monkeypatch.setattr(chats, "mark_read", fake_mark)
    monkeypatch.setattr(chats, "auto_reply", fake_auto)
    d = await chats._humanized_auto("c1")
    assert d is not None
    assert events == ["sync", "read", "draft", "sync", "send"]


async def test_humanized_flow_drops_answer_when_buyer_writes_again(seller_chat, monkeypatch):
    set_setting("reply_delay_min_s", "0")
    set_setting("reply_delay_max_s", "0")
    monkeypatch.setattr(humanize, "THINK_S", (0.0, 0.0))
    monkeypatch.setattr(humanize, "typing_seconds", lambda text, rng=None, warm=False: 0.0)
    calls = {"sync": 0, "drafts": 0}

    async def fake_sync(cid):
        calls["sync"] += 1
        if calls["sync"] == 2:   # while "typing", a second question arrives
            with session() as db:
                db.add(Message(conversation_id="c1", ext_id="m2", direction="in", text="Und der Preis?",
                               created_at=utcnow()))
                db.commit()
        return "full"

    async def fake_mark(cid):
        return 1

    async def fake_auto(cid, before_send=None):
        calls["drafts"] += 1
        d = Draft(id=calls["drafts"], conversation_id=cid, text="Antwort")
        return d if await before_send(d) else None

    monkeypatch.setattr(chats, "sync_messages", fake_sync)
    monkeypatch.setattr(chats, "mark_read", fake_mark)
    monkeypatch.setattr(chats, "auto_reply", fake_auto)
    d = await chats._humanized_auto("c1")
    assert d is not None and calls["drafts"] == 2   # first answer dropped, rewritten for the new message


def test_pending_draft_of_lost_task_does_not_block_new_answer(seller_chat):
    """Restart during the typing pause: the 'pending' draft must not make the chat look answered."""
    with session() as db:
        db.add(Draft(conversation_id="c1", text="halb getippt", status="pending", auto=True))
        db.commit()
    assert chats._waiting_for_answer("c1") is True
    with session() as db:
        db.add(Draft(conversation_id="c1", text="echter Entwurf", status="draft"))
        db.commit()
    assert chats._waiting_for_answer("c1") is False


async def test_limits_are_checked_again_at_send_time(seller_chat, monkeypatch):
    sent = []

    async def fake_draft(cid, hint=None, auto=False):
        with session() as db:
            d = Draft(conversation_id=cid, text="Ja, noch da.", auto=True, status="draft")
            db.add(d)
            db.commit()
            db.refresh(d)
            return d

    checks = iter([[], ["hourly limit reached"]])   # fine when drafting, limit hit after the delay
    monkeypatch.setattr(chats, "draft_reply", fake_draft)
    monkeypatch.setattr(chats, "_auto_blockers", lambda *a: next(checks))

    async def fake_send(*a):
        sent.append(a)

    async def fake_offer(conv, d, reason):
        sent.append(("offer", reason))

    async def ok(d):
        return True

    monkeypatch.setattr(chats, "_send_auto", fake_send)
    monkeypatch.setattr(chats, "offer_draft", fake_offer)
    d = await chats.auto_reply("c1", before_send=ok)
    assert sent == [("offer", "not sent automatically: hourly limit reached")]
    assert d.status == "draft"


async def test_failed_send_after_the_typing_pause_offers_the_draft(seller_chat, monkeypatch):
    """willhaben rejects the message once the human-like delay is over: the draft is offered with the reason, as
    when a send fails without the delay - not dropped without a word."""
    notes = []

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, message, kw))
        return True

    class Backend:
        model = "fake"

        async def complete(self, *, system, prompt, schema, images=None):
            return LLMResult(data={"reply": "Ja, noch da.", "intent": "availability", "needs_info": [],
                                   "suggested_price": None, "note": "", "auto_send": True, "auto_reason": "einfach"},
                             model="fake")

    class Account:
        async def send_message(self, conversation_id, text):
            raise SendFailed("willhaben rejected the message")

    async def no_ctx(cid, force=False):
        with session() as db:
            return db.get(Conversation, cid)

    async def ok(d):
        return True

    set_setting("send_verified", "1")
    monkeypatch.setattr(chats, "get_backend", lambda *a: Backend())
    monkeypatch.setattr(chats, "_account", lambda: Account())
    monkeypatch.setattr(chats, "ensure_ad_context", no_ctx)
    monkeypatch.setattr(chats, "notify", fake_notify)
    d = await chats.auto_reply("c1", before_send=ok)
    assert d.status == "draft" and d.text == "Ja, noch da." and "rejected" in d.error
    assert d.auto_reason == "✓ einfach · not sent: Sending failed: willhaben rejected the message"
    assert [n[0] for n in notes] == ["auto_reply_pending"] and "rejected" in notes[0][2]
    assert notes[0][3]["tag"] == f"wh-draft-{d.id}" and "Send" in [a["title"] for a in notes[0][3]["actions"]]
    with session() as db:
        assert db.get(Draft, d.id).status == "draft"


async def test_failure_during_the_delayed_answer_is_reported(seller_chat, monkeypatch):
    """Claude fails when the human-like answer is due: the owner is told once (push, chat page), not left waiting."""
    notes = []

    async def fake_notify(kind, title, message, **kw):
        notes.append((kind, title, message, kw))
        return True

    async def noop(*a, **k):
        return 1

    async def boom(cid, before_send=None):
        raise LLMError("Claude antwortet nicht")

    set_setting("reply_delay_min_s", "0")
    set_setting("reply_delay_max_s", "0")
    monkeypatch.setattr(humanize, "THINK_S", (0.0, 0.0))
    monkeypatch.setattr(chats, "sync_messages", noop)
    monkeypatch.setattr(chats, "mark_read", noop)
    monkeypatch.setattr(chats, "auto_reply", boom)
    monkeypatch.setattr(chats, "notify", fake_notify)
    assert await chats._humanized_auto("c1") is None
    assert [n[0] for n in notes] == ["auto_reply_pending"] and "Claude antwortet nicht" in notes[0][2]
    assert notes[0][3]["tag"] == "wh-msg-c1" and "c1" not in chats._humanized
    with session() as db:
        assert db.get(Conversation, "c1").draft_error == "Automatic answer failed: Claude antwortet nicht"
    assert await chats._humanized_auto("c1") is None and len(notes) == 1       # same message: no second push
    assert chats._waiting_for_answer("c1")                                     # the sync tries again (a few times)


def test_ongoing_chat_answers_within_seconds():
    set_setting("reply_delay_min_s", "60")
    set_setting("reply_delay_max_s", "300")
    set_setting("reply_fast_min_s", "5")
    set_setting("reply_fast_max_s", "20")
    now = utcnow()
    for seed in range(200):
        r = random.Random(seed)
        p = humanize.plan(now, now, r, warm=True)
        total = (p.send_after - now).total_seconds() + humanize.typing_seconds("x" * 500, r, warm=True)
        assert total <= 20 + 20   # window plus typing capped to the window: always well under a minute
        assert (p.read_at - now).total_seconds() <= 20 * 0.6


async def test_warm_flag_follows_my_earlier_answer(seller_chat, monkeypatch):
    seen = []
    real_plan = humanize.plan
    monkeypatch.setattr(humanize, "plan", lambda *a, warm=False, **k: seen.append(warm) or real_plan(*a, warm=warm, **k))
    monkeypatch.setattr(humanize, "typing_seconds", lambda text, rng=None, warm=False: 0.0)
    set_setting("reply_fast_min_s", "0")
    set_setting("reply_fast_max_s", "0")
    with session() as db:
        db.add(Message(conversation_id="c1", ext_id="m0", direction="out", text="Hallo!",
                       created_at=utcnow() - timedelta(minutes=10)))
        db.commit()

    async def noop(*a, **k):
        return 1

    async def fake_auto(cid, before_send=None):
        return None

    monkeypatch.setattr(chats, "sync_messages", noop)
    monkeypatch.setattr(chats, "mark_read", noop)
    monkeypatch.setattr(chats, "auto_reply", fake_auto)
    await chats._humanized_auto("c1")
    assert seen == [True]
