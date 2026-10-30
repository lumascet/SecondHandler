"""Finished chats: the list shows the open ones by default; a delivered package or Claude's verdict marks a chat
finished; a new message from the other person opens it again."""
from datetime import timedelta

from app.db import Conversation, Message, session, utcnow
from app.llm.base import LLMResult
from app.services import chat_done, chats
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)

T = utcnow() - timedelta(hours=3)


def _conv(cid, *msgs, at=T, **kw):
    with session() as db:
        db.add(Conversation(id=cid, ad_id="1", ad_title=f"Ding {cid}", partner_name=f"P{cid}",
                            last_message_at=at + timedelta(minutes=len(msgs)), **kw))
        for i, (direction, text, kind) in enumerate(msgs):
            db.add(Message(conversation_id=cid, ext_id=f"{cid}{i}", direction=direction, text=text, kind=kind,
                           created_at=at + timedelta(minutes=i)))
        db.commit()


def test_a_delivered_package_finishes_the_chat_until_they_write_again():
    _conv("d1", ("in", "Wann kommt es?", None), ("unknown", "Das Paket wurde zugestellt.", "integration"))
    _conv("d2", ("in", "Wann kommt es?", None), ("unknown", "Sendung wurde verschickt", "integration"))
    with session() as db:
        o = chats.overview(db)
    assert "d1" in o["finished"] and o["finished_why"]["d1"] == "delivered" and "d2" not in o["finished"]
    with session() as db:
        db.add(Message(conversation_id="d1", ext_id="d1x", direction="in", text="Es ist kaputt!",
                       created_at=T + timedelta(minutes=9)))
        db.commit()
        o = chats.overview(db)
    assert "d1" not in o["finished"]


async def test_claude_marks_a_chat_it_judges_over_once_per_new_message(monkeypatch):
    _conv("c1", ("out", "Paket ist unterwegs.", None), ("in", "Ist angekommen und läuft, Bewertung habe ich dir gegeben.", None))
    _conv("c2", ("in", "Kannst du morgen um 18 Uhr?", None), ("out", "Ja, passt.", None))
    calls = []

    class Fake:
        async def complete(self, *, system, prompt, schema, images=None):
            calls.append(prompt)
            return LLMResult(data={"chats": [{"n": i, "over": "angekommen" in block, "reason": "x"}
                                             for i, block in enumerate(prompt.split("Chat ")[1:], 1)]}, model="f")
    monkeypatch.setattr(chat_done, "get_backend", lambda *a: Fake())
    assert await chat_done.check_job() == 1
    with session() as db:
        c1, c2 = db.get(Conversation, "c1"), db.get(Conversation, "c2")
        assert c1.finished_by == "claude" and c1.finished_at and c2.finished_at is None
        o = chats.overview(db)
    assert "c1" in o["finished"] and "c2" not in o["finished"]
    assert await chat_done.check_job() == 0 and len(calls) == 1             # nothing new: not asked again


def test_the_list_shows_open_chats_and_finished_behind_their_filter(client):  # noqa: F811
    _conv("o1", ("in", "Noch da?", None))
    _conv("f1", ("in", "Danke, alles bestens!", None), ("unknown", "Das Paket wurde zugestellt.", "integration"),
          at=utcnow() - timedelta(days=2))
    page = client.get("/chats").text
    assert "Ding o1" in page and "Ding f1" not in page
    page = client.get("/chats?show=finished").text
    assert "Ding f1" in page and "Ding o1" not in page and "data-finished" in page
    assert "Ding o1" in client.get("/chats?show=all").text and "Ding f1" in client.get("/chats?show=all").text
    tg = client.get("/tg/chats").text
    assert "Ding o1" in tg and "Ding f1" not in tg and "Ding f1" in client.get("/tg/chats?show=finished").text


def test_a_chat_finished_today_stays_in_the_list_with_its_tag(client):  # noqa: F811
    _conv("n1", ("out", "Funktioniert mit jedem Anschluss.", None), ("in", "ok, vielen dank", None))
    page = client.get("/chats").text
    assert "Ding n1" in page and "data-finished" in page
