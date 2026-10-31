"""PayLivery process: stage detection for both roles, state and history, card, settings, actions and their guards.

The browser helpers of paylivery (_collect, _click, _fill, _bank_reason ...) are replaced by fakes, so the step loop
runs without Chromium. One test uses a real Chromium page when one is installed (skipped otherwise).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db import Conversation, Message, Shipment, get_setting, session, set_setting, utcnow
from app.llm.base import LLMResult
from app.services import paylivery, shipping
from app.willhaben import session as willhaben_session
from app.willhaben.account import parse_messages
from app.willhaben.parse import parse_detail
from tests.conftest import load

CHAT = "https://www.willhaben.at/iad/myprofile/chat"
AD_URL = "https://www.willhaben.at/iad/object?adId=77"
AD_CTX = {"title": "Kinderbuch", "url": AD_URL, "price": "€ 12", "paylivery": True, "buy_now": True}


# ------------------------------------------------------------------ stage detection
@pytest.mark.parametrize("text, role, stage", [
    ("Preisvorschlag erhalten: € 120,00", "seller", "offer_received"),
    ("Preisvorschlag gesendet: € 100", "buyer", "offer_sent"),
    ("Preisvorschlag", "seller", "offer_received"),          # no direction word: the role decides
    ("Preisvorschlag", "buyer", "offer_sent"),
    ("Dein Preisvorschlag wurde angenommen", "buyer", "offer_accepted"),
    ("Preisvorschlag abgelehnt", "buyer", "offer_declined"),
    ("Der Preisvorschlag wurde nicht angenommen", "buyer", "offer_declined"),
    ("Zahlung eingegangen: € 12,34", "seller", "paid"),
    ("Du hast bezahlt", "buyer", "paid"),
    ("Dein Paketschein steht bereit", "seller", "label_ready"),
    ("Bezahlt. Versandlabel steht bereit", "seller", "label_ready"),   # most advanced stage wins
    ("Paket verschickt, Sendungsnummer: ABC12345678", "seller", "shipped"),
    ("Paket zugestellt", "buyer", "delivered"),
    ("Erhalt bestätigt", "seller", "received"),
    ("Problem gemeldet", "seller", "problem"),
    ("Auszahlung erfolgt", "seller", "paid_out"),
    ("Vorgang storniert", "buyer", "cancelled"),
    ("Sofort gekauft", "seller", "offer_accepted"),
    ("Du hast gekauft", "buyer", "paid"),
])
def test_stage_patterns(text, role, stage):
    assert paylivery.detect_stage(text, role) == stage


@pytest.mark.parametrize("text", [
    "Ist das noch da?",
    "PayLivery: sicher bezahlen",
    "Der Käufer hat nicht bezahlt",
    "Du hast unbezahlt gelassen",
    "Dank QR-Code kannst du ab jetzt ganz ohne Drucker versenden.",
    "Das Versandetikett lässt sich bequem ausdrucken.",
    "Angebote, die auch PayLivery anbieten, werden öfter geklickt. Die Zahlung passiert in Echtzeit.",
    "Erhalte jetzt Fixpreis-Angebote ohne zu verhandeln. Du entscheidest weiterhin, ob du annimmst oder ablehnst.",
])
@pytest.mark.parametrize("role", ["seller", "buyer"])
def test_generic_text_sets_no_stage(text, role):
    assert paylivery.detect_stage(text, role) is None


def test_structured_parameters_are_matched_too():
    params = {"status": {"label": "Preisvorschlag erhalten"}, "amount": 99.5}
    assert paylivery.detect_stage(paylivery._full_text("", params), "seller") == "offer_received"
    assert paylivery.notice_facts("", params, None)["amount"] == 99.5


def test_facts_amount_deadline_tracking_and_label():
    from datetime import datetime, timezone

    created = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    f = paylivery.notice_facts("Bitte bis 12.10. verschicken. Sendungsnummer: DX123456789 € 12,50", None, created)
    assert f["deadline"].startswith("2026-10-12") and f["tracking"] == "DX123456789" and f["amount"] == 12.5
    f = paylivery.notice_facts("Innerhalb von 7 Tagen", None, created)
    assert f["deadline"].startswith("2026-10-08")
    f = paylivery.notice_facts("Paket verschickt", None, created)       # a word is not a tracking number
    assert f["tracking"] is None
    f = paylivery.notice_facts("Label", None, created)
    assert f["label_url"] is None
    f = paylivery.notice_facts("Dein Etikett: https://www.willhaben.at/iad/paketschein/x.pdf", None, created)
    assert f["label_url"] == "https://www.willhaben.at/iad/paketschein/x.pdf"


def test_ad_detail_flags_paylivery_and_buy_now():
    """Fixture ad: buy-now (Sofort kaufen) but PayLivery switched off on the ad."""
    d = parse_detail(load("detail_1662146981_nextdata.json"))
    assert d.buy_now is True and d.paylivery_enabled is False
    ctx = d.as_context()
    assert ctx["buy_now"] is True and ctx["paylivery"] is False
    data = load("detail_1662146981_nextdata.json")
    data["advertDetails"]["advertisingParameters"]["payliveryEnabled"] = ["true"]
    assert parse_detail(data).paylivery_enabled is True


def test_parse_messages_keeps_parameters():
    data = {"messages": [{"message_id": "n1", "type": "integration_message", "text": "Hinweis",
                          "parameters": {"status": "Preisvorschlag erhalten", "amount": 5}}]}
    [m] = parse_messages(data, set())
    assert m.params == {"status": "Preisvorschlag erhalten", "amount": 5}


# ------------------------------------------------------------------ state and history
def _chat(cid: str = "c1", role: str = "seller", ad_ctx: dict | None = None, state: dict | None = None) -> None:
    with session() as db:
        db.add(Conversation(id=cid, ad_id="77", role=role, partner_name="Max", ad_title="Kinderbuch",
                            ad_context=AD_CTX if ad_ctx is None else ad_ctx, paylivery=state))
        db.commit()


def _notice(ext_id: str, text: str, cid: str = "c1", minutes_ago: int = 5, params=None) -> Message:
    with session() as db:
        m = Message(conversation_id=cid, ext_id=ext_id, direction="unknown", text=text, kind="integration",
                    created_at=utcnow() - timedelta(minutes=minutes_ago), params=params)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m


def _state(cid: str = "c1") -> dict:
    with session() as db:
        return dict(db.get(Conversation, cid).paylivery or {})


from datetime import timedelta  # noqa: E402  (used by the helpers above)


async def test_state_is_written_and_survives_a_reload():
    _chat()
    m = _notice("o1", "Preisvorschlag erhalten: € 120")
    with session() as db:
        conv = db.get(Conversation, "c1")
    changed = paylivery.apply_notice(conv, m)
    assert changed["stage"] == "offer_received" and changed["offer"] == 120.0
    fresh = _state()                                          # a new session reads what was written
    assert fresh["stage"] == "offer_received" and fresh["history"][0]["text"] == "Preisvorschlag erhalten: € 120"
    assert fresh["role"] == "seller" and fresh["applied"] == ["o1"]


async def test_a_notice_is_applied_once():
    _chat()
    m = _notice("o1", "Preisvorschlag erhalten: € 120")
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert paylivery.apply_notice(conv, m) is not None
    assert paylivery.apply_notice(conv, m) is None
    assert len(_state()["history"]) == 1


async def test_history_is_capped_at_30():
    _chat(state={"role": "seller", "stage": None, "history": [{"at": "x", "stage": None, "text": str(i)}
                                                              for i in range(30)]})
    with session() as db:
        conv = db.get(Conversation, "c1")
    paylivery.apply_notice(conv, _notice("o1", "Preisvorschlag erhalten: € 5"))
    hist = _state()["history"]
    assert len(hist) == 30 and hist[-1]["text"] == "Preisvorschlag erhalten: € 5" and hist[0]["text"] == "1"


async def test_stage_never_goes_back_after_payment():
    _chat(state={"role": "seller", "stage": "paid", "history": []})
    with session() as db:
        conv = db.get(Conversation, "c1")
    assert paylivery.apply_notice(conv, _notice("o1", "Preisvorschlag erhalten: € 5")) is None
    assert _state()["stage"] == "paid"
    assert paylivery.apply_notice(conv, _notice("c2", "Problem gemeldet")) is not None   # problems always apply
    assert _state()["stage"] == "problem"


async def test_offer_may_restart_before_payment():
    _chat(role="buyer", state={"role": "buyer", "stage": "offer_declined", "history": []})
    with session() as db:
        conv = db.get(Conversation, "c1")
    paylivery.apply_notice(conv, _notice("o2", "Preisvorschlag gesendet: € 90", cid="c1"))
    assert _state()["stage"] == "offer_sent" and _state()["offer"] == 90.0


async def test_buyer_notice_never_creates_or_marks_a_shipment(monkeypatch):
    _chat(cid="b1", role="buyer")
    _notice("p1", "Zahlung eingegangen: € 12,34", cid="b1")
    called = []

    async def fake_detect(*a, **k):
        called.append("detect")

    monkeypatch.setattr(shipping, "detect", fake_detect)
    assert await paylivery.scan("b1") is None
    with session() as db:
        assert db.exec(select(Shipment)).all() == []
    assert called == []
    assert _state("b1")["stage"] == "paid"                    # the state still follows the notice


async def test_scan_applies_notices_of_both_roles_and_marks_them_seen(monkeypatch):
    _chat()
    _notice("o1", "Preisvorschlag erhalten: € 120", minutes_ago=20)
    _notice("o2", "Preisvorschlag angenommen", minutes_ago=10)
    await paylivery.scan("c1")
    assert _state()["stage"] == "offer_accepted"
    assert json.loads(get_setting("paylivery_seen")) == ["c1:o1", "c1:o2"]


# ------------------------------------------------------------------ pushes
async def test_push_only_for_recent_stage_changes(monkeypatch):
    pushes = []

    async def fake_notify(kind, title, message, **kw):
        pushes.append((kind, title, kw.get("url")))
        return True

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    _chat()
    _notice("o0", "Preisvorschlag erhalten: € 90", minutes_ago=60 * 24 * 30)      # old: history only, no push
    _notice("p1", "Zahlung eingegangen: € 120", minutes_ago=5)                    # recent stage change: push
    _notice("o1", "Preisvorschlag erhalten: € 130", minutes_ago=4)                # same stage again: no push
    await paylivery.scan("c1")
    assert len(pushes) == 1 and pushes[0][0] == "paylivery"
    assert pushes[0][2].endswith("/chats/c1#paylivery")


async def test_push_can_be_switched_off(monkeypatch):
    pushes = []

    async def fake_notify(kind, title, message, **kw):
        pushes.append(kind)
        return True

    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    set_setting("paylivery_notify", "0")
    _chat()
    _notice("o1", "Preisvorschlag erhalten: € 120", minutes_ago=5)
    await paylivery.scan("c1")
    assert pushes == [] and _state()["stage"] == "offer_received"


# ------------------------------------------------------------------ card
def _card(role, stage, ad_ctx=None):
    conv = SimpleNamespace(id="c1", role=role, ad_context=AD_CTX if ad_ctx is None else ad_ctx,
                           paylivery={"role": role, "stage": stage, "history": [], "offer": 120.0} if stage else None,
                           partner_name="Max", ad_title="Kinderbuch")
    return paylivery.card(conv)


def test_card_hidden_without_state_and_without_paylivery():
    assert _card("seller", None, ad_ctx={"paylivery": False}) is None
    assert _card("buyer", None, ad_ctx={}) is None


def test_card_seller_offer_received_has_accept_counter_and_decline():
    c = _card("seller", "offer_received")
    assert [a["name"] for a in c["actions"]] == ["accept", "counter", "decline"]
    assert [a["input"] for a in c["actions"]] == [False, True, False]          # the counter offer takes an amount
    assert c["stage_label"] == "Offer received" and c["offer"] == 120.0          # the test locale is English


def test_card_seller_paid_offers_label_and_mark_shipped():
    assert [a["name"] for a in _card("seller", "paid")["actions"]] == ["label", "mark_shipped"]
    assert _card("seller", "delivered")["actions"] == []


def test_card_buyer_offers_offer_and_buy_now():
    c = _card("buyer", None)
    names = {a["name"]: a for a in c["actions"]}
    assert set(names) == {"offer", "buy"}
    assert not names["buy"]["disabled"] and not names["offer"]["disabled"]   # no switch: the confirm dialog guards


def test_card_buy_now_only_with_both_flags():
    no_buy_now = _card("buyer", None, ad_ctx={**AD_CTX, "buy_now": False})
    assert [a["name"] for a in no_buy_now["actions"]] == ["offer"]
    accepted = _card("buyer", "offer_accepted")
    assert [a["name"] for a in accepted["actions"]] == ["buy"] and not accepted["actions"][0]["disabled"]


def test_card_buyer_delivered_confirms_or_reports():
    assert [a["name"] for a in _card("buyer", "delivered")["actions"]] == ["receipt", "problem"]


def test_card_history_is_newest_first():
    conv = SimpleNamespace(id="c1", role="seller", ad_context=AD_CTX, partner_name="x", ad_title="y",
                           paylivery={"role": "seller", "stage": "paid", "offer": None,
                                      "history": [{"at": "1", "stage": "offer_received", "text": "first"},
                                                  {"at": "2", "stage": "paid", "text": "second"}]})
    assert [h["text"] for h in paylivery.card(conv)["history"]] == ["second", "first"]


def test_settings_card_defaults():
    assert paylivery.card(None) == {"notify": True}
    paylivery.save_settings(notify=False)
    assert paylivery.card(None) == {"notify": False}


# ------------------------------------------------------------------ routes, pages and settings
def test_chat_page_shows_the_card():
    from fastapi.testclient import TestClient

    from app.main import app

    _chat(state={"role": "seller", "stage": "offer_received", "offer": 120.0, "history": [],
                 "applied": []})
    with TestClient(app) as c:
        r = c.get("/chats/c1")
    assert r.status_code == 200
    assert 'id="paylivery"' in r.text and "Offer received" in r.text
    assert "/chats/c1/paylivery/accept" in r.text and "/chats/c1/paylivery/decline" in r.text
    card = r.text.split('id="paylivery"')[1].split("</section>")[0]       # sends in the background, swaps the card
    assert 'hx-post="/chats/c1/paylivery/accept" hx-target="#paylivery" hx-select="#paylivery"' in card
    assert 'hx-confirm="Accept this offer on willhaben now?"' in card and "return confirm(" not in card
    _chat(cid="c2", state={"role": "seller", "stage": "paid", "amount": 120.0, "history": [], "applied": []})
    with TestClient(app) as c:
        card = c.get("/chats/c2").text.split('id="paylivery"')[1].split("</section>")[0]
    assert 'hx-post="/chats/c2/paylivery/mark_shipped"' in card and "hx-confirm" not in card   # no confirm: one click


def test_settings_route_saves_switches():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post("/settings/paylivery", data={}, follow_redirects=False)
        assert r.status_code == 303 and "/settings/paylivery?" in r.headers["location"]
        assert get_setting("paylivery_notify") == "0"
        page = c.get("/settings/paylivery")
    assert page.status_code == 200 and 'id="paylivery"' in page.text


def test_action_route_refuses_buying_without_paylivery_on_the_ad():
    from fastapi.testclient import TestClient

    from app.main import app

    _chat(cid="b1", role="buyer", ad_ctx={**AD_CTX, "paylivery": False})
    with TestClient(app) as c:
        r = c.post("/chats/b1/paylivery/buy", data={"amount": ""}, follow_redirects=False)
        assert r.status_code == 303 and "#paylivery" in r.headers["location"]
        assert "PayLivery" in r.headers["location"] or "msg=" in r.headers["location"]
        hx = c.post("/chats/b1/paylivery/buy", data={"amount": ""}, headers={"HX-Request": "true"}, follow_redirects=False)
        assert hx.headers["location"] == r.headers["location"]                 # a refusal stays, also through htmx


# ------------------------------------------------------------------ browser action guards (pure)
@pytest.mark.parametrize("action, label, verdict", [
    ("accept", "Angebot annehmen", "final"),
    ("accept", "Kaufen", "refused"),                 # another action's commit button
    ("accept", "Verkaufen", "neutral"),              # no word-boundary match
    ("accept", "Alle akzeptieren", "refused"),       # cookie banner words
    ("accept", "Bedingungen akzeptieren", "refused"),
    ("accept", "Konto löschen", "refused"),
    ("accept", "", "refused"),
    ("accept", "Nachricht lesen", "neutral"),
    ("decline", "Ablehnen", "final"),
    ("buy", "Sofort kaufen", "final"),
    ("buy", "Zur Kasse", "neutral"),
    ("buy", "Bezahlen", "final"),
    ("buy", "Angebot annehmen", "refused"),
    ("offer", "Preisvorschlag senden", "final"),
    ("offer", "Nachricht senden", "neutral"),
    ("receipt", "Erhalt bestätigen", "final"),
    ("problem", "Problem melden", "final"),
])
def test_click_policy(action, label, verdict):
    assert paylivery.click_verdict(action, label) == verdict


@pytest.mark.parametrize("field, allowed", [
    ({"type": "password", "label": "Passwort"}, False),
    ({"type": "text", "name": "cc-number", "autocomplete": "cc-number"}, False),
    ({"type": "text", "label": "IBAN"}, False),
    ({"type": "checkbox", "label": "Ich akzeptiere die AGB"}, False),
    ({"type": "checkbox", "label": "Paket versichern"}, True),
    ({"type": "text", "label": "Betrag"}, True),
])
def test_fill_policy(field, allowed):
    assert paylivery.fill_allowed(field) is allowed


# ------------------------------------------------------------------ browser actions with fakes
class FakeSess:
    def __init__(self, page, capturing: bool = False):
        self.started = True
        self.capturing = capturing
        self.ui_page = page
        self.opened: list[str] = []
        self.starts: list[str] = []
        self.stops = 0

    async def open_url(self, url):
        self.opened.append(url)

    def ui_in_use(self):
        from contextlib import nullcontext
        return nullcontext()

    def start_capture(self, label):
        self.starts.append(label)
        self.capturing = True

    async def stop_capture(self):
        self.stops += 1
        self.capturing = False
        return Path("/tmp/capture-1.json")


class FakeBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, *, system, prompt, schema, images=None):
        self.prompts.append(prompt)
        return LLMResult(data=self.replies.pop(0), model="fake")


def _step(**kw):
    base = {"fills": [], "action_idx": None, "status": "continue", "note": ""}
    return {**base, **kw}


@pytest.fixture
def browser(monkeypatch):
    """Fake page + session + Claude; helpers replaced so no browser is needed. Returns a namespace to script."""
    async def noop(*a, **k):
        return None

    ns = SimpleNamespace(page=SimpleNamespace(url=CHAT), elements=[], clicked=[], filled=[], bank=None,
                         replies=[], sess=None, backend=None, body="Gesamtbetrag € 12,00")

    async def inner_text(selector):
        return ns.body
    ns.page.inner_text = inner_text            # the checkout's total, read before the pay click

    async def collect(page):
        return list(ns.elements)

    async def click(page, el):
        ns.clicked.append(el["label"])

    async def fill(page, el, value):
        ns.filled.append((el["idx"], value))

    async def bank(page):
        return ns.bank

    async def card_visible(page):
        return False

    monkeypatch.setattr(paylivery, "_busy", {})          # a faked run_action never frees its chat
    monkeypatch.setattr(paylivery, "_settle", noop)
    monkeypatch.setattr(paylivery, "_dismiss", noop)
    monkeypatch.setattr(paylivery, "_collect", collect)
    monkeypatch.setattr(paylivery, "_click", click)
    monkeypatch.setattr(paylivery, "_fill", fill)
    monkeypatch.setattr(paylivery, "_bank_reason", bank)
    monkeypatch.setattr(paylivery, "_card_fields_visible", card_visible)
    ns.sess = FakeSess(ns.page)
    monkeypatch.setattr(willhaben_session, "get_session", lambda: ns.sess)
    ns.backend = FakeBackend([])
    ns.purposes = []                                # the get_backend(purpose) of each call
    monkeypatch.setattr("app.llm.base.get_backend", lambda purpose=None: ns.purposes.append(purpose) or ns.backend)
    return ns


def _btn(idx: int, label: str) -> dict:
    return {"idx": idx, "kind": "button", "tag": "button", "type": "button", "name": "", "label": label,
            "text": label, "placeholder": "", "autocomplete": "", "aria": "", "value": ""}


def _field(idx: int, label: str, type_: str = "text", name: str = "") -> dict:
    return {"idx": idx, "kind": "field", "tag": "input", "type": type_, "name": name, "label": label,
            "placeholder": "", "autocomplete": "", "aria": "", "value": ""}


async def test_accept_offer_clicks_once_and_moves_the_stage(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Angebot annehmen"), _btn(2, "Ablehnen")]
    browser.backend.replies = [_step(action_idx=1, status="done", note="angenommen")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == ["Angebot annehmen"]
    assert browser.purposes == ["purchase"]                          # Settings → Claude → "Purchases & PayLivery"
    assert browser.sess.opened == [CHAT]
    assert browser.sess.starts == ["paylivery-accept"] and browser.sess.stops == 1
    assert "/tmp/capture-1.json" not in msg and "capture-1.json" in msg
    st = _state()
    assert st["stage"] == "offer_accepted" and st["history"][-1]["text"].startswith("accept:")


async def test_never_clicks_another_actions_commit_button(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Kaufen"), _btn(2, "Angebot annehmen")]
    browser.backend.replies = [_step(action_idx=1, status="continue")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == [] and "refused" in msg
    assert _state()["stage"] == "offer_received"              # not moved by a refused click
    assert browser.sess.stops == 1                             # the recording is still stopped


async def test_done_without_a_confirm_button_is_not_success(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Nachricht lesen")]
    browser.backend.replies = [_step(action_idx=1, status="done", note="fertig?")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == ["Nachricht lesen"]
    assert "no confirm" in msg or "confirmed" in msg
    assert _state()["stage"] == "offer_received"


async def test_bank_page_stops_for_the_user(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.bank = "Zahlungsbestätigung (3-D Secure / TAN)"
    browser.elements = [_btn(1, "Angebot annehmen")]
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == [] and browser.backend.prompts == []
    assert "/session" in msg or "Session" in msg
    assert browser.sess.stops == 1


async def test_recording_started_by_the_user_is_left_alone(browser):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.sess.capturing = True
    browser.elements = [_btn(1, "Angebot annehmen")]
    browser.backend.replies = [_step(action_idx=1, status="done")]
    await paylivery.run_action("c1", "accept")
    assert browser.sess.starts == [] and browser.sess.stops == 0


async def test_offer_amount_is_the_confirmed_one_not_claudes(browser, monkeypatch):
    monkeypatch.setattr(paylivery.willhaben_session, "get_session", lambda: browser.sess)
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(1, "Preisvorschlag"), _field(2, "Betrag in €", "text", "amount")]
    browser.backend.replies = [
        _step(fills=[{"idx": 2, "value": "999"}], action_idx=1, status="continue"),
        _step(action_idx=3, status="done", note="gesendet"),
    ]
    browser.elements.append(_btn(3, "Preisvorschlag senden"))
    msg = await paylivery.run_action("b1", "offer", 120.0)
    assert set(browser.filled) == {(2, "120,00")}             # the code's amount (German style for a text field), never 999
    assert browser.clicked == ["Preisvorschlag", "Preisvorschlag senden"]
    assert "gesendet" in msg
    assert _state("b1")["stage"] == "offer_sent" and _state("b1")["history"][-1]["text"].startswith("offer:")


async def test_buy_needs_paylivery_and_a_price(browser):
    _chat(cid="b1", role="buyer", ad_ctx={**AD_CTX, "paylivery": False})
    with pytest.raises(paylivery.PayLiveryError):
        await paylivery.run_action("b1", "buy", 12.0)
    with pytest.raises(paylivery.PayLiveryError):
        paylivery.check_action(SimpleNamespace(role="buyer", paylivery={}, ad_context={"paylivery": True}),
                               "buy", None)


async def test_buy_never_fills_card_fields_itself(browser, monkeypatch):
    _chat(cid="b1", role="buyer")
    browser.elements = [_field(1, "Kartennummer", "text", "cardnumber"), _btn(2, "Sofort kaufen")]
    monkeypatch.setattr(paylivery, "_card_fields_visible", _true)
    browser.backend.replies = []
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert browser.filled == [] and browser.clicked == []
    assert "card" in msg or "Karte" in msg or "Kartendaten" in msg or "card details" in msg


async def _true(page):
    return True


async def test_buy_fills_the_card_once_then_presses_pay(browser, monkeypatch):
    _chat(cid="b1", role="buyer")
    browser.elements = [_field(1, "Kartennummer", "text", "cardnumber"), _btn(2, "Jetzt bezahlen")]
    monkeypatch.setattr(paylivery, "_card_fields_visible", _true)
    fills = []

    async def fill_card(page):
        fills.append(1)
        return True

    monkeypatch.setattr(paylivery, "_fill_card", fill_card)
    browser.backend.replies = [_step(action_idx=2, status="done", note="bezahlt")]
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert fills == [1] and browser.clicked == ["Jetzt bezahlen"] and "bezahlt" in msg


BANK = "Zahlungsbestätigung (3-D Secure / TAN)"


@pytest.fixture
def bank_steps(browser, monkeypatch):
    """Scripted bank check (script first, then `default`); asyncio.sleep is recorded instead of waited for; pushes
    are recorded. Built on the browser fixture, so the rest of the step loop is the fake one."""
    real_sleep = asyncio.sleep
    ns = SimpleNamespace(script=[], default=None, sleeps=[], pushes=[])

    async def bank(page):
        return ns.script.pop(0) if ns.script else ns.default

    async def fake_sleep(seconds, *a, **k):
        ns.sleeps.append(seconds)
        await real_sleep(0)

    async def fake_notify(kind, title, message, **kw):
        ns.pushes.append({"kind": kind, "title": title, "message": message, **kw})
        return True

    monkeypatch.setattr(paylivery, "_bank_reason", bank)
    monkeypatch.setattr(paylivery.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    return ns


async def test_buy_waits_for_the_bank_approval_then_continues(browser, bank_steps):
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(2, "Jetzt bezahlen")]
    # checks: before the pay click (no bank), the bank step appears, polls still bank, then approved (re-checked)
    bank_steps.script = [None, BANK, BANK, None, None]
    browser.backend.replies = [_step(action_idx=2, status="continue"), _step(status="done", note="bezahlt")]
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert browser.clicked == ["Jetzt bezahlen"] and len(browser.backend.prompts) == 2   # Claude sees the confirmation
    assert "bezahlt" in msg
    assert bank_steps.sleeps == [paylivery.BANK_POLL_S] * 2
    assert len(bank_steps.pushes) == 1
    push = bank_steps.pushes[0]
    assert push["kind"] == "paylivery" and push["url"].endswith("/chats/b1#paylivery")
    assert push["data"] == {"stage": "bank_approval", "role": "buyer", "ad_id": "77", "conversation_id": "b1", "amount": 12.0}
    assert "Kinderbuch" in push["message"] and "12,00 €" in push["message"]
    st = _state("b1")
    assert st["stage"] == "paid" and any("bank" in h["text"].lower() for h in st["history"])


async def test_buy_bank_wait_times_out_into_the_remote_view_message(browser, bank_steps):
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(2, "Jetzt bezahlen")]
    bank_steps.script = [None]
    bank_steps.default = BANK                                 # never approved
    browser.backend.replies = [_step(action_idx=2, status="continue")]
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert len(bank_steps.sleeps) == paylivery.BANK_WAIT_S // paylivery.BANK_POLL_S
    assert len(bank_steps.pushes) == 1                        # still only one push
    assert len(browser.backend.prompts) == 1                  # no further Claude step after the timeout
    assert "Session" in msg
    assert _state("b1")["history"][-1]["text"].startswith("buy:")
    assert browser.sess.stops == 1


async def test_buy_bank_push_can_be_switched_off(browser, bank_steps):
    set_setting("paylivery_notify", "0")
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(2, "Jetzt bezahlen")]
    bank_steps.script = [None, BANK, None, None]
    browser.backend.replies = [_step(action_idx=2, status="continue"), _step(status="done", note="bezahlt")]
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert "bezahlt" in msg and bank_steps.pushes == []
    assert len(bank_steps.sleeps) == 1                        # the wait itself still happens


async def test_other_actions_stop_at_the_bank_step_at_once(browser, bank_steps):
    _chat(state={"role": "seller", "stage": "offer_received", "history": [], "applied": []})
    browser.elements = [_btn(1, "Angebot annehmen")]
    bank_steps.default = BANK
    msg = await paylivery.run_action("c1", "accept")
    assert browser.clicked == [] and browser.backend.prompts == []
    assert bank_steps.sleeps == [] and bank_steps.pushes == []
    assert browser.sess.stops == 1 and msg


async def test_label_and_mark_shipped_are_local(browser):
    _chat(state={"role": "seller", "stage": "paid", "history": [], "applied": []})
    msg = await paylivery.start_action("c1", "mark_shipped")
    assert "Marked" in msg or "markiert" in msg
    assert _state()["stage"] == "shipped"
    assert browser.sess.opened == []                          # no browser for a local action


async def test_download_label_without_link_leaves_it_to_the_browser(browser):
    with session() as db:
        db.add(Conversation(id="c1", role="seller", paylivery={"role": "seller", "stage": "paid", "history": []}))
        db.add(Shipment(conversation_id="c1", status="awaiting"))
        db.commit()
    assert await paylivery.download_label("c1") is None


# ------------------------------------------------------------------ label: created in willhaben's dialog
LABEL_LINK = "https://www.willhaben.at/iad/paketschein/x.pdf"
PDF = b"%PDF-1.4 label"


class FakeContext:
    """Browser context events of the label action: responses, new pages, downloads."""

    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.pages: list = []

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def remove_listener(self, event, fn):
        self.handlers[event].remove(fn)

    def emit(self, event, arg):
        for fn in list(self.handlers.get(event, [])):
            fn(arg)


class FakeResponse:
    def __init__(self, body, kind="application/pdf", url="https://www.willhaben.at/iad/paketschein/x"):
        self.url, self.headers, self._body = url, {"content-type": kind}, body

    async def body(self):
        return self._body


class _LinkReq:
    """context.request.get of the plain link fetch: an HTML page (the create dialog) until `pdf` is set."""

    def __init__(self):
        self.pdf, self.urls = False, []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        body = PDF if self.pdf else b"<html>Etikett erstellen?</html>"
        return SimpleNamespace(ok=True, status=200, headers={}, body=lambda: _value(body))


async def _value(v):
    return v


@pytest.fixture
def label_chat(browser):
    _chat(state={"role": "seller", "stage": "label_ready", "history": [], "applied": [], "label_url": LABEL_LINK})
    with session() as db:
        db.add(Shipment(conversation_id="c1", status="awaiting"))
        db.commit()
    browser.sess.context = FakeContext()
    browser.req = _LinkReq()
    browser.sess.context.request = browser.req
    return browser


def _shipment() -> Shipment:
    with session() as db:
        return db.exec(select(Shipment).where(Shipment.conversation_id == "c1")).one()


def test_label_dialog_buttons_are_this_actions_own():
    assert paylivery.click_verdict("label", "Etikett erstellen") == "final"
    assert paylivery.click_verdict("label", "Bestätigen") == "final"        # receipt's word, but the dialog's here
    assert paylivery.click_verdict("label", "Herunterladen") == "final"
    assert paylivery.click_verdict("label", "Angebot annehmen") == "refused"


async def test_get_label_uses_the_link_when_it_gives_the_pdf(label_chat):
    label_chat.req.pdf = True
    msg = await paylivery.start_action("c1", "label")
    assert "saved" in msg or "gespeichert" in msg
    assert label_chat.sess.opened == []                       # no browser for a ready PDF
    assert paylivery._busy == {}                              # and the chat is free again
    s = _shipment()
    assert s.status == "labelled" and s.label_path == f"shipment-{s.id}-paylivery.pdf"


async def test_get_label_starts_the_browser_when_willhaben_asks_to_create_it(label_chat, monkeypatch):
    started = []

    async def fake_run(cid, action, amount=None, claimed=False):
        started.append((cid, action, claimed))
        paylivery._busy.pop(cid, None)
        return "ok"

    monkeypatch.setattr(paylivery, "run_action", fake_run)
    msg = await paylivery.start_action("c1", "label")
    assert paylivery._busy == {"c1": "label"}                 # the chat is taken until the browser action ends
    await asyncio.gather(*paylivery._tasks)
    assert started == [("c1", "label", True)] and label_chat.req.urls == [LABEL_LINK]
    assert "Started" in msg or "Im Browser" in msg
    assert paylivery.card(_conv("c1"))["actions"][0]["confirm"] == ""   # one click: no confirm dialog


async def test_label_action_creates_it_and_catches_the_pdf(label_chat, monkeypatch):
    label_chat.elements = [_btn(1, "Etikett erstellen"), _btn(2, "Abbrechen")]
    label_chat.backend.replies = [_step(action_idx=1, status="continue", note="Dialog bestätigt")]

    async def click(page, el):
        label_chat.clicked.append(el["label"])
        label_chat.sess.context.emit("response", FakeResponse(b"<html>", kind="text/html"))
        label_chat.sess.context.emit("response", FakeResponse(PDF))      # the PDF tab after creating

    monkeypatch.setattr(paylivery, "_click", click)
    msg = await paylivery.run_action("c1", "label")
    assert label_chat.sess.opened == [LABEL_LINK]             # starts on the label link, not the chat list
    assert label_chat.clicked == ["Etikett erstellen"] and len(label_chat.backend.prompts) == 1
    assert "gespeichert" in msg or "saved" in msg
    s = _shipment()
    assert s.status == "labelled"
    assert (get_settings().data_dir / "labels" / s.label_path).read_bytes() == PDF
    assert label_chat.sess.context.handlers == {"response": [], "page": []}   # listeners removed
    st = _state()
    assert st["stage"] == "label_ready" and st["history"][-1]["action"] == "label" and st["history"][-1]["ok"]


async def test_label_action_catches_a_download_too(label_chat, monkeypatch, tmp_path):
    f = tmp_path / "label.pdf"
    f.write_bytes(PDF)
    download = SimpleNamespace(path=lambda: _value(str(f)))
    page = FakeContext()                                      # a page: it only needs on/remove_listener/emit
    label_chat.sess.context.pages = [page]
    label_chat.elements = [_btn(1, "Herunterladen")]
    label_chat.backend.replies = [_step(action_idx=1, status="done")]

    async def click(page_, el):
        page.emit("download", download)

    monkeypatch.setattr(paylivery, "_click", click)
    await paylivery.run_action("c1", "label")
    assert _shipment().status == "labelled" and page.handlers["download"] == []


async def test_label_action_takes_the_link_once_created(label_chat, monkeypatch):
    label_chat.elements = [_btn(1, "Etikett erstellen")]
    label_chat.backend.replies = [_step(action_idx=1, status="done", note="erstellt")]

    async def click(page, el):
        label_chat.req.pdf = True                             # created: the link serves the PDF now

    monkeypatch.setattr(paylivery, "_click", click)
    await paylivery.run_action("c1", "label")
    assert _shipment().status == "labelled"


async def test_label_action_without_pdf_says_so(label_chat):
    label_chat.elements = [_btn(1, "Etikett erstellen")]
    label_chat.backend.replies = [_step(action_idx=1, status="done", note="erstellt")]
    msg = await paylivery.run_action("c1", "label")
    s = _shipment()
    assert s.label_path is None and s.status == "awaiting"
    assert "PDF" in msg
    assert _state()["history"][-1]["ok"] is False


async def test_action_refused_for_the_other_role(browser):
    _chat(cid="b1", role="buyer")
    with pytest.raises(paylivery.PayLiveryError):
        paylivery.check_action(_conv("b1"), "accept", None)


async def test_start_action_is_background_for_browser_actions(browser, monkeypatch):
    _chat(cid="b1", role="buyer")
    started = []

    async def fake_run(cid, action, amount=None, claimed=False):
        started.append((cid, action, amount))
        return "ok"

    monkeypatch.setattr(paylivery, "run_action", fake_run)
    msg = await paylivery.start_action("b1", "offer", "120,50")
    assert "Started" in msg or "Im Browser" in msg
    await asyncio.gather(*paylivery._tasks)
    assert started == [("b1", "offer", 120.5)]


def _conv(cid: str):
    with session() as db:
        return db.get(Conversation, cid)


# ------------------------------------------------------------------ real Chromium (skipped when not installed)
@pytest.fixture
async def chromium_page():
    pw_mod = pytest.importorskip("playwright.async_api")
    async with pw_mod.async_playwright() as pw:
        browser = None
        for kwargs in ({}, {"channel": "chrome"}):
            try:
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001 - not installed under this name
                continue
        if browser is None:
            pytest.skip("no Chromium or Google Chrome for Playwright")
        page = await browser.new_page()
        yield page
        await browser.close()


async def test_collector_and_click_on_a_real_page(chromium_page):
    page = chromium_page
    await page.set_content("""<html><body>
      <input name="betrag" placeholder="Betrag">
      <input type="password" name="pw">
      <button id="ok" onclick="window.clicked = (window.clicked||0)+1">Angebot annehmen</button>
      <button onclick="window.bad = 1">Kaufen</button>
    </body></html>""")
    elements = await paylivery._collect(page)
    labels = [e.get("label") for e in elements]
    assert "Angebot annehmen" in labels and "Kaufen" in labels
    assert not any(e.get("name") == "pw" for e in elements)               # password fields never reach Claude
    accept = next(e for e in elements if e.get("label") == "Angebot annehmen")
    assert paylivery.click_verdict("accept", accept["label"]) == "final"
    await paylivery._click(page, accept)
    assert await page.evaluate("window.clicked") == 1
    assert await paylivery._bank_reason(page) is None


async def test_buy_refuses_a_checkout_total_above_the_limit(browser):
    """The last look before money moves: the seller raised the price since the ad was read."""
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(2, "Jetzt bezahlen")]
    browser.body = "Artikel € 60,00 · Versand € 5,90 · Gesamtbetrag € 65,90"     # confirmed: 12 € (+10 % + 25 €)
    browser.backend.replies = [_step(action_idx=2, status="done", note="bezahlt")]
    msg = await paylivery.run_action("b1", "buy", 12.0)
    assert browser.clicked == [] and "65,90" in msg and "38,20" in msg


async def test_buy_refuses_a_page_without_a_total(browser):
    _chat(cid="b1", role="buyer")
    browser.elements = [_btn(2, "Jetzt bezahlen")]
    browser.body = "Zahlungsart wählen"
    browser.backend.replies = [_step(action_idx=2, status="done", note="bezahlt")]
    await paylivery.run_action("b1", "buy", 12.0)
    assert browser.clicked == []


async def test_a_double_click_starts_one_browser_action(browser, monkeypatch):
    _chat(cid="b1", role="buyer")
    runs = []

    async def fake_drive(sess, conv, action, amount):
        runs.append(action)
        await asyncio.sleep(0)
        return True, "ok"
    monkeypatch.setattr(paylivery, "_drive", fake_drive)
    await paylivery.start_action("b1", "offer", "100")
    with pytest.raises(paylivery.PayLiveryError):              # before the first one ran a single step
        await paylivery.start_action("b1", "offer", "100")
    with pytest.raises(paylivery.PayLiveryError):              # the order's own offer at the same moment
        await paylivery.run_action("b1", "offer", 100.0)
    await asyncio.gather(*paylivery._tasks)
    assert runs == ["offer"] and "b1" not in paylivery._busy
    await paylivery.run_action("b1", "offer", 100.0)          # free again afterwards
    assert runs == ["offer", "offer"]


def test_buy_now_over_the_orders_maximum_is_refused_before_the_browser():
    from app.db import Purchase
    _chat(cid="b1", role="buyer")
    with session() as db:
        db.add(Purchase(ad_id="77", status="pay_now", mode="paylivery", conversation_id="b1", max_price=50.0,
                        shipping_cost=5.0))
        db.commit()
    with pytest.raises(paylivery.PayLiveryError, match="maximum"):
        paylivery.check_action(_conv("b1"), "buy", 48.0)          # 48 + shipping + fee > 50
    paylivery.check_action(_conv("b1"), "buy", 40.0)
    assert paylivery.buy_ceiling(_conv("b1"), 40.0) == 50.0       # and the checkout total may not pass it either



async def test_seller_counter_offer_fills_the_amount_and_presses_send(browser):
    _chat(cid="s1", role="seller", state={"role": "seller", "stage": "offer_received", "offer": 30.0, "history": []})
    browser.elements = [_btn(1, "Annehmen"), _btn(2, "Ablehnen"), _field(3, "Dein Gegenangebot (€)", "number", "amount"),
                        _btn(4, "Gegenangebot senden")]
    browser.backend.replies = [_step(fills=[{"idx": 3, "value": "99"}], action_idx=1, status="continue"),  # refused
                               ]
    msg = await paylivery.run_action("s1", "counter", 45.0)
    assert browser.clicked == [] and "Annehmen" in msg                          # never accept while countering
    browser.backend.replies = [_step(fills=[{"idx": 3, "value": "99"}], action_idx=4, status="done", note="gesendet")]
    msg = await paylivery.run_action("s1", "counter", 45.0)
    assert browser.clicked == ["Gegenangebot senden"] and (3, "45.00") in browser.filled and (3, "99") not in browser.filled
    st = _state("s1")
    assert st["stage"] == "counter_sent" and st["offer"] == 45.0


def test_click_verdicts_keep_offer_and_counter_apart():
    assert paylivery.click_verdict("counter", "Gegenangebot senden") == "final"
    assert paylivery.click_verdict("offer", "Gegenangebot senden") == "refused"
    assert paylivery.click_verdict("counter", "Angebot senden") == "final"          # willhaben's window button
    assert paylivery.click_verdict("counter", "Anfrage annehmen") == "refused"
    assert paylivery.click_verdict("counter", "Gegenangebot machen") == "neutral"      # opens the form only


def test_buyer_accepts_a_counter_offer_only_within_the_order():
    from app.db import Purchase
    _chat(cid="b1", role="buyer", state={"role": "buyer", "stage": "counter_received", "offer": 48.0, "history": []})
    c = paylivery.card(_conv("b1"))
    assert [a["name"] for a in c["actions"]] == ["accept", "decline"] and c["stage_label"] == "Counter offer received"
    paylivery.check_action(_conv("b1"), "accept", None)
    with session() as db:
        db.add(Purchase(ad_id="77", status="negotiating", mode="paylivery", conversation_id="b1", max_price=50.0,
                        shipping_cost=5.0))
        db.commit()
    with pytest.raises(paylivery.PayLiveryError, match="maximum"):
        paylivery.check_action(_conv("b1"), "accept", None)
    assert paylivery._goal("accept", None, "buyer").startswith("Das Gegenangebot des Verkäufers ANNEHMEN")


async def test_the_paylivery_window_iframe_is_read_and_clicked_in():
    """The counter offer dialog is willhaben's iframe: its fields come first (marked), clicks go into that frame."""
    class Loc:
        def __init__(self, fr, sel):
            self.fr, self.sel, self.first = fr, sel, self

        async def click(self, timeout=None):
            self.fr.clicked.append(self.sel)

    class Frame:
        def __init__(self, url, els, text=""):
            self.url, self.els, self.text, self.clicked = url, els, text, []

        async def evaluate(self, js, start):
            return [{**e, "idx": start + i} for i, e in enumerate(self.els)]

        def locator(self, sel):
            return Loc(self, sel)

        async def inner_text(self, sel):
            return self.text

    window = Frame("https://www.willhaben.at/p2p/offer/x", [{"kind": "field", "label": "Betrag"},
                                                            {"kind": "button", "label": "Preisvorschlag senden"}],
                   "Gesamt € 50,00")
    ad = Frame("https://ads.example.com/x", [{"kind": "button", "label": "Werbung"}])
    page = Frame("https://www.willhaben.at/iad/myprofile/chat/x", [{"kind": "button", "label": "Neuer Preisvorschlag"}],
                 "Chat")
    page.main_frame, page.frames = page, [page, window, ad]
    els = await paylivery._collect(page)
    assert [(e["idx"], e["label"], e.get("fenster")) for e in els] == [
        (1, "Betrag", True), (2, "Preisvorschlag senden", True), (3, "Neuer Preisvorschlag", None)]
    await paylivery._click(page, els[1])
    assert window.clicked == ['[data-plidx="2"]'] and page.clicked == []
    assert "Gesamt € 50,00" in await paylivery._all_text(page)
