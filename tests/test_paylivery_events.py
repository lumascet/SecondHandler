"""PayLivery notices: only reported events set a stage (explanations, conditions, announcements do not), the
"released" stage of a paid and accepted purchase, and the recheck of chats stuck on a stage older rules misread.

The buy-now chat of 11.10. (buyer): the PayLivery intro said "... wird dir zugestellt", the release notice said
"sobald dein Artikel unterwegs ist", and the card showed "Zugestellt" with "Erhalt bestätigen" for an item just bought.
"""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.db import Conversation, Message, session, utcnow
from app.services import paylivery

AD_CTX = {"title": "Kinderbuch", "url": "https://www.willhaben.at/iad/object?adId=77", "price": "€ 12",
          "paylivery": True, "buy_now": True}
INTRO = ("willhaben PayLivery - Online bezahlen und liefern lassen Kaufe sofort oder sende eine Anfrage. Wird sie "
         "angenommen, bezahlst du bequem online und der Artikel wird dir zugestellt - mit Käuferschutz! "
         "Mehr zu PayLivery")
WAIT = "Der Verkäufer hat jetzt 24 Stunden Zeit, deinen Kauf zu akzeptieren."
RELEASED = ("Der Kauf wurde freigegeben und ist nun zum Versand bereit. Wir informieren dich, sobald dein Artikel "
            "unterwegs ist.")


# ------------------------------------------------------------------ helpers
@pytest.fixture
def hooks(monkeypatch):
    """Clean stage hooks for one test; returns the (chat, stage) calls they received."""
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [])
    calls = HookCalls()

    async def record(conv, stage, state):
        calls.append((conv.id, stage))
        calls.states.append(state)

    paylivery.on_stage(record)
    return calls


class HookCalls(list):
    """(chat, stage) per hook call; .states: the state each call got."""

    def __init__(self):
        super().__init__()
        self.states: list[dict] = []


class _Sent(list):
    """The pushes' titles; their texts in .bodies."""
    def __init__(self):
        super().__init__()
        self.bodies = []


@pytest.fixture
def pushes(monkeypatch):
    async def fake_notify(kind, title, message, **kw):
        sent.append(title)
        sent.bodies.append(message)
        return True

    sent = _Sent()
    monkeypatch.setattr("app.services.notify.notify", fake_notify)
    return sent


async def _settle():
    while paylivery._hook_tasks:
        await asyncio.gather(*list(paylivery._hook_tasks), return_exceptions=True)


def _chat(cid="cb", role="buyer", state=None):
    with session() as db:
        db.add(Conversation(id=cid, ad_id="77", role=role, partner_name="Max", ad_title="Kinderbuch",
                            ad_context=AD_CTX, paylivery=state))
        db.commit()


def _notices(cid="cb", texts=((INTRO, 0), (WAIT, 1), (RELEASED, 151))) -> list[Message]:
    """The chat's notices, oldest first; minutes after a start three hours ago (19:54, 19:55, 22:25)."""
    t0 = utcnow() - timedelta(hours=3)
    out = []
    with session() as db:
        for i, (text, minutes) in enumerate(texts, 1):
            m = Message(conversation_id=cid, ext_id=f"n{i}", direction="unknown", text=text, kind="integration",
                        created_at=t0 + timedelta(minutes=minutes))
            db.add(m)
            db.commit()
            db.refresh(m)
            out.append(m)
    return out


def _state(cid="cb") -> dict:
    with session() as db:
        return dict(db.get(Conversation, cid).paylivery or {})


def _set_state(st, cid="cb"):
    with session() as db:
        row = db.get(Conversation, cid)
        row.paylivery = st
        db.add(row)
        db.commit()


def _conv(cid="cb"):
    with session() as db:
        return db.get(Conversation, cid)


def _at(m: Message, minutes: float = 0) -> str:
    return paylivery._iso(m.created_at + timedelta(minutes=minutes))


def _stuck(n1: Message) -> dict:
    """What the rules before RULES 2 left: the intro read as "delivered", the release notice's "unterwegs" refused."""
    return {"role": "buyer", "stage": "delivered", "updated_at": _at(n1),
            "history": [{"at": _at(n1), "stage": "delivered", "text": INTRO[:300]}], "applied": ["n1", "n2", "n3"]}


# ------------------------------------------------------------------ events, not explanations
@pytest.mark.parametrize("text", [
    INTRO,                                                                   # explains the process
    INTRO.replace("Anfrage. Wird", "Anfrage\nWird"),
    "Wir informieren dich, sobald dein Artikel unterwegs ist.",
    "Wir informieren dich sobald dein Artikel unterwegs ist und zugestellt wurde.",
    "Sobald der Käufer bezahlt hat, steht dein Versandlabel bereit.",
    "Wenn das Paket zugestellt wurde, kannst du den Erhalt bestätigen.",
    "Falls der Artikel beschädigt ankommt, wird der Kauf storniert.",
    "Das Geld wird ausgezahlt, sobald der Käufer den Erhalt bestätigt hat.",
    "Der Artikel wird versendet, sobald der Verkäufer ihn abgibt.",
    "Dein Paket wird voraussichtlich morgen zugestellt.",
    "Der Artikel kann jetzt versendet werden.",
    "Bitte bestätige den Erhalt, wenn das Paket zugestellt wurde.",
    "Wurde dein Paket zugestellt? Dann bestätige den Erhalt.",
    "Der Kauf wurde noch nicht freigegeben.",
    # a date or abbreviation ends no sentence: the condition and its "nicht" stay with the verb
    "Wird der Artikel nicht bis 16.10. versendet, wird der Kauf storniert.",
    "Wenn du den Artikel nicht bis 16.10. versendet hast, wird der Kauf automatisch storniert.",
    "Solltest du den Artikel nicht bis 16.10. versendet haben, wird der Kauf storniert.",
    "Falls du den Artikel nicht bis 15.10. verschickt hast, wird der Kauf storniert.",
    "Hast du den Artikel nicht bis 15.10. verschickt, wird der Kauf storniert.",
    "Bitte versende den Artikel bis 16.10. Wenn du ihn bis dahin nicht verschickt hast, wird der Kauf storniert.",
    "Sobald der Verkäufer bis 15.10. versendet hat, informieren wir dich.",
    "Wenn der Artikel z. B. beschädigt zugestellt wurde, kannst du ein Problem melden.",
    "Dein Paket wird am 15.10. zugestellt.",
    "Der Artikel wird am 14.10. versendet.",
    # inverted conditions, nachdem/bis, a condition after a title, and the main clause they govern
    "Die Auszahlung erfolgt, sobald der Käufer den Erhalt bestätigt.",
    "Wurde das Paket zugestellt, hast du 2 Tage Zeit, den Erhalt zu bestätigen oder ein Problem zu melden.",
    "Hat der Käufer bezahlt, kannst du das Versandlabel herunterladen.",
    "Versendet der Verkäufer nicht rechtzeitig, wird der Kauf storniert.",
    "Der Verkäufer erhält sein Geld, nachdem du den Erhalt bestätigt hast.",
    "Käuferschutz: Der Verkäufer erhält sein Geld erst, nachdem du den Erhalt bestätigt hast.",
    "Mit PayLivery ist dein Geld sicher: Wir halten die Zahlung zurück, bis du den Erhalt bestätigt hast.",
    "Dein Geld bleibt bei uns, bis der Artikel zugestellt wurde.",
    "Wir informieren dich, nachdem der Artikel zugestellt wurde.",
    "Hinweis: Sobald der Käufer bezahlt hat, steht dein Versandlabel bereit.",
    "willhaben PayLivery Wird dein Artikel gekauft, bezahlt der Käufer bequem online und du versendest mit "
    "Käuferschutz. Mehr zu PayLivery",
    "Das Geld wird dir nach der Zustellung überwiesen.",
    # advert, instructions, refunds, a payout held back, negations
    "willhaben PayLivery - Sicher verkaufen Der Käufer bezahlt online und du versendest den Artikel ganz einfach "
    "mit QR-Code. Mehr zu PayLivery",
    "Schon über 1 Million Artikel wurden mit PayLivery sicher verschickt!",
    "Sende einen Preisvorschlag oder kaufe sofort mit PayLivery.",
    "Verpacke den Artikel versandbereit und gib ihn bei der Post ab.",
    "Dein Kaufbetrag wurde wieder freigegeben.",
    "Der Kaufpreis wurde dir wieder freigegeben.",
    "Die Auszahlung wird bis zur Klärung zurückgehalten.",
    "Der Verkäufer hat den Artikel nicht rechtzeitig versendet.",
    "Dein Versandlabel ist noch nicht bereit.",
])
@pytest.mark.parametrize("role", ["seller", "buyer"])
def test_explanations_conditions_and_announcements_set_no_stage(text, role):
    assert paylivery.detect_stage(text, role) is None


@pytest.mark.parametrize("text, role, stage", [
    ("Dein Paket wurde zugestellt.", "buyer", "delivered"),
    ("Das Paket ist zugestellt worden.", "buyer", "delivered"),
    ("Der Artikel ist unterwegs.", "buyer", "shipped"),
    ("Der Artikel wurde versendet. Sendungsnummer: AB123456789", "buyer", "shipped"),
    ("Wir haben dem Käufer mitgeteilt, dass der Artikel unterwegs ist.", "seller", "shipped"),
    ("Das Paket wurde zugestellt. Bitte bestätige den Erhalt, sobald du es geprüft hast.", "buyer", "delivered"),
    ("Die Zahlung ist eingegangen und wird nach dem Erhalt ausgezahlt.", "seller", "paid"),
    ("Dein Paketschein, den du ausdrucken kannst, steht bereit", "seller", "label_ready"),
    ("Bitte bis 12.10. verschicken, Zahlung eingegangen: € 12,34", "seller", "paid"),
    (RELEASED, "buyer", "released"),
    ("Der Kauf wurde freigegeben und ist nun zum Versand bereit.", "seller", "released"),
    ("Der Verkauf wurde freigegeben.", "seller", "released"),
    ("Dein Artikel ist unterwegs und wird am 14.10. zugestellt.", "buyer", "shipped"),
    ("Dein Paket wird heute zugestellt.", "buyer", "shipped"),
    ("Der Kauf wurde storniert, nachdem der Verkäufer nicht reagiert hat.", "buyer", "cancelled"),
    ("Der Käufer hat ein Problem gemeldet, bevor er den Erhalt bestätigt hat.", "seller", "problem"),
    ("Nachdem der Käufer bezahlt hat, wurde dein Versandlabel erstellt.", "seller", "label_ready"),
    ("Max hat dir einen Preisvorschlag über € 40 gemacht.", "seller", "offer_received"),
    ("Deine Zahlung war erfolgreich.", "buyer", "paid"),
    ("Dein Paket ist angekommen.", "buyer", "delivered"),
    ("Dein Paket ist in der Abholstation angekommen.", "buyer", "delivered"),
    ("Der Käufer hat den Erhalt des Artikels bestätigt.", "seller", "received"),
])
def test_reported_events_still_set_their_stage(text, role, stage):
    assert paylivery.detect_stage(text, role) == stage


@pytest.mark.parametrize("text, role, stage", [
    # a headline, then an instruction after a colon or in brackets: the headline is the event
    ("Zahlung eingegangen: Bitte versende den Artikel bis 15.10.", "seller", "paid"),
    ("Der Käufer hat bezahlt: Bitte verschicke den Artikel innerhalb von 5 Werktagen.", "seller", "paid"),
    ("Bezahlt (bitte bis 15.10. versenden)", "seller", "paid"),
    ("Paket zugestellt: Bitte bestätige den Erhalt.", "buyer", "delivered"),
    ("Zugestellt: Du kannst jetzt den Erhalt bestätigen oder ein Problem melden.", "buyer", "delivered"),
    ("Dein Paket wurde zugestellt (bitte bestätige den Erhalt innerhalb von 48 Stunden).", "buyer", "delivered"),
    ("Artikel zugestellt Ist alles in Ordnung? Dann bestätige den Erhalt.", "buyer", "delivered"),
    ("Versendet: Du kannst die Sendung jetzt verfolgen.", "buyer", "shipped"),
    ("Paket unterwegs: Du kannst die Sendung hier verfolgen.", "buyer", "shipped"),
    ("Der Verkäufer hat den Artikel verschickt (du kannst die Sendung jederzeit verfolgen).", "buyer", "shipped"),
    ("Versandlabel bereit: Bitte drucke es aus und klebe es auf das Paket.", "seller", "label_ready"),
    ("Hier kannst du dein Versandetikett herunterladen: https://www.willhaben.at/iad/paketschein/x.pdf", "seller",
     "label_ready"),                                                         # a label link counts in any mood
    ("Bitte drucke das Versandlabel aus: https://www.willhaben.at/iad/paketschein/x.pdf", "seller", "label_ready"),
    ("Problem gemeldet: Bitte kontaktiere den Käufer.", "seller", "problem"),
    ("Bitte beachte: Der Käufer hat ein Problem gemeldet.", "seller", "problem"),
    ("Storniert: Bitte versende den Artikel nicht.", "seller", "cancelled"),
    ("Bitte beachte: Der Kauf wurde storniert.", "buyer", "cancelled"),
    ("Neuer Preisvorschlag: Max möchte den Artikel für € 40 kaufen.", "seller", "offer_received"),
    ("Neue Kaufanfrage: Max möchte deinen Artikel um € 40 kaufen.", "seller", "offer_received"),
    ("Max hat deinen Artikel gekauft: Bitte akzeptiere den Kauf innerhalb von 24 Stunden.", "seller", "offer_accepted"),
    ("Preisvorschlag angenommen: Bitte bezahle bis 13.10.", "buyer", "offer_accepted"),
    ("Kauf freigegeben: Der Verkäufer muss den Artikel jetzt innerhalb von 5 Werktagen versenden.", "buyer",
     "released"),
    # willhaben's own decisions in the present tense
    ("Die Bestellung wird storniert, da der Verkäufer den Artikel nicht rechtzeitig versendet hat.", "buyer",
     "cancelled"),
    ("Leider hat der Verkäufer deinen Kauf nicht akzeptiert. Der Kauf wird storniert und du bekommst dein Geld "
     "zurück.", "buyer", "cancelled"),
    ("Der Verkäufer hat nicht rechtzeitig reagiert. Dein Kauf wird automatisch storniert.", "buyer", "cancelled"),
    ("Der Kauf wird storniert, da die Zahlung nicht bestätigt werden konnte.", "buyer", "cancelled"),
    ("Der Kauf wird storniert, da die Zahlung nicht bestätigt werden konnte.", "seller", "cancelled"),
    ("Der Verkäufer hat den Kauf abgelehnt. Dein Geld wird zurücküberwiesen.", "buyer", "cancelled"),
    ("Der Betrag von € 25,00 wurde dir zurückerstattet.", "buyer", "cancelled"),
    ("Die Auszahlung wird veranlasst.", "seller", "paid_out"),
    ("Dein Geld ist auf dem Weg! Der Betrag von € 25,00 wird in 1-2 Werktagen auf dein Konto überwiesen.", "seller",
     "paid_out"),
    ("Der Verkaufserlös wurde freigegeben und wird in 2-3 Werktagen ausgezahlt.", "seller", "paid_out"),
    ("Die Auszahlung wird aufgrund einer Reklamation zurückgehalten.", "seller", "problem"),
    ("Deine Reklamation wird geprüft.", "buyer", "problem"),
    # a purchase (not an offer) accepted is released; the buyer's purchase waiting for the seller is paid
    ("Der Verkäufer hat deinen Kauf akzeptiert. Wir informieren dich, sobald dein Artikel unterwegs ist.", "buyer",
     "released"),
    ("Der Verkäufer hat den Kauf angenommen.", "buyer", "released"),
    ("Du hast den Kauf akzeptiert. Bitte versende den Artikel innerhalb von 5 Werktagen.", "seller", "released"),
    ("Dein Kauf über € 25,00 wurde freigegeben.", "buyer", "released"),
    ("Der Kauf ist bezahlt und freigegeben.", "buyer", "released"),
    ("Deine Bestellung wurde freigegeben und ist versandbereit.", "buyer", "released"),
    ("Dein Preisvorschlag für den Kauf wurde angenommen.", "buyer", "offer_accepted"),
    (WAIT, "buyer", "paid"),
    ("Danke für deinen Kauf! " + WAIT, "buyer", "paid"),
    (WAIT, "seller", None),
])
def test_headlines_decisions_and_purchases(text, role, stage):
    assert paylivery.detect_stage(text, role) == stage


def test_a_conditional_payment_marks_no_shipment_paid():
    assert not paylivery.is_paid("Sobald der Käufer bezahlt hat, steht dein Versandlabel bereit.")
    assert not paylivery.is_paid("Hat der Käufer bezahlt, kannst du das Versandlabel herunterladen.")
    assert not paylivery.is_paid("So funktioniert PayLivery: Der Käufer bezahlt sicher online, du verschickst den "
                                 "Artikel und erhältst dein Geld nach der Zustellung.")
    assert paylivery.is_paid("Der Käufer hat bezahlt. Bitte verschicke den Artikel, sobald du kannst.")
    assert paylivery.is_paid("Zahlung eingegangen: Bitte versende den Artikel bis 15.10.")
    assert paylivery.is_paid("Bezahlt (bitte bis 15.10. versenden)")


# ------------------------------------------------------------------ the released stage
def test_released_sits_between_paid_and_the_label():
    assert paylivery._advances("paid", "released") and paylivery._advances("released", "label_ready")
    assert paylivery._advances("released", "shipped")
    assert not paylivery._advances("released", "paid") and not paylivery._advances("label_ready", "released")
    assert not paylivery._advances("released", "offer_sent")              # no new offers after the payment


def _card(role, stage):
    conv = SimpleNamespace(id="c9", role=role, ad_context=AD_CTX, partner_name="Max", ad_title="Kinderbuch",
                           paylivery={"role": role, "stage": stage, "history": []})
    return paylivery.card(conv)


def test_released_card_buyer_waits_for_the_parcel_and_seller_ships():
    buyer = _card("buyer", "released")
    assert buyer["stage_label"] == "Released for shipping"
    assert buyer["next"] == "Paid and released. Waiting for the seller to ship it."
    assert [a["name"] for a in buyer["actions"]] == ["problem"]           # no receipt before the parcel is there
    seller = _card("seller", "released")
    assert seller["next"].startswith("Released: ship it now.")
    assert [a["name"] for a in seller["actions"]] == ["label", "mark_shipped"]


async def test_the_three_notices_of_a_buy_now_purchase_give_released(hooks, pushes):
    _chat()
    _notices()
    await paylivery.scan("cb")
    await _settle()
    st = _state()
    assert st["stage"] == "released" and st["rules"] == paylivery.RULES
    assert [(h["stage"], h["notice"]) for h in st["history"]] == [("paid", "n2"), ("released", "n3")]   # intro: nothing
    assert st["applied"] == ["n1", "n2", "n3"]
    assert hooks == [("cb", "paid"), ("cb", "released")]
    assert pushes == ["PayLivery: Paid", "PayLivery: Released for shipping"]
    assert all(b.startswith("willhaben · Kinderbuch\n") for b in pushes.bodies)     # which ad, on which site
    card = paylivery.card(_conv())
    assert card["stage"] == "released" and [a["name"] for a in card["actions"]] == ["problem"]


# ------------------------------------------------------------------ recheck of stuck chats
async def test_a_chat_stuck_on_delivered_is_rechecked_on_the_next_scan(hooks, pushes):
    _chat()
    n1, _, _ = _notices()
    _set_state(_stuck(n1))
    await paylivery.scan("cb")
    await _settle()
    st = _state()
    assert st["stage"] == "released" and st["rules"] == paylivery.RULES
    assert [(h["stage"], h.get("notice")) for h in st["history"]] == [("paid", "n2"), ("released", "n3")]
    assert st["applied"] == ["n1", "n2", "n3"]
    assert hooks == [("cb", "released")]          # a stage this chat never had: a purchase order may catch up
    assert pushes == []                           # a correction, no news: no push
    await paylivery.scan("cb")                    # rechecked once: nothing again
    await _settle()
    assert hooks == [("cb", "released")] and _state() == st



def test_recheck_reads_notices_stored_before_they_got_their_kind():
    """Rows stored before notices got their kind carry direction "unknown" and no kind (154 on a live install); the
    recheck replays them when they were applied, but an "unknown" row that was never applied stays out."""
    _chat()
    n1, n2, n3 = _notices()
    with session() as db:
        for m in (n1, n2, n3):
            row = db.get(Message, m.id)
            row.kind = None
            db.add(row)
        db.add(Message(conversation_id="cb", ext_id="x1", direction="unknown", kind=None,
                       text="Paket wurde zugestellt, danke!", created_at=utcnow()))        # the other person, from the list
        db.commit()
    _set_state(_stuck(n1))
    assert paylivery.recheck("cb")["stage"] == "released"
    assert [h.get("notice") for h in _state()["history"]] == ["n2", "n3"]

async def test_recheck_keeps_own_actions_amounts_and_notes(hooks, pushes):
    """Bought in the app (paid, 12 €, a bank wait before it), then the intro notice moved the chat to "delivered"."""
    _chat()
    n1, _, _ = _notices()
    bank = {"at": _at(n1, 0.3), "stage": None, "text": "Waiting for the approval in the bank app"}
    buy = {"at": _at(n1, 0.5), "stage": "paid", "text": "buy: bezahlt"}
    _set_state({"role": "buyer", "stage": "delivered", "amount": 12.0, "applied": ["n1", "n2", "n3"],
                "history": [bank, buy, {"at": _at(n1), "stage": "delivered", "text": INTRO[:300]}]})
    await paylivery.scan("cb")
    await _settle()
    st = _state()
    assert st["stage"] == "released" and st["amount"] == 12.0
    assert [h["text"] for h in st["history"]] == [bank["text"], buy["text"], WAIT, RELEASED]
    assert st["history"][1] == {**buy, "action": "buy", "ok": True, "amount": 12.0}
    assert hooks == [("cb", "released")]          # "paid" was handled already: not again
    assert pushes == []


async def test_recheck_keeps_a_local_mark_shipped_and_tells_the_hooks_it_was_seen(hooks, pushes):
    """Seller: paid notice, parcel marked shipped here (German entry from before the tags), then an explanation of
    the payout that the older rules read as "paid_out"."""
    _chat(cid="cs", role="seller")
    p1, x2 = _notices(cid="cs", texts=(("Zahlung eingegangen: € 12,34", 0),
                                       ("Das Geld wird ausgezahlt, sobald der Käufer den Erhalt bestätigt hat.", 90)))
    mark = {"at": _at(p1, 30), "stage": "shipped", "text": "In dieser App als verschickt markiert"}
    history = [{"at": _at(p1), "stage": "paid", "text": p1.text}, mark,
               {"at": _at(x2), "stage": "paid_out", "text": x2.text}]
    _set_state({"role": "seller", "stage": "paid_out", "amount": 12.34, "applied": ["n1", "n2"], "history": history},
               cid="cs")
    assert paylivery.recheck("cs")["stage"] == "shipped"
    await _settle()
    st = _state("cs")
    assert st["amount"] == 12.34 and st["rules"] == paylivery.RULES
    assert st["history"] == [{"at": _at(p1), "stage": "paid", "text": p1.text, "notice": "n1"},
                             {**mark, "action": "mark_shipped", "ok": True}]
    assert pushes == []
    # "shipped" was handled when it was marked: the hooks hear of the correction, marked as seen
    assert hooks == [("cs", "shipped")] and hooks.states[0]["seen"] is True
    assert hooks.states[0]["corrected_from"] == "paid_out"


# ------------------------------------------------------------------ notices on the state
def _apply(cid, texts, role="buyer") -> dict:
    """The notices applied one by one, oldest first; returns the chat's state."""
    _chat(cid=cid, role=role)
    conv = _conv(cid)
    for m in _notices(cid=cid, texts=texts):
        paylivery.apply_notice(conv, m)
    return _state(cid)


def test_a_refund_after_the_cancellation_keeps_it_cancelled():
    st = _apply("cr", (("Der Kauf wurde storniert. Du bekommst dein Geld zurück.", 0),
                       ("Dein Kaufbetrag wurde wieder freigegeben.", 5)))
    assert st["stage"] == "cancelled" and len(st["history"]) == 1


def test_label_link_and_tracking_are_kept_from_any_notice():
    """A label link in a modal sentence still gives label_ready; a tracking number in a notice that sets no stage
    (or none that advances) is still kept."""
    url = "https://www.willhaben.at/iad/paketschein/x.pdf"
    st = _apply("cl", (("Zahlung eingegangen: € 12,34", 0),
                       (f"Hier kannst du dein Versandetikett herunterladen: {url}", 5),
                       ("Du hast den Artikel als versendet markiert.", 60),
                       ("Du kannst die Sendung verfolgen. Sendungsnummer: AB123456789AT", 61)), role="seller")
    assert st["stage"] == "shipped" and st["label_url"] == url and st["tracking"] == "AB123456789AT"
    assert st["amount"] == 12.34
    assert [h["stage"] for h in st["history"]] == ["paid", "label_ready", "shipped"]


# ------------------------------------------------------------------ recheck: what it leaves alone, what it corrects
def test_recheck_leaves_a_removed_chat_alone():
    """chats.remove deletes the notices but keeps the state: the history can no longer be replayed, so nothing of it
    is lost (the re-synced notices keep their ids, already applied, and are never applied again)."""
    from app.services import chats

    _chat(cid="cs", role="seller")
    p1, l2 = _notices(cid="cs", texts=(("Zahlung eingegangen: € 12,34. Bitte bis 20.10. verschicken.", 0),
                                       ("Dein Paketschein steht bereit: https://www.willhaben.at/iad/paketschein/x.pdf",
                                        5)))
    old = {"role": "seller", "stage": "shipped", "amount": 12.34, "deadline": "2026-10-20T23:59:00+00:00",
           "label_url": "https://www.willhaben.at/iad/paketschein/x.pdf", "applied": ["n1", "n2"],
           "history": [{"at": _at(p1), "stage": "paid", "text": p1.text},
                       {"at": _at(l2), "stage": "label_ready", "text": l2.text},
                       {"at": _at(p1, 30), "stage": "shipped", "text": "Marked as shipped in this app"}]}
    _set_state(old, cid="cs")
    chats.remove("cs")
    assert paylivery.recheck("cs") is None
    assert _state("cs") == {**old, "rules": paylivery.RULES}


def test_recheck_keeps_a_label_stage_and_its_link():
    """The label notice is a modal sentence with the link: still label_ready, the link stays."""
    url = "https://www.willhaben.at/iad/paketschein/x.pdf"
    _chat(cid="cs", role="seller")
    p1, l2 = _notices(cid="cs", texts=(("Zahlung eingegangen: € 12,34", 0),
                                       (f"Hier kannst du dein Versandetikett herunterladen: {url}", 5)))
    old = {"role": "seller", "stage": "label_ready", "amount": 12.34, "label_url": url, "applied": ["n1", "n2"],
           "history": [{"at": _at(p1), "stage": "paid", "text": p1.text},
                       {"at": _at(l2), "stage": "label_ready", "text": l2.text}]}
    _set_state(old, cid="cs")
    assert paylivery.recheck("cs") is None
    assert _state("cs") == {**old, "rules": paylivery.RULES}


def test_recheck_of_a_purchase_waiting_for_the_seller_offers_no_second_purchase():
    """Bought on willhaben, the seller has not accepted yet: intro and "24 Stunden Zeit" only. The stuck "delivered"
    becomes paid, and the card offers no offer or buy for an item already bought."""
    _chat()
    n1, _ = _notices(texts=((INTRO, 0), (WAIT, 1)))
    _set_state({"role": "buyer", "stage": "delivered", "applied": ["n1", "n2"],
                "history": [{"at": _at(n1), "stage": "delivered", "text": INTRO[:300]}]})
    assert paylivery.recheck("cb")["stage"] == "paid"
    assert [a["name"] for a in paylivery.card(_conv())["actions"]] == ["problem"]


async def test_a_lowered_stage_reaches_the_hooks_as_a_correction(hooks, pushes):
    """Bought in the app (paid), then the release notice read as "shipped" by the older rules (a purchase order went
    to shipped with it). The recheck gives released and tells the hooks it corrected "shipped"."""
    _chat()
    w1, r2 = _notices(texts=((WAIT, 1), (RELEASED, 151)))
    buy = {"at": _at(w1, -0.5), "stage": "paid", "text": "buy: bezahlt", "action": "buy", "ok": True, "amount": 12.0}
    _set_state({"role": "buyer", "stage": "shipped", "amount": 12.0, "applied": ["n1", "n2"],
                "history": [buy, {"at": _at(r2), "stage": "shipped", "text": RELEASED}]})
    assert paylivery.recheck("cb")["stage"] == "released"
    await _settle()
    assert hooks == [("cb", "released")] and pushes == []
    assert hooks.states[0]["corrected_from"] == "shipped" and hooks.states[0]["seen"] is False
    assert "corrected_from" not in _state()                     # only the hooks are told


async def test_a_lowered_stage_seen_before_is_marked_seen(hooks, pushes):
    """Paid in the app, then "Wir informieren dich, sobald dein Artikel unterwegs ist" read as shipped: back to paid,
    a stage the hooks already handled."""
    _chat()
    [x1] = _notices(texts=(("Wir informieren dich, sobald dein Artikel unterwegs ist.", 5),))
    buy = {"at": _at(x1, -1), "stage": "paid", "text": "buy: bezahlt", "action": "buy", "ok": True, "amount": 12.0}
    _set_state({"role": "buyer", "stage": "shipped", "amount": 12.0, "applied": ["n1"],
                "history": [buy, {"at": _at(x1), "stage": "shipped", "text": x1.text}]})
    assert paylivery.recheck("cb")["stage"] == "paid"
    await _settle()
    assert hooks == [("cb", "paid")]
    assert hooks.states[0]["corrected_from"] == "shipped" and hooks.states[0]["seen"] is True


def test_startup_rechecks_stuck_chats_and_only_marks_the_others():
    _chat()
    n1, _, _ = _notices()
    _set_state(_stuck(n1))
    unexplained = {"role": "seller", "stage": "paid", "history": [], "applied": []}    # no notice or action set it
    _chat(cid="cs", role="seller", state=unexplained)
    _chat(cid="ok", role="seller")
    [p1] = _notices(cid="ok", texts=(("Zahlung eingegangen: € 12,34", 5),))
    right = {"role": "seller", "stage": "paid", "amount": 12.34, "applied": ["n1"],
             "history": [{"at": _at(p1), "stage": "paid", "text": p1.text}]}
    _set_state(right, cid="ok")
    assert paylivery.recheck_all() == 1
    assert _state()["stage"] == "released"
    assert _state("cs") == {**unexplained, "rules": paylivery.RULES}          # kept as it is
    assert _state("ok") == {**right, "rules": paylivery.RULES}                # right already: history untouched
    assert paylivery.recheck_all() == 0


def test_chat_page_after_startup_shows_released_without_receipt():
    """The reported chat in German: the stuck "Zugestellt" with "Erhalt bestätigen" is gone after a restart."""
    from fastapi.testclient import TestClient

    from app.main import app

    _chat()
    n1, _, _ = _notices()
    _set_state(_stuck(n1))
    with TestClient(app) as c:
        page = c.get("/chats/cb", headers={"accept-language": "de"}).text
    card = page[page.index('id="paylivery"'):]
    card = card[:card.index("</section>")]
    assert "Zum Versand freigegeben" in card and "Warten, bis der Verkäufer das Paket verschickt" in card
    assert "Erhalt bestätigen" not in card and "Zugestellt" not in card
    assert _state()["stage"] == "released"



@pytest.mark.parametrize("text,role,stage", [
    ("Du hast eine Anfrage über € 30 erhalten. Wenn der Preis für dich passt, nimm die Anfrage gleich an!", "seller",
     "offer_received"),
    ("Du hast ein Gegenangebot über € 45 gesendet.", "seller", "counter_sent"),
    ("Du hast einen Preisvorschlag über € 45 gesendet.", "seller", "counter_sent"),
    ("Gegenangebot erhalten: € 45", "buyer", "counter_received"),
    ("Max hat dir ein Gegenangebot über € 45 gemacht.", "buyer", "counter_received"),
    ("Max hat dir einen Preisvorschlag über € 45 gemacht.", "buyer", "counter_received"),
    ("Du hast einen Preisvorschlag über € 30 gesendet.", "buyer", "offer_sent"),
    ("Dein Gegenangebot wurde angenommen.", "seller", "offer_accepted"),
    ("Das Gegenangebot wurde abgelehnt.", "buyer", "offer_declined"),
])
def test_counter_offers(text, role, stage):
    """The seller may answer a buyer's offer with a counter offer; the buyer accepts or declines it."""
    assert paylivery.detect_stage(text, role) == stage
