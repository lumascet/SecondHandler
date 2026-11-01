from datetime import timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, Message, session, set_setting, utcnow
from app.services import chats
from app.willhaben.account import ConversationSummary


def _setup(monkeypatch, state):
    class _Sess:
        started = logged_in = True

    class _Acct:
        async def conversations(self, limit=50):
            return [ConversationSummary(id="c1", ad_id="9", ad_title="Rad", partner_name="Anna", unread_count=0,
                                        last_text=state["text"], last_at=state["at"], raw={}, role="seller",
                                        last_is_mine=state["mine"])]

    synced = []

    async def sync_messages(cid):
        synced.append(cid)
        return "full"

    monkeypatch.setattr(chats, "get_session", lambda: _Sess())
    monkeypatch.setattr(chats, "_account", lambda: _Acct())
    monkeypatch.setattr(chats, "sync_messages", sync_messages)
    return synced


async def test_removed_chat_stays_away_until_they_write_again(monkeypatch):
    at = utcnow() - timedelta(hours=1)
    state = {"text": "Noch da?", "at": at, "mine": False}
    synced = _setup(monkeypatch, state)
    await chats.sync()
    with session() as db:
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Noch da?"))
        db.add(Draft(conversation_id="c1", text="Ja"))
        db.commit()
    chats.remove("c1")
    with session() as db:
        assert db.exec(select(Message)).all() == [] and db.exec(select(Draft)).all() == []
        assert db.get(Conversation, "c1").removed
    synced.clear()
    await chats.sync(force=True)
    assert synced == []                                     # removed: not synced again
    state.update(at=utcnow(), mine=True, text="Meine Antwort von woanders")
    await chats.sync()
    with session() as db:
        assert synced == [] and db.get(Conversation, "c1").removed   # my own message does not bring it back
    state.update(at=utcnow() + timedelta(seconds=1), mine=False, text="Hallo?")
    await chats.sync()
    with session() as db:
        c = db.get(Conversation, "c1")
        assert synced == ["c1"] and not c.removed and c.hidden_at is None and c.last_message_text == "Hallo?"


async def test_hidden_chat_comes_back_on_a_new_message(monkeypatch):
    state = {"text": "Noch da?", "at": utcnow() - timedelta(hours=1), "mine": False}
    _setup(monkeypatch, state)
    await chats.sync()
    chats.hide("c1")
    await chats.sync(force=True)
    with session() as db:
        assert db.get(Conversation, "c1").hidden_at is not None    # still hidden, but kept up to date
    state.update(at=utcnow(), text="Hallo?")
    await chats.sync()
    with session() as db:
        assert db.get(Conversation, "c1").hidden_at is None


class FakeAccount:
    """willhaben's chat delete (recorded): the ids asked for, or a refusal."""
    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    async def delete_conversations(self, ids):
        if self.fail:
            from app.willhaben.account import SendFailed
            raise SendFailed("HTTP 403")
        self.deleted.append(list(ids))


def test_chat_list_hides_and_removes(monkeypatch):
    from app.main import app
    from app.services import chats
    fake = FakeAccount()
    monkeypatch.setattr(chats, "_account", lambda: fake)

    with session() as db:
        db.add(Conversation(id="a", partner_name="Sichtbar", last_message_at=utcnow()))
        db.add(Conversation(id="b", partner_name="Versteckt", last_message_at=utcnow(), hidden_at=utcnow()))
        db.add(Conversation(id="c", partner_name="Entfernt", last_message_at=utcnow(), hidden_at=utcnow(), removed=True))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/chats").text
        assert "Sichtbar" in page and "Versteckt" not in page and "Entfernt" not in page
        assert "Show 1 hidden or older chats" in page
        page = c.get("/chats?show_hidden=1").text
        assert "Versteckt" in page and "Entfernt" not in page
        c.post("/chats/a/hide", data={"hidden": "1"})
        c.post("/chats/b/hide", data={"hidden": "0"})
        page = c.get("/chats").text
        assert "Versteckt" in page and "Sichtbar" not in page
        c.post("/chats/b/remove")
        assert fake.deleted == [["b"]]                          # deleted on willhaben first, then here
        assert "Versteckt" not in c.get("/chats?show_hidden=1").text
        assert {x["id"] for x in c.get("/api/chats").json()} == {"a"}


def test_chat_list_filters_unread_awaiting_reply_and_drafts():
    from app.main import app

    with session() as db:
        for cid, name, unread in (("u", "Ungelesen-Person", 2), ("w", "Wartet-Person", 0), ("d", "Entwurf-Person", 0),
                                  ("m", "Erledigt-Person", 0), ("f", "Fertig-mit-Entwurf", 0)):
            db.add(Conversation(id=cid, partner_name=name, unread_count=unread, last_message_at=utcnow(), role="seller"))
        db.commit()
        at = utcnow()
        for cid, direction in (("w", "out"), ("w", "in"), ("m", "in"), ("d", "in"), ("f", "in")):
            at += timedelta(seconds=1)
            db.add(Message(conversation_id=cid, ext_id=f"{cid}{at.timestamp()}", direction=direction, text="x", created_at=at))
        at += timedelta(seconds=1)
        db.add(Message(conversation_id="m", ext_id="m-last", direction="out", text="erledigt", created_at=at))
        db.add(Message(conversation_id="m", ext_id="m-notice", direction="in", text="notice", kind="system",
                       created_at=at + timedelta(seconds=1)))          # a site notice does not count as their message
        db.add(Draft(conversation_id="d", text="Ja"))
        db.add(Draft(conversation_id="f", text="Ja"))                  # a draft left over on a finished deal
        # "f" is finished (marked), yet still carries a draft: it must not nag from the action filters
        db.get(Conversation, "f").finished_at = at + timedelta(seconds=2)
        db.commit()
    set_setting("chats_app_seen_since", (at + timedelta(seconds=5)).isoformat())   # all of it seen in the app
    names = ("Ungelesen-Person", "Wartet-Person", "Entwurf-Person", "Erledigt-Person", "Fertig-mit-Entwurf")
    with TestClient(app) as c:
        def shown(url):
            page = c.get(url).text.split('id="chat-list-items"')[1]
            return {n for n in names if n in page}
        assert shown("/chats") == set(names)                          # a finished-today chat still shows in the list
        assert shown("/chats?show=unread") == {"Ungelesen-Person"}
        assert shown("/chats?show=reply") == {"Wartet-Person", "Entwurf-Person"}   # my move: they wrote, or a draft waits
        assert shown("/chats?show=draft") == {"Entwurf-Person"}                     # the finished deal's draft is not here
        assert shown("/chats?show=finished") == {"Fertig-mit-Entwurf"}
        assert shown("/chats?show=reply&role=buyer") == set()
        assert 'href="/chats?role=seller&amp;show=reply"' in c.get("/chats?show=reply").text


def test_deleting_chats_on_willhaben(monkeypatch):
    """One request for several chats; a refusal removes nothing here; a Vinted chat is only removed here."""
    from app.main import app
    from app.services import chats
    with session() as db:
        for cid in ("x", "y", "z"):
            db.add(Conversation(id=cid, partner_name=cid, last_message_at=utcnow()))
        db.add(Conversation(id="v1", platform="vinted", partner_name="v", last_message_at=utcnow()))
        db.commit()
    fake = FakeAccount(fail=True)
    monkeypatch.setattr(chats, "_account", lambda: fake)
    with TestClient(app) as c:
        r = c.post("/chats/x/remove", follow_redirects=False)
        assert "/chats/x?msg=" in r.headers["location"]          # refused: back to the chat, it stays
        with session() as db:
            assert not db.get(Conversation, "x").removed
        fake.fail = False
        r = c.post("/chats/bulk/remove", data={"ids": ["y", "z", "v1"]}, follow_redirects=False)
        assert fake.deleted == [["y", "z"]]                      # one willhaben request for both
        assert "Vinted" in unquote(r.headers["location"])
    with session() as db:
        assert db.get(Conversation, "y").removed and db.get(Conversation, "z").removed and db.get(Conversation, "v1").removed


async def test_the_account_sends_willhabens_delete():
    """DELETE chat-api/v1/conversations with {"ids": [...]}, as the chat web app does; anything but 2xx raises."""
    from app.willhaben.account import AccountClient, SendFailed
    from app.willhaben.session import ApiResponse
    calls = []

    class S:
        status = 204

        async def api(self, method, path, body=None):
            calls.append((method, path, body))
            return ApiResponse(self.status, "", path)
    s = S()
    await AccountClient(s).delete_conversations(["a", "b"])
    assert calls == [("DELETE", "/webapi/chat-api/v1/conversations", {"ids": ["a", "b"]})]
    s.status = 403
    with pytest.raises(SendFailed):
        await AccountClient(s).delete_conversations(["a"])
