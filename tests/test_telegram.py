import json

import httpx
import pytest

from app.db import Conversation, Draft, Hit, MyAd, get_setting, session, set_setting
from app.services import feedback, ha_actions, telegram
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.conftest import wait_jobs  # noqa: E402

CHAT = 4242


@pytest.fixture
def tg(monkeypatch):
    """Mocked Bot API; returns the list of (method, body) calls."""
    calls, ids = [], iter(range(100, 10_000))

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "wh_test_bot"}})
        if method in ("sendMessage", "sendPhoto"):
            body["_id"] = next(ids)          # the id Telegram gave this message (see _sent_ids)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": body["_id"], "chat": {"id": body["chat_id"]}}})
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    telegram._pending.clear()
    yield calls
    telegram.forget()


def _buttons(body):
    return {b["text"]: b.get("callback_data") or b.get("url") for row in body["reply_markup"]["inline_keyboard"] for b in row}


def _press(data, chat=CHAT, msg_id=7, markup=None):
    msg = {"message_id": msg_id, "chat": {"id": chat}}
    if markup:
        msg["reply_markup"] = markup
    return {"callback_query": {"id": "cq", "data": data, "message": msg}}


def _sent_ids(tg):
    """message ids the mock handed out, in order, one per sendMessage/sendPhoto."""
    return [b["_id"] for m, b in tg if m in ("sendMessage", "sendPhoto")]


def _text(text, chat=CHAT, reply_to=None):
    msg = {"message_id": 99, "chat": {"id": chat, "first_name": "Max"}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"message": msg}


async def _linked(tg):
    """Pairing: whoever writes to the bot becomes a candidate; the Settings button picks one."""
    assert await telegram.save_token("123:abc") == "wh_test_bot"
    assert telegram.bot_url() == "https://t.me/wh_test_bot" and telegram.status()["candidates"] == []
    await telegram.handle_update(_text("/start", chat=666))
    await telegram.handle_update(_text("hello"))
    await telegram.handle_update(_text("hello again"))                  # same chat: one entry, one reply
    assert [c["chat_id"] for c in telegram.candidates()] == [CHAT, 666]
    assert [b["chat_id"] for m, b in tg if m == "sendMessage"] == [666, CHAT]
    assert "Settings" in tg[-1][1]["text"] and telegram.chat_id() is None
    codes = {c["chat_id"]: c["code"] for c in telegram.candidates()}     # each chat gets its own code, shown in Settings
    assert [b["text"][-4:] for m, b in tg if m == "sendMessage"] == [codes[666], codes[CHAT]]
    assert await telegram.link(4711) is None                             # not a candidate
    assert await telegram.link(CHAT) == "Max"
    assert telegram.chat_id() == CHAT and get_setting("telegram_chat_name") == "Max" and not telegram.candidates()
    assert tg[-1][1]["chat_id"] == CHAT and tg[-1][1]["text"].startswith("✅")
    await telegram.handle_update(_text("/start", chat=666))             # linked: strangers get silence
    assert tg[-1][1]["chat_id"] == CHAT
    tg.clear()


async def test_candidates_expire_and_relink(tg, monkeypatch):
    await _linked(tg)
    telegram.unlink()
    assert telegram.chat_id() is None and not telegram.candidates()
    await telegram.handle_update(_text("hi", chat=555))
    assert [c["chat_id"] for c in telegram.candidates()] == [555]
    now = telegram.time.monotonic()
    monkeypatch.setattr(telegram.time, "monotonic", lambda: now + telegram.CANDIDATE_TTL + 1)
    assert telegram.candidates() == [] and await telegram.link(555) is None


async def test_link_and_kinds(tg):
    assert not await telegram.send("hit", "x", "y")                     # nothing linked yet
    await _linked(tg)
    assert await telegram.send("hit", "Rad: 120 €", "KTM <20\"> (Wien)", url="https://www.willhaben.at/x",
                               image="https://img/x.jpg", data={"ad_id": "77"})
    method, body = tg[-1]
    assert method == "sendPhoto" and "<b>Rad: 120 €</b>\nKTM &lt;20\"&gt; (Wien)" == body["caption"]
    assert set(_buttons(body)) == {"⭐ Keep", "🚫 Ignore", "🔗 Open"}
    telegram.set_kinds(["message"])
    assert not await telegram.send("hit", "x", "y")
    assert await telegram.send("test", "x", "y")                        # the test push always goes through
    await telegram.handle_update(_press("t:unknown", chat=666))
    assert tg[-1] == ("answerCallbackQuery", {"callback_query_id": "cq", "text": "Not allowed."})


async def test_draft_buttons_use_the_ha_action_path(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c1", ad_id="5", ad_title="Rad", partner_name="Max"))
        db.add(Draft(id=11, conversation_id="c1", text="Ja, noch da.", alternatives=[
            {"label": "💶 Preis halten", "instruction": "Bleib beim Preis."},
            {"label": "🤝 Annehmen", "instruction": "Nimm das Angebot an."},
            {"label": "❌ Absagen", "instruction": "Sag freundlich ab."}]))
        db.add(Draft(id=12, conversation_id="c1", text="[Uhrzeit] passt"))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append((draft_id, text))
        with session() as db:
            d = db.get(Draft, draft_id)
            d.status, d.text = "sent", text
            db.add(d)
            db.commit()
        return d

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)
    await telegram.send("auto_reply_pending", "Antwort freigeben? Max", "…", tag=ha_actions.tag_for(11),
                        data={"conversation_id": "c1", "draft_id": 11}, actions=ha_actions.draft_actions(11, True))
    b = _buttons(tg[-1][1])
    assert list(b) == ["✅ Send", "↩️ Direct answer", "✍️ Revise with Claude", "🗑 Discard",
                       "💶 Preis halten", "🤝 Annehmen", "❌ Absagen"]               # Claude's alternatives, 2 per row
    assert [len(r) for r in tg[-1][1]["reply_markup"]["inline_keyboard"]] == [2, 2, 2, 1]
    offer_id = _sent_ids(tg)[-1]

    # "Direct answer": ForceReply prompt quoting the offer, the reply text is sent through ha_actions.handle
    await telegram.handle_update(_press(b["↩️ Direct answer"], msg_id=offer_id))
    prompt = tg[-1]
    assert prompt[0] == "sendMessage" and prompt[1]["reply_markup"]["force_reply"] is True
    assert prompt[1]["reply_parameters"]["message_id"] == offer_id
    await telegram.handle_update(_text("Ja, Abholung heute ab 18 Uhr.", reply_to=next(iter(telegram._pending))))
    assert sent == [(11, "Ja, Abholung heute ab 18 Uhr.")]

    # second tap on an old button: nothing is sent again
    await telegram.handle_update(_press(b["✅ Send"]))
    assert sent == [(11, "Ja, Abholung heute ab 18 Uhr.")] and "Already done." in tg[-2][1]["text"]

    await telegram.send("auto_reply_pending", "x", "y", data={"conversation_id": "c1", "draft_id": 12},
                        actions=ha_actions.draft_actions(12, False))
    b12 = _buttons(tg[-1][1])
    assert "✅ Send" not in b12                                         # placeholders: no plain send
    assert list(b12) == ["↩️ Direct answer", "✍️ Revise with Claude", "🗑 Discard"]   # no alternatives: none shown
    await telegram.handle_update(_press(b12["🗑 Discard"]))
    with session() as db:
        assert db.get(Draft, 12).status == "discarded"


async def test_message_reply_feedback_auto_and_hits(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c2", ad_id="9", ad_title="Stuhl", partner_name="Eva"))
        db.add(Draft(id=21, conversation_id="c2", text="Ja, ist noch da.", status="sent", auto=True))
        db.add(MyAd(ad_id="9", heading="Stuhl", auto_reply=True, auto_reply_instructions="Preis fix 20 €"))
        db.add(Hit(agent_id=1, ad_id="77", heading="Rad", url="https://x"))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append((draft_id, text))

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)

    # new chat message -> "Reply": typed text (without using Telegram's reply) becomes a draft and is sent
    await telegram.send("message", "Nachricht von Eva", "Stuhl: noch da?", data={"conversation_id": "c2", "ad_id": "9"})
    b = _buttons(tg[-1][1])
    assert set(b) == {"✍️ Draft with Claude", "↩️ Reply"}
    await telegram.handle_update(_press(b["↩️ Reply"]))
    await telegram.handle_update(_text("Ja, heute abholbar."))
    with session() as db:
        d = db.exec(__import__("sqlmodel").select(Draft).where(Draft.model == "telegram")).one()
    assert sent == [(d.id, "Ja, heute abholbar.")] and d.conversation_id == "c2"

    # automatic answer sent -> 👎 + note feeds the next automatic answers
    await telegram.send("auto_reply", "Automatisch geantwortet: Eva", "…",
                        data={"conversation_id": "c2", "ad_id": "9", "draft_id": 21})
    b = _buttons(tg[-1][1])
    await telegram.handle_update(_press(b["👎 Bad answer"]))
    await telegram.handle_update(_text("Nie Versand zusagen", reply_to=next(iter(telegram._pending))))
    with session() as db:
        assert (db.get(Draft, 21).feedback, db.get(Draft, 21).feedback_note) == ("down", "Nie Versand zusagen")
    block = feedback.prompt_block()
    assert "Rückmeldungen von mir" in block and "Nie Versand zusagen" in block and "Ja, ist noch da." in block

    # auto answers off for the ad keeps the per-ad instructions
    await telegram.handle_update(_press(b["⏸ Auto answers off for this ad"]))
    with session() as db:
        ad = db.get(MyAd, "9")
    assert ad.auto_reply is False and ad.auto_reply_instructions == "Preis fix 20 €"

    await telegram.send("hit", "Rad", "x", data={"ad_id": "77"})
    await telegram.handle_update(_press(_buttons(tg[-1][1])["⭐ Keep"]))
    with session() as db:
        assert db.exec(__import__("sqlmodel").select(Hit)).one().state == "saved"


async def test_same_tag_edits_and_clear(tg):
    await _linked(tg)
    assert await telegram.send("auto_reply_pending", "Antwort freigeben?", "a", tag="wh-draft-5")
    assert await telegram.send("auto_reply", "Gesendet", "b", tag="wh-draft-5")
    assert tg[-1][0] == "editMessageText" and tg[-1][1]["text"] == "<b>Gesendet</b>\nb"
    await telegram.clear("wh-draft-5")
    assert tg[-1][0] == "editMessageReplyMarkup"
    set_setting("telegram_enabled", "0")
    assert not await telegram.send("test", "x", "y")


def test_settings_panel_and_routes(client, monkeypatch):  # noqa: F811
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[1])
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "wh_test_bot"}})
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "chat": {"id": CHAT}}})
        return httpx.Response(200, json={"ok": True, "result": True})

    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(handler))
    page = client.get("/settings/telegram").text
    assert "/newbot" in page
    r = client.post("/settings/telegram/token", data={"token": "123:abc"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/telegram?") and telegram.token() == "123:abc"
    assert "setMyCommands" in calls and "deleteWebhook" in calls
    page = client.get("/settings/telegram").text
    assert "@wh_test_bot" in page and 'id="tg-link"' in page and "https://t.me/wh_test_bot" in page and "<svg" in page
    assert client.get("/settings/telegram/status").json() == {"linked": False, "chat_name": "", "candidates": []}
    telegram._candidates[CHAT] = ("Max <b>", telegram.time.monotonic(), "0042")
    st = client.get("/settings/telegram/status").json()
    assert st["candidates"][0]["chat_id"] == CHAT and st["candidates"][0]["name"] == "Max <b>"
    page = client.get("/settings/telegram").text
    assert "New chat: Max &lt;b&gt; — is that you?" in page and "code 0042" in page and f'data-tg-fp="{CHAT}"' in page
    r = client.post("/settings/telegram/link", data={"chat_id": "999"}, follow_redirects=False)
    assert "expired" in r.headers["location"] and telegram.chat_id() is None
    r = client.post("/settings/telegram/link", data={"chat_id": str(CHAT)}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/telegram?") and telegram.chat_id() == CHAT and calls[-1] == "sendMessage"
    assert client.get("/settings/telegram/status").json()["linked"]
    client.post("/settings/telegram/kinds", data={"enabled": "on", "kind": ["message", "hit"]})
    assert telegram.enabled_kinds() == {"message", "hit"} and telegram.active()
    client.post("/settings/telegram/test")
    assert calls[-1] == "sendMessage"
    client.post("/settings/telegram/kinds", data={"kind": []})
    assert not telegram.active() and telegram.enabled_kinds() == set()
    client.post("/settings/telegram/forget")
    assert telegram.token() == "" and not telegram.status()["configured"]


async def test_typing_while_claude_writes(tg, monkeypatch):
    async with telegram.typing():                       # not linked: no API calls
        pass
    assert not tg
    await _linked(tg)
    monkeypatch.setattr(telegram, "TYPING_EVERY_S", 0.01)
    async with telegram.typing():
        await __import__("asyncio").sleep(0.05)
    n = len([c for c in tg if c[0] == "sendChatAction"])
    assert n >= 2 and tg[0][1] == {"chat_id": CHAT, "action": "typing"}
    await __import__("asyncio").sleep(0.05)
    assert len([c for c in tg if c[0] == "sendChatAction"]) == n     # stopped after the block


def test_draft_reply_shows_typing(client, monkeypatch):  # noqa: F811
    from tests.test_dashboard import _conversation

    seen = []

    class Spy:
        async def __aenter__(self):
            seen.append("start")

        async def __aexit__(self, *exc):
            seen.append("stop")

    monkeypatch.setattr(telegram, "typing", lambda: Spy())
    _conversation()
    client.post("/chats/c1/draft", data={"hint": ""})
    wait_jobs()
    assert seen == ["start", "stop"] and client.fake.calls


async def test_bot_token_never_logged(tg, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    await telegram.save_token("8774:SECRET-token")
    await telegram.api("getMe")
    assert "SECRET-token" not in caplog.text


async def test_alternative_button_revises_in_place(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c1", ad_id="5", ad_title="Rad", partner_name="Max"))
        db.add(Draft(id=11, conversation_id="c1", text="Ich könnte auf 18 € runter.", alternatives=[
            {"label": "💶 Preis halten", "instruction": "Bleib beim Preis, geh nicht runter."},
            {"label": "🤝 18 € annehmen", "instruction": "Sag 18 € zu."}]))
        db.commit()
    revised = []

    async def fake_revise(draft_id, instruction, *, chosen=False):
        revised.append((draft_id, instruction, chosen))
        from app.services import chats
        with session() as db:
            conv = db.get(Conversation, "c1")
            new = Draft(conversation_id="c1", text="Der Preis ist fix, 20 €.")
            db.add(new)
            db.commit()
            db.refresh(new)
        await chats.offer_draft(conv, new, "revised", replaces=ha_actions.tag_for(draft_id))
        return new

    monkeypatch.setattr("app.services.chats.revise_and_offer", fake_revise)
    await telegram.send("auto_reply_pending", "Antwort freigeben? Max", "„Ich könnte auf 18 € runter.“",
                        tag=ha_actions.tag_for(11), data={"conversation_id": "c1", "draft_id": 11},
                        actions=ha_actions.draft_actions(11, True))
    offer_id = _sent_ids(tg)[-1]
    b = _buttons(tg[-1][1])
    n = len(tg)
    await telegram.handle_update(_press(b["💶 Preis halten"], msg_id=offer_id))
    assert revised == [(11, "Bleib beim Preis, geh nicht runter.", True)]      # chosen: Claude's own alternative
    edits = [c for c in tg[n:] if c[0] == "editMessageText"]
    # 1. progress line into the same message (buttons gone), 2. the new draft replaces it, still the same message
    assert "Claude is revising: 💶 Preis halten" in edits[0][1]["text"]
    assert edits[0][1]["reply_markup"] == {"inline_keyboard": []}
    assert edits[-1][1]["message_id"] == offer_id and "Der Preis ist fix" in edits[-1][1]["text"]
    assert "✅ Send" in _buttons(edits[-1][1])
    assert not [c for c in tg[n:] if c[0] == "sendMessage"]            # nothing new in the chat
    with session() as db:
        new_id = db.exec(__import__("sqlmodel").select(Draft).where(Draft.text.startswith("Der Preis"))).one().id
    assert telegram._message_for(ha_actions.tag_for(new_id)).message_id == offer_id
    assert telegram._message_for(ha_actions.tag_for(11)) is None


async def test_swipe_reply_answers_the_chat(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c2", ad_id="9", ad_title="Stuhl", partner_name="Eva"))
        db.add(Conversation(id="c3", ad_id="9", ad_title="Stuhl", partner_name="Tom"))
        db.add(Draft(id=31, conversation_id="c3", text="Ja, noch da."))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append((draft_id, text))
        with session() as db:
            d = db.get(Draft, draft_id)
            d.status, d.text = "sent", text
            db.add(d)
            db.commit()
        return d

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)
    await telegram.send("message", "Nachricht von Eva", "Stuhl: noch da?", tag="wh-msg-c2",
                        data={"conversation_id": "c2", "ad_id": "9"})
    eva_id = _sent_ids(tg)[-1]
    await telegram.send("auto_reply_pending", "Antwort freigeben? Tom", "…", tag=ha_actions.tag_for(31),
                        data={"conversation_id": "c3", "draft_id": 31}, actions=ha_actions.draft_actions(31, True))
    tom_id = _sent_ids(tg)[-1]

    # reply (swipe) to Eva's notification, no button: a new message in that chat, result edited into the notification
    await telegram.handle_update(_text("Ja, heute abholbar.", reply_to=eva_id))
    with session() as db:
        d = db.exec(__import__("sqlmodel").select(Draft).where(Draft.model == "telegram")).one()
    assert sent == [(d.id, "Ja, heute abholbar.")] and d.conversation_id == "c2"
    edit = tg[-1]
    assert edit[0] == "editMessageText" and edit[1]["message_id"] == eva_id
    assert "Nachricht von Eva" in edit[1]["text"] and "<b>Sent:</b> Ja, heute abholbar." in edit[1]["text"]

    # reply to a held-back draft: the typed text is sent as that draft (ha_actions path)
    await telegram.handle_update(_text("Nein, leider weg.", reply_to=tom_id))
    assert sent[-1] == (31, "Nein, leider weg.")

    # a reply to some unrelated message is not an answer: help text
    await telegram.handle_update(_text("Hallo?", reply_to=5))
    assert "/drafts" in tg[-1][1]["text"]


async def test_several_open_prompts_need_an_explicit_reply(tg, monkeypatch):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c2", ad_id="9", ad_title="Stuhl", partner_name="Eva"))
        db.add(Conversation(id="c3", ad_id="9", ad_title="Tisch", partner_name="Tom"))
        db.commit()
    sent = []

    async def fake_send(draft_id, text):
        sent.append(text)

    monkeypatch.setattr("app.services.chats.send_draft", fake_send)
    for cid in ("c2", "c3"):
        await telegram.send("message", f"Nachricht {cid}", "…", tag=f"wh-msg-{cid}", data={"conversation_id": cid})
        await telegram.handle_update(_press(_buttons(tg[-1][1])["↩️ Reply"], msg_id=_sent_ids(tg)[-1]))
    assert len(telegram._pending) == 2
    await telegram.handle_update(_text("Ja"))                            # which one? we do not guess
    assert sent == [] and "Several questions are open" in tg[-1][1]["text"] and len(telegram._pending) == 2
    await telegram.handle_update(_text("Ja", reply_to=max(telegram._pending)))
    assert sent == ["Ja"] and len(telegram._pending) == 1
    await telegram.handle_update(_text("Nein"))                          # one left: plain text is enough
    assert sent == ["Ja", "Nein"] and not telegram._pending


async def test_auto_off_flips_the_button_in_place(tg):
    await _linked(tg)
    with session() as db:
        db.add(Conversation(id="c2", ad_id="9", ad_title="Stuhl", partner_name="Eva"))
        db.add(MyAd(ad_id="9", heading="Stuhl", auto_reply=True))
        db.commit()
    await telegram.send("auto_reply", "Automatisch geantwortet: Eva", "…",
                        data={"conversation_id": "c2", "ad_id": "9", "draft_id": "conv:c2"})
    markup = tg[-1][1]["reply_markup"]
    b = _buttons(tg[-1][1])
    n = len(tg)
    await telegram.handle_update(_press(b["⏸ Auto answers off for this ad"], markup=markup))
    with session() as db:
        assert db.get(MyAd, "9").auto_reply is False
    swaps = [c for c in tg[n:] if c[0] == "editMessageReplyMarkup"]
    assert len(swaps) == 1 and not [c for c in tg[n:] if c[0] == "sendMessage"]
    new = _buttons(swaps[0][1])
    assert set(new) == {"👍", "👎 Bad answer", "▶️ Switch on again"}      # feedback stays, the switch flipped
    await telegram.handle_update(_press(new["▶️ Switch on again"], markup=swaps[0][1]["reply_markup"]))
    with session() as db:
        assert db.get(MyAd, "9").auto_reply is True
    assert "⏸ Auto answers off for this ad" in _buttons(tg[-1][1])


async def test_status_line_and_retag(tg):
    await _linked(tg)
    await telegram.send("message", "Nachricht von Eva", "Stuhl: noch da?", tag="wh-msg-c2",
                        data={"conversation_id": "c2"})
    mid = _sent_ids(tg)[-1]
    await telegram.progress("wh-msg-c2", "Claude is writing a draft …")
    m, body = tg[-1]
    assert m == "editMessageText" and body["message_id"] == mid and body["reply_markup"] == {"inline_keyboard": []}
    assert body["text"] == "<b>Nachricht von Eva</b>\nStuhl: noch da?\n\n<i>Claude is writing a draft …</i>"
    assert telegram.retag("wh-msg-c2", "wh-draft-7") and telegram._message_for("wh-msg-c2") is None
    assert telegram._message_for("wh-draft-7").message_id == mid
    await telegram.send("auto_reply_pending", "Antwort freigeben?", "…", tag="wh-draft-7")
    assert tg[-1][0] == "editMessageText" and tg[-1][1]["message_id"] == mid
    assert telegram._message_for("wh-draft-7").text.startswith("<b>Antwort freigeben?</b>")
    await telegram.progress("wh-nothing", "x")                             # unknown tag: nothing happens
    assert tg[-1][0] == "editMessageText"
    assert not telegram.retag("wh-nothing", "wh-x")


async def test_hit_starts_a_purchase_order(tg, monkeypatch):
    from app.services import purchase

    await _linked(tg)
    with session() as db:
        db.add(Hit(agent_id=1, ad_id="77", heading="KTM Rad", url="https://www.willhaben.at/iad/x-77", price=120.0,
                   image_url="https://img/x.jpg"))
        db.commit()
    await telegram.send("hit", "Rad: 120 €", "KTM", url="https://www.willhaben.at/iad/x-77", data={"ad_id": "77"})
    assert "🛒 Start purchase order" not in _buttons(tg[-1][1])        # purchases off: no button

    started = []

    async def no_begin(pid):
        started.append(pid)

    monkeypatch.setattr(purchase, "_begin", no_begin)
    set_setting("purchase_enabled", "1")
    set_setting(purchase.SETTING_PREFER_PAYLIVERY, "1")
    await telegram.send("hit", "Rad: 120 €", "KTM", url="https://www.willhaben.at/iad/x-77", data={"ad_id": "77"})
    markup = tg[-1][1]["reply_markup"]
    b = _buttons(tg[-1][1])
    await telegram.handle_update(_press(b["🛒 Start purchase order"], markup=markup))
    from tests.conftest import finish_jobs
    await finish_jobs()                                                 # the background start (a job; replaced above)
    row = purchase.recent()[0]
    assert row.ad_id == "77" and row.mode == "paylivery" and row.ad_title == "KTM Rad" and row.asking_price == 120.0
    assert row.max_price is None and started == [row.id]
    toast = next(c for c in reversed(tg) if c[0] == "answerCallbackQuery")
    assert toast[1]["text"] == f"🛒 Purchase order #{row.id} started"
    swapped = _buttons(tg[-1][1])
    assert tg[-1][0] == "editMessageReplyMarkup" and f"🛒 Purchase order #{row.id}" in swapped
    assert "⭐ Keep" in swapped and "🛒 Start purchase order" not in swapped
    # a second tap: the refusal (open order exists) is the toast, nothing else happens
    await telegram.handle_update(_press(b["🛒 Start purchase order"], markup=markup))
    assert tg[-1][0] == "answerCallbackQuery" and "already an open purchase" in tg[-1][1]["text"]
    set_setting("purchase_enabled", "0")


async def test_hit_opens_the_buy_dialog_with_the_dashboards_choices(tg, monkeypatch):
    """With an https address the hit's 🛒 button opens the buy dialog (way to buy, wish price, maximum, time,
    questions) in the Mini App instead of starting with the defaults; in a group chat it is the dashboard's page."""
    from app.config import get_settings

    await _linked(tg)
    set_setting("purchase_enabled", "1")
    monkeypatch.setattr(get_settings(), "public_url", "https://sh.example.org/")
    await telegram.send("hit", "Rad: 120 €", "KTM", url="https://www.willhaben.at/iad/x-77", data={"ad_id": "77"})
    rows = [b for row in tg[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    buy = next(b for b in rows if b["text"] == "🛒 Start purchase order")
    assert buy == {"text": "🛒 Start purchase order", "web_app": {"url": "https://sh.example.org/tg/purchases/new/77"}}
    monkeypatch.setattr(telegram, "chat_id", lambda: -100)                # a group: no Mini App, the dashboard page
    await telegram.send("hit", "Rad: 120 €", "KTM", url="https://www.willhaben.at/iad/x-77", data={"ad_id": "77"})
    assert _buttons(tg[-1][1])["🛒 Start purchase order"] == "https://sh.example.org/purchases/new/77"
    await telegram.send("hit", "Rad", "x", data={"ad_id": "vinted:5"})   # other sites: no purchase orders
    assert "🛒 Start purchase order" not in _buttons(tg[-1][1])
    set_setting("purchase_enabled", "0")


def _button_row(verb, ref, token):
    from app.db import TelegramAction
    with session() as db:
        db.add(TelegramAction(token=token, verb=verb, ref=ref))
        db.commit()


async def test_the_money_buttons_reach_their_actions(tg, monkeypatch):
    """The buttons that buy, cancel or pay: each one goes to its signed action (ha_actions) for its draft, and the
    auto-buy countdown's Cancel takes the queued purchase back."""
    await _linked(tg)
    handled = []

    async def handle(event):
        handled.append(event)
        return "ok"
    monkeypatch.setattr(ha_actions, "handle", handle)
    for verb in ("D_BUY", "D_CANCEL", "D_DECL", "D_PAID"):
        _button_row(verb, "11", f"tok-{verb}")
        await telegram.handle_update(_press(f"t:tok-{verb}"))
    assert [e["action"] for e in handled] == [ha_actions.action_id(v, 11) for v in ("BUY", "CANCEL", "DECL", "PAID")]
    _button_row("D_ALT", "11|Bei 45 € bleiben|Beim Preis bleiben.", "tok-alt")
    await telegram.handle_update(_press("t:tok-alt"))
    assert handled[-1] == {"action": ha_actions.action_id("REVISE", 11), "reply_text": "Beim Preis bleiben.",
                           "chosen": True}

    from app.services import jobs
    stopped = []
    monkeypatch.setattr(jobs, "cancel_key", lambda key: stopped.append(key) or True)
    _button_row("AB_STOP", "5", "tok-stop")
    await telegram.handle_update(_press("t:tok-stop"))
    assert stopped == ["purchase:5"]
    assert any(m == "answerCallbackQuery" and "cancelled" in (b.get("text") or "").lower() for m, b in tg)
    monkeypatch.setattr(jobs, "cancel_key", lambda key: False)                 # too late: the buy runs
    _button_row("AB_STOP", "6", "tok-late")
    await telegram.handle_update(_press("t:tok-late"))
    assert "too late" in (tg[-1][1].get("text") or "").lower() or any(
        "too late" in (b.get("text") or "").lower() for m, b in tg if m == "answerCallbackQuery")
