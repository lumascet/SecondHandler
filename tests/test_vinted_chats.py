"""Vinted chats in the chat list: the inbox and conversation answers of Vinted's web app (shapes as recorded, made-up
content) become Conversation and Message rows; sending is refused for now."""
import pytest
from fastapi.testclient import TestClient

from app.db import Conversation, Message, session
from app.platforms import vinted_inbox
from app.services import chats

INBOX = {"conversations": [{
    "id": "01a1-uuid", "conversation_type": "order", "created_at": "2026-10-20T10:00:00+02:00",
    "is_unread_by_current_user": True,
    "data": {"inventory_items": [{"id": 555, "title": "HomePod mini", "photos": [{"url": "https://img/inbox.webp"}]}]},
    "last_message": {"id": "m2", "created_at": "2026-10-21T12:00:00+02:00", "sender_id": "77",
                     "message_type": "legacy_last_message", "data": {"content": "Ist versendet!"}},
    "opposite_users": [{"type": "user", "data": {"id": 77, "login_name": "verkaeufer_x"}}]}],
    "pagination_temp": {"has_next": False}}
CONV = {"id": "01a1-uuid", "allow_reply": True, "data": {"order_id": "999"},
        "opposite_users": [{"type": "user", "data": {"id": 77, "login_name": "verkaeufer_x"}}],
        "messages": [
            {"id": "e1", "created_at": "2026-10-20T10:00:00+02:00", "sender_id": "1", "message_type": "legacy_action",
             "data": {"entity": {"title": "Bestellung bezahlt", "subtitle": "Der Verkäufer verschickt sie."}}},
            {"id": "m1", "created_at": "2026-10-20T11:00:00+02:00", "sender_id": "5", "message_type": "message",
             "data": {"body": "Danke, wann schickst du?"}},
            {"id": "m2", "created_at": "2026-10-21T12:00:00+02:00", "sender_id": "77", "message_type": "message",
             "data": {"body": "Ist versendet!"}}]}


def test_inbox_and_conversation_become_rows():
    assert vinted_inbox.apply_inbox(INBOX) == ["01a1-uuid"]
    with session() as db:
        c = db.get(Conversation, "01a1-uuid")
    assert c.platform == "vinted" and c.ad_id == "555" and c.partner_name == "verkaeufer_x" and c.unread_count == 1
    assert c.last_message_text == "Ist versendet!" and c.ad_context["url"] == "https://www.vinted.at/items/555"
    vinted_inbox.apply_conversation("01a1-uuid", CONV)
    msgs = chats.messages_of("01a1-uuid")
    assert [(m.direction, m.kind, m.text) for m in msgs] == [
        ("unknown", "integration", "Bestellung bezahlt – Der Verkäufer verschickt sie."),
        ("out", None, "Danke, wann schickst du?"), ("in", None, "Ist versendet!")]
    assert vinted_inbox.apply_inbox(INBOX) == []                    # nothing new: not opened again


async def test_sending_on_vinted_goes_through_its_page_and_keeps_vinteds_id(monkeypatch):
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        d = chats.Draft(conversation_id="01a1-uuid", text="Danke!")
        db.add(d)
        db.commit()
        db.refresh(d)
    sent = []

    async def send(cid, text):
        sent.append((cid, text))
        return {"id": "uuid-9", "created_at": "2026-10-21T22:00:00+02:00", "message_type": "reply_plain",
                "data": {"content": text, "id": "m9"}}
    monkeypatch.setattr(vinted_inbox, "send", send)
    marked = []

    async def no_read(cid):
        marked.append(cid)
    monkeypatch.setattr(chats, "mark_read", no_read)
    out = await chats.send_draft(d.id, "Danke!")
    assert out.status == "sent" and sent == [("01a1-uuid", "Danke!")] and marked == []      # no willhaben receipt
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [
        {"id": "x-uuid", "created_at": "2026-10-21T22:00:00+02:00", "sender_id": "5", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "entity": {"id": "m9", "body": "Danke!"}}}]})
    assert [m.text for m in chats.messages_of("01a1-uuid")].count("Danke!") == 1                # no double


async def test_an_unconfirmed_vinted_send_keeps_the_draft(monkeypatch):
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        d = chats.Draft(conversation_id="01a1-uuid", text="Danke!")
        db.add(d)
        db.commit()
        db.refresh(d)

    async def send(cid, text):
        raise vinted_inbox.SendError("Vinted answered HTTP 403")
    monkeypatch.setattr(vinted_inbox, "send", send)
    with pytest.raises(chats.SendFailed, match="Vinted"):
        await chats.send_draft(d.id, "Danke!")
    with session() as db:
        row = db.get(chats.Draft, d.id)
    assert row.status == "draft" and "403" in row.error
    assert await chats.mark_read("01a1-uuid") == 0


def test_the_chat_page_links_to_vinted():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", CONV)
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats/01a1-uuid").text
        assert 'href="https://www.vinted.at/inbox/01a1-uuid"' in page and "Ist versendet!" in page
        assert "Vinted" in c.get("/chats").text


async def test_sync_reads_inbox_then_changed_conversations(monkeypatch):
    import json

    class Sess:
        started = True
        opened = []
    sess = Sess()
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: sess)

    async def catch(url, part, timeout_ms=0, extra=None):
        sess.opened.append(url)
        return json.loads(json.dumps(INBOX if url.endswith("/inbox") else CONV))
    monkeypatch.setattr(vinted_inbox, "_catch", catch)
    res = await vinted_inbox.sync(force=True)
    assert res == {"conversations": 1, "opened": 1}
    assert sess.opened == ["https://www.vinted.at/inbox", "https://www.vinted.at/inbox/01a1-uuid"]
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").messages_synced_at is not None
        assert len(list(db.exec(Message.__table__.select()))) == 3


def test_the_chat_list_filters_by_site():
    from app.db import set_setting
    set_setting("platform_vinted_enabled", "1")
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner", ad_title="Router"))
        db.commit()
    from app.main import app
    with TestClient(app) as c:
        both = c.get("/chats").text
        only_v = c.get("/chats?site=vinted").text
        only_w = c.get("/chats?site=willhaben").text
    assert "data-chat-site" in both and "verkaeufer_x" in both and "wh_partner" in both
    assert "verkaeufer_x" in only_v and "wh_partner" not in only_v
    assert "wh_partner" in only_w and "verkaeufer_x" not in only_w


def test_the_side_of_an_items_conversation_is_the_role():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "data": {"current_user_side": "buyer", "items": []}})
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").role == "buyer"


async def test_a_new_message_from_the_other_side_is_pushed(monkeypatch):
    import copy
    import json

    from app.db import set_setting
    set_setting("notify_messages", "1")
    pushes = []

    async def notify(kind, title, message, **kw):
        pushes.append((kind, title, message))
    monkeypatch.setattr("app.services.notify.notify", notify)
    state = {"inbox": copy.deepcopy(INBOX)}

    class Sess:
        started = True
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: Sess())

    async def catch(url, part, timeout_ms=0, extra=None):
        return json.loads(json.dumps(state["inbox"] if url.endswith("/inbox") else CONV))
    monkeypatch.setattr(vinted_inbox, "_catch", catch)
    await vinted_inbox.sync(force=True)
    assert pushes == []                                                  # the first sync: no news
    state["inbox"]["conversations"][0]["last_message"] = {
        "id": "m3", "created_at": "2026-10-21T23:00:00+02:00", "sender_id": "77",
        "message_type": "legacy_last_message", "data": {"content": "Noch Fragen?"}}
    await vinted_inbox.sync(force=True)
    assert pushes == [("message", "Vinted: message from verkaeufer_x", "HomePod mini: Noch Fragen?")]



async def test_live_events_and_the_reloaded_inbox(monkeypatch):
    import json

    opened = []

    async def sync_one(cid):
        opened.append(cid)
    monkeypatch.setattr(vinted_inbox, "sync_one", sync_one)
    await vinted_inbox._on_live(json.dumps({"_state": "open", "status": 200}))
    assert vinted_inbox.live["state"] == "connected"
    await vinted_inbox._on_live(json.dumps({"message_type": "refresh_conversation",
                                            "data": {"conversation_id": "01a1-uuid", "user_msg_thread_id": 5}}))
    await vinted_inbox._on_live(json.dumps({"message_type": "refresh_inbox", "data": {}}))
    assert opened == ["01a1-uuid"]

    class Resp:
        url = "https://api.vinted.at/messaging/main/inbox?page=1"
        status = 200
        request = type("Q", (), {"method": "GET"})()

        async def text(self):
            return json.dumps(INBOX)
    await vinted_inbox._on_live_response(Resp())
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").partner_name == "verkaeufer_x"


def test_a_site_not_set_up_does_not_show():
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/chats").text                      # no chats, not logged in anywhere, Vinted off
    assert "data-vinted-live" not in page and "data-chat-site" not in page and "Not logged in to willhaben" not in page


def test_the_live_lines_name_their_site():
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner"))
        db.commit()
    vinted_inbox.live.update(state="connected")
    with TestClient(app) as c:
        page = c.get("/chats").text
    live = page[page.index('id="chat-live-state"'):page.index('id="chat-filters"')]
    assert 'data-live-site="willhaben" data-live="0"' in live and "text-error" in live      # not live in tests
    assert 'data-live-site="vinted" data-vinted-live data-live="1"' in live and "text-success" in live
    assert ">offline</span>" in live and ">online</span>" in live
    vinted_inbox.live.update(state="off")


def test_a_legacy_reply_is_a_written_message():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [
        {"id": "r1", "created_at": "2026-10-21T21:56:44+02:00", "sender_id": "5", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "created_at_ts": "x", "entity": {"body": "Ist das noch da?", "id": 1,
                                                                                "user_id": 5, "photos": []}}},
        {"id": "r2", "created_at": "2026-10-21T22:30:00+02:00", "sender_id": "77", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "entity": {"body": "Ja!", "id": 2, "user_id": 77}}}]})
    assert [(m.direction, m.kind, m.text) for m in chats.messages_of("01a1-uuid")] == [
        ("out", None, "Ist das noch da?"), ("in", None, "Ja!")]


async def test_the_item_card_comes_from_vinteds_item_page(monkeypatch):
    from app import platforms
    from app.platforms.types import AdDetail
    vinted_inbox.apply_inbox(INBOX)
    asked = []

    class V:
        async def detail(self, ad_id, url=None):
            asked.append((ad_id, url))
            return AdDetail(ad_id=ad_id, heading="HomePod mini", description="Wie neu", price=50.0,
                            price_display="€ 50,00", status="sold", published_at=None, first_published_at=None,
                            end_date=None, url="https://www.vinted.at/items/555", images=["https://img/1.webp"],
                            platform="vinted")
    real_get = platforms.get
    monkeypatch.setattr(platforms, "get", lambda pid: V() if pid == "vinted" else real_get(pid))
    conv = await chats.ensure_ad_context("01a1-uuid")
    assert asked == [("555", "https://www.vinted.at/items/555")]
    assert conv.ad_context["price"] == "€ 50,00" and conv.ad_context["images"] == ["https://img/1.webp"]
    assert conv.ad_context["platform"] == "vinted"
    await chats.ensure_ad_context("01a1-uuid")
    assert len(asked) == 1                                               # fresh: not asked again
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        page = c.get("/chats/01a1-uuid").text
    assert 'src="https://img/1.webp"' in page and "€ 50,00" in page


def test_each_chat_carries_its_sites_coloured_mark():
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Conversation(id="wh-1", partner_name="wh_partner"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/chats").text
    assert 'data-site-mark="vinted"' in page and 'data-site-mark="willhaben"' in page
    assert "color: #0f9fa8" in page and "color: #2f7fd8" in page


async def test_a_sold_items_card_keeps_the_inbox_photo(monkeypatch):
    from app import platforms
    from app.platforms.types import AdDetail
    vinted_inbox.apply_inbox(INBOX)

    class V:
        async def detail(self, ad_id, url=None):
            return AdDetail(ad_id=ad_id, heading="HomePod mini", description="", price=50.0, price_display="50 €",
                            status="sold", published_at=None, first_published_at=None, end_date=None, url=url,
                            images=[], platform="vinted")
    real_get = platforms.get
    monkeypatch.setattr(platforms, "get", lambda pid: V() if pid == "vinted" else real_get(pid))
    conv = await chats.ensure_ad_context("01a1-uuid")
    assert conv.ad_context["images"] == ["https://img/inbox.webp"] and conv.ad_context["status"] == "sold"


def test_an_orders_side_comes_from_the_order():
    vinted_inbox.apply_inbox(INBOX)
    vinted_inbox.apply_conversation("01a1-uuid", CONV, {"escrow_order": {"current_user_side": "buyer"}})
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").role == "buyer"


def test_no_side_no_tag():
    vinted_inbox.apply_inbox(INBOX)
    from app.db import set_setting
    from app.main import app
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        page = c.get("/chats").text
    row = page[page.index("verkaeufer_x"):][:900]
    assert "Selling" not in row and "Buying" not in row


def test_an_event_with_a_new_id_on_every_load_is_stored_once():
    vinted_inbox.apply_inbox(INBOX)
    ev = {"created_at": "2026-10-21T18:44:03+02:00", "sender_id": "1", "message_type": "legacy_action",
          "data": {"event_type": "success", "entity_type": "action_message", "entity": {"title": "Gekauft!", "subtitle": "x"}}}
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [{**ev, "id": "a1"}]})
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [{**ev, "id": "b2"}]})
    assert [m.text for m in chats.messages_of("01a1-uuid")] == ["Gekauft! – x"]


def test_old_unstable_rows_are_replaced():
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Message(conversation_id="01a1-uuid", ext_id="vinted-oldid", direction="unknown", text="Gekauft! – x",
                       kind="integration"))
        db.commit()
    ev = {"id": "c3", "created_at": "2026-10-21T18:44:03+02:00", "sender_id": "1", "message_type": "legacy_action",
          "data": {"event_type": "success", "entity": {"title": "Gekauft!", "subtitle": "x"}}}
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [ev]})
    assert [m.ext_id.startswith("vinted-ev-") for m in chats.messages_of("01a1-uuid")] == [True]


def test_a_members_country_comes_from_their_profile_page():
    from app.platforms.vinted import parse_member_country
    page = ('…"login":"x","country_id":2,"city":"","country_code":"DE",…'
            '"country_title_local":"Deutschland","country_iso_code":"DE","country_title":"Deutschland"…')
    assert parse_member_country(page) == "DE" and parse_member_country("no user") is None


@pytest.mark.parametrize("country,says", [("AT", "auf Deutsch"), ("DE", "auf Deutsch"),
                                          ("IT", "Landessprache seines Landes"),
                                          (None, "in der der Gesprächspartner selbst schreibt")])
async def test_answers_on_vinted_follow_the_partners_language(monkeypatch, country, says):
    from app.platforms import vinted
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        assert db.get(Conversation, "01a1-uuid").ad_context["partner_id"] == "77"
    asked = []

    class Client:
        async def member(self, user_id, cache_s=None):
            asked.append(user_id)
            return {"country": country, "country_title": None, "city": None, "online": None, "last_seen": None}
    monkeypatch.setattr(vinted, "get_client", lambda: Client())
    with session() as db:
        conv = db.get(Conversation, "01a1-uuid")
    out = await chats.compose(conv, [("in", None, "Hi, still available?")], auto=True, ad=None)   # as real drafts do
    assert "Vinted" in out["prompt"]                          # the chat's own site, without being told
    rules = out["prompt"].split("Meine Regeln:")[1]
    assert "Sprache:" in rules and says in rules
    with session() as db:
        conv = db.get(Conversation, "01a1-uuid")
    await chats.compose(conv, [("in", None, "Hi")], auto=False, ad=None, platform="vinted")
    assert asked == ["77"]                                    # read once, kept with the chat



def test_the_chat_shows_where_the_other_person_is_from():
    from app.platforms.vinted import parse_member
    page = ('"login":"x","expose_location":true,"city":"Milano","country_code":"IT","is_online":false,'
            '"country_title_local":"Italia","country_iso_code":"IT","country_title":"Italien"')
    m = parse_member(page)
    assert (m["country"], m["country_title"], m["city"]) == ("IT", "Italien", "Milano")
    assert parse_member(page.replace('"expose_location":true', '"expose_location":false'))["city"] is None
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        conv = db.get(Conversation, "01a1-uuid")
    chats._keep_place(conv, m)
    assert chats.partner_place(conv) == "Milano, 🇮🇹 Italien"
    from app.main import app
    from app.db import set_setting
    set_setting("platform_vinted_enabled", "1")
    with TestClient(app) as c:
        assert "🇮🇹 Italien" in c.get("/chats/01a1-uuid").text


def test_the_place_found_after_the_page_opened_comes_in_by_itself():
    vinted_inbox.apply_inbox(INBOX)
    before = chats.live_state("01a1-uuid")["presence"]
    with session() as db:
        conv = db.get(Conversation, "01a1-uuid")
    chats._keep_place(conv, {"country": "IT", "country_title": "Italien", "city": "Brescia"})
    assert chats.live_state("01a1-uuid")["presence"] != before         # the open page swaps the header part



def test_the_willhaben_partners_place_from_their_profile():
    from app.willhaben.public import parse_member_place
    page = {"props": {"pageProps": {"sellerProfileWrapper": {"sellerProfile": {"location": "8530 Deutschlandsberg"}}}}}
    assert parse_member_place(page) == "8530 Deutschlandsberg"
    ads = [{"attributes": {"attribute": [{"name": "POSTCODE", "values": ["1160"]}, {"name": "LOCATION", "values": ["Wien"]}]}}] * 2
    page = {"props": {"pageProps": {"sellerProfileWrapper": {"sellerProfile": {"location": None}},
                                    "searchResult": {"advertSummaryList": {"advertSummary": ads}}}}}
    assert parse_member_place(page) == "1160 Wien" and parse_member_place({}) is None
    with session() as db:
        db.add(Conversation(id="w1", partner_name="Erika", partner_place="8530 Deutschlandsberg"))
        db.commit()
        conv = db.get(Conversation, "w1")
    assert chats.partner_place(conv) == "8530 Deutschlandsberg"


async def test_a_message_the_live_event_brings_is_pushed_once(monkeypatch):
    """10-27: no Vinted push at all – the live event stored a new message first (sync_one, no push), and the inbox
    read right after found nothing newer. The live event pushes it now; the inbox does not push it a second time."""
    import copy
    import json

    from app.db import set_setting
    set_setting("notify_messages", "1")
    pushes = []

    async def notify(kind, title, message, **kw):
        pushes.append(message)
    monkeypatch.setattr("app.services.notify.notify", notify)

    class Sess:
        started = True
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: Sess())
    conv = copy.deepcopy(CONV)

    async def catch(url, part, timeout_ms=0, extra=None):
        return copy.deepcopy(INBOX if url.endswith("/inbox") else conv)
    monkeypatch.setattr(vinted_inbox, "_catch", catch)

    async def card(cid):
        return None
    monkeypatch.setattr(chats, "ensure_ad_context", card)
    await vinted_inbox.sync(force=True)                                    # known, no news
    assert pushes == []
    conv["messages"].append({"id": "m3", "created_at": "2026-10-21T23:00:00+02:00", "sender_id": "77",
                             "message_type": "message", "data": {"body": "Noch Fragen?"}})
    await vinted_inbox._on_live(json.dumps({"message_type": "refresh_conversation",
                                            "data": {"conversation_id": "01a1-uuid"}}))
    assert len(pushes) == 1                                                # the live event pushed it

    inbox = copy.deepcopy(INBOX)
    inbox["conversations"][0]["last_message"] = {"id": "m3", "created_at": "2026-10-21T23:00:00+02:00",
                                                 "sender_id": "77", "message_type": "legacy_last_message",
                                                 "data": {"content": "Noch Fragen?"}}

    class Resp:
        url = "https://api.vinted.at/messaging/main/inbox?page=1"
        status = 200
        request = type("Q", (), {"method": "GET"})()

        async def text(self):
            return json.dumps(inbox)
    await vinted_inbox._on_live_response(Resp())                          # the reloaded inbox: the same message
    await vinted_inbox.sync(force=True)
    assert len(pushes) == 1


async def test_an_orders_shipping_update_is_read_and_pushed(monkeypatch):
    """10-27: the shipping updates of an order were missing – they do not change the inbox's last message, and the
    chat's page did not answer several times in a row. An order's chat is read again every 30 min, a page that does
    not answer gets a second try, and a new order event is pushed once."""
    import copy
    from datetime import timedelta

    from app.db import set_setting, utcnow
    set_setting("notify_messages", "1")
    pushes = []

    async def notify(kind, title, message, **kw):
        pushes.append((title, message))
    monkeypatch.setattr("app.services.notify.notify", notify)

    class Sess:
        started = True
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: Sess())
    monkeypatch.setattr(vinted_inbox, "LOAD_RETRY_S", 0)
    conv = copy.deepcopy(CONV)
    asked = {"conv": 0, "fail": 0}

    async def catch(url, part, timeout_ms=0, extra=None):
        if url.endswith("/inbox"):
            return copy.deepcopy(INBOX)
        asked["conv"] += 1
        if asked["fail"]:
            asked["fail"] -= 1
            return None                                                  # the page did not answer
        return copy.deepcopy(conv)
    monkeypatch.setattr(vinted_inbox, "_catch", catch)

    async def card(cid):
        return None
    monkeypatch.setattr(chats, "ensure_ad_context", card)
    await vinted_inbox.sync(force=True)                                  # known now; no news
    n = asked["conv"]
    await vinted_inbox.sync(force=True)
    assert asked["conv"] == n                                            # just read: not again
    with session() as db:                                                # half an hour later
        row = db.get(Conversation, "01a1-uuid")
        row.messages_synced_at = utcnow() - timedelta(minutes=31)
        row.last_message_at = utcnow() - timedelta(days=1)
        db.add(row)
        db.commit()
    conv["messages"].append({"id": "e9", "created_at": "2026-10-22T09:00:00+02:00", "sender_id": "1",
                             "message_type": "legacy_action", "data": {"entity": {"title": "Versendet",
                                                                                   "subtitle": "Das Paket ist unterwegs."}}})
    asked["fail"] = 1                                                    # the first try fails, the second answers
    await vinted_inbox.sync(force=True)
    assert pushes == [("Vinted · HomePod mini", "Versendet – Das Paket ist unterwegs.")]
    with session() as db:
        row = db.get(Conversation, "01a1-uuid")
        row.messages_synced_at = utcnow() - timedelta(minutes=31)
        db.add(row)
        db.commit()
    await vinted_inbox.sync(force=True)                                  # read again: the same event, no 2nd push
    assert len(pushes) == 1


def test_vinteds_own_translation_gives_way_to_the_original(caplog):
    """10-27: an Italian seller's message came as Vinted's German machine translation. Where Vinted carries the
    original beside it, the original is stored; the field names are logged once (no content)."""
    import logging
    m = {"id": "m5", "message_type": "legacy_reply", "sender_id": "77",
         "data": {"entity_type": "message", "entity": {"id": 5, "body": "Paket wurde versendet",
                                                        "original_body": "Pacco spedito"}}}
    with caplog.at_level(logging.INFO):
        assert vinted_inbox.message_text(m) == ("Pacco spedito", None)
        vinted_inbox.log_shape(m)
    assert "original_body" in caplog.text and "Pacco" not in caplog.text
    plain = {"id": "m6", "message_type": "legacy_reply", "data": {"entity_type": "message", "entity": {"body": "Ciao"}}}
    assert vinted_inbox.message_text(plain) == ("Ciao", None)


def test_a_stored_translation_is_replaced_by_the_original():
    from app.db import Message
    first = {"id": "01a1-uuid", "opposite_users": CONV["opposite_users"], "messages": [
        {"id": "m7", "created_at": "2026-10-27T13:22:00+01:00", "sender_id": "77", "message_type": "legacy_reply",
         "data": {"entity_type": "message", "entity": {"id": 7, "body": "Paket wurde versendet"}}}]}
    vinted_inbox.apply_conversation("01a1-uuid", first)
    from sqlmodel import select
    with session() as db:
        m = db.exec(select(Message).where(Message.conversation_id == "01a1-uuid")).one()
        m.lang, m.translation = "de", None
        db.add(m)
        db.commit()
    first["messages"][0]["data"]["entity"]["original_body"] = "Pacco spedito"
    vinted_inbox.apply_conversation("01a1-uuid", first)
    with session() as db:
        m = db.exec(select(Message).where(Message.conversation_id == "01a1-uuid")).one()
    assert m.text == "Pacco spedito" and m.lang is None                    # asked again for its translation


async def test_the_language_they_write_in_decides(monkeypatch):
    """10-27: an Italian seller got German drafts. The language their messages are in (services/translate) wins."""
    from app.db import Message
    from app.platforms import vinted
    vinted_inbox.apply_inbox(INBOX)

    class Client:
        async def member(self, user_id, cache_s=None):
            return {"country": "DE", "country_title": None, "city": None, "online": None, "last_seen": None}
    monkeypatch.setattr(vinted, "get_client", lambda: Client())
    with session() as db:
        db.add(Message(conversation_id="01a1-uuid", ext_id="i1", direction="in", text="Oggi penso proprio di sì.",
                       lang="it"))
        db.commit()
        conv = db.get(Conversation, "01a1-uuid")
    out = await chats.compose(conv, [("in", None, "Oggi penso proprio di sì.")], auto=False, ad=None)
    rules = out["prompt"].split("Meine Regeln:")[1]
    assert "schreibt Italienisch: schreib deine Antwort auf Italienisch" in rules and "auf Deutsch." not in rules


async def test_a_refused_live_stream_is_reloaded_once_then_left_alone(monkeypatch):
    """10-28/29: the live tab's stream was refused (401) every 2 s for 18 hours – its login ran out and the page
    never renewed it, and the watchdog took the refusals for signs of life."""
    import asyncio
    import json as _json

    from app.platforms import vinted_inbox as vi

    class Page:
        def __init__(self):
            self.reloads, self.closed = 0, False

        def is_closed(self):
            return self.closed

        async def reload(self, **kw):
            self.reloads += 1

        async def close(self):
            self.closed = True
    page = Page()
    monkeypatch.setattr(vi, "live", {"state": "connected", "since": None, "last_event": None, "events": 0, "page": page})
    refused = _json.dumps({"_state": "open", "status": 401})
    for _ in range(vi.LIVE_REFUSED_MAX):
        await vi._on_live(refused)
    await asyncio.sleep(0)
    await asyncio.gather(*list(vi._live_tasks))
    assert page.reloads == 1 and not page.closed and vi.live["last_event"] is None   # no sign of life
    for _ in range(vi.LIVE_REFUSED_MAX):
        await vi._on_live(refused)
    await asyncio.sleep(0)
    await asyncio.gather(*list(vi._live_tasks))
    assert page.closed and page.reloads == 1 and vi.live["state"] == "refused" and vi.live["retry_at"]
    await vi._on_live(_json.dumps({"_state": "open", "status": 200}))
    assert vi.live["refused"] == 0 and vi.live["renewals"] == 0 and vi.live["state"] == "connected"


def test_offers_in_a_vinted_chat_are_read_with_their_amounts():
    """10-29: the owner's counter-offer was dropped (no title), the buyer's offer lacked its amount and status."""
    from app.platforms import vinted_inbox as vi
    ask = {"message_type": "legacy_offer_request", "data": {"entity_type": "offer_request_message", "entity": {
        "title": "kaeufer_42 hat dir ein neues Angebot gemacht:", "price_label": "30,00 €",
        "original_price_label": "40,00 €", "status_title": "Abgelehnt"}}}
    sent = {"message_type": "legacy_offer", "data": {"entity_type": "offer_message", "entity": {
        "price": {"amount": "35.0"}, "price_label": "35,00 €", "original_price_label": "40,00 €"}}}
    assert vi.message_text(ask) == ("kaeufer_42 hat dir ein neues Angebot gemacht: 30,00 € (was 40,00 €) · Abgelehnt",
                                    "integration")
    assert vi.message_text(sent) == ("Offer: 35,00 € (was 40,00 €)", "integration")


def test_a_notices_markup_is_left_out():
    m = {"message_type": "legacy_action", "data": {"entity_type": "action_message", "entity": {
        "title": "Angekommen", "subtitle": 'Hol es ab: <a class="msg-status__link" href="/">Abholstelle</a> &amp; mehr'}}}
    assert vinted_inbox.message_text(m) == ("Angekommen – Hol es ab: & mehr", "integration")


def test_an_event_worded_anew_is_kept_once_with_its_fuller_words():
    vinted_inbox.apply_inbox(INBOX)
    full = {"id": "a", "created_at": "2026-10-21T16:44:03Z", "sender_id": "0", "message_type": "legacy_status",
            "data": {"entity_type": "status_message", "event_type": "success",
                     "entity": {"title": "Gekauft!", "subtitle": "Versand bis Dienstag"}}}
    with session() as db:       # stored under the key of its words (before 10-30)
        db.add(Message(conversation_id="01a1-uuid", ext_id="vinted-ev-0123456789abcdef", direction="unknown",
                       text="Gekauft! – Versand bis Dienstag", kind="integration",
                       created_at=vinted_inbox._when(full["created_at"])))
        db.commit()
    short = {**full, "id": "b", "data": {**full["data"], "entity": {"title": "Gekauft!"}}}
    other = {"id": "c", "created_at": "2026-10-21T16:44:03Z", "sender_id": "0", "message_type": "legacy_action",
             "data": {"entity_type": "action_message", "event_type": "other", "entity": {"title": "Zweites"}}}
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [other, short]})
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": [short, other]})
    assert sorted(m.text for m in chats.messages_of("01a1-uuid")) == ["Gekauft! – Versand bis Dienstag", "Zweites"]


def test_a_notice_stored_with_markup_is_cleaned_on_the_next_read():
    vinted_inbox.apply_inbox(INBOX)
    with session() as db:
        db.add(Message(conversation_id="01a1-uuid", ext_id="vinted-ev-fedcba9876543210", direction="unknown",
                       text='Angekommen – Hol es ab: <a class="msg-status__link" href="/">Abholstelle</a>',
                       kind="integration"))
        db.commit()
    vinted_inbox.apply_conversation("01a1-uuid", {**CONV, "messages": []})
    assert [m.text for m in chats.messages_of("01a1-uuid")] == ["Angekommen – Hol es ab:"]
