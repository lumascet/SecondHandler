"""AccountClient.create_conversation: the ad page's contact form (`POST iad-messaging/sendrequest/chat`).

No network: the BrowserSession is replaced by a fake with `api` (the POST, the profile) and `api_json` (the
conversation list; same pattern as tests/test_auto_reply.py). The access-log test uses the real BrowserSession with a
fake page (tests/test_access_stats.py).
"""
import json

import pytest

from app.willhaben.account import SEND_REQUEST_CHAT, AccountClient, OpenerUnconfirmed, SendFailed
from app.willhaben.session import ApiResponse, NotLoggedIn
from tests.test_access_stats import _counts, _session

TEXT = "  Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.  "
ME = {"userData": {"loginId": 42, "uuid": "u-1", "firstName": "Niklas", "lastName": "Ruby",
                   "emailAddress": "buyer@example.org"}}


class _FakeSession:
    """`post`: (status, body) of the contact request. `lists`: one conversation list per poll. `history`: messages of
    a conversation id (else its history endpoint answers 404). `logged_out`: the profile fails after the POST.
    `list_error`: the conversation list raises it."""

    def __init__(self, post=(200, {"sent": True, "conversationId": "c-new"}), lists=None, me=ME, history=None,
                 logged_out=False, list_error=None):
        self.post = post
        self.me = me
        self.lists = list(lists or [])
        self.history = history or {}
        self.logged_out = logged_out
        self.list_error = list_error
        self.logged_in = True
        self.user = None
        self.posts = []
        self.list_calls = 0

    async def api(self, method, path, body=None, accept="application/json"):
        if method == "GET":
            for cid, msgs in self.history.items():
                if path == f"/webapi/chat-api/v1/conversations/{cid}/messages?limit=100&offset=0":
                    return ApiResponse(200, json.dumps({"messages": msgs}), path)
            if "/conversations/" in path:
                return ApiResponse(404, "", path)
            assert path == "/webapi/userprofile-service/userprofile/me"
            return ApiResponse(200, json.dumps(self.me), path)
        assert method == "POST" and path == SEND_REQUEST_CHAT
        self.posts.append(body)
        status, data = self.post
        return ApiResponse(status, data if isinstance(data, str) else json.dumps(data), path)

    async def api_json(self, method, path, body=None):
        if path.endswith("/userprofile/me"):
            if self.logged_out and self.posts:
                self.logged_in = False
                raise NotLoggedIn(path)
            return self.me
        assert method == "GET" and path == "/webapi/chat-api/v1/conversations?limit=50&offset=0"
        if self.list_error:
            raise self.list_error
        self.list_calls += 1
        return self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]


def _entry(cid="c9", ad_id="77", preview="Hallo, ist der Artikel noch zu haben? Ich würde 80 € zahlen.", mine=True):
    return {"conversation_id": cid, "ad_info": {"id": ad_id, "subject": "Kinderbuch"},
            "participant_info": {"name": "Max"}, "unseen": 0,
            "last_message": {"is_mine": mine, "preview": preview, "timestamp": 1790000000000}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr("app.willhaben.account.asyncio.sleep", instant)


async def test_body_like_the_contact_form_and_id_from_response():
    s = _FakeSession()
    assert await AccountClient(s).create_conversation("77", TEXT) == "c-new"
    assert s.posts == [{"fromFullName": "Niklas Ruby", "firstName": "Niklas", "lastName": "Ruby",
                        "shareTenantProfile": False, "from": "buyer@example.org", "adId": 77,
                        "copyToSender": False, "showTelephoneNumber": False, "telephone": "",
                        "selectedContactSuggestions": [], "mailContent": TEXT.strip()}]
    assert s.list_calls == 0                  # no polling when the answer carries the id


async def test_profile_without_name_leaves_the_name_fields_out():
    s = _FakeSession(me={"userData": {"loginId": 42, "emailAddress": "buyer@example.org"}})
    await AccountClient(s).create_conversation("77", TEXT)
    body = s.posts[0]
    assert body["fromFullName"] == "" and "firstName" not in body and "lastName" not in body


async def test_id_found_via_conversation_list_after_empty_polls():
    s = _FakeSession(post=(200, {"sent": True}),
                     lists=[{"conversations": []}, {"conversations": [_entry(ad_id="12")]},
                            {"conversations": [_entry(cid="c9")]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"
    assert s.list_calls == 3


async def test_truncated_preview_still_matches():
    preview = TEXT.strip()[:30] + "…"
    s = _FakeSession(post=(200, ""), lists=[{"conversations": [_entry(preview=preview)]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"


@pytest.mark.parametrize("entry", [
    _entry(ad_id="12"),                       # another ad
    _entry(mine=False),                       # newest message is the seller's
    _entry(preview="Ganz anderer Text"),      # not our message
    _entry(preview=None),                     # no preview to compare with
])
async def test_non_matching_entries_are_not_taken(entry):
    s = _FakeSession(post=(200, {"sent": True}), lists=[{"conversations": [entry]}])
    with pytest.raises(OpenerUnconfirmed, match="did not show up"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 3                  # all three polls were made


@pytest.mark.parametrize("post", [(400, {"errorMessage": "Anzeige nicht verfügbar"}), (200, {"sent": False})])
async def test_willhaben_refusal_is_send_failed(post):
    s = _FakeSession(post=post)
    with pytest.raises(SendFailed, match="rejected the new conversation"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.list_calls == 0


async def test_logged_out_is_not_logged_in():
    s = _FakeSession(post=(401, ""), logged_out=True)
    with pytest.raises(NotLoggedIn):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.logged_in is False and len(s.posts) == 1          # marked logged out, no second POST


async def test_forbidden_contact_form_keeps_the_login():
    s = _FakeSession(post=(403, {}))                           # e.g. contact blocked; the profile still works
    with pytest.raises(SendFailed, match="refused the contact form"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert s.logged_in is True and s.list_calls == 0


@pytest.mark.parametrize("text", ["", "   \n"])
async def test_empty_text_is_value_error(text):
    s = _FakeSession()
    with pytest.raises(ValueError):
        await AccountClient(s).create_conversation("77", text)
    assert s.posts == []                      # nothing is sent


async def test_counts_as_a_send_in_the_access_log():
    s = _session([{"status": 200, "text": '{"sent": true, "conversationId": "c1"}', "url": "x"}])
    await s.api("POST", SEND_REQUEST_CHAT, {"adId": 1})
    assert _counts() == {("account", "send", "ok"): 1}


async def test_unconfirmed_opener_is_its_own_error():
    s = _FakeSession(post=(200, {"sent": True}), lists=[{"conversations": []}])
    with pytest.raises(OpenerUnconfirmed):                      # sent (2xx) but not found: must not be retried blindly
        await AccountClient(s).create_conversation("77", TEXT)
    assert not issubclass(OpenerUnconfirmed, SendFailed)       # "except SendFailed" must never treat it as unsent


@pytest.mark.parametrize("error", [RuntimeError("HTTP 503"), NotLoggedIn("gone")])
async def test_error_after_the_post_is_unconfirmed(error):
    s = _FakeSession(post=(200, {"sent": True}), list_error=error)
    with pytest.raises(OpenerUnconfirmed, match="could not be looked up"):
        await AccountClient(s).create_conversation("77", TEXT)
    assert len(s.posts) == 1


async def test_seller_answered_already_the_history_decides():
    mine = {"message_id": "m1", "text": TEXT.strip(), "is_mine": True, "created_at": "2026-10-10T10:00:00Z"}
    reply = {"message_id": "m2", "text": "Ja, noch da", "is_mine": False, "created_at": "2026-10-10T10:05:00Z"}
    s = _FakeSession(post=(200, {"sent": True}), history={"c9": [mine, reply], "c8": [reply]},
                     lists=[{"conversations": [_entry(cid="c8", preview="Ja, noch da", mine=False),
                                               _entry(cid="c9", preview="Ja, noch da", mine=False)]}])
    assert await AccountClient(s).create_conversation("77", TEXT) == "c9"



def test_the_ad_pane_writes_to_the_seller_and_opens_the_chat(monkeypatch):
    """"Message the seller": the opener opens the chat, it is kept as a buyer's chat with that message, the chat page
    follows; with a chat about the ad already there that one opens and nothing is sent."""
    from fastapi.testclient import TestClient

    from app.db import Conversation, Message, session
    from app.main import app
    from app.services import chats

    sent = []

    class Acc:
        async def create_conversation(self, ad_id, text):
            sent.append((ad_id, text))
            return "conv-new-1"

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(chats, "_account", lambda: Acc())
    monkeypatch.setattr(chats, "sync_messages", nothing)
    monkeypatch.setattr(chats, "ensure_ad_context", nothing)

    class Pub:
        async def get_detail(self, ad_id):
            return None

    monkeypatch.setattr("app.willhaben.public.get_public", lambda: Pub())
    with TestClient(app) as c:
        r = c.post("/hits/ad/willhaben/987654/message", data={"text": " Hallo, noch da? "}, follow_redirects=False)
        assert r.status_code in (302, 303) and r.headers["location"].endswith("/chats/conv-new-1")
        assert sent == [("987654", "Hallo, noch da?")]
        with session() as db:
            conv = db.get(Conversation, "conv-new-1")
            assert conv.role == "buyer" and conv.ad_id == "987654"
            assert db.query(Message).filter(Message.conversation_id == "conv-new-1").count() == 1
        r = c.post("/hits/ad/willhaben/987654/message", data={"text": "noch einmal"}, follow_redirects=False)
        assert r.headers["location"].endswith("/chats/conv-new-1") and len(sent) == 1      # no second opener
        assert chats.chat_for_ad("987654") == "conv-new-1"


def test_our_copy_of_the_opener_merges_with_willhabens_despite_its_line_breaks():
    """willhaben gives "\\r\\n" back as " \\n": the opener must not show twice."""
    from datetime import timedelta

    from app.db import Conversation, Message, session, utcnow
    from app.services import chats

    now = utcnow()
    with session() as db:
        db.add(Conversation(id="conv-crlf", role="buyer", ad_id="1"))
        db.add(Message(conversation_id="conv-crlf", ext_id="draft-contact-conv-crlf", direction="out",
                       text="Hallo, ist preislich noch was machbar?\r\nLG", created_at=now))
        db.add(Message(conversation_id="conv-crlf", ext_id="133e18d0-7b0e-48c0-ba33-5879a75acacb", direction="out",
                       text="Hallo, ist preislich noch was machbar? \nLG", created_at=now + timedelta(seconds=3)))
        db.commit()
        chats._drop_sent_placeholders(db, "conv-crlf")
        db.commit()
        left = [m.ext_id for m in db.query(Message).filter(Message.conversation_id == "conv-crlf")]
    assert left == ["133e18d0-7b0e-48c0-ba33-5879a75acacb"]
    assert not chats._same_text("Hallo", "Hallo du")
