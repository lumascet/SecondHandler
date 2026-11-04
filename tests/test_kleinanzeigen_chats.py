"""Kleinanzeigen chats through the web inbox's API (a fake Box: no browser): the inbox and a conversation into the
chat list, a reply sent and stored once, a logout noted. The data below is made up in the API's shape."""
import pytest
from sqlmodel import select

from app.db import Conversation, Draft, Message, session
from app.platforms import kleinanzeigen_inbox as ki
from app.platforms import kleinanzeigen_messaging as km

CID = "3ab:5kxyz:2qabc"


def _msg(n, bound, text, at, kind="MESSAGE"):
    return {"messageId": f"m{n}", "type": kind, "boundness": bound, "receivedDate": at, "textShort": text[:20],
            "title": "Echo Show 8", "attachments": [],
            "unichatMessage": {"payload": {"type": "chat/text-message", "content": {"text": text}}}}


def _conv(messages=None, unread=True, at="2026-11-04T19:40:00.000+0100"):
    return {"id": CID, "role": "Seller", "buyerName": "Käufer", "sellerName": "Ich", "userIdBuyer": 11,
            "userIdSeller": 22, "adId": "3200000001", "adTitle": "Echo Show 8", "adImage": "https://img.example/1.jpg",
            "adPriceInEuroCent": 4500, "adStatus": "ACTIVE", "unread": unread, "unreadMessagesCount": 1 if unread else 0,
            "boundness": "INBOUND", "textShortTrimmed": "Noch da?", "receivedDate": at,
            **({"messages": messages} if messages is not None else {})}


class FakeBox:
    """The Box's calls, answered from `data`; `sent` records the replies."""
    inbox_data: dict = {}
    convs: dict = {}
    sent: list = []
    refuse = False

    async def __aenter__(self):
        if FakeBox.refuse:
            raise km.LoggedOut("not logged in")
        return self

    async def __aexit__(self, *e):
        return False

    async def inbox(self, size=50):
        return FakeBox.inbox_data

    async def conversation(self, cid):
        return FakeBox.convs[cid]

    async def reply(self, cid, text):
        FakeBox.sent.append((cid, text))
        FakeBox.convs[cid]["messages"].append(_msg(9, "OUTBOUND", text, "2026-11-04T19:45:00.000+0100"))
        return 201, None


@pytest.fixture
def box(monkeypatch):
    from app import platforms
    from app.services import chats
    from app.willhaben import session as ws

    FakeBox.inbox_data, FakeBox.convs, FakeBox.sent, FakeBox.refuse = {"conversations": [_conv()]}, {}, [], False
    FakeBox.convs[CID] = _conv([_msg(1, "INBOUND", "Hallo, ist das Echo Show 8 noch da?", "2026-11-04T19:40:00.000+0100"),
                                _msg(2, None, "Sicher bezahlen", "2026-11-04T19:41:00.000+0100",
                                     "PAYMENT_AND_SHIPPING_MESSAGE")])
    monkeypatch.setattr(km, "Box", FakeBox)
    monkeypatch.setattr(platforms, "enabled", lambda pid: True)
    monkeypatch.setattr(type(ws.get_session()), "started", property(lambda self: True))

    async def no_ad(cid, force=False):
        return None
    monkeypatch.setattr(chats, "ensure_ad_context", no_ad)
    ki._last_sync[0] = 0
    return FakeBox


async def test_the_inbox_and_its_conversation_come_into_the_chat_list(box):
    res = await ki.sync(force=True)
    assert res == {"conversations": 1, "opened": 1}
    with session() as db:
        c = db.get(Conversation, CID)
        msgs = list(db.exec(select(Message).where(Message.conversation_id == CID).order_by(Message.created_at)))
    assert c.platform == "kleinanzeigen" and c.role == "seller" and c.partner_name == "Käufer"   # I sell: the buyer
    assert c.ad_id == "3200000001" and c.ad_context["price"] == 45.0 and c.ad_context["url"].endswith("3200000001")
    assert [(m.direction, m.kind) for m in msgs] == [("in", None), ("unknown", "integration")]
    assert msgs[0].text == "Hallo, ist das Echo Show 8 noch da?"                     # the full text, not the short one
    assert ki.is_kleinanzeigen(CID)


async def test_a_new_message_of_the_other_person_is_pushed_once(box, monkeypatch):
    from app.db import set_setting
    pushes = []

    async def notify(kind, title, text, **kw):
        pushes.append(title)
    monkeypatch.setattr("app.services.notify.notify", notify)
    set_setting("notify_messages", "1")
    await ki.sync(force=True)                                    # first sync of all: no news
    box.inbox_data = {"conversations": [_conv(at="2026-11-04T19:50:00.000+0100")]}
    await ki.sync(force=True)
    await ki.sync(force=True)
    assert pushes == ["Kleinanzeigen: message from Käufer"]


async def test_a_reply_is_sent_and_stored_once(box):
    from app.services import chats
    await ki.sync(force=True)
    with session() as db:
        d = Draft(conversation_id=CID, text="Ja, ist noch da.")
        db.add(d)
        db.commit()
        db.refresh(d)
    await chats._send_claimed(d, "Ja, ist noch da.")
    assert box.sent == [(CID, "Ja, ist noch da.")]
    with session() as db:
        mine = list(db.exec(select(Message).where(Message.conversation_id == CID, Message.direction == "out")))
    assert [m.ext_id for m in mine] == ["ka-m9"]                 # Kleinanzeigen's copy, no second one from the draft


async def test_a_logout_is_noted_and_nothing_breaks(box):
    from app.services import site_accounts
    box.refuse = True
    assert await ki.sync(force=True) == {"skipped": "not logged in"}
    assert site_accounts.state("kleinanzeigen")["logged_in"] is False
    assert await ki.sync(force=True) == {"skipped": "not logged in"}     # no tab opened while known logged out


def test_message_text():
    assert ki.message_text(_msg(1, "INBOUND", "Wann kann ich es abholen?", "x")) == ("Wann kann ich es abholen?", None)
    text, kind = ki.message_text(_msg(2, None, "Zahlung erhalten", "x", "PAYMENT_AND_SHIPPING_MESSAGE"))
    assert kind == "integration" and "Zahlung erhalten" in text
