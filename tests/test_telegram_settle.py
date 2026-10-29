"""An action taken in the app (dashboard, Mini App) shows in the Telegram message it belongs to: a last line saying
what was done, the buttons gone but "Open" (services/telegram.settle)."""
from fastapi.testclient import TestClient

from app.db import Conversation, Draft, Hit, SearchAgent, TelegramMessage, session
from app.services import telegram


async def test_the_message_says_what_was_done_and_keeps_only_open(monkeypatch):
    with session() as db:
        db.add(TelegramMessage(tag="wh-draft-7", chat_id=1, message_id=10, text="<b>Neue Nachricht</b>\\nHallo"))
        db.commit()
    calls = []

    async def api(method, **kw):
        calls.append((method, kw))
        return {}
    monkeypatch.setattr(telegram, "api", api)
    monkeypatch.setattr(telegram, "active", lambda: True)
    assert await telegram.settle("wh-draft-7", "✅ Sent in the app", "https://app.example.org/chats/c1")
    method, kw = calls[0]
    assert method == "editMessageText" and kw["text"].endswith("<b>✅ Sent in the app</b>")
    assert [b["text"] for row in kw["reply_markup"]["inline_keyboard"] for b in row] == ["🔗 Open"]
    assert not await telegram.settle("wh-draft-7", "✅ Sent in the app")          # once
    assert not await telegram.settle("wh-draft-404", "✅ Sent in the app")        # no such message
    assert len(calls) == 1


def test_the_app_routes_settle_their_telegram_messages(monkeypatch):
    settled = []
    monkeypatch.setattr(telegram, "settle_later", lambda tags, line, url=None: settled.append((tags, line)))
    with session() as db:
        a = SearchAgent(name="A", source_url="https://x", webapi_url="/w")
        db.add(a)
        db.add(Conversation(id="c1", ad_id="1", role="seller"))
        db.commit()
        db.refresh(a)
        h = Hit(agent_id=a.id, ad_id="55", heading="Router", url="https://x/55")
        d = Draft(conversation_id="c1", text="Hallo")
        db.add(h)
        db.add(d)
        db.commit()
        db.refresh(h)
        db.refresh(d)
    from app.main import app
    with TestClient(app) as c:
        c.post(f"/hits/{h.id}/state", data={"state": "saved"})
        c.post(f"/drafts/{d.id}/discard")
    assert (["wh-hit-willhaben-55"], "⭐ Kept in the app") in settled
    assert ([f"wh-draft-{d.id}", "wh-msg-c1"], "🗑 Discarded in the app") in settled
