"""The price steps of a Vinted chat (services/vinted_offers.py): the offer state read from the chat, the steps that
fit it, and the calls of Vinted's own web app (made-up chat data)."""
from fastapi.testclient import TestClient

from app.db import Conversation, session
from app.platforms import vinted_inbox as vi
from app.services import vinted_offers as vo

PARTNER = "111"


def _chat(role="seller", status=10, kind="offer_request_message"):
    data = {"data": {"transaction_id": "777", "current_user_side": role, "inquiry_value": {"amount": "40.00"}},
            "messages": [{"created_at": "2026-10-29T10:00:00Z", "sender_id": PARTNER, "message_type": "legacy_offer_request",
                          "data": {"entity_type": kind, "id": 5, "entity": {
                              "offer_request_id": 9001, "price": {"amount": "30.0"}, "original_price": {"amount": "40.0"},
                              "status": status, "status_title": "Ausstehend", "user_id": int(PARTNER),
                              "transaction_id": 777, "title": "kaeufer_42 hat dir ein neues Angebot gemacht:"}}}]}
    return data


def test_the_offer_state_of_a_chat():
    st = vi.offer_state(_chat(), PARTNER)
    assert st["transaction_id"] == "777" and st["price"] == 40.0
    assert st["offer"] == {"kind": "request", "id": "9001", "price": 30.0, "original": 40.0, "status": 10,
                           "status_title": "Ausstehend", "at": "2026-10-29T10:00:00Z", "from_me": False}


def _conv(role="seller", status=10):
    with session() as db:
        c = Conversation(id=f"v-{role}-{status}", platform="vinted", role=role, partner_name="kaeufer_42",
                         vinted_offer=vi.offer_state(_chat(role, status), PARTNER))
        db.add(c)
        db.commit()
        db.refresh(c)
        return c


def test_the_steps_that_fit():
    c = _conv()
    assert [a["name"] for a in vo.card(c)["actions"]] == ["accept", "decline", "counter", "reserve"]
    assert vo.plan(c, "accept") == ("PUT", "https://www.vinted.at/api/v2/transactions/777/offer_requests/9001/accept", None)
    assert vo.plan(c, "decline")[1].endswith("/offer_requests/9001/reject")
    assert vo.plan(c, "counter", "35,5") == ("POST", "https://www.vinted.at/api/v2/transactions/777/offers",
                                             {"offer": {"price": "35.50", "currency": "EUR"}})
    assert vo.plan(c, "reserve") == ("PUT", "https://www.vinted.at/api/v2/transactions/777/reservation", {"reserved": True})
    answered = _conv(status=30)                                          # declined already: nothing to answer
    assert [a["name"] for a in vo.card(answered)["actions"]] == ["offer", "reserve"]
    try:
        vo.plan(answered, "accept")
        raise AssertionError("no error")
    except vo.OfferError as e:
        assert "no open offer" in str(e)
    buyer = _conv(role="buyer")
    assert [a["name"] for a in vo.card(buyer)["actions"]] == ["offer"]
    assert vo.plan(buyer, "offer", "25")[2] == {"offer_request": {"price": "25.00", "currency": "EUR"}}
    try:
        vo.plan(c, "counter", "abc")
        raise AssertionError("no error")
    except vo.OfferError as e:
        assert "amount" in str(e)


async def test_a_step_runs_on_vinted_and_reads_the_chat_again(monkeypatch):
    c = _conv()
    calls, synced = [], []

    class Page:
        def __init__(self, url=None):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, method, url, body=None):
            calls.append((self.url, method, url, body))
            return 200, {}, "{}"

    async def sync_one(cid, push=True):
        synced.append(cid)
        return 1
    from app.platforms import vinted_listing as vl
    monkeypatch.setattr(vl, "Upload", Page)
    monkeypatch.setattr(vi, "sync_one", sync_one)
    assert await vo.run(c.id, "accept") == "Offer accepted on Vinted"
    assert calls[0][0].endswith(f"/inbox/{c.id}") and calls[0][1] == "PUT" and synced == [c.id]


def test_the_chat_page_shows_the_price_steps(monkeypatch):
    c = _conv()
    from app.main import app
    with TestClient(app) as client:
        page = client.get(f"/chats/{c.id}").text
    assert 'id="vinted-offer"' in page and f'action="/chats/{c.id}/vinted-offer/accept"' in page
    assert "Accept 30,00 €" in page and "Offer waiting for you" in page
