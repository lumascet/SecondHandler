"""Publishing from the single-draft page, manual category picker and the shipping (PayLivery) step.

All willhaben calls are fakes. The shipping step's paths/bodies come from the bbx-adin bundle and are NOT verified
live (needs live verification)."""
from types import SimpleNamespace

import pytest

from app import db as dbmod
from app.services import ad_creator
from app.willhaben import insert
from tests.test_dashboard import client  # noqa: F401  (pytest fixture)
from tests.conftest import wait_jobs

TREE = {
    "categoryNode": {"label": "Marktplatz", "code": "ROOT", "children": [
        {"label": "Audio", "code": "AUDIO", "children": [
            {"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER",
             "attributeReferences": [{"code": "Zustand", "required": True}, {"code": "Uebergabe", "required": True}]},
            {"label": "Kopfhörer", "code": "HEADPHONES", "attributeReferences": [{"code": "Zustand", "required": True}]},
        ]},
        {"label": "Haushalt", "code": "HOME", "children": [{"label": "Lautsprecherboxen Deko", "code": "DECO"}]},
    ]},
    "attributes": [{"treeAttributeElement": {"code": "Zustand", "label": "Zustand"},
                    "values": [{"code": "neu", "label": "Neu"}, {"code": "gebraucht", "label": "Gebraucht"}]}],
}
FORM = {"categoryTree": TREE, "ad": {"postCode": "1150", "location": "Wien", "firstName": "Max", "surName": "Muster",
                                     "contextLinkList": {"contextLink": [
                                         {"id": "presave", "relativePath": "/bap/presave", "serviceName": "iad"}]}}}
SUGGEST = {"categories": [{"name": "Audio", "code": "AUDIO", "categories": [{"name": "Kopfhörer", "code": "HEADPHONES"}]}]}

LINKS = {"contextLink": [{"id": "imageList", "relativePath": "/atz/123/images", "serviceName": "iad"},
                         {"id": "selfLinkSave", "relativePath": "/bap/save", "serviceName": "iad"}]}
# made-up relative path: the real p2ppUserOptions link only exists on a live ad (needs live verification)
P2PP = {"id": "p2ppUserOptions", "relativePath": "/restapi/v2/p2pp/options/user/123", "serviceName": "p2p-aggregator"}
OPTIONS = {"deliveryOptions": [
    {"uuid": "opt-s-post", "parcelSize": {"size": "S"}, "carrier": {"description": "Post"}, "price": 4.5, "shippingAddOns": []},
    {"uuid": "opt-m-post", "parcelSize": {"size": "M"}, "carrier": {"description": "Post"}, "price": 6.9, "shippingAddOns": []},
    {"uuid": "opt-m-dpd", "parcelSize": {"size": "M"}, "carrier": {"description": "dpd"}, "price": 5.9, "shippingAddOns": []},
], "selectedDeliveryOptions": [], "autoSelectPayLivery": False}


# what GET bap/{id} returns after saving: server-side state (photos) the composed template does not have
SERVER_AD = {"adId": 123, "adUuid": "uuid-1", "heading": "Logitech S125i", "contextLinkList": LINKS,
             "advertImageList": {"advertImage": [{"id": 77, "reference": "ref-01.jpg"}]},
             "treeAttributes": [{"attributeNode": {"code": "Zustand"}, "selectedAttributeValueNodes": [{"code": "gebraucht"}]},
                                {"attributeNode": {"code": "Uebergabe"}, "selectedAttributeValueNodes": [{"code": "PICKUP"}]}]}


class FakeSession:
    def __init__(self, options=None, total_price=0):
        self.calls, self.options, self.total = [], options if options is not None else OPTIONS, total_price

    async def api_json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/presave"):
            return {"adId": 123, "adUuid": "uuid-1", "contextLinkList": LINKS, "productId": 67}
        if path.endswith("/bap/save"):
            return {"adId": body["adId"], "adUuid": body["adUuid"]}
        if method == "GET" and path == "/webapi/iad/bap/123":      # the ad willhaben holds after saving
            return {**SERVER_AD, "treeAttributes": list(SERVER_AD["treeAttributes"])}
        if path == "/webapi/iad/bap/edit":
            return {"adId": 123, "adUuid": "uuid-1", "contextLinkList": {"contextLink": LINKS["contextLink"] + [P2PP]}}
        if path == "/webapi/p2p-aggregator/restapi/v2/p2pp/options/user/123":
            return self.options
        if path == "/webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery":
            return {}
        if path.endswith("/carts"):
            return {"shoppingCartId": "c9", "status": "Open"}
        if path.endswith("/items"):
            return {"totalPrice": self.total}
        if path.endswith("/checkout"):
            return {"status": "ACTIVATED"}
        raise AssertionError(path)

    async def upload(self, path, field, name, data, mime):
        self.calls.append(("UPLOAD", path, name))
        return SimpleNamespace(ok=True, status=200, text="", json=lambda: {"reference": f"ref-{name}"})


def _ad(handover="pickup"):
    return insert.compose_ad(FORM["ad"], heading="Logitech S125i", description="Gut.", price=12.0,
                             path=[{"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER"}],
                             attrs={"Zustand": ("gebraucht", "Gebraucht")}, attr_labels={"Zustand": "Zustand"},
                             handover=handover)


def _handover(body):
    return [a for a in body["treeAttributes"] if a["attributeNode"]["code"] == "Uebergabe"]


# ------------------------------------------------------------------ insert: shipping step (fakes)
async def test_paylivery_step_between_save_and_cart():
    s = FakeSession()
    res = await insert.publish(s, _ad("both"), [], shipping={"handover": "both", "paylivery": True, "size": "M"})
    steps = [(m, p) for m, p, *_ in s.calls]
    assert steps == [("POST", "/webapi/iad/bap/presave"), ("POST", "/webapi/iad/bap/save"),
                     ("GET", "/webapi/iad/bap/123"), ("PUT", "/webapi/iad/bap/edit"),
                     ("GET", "/webapi/p2p-aggregator/restapi/v2/p2pp/options/user/123"),
                     ("PUT", "/webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery"),
                     ("POST", "/webapi/iad/products/privates/atz/uuid-1/carts"),
                     ("PUT", "/webapi/iad/products/privates/carts/c9/items"),
                     ("POST", "/webapi/iad/products/privates/carts/c9/checkout")]
    edit = s.calls[3][2]
    assert _handover(edit) == [{"attributeNode": {"label": "Übergabe", "code": "Uebergabe"},
                                "selectedAttributeValueNodes": [{"label": "Selbstabholung", "code": "PICKUP"},
                                                                {"label": "Versand", "code": "Versand"}]}]
    assert edit["adId"] == 123 and edit["heading"] == "Logitech S125i"
    # the edit body is willhaben's own ad (photos kept), only Uebergabe replaced
    assert edit["advertImageList"] == SERVER_AD["advertImageList"] and "firstName" not in edit
    assert [a["attributeNode"]["code"] for a in edit["treeAttributes"]] == ["Zustand", "Uebergabe"]
    assert s.calls[5][2] == {"adId": 123, "deliveryOptions": ["opt-m-dpd"]}      # cheapest M
    assert _handover(s.calls[0][2])[0]["selectedAttributeValueNodes"][1]["code"] == "Versand"   # presave too
    assert res["trace"]["delivery"]["option"] == {"size": "M", "carrier": "dpd", "price": 5.9}


async def test_paylivery_carrier_choice_and_shipping_without_paylivery():
    s = FakeSession()
    await insert.publish(s, _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": True,
                                                           "size": "M", "carrier": "Post"})
    assert ("PUT", "/webapi/p2p-aggregator/restapi/v2/p2pp/options/delivery",
            {"adId": 123, "deliveryOptions": ["opt-m-post"]}) in s.calls

    s = FakeSession()
    await insert.publish(s, _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": False})
    paths = [p for _, p, *_ in s.calls]
    assert not any("/options/user/" in p for p in paths)
    assert s.calls[3][1] == "/webapi/iad/bap/edit"
    assert _handover(s.calls[3][2])[0]["selectedAttributeValueNodes"] == [{"label": "Versand", "code": "Versand"}]
    assert s.calls[4][2] == {"adId": 123, "deliveryOptions": []}
    assert paths[-1].endswith("/checkout")


@pytest.mark.parametrize("options,size,match", [
    ({**OPTIONS, "sellerOnboardingDTO": {"x": 1}}, "S", "noch nicht eingerichtet"),
    (OPTIONS, "XS", r"keine Versandoption für Größe XS \(angeboten: M, S\)"),
])
async def test_paylivery_problems_stop_before_cart(options, size, match):
    s = FakeSession(options=options)
    with pytest.raises(insert.InsertError, match=match):
        await insert.publish(s, _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": True, "size": size})
    assert not any(p.endswith(("/carts", "/delivery", "/checkout")) for _, p, *_ in s.calls)


async def test_money_guard_still_applies_with_shipping():
    s = FakeSession(total_price=1.5)
    with pytest.raises(insert.InsertError, match="nichts bezahlt"):
        await insert.publish(s, _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": True, "size": "S"})
    assert not any(p.endswith("/checkout") for _, p, *_ in s.calls)


@pytest.mark.parametrize("shipping", [None, {"handover": "pickup"}])
async def test_pickup_path_unchanged(shipping):
    s = FakeSession()
    await insert.publish(s, _ad(), [], shipping=shipping)
    assert [p for _, p, *_ in s.calls] == ["/webapi/iad/bap/presave", "/webapi/iad/bap/save",
                                           "/webapi/iad/products/privates/atz/uuid-1/carts",
                                           "/webapi/iad/products/privates/carts/c9/items",
                                           "/webapi/iad/products/privates/carts/c9/checkout"]
    assert _handover(s.calls[0][2])[0]["selectedAttributeValueNodes"] == [{"label": "Selbstabholung", "code": "PICKUP"}]


async def test_stop_after_delivery():
    s = FakeSession()
    res = await insert.publish(s, _ad("shipping"), [], stop_after="delivery",
                               shipping={"handover": "shipping", "paylivery": True, "size": "S"})
    assert res["stopped"] == "delivery" and s.calls[-1][1].endswith("/options/delivery")


# ------------------------------------------------------------------ insert: category search, manual option, cache
def test_search_categories_and_manual_option():
    hits = insert.search_categories(TREE, "lautsprecher")
    assert [h["code"] for h in hits] == ["MOBILE_SPEAKER", "DECO"]
    assert hits[0] == {"code": "MOBILE_SPEAKER", "label": "Mobile Lautsprecher", "path": "Audio › Mobile Lautsprecher"}
    assert insert.search_categories(TREE, "audio") == [
        {"code": "HEADPHONES", "label": "Kopfhörer", "path": "Audio › Kopfhörer"},
        {"code": "MOBILE_SPEAKER", "label": "Mobile Lautsprecher", "path": "Audio › Mobile Lautsprecher"}]
    assert insert.search_categories(TREE, " ") == []

    opt = insert.manual_option(FORM, "MOBILE_SPEAKER", condition="Gebraucht", attributes=None)
    assert opt["manual"] and opt["label"] == "Audio › Mobile Lautsprecher"
    assert opt["path"] == [{"label": "Audio", "code": "AUDIO"}, {"label": "Mobile Lautsprecher", "code": "MOBILE_SPEAKER"}]
    assert [f["code"] for f in opt["fields"]] == ["Zustand"] and opt["fields"][0]["selected"] == "gebraucht"
    assert insert.manual_option(FORM, "AUDIO", condition=None, attributes=None) is None     # not a leaf
    assert insert.manual_option(FORM, "NOPE", condition=None, attributes=None) is None


def test_hint_option_from_claudes_category_hint():
    kw = {"condition": "Gebraucht", "attributes": None}
    opt = insert.hint_option(FORM, "Marktplatz › Audio > Mobile Lautsprecher", **kw)
    assert opt["label"] == "Audio › Mobile Lautsprecher" and "manual" not in opt
    assert opt["fields"][0]["selected"] == "gebraucht"
    assert insert.hint_option(FORM, "Elektronik > Kopfhörer", **kw)["path"][-1]["code"] == "HEADPHONES"  # leaf name alone
    assert insert.hint_option(FORM, "Audio", **kw) is None                                       # not a leaf
    assert insert.hint_option(FORM, "KFZ-Zubehör/Motorradteile > Ladestationen/Wallboxen", **kw) is None
    assert insert.hint_option(FORM, None, **kw) is None


def test_hint_in_claudes_own_words_finds_willhabens_category():
    """10-30: Claude wrote "Auto/Motorrad > Autozubehör > Elektromobilität/Ladestationen", willhaben calls it
    "KFZ-Zubehör/Motorradteile > E-Mobilität > Ladestationen/Wallboxen": nothing was assigned, the owner searched."""
    tree = {"categoryNode": {"label": "Marktplatz", "code": "ROOT", "children": [
        {"label": "KFZ-Zubehör/Motorradteile", "code": "KFZ", "children": [
            {"label": "E-Mobilität", "code": "EMOB", "children": [
                {"label": "Ladestationen/Wallboxen", "code": "WALLBOX"}, {"label": "Ladekabel", "code": "CABLE"}]},
            {"label": "Autoteile", "code": "PARTS"}]},
        {"label": "Haushalt", "code": "HOME", "children": [{"label": "Zubehör", "code": "HOME_ACC"}]},
        {"label": "Foto", "code": "FOTO", "children": [{"label": "Zubehör", "code": "FOTO_ACC"}]},
    ]}, "attributes": []}
    kw = {"condition": None, "attributes": None}
    hint = "Auto/Motorrad > Autozubehör > Elektromobilität/Ladestationen"
    assert insert.hint_option({"categoryTree": tree}, hint, **kw)["path"][-1]["code"] == "WALLBOX"
    assert insert.hint_option({"categoryTree": tree}, "Sonstiges > Zubehör", **kw) is None      # two alike: no guess
    assert insert.hint_option({"categoryTree": tree}, "Garten > Rasenmäher", **kw) is None

    # the search: every result (not the first 25), and the closest ones when no category has every word
    assert [h["code"] for h in insert.search_categories(tree, "Elektromobilität Ladestationen")] == ["WALLBOX"]
    assert len(insert.search_categories(tree, "zubehör")) == 5                  # every leaf under KFZ-Zubehör too


async def test_prepare_falls_back_to_the_category_hint(wh):
    wh.suggest = {"categories": []}                    # live: willhaben suggested nothing for a wallbox
    d = _draft(category_hint="Audio > Mobile Lautsprecher")
    plan = (await ad_creator.prepare_willhaben(d.id)).wh_plan
    assert [o["path"][-1]["code"] for o in plan["options"]] == ["MOBILE_SPEAKER"]
    d = _draft(category_hint="Garten > Rasenmäher")
    assert (await ad_creator.prepare_willhaben(d.id)).wh_plan["options"] == []
    wh.suggest = SUGGEST                               # willhaben's own suggestion wins over the hint
    d = _draft(category_hint="Audio > Mobile Lautsprecher")
    assert [o["path"][-1]["code"] for o in (await ad_creator.prepare_willhaben(d.id)).wh_plan["options"]] == ["HEADPHONES"]


async def test_form_cache(monkeypatch):
    calls = []

    async def load(s):
        calls.append(1)
        return FORM
    monkeypatch.setattr(insert, "load_form", load)
    insert.clear_form_cache()
    assert await insert.cached_form(None) is FORM and await insert.cached_form(None) is FORM
    assert len(calls) == 1
    await insert.cached_form(None, max_age=0)
    assert len(calls) == 2
    insert.clear_form_cache()


# ------------------------------------------------------------------ routes (fake willhaben)
@pytest.fixture
def wh(monkeypatch):
    """Fake logged-in willhaben for ad_creator: form, suggestions and a recording insert.publish."""
    state = SimpleNamespace(published=[], suggest=SUGGEST, fail=None, fail_after_delivery=None)

    async def load(s):
        if state.fail:
            raise RuntimeError(state.fail)
        return FORM

    async def suggest(s, title):
        return state.suggest

    async def publish(s, ad, photos, existing_ad_id=None, on_presaved=None, stop_after=None, shipping=None,
                      on_delivery=None, on_step=None):
        state.published.append({"ad": ad, "shipping": shipping, "existing": existing_ad_id})
        if on_step:
            on_step("presave")
        on_presaved("1000000001")
        if shipping and on_delivery:
            on_delivery()
        if state.fail_after_delivery:
            raise insert.InsertError(state.fail_after_delivery)
        return {"ad_id": "1000000001", "ad_uuid": "u", "url": "https://www.willhaben.at/iad/object?adId=1000000001"}

    async def no_import():
        return set()

    insert.clear_form_cache()
    monkeypatch.setattr(insert, "load_form", load)
    monkeypatch.setattr(insert, "suggest_categories", suggest)
    monkeypatch.setattr(insert, "publish", publish)
    monkeypatch.setattr("app.willhaben.session.get_session", lambda: object())
    monkeypatch.setattr("app.services.my_ads.import_from_account", no_import)
    yield state
    insert.clear_form_cache()


def _draft(**kw):
    with dbmod.session() as db:
        d = dbmod.AdDraft(input_text="x", title="Logitech S125i", description="Gut erhalten.", price_suggestion=12,
                          condition="Gebraucht", **kw)
        db.add(d)
        db.commit()
        db.refresh(d)
    return d


def _get(draft_id):
    with dbmod.session() as db:
        return db.get(dbmod.AdDraft, draft_id)


def _batch_of(*drafts):
    with dbmod.session() as db:
        batch = dbmod.AdBatch(status="review", groups=[{"id": f"g{i}", "photos": [], "draft_id": d.id}
                                                       for i, d in enumerate(drafts)])
        db.add(batch)
        db.commit()
        db.refresh(batch)
    return batch.id


def test_single_page_sections(client, wh):  # noqa: F811
    d = _draft()
    page = client.get(f"/new-ad/{d.id}").text
    assert f'action="/new-ad/{d.id}/willhaben/prepare"' in page and "Insert manually" not in page
    assert "/willhaben/publish" not in page

    r = client.post(f"/new-ad/{d.id}/willhaben/prepare", data={"category_query": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#willhaben")
    page = client.get(f"/new-ad/{d.id}").text
    assert "Audio › Kopfhörer" in page and 'name="o0_Zustand"' in page and "Publish on willhaben now" in page
    assert 'name="handover" value="pickup" checked' in page and "Choose another category" in page
    assert f'data-pick="/new-ad/{d.id}/willhaben/category"' in page and 'name="parcel_option" value="S|dpd" checked' in page

    wh.fail = "nicht eingeloggt"
    insert.clear_form_cache()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    assert "willhaben form not reachable: nicht eingeloggt" in client.get(f"/new-ad/{d.id}").text


def test_category_search_endpoint(client, wh):  # noqa: F811
    r = client.get("/api/willhaben/categories", params={"q": "lautsprecher"}).json()
    assert [x["code"] for x in r["results"]] == ["MOBILE_SPEAKER", "DECO"]
    assert client.get("/api/willhaben/categories", params={"q": "l"}).json() == {"results": []}
    wh.fail = "nicht eingeloggt"
    insert.clear_form_cache()
    r = client.get("/api/willhaben/categories", params={"q": "audio"})
    assert r.status_code == 503 and "nicht eingeloggt" in r.json()["error"]


def test_manual_category_saved_and_published(client, wh):  # noqa: F811
    d = _draft()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    client.post(f"/new-ad/{d.id}/willhaben/category", data={"code": "MOBILE_SPEAKER"})
    plan = _get(d.id).wh_plan
    assert [o["label"] for o in plan["options"]] == ["Audio › Kopfhörer", "Audio › Mobile Lautsprecher"]
    assert plan["selected"] == 1 and plan["options"][1]["manual"]
    assert plan["options"][1]["fields"][0]["selected"] == "gebraucht"
    page = client.get(f"/new-ad/{d.id}").text
    assert 'name="option" value="1" checked' in page and "chosen by hand" in page

    client.post(f"/new-ad/{d.id}/willhaben/category", data={"code": "MOBILE_SPEAKER"})   # same again: no duplicate
    assert len(_get(d.id).wh_plan["options"]) == 2
    client.post(f"/new-ad/{d.id}/willhaben/category", data={"code": "AUDIO"})
    assert "cannot be chosen" in _get(d.id).error

    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})                         # re-prepare keeps the manual one
    assert [o["path"][-1]["code"] for o in _get(d.id).wh_plan["options"]] == ["HEADPHONES", "MOBILE_SPEAKER"]

    r = client.post(f"/new-ad/{d.id}/willhaben/publish", follow_redirects=False,
                    data={"option": "1", "o1_Zustand": "neu", "o0_Zustand": "gebraucht", "handover": "pickup",
                          "title": "Logitech S125i Lautsprecher", "description": "Gut erhalten.", "price": "15"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    wait_jobs()
    sent = wh.published[-1]
    assert sent["shipping"] is None                                                   # pickup: verified path
    assert sent["ad"]["categoryPath"]["path"][-1]["code"] == "MOBILE_SPEAKER"
    assert sent["ad"]["heading"] == "Logitech S125i Lautsprecher" and sent["ad"]["price"] == 15
    zustand = [a for a in sent["ad"]["treeAttributes"] if a["attributeNode"]["code"] == "Zustand"]
    assert zustand[0]["selectedAttributeValueNodes"] == [{"label": "Neu", "code": "neu"}]
    d2 = _get(d.id)
    assert (d2.status, d2.wh_ad_id, d2.error) == ("published", "1000000001", None)

    page = client.get(f"/new-ad/{d.id}").text
    assert "https://www.willhaben.at/iad/object?adId=1000000001" in page and 'bg-ok-soft text-success' in page
    assert "/willhaben/publish" not in page

    client.post(f"/new-ad/{d.id}/willhaben/publish", data={"option": "1"})               # never twice
    wait_jobs()
    assert len(wh.published) == 1 and "Schon veröffentlicht" in _get(d.id).error


def test_publish_with_shipping_from_form(client, wh):  # noqa: F811
    d = _draft()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={"option": "0", "handover": "both", "paylivery": "1",
                                                           "parcel_size": "m", "parcel_carrier": "Hermes"})
    wait_jobs()
    sent = wh.published[-1]
    assert sent["shipping"] == {"handover": "both", "paylivery": True, "size": "M", "carrier": "Hermes"}
    ho = [a for a in sent["ad"]["treeAttributes"] if a["attributeNode"]["code"] == "Uebergabe"]
    assert [n["code"] for n in ho[0]["selectedAttributeValueNodes"]] == ["PICKUP", "Versand"]
    assert _get(d.id).wh_plan["shipping"]["handover"] == "both"


def test_parse_shipping_defaults():
    assert ad_creator.parse_shipping({}) == {"handover": "pickup"}
    assert ad_creator.parse_shipping({"handover": "bogus", "paylivery": "1"}) == {"handover": "pickup"}
    assert ad_creator.parse_shipping({"handover": "shipping", "parcel_size": "XXL", "parcel_carrier": "UPS"}) == {
        "handover": "shipping", "paylivery": False, "size": None, "carrier": None}


async def test_paylivery_needs_size(wh):
    d = _draft()
    await ad_creator.prepare_willhaben(d.id)
    with pytest.raises(insert.InsertError, match="Paketgröße"):
        await ad_creator.publish_willhaben(d.id, 0, {}, shipping={"handover": "shipping", "paylivery": True, "size": None})
    assert wh.published == []


def test_batch_table_picker_category_and_publish(client, wh):  # noqa: F811
    d = _draft()
    bid = _batch_of(d)
    url = f"/new-ad/batch/{bid}/drafts"
    wh.suggest = {"categories": []}
    client.post(url, data={"act": f"prepare:{d.id}"})
    wait_jobs()
    row = client.get(f"/new-ad/batch/{bid}/row/{d.id}").text
    assert "willhaben suggests no category" not in row and "Choose another category" in row and "– no suggestion –" in row
    assert "No category found for this draft" in row                                 # the click visibly did something
    assert f'data-pick="{url}?act=category:{d.id}"' in row and f'data-target="#row-{d.id}"' in row
    assert f'name="{d.id}.f:' not in row                                            # nothing to choose yet

    r = client.post(f"{url}?act=category:{d.id}",
                    data={"code": "MOBILE_SPEAKER", f"{d.id}.title": "Neu", f"{d.id}.description": "Text"})
    assert f'<tbody id="row-{d.id}"' in r.text and f'name="{d.id}.f:MOBILE_SPEAKER:Zustand"' in r.text
    assert _get(d.id).title == "Neu"                                                 # unsaved edits came along

    r = client.post(url, data={f"{d.id}.title": "Neu", f"{d.id}.description": "Text", f"{d.id}.price": "12",
                               f"{d.id}.cat": "MOBILE_SPEAKER", f"{d.id}.f:MOBILE_SPEAKER:Zustand": "gebraucht",
                               f"{d.id}.handover": "shipping", "sel": str(d.id), "act": "publish"})
    assert f'data-pending-key="addraft:{d.id}"' in r.text and f'data-refresh="/new-ad/batch/{bid}/row/{d.id}?checked=' in r.text
    assert f'name="{d.id}.title"' not in r.text                                      # nothing to edit while queued
    wait_jobs()
    row = client.get(f"/new-ad/batch/{bid}/row/{d.id}").text
    assert "willhaben.at/iad/object?adId=1000000001" in row and "data-pending-key" not in row
    assert wh.published[-1]["shipping"] == {"handover": "shipping", "paylivery": False, "size": None, "carrier": None}


async def test_delivery_step_aborts_when_saved_ad_unreadable():
    class NoAd(FakeSession):
        async def api_json(self, method, path, body=None):
            if method == "GET" and path == "/webapi/iad/bap/123":
                self.calls.append((method, path, body))
                return {}
            return await super().api_json(method, path, body)
    s = NoAd()
    with pytest.raises(insert.InsertError, match="nicht lesbar"):
        await insert.publish(s, _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": False})
    assert not any(p.endswith(("/edit", "/delivery", "/carts")) for _, p, *_ in s.calls)


async def test_on_delivery_called_only_after_delivery_step():
    hits = []
    await insert.publish(FakeSession(), _ad("shipping"), [], shipping={"handover": "shipping", "paylivery": False},
                         on_delivery=lambda: hits.append(1))
    await insert.publish(FakeSession(), _ad(), [], on_delivery=lambda: hits.append(2))
    assert hits == [1]


async def test_retry_to_pickup_after_delivery_set_is_refused(wh):
    d = _draft()
    await ad_creator.prepare_willhaben(d.id)
    wh.fail_after_delivery = "Warenkorb kaputt"
    with pytest.raises(insert.InsertError, match="Warenkorb"):
        await ad_creator.publish_willhaben(d.id, 0, {}, shipping={"handover": "shipping", "paylivery": True, "size": "S"})
    assert _get(d.id).wh_plan["delivery_set"] is True and _get(d.id).wh_ad_id == "1000000001"
    wh.fail_after_delivery = None
    with pytest.raises(insert.InsertError, match="schon eingerichtet"):
        await ad_creator.publish_willhaben(d.id, 0, {}, shipping={"handover": "pickup"})
    assert len(wh.published) == 1
    await ad_creator.publish_willhaben(d.id, 0, {}, shipping={"handover": "shipping", "paylivery": True, "size": "S"})
    assert wh.published[-1]["existing"] == "1000000001" and _get(d.id).status == "published"


def test_parse_shipping_paylivery_strings():
    for v, want in (("false", False), ("0", False), ("", False), (None, False), ("on", True), ("1", True),
                    (True, True), ("TRUE", True)):
        assert ad_creator.parse_shipping({"handover": "shipping", "paylivery": v})["paylivery"] is want, v


@pytest.mark.parametrize("shipping", ["pickup", ["shipping"], 3])
def test_api_publish_rejects_non_object_shipping(client, wh, shipping):  # noqa: F811
    d = _draft()
    r = client.post(f"/api/ad-drafts/{d.id}/willhaben-publish", json={"shipping": shipping})
    assert r.status_code == 400 and "shipping must be an object" in r.text
    assert wh.published == []


# ------------------------------------------------------------------ pieces and parcel estimate go to My ads
PARCEL = {"length_cm": 40, "width_cm": 30, "height_cm": 20, "weight_kg": 3.0, "contents": "Stuhl", "source": "claude"}


def _my_ad(ad_id="1000000001"):
    with dbmod.session() as db:
        return db.get(dbmod.MyAd, ad_id)


def test_publish_hands_pieces_and_parcel_to_my_ads(client, wh):  # noqa: F811
    d = _draft(quantity=4, parcel=PARCEL)
    page = client.post(f"/new-ad/{d.id}/willhaben/prepare", data={}, follow_redirects=True).text
    assert 'hx-include="#title, #desc, #price, #quantity"' in page

    client.post(f"/new-ad/{d.id}/willhaben/publish",
                data={"option": "0", "o0_Zustand": "gebraucht", "handover": "pickup", "title": "Stuhl", "quantity": "6",
                      "description": "Gut erhalten.", "price": "15"})
    wait_jobs()
    assert _get(d.id).quantity == 6                                       # the unsaved count went along
    row = _my_ad()
    assert row.parcel == PARCEL and row.parcel["source"] == "claude"      # an estimate: the shipping card says so
    assert row.published_at is not None and row.status in ("unknown", "active")
    if "quantity" in dbmod.MyAd.model_fields:                             # MyAd.quantity comes with the stock change
        assert row.quantity == 6

    page = client.get(f"/new-ad/{d.id}").text
    assert 'href="/myads?ad=1000000001"' in page and 'href="/sold/1000000001?kind=sold"' in page

    # shipping takes the ad's parcel over as it is: still an estimate, so automatic buying holds back
    from app.services import shipping
    estimate = dbmod.Shipment(conversation_id="c1", ad_id=row.ad_id, parcel=row.parcel)
    assert "the parcel size is only an estimate" in shipping.auto_blockers(estimate)
    confirmed = dbmod.Shipment(conversation_id="c1", ad_id=row.ad_id, parcel={**row.parcel, "source": "user"})
    assert "the parcel size is only an estimate" not in shipping.auto_blockers(confirmed)


def test_publish_without_pieces_or_parcel_is_as_before(client, wh):  # noqa: F811
    d = _draft()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={"option": "0", "o0_Zustand": "gebraucht",
                                                           "handover": "pickup"})
    wait_jobs()
    row = _my_ad()
    assert row.parcel is None and row.published_at is not None
    if "quantity" in dbmod.MyAd.model_fields:
        assert row.quantity is None


async def test_publish_fills_the_gaps_of_an_ad_the_import_created_first(wh):
    from datetime import datetime, timezone
    imported = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with dbmod.session() as db:
        db.add(dbmod.MyAd(ad_id="1000000001", heading="vom Import", status="active", published_at=imported))
        db.commit()
    d = _draft(quantity=3, parcel=PARCEL)
    await ad_creator.prepare_willhaben(d.id)
    await ad_creator.publish_willhaben(d.id, 0, {}, shipping={"handover": "pickup"})
    row = _my_ad()
    assert row.heading == "vom Import" and row.status == "active"        # the import's data stays
    assert row.published_at.replace(tzinfo=timezone.utc) == imported
    assert row.parcel == PARCEL
    if "quantity" in dbmod.MyAd.model_fields:
        assert row.quantity == 3


def test_batch_row_links_to_the_published_ad(client, wh):  # noqa: F811
    d, marked = _draft(), _draft()                                       # marked: "published" without a willhaben ad id
    bid = _batch_of(d, marked)
    ad_creator.update_draft(d.id, status="published", wh_ad_id="1000000001")
    ad_creator.update_draft(marked.id, status="published")
    row = client.get(f"/new-ad/batch/{bid}/row/{d.id}").text
    assert 'href="/myads?ad=1000000001"' in row and 'href="/sold/1000000001?kind=sold"' in row
    row = client.get(f"/new-ad/batch/{bid}/row/{marked.id}").text
    assert "/myads?ad=" not in row and "/sold/" not in row


def test_catalog_has_the_german_texts_of_the_new_links():
    from app import i18n
    de = i18n.catalog("de")
    assert de["My ad"] == "Meine Anzeige" and de["Pcs."] == "Stück" and "Mark sold…" in de


# ------------------------------------------------------------------ the price field: German formats, junk keeps the old price
@pytest.mark.parametrize("raw,want", [
    ("12", 12.0), ("12,50", 12.5), ("12.50", 12.5), ("1.200", 1200.0), ("1.200,50", 1200.5), ("1 200", 1200.0),
    ("12 €", 12.0), ("€ 12", 12.0), ("12€", 12.0), ("80 EUR", 80.0), ("80 Euro", 80.0), ("25,-", 25.0), ("25.-", 25.0),
    ("", None), ("  ", None), (None, None),
])
def test_parse_price_german_formats(raw, want):
    assert ad_creator.parse_price(raw) == want


@pytest.mark.parametrize("raw", ["abc", "12 Stück", "-5", "1,200", "12..5", "1.2.3"])
def test_parse_price_rejects_junk(raw):
    with pytest.raises(ValueError):
        ad_creator.parse_price(raw)


def test_unreadable_price_keeps_the_old_one(client, wh):  # noqa: F811
    from urllib.parse import unquote
    d = _draft()                                                              # price 12
    r = client.post(f"/new-ad/{d.id}", data={"title": "Neu", "description": "Text", "price": "zwölf"},
                    follow_redirects=False)
    msg = unquote(r.headers["location"])
    assert "Price not recognized" in msg and "zwölf" in msg and "Changes saved" not in msg
    d2 = _get(d.id)
    assert (d2.title, d2.description, d2.price_suggestion) == ("Neu", "Text", 12) and "zwölf" in d2.error

    r = client.post(f"/new-ad/{d.id}", data={"title": "Neu", "description": "Text", "price": "1.200,50"},
                    follow_redirects=False)
    assert "Changes saved" in unquote(r.headers["location"]) and _get(d.id).price_suggestion == 1200.5

    # publishing with a price that was not understood does not go out with the old one
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    r = client.post(f"/new-ad/{d.id}/willhaben/publish", follow_redirects=False,
                    data={"option": "0", "handover": "pickup", "title": "Neu", "description": "Text", "price": "abc"})
    assert r.status_code == 303 and "msg=" not in r.headers["location"]
    wait_jobs()
    assert wh.published == [] and "Price not recognized" in _get(d.id).error and _get(d.id).publishing is None


# ------------------------------------------------------------------ a published draft stays published while the ad is online
def _myad(ad_id, status):
    with dbmod.session() as db:
        row = db.get(dbmod.MyAd, ad_id) or dbmod.MyAd(ad_id=ad_id)
        row.status = status
        db.add(row)
        db.commit()


def test_batch_table_publishes_once_while_the_ad_is_online(client, wh):  # noqa: F811
    """The table publishes a draft once; a draft that went back to "draft" (old data) is not published again while its
    ad is online – only once My ads has seen it go, and then onto the same willhaben ad."""
    d = _draft()
    url = f"/new-ad/batch/{_batch_of(d)}/drafts"
    client.post(url, data={"act": f"prepare:{d.id}"})
    wait_jobs()
    publish = {f"{d.id}.title": "Logitech S125i", f"{d.id}.description": "Gut erhalten.", f"{d.id}.price": "12",
               f"{d.id}.handover": "pickup", "sel": str(d.id), "act": "publish"}
    client.post(url, data=publish)
    wait_jobs()
    assert (_get(d.id).status, _get(d.id).wh_ad_id) == ("published", "1000000001")
    with dbmod.session() as db:
        assert db.get(dbmod.MyAd, "1000000001").status == "unknown"        # just published, import not run yet
    assert "Nothing published" in client.post(url, data=publish).text and len(wh.published) == 1

    ad_creator.update_draft(d.id, status="draft")                            # reset by old data, the ad still online
    r = client.post(url, data=publish)
    assert "Nothing published" in r.text and "1000000001" in r.text and "/myads" in r.text
    _myad("1000000001", "active")
    client.post(url, data=publish)
    wait_jobs()
    assert _get(d.id).status == "draft" and len(wh.published) == 1

    _myad("1000000001", "deleted")                                           # gone from willhaben: publish again
    client.post(url, data=publish)
    wait_jobs()
    assert len(wh.published) == 2 and wh.published[-1]["existing"] == "1000000001"


def test_publish_refused_while_the_ad_is_online(client, wh):  # noqa: F811
    """Old data or a reset that slipped through: the publish itself checks My ads before touching willhaben."""
    d = _draft(wh_ad_id="1000000001")                                        # status draft, ad id of a live ad
    _myad("1000000001", "active")
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    r = client.post(f"/new-ad/{d.id}/willhaben/publish", data={"option": "0", "handover": "pickup"},
                    follow_redirects=False)
    assert r.status_code == 303 and "msg=" not in r.headers["location"]
    wait_jobs()
    err = _get(d.id).error
    assert wh.published == [] and "1000000001" in err and "/myads" in err and _get(d.id).publishing is None
    assert ad_creator.live_on_willhaben(_get(d.id)).status == "active"
    for status in ("expired", "sold", "deleted", "replaced"):
        _myad("1000000001", status)
        assert ad_creator.live_on_willhaben(_get(d.id)) is None, status


# ------------------------------------------------------------------ a published draft is read-only on its page
def test_published_draft_page_is_read_only(client, wh):  # noqa: F811
    from urllib.parse import unquote
    d = _draft()
    client.post(f"/new-ad/{d.id}/willhaben/prepare", data={})
    client.post(f"/new-ad/{d.id}/willhaben/publish", data={"option": "0", "handover": "pickup"})
    wait_jobs()
    page = client.get(f"/new-ad/{d.id}").text
    assert page.count(" readonly") == 4 and 'href="/myads/1000000001/edit"' in page
    assert f'action="/new-ad/{d.id}/refine"' not in page and 'form="draft-form"' not in page
    assert "/willhaben/publish" not in page and "Edit on willhaben" in page

    r = client.post(f"/new-ad/{d.id}/refine", data={"instruction": "kürzer"}, follow_redirects=False)
    assert "Already published" in unquote(r.headers["location"])
    wait_jobs()
    assert not any("kürzer" in c["prompt"] for c in client.fake.calls)
    r = client.post(f"/new-ad/{d.id}", data={"title": "Anders", "description": "Anders", "price": "99"},
                    follow_redirects=False)
    assert "Already published" in unquote(r.headers["location"]) and "Changes saved" not in r.headers["location"]
    d2 = _get(d.id)
    assert (d2.title, d2.price_suggestion) == ("Logitech S125i", 12)

    # a draft Claude still works on cannot be revised twice; an empty instruction is nothing to do
    d3 = _draft()
    ad_creator.update_draft(d3.id, refining=True)
    r = client.post(f"/new-ad/{d3.id}/refine", data={"instruction": "kürzer"}, follow_redirects=False)
    assert "Not revised" in unquote(r.headers["location"])
    ad_creator.update_draft(d3.id, refining=None)
    r = client.post(f"/new-ad/{d3.id}/refine", data={"instruction": "  "}, follow_redirects=False)
    assert "Nothing to revise" in unquote(r.headers["location"])


def test_batch_table_choices_survive_a_new_prepare_and_reach_willhaben(client, wh):  # noqa: F811
    """The batch table end to end against the fake willhaben: categories asked in the background, the choices saved
    per row (one category copied that the row lacked), asked again without losing them, then all published."""
    a, b = _draft(), _draft()
    ad_creator.update_draft(b.id, title="JBL Go 3")
    with dbmod.session() as db:
        batch = dbmod.AdBatch(status="review", groups=[{"id": "g1", "photos": [], "draft_id": a.id},
                                                       {"id": "g2", "photos": [], "draft_id": b.id}])
        db.add(batch)
        db.commit()
        db.refresh(batch)
    url = f"/new-ad/batch/{batch.id}/drafts"
    rows = {a.id: {"title": "Logitech S125i", "description": "Gut erhalten.", "price": "12"},
            b.id: {"title": "JBL Go 3", "description": "Gut erhalten.", "price": "12"}}
    ticks = {"sel": [str(a.id), str(b.id)]}

    def form(**extra):
        return {**{f"{i}.{k}": v for i, f in rows.items() for k, v in f.items()}, **ticks, **extra}

    assert "2 ads are being prepared" in client.post(url, data=form(act="prepare")).text
    wait_jobs()
    assert [o["path"][-1]["code"] for o in _get(a.id).wh_plan["options"]] == ["HEADPHONES"]

    rows[a.id] |= {"cat": "HEADPHONES", "f:HEADPHONES:Zustand": "neu", "handover": "both", "paylivery": "1",
                   "parcel_size": "M"}
    rows[b.id] |= {"cat": "MOBILE_SPEAKER", "handover": "pickup"}                 # copied from a row that has it
    r = client.post(url, data=form(act="save"))
    assert f'<tbody id="row-{b.id}"' in r.text and "chosen by hand" not in r.text
    plan_b = _get(b.id).wh_plan
    assert plan_b["selected"] == 1 and plan_b["options"][1]["manual"]

    client.post(url, data=form(act="prepare"))                                      # ask willhaben again
    wait_jobs()
    plan_a, plan_b = _get(a.id).wh_plan, _get(b.id).wh_plan
    assert plan_a["options"][0]["fields"][0]["selected"] == "neu" and plan_a["shipping"]["paylivery"] is True
    assert plan_b["selected"] == 1 and plan_b["options"][1]["path"][-1]["code"] == "MOBILE_SPEAKER"

    rows[a.id] = {k: v for k, v in rows[a.id].items() if not k.startswith("f:")}    # the page's values are the saved ones
    assert "2 ads are being published one after another" in client.post(url, data=form(act="publish")).text
    wait_jobs()
    sent = {p["ad"]["heading"]: p for p in wh.published}
    one, two = sent["Logitech S125i"], sent["JBL Go 3"]
    assert one["ad"]["categoryPath"]["path"][-1]["code"] == "HEADPHONES"
    assert one["shipping"] == {"handover": "both", "paylivery": True, "size": "M", "carrier": None}
    zustand = [x for x in one["ad"]["treeAttributes"] if x["attributeNode"]["code"] == "Zustand"]
    assert zustand[0]["selectedAttributeValueNodes"] == [{"label": "Neu", "code": "neu"}]
    assert two["ad"]["categoryPath"]["path"][-1]["code"] == "MOBILE_SPEAKER" and two["shipping"] is None
    assert {_get(a.id).status, _get(b.id).status} == {"published"}




def test_batch_table_does_not_publish_an_unreadable_price(client, wh):  # noqa: F811
    """A price the table could not read is not published with the old one: the row keeps the note, nothing queued."""
    d = _draft()                                                              # price 12
    url = f"/new-ad/batch/{_batch_of(d)}/drafts"
    client.post(url, data={"act": f"prepare:{d.id}"})
    wait_jobs()
    assert _get(d.id).wh_plan["options"]
    row = {f"{d.id}.title": "Neu", f"{d.id}.description": "Text", f"{d.id}.handover": "pickup", "sel": str(d.id)}
    r = client.post(url, data={**row, f"{d.id}.price": "abc", "act": "publish"})
    wait_jobs()
    assert "Nothing published" in r.text and "Price not recognized" in r.text and 'data-pending-key="addraft:' not in r.text
    assert wh.published == [] and _get(d.id).price_suggestion == 12 and _get(d.id).publishing is None

    r = client.post(url, data={**row, f"{d.id}.price": "80 EUR", "act": "publish"})
    wait_jobs()
    assert len(wh.published) == 1 and _get(d.id).price_suggestion == 80 and _get(d.id).status == "published"
