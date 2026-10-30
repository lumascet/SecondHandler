import json
import re
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db as dbmod
from app.llm.base import LLMResult
from app.willhaben import public as public_mod
from app.willhaben.public import PublicClient
from tests.conftest import FIXTURES, load
from tests.conftest import wait_jobs


class FakeBackend:
    model = "fake-opus"

    def __init__(self):
        self.calls = []
        self.ad_extra: dict = {}      # a test overrides single fields of the ad answer (quantity, parcel, ...)

    async def complete(self, *, system, prompt, schema, images=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "images": images or []})
        if "reply" in schema["properties"]:
            data = {"reply": "Hallo, ja, das Rad ist noch da. 180 € wäre ok.", "intent": "price_negotiation",
                    "needs_info": ["Abholzeit"], "suggested_price": 180, "note": "",
                    "alternatives": [{"label": "🤝 150 € annehmen", "instruction": "Nimm die 150 € an."},
                                     {"label": "💶 Preis halten", "instruction": "Bleib bei 200 €."}]}
        else:
            data = {"title": "KTM Kinderrad 20 Zoll blau", "description": "Gut erhalten.", "category_hint": "Sport",
                    "condition": "gebraucht", "attributes": [{"name": "Marke", "value": "KTM"}],
                    "search_keywords": "ktm kinderrad 20", "price_estimate": 120, "note": "", "quantity": 1,
                    "parcel": {"length_cm": 120, "width_cm": 20, "height_cm": 70, "weight_kg": 14.2}, **self.ad_extra}
        return LLMResult(data=data, model=self.model)


def _detail_html() -> str:
    nd = {"page": "/iad/kaufen-und-verkaufen/d/[[...seopath]]", "buildId": "x",
          "props": {"pageProps": load("detail_1662146981_nextdata.json")}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'


@pytest.fixture
def client(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr("app.services.chats.get_backend", lambda *a: fake)
    monkeypatch.setattr("app.services.ad_creator.get_backend", lambda *a: fake)
    search = load("search_fahrrad_rows3.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/webapi/ad-search/search"):
            return httpx.Response(200, json=search)
        if request.url.path == "/iad/object":
            return httpx.Response(200, text=_detail_html())
        return httpx.Response(404)

    public_mod.set_public(PublicClient(transport=httpx.MockTransport(handler)))
    from app.main import app

    with TestClient(app) as c:
        c.fake = fake
        yield c
    public_mod.set_public(None)


def _conversation():
    with dbmod.session() as db:
        db.add(dbmod.Conversation(id="c1", ad_id="1662146981", ad_title="Fahrradhose", partner_name="Max",
                                  unread_count=1, last_message_text="Nehmen Sie 150?",
                                  last_message_at=datetime(2026, 10, 9, 8, tzinfo=timezone.utc)))
        db.add(dbmod.Message(conversation_id="c1", ext_id="m1", direction="in", text="Nehmen Sie 150?",
                             created_at=datetime(2026, 10, 9, 8, tzinfo=timezone.utc)))
        db.commit()


def test_pages_render(client):
    for path in ["/", "/agents", "/hits", "/chats", "/myads", "/new-ad", "/session", "/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "SecondHandler" in r.text


def test_chat_page_offers_claudes_alternatives(client):
    """The draft's alternatives are buttons under it; one click revises in the background like a redraft."""
    _conversation()
    client.post("/chats/c1/draft", data={"hint": ""})
    wait_jobs()
    page = client.get("/chats/c1").text
    assert "🤝 150 € annehmen" in page and "💶 Preis halten" in page and 'value="Nimm die 150 € an."' in page
    with dbmod.session() as db:
        old = db.exec(select(dbmod.Draft)).one()

    r = client.post(f"/drafts/{old.id}/revise", data={"instruction": "Nimm die 150 € an."}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/chats/c1"
    wait_jobs()
    prompt = client.fake.calls[-1]["prompt"]
    # one of Claude's own alternatives: the hint names the decision, the old draft only for comparison
    assert "Meine Entscheidung für diese Antwort (einer deiner eigenen Vorschläge" in prompt
    assert "Nimm die 150 € an." in prompt and "„Hallo, ja, das Rad ist noch da. 180 € wäre ok.“ – nur zum Vergleich" in prompt
    with dbmod.session() as db:
        drafts = db.exec(select(dbmod.Draft).order_by(dbmod.Draft.id)).all()
    assert [d.status for d in drafts] == ["discarded", "draft"]
    assert "revised as asked" in drafts[1].auto_reason and drafts[1].alternatives[0]["label"] == "🤝 150 € annehmen"
    assert client.post(f"/drafts/{old.id}/revise", data={"instruction": "x"}).status_code == 200   # old draft: no-op
    wait_jobs()
    with dbmod.session() as db:
        assert db.exec(select(dbmod.Draft)).all().__len__() == 2
    assert client.post("/drafts/999/revise", data={"instruction": "x"}).status_code == 404


def test_chat_composer_reads_top_down_and_groups_the_rewrites(client):
    """The composer names the draft (German intent label, price), shows what to check before the text, and keeps
    both ways of having Claude rewrite it, the alternatives and the free instruction, together under the actions."""
    _conversation()
    dbmod.set_setting("ui_lang", "de")
    client.post("/chats/c1/draft", data={"hint": ""})
    wait_jobs()
    with dbmod.session() as db:
        d = db.exec(select(dbmod.Draft)).one()
    page = client.get("/chats/c1").text
    composer = page[page.index('id="chat-composer"'):page.index('id="chat-context"')]
    head = composer[composer.index("data-draft-head"):composer.index("data-draft-check")]
    assert "Entwurf von Claude" in head and "Preisverhandlung" in head and "Preis € 180" in head
    assert ">price_negotiation<" not in composer                       # the raw key only as a data attribute
    check = composer[composer.index("data-draft-check"):composer.index('name="text"')]
    assert "Vor dem Senden prüfen" in check and "Claude braucht Infos: Abholzeit" in check
    revise = composer[composer.index("data-draft-revise"):]
    assert "Mit Claude überarbeiten" in revise
    assert f'action="/drafts/{d.id}/revise"' in revise and 'value="Nimm die 150 € an."' in revise
    assert 'action="/chats/c1/draft"' in revise and 'name="hint"' in revise and "Neu entwerfen" in revise
    # nothing to rewrite with above the text and the send button any more
    before = composer[:composer.index("data-draft-revise")]
    assert 'name="hint"' not in before and 'name="instruction"' not in before
    assert before.index("data-draft-actions") > before.index('name="text"') and "Senden" in before


def test_intent_labels():
    from app.i18n import set_request_lang
    from app.services.chats import intent_label

    assert intent_label("price_negotiation") == "Price negotiation" and intent_label("logistics") == "Pickup & shipping"
    assert intent_label(None) == "" and intent_label("other") == ""              # nothing worth saying
    assert intent_label("counter_offer") == "counter offer"                      # a value from the API, as written
    set_request_lang("de")
    try:
        assert [intent_label(i) for i in ("availability", "price_negotiation", "logistics", "question", "agreement",
                                          "scam_suspect", "sold_notice", "reserved_notice")] == [
            "Verfügbarkeit", "Preisverhandlung", "Abholung & Versand", "Frage", "Einigung", "Betrugsverdacht",
            "Info: verkauft", "Info: reserviert"]
    finally:
        set_request_lang("en")


def test_chat_draft_send_flow(client):
    _conversation()
    r = client.post("/chats/c1/draft", data={"hint": "Gegenangebot 180"}, follow_redirects=False)
    wait_jobs()
    assert r.status_code == 303 and r.headers["location"] == "/chats/c1"

    prompt = client.fake.calls[-1]["prompt"]
    assert "Fahrradhose Gr xs" in prompt          # ad context fetched from the (mocked) detail page
    assert "Partner: Nehmen Sie 150?" in prompt
    assert "Gegenangebot 180" in prompt

    page = client.get("/chats/c1").text
    assert "180 € wäre ok" in page and "Abholzeit" in page

    with dbmod.session() as db:
        draft = db.exec(select(dbmod.Draft)).one()
        conv = db.get(dbmod.Conversation, "c1")
    assert conv.ad_context["title"] == "Fahrradhose Gr xs"

    # Sending is queued: the page comes back at once with the message marked "sending" ...
    r = client.post(f"/drafts/{draft.id}/send", data={"text": "Hallo, 180 € passt."}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/chats/c1#draft-{draft.id}"
    wait_jobs()
    # ... no app API login in tests: it fails visibly, goes back to an open draft and nothing is marked as sent
    with dbmod.session() as db:
        d = db.get(dbmod.Draft, draft.id)
        assert d.status == "draft" and d.text == "Hallo, 180 € passt." and "app interface" in d.error
    assert client.get("/jobs").json()["recent"][0]["status"] == "failed"
    assert "app interface" in client.get("/chats/c1").text

    client.post(f"/drafts/{draft.id}/mark-sent", data={"text": "Hallo, 180 € passt."})
    msgs = client.get("/api/chats/c1").json()["messages"]
    assert [m["direction"] for m in msgs] == ["in", "out"]

    # the sent text becomes a style example for the next draft
    client.post("/chats/c1/draft", data={"hint": ""})
    wait_jobs()
    assert "Hallo, 180 € passt." in client.fake.calls[-1]["prompt"]


def test_api_save_draft_and_send_conflict(client):
    _conversation()
    d = client.post("/api/chats/c1/drafts", json={"text": "Ja, noch da."}).json()
    r = client.post(f"/api/drafts/{d['id']}/send", json={"text": "Ja, noch da."})
    assert r.status_code == 503 and r.json()["sent"] is False


def test_ad_draft_with_photo_and_price_stats(client):
    jpg = (FIXTURES / "search_fahrrad_rows3.json").read_bytes()[:100]  # any bytes, the fake ignores content
    r = client.post("/new-ad", data={"notes": "KTM Kinderrad 20 Zoll"},
                    files=[("photos", ("rad.jpg", jpg, "image/jpeg"))], follow_redirects=False)
    assert r.status_code == 303
    draft_url = r.headers["location"]                         # at once: Claude writes in the background
    wait_jobs()
    call = client.fake.calls[-1]
    assert len(call["images"]) == 1 and "KTM Kinderrad" in call["prompt"]
    page = client.get(draft_url).text
    assert "KTM Kinderrad 20 Zoll blau" in page
    assert 'id="draft-form" x-data="autosave"' in page and 'id="draft-aside"' in page and 'data-pending-key="refining:' not in page
    with dbmod.session() as db:
        d = db.exec(select(dbmod.AdDraft)).one()
    assert d.price_stats["llm_estimate"] == 120
    assert d.comparables  # comparable ads from the mocked search
    assert client.get(f"/ad-drafts/{d.id}/image/0").status_code == 200


def test_agent_create_via_api(client):
    r = client.post("/api/agents", json={"url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=rad"})
    assert r.status_code == 200 and r.json()["webapi_url"].endswith("keyword=rad&sort=1&rows=30")
    assert client.post("/api/agents", json={"url": "https://example.com"}).status_code == 400


class _FakeKeyboard:
    def __init__(self, log):
        self.log = log

    async def type(self, text):
        self.log.append(("type", text))

    async def press(self, key):
        self.log.append(("press", key))


class _FakeMouse:
    def __init__(self, log):
        self.log = log

    async def click(self, x, y):
        self.log.append(("click", x, y))

    async def move(self, x, y, steps=1):
        self.log.append(("move", x, y))

    async def down(self):
        self.log.append(("down",))

    async def up(self):
        self.log.append(("up",))

    async def wheel(self, dx, dy):
        self.log.append(("wheel", dy))


class _FakeContext:
    async def close(self):
        pass


class _FakePage:
    def __init__(self):
        self.log = []
        self.keyboard, self.mouse = _FakeKeyboard(self.log), _FakeMouse(self.log)


def test_remote_input(client, monkeypatch):
    from app.willhaben.session import get_session

    sess, page = get_session(), _FakePage()
    monkeypatch.setattr(sess, "context", _FakeContext())
    monkeypatch.setattr(sess, "api_page", object())
    monkeypatch.setattr(sess, "ui_page", page)

    assert client.post("/session/input", json={"type": "click", "x": 10.5, "y": 20}).status_code == 204
    assert client.post("/session/input", json={"type": "text", "text": "user@example.at"}).status_code == 204
    assert client.post("/session/input", json={"type": "key", "key": "Enter"}).status_code == 204
    assert client.post("/session/input", json={"type": "scroll", "dy": 99999}).status_code == 204
    assert [e for e in page.log if e[0] != "move"] == [("click", 10.5, 20.0), ("type", "user@example.at"),
                                                        ("press", "Enter"), ("wheel", 3000.0)]
    page.log.clear()
    slide = [{"x": 10, "y": 50, "t": 0}, {"x": 60, "y": 51, "t": 120}, {"x": 140, "y": 50, "t": 300}]
    assert client.post("/session/input", json={"type": "drag", "points": slide}).status_code == 204
    assert page.log == [("move", 10.0, 50.0), ("down",), ("move", 60.0, 51.0), ("move", 140.0, 50.0), ("up",)]
    assert client.post("/session/input", json={"type": "drag", "points": [{"x": 1, "y": 1}]}).status_code == 400

    assert client.post("/session/input", json={"type": "key", "key": "Control+w"}).status_code == 400
    assert client.post("/session/input", json={"type": "click"}).status_code == 400
    assert client.post("/session/input", json={"type": "nope"}).status_code == 400


def test_remote_input_without_browser(client):
    assert client.post("/session/input", json={"type": "text", "text": "x"}).status_code == 503
    assert client.get("/session/screen.jpg").status_code == 503


def test_agent_builder(client):
    r = client.get("/agents/new", params={"keyword": "fahrrad", "r_PRICE_TO": "800 €"})
    assert r.status_code == 200
    assert "Antiquitäten / Kunst" in r.text and "Create search agent" in r.text
    assert 'name="url" value="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad"' in r.text

    r = client.post("/agents", data={"url": "https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=fahrrad",
                                     "name": "fahrrad", "interval_min": "5"}, follow_redirects=False)
    assert r.status_code == 303
    agents = client.get("/api/agents").json()
    assert agents[-1]["name"] == "fahrrad" and "keyword=fahrrad" in agents[-1]["webapi_url"]


def test_agent_builder_rejects_foreign_url(client):
    r = client.get("/agents/new", params={"url": "https://evil.example/iad/x"})
    assert r.status_code == 200 and "Not a search link of a supported site" in r.text


def test_model_selector(client):
    from app.llm.base import get_backend

    import re

    def selected(page, p):           # the option(s) marked selected in the model_<p> select
        box = re.search(rf'<select name="model_{p}"[^>]*>(.*?)</select>', page, re.S).group(1)
        return re.findall(r'<option value="([^"]+)" selected>', box)

    page = client.get("/settings/claude").text
    assert 'name="model_chat"' in page and 'name="model_ads"' in page and 'name="claude_token_limit_m"' in page
    assert 'name="model_shipping"' in page and 'name="model_purchase"' in page
    assert selected(page, "shipping") == selected(page, "purchase") == ["claude-sonnet-5-5"]
    assert selected(page, "ads") == ["claude-opus-5-5"] and 'value="claude-fable-5-1"' in page
    assert client.post("/settings/claude", data={"model_chat": "haiku", "model_screen": "sonnet", "claude_token_limit_m": "12",
                                                 "model_purchase": "claude-fable-5-1", "model_shipping": "fable",
                                                 "claude_limit_mode": "haiku"}, follow_redirects=False).status_code == 303
    from app.db import get_setting, set_setting
    assert get_setting("claude_token_limit_m") == "12" and get_setting("claude_limit_mode") == "haiku"
    assert get_setting("llm_model_purchase") == get_setting("llm_model_shipping") == "claude-fable-5-1"
    assert get_setting("llm_model_chat") == "claude-haiku-5-5" and get_setting("llm_model_screen") == "claude-sonnet-5-5"
    page = client.get("/settings/claude").text
    assert selected(page, "purchase") == selected(page, "shipping") == ["claude-fable-5-1"]
    assert selected(page, "chat") == ["claude-haiku-5-5"]
    home = client.get("/").text
    assert 'href="/settings/claude" data-pop="claude" title="Claude tokens in the last 7 days"' in home and 'id="claude-pop"' in home   # meter + popover
    assert client.post("/settings/model", data={"purpose": "ads", "model": "claude-fable-5-1"},
                       follow_redirects=False).status_code == 303
    assert get_backend("chat").model == "claude-haiku-5-5"
    assert get_backend("ads").model == "claude-fable-5-1"
    args = get_backend("ads").build_args(system="s", schema={}, with_images=True)
    assert args[args.index("--model") + 1] == "claude-fable-5-1"
    assert get_backend(None).model == "opus"            # test-llm etc. still use the default
    assert client.post("/settings/model", data={"purpose": "chat", "model": "gpt-4"}).status_code == 400
    assert client.post("/settings/model", data={"purpose": "x", "model": "claude-opus-5-5"}).status_code == 400
    set_setting("llm_model_search", "claude-sonnet-4-5")     # not in the list: shown as it is, Save keeps it
    assert selected(client.get("/settings/claude").text, "search") == ["claude-sonnet-4-5"]
    client.post("/settings/claude", data={"model_search": "claude-sonnet-4-5"}, follow_redirects=False)
    assert get_setting("llm_model_search") == "claude-sonnet-4-5"


def test_hit_detail_viewer(client):
    from app.db import Hit, SearchAgent

    with dbmod.session() as db:
        db.add(SearchAgent(id=7, name="Rad", source_url="https://www.willhaben.at/iad/x", webapi_url="https://x"))
        db.add(Hit(id=5, agent_id=7, ad_id="1662146981", heading="Fahrradhose", url="https://www.willhaben.at/iad/object?adId=1662146981",
                   state="new"))
        db.commit()
    page = client.get("/hits", params={"agent_id": 7}).text
    assert 'data-ad="1662146981" data-hit="5"' in page and 'id="ad-viewer"' in page
    r = client.get("/hits?state=new&agent_id=")                 # "All search agents" sends an empty value
    assert r.status_code == 200 and 'data-hit="5"' in r.text
    assert client.get("/hits?agent_id=abc").status_code == 200   # junk is "all", not a 422 page
    frag = client.get("/hits/ad/1662146981", params={"hit": 5}).text
    assert "Fahrradhose Gr xs" in frag and "willhaben ↗" in frag and 'data-state="saved"' in frag
    assert "<html" not in frag                                   # fragment, not a full page
    with dbmod.session() as db:
        assert db.get(Hit, 5).state == "seen"                      # opening counts as seen
    assert client.get("/hits/ad/abc").status_code == 400


def test_ad_viewer_shows_full_claude_assessment(client):
    from app.db import Hit, SearchAgent

    reason = "Passt genau: Carbon-Rennrad Größe M. " + "Preis liegt deutlich unter vergleichbaren Angeboten. " * 6
    with dbmod.session() as db:
        db.add(SearchAgent(id=8, name="Rad", source_url="https://www.willhaben.at/iad/x", webapi_url="https://x"))
        db.add(Hit(id=6, agent_id=8, ad_id="1662146981", heading="Fahrradhose", url="https://www.willhaben.at/iad/object?adId=1662146981",
                   state="new", screen_relevant=True, screen_deal="great", screen_reason=reason, screen_at=dbmod.utcnow()))
        db.commit()
    frag = client.get("/hits/ad/1662146981", params={"hit": 6}).text
    assert "Claude" in frag and reason.strip() in frag            # complete text, not the 90-char card excerpt


def test_remote_overlay_compact_header(client, monkeypatch):
    from app.willhaben.session import get_session

    sess = get_session()
    status = {**sess.status(), "started": True, "logged_in": True, "user": {"nickname": "Max"}}
    monkeypatch.setattr(sess, "status", lambda: status)
    embed = client.get("/session", params={"embed": 1}).text
    assert 'class="embed-status' in embed and "Max" in embed
    assert "You log in in the app" not in embed and "Sie loggen" not in embed     # long explanation only on the full page
    assert '<header>' not in embed.split('id="remote"')[1].split("remote-screen")[0]  # no "Browser window" heading in the overlay
    full = client.get("/session").text
    assert 'class="embed-status' not in full and 'id="remote"' in full


def test_settings_link_to_the_browser_window(client, monkeypatch):
    """Settings → willhaben is the account's page: it links to the browser window (/session) instead of embedding
    it; /session shows the live view itself, unfolded."""
    from app.willhaben.session import get_session

    sess = get_session()
    status = {**sess.status(), "started": True, "logged_in": False}
    monkeypatch.setattr(sess, "status", lambda: status)
    page = client.get("/settings/willhaben").text
    assert 'id="s-browser-window" href="/session"' in page and 'id="remote"' not in page
    session_page = client.get("/session").text
    assert 'id="remote"' in session_page and '<details class="group"' not in session_page


def test_agent_create_form_is_not_a_squeezing_grid(client):
    page = client.get("/agents/new", params={"keyword": "fahrrad"}).text
    form = page.split('action="/agents"')[1].split("</form>")[0]
    assert 'name="name"' in form and 'name="screen_on"' in form and 'name="interval_min"' in form


def test_home_lists_only_ads_expiring_within_5_days_and_8_activities(client):  # noqa: F811
    from datetime import timedelta

    from app.db import EventLog, MyAd, session, utcnow
    with session() as db:
        db.add(MyAd(ad_id="31", heading="Bald weg", status="active", end_date=utcnow() + timedelta(days=3)))
        db.add(MyAd(ad_id="32", heading="Noch lange", status="active", end_date=utcnow() + timedelta(days=20)))
        for i in range(12):
            db.add(EventLog(kind="hit", message=f"Ereignis {i:02d}"))
        db.commit()
    page = client.get("/").text
    soon = page.split("Expiring soon")[1]
    assert "Bald weg" in soon and "Noch lange" not in soon
    activity = page.split('id="activity"')[1].split("</section>")[0]          # the inbox popover has its own list
    assert sum(f"Ereignis {i:02d}" in activity for i in range(12)) == 8


def test_home_counts_only_own_ads_online(client):
    with dbmod.session() as db:
        for n, (status, hidden) in enumerate((("active", None), ("active", False), ("active", True), ("expired", None),
                                              ("sold", None), ("deleted", None))):
            db.add(dbmod.MyAd(ad_id=f"9{n}", heading="Rad", status=status, hidden=hidden))
        db.commit()
    page = client.get("/").text
    assert re.search(r">2</div>\s*<div[^>]*>own ads online", page)


def test_home_counts_expired_own_ads_within_the_cutoff(client):
    """"n expired" on Home: as the My-ads list, only ads that ended within the last 30 days (the setting)."""
    from datetime import timedelta

    from app.db import set_setting, utcnow
    with dbmod.session() as db:
        for n, (days, hidden) in enumerate(((2, None), (29, None), (31, None), (90, None), (5, True))):
            db.add(dbmod.MyAd(ad_id=f"8{n}", heading="Rad", status="expired", hidden=hidden,
                              end_date=utcnow() - timedelta(days=days)))
        db.commit()
    assert re.search(r">2 expired<", client.get("/").text)
    set_setting("myads_expired_days", "100")
    assert re.search(r">4 expired<", client.get("/").text)          # 90 days too; hidden never


def test_home_counts_chats_with_a_draft_as_the_chat_list(client):
    """"open drafts" on Home: the shown chats with a draft (the list's "Draft" filter), not every stored draft – two
    drafts of one chat count once, a removed or hidden chat's draft not at all."""
    from app.db import utcnow
    with dbmod.session() as db:
        for cid, extra in (("d1", {}), ("d2", {"removed": True}), ("d3", {"hidden_at": utcnow()})):
            db.add(dbmod.Conversation(id=cid, ad_id="1", ad_title="Rad", partner_name="x", last_message_at=utcnow(),
                                      **extra))
        for cid in ("d1", "d1", "d2", "d3"):
            db.add(dbmod.Draft(conversation_id=cid, text="Hallo", status="draft"))
        db.commit()
    page = client.get("/").text
    assert re.search(r'href="/chats\?show=draft">\s*<div[^>]*>1</div>', page)
