"""The item condition every site states, normalised (app/platforms/condition.py), and how it travels: the parsers,
Listing/AdDetail, the hit columns."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.i18n import set_request_lang
from app.platforms import AdDetail, condition
from app.platforms import ebay, kleinanzeigen as ka, preisjaeger as pj, shpock as sh, vinted as vi
from app.willhaben.parse import parse_detail, parse_search
from tests.conftest import FIXTURES, load


@pytest.mark.parametrize("raw, value", [
    # willhaben's six values, the eBay cards and pages, Vinted, Shpock, Kleinanzeigen (live 2026-10)
    ("Neu", "new"), ("Neuwertig", "like_new"), ("Ausstellungsstück", "like_new"), ("Generalüberholt", "refurbished"),
    ("Gebraucht", "used"), ("Defekt", "defect"), ("Brandneu", "new"), ("Brand New", "new"), ("Refurbished", "refurbished"),
    ("Neu, mit Etikett", "new"), ("Sehr gut", "used"), ("Gut", "used"), ("Sehr Gut", "used"), ("In Ordnung", "used"),
    # known from the sites, not in the fixtures
    ("Neu (Sonstige)", "new"), ("Neu – Sonstige (siehe Artikelbeschreibung)", "new"), ("Neu mit Fehlern", "new"),
    ("Neu ohne Etikett", "like_new"), ("Neu, ohne Etikett", "like_new"), ("Wie neu", "like_new"),
    ("Sehr gut – Refurbished", "refurbished"), ("Zertifiziert – Generalüberholt", "refurbished"),
    ("Vom Verkäufer generalüberholt", "refurbished"), ("Akzeptabel", "used"), ("Zufriedenstellend", "used"),
    ("Nur Ersatzteile", "defect"), ("Als Ersatzteil / defekt", "defect"), ("Nicht funktionsfähig", "defect"),
    # unknown text stays unknown, never "used"
    ("Top", None), ("", None), (None, None), ("Neuer Akku", None), ("voll funktionsfähig", None),
])
def test_text_rules(raw, value):
    assert condition.from_text(raw) == value
    assert condition.pair(raw) == ((value, raw.strip()) if value else (None, None))


def test_willhaben_list_and_detail():
    ads = parse_search(load("search_fahrrad_rows3.json")).ads
    assert [(a.item_condition, a.item_condition_label) for a in ads] == [
        ("used", "Gebraucht"), ("like_new", "Neuwertig"), ("new", "Neu")]
    assert ads[0].as_dict()["item_condition"] == "used" and ads[1].as_dict()["item_condition_label"] == "Neuwertig"
    for props in (load("detail_922008415_nextdata.json")["props"]["pageProps"], load("detail_1662146981_nextdata.json")):
        d = parse_detail(props)
        assert (d.item_condition, d.item_condition_label) == ("like_new", "Neuwertig")
    # only attribute 21 is the condition (2535;2536 is the handover); the ad page's text when the id is unknown
    assert condition.willhaben_list("[2535;23, 3199;22]") == (None, None)
    assert condition.willhaben_list("[21;2539]") == ("like_new", "Ausstellungsstück")
    assert condition.willhaben_detail({"advertisingParameters": {"Zustand": ["Defekt"]}}) == ("defect", "Defekt")
    assert condition.willhaben_detail({}) == (None, None)


def test_ebay_cards_and_item_page():
    by_id = {a.ad_id: a for a in ebay.parse_search((FIXTURES / "ebay_search.html").read_text(encoding="utf-8")).ads}
    assert (by_id["407279207688"].item_condition, by_id["407279207688"].item_condition_label) == ("refurbished", "Refurbished")
    assert (by_id["237116368492"].item_condition, by_id["237116368492"].item_condition_label) == ("used", "Gebraucht")
    assert (by_id["287646086506"].item_condition, by_id["287646086506"].item_condition_label) == ("new", "Brandneu")
    assert by_id["407279207688"].body.startswith("Refurbished · ")                     # the card text stays as it was
    d = ebay.parse_item_page((FIXTURES / "ebay_item.html").read_text(encoding="utf-8"), "920036165580")
    assert (d.item_condition, d.item_condition_label) == ("used", "Gebraucht")
    assert condition.ebay(None, "UsedCondition") == ("used", "Gebraucht")             # JSON-LD alone: still stated
    assert condition.ebay("Top", "NewCondition") == ("new", "Neu") and condition.ebay(None, "Unknown") == (None, None)


def test_vinted_catalog_and_item():
    first, second = vi.parse_catalog((FIXTURES / "vinted_catalog.rsc").read_text(encoding="utf-8")).ads[:2]
    assert (first.item_condition, first.item_condition_label) == ("new", "Neu, mit Etikett")
    assert (second.item_condition, second.item_condition_label) == ("used", "Sehr gut")
    d = vi.parse_item((FIXTURES / "vinted_item.rsc").read_text(encoding="utf-8"), "10341874098")
    assert (d.item_condition, d.item_condition_label) == ("new", "Neu, mit Etikett")
    # clothes put the size first: only the condition is the label
    assert condition.vinted_listing({"secondLine": "M · Neu, mit Etikett"}) == ("new", "Neu, mit Etikett")
    assert condition.vinted_listing({"secondLine": "42 · Sehr gut"}) == ("used", "Sehr gut")
    assert condition.vinted_listing({"secondLine": "Zara"}) == (None, None)
    assert condition.vinted_listing({"secondLine": "Zara · M"}) == (None, None)


def test_shpock_item_page_only():
    d = sh.parse_item_page((FIXTURES / "shpock_item.html").read_text(encoding="utf-8"), "asu6-t2hxq7bDRFz")
    assert (d.item_condition, d.item_condition_label) == ("used", "Gut") and d.description.endswith("Zustand: Gut")
    assert {a.item_condition for a in sh.parse_search(load("shpock_search.json")).ads} == {None}   # isNew is not read
    # live 2026-10-14: an ad titled "Neu" whose seller picked no condition (isNew false): unknown, not guessed
    neu = sh.parse_item_page((FIXTURES / "shpock_item_neu.html").read_text(encoding="utf-8"), "asuXd_pn-ixnBBF2")
    assert neu.heading == "Iphone 15 Pro Max Neu" and neu.item_condition is None and neu.seller_name == "Anna B."
    assert condition.shpock([{"name": "condition", "value": "as_good_as_new", "valueLabel": "Wie neu"}]) == ("like_new", "Wie neu")
    assert condition.shpock([{"name": "condition", "value": "mystery", "valueLabel": "Akzeptabel"}]) == ("used", "Akzeptabel")
    assert condition.shpock([{"name": "condition", "value": "new"}]) == ("new", "Neu")


def test_kleinanzeigen_list_and_ad():
    assert {a.item_condition for a in ka.parse_ads(load("kleinanzeigen_ads.json")).ads} == {None}   # Fahrräder: no Zustand
    # live 2026-10-14, Handy & Telefon: attribute handy_telekom.condition, codes new / like_new / ok / alright / defect
    page = ka.parse_ads(load("kleinanzeigen_ads_handy.json"))
    assert [(a.item_condition, a.item_condition_label) for a in page.ads] == [
        ("new", "Neu"), ("like_new", "Sehr Gut"), ("used", "Gut"), ("used", "In Ordnung"), ("defect", "Defekt"),
        (None, None)]                                   # handy_telekom.battery_condition is not the item's condition
    d = ka.parse_ad(load("kleinanzeigen_ad_handy.json"))
    assert d.ad_id == "3540363264" and (d.item_condition, d.item_condition_label) == ("new", "Neu")
    assert ka.parse_ad(load("kleinanzeigen_ad.json")).item_condition is None
    other = {"attributes": {"attribute": {"name": "mode.condition_s", "localized-label": "Zustand",
                                          "value": {"value": "like_new", "localized-label": "Sehr Gut"}}}}
    assert condition.kleinanzeigen(other) == ("like_new", "Sehr Gut")
    by_label = {"attributes": {"attribute": [{"name": "x.zustand", "localized-label": "Zustand",
                                              "value": [{"value": "?", "localized-label": "Gebraucht"}]}]}}
    assert condition.kleinanzeigen(by_label) == ("used", "Gebraucht")


def test_preisjaeger_is_inferred():
    page = pj.parse_search((FIXTURES / "preisjaeger_search.json").read_text(encoding="utf-8"))
    assert {(a.item_condition, a.item_condition_label) for a in page.ads} == {("new", None)}
    assert condition.preisjaeger("Apple iPad Air refurbished") == ("refurbished", None)
    assert condition.preisjaeger("Ersatzteil-Set für Akkuschrauber") == ("new", None)
    assert condition.preisjaeger("Vorführgerät Dyson V15") == ("like_new", None)
    assert condition.preisjaeger("PS5 gebraucht bei Willhaben") == ("used", None)


def test_shown_and_tone():
    set_request_lang("de")
    try:
        assert condition.shown("used", "Sehr gut", "vinted") == "Sehr gut"
        assert condition.shown("new", None, "preisjaeger") == "Neu (Shop-Deal)"
        assert condition.shown("like_new") == "Neuwertig" and condition.shown("defect") == "Defekt"
        assert condition.shown(None, "Top") is None
    finally:
        set_request_lang("en")
    assert condition.label("refurbished") == "Refurbished" and condition.label(None) == ""
    assert (condition.tone("new"), condition.tone("defect"), condition.tone("used"), condition.tone(None)) == ("brand", "bad", "", "")


def test_detail_from_a_hit_or_a_result_keeps_it():
    h = SimpleNamespace(ad_id="1", heading="x", body=None, price=10.0, price_display="€ 10", published_at=None,
                        end_date=None, url="u", image_url=None, location=None, is_private=True, platform="shpock",
                        item_condition="used", item_condition_label="Gut")
    d = AdDetail.from_hit(h)
    assert (d.item_condition, d.item_condition_label) == ("used", "Gut") and d.as_context()["item_condition"] == "used"
    r = AdDetail.from_result({"ad_id": "2", "title": "y", "platform": "vinted", "item_condition": "new",
                              "item_condition_label": "Neu, mit Etikett",
                              "published_at": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()})
    assert (r.item_condition, r.item_condition_label) == ("new", "Neu, mit Etikett")
    assert AdDetail.from_result({"ad_id": "3"}).item_condition is None
