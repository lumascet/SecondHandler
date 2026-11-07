"""Fixes to the Kleinanzeigen posting found in review: a renewed ad keeps its copy (no second posting), a removed ad
keeps its Vinted item listed, an analysed price never undercuts the ad's floor, only an ad for sale is listed."""
from app.db import MyAd, session
from app.services import crosspost_kleinanzeigen as ck


def test_an_analysed_price_never_undercuts_the_floor():
    assert ck._guard(50.0, "Analyse", 500.0, None)[0] == 500.0                 # far below willhaben: willhaben's
    assert ck._guard(420.0, "Analyse", 500.0, None) == (420.0, "Analyse")      # within the floor: as analysed
    assert ck._guard(380.0, "Analyse", 500.0, 400.0)[0] == 500.0               # below the ad's own lowest price
    assert ck._guard(0.0, None, 0.0, None) == (0.0, None)                      # a giveaway stays one
    draft = {"also": {}, "price": 500.0, "kleinanzeigen": {"price": 60.0, "note": "x"}}
    assert ck._draft_price(draft)[0] == 500.0
    draft["also"]["kleinanzeigen_price"] = 60.0                                # typed by the owner: as typed
    assert ck._draft_price(draft) == (60.0, None)


def test_only_an_ad_for_sale_is_listed():
    with session() as db:
        db.add(MyAd(ad_id="9000000001", heading="Rad", status="sold"))
        db.commit()
    assert ck.enqueue("9000000001") is False


def test_a_renewed_ad_keeps_its_kleinanzeigen_copy():
    """The copy follows the ad to its new id: the old row (replaced) is skipped by the sync, and the new one without a
    record would be posted a second time by "List"."""
    from app.services import my_ads
    with session() as db:
        db.add(MyAd(ad_id="9000000002", heading="Rad", status="active",
                    kleinanzeigen={"id": "3210987654", "state": "active"}))
        db.commit()
    my_ads._move_to_new_id("9000000002", "9000000003")
    with session() as db:
        new, old = db.get(MyAd, "9000000003"), db.get(MyAd, "9000000002")
    assert (new.kleinanzeigen or {}).get("id") == "3210987654" and old.kleinanzeigen is None
