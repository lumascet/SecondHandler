"""Foreign chat messages (services/translate): Claude names each message's language once and translates the foreign
ones; the bubble shows the original and the translation (10-27: a Vinted chat mixed Italian and German)."""
from datetime import timedelta

from app.db import Conversation, Message, session, utcnow
from app.llm.base import LLMResult
from app.services import translate
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)


class FakeClaude:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append(prompt)
        return LLMResult(data=self.data, model="fake")


def _chat():
    now = utcnow()
    with session() as db:
        db.add(Conversation(id="t1", ad_id="1", partner_name="x", platform="vinted"))
        db.add(Message(conversation_id="t1", ext_id="a", direction="in", text="Oggi penso proprio di sì.",
                       created_at=now - timedelta(minutes=5)))
        db.add(Message(conversation_id="t1", ext_id="b", direction="out", text="Super, danke dir!", created_at=now))
        db.add(Message(conversation_id="t1", ext_id="c", direction="unknown", kind="integration",
                       text="Sendung wurde verschickt", created_at=now))          # a notice: not asked
        db.commit()


async def test_foreign_messages_get_a_translation_once(monkeypatch):
    _chat()
    fake = FakeClaude({"items": [{"n": 1, "lang": "de", "translation": ""},
                                 {"n": 2, "lang": "it", "translation": "Heute denke ich schon."}]})
    monkeypatch.setattr(translate, "get_backend", lambda *a: fake)
    assert await translate.translate_chat("t1") == 1
    assert "1. Super, danke dir!" in fake.calls[0] and "Sendung" not in fake.calls[0]   # newest first, no notices
    with session() as db:
        rows = {m.ext_id: m for m in db.exec(translate.select(Message))}
    assert rows["a"].translation == "Heute denke ich schon." and rows["a"].lang == "it"
    assert rows["b"].translation is None and rows["b"].lang == "de"
    assert await translate.translate_chat("t1") == 0 and len(fake.calls) == 1        # asked once per message


def test_the_bubble_shows_original_and_translation(client):  # noqa: F811
    _chat()
    with session() as db:
        m = db.exec(translate.select(Message).where(Message.ext_id == "a")).one()
        m.lang, m.translation = "it", "Heute denke ich schon."
        db.add(m)
        db.commit()
    page = client.get("/chats/t1").text
    i = page.index("Oggi penso proprio di sì.")
    assert "data-translation" in page[i:i + 600] and "Heute denke ich schon." in page[i:i + 600]
    tg = client.get("/tg/chats/t1").text
    i = tg.index("Oggi penso proprio di sì.")
    assert "Heute denke ich schon." in tg[i:i + 700]


async def test_a_draft_in_their_language_gets_a_translation(monkeypatch):
    """The draft is written in the other person's language now: I read what it says, under it."""
    from app.db import Draft
    _chat()
    with session() as db:
        d = Draft(conversation_id="t1", text="Perfetto, grazie mille!", status="draft")
        db.add(d)
        db.commit()
        db.refresh(d)
    fake = FakeClaude({"items": [{"n": 1, "lang": "it", "translation": "Perfekt, vielen Dank!"}]})
    monkeypatch.setattr(translate, "get_backend", lambda *a: fake)
    assert await translate.translate_draft(d.id) == "Perfekt, vielen Dank!"
    assert await translate.translate_draft(d.id) is None and len(fake.calls) == 1      # asked once


def test_the_composer_shows_the_drafts_translation(client):  # noqa: F811
    from app.db import Draft
    _chat()
    with session() as db:
        db.add(Draft(conversation_id="t1", text="Perfetto, grazie mille!", status="draft", lang="it",
                     translation="Perfekt, vielen Dank!"))
        db.commit()
    for path in ("/chats/t1", "/tg/chats/t1"):
        page = client.get(path).text
        assert "data-draft-translation" in page and "Perfekt, vielen Dank!" in page, path


def test_only_ad_texts_in_other_languages_count_as_foreign():
    it = ("Ottime condizioni estetiche. Smontato funzionante 7 mesi fa e conservato con cura. Ora non posso "
          "testarlo quindi venduto come non testato/funzionante.")
    assert translate.foreign(it)
    assert not translate.foreign("Verkaufe meinen Router, funktioniert einwandfrei, Versand ist möglich und gut verpackt.")
    assert not translate.foreign("Selling my router, it works and is in very good condition, shipping is possible.")
    assert not translate.foreign("Samsung 980 Pro 1TB")                       # too few words to tell
    assert not translate.foreign("")


async def test_an_ad_text_is_translated_once_per_text(monkeypatch):
    calls = []

    class Fake:
        async def complete(self, *, system, prompt, schema, images=None):
            calls.append(prompt)
            return LLMResult(data={"items": [{"n": 1, "lang": "it", "translation": "Sehr guter Zustand."}]}, model="f")
    monkeypatch.setattr(translate, "get_backend", lambda *a: Fake())
    it = "Ottime condizioni estetiche, smontato funzionante e conservato con cura, venduto come non testato."
    tr = await translate.translate_ad("vinted", "123", it)
    assert tr.translation == "Sehr guter Zustand." and tr.lang == "it"
    assert (await translate.translate_ad("vinted", "123", it)).translation == "Sehr guter Zustand."
    assert len(calls) == 1                                                  # kept: asked once
    await translate.translate_ad("vinted", "123", it + " Spedizione possibile.")
    assert len(calls) == 2                                                  # the text changed: asked again
    assert await translate.translate_ad("vinted", "124", "Funktioniert einwandfrei und ist in sehr gutem Zustand.") is None
    assert len(calls) == 2                                                  # German: never asked


def test_the_viewer_loads_the_translation(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def fake(pid, ad_id, text):
        from app.db import AdTranslation
        return AdTranslation(key=f"{pid}:{ad_id}", text_hash="x", lang="it", translation="Sehr guter Zustand.")
    monkeypatch.setattr(translate, "translate_ad", fake)
    with TestClient(app) as c:
        r = c.post("/hits/ad/vinted/123/translation", data={"text": "Ottime condizioni"})
        assert r.status_code == 200 and "data-ad-translation" in r.text and "Sehr guter Zustand." in r.text
        assert c.post("/hits/ad/nope/123/translation", data={"text": "x"}).status_code == 400


def test_the_mini_app_gets_the_translation_itself_not_its_page_again(monkeypatch):
    """10-30: from the Mini App the translation's request came back as the ad page itself, swapped into its own
    description – which loaded its translation again, endlessly."""
    from fastapi.testclient import TestClient

    from app.main import app

    async def fake(pid, ad_id, text):
        from app.db import AdTranslation
        return AdTranslation(key=f"{pid}:{ad_id}", text_hash="x", lang="fr", translation="Sehr guter Zustand.")
    monkeypatch.setattr(translate, "translate_ad", fake)
    with TestClient(app) as c:
        r = c.post("/hits/ad/vinted/123/translation", data={"text": "Très bon état"},
                   headers={"X-TG-Back": "/tg/ad/vinted:123", "HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 200 and "data-ad-translation" in r.text and "Sehr guter Zustand." in r.text
    assert "<html" not in r.text.lower()
