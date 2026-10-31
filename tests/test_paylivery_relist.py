"""A piece paid with PayLivery counts in the stock, and an ad with pieces left goes online again after willhaben took
it offline: in place when willhaben offers a way back, otherwise as a new ad copied from it (my_ads.relist)."""
import pytest

from app.db import Conversation, MyAd, Sale, session
from app.services import jobs, my_ads, sold, stock
from app.willhaben import insert
from app.willhaben.account import AdvertList, OwnAdvert
from tests.conftest import finish_jobs

AD, NEW = "1000000001", "1000000009"


@pytest.fixture
def ad():
    with session() as db:
        db.add(MyAd(ad_id=AD, heading="USB-Kabel", status="active", quantity=3, auto_reply=True, auto_renew=True,
                    min_price=5, url=f"https://www.willhaben.at/iad/object?adId={AD}"))
        db.add(Conversation(id="cs1", ad_id=AD, role="seller", partner_name="Max"))
        db.add(Conversation(id="cs2", ad_id=AD, role="seller", partner_name="Eva"))
        db.commit()


def _conv(cid="cs1"):
    with session() as db:
        return db.get(Conversation, cid)


def _row(ad_id=AD):
    with session() as db:
        return db.get(MyAd, ad_id)


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(my_ads, "enqueue_relist", lambda ad_id: calls.append(ad_id) or True)
    return calls


# ------------------------------------------------------------------ the sale
async def test_paid_purchase_counts_one_piece_once(ad, queued):
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid", "amount": 12.5})
    await sold.on_paylivery_stage(_conv(), "released", {"stage": "released", "amount": 12.5})   # next stage: no 2nd
    (s,) = stock.sales_of(AD)
    assert (s.conversation_id, s.pieces, s.price, s.note) == ("cs1", 1, 12.5, "paylivery")
    row = _row()
    assert stock.remaining(AD) == 2 and row.sale_status is None
    assert row.auto_reply is True and row.auto_renew is True              # pieces left: answers go on
    assert row.relist and queued == [AD, AD]                              # each stage arms the relisting again


async def test_sale_recorded_by_hand_is_not_counted_again(ad, queued):
    sold.finish(AD, "sold", "cs1", pieces=2)
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    assert stock.pieces_sold(AD) == 2 and queued == [AD]


async def test_old_notice_records_nothing(ad, queued):
    """A first sync or a recheck of an old chat applies weeks-old notices too."""
    from datetime import timedelta

    from app.db import utcnow

    old = (utcnow() - timedelta(days=30)).isoformat()
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid", "history": [{"at": old, "stage": "paid"}]})
    assert stock.sales_of(AD) == [] and not queued


async def test_paid_notice_from_the_chat_sync_counts(ad, monkeypatch):
    """The real path: a seller notice in the chat, paylivery.scan, the stage hook as registered by app.main."""
    import app.main  # noqa: F401 - registers the hooks
    from datetime import timedelta

    from app.db import Message, Shipment, utcnow
    from app.services import paylivery, shipping
    from tests.test_paylivery_hooks import _settle

    assert sold.on_paylivery_stage in paylivery.STAGE_HOOKS
    monkeypatch.setattr(paylivery, "STAGE_HOOKS", [sold.on_paylivery_stage])
    armed = []
    monkeypatch.setattr(my_ads, "enqueue_relist", lambda ad_id: armed.append(ad_id) or True)

    async def detect(conversation_id, force=False):
        with session() as db:
            s = Shipment(conversation_id=conversation_id, status="draft")
            db.add(s)
            db.commit()
            db.refresh(s)
            return s

    async def after_paid(shipment_id, **kw):
        pass

    monkeypatch.setattr(shipping, "detect", detect)
    monkeypatch.setattr(shipping, "after_paid", after_paid)
    with session() as db:
        db.add(Message(conversation_id="cs1", ext_id="n1", direction="unknown", text="Zahlung erhalten: 12,50 €",
                       kind="integration", created_at=utcnow() - timedelta(minutes=5)))
        db.commit()
    await paylivery.scan("cs1")
    await _settle()
    (s,) = stock.sales_of(AD)
    assert (s.conversation_id, s.price, s.note) == ("cs1", 12.5, "paylivery") and armed == [AD]


async def test_last_piece_sells_out_without_relisting(ad, queued):
    sold.finish(AD, "sold", "cs2", pieces=2)
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    row = _row()
    assert stock.remaining(AD) == 0 and row.sale_status == "sold" and row.auto_renew is False
    assert row.relist is None and not queued
    assert not jobs._active                                               # nothing marked sold on willhaben either


async def test_single_item_is_left_to_the_import(ad, queued):
    with session() as db:
        row = db.get(MyAd, AD)
        row.quantity = None
        db.add(row)
        db.commit()
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    assert stock.sales_of(AD) == [] and _row().relist is None and not queued


async def test_offers_and_buyer_chats_change_nothing(ad, queued):
    await sold.on_paylivery_stage(_conv(), "offer_accepted", {"stage": "offer_accepted"})
    with session() as db:
        db.add(Conversation(id="cb1", ad_id=AD, role="buyer"))
        db.commit()
    await sold.on_paylivery_stage(_conv("cb1"), "paid", {"stage": "paid"})
    assert stock.sales_of(AD) == [] and not queued


async def test_cancelled_purchase_gives_its_piece_back(ad, queued):
    sold.finish(AD, "sold", "cs2")                                        # by hand: stays
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    assert stock.remaining(AD) == 1
    await sold.on_paylivery_stage(_conv(), "cancelled", {"stage": "cancelled"})
    assert stock.remaining(AD) == 2 and [s.conversation_id for s in stock.sales_of(AD)] == ["cs2"]


# ------------------------------------------------------------------ the import while willhaben shows "sold"
def _own(status, actions=(), ad_id=AD):
    return OwnAdvert(ad_id=ad_id, heading="USB-Kabel", status=status, status_label=status, price_display="12",
                     start=None, end=None, url=f"https://www.willhaben.at/iad/object?adId={ad_id}", image_url=None,
                     actions={a: {"id": a, "relativePath": f"/bap/{a}/{ad_id}", "serviceName": "iad"} for a in actions})


async def test_willhaben_sold_seen_before_the_paid_notice(ad, queued):
    """The import saw willhaben's "sold" for the purchase first: the paid piece counts, the ad sells on and goes online
    again with its switches as they were."""
    await my_ads.import_from_account(_list(_own("sold")))
    assert _row().sale_status == "sold" and _row().auto_reply is False and not queued
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid", "amount": 10})
    row = _row()
    assert stock.pieces_sold(AD) == 1 and stock.remaining(AD) == 2
    assert row.sale_status is None and not my_ads.is_sold(row)
    assert row.auto_reply is True and row.auto_renew is True and row.auto_reply_before_sale is None
    assert row.relist and queued == [AD]


async def test_last_piece_after_willhaben_sold_stays_sold(ad, queued):
    sold.finish(AD, "sold", "cs2", pieces=2)
    await my_ads.import_from_account(_list(_own("sold")))
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    row = _row()
    assert stock.remaining(AD) == 0 and row.sale_status == "sold" and row.auto_reply is False and not queued


async def test_later_stage_of_an_older_purchase_does_not_open_a_sold_ad(ad, queued):
    """Marked sold on willhaben after the piece was counted (marker gone): a later stage of that purchase leaves it."""
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    my_ads._set_relist(AD, None)
    queued.clear()
    await my_ads.import_from_account(_list(_own("sold")))
    await sold.on_paylivery_stage(_conv(), "shipped", {"stage": "shipped"})
    assert _row().sale_status == "sold" and stock.pieces_sold(AD) == 1 and not queued

def _list(*ads):
    out = AdvertList(ads)
    out.total = len(out)
    return out


async def test_import_keeps_a_relisting_ad_open_and_queues_it(ad, queued):
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid"})
    queued.clear()
    await my_ads.import_from_account(_list(_own("sold")))
    row = _row()
    assert row.status == "sold" and row.sale_status is None and row.auto_reply is True and row.auto_renew is True
    assert not my_ads.is_sold(row) and queued == [AD]


async def test_import_leaves_a_reserved_ad_alone(ad, queued):
    """Reserved ads are still listed publicly: a copy would be a second live ad."""
    my_ads.mark_relist(AD)
    await my_ads.import_from_account(_list(_own("reserved")))
    assert not queued and _row().relist


async def test_import_drops_an_old_marker(ad, queued):
    from datetime import timedelta

    from app.db import utcnow

    my_ads.mark_relist(AD)
    my_ads._set_relist(AD, {"since": (utcnow() - timedelta(days=3)).isoformat(), "checks": 0, "tries": 0})
    await my_ads.import_from_account(_list(_own("sold")))
    row = _row()
    assert not queued and row.relist is None and row.sale_status == "sold"      # willhaben's sold counts again


async def test_import_without_marker_follows_willhaben_sold(ad, queued):
    await my_ads.import_from_account(_list(_own("sold")))
    row = _row()
    assert row.sale_status == "sold" and my_ads.is_sold(row) and not queued


async def test_deactivated_by_hand_is_not_relisted(ad, queued, monkeypatch):
    my_ads.mark_relist(AD)

    class Acct:
        def __init__(self, s):
            pass

        async def deactivate_ad(self, ad_id, ads=None):
            pass

    async def no_import(ads=None):
        return set()

    monkeypatch.setattr(my_ads, "AccountClient", Acct)
    monkeypatch.setattr(my_ads, "get_session", lambda: None)
    monkeypatch.setattr(my_ads, "import_from_account", no_import)
    await my_ads.deactivate(AD)
    assert _row().relist is None


# ------------------------------------------------------------------ the relisting job
class FakeAccount:
    ads: AdvertList = AdvertList()
    reactivated: list = []

    def __init__(self, s):
        pass

    async def my_adverts(self):
        return FakeAccount.ads

    async def reactivate(self, ad_id, ads=None):
        FakeAccount.reactivated.append(ad_id)
        return ad_id


@pytest.fixture
def willhaben(monkeypatch):
    """The account (my_adverts / reactivate), pushes and the copy's willhaben calls, all faked."""
    FakeAccount.ads, FakeAccount.reactivated = _list(_own("active")), []
    pushes, published = [], []

    async def notify(kind, title, message, **kw):
        pushes.append((kind, title, message))

    async def no_import(ads=None):
        return set()

    async def copy_plan(s, ad_id):
        return {"ad": {"heading": "USB-Kabel", "contextLinkList": {}}, "photos": ["https://cache.willhaben.at/mmo/1.jpg"],
                "shipping": {"handover": "both", "paylivery": True, "size": "S", "carrier": "Post"}}

    async def download(url, folder, name, **kw):
        p = folder / f"{name}.jpg"
        p.write_bytes(b"jpeg")
        return p

    async def publish(s, ad, photos, existing_ad_id=None, on_presaved=None, shipping=None, on_step=None, **kw):
        published.append({"ad": ad, "photos": photos, "existing": existing_ad_id, "shipping": shipping})
        on_presaved(NEW)
        return {"ad_id": NEW, "url": f"https://www.willhaben.at/iad/object?adId={NEW}"}

    from app.services import ad_images
    monkeypatch.setattr(my_ads, "AccountClient", FakeAccount)
    monkeypatch.setattr(my_ads, "get_session", lambda: None)
    monkeypatch.setattr(my_ads, "notify", notify)
    monkeypatch.setattr(my_ads, "import_from_account", no_import)
    monkeypatch.setattr(insert, "copy_plan", copy_plan)
    monkeypatch.setattr(insert, "publish", publish)
    monkeypatch.setattr(ad_images, "download", download)
    return {"pushes": pushes, "published": published}


async def _sell_one():
    await sold.on_paylivery_stage(_conv(), "paid", {"stage": "paid", "amount": 12.0})
    await finish_jobs()


async def test_still_online_waits_and_finally_lets_go(ad, willhaben):
    await _sell_one()                                                     # willhaben still shows it active
    assert _row().relist["checks"] == 1 and not willhaben["published"] and not FakeAccount.reactivated
    for _ in range(my_ads.RELIST_CHECKS - 1):
        assert my_ads.enqueue_relist(AD)
        await finish_jobs()
    assert _row().relist is None and not willhaben["published"]           # willhaben kept it online
    assert _row().pending_action is None


async def test_deactivated_ad_is_activated_in_place(ad, willhaben):
    FakeAccount.ads = _list(_own("deactivated", ["4"]))
    await _sell_one()
    row = _row()
    assert FakeAccount.reactivated == [AD] and not willhaben["published"]
    assert row.relist is None and row.replaced_by is None and row.pending_action is None
    assert [k for k, *_ in willhaben["pushes"]] == ["ad_renewed"]


async def test_sold_ad_comes_back_as_a_new_ad(ad, willhaben):
    """Marked sold, willhaben offers no way back: the ad is copied (text, photos, PayLivery) as a new insertion."""
    FakeAccount.ads = _list(_own("sold"))
    await _sell_one()
    (pub,) = willhaben["published"]
    assert pub["shipping"] == {"handover": "both", "paylivery": True, "size": "S", "carrier": "Post"}
    assert pub["existing"] is None and len(pub["photos"]) == 1 and not FakeAccount.reactivated
    old, new = _row(), _row(NEW)
    assert old.replaced_by == NEW and old.relist is None and old.status == "replaced"
    assert (new.quantity, new.auto_reply, new.auto_renew, new.min_price) == (3, True, True, 5)
    assert stock.remaining(NEW) == 2 and stock.remaining(AD) == 2         # the sale counts over the chain
    (kind, title, message), = willhaben["pushes"]
    assert kind == "ad_renewed" and "2" in message


async def test_taken_offline_only_when_the_seller_accepts(ad, willhaben):
    """Paid while willhaben keeps the ad online (the looks run out), offline once the purchase is accepted."""
    await _sell_one()
    for _ in range(my_ads.RELIST_CHECKS - 1):
        my_ads.enqueue_relist(AD)
        await finish_jobs()
    assert _row().relist is None
    FakeAccount.ads = _list(_own("sold"))
    await sold.on_paylivery_stage(_conv(), "released", {"stage": "released"})
    await finish_jobs()
    assert len(willhaben["published"]) == 1 and _row().replaced_by == NEW and stock.remaining(NEW) == 2


async def test_reserved_ad_is_not_copied(ad, willhaben):
    FakeAccount.ads = _list(_own("reserved"))
    await _sell_one()
    assert not willhaben["published"] and _row().relist["checks"] == 1


async def test_deleted_on_willhaben_is_not_copied(ad, willhaben):
    FakeAccount.ads = _list(_own("active", ad_id="1000000005"))          # complete, without the ad
    await _sell_one()
    assert not willhaben["published"] and _row().relist is None


async def test_never_relisted_while_the_account_list_is_incomplete(ad, willhaben):
    FakeAccount.ads = AdvertList()                                       # total unknown: the ad may be online
    await _sell_one()
    assert not willhaben["published"] and _row().relist["checks"] == 1


async def test_failed_copy_continues_its_draft_then_asks_the_owner(ad, willhaben, monkeypatch):
    FakeAccount.ads = _list(_own("sold"))
    tries = []

    async def publish(s, ad, photos, existing_ad_id=None, on_presaved=None, **kw):
        tries.append(existing_ad_id)
        on_presaved(NEW)
        raise insert.InsertError("Speichern fehlgeschlagen")

    monkeypatch.setattr(insert, "publish", publish)
    await _sell_one()
    assert _row().relist["draft"] == NEW and _row().last_error
    for _ in range(my_ads.RELIST_TRIES - 1):
        assert my_ads.enqueue_relist(AD)
        await finish_jobs()
    assert tries == [None, NEW, NEW]                                      # one willhaben draft, finished again
    assert _row().relist is None
    (kind, title, message), = willhaben["pushes"]
    assert kind == "ad_expired" and "PayLivery" in message


# ------------------------------------------------------------------ the copy's willhaben side
class CopySession:
    def __init__(self, selected=True):
        self.selected, self.calls = selected, []

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path))
        if path == f"/webapi/iad/bap/{AD}":
            return {"adId": int(AD), "adUuid": "old", "heading": "USB-Kabel", "description": "<p>3 Stück</p>",
                    "price": 12, "categoryPath": {"path": [{"code": "1"}]}, "treeAttributeIds": [1],
                    "treeAttributes": [{"attributeNode": {"code": "Uebergabe"},
                                        "selectedAttributeValueNodes": [{"code": "PICKUP"}, {"code": "Versand"}]}],
                    "contextLinkList": {"contextLink": [
                        {"id": "imageList", "relativePath": f"/atz/{AD}/images", "serviceName": "iad"},
                        {"id": "p2ppUserOptions", "relativePath": f"/p2pp/{AD}", "serviceName": "p2p"}]}}
        if path.endswith("/images"):
            return {"advertImage": [{"reference": "a/b.jpg", "mainImageUrl": "https://cache.willhaben.at/mmo/a/b.jpg"},
                                    {"reference": "a/c.jpg"}]}
        if path.endswith(f"/p2pp/{AD}"):
            return {"deliveryOptions": [{"uuid": "u1", "parcelSize": {"size": "S"}, "carrier": {"description": "dpd"}},
                                        {"uuid": "u2", "parcelSize": {"size": "M"}, "carrier": {"description": "Post"}}],
                    "selectedDeliveryOptions": [{"deliveryOptionUuid": "u2"}] if self.selected else []}
        raise AssertionError(path)


async def test_copy_plan_takes_content_photos_and_paylivery(monkeypatch):
    template = {"location": {"zip": "1010"}, "contextLinkList": {"contextLink": [{"id": "presave", "relativePath": "/bap/presave"}]}}

    async def load_form(s):
        return {"ad": template}

    monkeypatch.setattr(insert, "load_form", load_form)
    plan = await insert.copy_plan(CopySession(), AD)
    ad = plan["ad"]
    assert (ad["heading"], ad["description"], ad["price"], ad["location"]) == ("USB-Kabel", "<p>3 Stück</p>", 12, {"zip": "1010"})
    assert "adId" not in ad and "treeAttributeIds" not in ad and ad["contextLinkList"] == template["contextLinkList"]
    assert plan["photos"] == ["https://cache.willhaben.at/mmo/a/b.jpg", "https://cache.willhaben.at/mmo/a/c.jpg"]
    assert plan["shipping"] == {"handover": "both", "paylivery": True, "size": "M", "carrier": "Post"}
    assert template == {"location": {"zip": "1010"}, "contextLinkList": template["contextLinkList"]}   # not changed


async def test_shipping_without_paylivery_option():
    s = CopySession(selected=False)
    old = await s.api_json("GET", f"/webapi/iad/bap/{AD}")
    assert await insert.shipping_of(s, old) == {"handover": "both", "paylivery": False}
    old["treeAttributes"] = []
    assert await insert.shipping_of(s, old) == {"handover": "pickup"}



# ------------------------------------------------------------------ "Online again" by hand
async def test_reopen_records_the_missing_pieces_and_relists(ad, queued):
    """willhaben's "sold" without a counted piece (an older version): 2 of 3 still there."""
    await my_ads.import_from_account(_list(_own("sold")))
    assert my_ads.reopen(AD, 2)
    row = _row()
    (s,) = stock.sales_of(AD)
    assert (s.pieces, s.conversation_id, s.note) == (1, None, my_ads.REOPEN_NOTE)
    assert row.quantity == 3 and stock.remaining(AD) == 2 and row.sale_status is None
    assert row.auto_reply is True and row.auto_renew is True and row.relist and queued == [AD]


async def test_reopen_counts_a_paid_chat_once(ad, queued):
    """The paid chat gets the missing piece: its later stages count nothing twice, a cancellation gives it back."""
    with session() as db:
        conv = db.get(Conversation, "cs1")
        conv.paylivery = {"stage": "paid", "amount": 11.5}
        db.add(conv)
        db.commit()
    await my_ads.import_from_account(_list(_own("sold")))
    assert my_ads.reopen(AD, 1)
    sales = stock.sales_of(AD)
    assert sorted((s.conversation_id or "", s.pieces, s.note) for s in sales) == [
        ("", 1, my_ads.REOPEN_NOTE), ("cs1", 1, sold.PAYLIVERY_NOTE)]
    await sold.on_paylivery_stage(_conv(), "shipped", {"stage": "shipped", "amount": 11.5})
    assert stock.pieces_sold(AD) == 2 and stock.remaining(AD) == 1
    await sold.on_paylivery_stage(_conv(), "cancelled", {"stage": "cancelled"})
    assert stock.remaining(AD) == 2

async def test_reopen_with_more_pieces_raises_the_count(ad, queued):
    sold.finish(AD, "sold", "cs1", pieces=3)
    assert _row().sale_status == "sold" and _row().auto_renew is False
    assert my_ads.reopen(AD, 2)
    row = _row()
    assert row.quantity == 5 and stock.pieces_sold(AD) == 3 and stock.remaining(AD) == 2
    assert row.sale_status is None and row.auto_renew is True and queued == [AD]


async def test_reopen_refuses_a_bad_count_and_a_busy_ad(ad, queued):
    with pytest.raises(ValueError):
        my_ads.reopen(AD, 0)
    my_ads._mark(AD, "edit")
    assert my_ads.reopen(AD, 1) is False and not stock.sales_of(AD) and not queued


async def test_reopened_single_item_comes_back_as_a_new_ad(ad, willhaben):
    with session() as db:
        row = db.get(MyAd, AD)
        row.quantity = None
        db.add(row)
        db.commit()
    await my_ads.import_from_account(_list(_own("sold")))
    FakeAccount.ads = _list(_own("sold"))
    assert my_ads.reopen(AD, 1)
    await finish_jobs()
    assert len(willhaben["published"]) == 1
    old, new = _row(), _row(NEW)
    assert old.replaced_by == NEW and new.quantity is None and not stock.sales_of(AD) and new.sale_status is None


def test_reopen_route(ad, queued):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.post(f"/myads/{AD}/reopen", data={"available": "2"}, follow_redirects=False)
        bad = c.post(f"/myads/{AD}/reopen", data={"available": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303) and "msg=&" in r.headers["location"] + "&"
    assert queued == [AD] and stock.remaining(AD) == 2
    assert "999" in bad.headers["location"]            # the count's range as the message

def test_sales_ledger_note_column():
    with session() as db:
        db.add(Sale(ad_id=AD, note="paylivery"))
        db.commit()
    assert stock.sales_of(AD)[0].note == "paylivery"
