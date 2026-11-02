"""The Telegram Mini App (/tg): Telegram's own look, the same data as the dashboard, hits with their Geizhals rating
and the ad, the chats with sending, my ads, purchases and the search agents' switches."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import Conversation, Draft, Hit, Message, MyAd, SearchAgent, session, utcnow
from app.services import chats


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _data():
    with session() as db:
        a = SearchAgent(name="Raspberry Pi 5", source_url="https://www.willhaben.at/iad/x", webapi_url="/x")
        db.add(a)
        db.commit()
        db.refresh(a)
        h = Hit(agent_id=a.id, ad_id="555", heading="Raspberry Pi 5 8GB", url="https://www.willhaben.at/iad/object?adId=555",
                state="new", price=65, geizhals={"name": "Raspberry Pi 5 8GB", "url": "https://geizhals.at/a1",
                                                 "price_from": 89.0, "by": "geizhals"})
        db.add(h)
        db.add(Conversation(id="c1", partner_name="Erika", ad_title="Echo Show 8", role="seller", last_message_at=utcnow()))
        db.add(Message(conversation_id="c1", ext_id="m1", direction="in", text="Noch da?", created_at=utcnow()))
        db.add(Draft(conversation_id="c1", text="Ja, ist noch da.", alternatives=[{"label": "Bei 45 € bleiben",
                                                                                  "instruction": "Beim Preis bleiben."}]))
        db.add(MyAd(ad_id="900", heading="Fritzbox 7530", status="active"))
        db.commit()
        return a.id, h.id


def test_home_and_lists_in_telegrams_look(client):
    agent_id, hit_id = _data()
    home = client.get("/tg").text
    assert 'data-cell="hits"' in home and 'data-cell="chats"' in home and "Raspberry Pi 5" in home
    assert "web_app_setup_main_button" in home and "<html" in home and 'id="sidebar"' not in home   # no dashboard shell
    hits = client.get("/tg/hits").text
    assert f'data-hit="{hit_id}"' in hits and "data-geizhals" in hits and "89" in hits      # the Geizhals rating
    assert 'data-chat="c1"' in client.get("/tg/chats").text
    assert 'data-myad="900"' in client.get("/tg/myads").text
    client.post(f"/tg/agents/{agent_id}/toggle")
    with session() as db:
        assert db.get(SearchAgent, agent_id).enabled is False


def test_a_hit_opens_with_its_rating_and_is_saved_with_the_main_button(client, monkeypatch):
    from app import platforms
    _a, hit_id = _data()
    monkeypatch.setattr(platforms.get("willhaben"), "detail", lambda *a, **k: _none())
    page = client.get(f"/tg/hits/{hit_id}").text
    assert 'data-main-form="tg-save"' in page and "data-geizhals" in page and "data-geizhals-link" in page
    with session() as db:
        assert db.get(Hit, hit_id).state == "seen"                           # opened = seen
    client.post(f"/tg/hits/{hit_id}/state", data={"state": "saved"})
    with session() as db:
        assert db.get(Hit, hit_id).state == "saved"


async def _none():
    return None


def test_the_chat_sends_the_draft_as_edited(client, monkeypatch):
    _data()
    sent = []
    monkeypatch.setattr(chats, "enqueue_send", lambda did, text, now=False: sent.append((did, text, now)) or True)
    page = client.get("/tg/chats/c1").text
    assert 'data-main-form="tg-send"' in page and "Ja, ist noch da." in page and "Bei 45 € bleiben" in page
    with session() as db:
        draft_id = db.exec(__import__("sqlmodel").select(Draft.id)).one()
    client.post("/tg/chats/c1/send", data={"text": "Ja, noch da!", "draft_id": str(draft_id)})
    assert sent == [(draft_id, "Ja, noch da!", True)]
    client.post("/tg/chats/c1/send", data={"text": "Bis morgen"})              # no draft: my own text
    assert sent[-1][1:] == ("Bis morgen", True) and sent[-1][0] != draft_id


def test_telegram_buttons_open_the_mini_app(monkeypatch):
    from app.config import get_settings
    from app.tg_web import menu_url, tg_link
    monkeypatch.setattr(get_settings(), "public_url", "https://sh.example.org")
    assert tg_link("https://sh.example.org/chats/c1") == "https://sh.example.org/tg/chats/c1"
    assert tg_link("https://sh.example.org/hits?view=vinted:77") == "https://sh.example.org/tg/ad/vinted:77"
    assert tg_link("https://www.willhaben.at/iad/x") == "https://www.willhaben.at/iad/x"
    assert menu_url() == "https://sh.example.org/tg"
    monkeypatch.setattr(get_settings(), "public_url", "http://10.0.0.5:8000")
    assert menu_url() is None                                                    # Telegram opens https only



def test_the_mini_app_shows_what_the_site_shows(client, monkeypatch):
    """Close to the dashboard: the cards' Claude verdict, held-back reason and quick buttons, the viewer's price check
    and sync, the chat page's notes, instruction, manual send and actions, the list's role filter."""
    from app import platforms
    _a, hit_id = _data()
    with session() as db:
        h = db.get(Hit, hit_id)
        h.screen_relevant, h.screen_deal, h.screen_reason, h.buyer_fee = True, "good", "Gutes Angebot.", 2.45
        db.add(h)
        d = db.exec(__import__("sqlmodel").select(Draft)).one()
        d.needs_info = ["Abholzeit fehlt"]
        db.add(d)
        db.commit()
    hits = client.get("/tg/hits").text
    assert "data-claude" in hits and 'data-quick="saved"' in hits and "2,45" in hits
    assert ">Held back" in client.get("/tg/hits?state=filtered").text or "Held back" in client.get("/tg/hits").text
    monkeypatch.setattr(platforms.get("willhaben"), "detail", lambda *a, **k: _none())
    page = client.get(f"/tg/hits/{hit_id}").text
    assert "data-deal" in page and "data-sync" in page and "Gutes Angebot." in page
    client.post(f"/tg/hits/{hit_id}/quick", data={"state": "ignored", "tab": "new"})
    with session() as db:
        assert db.get(Hit, hit_id).state == "ignored"
    chat = client.get("/tg/chats/c1").text
    assert "data-notes" in chat and "Abholzeit fehlt" in chat and 'name="hint"' in chat and "data-info" in chat
    assert "/discard" in chat and 'action="/drafts/' in chat                    # the dashboard's own endpoints
    assert "/mark-sent" not in chat                                         # no "Sent manually": it sent nothing
    info = client.get("/tg/chats/c1/info").text
    assert 'action="/chats/c1/remove"' in info and 'action="/chats/c1/finished"' in info
    lst = client.get("/tg/chats?role=buyer").text
    assert 'data-chat="c1"' not in lst and 'data-chat="c1"' in client.get("/tg/chats?role=seller").text
    client.post("/tg/chats/c1/finished", data={"finished": "1"})
    with session() as db:
        assert db.get(Conversation, "c1").finished_at is not None


def test_the_refreshing_parts_do_not_pass_their_settings_to_the_links_inside(client, monkeypatch):
    """A link inside the chat list (hx-select="#tg-chats") opened the chat with only that part of it, which the chat
    page does not have: no messages, no composer, nothing refreshed."""
    import re
    _, hit_id = _data()
    monkeypatch.setattr(chats, "presence", lambda *a, **k: None, raising=False)
    pages = [client.get("/tg/chats").text, client.get("/tg/chats/c1").text]
    polled = [t for page in pages for t in re.findall(r"<div[^>]*hx-trigger=\"every[^>]*>", page)]
    assert polled and all('hx-disinherit="*"' in t for t in polled)
    hit = client.get(f"/tg/hits/{hit_id}").text
    assert "data-pricebar" in hit                       # the price bar from the Geizhals new price alone


def test_a_hit_with_a_geizhals_product_but_no_price_opens(client):
    """Claude found the product on Geizhals, but no offer: no new price to show, the page still opens."""
    agent_id, _ = _data()
    with session() as db:
        h = Hit(agent_id=agent_id, ad_id="556", heading="Miniserver", url="https://www.willhaben.at/iad/object?adId=556",
                state="filtered", price=289, geizhals={"name": "Miniserver", "url": "https://geizhals.at/a2", "by": "claude",
                                                       "checked": True})
        db.add(h)
        db.commit()
        db.refresh(h)
    page = client.get(f"/tg/hits/{h.id}")
    assert page.status_code == 200 and "data-geizhals-link" in page.text and "data-pricebar" not in page.text


def test_the_price_check_runs_on_its_own_and_the_section_asks_until_it_is_done(client, monkeypatch):
    """A check takes longer than a phone keeps a request open: the button starts it, the section shows it running and
    asks again, then shows the rating."""
    import asyncio

    from app import deals_web, tg_web
    _, hit_id = _data()
    gate = asyncio.Event()

    async def analysis(*a, **k):
        await gate.wait()
        return {"ad": {"ad_id": "555", "platform": "willhaben"}, "position": {"pos": 0.3, "level": "bargain"}}, None
    monkeypatch.setattr(deals_web, "_analysis", analysis)
    monkeypatch.setattr("app.services.deals.remember", lambda res: None)
    stored = {}
    monkeypatch.setattr("app.services.deals.cached", lambda *a, **k: stored.get("res"))
    assert client.post(f"/tg/hits/{hit_id}/deal", follow_redirects=False).status_code == 303
    part = client.get(f"/tg/hits/{hit_id}/deal").text
    assert "data-checking" in part and 'hx-trigger="every 3s"' in part and 'hx-disinherit="*"' in part
    stored["res"] = {"position": {"pos": 0.3, "level": "bargain"}}
    tg_web._checks.pop(hit_id).cancel()
    part = client.get(f"/tg/hits/{hit_id}/deal").text
    assert "data-checking" not in part and "every 3s" not in part and "lvl-bargain" in part


def test_an_action_posted_to_the_dashboard_comes_back_to_the_mini_app(client):
    """The Mini App posts to the dashboard's own endpoints with X-TG-Back: the redirect goes to the /tg page (the
    dashboard's message along), not to the dashboard; the dashboard's referer reads keep the open ad."""
    _data()
    r = client.post("/myads/900/auto-renew", data={"on": "1"}, headers={"X-TG-Back": "/tg/myads/900"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/myads/900")
    with session() as db:
        assert db.get(MyAd, "900").auto_renew is True
    r = client.post("/myads/900/quantity", data={"quantity": "x"}, headers={"X-TG-Back": "/tg/myads/900"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tg/myads/900?msg=")             # the dashboard's message comes along
    r = client.post("/chats/c1/finished", data={"finished": "1"}, headers={"X-TG-Back": "/tg/chats/c1/info"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tg/chats/c1")
    r = client.post("/chats/nope/send-direct", data={"hint": "x"}, headers={"X-TG-Back": "/tg/chats"},
                    follow_redirects=False)                                    # an error: back with its text
    assert r.status_code == 303 and r.headers["location"].startswith("/tg/chats?msg=")
    plain = client.post("/myads/900/auto-renew", data={"on": "0"}, follow_redirects=False)
    assert plain.headers["location"].startswith("/myads")                      # the dashboard itself: unchanged


def test_my_ads_their_edit_page_and_the_sale_page(client, monkeypatch):
    """The ad's page (sale, settings, automatic answers), the edit page with Claude's proposal coming back into the
    Mini App's twin, the sale page."""
    from app.services import my_ads
    _data()
    assert 'href="/tg/myads/900"' in client.get("/tg/myads").text
    page = client.get("/tg/myads/900").text
    assert 'action="/sold/900/sell"' in page and 'action="/myads/900/auto-reply"' in page and "data-description" in page

    async def load(ad_id):
        return {"heading": "Fritzbox 7530", "description": "Wie neu.", "price": 60.0, "images": []}

    async def revise(ad_id, heading, description, price, instruction):
        return {"heading": "FRITZ!Box 7530", "description": "Wie neu, mit Netzteil.", "note": "Kürzer.", "questions": []}
    monkeypatch.setattr(my_ads, "load_for_edit", load)
    monkeypatch.setattr(my_ads, "ai_revise", revise)
    edit = client.get("/tg/myads/900/edit").text
    assert 'id="edit-form"' in edit and 'formaction="/myads/900/edit/ai"' in edit and "tg-hide" in edit
    r = client.post("/myads/900/edit/ai", data={"heading": "Fritzbox 7530", "description": "Wie neu.", "price": "60",
                                                "instruction": "kürzer"}, headers={"X-TG-Back": "/tg/myads/900/edit"})
    assert r.status_code == 200 and "data-proposal" in r.text and "FRITZ!Box 7530" in r.text and "tg-hide" in r.text
    assert r.headers["hx-replace-url"] == "/tg/myads/900/edit"
    dashboard = client.post("/myads/900/edit/ai", data={"heading": "x", "description": "y"}).text
    assert "tg-hide" not in dashboard and 'id="edit-form"' in dashboard          # the dashboard keeps its own page
    sale = client.get("/tg/sold/900").text
    assert "data-buyer" in sale and 'action="/sold/900/sell"' in sale


def test_inbox_jobs_and_the_dashboard_pages(client):
    """Notifications open their /tg page, background jobs can be taken back, the admin pages are the dashboard's."""
    from app.db import EventLog
    _data()
    with session() as db:
        db.add(EventLog(kind="message", message="Neue Nachricht", conversation_id="c1"))
        db.commit()
    home = client.get("/tg").text
    assert 'data-cell="inbox"' in home and 'data-cell="search"' in home and 'data-cell="new-ad"' in home
    inbox = client.get("/tg/inbox").text
    assert 'data-inbox="message"' in inbox and 'action="/inbox/read-all"' in inbox
    assert "Nothing running." in client.get("/tg/jobs").text
    more = client.get("/tg/more").text
    assert 'data-admin="/settings"' in more and 'hx-boost="false"' in more
    r = client.post("/inbox/read-all", headers={"X-TG-Back": "/tg/inbox"}, follow_redirects=False)
    assert r.headers["location"] == "/tg/inbox"


def test_where_the_bridge_goes_after_an_action():
    """The dashboard's redirect becomes its /tg page with the message; an agent's page stays open after its own
    actions (not after deleting it, not after creating one); the hits view keeps its tab and filters."""
    from app.tg_bridge import target
    assert target("/agents?error=Bad%20link", "/tg/agents/new", path="/agents") == "/tg/agents?msg=Bad%20link"
    assert target("/agents", "/tg/agents/7", path="/agents/7/edit") == "/tg/agents/7"
    assert target("/agents", "/tg/agents/7", path="/agents/7/delete") == "/tg/agents"
    assert target("/hits?state=filtered&agent_id=3&msg=ok", "/tg/hits", path="/hits/clear-filtered") \
        == "/tg/hits?state=filtered&agent_id=3&msg=ok"
    assert target("/chats", "/tg/chats/c1/info", path="/chats/c1/remove") == "/tg/chats"
    assert target("/chats/c1?msg=ok", "/tg/chats/c1/info", path="/purchases/3/paid") == "/tg/chats/c1/info?msg=ok"
    assert target("", "/tg/new-ad/5", path="/new-ad/batch/1/drafts") == "/tg/new-ad/5"


def test_saving_the_web_address_sets_the_menu_button(client, monkeypatch):
    """An https web address saved in the settings makes the bot's menu button open the Mini App at once (its
    notification buttons follow the address by themselves)."""
    import asyncio

    from app.services import telegram
    called = []

    async def menu():
        called.append(True)
        return True
    monkeypatch.setattr(telegram, "active", lambda: True)
    monkeypatch.setattr(telegram, "menu_button", menu)
    client.post("/settings/web", data={"public_url": "https://sh.example.com"}, follow_redirects=False)
    asyncio.run(asyncio.sleep(0))
    assert called
    monkeypatch.setattr(telegram, "chat_id", lambda: 42)
    button = telegram.open_button("Open", "https://sh.example.com/hits?view=555")
    assert button == {"text": "Open", "web_app": {"url": "https://sh.example.com/tg/ad/555"}}


def test_chat_list_bulk_and_my_ads_clean_up(client, monkeypatch):
    """The chat list syncs and marks or hides several chats at once (its filters stay); My ads cleans up old ads."""
    from app.services import my_ads
    from app.tg_bridge import target
    _data()
    lst = client.get("/tg/chats?role=seller").text
    assert 'action="/chats/sync"' in lst and 'formaction="/chats/bulk/hide"' in lst and 'form="tg-chat-bulk"' in lst
    r = client.post("/chats/bulk/hide", data={"ids": ["c1"]}, headers={"X-TG-Back": "/tg/chats?role=seller"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tg/chats?role=seller")
    with session() as db:
        assert db.get(Conversation, "c1").hidden_at is not None
    assert target("/chats", "/tg/chats?show=unread") == "/tg/chats?show=unread"

    async def cands():
        return [{"ad_id": "900", "heading": "Fritzbox 7530", "url": "https://x/900", "status": "expired",
                 "reasons": ["no photos"], "deletable": True}]
    monkeypatch.setattr(my_ads, "cleanup_candidates", cands)
    page = client.get("/tg/myads/cleanup").text
    assert 'data-cleanup="900"' in page and 'action="/myads/cleanup"' in page and "hx-confirm" in page
    assert 'href="/tg/myads/cleanup"' in client.get("/tg/myads").text


def test_chats_are_removed_from_the_list_several_at_once(client, monkeypatch):
    """Selected chats are deleted at once, on willhaben and here."""
    from app.services import chats
    deleted = []

    class Account:
        async def delete_conversations(self, ids):
            deleted.append(list(ids))
    monkeypatch.setattr(chats, "_account", lambda: Account())
    _data()
    assert 'formaction="/chats/bulk/remove"' in client.get("/tg/chats").text
    r = client.post("/chats/bulk/remove", data={"ids": ["c1"]}, headers={"X-TG-Back": "/tg/chats"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/tg/chats?msg=")
    with session() as db:
        assert db.get(Conversation, "c1").removed and not db.exec(select(Message).where(Message.conversation_id == "c1")).all()
    assert deleted == [["c1"]]


def test_the_send_button_comes_with_the_draft_that_was_being_written(client):
    """10-27: while Claude wrote, only the composer reloaded; Telegram's Send button was declared on <main>, so once
    the draft came there was none – only "Sent manually", which sends nothing. The composer declares it now."""
    import re

    from app.db import Conversation
    _data()
    with session() as db:
        conv = db.get(Conversation, "c1")
        conv.drafting = True
        db.add(conv)
        db.commit()
    page = client.get("/tg/chats/c1").text
    assert "data-main=" not in page                                       # writing: no Send yet
    with session() as db:
        conv = db.get(Conversation, "c1")
        conv.drafting = False
        db.add(conv)
        db.commit()
    page = client.get("/tg/chats/c1").text
    composer = re.search(r'<div id="tg-composer"[^>]*>', page).group(0)    # what the reload swaps in
    assert 'data-main="Send"' in composer and 'data-main-form="tg-send"' in composer
    assert "mark-sent" not in page                                        # "Sent manually" is gone


def test_mini_app_actions_do_what_the_web_does(client, monkeypatch):
    """A draft discarded in the Mini App is dismissed for good (no new auto-draft for the same message), a hit kept
    there reaches the taste model; the composer's Send waits for the take-back window (no "now")."""
    from app.services import taste
    _a, hit_id = _data()
    liked = []
    monkeypatch.setattr(taste, "record", lambda h, like: liked.append((h.id, like)))
    page = client.get("/tg/chats/c1").text
    assert 'name="now"' not in page
    with session() as db:
        draft_id = db.exec(select(Draft.id)).one()
    client.post("/tg/chats/c1/discard", data={"draft_id": str(draft_id)})
    with session() as db:
        d = db.get(Draft, draft_id)
        assert d.status == "discarded" and d.dismissed is True
    client.post(f"/tg/hits/{hit_id}/state", data={"state": "saved"})
    assert liked == [(hit_id, True)]


def test_mini_app_send_asks_before_a_risky_draft(client):
    _data()
    with session() as db:
        d = db.exec(select(Draft)).one()
        d.text = "Abholung am [Tag] möglich."
        db.add(d)
        db.commit()
    assert "data-confirm=" in client.get("/tg/chats/c1").text
