"""Sold / reserved workflow: willhaben calls (fake session), notice drafts, auto replies off, web flow."""
import json

import pytest
from sqlmodel import select

from app.db import Conversation, Draft, Message, MyAd, session, set_setting, utcnow
from app.services import chats, sold
from app.willhaben import sales
from app.willhaben.account import parse_my_adverts
from app.willhaben.session import ApiResponse, NotLoggedIn
from tests.conftest import finish_jobs, wait_jobs

AD = "1000000001"


def _links(*ids):
    paths = {"setAdSold": f"/bap/sold/{AD}", "markAdReserved": f"/bap/updatesalesinfo/{AD}/5",
             "removeMarkAdReserved": f"/bap/updatesalesinfo/{AD}/0", "2": f"/bap/deactivate/{AD}"}
    return {"contextLink": [{"id": i, "serviceName": "iad", "relativePath": paths[i]} for i in ids]}


def _page(links, reasons_query=None):
    ad = {"id": AD, "description": "Fahrrad", "advertStatus": {"id": "active", "description": "Aktiv"},
          "advertEditStatusActionsList": {"statusId": 50, "contextLinkList": links},
          "deleteReasonQueryParameter": reasons_query, "attributes": {"attribute": []}}
    return {"page": "/iad/myprofile/myadverts", "props": {"pageProps": {"initialUserAdsList": {
        "verticalList": [{"verticalId": 5, "advertDetails": [ad]}]}}}}


REASONS = {"header": "Wo hast du verkauft?", "soldReasons": [
    {"soldReasonId": 1, "description": "Über willhaben", "playAnimation": True},
    {"soldReasonId": 2, "description": "Woanders", "playAnimation": False}]}


class FakeSession:
    """Logged-in browser stand-in: serves the my-adverts page, records every other call (all go through api())."""

    def __init__(self, page, reasons=None, fail=False, write_text="", reasons_status=200):
        self.page, self.reasons, self.fail, self.calls = page, reasons, fail, []
        self.write_text, self.reasons_status, self.logged_in = write_text, reasons_status, True

    async def api(self, method, path, body=None, accept="application/json"):
        if (method, path) == ("GET", "/iad/myprofile/myadverts"):
            html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(self.page)}</script>'
            return ApiResponse(status=200, text=html, url="https://www.willhaben.at" + path)
        self.calls.append((method, path, body))
        url = "https://www.willhaben.at" + path
        if method == "GET" and path.startswith("/webapi/iad/soldreasons"):
            return ApiResponse(status=self.reasons_status, text=json.dumps(self.reasons) if self.reasons else "", url=url)
        if self.fail:
            return ApiResponse(status=409, text="conflict", url=url)
        return ApiResponse(status=200, text=self.write_text, url=url)

    async def refresh_sso(self):
        return False

    async def api_json(self, method, path, body=None):
        raise AssertionError("sales.py decides on the HTTP status itself, not via api_json")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


# ------------------------------------------------------------------ willhaben layer
def test_parse_keeps_action_links():
    ad = parse_my_adverts(_page(_links("setAdSold", "markAdReserved"), "adId=1"))[0]
    assert set(ad.actions) == {"setAdSold", "markAdReserved"} and ad.reasons_query
    assert sales.action_path(ad, sales.SOLD) == f"/webapi/iad/bap/sold/{AD}"
    assert sales.offers(ad) == {"sold": True, "reserve": True, "unreserve": False}
    with pytest.raises(sales.NotOffered):
        sales.action_path(ad, sales.UNRESERVE)


async def test_mark_sold_asks_for_reason_before_writing():
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS)
    with pytest.raises(sales.ReasonRequired) as e:
        await sales.mark_sold(s, AD)
    assert [(r.id, r.label) for r in e.value.reasons] == [("1", "Über willhaben"), ("2", "Woanders")]
    assert not s.writes
    with pytest.raises(sales.ReasonRequired):
        await sales.mark_sold(s, AD, reason_id="99")        # not one of willhaben's reasons
    assert not s.writes

    await sales.mark_sold(s, AD, reason_id="1")
    # same order and bodies as the web app: reason (as string) first, then the sold link with PUT and no body
    assert s.writes == [("POST", f"/webapi/iad/soldreasons?adId={AD}", {"soldReasonId": "1"}),
                        ("PUT", f"/webapi/iad/bap/sold/{AD}", None)]


async def test_mark_sold_without_reason_query_and_not_offered():
    s = FakeSession(_page(_links("setAdSold")))
    await sales.mark_sold(s, AD)
    assert s.calls == [("PUT", f"/webapi/iad/bap/sold/{AD}", None)]

    s = FakeSession(_page(_links("2")))                     # e.g. expired ad: willhaben offers no "sold"
    with pytest.raises(sales.NotOffered):
        await sales.mark_sold(s, AD)
    assert not s.calls

    s = FakeSession({"page": "/iad/myprofile/myadverts", "props": {"pageProps": {"initialUserAdsList": {"verticalList": []}}}})
    with pytest.raises(sales.NotOffered, match="not in your willhaben list"):
        await sales.mark_sold(s, AD)


async def test_mark_reserved_and_undo():
    s = FakeSession(_page(_links("markAdReserved", "removeMarkAdReserved")))
    await sales.mark_reserved(s, AD)
    await sales.mark_reserved(s, AD, on=False)
    assert s.calls == [("PUT", f"/webapi/iad/bap/updatesalesinfo/{AD}/5", None),
                       ("PUT", f"/webapi/iad/bap/updatesalesinfo/{AD}/0", None)]


async def test_refused_change_and_login_errors():
    s = FakeSession(_page(_links("markAdReserved")), fail=True)
    with pytest.raises(sales.SaleFailed, match="409"):
        await sales.mark_reserved(s, AD)

    class Expired(FakeSession):
        async def api(self, method, path, body=None, accept="application/json"):
            r = await super().api(method, path, body, accept)
            return r if method == "GET" else ApiResponse(status=401, text="", url=r.url)
    s = Expired(_page(_links("markAdReserved")))
    with pytest.raises(NotLoggedIn):
        await sales.mark_reserved(s, AD)
    assert s.logged_in is False


async def test_write_with_non_json_2xx_body_counts_as_done():
    # willhaben's answer bodies are unknown: a 2xx with plain text / HTML must not be reported as "not changed"
    s = FakeSession(_page(_links("setAdSold")), write_text="OK")
    await sales.mark_sold(s, AD)
    assert s.writes == [("PUT", f"/webapi/iad/bap/sold/{AD}", None)]
    s = FakeSession(_page(_links("markAdReserved")), write_text="<!DOCTYPE html><html></html>")
    await sales.mark_reserved(s, AD)


async def test_sold_reasons_http_error_is_sale_failed(ad_with_chats, monkeypatch):
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS, reasons_status=500)
    with pytest.raises(sales.SaleFailed, match="500"):
        await sales.mark_sold(s, AD)
    assert not s.writes

    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(sold, "get_session", lambda: s)
    with TestClient(app) as c:
        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer"})
        assert r.status_code == 200 and not s.writes
        wait_jobs()                                                      # the step runs as a background job
        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
    assert "Not changed on willhaben" in page and not s.writes


def test_action_path_rejects_odd_links():
    for link in ({"id": "setAdSold", "serviceName": "iad", "relativePath": "/bap/../x"},
                 {"id": "setAdSold", "serviceName": "iad/x", "relativePath": "/bap/sold/1"}):
        ad = parse_my_adverts(_page({"contextLink": [link]}))[0]
        with pytest.raises(sales.NotOffered):
            sales.action_path(ad, sales.SOLD)


# ------------------------------------------------------------------ service
class _Account:
    def __init__(self):
        self.sent = []

    async def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "m1"

    async def mark_read(self, conversation_id, msgs=None):
        return 0


@pytest.fixture
def ad_with_chats(monkeypatch):
    account = _Account()
    monkeypatch.setattr(chats, "_account", lambda: account)
    with session() as db:
        db.add(MyAd(ad_id=AD, heading="Fahrrad", status="active", auto_reply=True, auto_reply_instructions="Preis fix"))
        for cid, name in (("c-buyer", "Max Muster"), ("c-2", "Erika Muster"), ("c-3", None)):
            db.add(Conversation(id=cid, ad_id=AD, ad_title="Fahrrad", partner_name=name, role="seller"))
            db.add(Message(conversation_id=cid, ext_id=f"m-{cid}", direction="in", text="Ist das Rad noch da?",
                           created_at=utcnow()))
        db.add(Conversation(id="c-other-ad", ad_id="1000000002", partner_name="Max Muster", role="seller"))
        db.commit()
    set_setting("send_verified", "1")
    return account


async def test_notices_for_other_buyers_only_after_send(ad_with_chats):
    account = ad_with_chats
    assert [o.conv.id for o in sold.others(AD, "c-buyer")] == ["c-2", "c-3"]
    made, errors = await sold.prepare_notices(AD, "sold", ["c-2", "c-3", "c-buyer", "c-other-ad"], buyer="c-buyer")
    assert (made, errors) == (2, []) and not account.sent
    drafts = {o.conv.id: o.draft for o in sold.others(AD, "c-buyer")}
    assert drafts["c-2"].text.startswith("Hallo Erika Muster,") and "schon verkauft" in drafts["c-2"].text
    assert drafts["c-3"].text.startswith("Hallo,") and drafts["c-2"].intent == "sold_notice"

    # preparing again replaces the unsent draft instead of piling up
    await sold.prepare_notices(AD, "sold", ["c-2"], buyer="c-buyer")
    with session() as db:
        open_ = list(db.exec(select(Draft).where(Draft.conversation_id == "c-2", Draft.status == "draft")))
    assert len(open_) == 1

    assert sold.send_notices(AD, [(open_[0].id, "Leider schon verkauft, sorry!")]) == 1
    assert sold.others(AD, "c-buyer")[0].sending is not None                    # queued: shown as "sending"
    await finish_jobs()
    assert account.sent == [("c-2", "Leider schon verkauft, sorry!")]
    others = {o.conv.id: o for o in sold.others(AD, "c-buyer")}
    assert others["c-2"].informed and others["c-3"].draft is not None and not others["c-3"].informed

    assert sold.discard_notices(AD, [others["c-3"].draft.id]) == 1
    assert sold.others(AD, "c-buyer")[1].draft is None


async def test_send_notices_ignores_foreign_drafts(ad_with_chats):
    account = ad_with_chats
    with session() as db:
        normal = Draft(conversation_id="c-2", text="normaler Entwurf")
        foreign = Draft(conversation_id="c-other-ad", text="x", intent="sold_notice")
        db.add(normal)
        db.add(foreign)
        db.commit()
        ids = [normal.id, foreign.id]
    assert sold.send_notices(AD, [(i, "x") for i in ids]) == 0
    assert not account.sent


async def test_finish_switches_auto_replies_off_and_blocks_sold(ad_with_chats):
    conv = Conversation(id="c-2", ad_id=AD, role="seller")
    assert chats._auto_ad(conv) is not None
    sold.finish(AD, "reserved", "c-buyer")
    ad = sold.ad_row(AD)
    assert ad.auto_reply is False and ad.auto_reply_instructions == "Preis fix" and ad.sale_status == "reserved"

    with session() as db:   # even if someone switches auto replies on again: a sold ad gets none
        row = db.get(MyAd, AD)
        row.auto_reply, row.sale_status = True, "sold"
        db.add(row)
        db.commit()
    assert chats._auto_ad(conv) is None
    assert await chats.process_pending_auto(AD) == 0 and not ad_with_chats.sent
    assert sold.state_for(Conversation(id="c-buyer", ad_id=AD))["kind"] == "sold"

    sold.reset(AD)
    assert sold.ad_row(AD).sale_status is None and chats._auto_ad(conv) is not None


async def test_on_willhaben_updates_status(ad_with_chats, monkeypatch):
    s = FakeSession(_page(_links("setAdSold", "markAdReserved", "removeMarkAdReserved")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    await sold.on_willhaben(AD, "reserved")
    assert sold.ad_row(AD).status == "reserved"
    await sold.on_willhaben(AD, "reserved", undo=True)
    assert sold.ad_row(AD).status == "active"
    with pytest.raises(sales.NotOffered):
        await sold.on_willhaben(AD, "sold", undo=True)
    await sold.on_willhaben(AD, "sold")
    assert sold.ad_row(AD).status == "sold" and s.writes[-1] == ("PUT", f"/webapi/iad/bap/sold/{AD}", None)


# ------------------------------------------------------------------ web flow
def test_web_flow(ad_with_chats, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    s = FakeSession(_page(_links("setAdSold"), "adId=1"), REASONS)
    monkeypatch.setattr(sold, "get_session", lambda: s)
    with TestClient(app) as c:
        assert "Mark sold to Max Muster" in c.get("/chats/c-buyer").text
        assert f"/sold/{AD}?kind=sold" in c.get("/myads").text
        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
        assert "Erika Muster" in page and "Mark sold to Max Muster" in page and "Reserve for Max Muster" in page
        assert 'hx-post="/sold/1000000001/reserve"' in c.get("/chats/c-buyer").text        # one click, no extra page
        myads = c.get(f"/myads?ad={AD}").text
        assert 'hx-post="/sold/1000000001/sell"' in myads and "Mark sold…" not in myads
        assert page.count('name="conv"') == 2 and page.count("checked>") >= 2 and "Pick the buyer above" not in page
        nobuyer = c.get(f"/sold/{AD}?kind=sold").text   # buyer unknown: nobody pre-checked (the buyer is among them)
        assert nobuyer.count('name="conv"') == 3 and "Pick the buyer above" in nobuyer
        assert not any('name="conv"' in line and "checked" in line for line in nobuyer.splitlines())

        # one click from the chat: sold here at once (buyer noted, automatic answers off), willhaben as a job
        r = c.post(f"/sold/{AD}/sell", data={"buyer": "c-buyer", "next": "/chats/c-buyer"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/chats/c-buyer"
        ad = sold.ad_row(AD)
        assert ad.sale_status == "sold" and ad.sale_conversation_id == "c-buyer" and ad.auto_reply is False
        wait_jobs()                                                      # background job: willhaben asks for a reason
        page = c.get("/chats/c-buyer").text
        assert "Über willhaben" in page and 'name="reason_id"' in page and not s.writes   # reason first, nothing written
        assert "to this buyer" in page and 'id="sale-actions"' in page
        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer", "reason_id": "2", "next": "/chats/c-buyer"},
                   follow_redirects=False)
        assert r.headers["location"] == "/chats/c-buyer"
        wait_jobs()
        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
        assert len(s.writes) == 2 and sold.ad_row(AD).last_action == "Marked as sold on willhaben" and sold.ad_row(AD).status == "sold"
        assert 'name="reason_id"' not in page and "willhaben: sold" in page   # the reasons are used up

        r = c.post(f"/sold/{AD}/notices", data={"kind": "sold", "buyer": "c-buyer", "conv": ["c-2", "c-3"],
                                                 "mode": "template"})
        assert "2 drafts prepared" in r.text and "Send selected" in r.text and not ad_with_chats.sent
        drafts = {o.conv.id: o.draft.id for o in sold.others(AD, "c-buyer")}
        r = c.post(f"/sold/{AD}/notices/send", data={"kind": "sold", "buyer": "c-buyer",   # no button value: nothing
                                                      "send": [str(drafts["c-2"])], f"text_{drafts['c-2']}": "Schon weg, sorry."})
        assert "Nothing done" in r.text and not ad_with_chats.sent
        r = c.post(f"/sold/{AD}/notices/send", data={"kind": "sold", "buyer": "c-buyer", "action": "send",
                                                      "send": [str(drafts["c-2"])], f"text_{drafts['c-2']}": "Schon weg, sorry."})
        assert "1 messages queued" in r.text
        wait_jobs()
        assert ad_with_chats.sent == [("c-2", "Schon weg, sorry.")]
        assert "informed" in c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text

        # the sold ad: no second sale button; "Not sold after all" lifts the local status only
        page = c.get("/chats/c-buyer").text
        assert "to this buyer" in page and "Mark sold to Max Muster" not in page and "Not sold after all" in page
        r = c.post(f"/sold/{AD}/reset", data={"kind": "sold", "buyer": "c-buyer", "next": "/chats/c-buyer"}, follow_redirects=False)
        assert r.headers["location"] == "/chats/c-buyer" and sold.ad_row(AD).sale_status is None


async def test_sold_ad_is_never_auto_renewed(ad_with_chats, monkeypatch):
    from app.services import my_ads
    calls = []

    async def no_import():
        return {AD}

    async def fake_reactivate(row):
        calls.append(row.ad_id)

    async def fake_notify(*a, **kw):
        calls.append("notify")
    monkeypatch.setattr(my_ads, "import_from_account", no_import)
    monkeypatch.setattr(my_ads, "_reactivate", fake_reactivate)
    monkeypatch.setattr(my_ads, "notify", fake_notify)
    with session() as db:
        row = db.get(MyAd, AD)
        row.auto_renew = True
        db.add(row)
        db.commit()
    assert sold.finish(AD, "sold", "c-buyer").auto_renew is False
    with session() as db:   # switched on again by hand and expired on willhaben: still never reactivated
        row = db.get(MyAd, AD)
        row.status, row.auto_renew = "expired", True
        db.add(row)
        db.commit()
    await my_ads.check_all()
    assert calls == []

    s = FakeSession(_page(_links("setAdSold")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    sold.reset(AD)
    with session() as db:
        row = db.get(MyAd, AD)
        row.auto_renew = True
        db.add(row)
        db.commit()
    await sold.on_willhaben(AD, "sold")
    assert sold.ad_row(AD).auto_renew is False


def _fake_draft(on_draft=None):
    async def fake_draft(cid, hint=None, auto=False):
        if on_draft:
            on_draft()
        with session() as db:
            d = Draft(conversation_id=cid, text="Ja, noch da.", auto=True, status="draft", auto_reason="✓ ok")
            db.add(d)
            db.commit()
            db.refresh(d)
            return d
    return fake_draft


@pytest.mark.parametrize("humanized", [True, False])
async def test_running_auto_answer_stops_when_sold(ad_with_chats, monkeypatch, humanized):
    account = ad_with_chats
    finish = lambda: sold.finish(AD, "sold", "c-buyer", auto_off=False)  # noqa: E731 - sold, auto switch untouched
    monkeypatch.setattr(chats, "draft_reply", _fake_draft(None if humanized else finish))
    monkeypatch.setattr(chats, "_auto_blockers", lambda *a: [])

    async def typing(d):   # the owner marks the ad sold while the answer is "typing"
        finish()
        return True
    d = await chats.auto_reply("c-2", before_send=typing if humanized else None)
    assert d is None and not account.sent
    with session() as db:
        assert {x.status for x in db.exec(select(Draft).where(Draft.conversation_id == "c-2"))} == {"discarded"}


async def test_running_auto_answer_stops_when_auto_off(ad_with_chats, monkeypatch):
    account = ad_with_chats
    monkeypatch.setattr(chats, "draft_reply", _fake_draft())
    monkeypatch.setattr(chats, "_auto_blockers", lambda *a: [])

    async def typing(d):
        sold.finish(AD, "reserved", "c-buyer")   # step 3 with "auto answers off"
        return True
    assert await chats.auto_reply("c-2", before_send=typing) is None and not account.sent


# ------------------------------------------------------------------ stock: several pieces in one ad
def _stocked(quantity=3, **kw):
    with session() as db:
        row = db.get(MyAd, AD)
        row.quantity, row.auto_renew = quantity, True
        for k, v in kw.items():
            setattr(row, k, v)
        db.add(row)
        db.commit()


def _sale_of(cid):
    from app.services import stock
    return stock.sold_to(cid)


async def test_one_buyer_takes_two_of_three_then_another_the_last(ad_with_chats):
    from app.services import my_ads, shipping, stock
    from app.db import Shipment
    _stocked()
    row = sold.finish(AD, "sold", "c-buyer", pieces=2, price=40.0)
    # pieces left: nothing ad-wide changes, the ad stays online and keeps answering
    assert (row.sale_status, row.auto_reply, row.auto_renew) == (None, True, True)
    assert row.last_action == "2 pieces sold (1 left)" and not my_ads.is_sold(row)
    assert stock.remaining(AD) == 1 and stock.pieces_sold(AD) == 2
    assert chats._auto_ad(Conversation(id="c-2", ad_id=AD, role="seller")) is not None
    assert sold.state_for(Conversation(id="c-buyer", ad_id=AD)) == {
        "kind": None, "buyer": None, "is_buyer": True, "pieces": 2, "quantity": 3, "remaining": 1}
    assert sold.state_for(Conversation(id="c-2", ad_id=AD))["is_buyer"] is False
    assert [o.conv.id for o in sold.others(AD, None)] == ["c-2", "c-3"]     # a piece buyer gets no "sold" notice

    for bad in (2, 0):
        with pytest.raises(ValueError, match="from 1 to 1"):
            sold.finish(AD, "sold", "c-2", pieces=bad)

    row = sold.finish(AD, "sold", "c-2", pieces=1, auto_off=True)      # the last piece: today's sold flow
    assert (row.sale_status, row.sale_conversation_id, row.auto_reply, row.auto_renew) == ("sold", "c-2", False, False)
    assert my_ads.is_sold(row) and stock.remaining(AD) == 0
    with session() as db:                       # switched on again by hand: a sold-out ad still gets none
        row.auto_reply = True
        db.add(row)
        db.commit()
    assert chats._auto_ad(Conversation(id="c-3", ad_id=AD, role="seller")) is None
    with pytest.raises(ValueError, match="No pieces left"):
        sold.finish(AD, "sold", "c-3")

    # label buying "only when sold to this buyer": every buyer of a piece, nobody else
    with session() as db:
        for cid in ("c-buyer", "c-2", "c-3"):
            db.add(Shipment(conversation_id=cid, ad_id=AD))
        db.commit()
        ships = {s.conversation_id: s for s in db.exec(select(Shipment))}
    assert [shipping._sold_to_buyer(ships[c]) for c in ("c-buyer", "c-2", "c-3")] == [True, True, False]

    # undo the last sale: pieces back, sold status gone here (willhaben untouched)
    last = _sale_of("c-2")
    assert not sold.undo_sale("1000000002", last.id)                   # not a sale of that ad
    assert sold.undo_sale(AD, last.id) and not sold.undo_sale(AD, last.id)
    row = sold.ad_row(AD)
    assert row.sale_status is None and row.sale_conversation_id is None and stock.remaining(AD) == 1
    assert row.last_action == "Sale undone (1 piece)"
    assert _sale_of("c-2") is None and not shipping._sold_to_buyer(ships["c-2"])
    sold.reset(AD)                                                      # an ad with a quantity keeps its ledger
    assert stock.pieces_sold(AD) == 2


async def test_single_item_keeps_one_sale_and_a_reservation_is_no_sale(ad_with_chats):
    from app.services import shipping, stock
    from app.db import Shipment
    sold.finish(AD, "sold", "c-buyer", pieces=5)
    assert stock.sold_to("c-buyer").pieces == 1                         # one item: one piece, whatever is posted
    sold.finish(AD, "sold", "c-2")                                      # sold to someone else after all
    assert _sale_of("c-buyer") is None and _sale_of("c-2") is not None
    assert sold.state_for(Conversation(id="c-2", ad_id=AD))["pieces"] == 1
    sold.finish(AD, "reserved", "c-3")
    assert stock.sales_of(AD) == [] and sold.ad_row(AD).sale_status == "reserved"
    with session() as db:
        db.add(Shipment(conversation_id="c-3", ad_id=AD))
        db.commit()
        s = db.exec(select(Shipment)).one()
    assert not shipping._sold_to_buyer(s)                               # reserved for this buyer: not sold to them
    sold.finish(AD, "sold", "c-3")
    assert shipping._sold_to_buyer(s)
    sold.reset(AD)
    assert stock.sales_of(AD) == [] and sold.ad_row(AD).sale_status is None and not shipping._sold_to_buyer(s)


async def test_renewal_keeps_stock_settings_and_sums_sales_over_the_chain(ad_with_chats):
    from app.services import my_ads, stock
    parcel = {"length_cm": 30, "width_cm": 20, "height_cm": 10, "weight_kg": 1.0, "source": "user"}
    _stocked(min_price=15.0, parcel=parcel)
    sold.finish(AD, "sold", "c-buyer", pieces=1)
    new = my_ads._move_to_new_id(AD, "1000000009")
    assert (new.quantity, new.min_price, new.parcel) == (3, 15.0, parcel)
    assert set(stock.ad_chain(AD)) == set(stock.ad_chain("1000000009")) == {AD, "1000000009"}
    assert stock.remaining("1000000009") == 2 and sold.current_id(AD) == "1000000009"

    # the chats keep the old id: the sale from one of them lands on the live copy and counts for the chain
    assert {c.id for c in sold.seller_conversations("1000000009")} == {"c-buyer", "c-2", "c-3"}
    sold.finish(AD, "sold", "c-2", pieces=1)
    assert _sale_of("c-2").ad_id == "1000000009" and stock.remaining(AD) == 1
    assert sold.state_for(Conversation(id="c-2", ad_id=AD))["remaining"] == 1
    sold.finish(AD, "sold", "c-3", pieces=1)
    with session() as db:
        old, live = db.get(MyAd, AD), db.get(MyAd, "1000000009")
    assert live.sale_status == "sold" and live.sale_conversation_id == "c-3" and old.sale_status is None
    assert chats._auto_ad(Conversation(id="c-3", ad_id=AD, role="seller")) is None


def test_auto_ad_skips_deleted_and_given_away_ads(ad_with_chats):
    conv = Conversation(id="c-2", ad_id=AD, role="seller")
    for status in ("deleted", "Verschenkt", "sold"):
        with session() as db:
            row = db.get(MyAd, AD)
            row.status = status
            db.add(row)
            db.commit()
        assert chats._auto_ad(conv) is None, status


def test_piece_count_guard():
    from app.services import stock
    assert stock.piece_counts("Ja, 2 Stück sind da, drei Stk. gehen nicht, 4 Teile, zwei Exemplare") == [2, 3, 4, 2]
    assert stock.piece_counts("Elf Stück oder zwölf Stk.?") == [11, 12]
    for text in ("Preis pro Stück 20 €", "Artikelnummer 06019H5202", "1,5 Stück", "Abholung in 1100 Wien"):
        assert stock.piece_counts(text) == [], text
    ad = MyAd(ad_id="x", quantity=3)
    assert stock.blockers(ad, "Klar, 4 Stück kannst du haben.") and stock.blockers(ad, "Klar, alle 3 Stück.") == []
    assert stock.blockers(MyAd(ad_id="y"), "3 Stück") == []                       # one item: no count to check


async def test_auto_answer_promising_more_pieces_is_held_back(ad_with_chats):
    _stocked()
    sold.finish(AD, "sold", "c-buyer", pieces=2)
    d = Draft(conversation_id="c-2", text="Ja, zwei Stück kannst du haben.", auto_reason="✓ einfach", intent="availability")
    why = chats._auto_blockers("c-2", d, chats.messages_of("c-2"))
    assert why == ["Answer offers 2 pieces, only 1 available"]
    d.text = "Ja, ein Stück ist noch da."
    assert chats._auto_blockers("c-2", d, chats.messages_of("c-2")) == []


async def test_reply_prompt_knows_the_stock(ad_with_chats, monkeypatch):
    from app.llm.base import LLMResult
    from app.llm.prompts import AUTO_REPLY_SYSTEM, REPLY_SYSTEM, reply_prompt
    args = dict(role="seller", ad={"title": "Rad", "price": "20 €"}, partner="Max", messages=[], rules="",
                examples=[], hint=None, now=utcnow())
    assert reply_prompt(**args) == reply_prompt(**args, stock=None)
    assert "- Stückzahl: noch 1 von 3 Stück verfügbar (Preis gilt pro Stück)" in reply_prompt(**args, stock={"quantity": 3, "remaining": 1})
    assert "- Stückzahl: ausverkauft" in reply_prompt(**args, stock={"quantity": 3, "remaining": 0})
    assert "sag nie mehr Stück zu" in REPLY_SYSTEM and "sag nie mehr Stück zu" in AUTO_REPLY_SYSTEM

    calls = []

    class Backend:
        model = "fake"

        async def complete(self, *, system, prompt, schema, images=None):
            calls.append(prompt)
            return LLMResult(data={"reply": "Ja.", "intent": "availability", "needs_info": [], "suggested_price": None,
                                   "note": ""}, model="fake")

    async def no_ctx(cid):
        with session() as db:
            return db.get(Conversation, cid)
    monkeypatch.setattr(chats, "get_backend", lambda *a: Backend())
    monkeypatch.setattr(chats, "ensure_ad_context", no_ctx)
    await chats.draft_reply("c-2")
    assert "Stückzahl" not in calls[-1]                                 # one item: the prompt as before
    _stocked()
    sold.finish(AD, "sold", "c-buyer", pieces=1)
    await chats.draft_reply("c-2")
    assert "- Stückzahl: noch 2 von 3 Stück verfügbar" in calls[-1]


def test_chat_context_refreshes_on_a_sale(ad_with_chats):
    _stocked()
    before = chats.live_state("c-2")["context"]
    sold.finish(AD, "sold", "c-buyer", pieces=1)
    after = chats.live_state("c-2")["context"]
    assert before != after
    assert sold.undo_sale(AD, _sale_of("c-buyer").id)
    assert chats.live_state("c-2")["context"] == before


def test_telegram_switch_acts_on_the_live_copy(ad_with_chats):
    from app.services import my_ads, telegram
    my_ads._move_to_new_id(AD, "1000000009")
    telegram._set_auto(AD, False)
    with session() as db:
        assert db.get(MyAd, "1000000009").auto_reply is False


def test_stock_web_flow(ad_with_chats, monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import Sale, Shipment
    from app.main import app
    auto = {"X-Autosave": "1"}
    s = FakeSession(_page(_links("setAdSold")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    with session() as db:
        db.add(Shipment(conversation_id="c-buyer", ad_id=AD, agreed_price=40.5))
        db.commit()
    with TestClient(app) as c:
        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
        assert 'name="pieces"' not in page and "Mark sold to Max Muster" in page and "Stock" not in page   # one item: as before

        assert c.post(f"/myads/{AD}/quantity", data={"quantity": "3"}, headers=auto).json() == {"ok": True, "error": None}
        for bad in ("0", "1000", "abc", "-2"):
            assert c.post(f"/myads/{AD}/quantity", data={"quantity": bad}, headers=auto).json()["ok"] is False, bad
        myads = c.get(f"/myads?ad={AD}").text
        assert "3/3 pcs" in myads and "0 sold · 3 available" in myads and 'name="quantity"' in myads
        assert "$event.detail.ok && refreshInPlace([document.getElementById('myads-detail')" in myads  # saved: stock line anew

        page = c.get(f"/sold/{AD}?kind=sold&buyer=c-buyer").text
        assert "3 of 3 pieces left" in page and 'name="pieces"' in page and 'value="40.5"' in page
        assert "Mark sold to Max Muster" in page and "Mark on willhaben" not in page and 'name="conv"' not in page
        assert page.count("the others are told only with the last piece") == 1
        assert "Record sale" in c.get(f"/sold/{AD}?kind=sold").text                      # no buyer: the plain label

        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-buyer", "pieces": "2", "price": "x"})
        assert "Price must be a number" in r.text
        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-buyer", "pieces": ""})   # blank is no 1
        assert "Pieces: a number from 1 to 3" in r.text and _sale_of("c-buyer") is None
        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-buyer", "pieces": "2", "price": "40,50",
                                                "auto_off": "1"})
        assert "2 pieces sold (1 left)" in r.text and "1 of 3 pieces left" in r.text and "2 pcs" in r.text
        assert "€ 40,50" in r.text and sold.ad_row(AD).auto_reply is True
        assert r.text.count("1 piece left – the ad stays online") == 1

        # a stale page with step 1 (pieces left): nothing is queued, nothing reaches willhaben
        r = c.post(f"/sold/{AD}/willhaben", data={"kind": "sold", "buyer": "c-buyer"})
        assert "Pieces left: record the sale instead" in r.text
        wait_jobs()
        assert s.calls == [] and sold.ad_row(AD).pending_action is None
        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-2", "pieces": "5"})
        assert "from 1 to 1" in r.text

        chat = c.get("/chats/c-buyer").text
        assert "2 pieces sold to this person" in chat and "1 of 3 still available" in chat
        assert "1 of 3 still available" in c.get("/chats/c-2").text and "sold to this person" not in c.get("/chats/c-2").text

        r = c.post(f"/myads/{AD}/quantity", data={"quantity": "1"}, headers=auto).json()
        assert r["ok"] is False and "2 pieces are sold already" in r["error"]
        assert "2 sold · 1 available" in c.get(f"/myads?ad={AD}").text

        with session() as db:
            sale_id = db.exec(select(Sale)).one().id
        r = c.post(f"/sold/{AD}/sales/{sale_id}/undo", data={"kind": "sold", "buyer": "c-buyer"})
        assert "Sale undone here" in r.text and "3 of 3 pieces left" in r.text
        assert "Sale not found" in c.post(f"/sold/{AD}/sales/{sale_id}/undo", data={"kind": "sold"}).text

        # the last pieces: steps 1 and 2 as for a single item, the others minus the piece buyers
        r = c.post(f"/sold/{AD}/finish", data={"kind": "sold", "buyer": "c-2", "pieces": "3", "auto_off": "1"})
        assert "Saved: sold" in r.text and "All pieces are sold" in r.text and 'name="pieces"' not in r.text
        assert "Mark on willhaben" in r.text and r.text.count('name="conv"') == 2      # c-buyer, c-3; willhaben still open
        assert "to this buyer" in c.get("/chats/c-2").text and sold.ad_row(AD).auto_reply is False
        r = c.post(f"/sold/{AD}/reset", data={"kind": "sold", "buyer": "c-2"})                 # sold out: undo a sale
        assert sold.ad_row(AD).sale_status == "sold" and "Not sold after all" not in r.text
        page = c.get(f"/sold/{AD}?kind=reserved").text                                         # nothing to reserve
        assert "All pieces are sold" in page and '/reserve"' not in page

        # clearing the count would make it a single item, whose next sale undoes the whole ledger
        r = c.post(f"/myads/{AD}/quantity", data={"quantity": ""}, headers=auto).json()
        assert r["ok"] is False and "3 pieces are sold already – undo the sales first" in r["error"]
        assert sold.ad_row(AD).quantity == 3

        last = _sale_of("c-2")
        r = c.post(f"/sold/{AD}/sales/{last.id}/undo", data={"kind": "sold", "buyer": "c-2"})
        assert "automatic answers and auto-renew stay off until you switch them on again" in r.text
        assert "Mark sold to Erika Muster" in r.text and 'name="pieces"' in r.text          # pieces again: the sale form is back
        assert c.post(f"/myads/{AD}/quantity", data={"quantity": ""}, headers=auto).json()["ok"] is True
    assert sold.ad_row(AD).quantity is None


def test_locally_reserved_ad_shows_reserved_in_my_ads(ad_with_chats):
    from fastapi.testclient import TestClient

    from app.main import app
    sold.finish(AD, "reserved", "c-buyer")              # willhaben still says active (the job may wait or have failed)
    with TestClient(app) as c:
        page = c.get(f"/myads?ad={AD}&show_sold=1").text
        row = page.split(f'id="row-{AD}"')[1].split("</a>")[0]
        head = page.split('id="myads-detail"')[1].split("</h2>")[1].split("</div>")[0]
        assert ">reserved<" in row and ">active<" not in row
        assert ">reserved<" in head and ">active<" not in head


def test_locally_sold_ad_shows_sold_in_my_ads(ad_with_chats):
    from fastapi.testclient import TestClient

    from app.main import app
    sold.finish(AD, "sold", "c-buyer")             # willhaben still says active
    with TestClient(app) as c:
        page = c.get(f"/myads?ad={AD}&show_sold=1").text
    row = page[page.index(f'id="row-{AD}"'):page.index('id="myads-detail"')]
    header = page[page.index('id="myads-detail"'):page.index("data-listings")]
    for part in (row, header):
        assert 'rounded-full bg-current"></span>sold</span>' in part and "</span>active</span>" not in part


async def test_set_quantity_follows_the_ledger(ad_with_chats):
    """The piece count and the sale state agree: blank only without sales, lowering to the pieces sold sells the ad
    out here, raising it again opens it; willhaben's status and the automatic answers stay as they are."""
    from app.services import my_ads, stock
    conv = Conversation(id="c-3", ad_id=AD, role="seller")
    _stocked()
    assert sold.finish(AD, "sold", "c-buyer", pieces=1).last_action == "1 piece sold (2 left)"
    with pytest.raises(ValueError, match="1 piece is sold already – undo the sale first"):
        my_ads.set_quantity(AD, None)
    sold.finish(AD, "sold", "c-2", pieces=1)
    with pytest.raises(ValueError, match="2 pieces are sold already – undo the sales first"):
        my_ads.set_quantity(AD, None)
    with pytest.raises(ValueError, match="the number cannot be lower"):
        my_ads.set_quantity(AD, 1)

    my_ads.set_quantity(AD, 2)                                          # sold out by lowering
    row, newest = sold.ad_row(AD), _sale_of("c-2")
    assert (row.quantity, row.sale_status, row.sale_conversation_id, row.sale_at) == (2, "sold", "c-2", newest.created_at)
    assert (row.auto_renew, row.auto_reply, row.status, row.last_action) == (False, True, "active", "Sold")
    assert my_ads.is_sold(row) and chats._auto_ad(conv) is None and stock.remaining(AD) == 0

    my_ads.set_quantity(AD, 4)                                          # restocked
    row = sold.ad_row(AD)
    assert (row.quantity, row.sale_status, row.sale_conversation_id, row.sale_at) == (4, None, None, None)
    assert (row.status, row.last_action) == ("active", "2 sold · 2 available") and stock.remaining(AD) == 2
    assert not my_ads.is_sold(row) and chats._auto_ad(conv) is not None

    for sale in stock.sales_of(AD):
        sold.undo_sale(AD, sale.id)
    my_ads.set_quantity(AD, 1)                                          # one piece, nothing sold: one item
    assert sold.ad_row(AD).quantity is None


async def test_willhaben_is_not_marked_sold_while_pieces_are_left(ad_with_chats, monkeypatch):
    s = FakeSession(_page(_links("setAdSold", "markAdReserved")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    _stocked()
    sold.finish(AD, "sold", "c-buyer", pieces=1)
    with pytest.raises(sales.NotOffered, match="Pieces left: record the sale instead"):
        await sold.on_willhaben(AD, "sold")
    assert sold.enqueue_willhaben(AD, "sold")                         # a job queued from a stale page
    await finish_jobs()
    assert s.calls == [] and "Pieces left" in sold.ad_row(AD).last_error
    await sold.on_willhaben(AD, "reserved")                             # a reservation stays ad-wide
    assert s.writes == [("PUT", f"/webapi/iad/bap/updatesalesinfo/{AD}/5", None)]


async def test_reserved_needs_a_piece_left(ad_with_chats):
    from app.services import stock
    _stocked()
    sold.finish(AD, "sold", "c-buyer", pieces=2)
    row = sold.finish(AD, "reserved", "c-2", pieces=5)                  # pieces do not matter for a reservation
    assert (row.sale_status, row.last_action, stock.pieces_sold(AD)) == ("reserved", "Reserved", 2)
    sold.finish(AD, "sold", "c-2", pieces=1)
    with pytest.raises(ValueError, match="No pieces left"):
        sold.finish(AD, "reserved", "c-3")
    assert sold.ad_row(AD).sale_status == "sold"


def test_api_lists_the_stock(ad_with_chats):
    from fastapi.testclient import TestClient

    from app.main import app
    _stocked()
    sold.finish(AD, "sold", "c-buyer", pieces=1)
    with session() as db:
        db.add(MyAd(ad_id="1000000002", heading="Lampe", status="active"))
        db.commit()
    with TestClient(app) as c:
        rows = {r["ad_id"]: r for r in c.get("/api/myads").json()}
    assert rows[AD]["stock"] == {"quantity": 3, "sold": 1, "remaining": 2} and rows[AD]["heading"] == "Fahrrad"
    assert rows["1000000002"]["stock"] is None


async def test_reserve_and_unreserve_in_one_click(ad_with_chats, monkeypatch):
    """One click: reserved here at once (buyer noted, automatic answers off) and on willhaben as a job; one click
    lifts both. Reserved on willhaben already: nothing queued."""
    s = FakeSession(_page(_links("markAdReserved", "removeMarkAdReserved")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    row, queued = sold.reserve(AD, "c-buyer")
    assert queued and row.sale_status == "reserved" and row.sale_conversation_id == "c-buyer" and row.auto_reply is False
    assert sold.ad_row(AD).pending_action == "reserved"
    await finish_jobs()
    ad = sold.ad_row(AD)
    assert ad.status == "reserved" and ad.pending_action is None and "updatesalesinfo" in s.calls[-1][1] and len(s.calls) == 1
    row, queued = sold.reserve(AD, "c-2")                 # another buyer: noted here, willhaben has it already
    assert not queued and row.sale_conversation_id == "c-2" and len(s.calls) == 1
    row, queued = sold.unreserve(AD)
    assert queued and row.sale_status is None and row.sale_conversation_id is None
    await finish_jobs()
    assert sold.ad_row(AD).status == "active" and len(s.calls) == 2 and s.calls[-1][1].endswith("/0")
    row, queued = sold.unreserve(AD)                      # nothing reserved: nothing queued
    assert not queued and len(s.calls) == 2


async def test_sell_in_one_click_and_pieces_only_with_the_last(ad_with_chats, monkeypatch):
    from app.services import my_ads
    s = FakeSession(_page(_links("setAdSold")))           # no reason query: written at once
    monkeypatch.setattr(sold, "get_session", lambda: s)
    row, queued = sold.sell(AD, "c-buyer")
    assert queued and row.sale_status == "sold" and row.auto_reply is False and sold.ad_row(AD).pending_action == "sold"
    await finish_jobs()
    assert sold.ad_row(AD).status == "sold" and len(s.calls) == 1 and "/sold/" in s.calls[0][1]
    row, queued = sold.sell(AD, "c-2")                     # sold on willhaben already: nothing queued
    assert not queued and len(s.calls) == 1
    sold.reset(AD)
    with session() as db:                                  # back to an active ad with pieces
        r = db.get(MyAd, AD)
        r.status, r.auto_reply = "active", True
        db.add(r)
        db.commit()
    s.calls.clear()
    my_ads.set_quantity(AD, 2)
    row, queued = sold.sell(AD, "c-buyer", pieces=1, price=20)
    assert not queued and row.sale_status is None and row.auto_reply is True and sold.ad_row(AD).pending_action is None
    row, queued = sold.sell(AD, "c-2", pieces=1)           # the last piece: sold out, willhaben too
    assert queued and row.sale_status == "sold"
    await finish_jobs()
    assert sold.ad_row(AD).status == "sold" and len(s.calls) == 1


def test_import_follows_the_marks_made_on_willhaben():
    """A mark set on willhaben itself (sold, reserved, reservation lifted) lands in the local sale status."""
    from app.services import my_ads
    row = MyAd(ad_id="x", status="reserved")
    my_ads._follow_sale_status(row, "active")
    assert row.sale_status == "reserved" and row.sale_conversation_id is None
    row.status = "active"
    my_ads._follow_sale_status(row, "reserved")
    assert row.sale_status is None
    row.status, row.auto_reply, row.auto_renew = "sold", True, True
    my_ads._follow_sale_status(row, "active")
    assert row.sale_status == "sold" and row.auto_reply is False and row.auto_renew is False
    kept = MyAd(ad_id="y", status="reserved", sale_status="reserved", sale_conversation_id="c-1")
    my_ads._follow_sale_status(kept, "active")              # reserved here for a known buyer: untouched
    assert kept.sale_conversation_id == "c-1"
    local = MyAd(ad_id="z", status="active", sale_status="reserved", sale_conversation_id="c-1")
    my_ads._follow_sale_status(local, "active")             # reserved here only (willhaben step still open): stays
    assert local.sale_status == "reserved"


async def test_unreserve_puts_the_automatic_answers_back(ad_with_chats, monkeypatch):
    """A reservation switches the automatic answers off; lifting it puts them back as they were – on, or off when
    they were off before. "Not sold after all" leaves them off."""
    s = FakeSession(_page(_links("markAdReserved", "removeMarkAdReserved")))
    monkeypatch.setattr(sold, "get_session", lambda: s)
    row, _ = sold.reserve(AD, "c-buyer")
    assert row.auto_reply is False and row.auto_reply_before_sale is True
    await finish_jobs()
    row, _ = sold.unreserve(AD)
    assert row.auto_reply is True and row.auto_reply_before_sale is None and row.sale_status is None
    await finish_jobs()
    with session() as db:                                  # answers off before the reservation: stay off
        r = db.get(MyAd, AD)
        r.auto_reply, r.status = False, "active"
        db.add(r)
        db.commit()
    sold.reserve(AD, "c-2")
    await finish_jobs()
    row, _ = sold.unreserve(AD)
    assert row.auto_reply is False
    await finish_jobs()
    with session() as db:
        r = db.get(MyAd, AD)
        r.auto_reply, r.status = True, "active"
        db.add(r)
        db.commit()
    sold.finish(AD, "sold", "c-buyer")                     # the deal fell through: "not sold after all" keeps them off
    sold.reset(AD)
    assert sold.ad_row(AD).auto_reply is False and sold.ad_row(AD).auto_reply_before_sale is None
