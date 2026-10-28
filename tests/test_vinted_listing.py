"""An own ad as a draft on Vinted (services/crosspost.py, platforms/vinted_listing.py): the shapes seen on Vinted's
upload page 2026-10-28, Claude's picks held to Vinted's lists, the draft's body, the state read back."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import MyAd, session, set_setting
from app.llm.base import LLMResult
from app.platforms import vinted_listing as vl
from app.services import crosspost

TREE = [
    {"id": 2994, "title": "Elektronik", "catalogs": [
        {"id": 3000, "title": "Computer & Zubehör", "catalogs": [
            {"id": 3010, "title": "Laptops", "catalogs": []},
            {"id": 3011, "title": "Desktop-Computer", "catalogs": []}]},
        {"id": 3100, "title": "Smart Home", "catalogs": []}]},
    {"id": 5496, "title": "Modeaccessoires", "catalogs": [
        {"id": 5541, "title": "Bandanas & Kopftücher", "catalogs": []}]},
]
ATTRS = {"attributes": [
    {"code": "brand", "configuration": None},
    {"id": 431, "code": "condition", "configuration": {"title": "Zustand", "required": True, "selection_type": "single",
     "options": [{"id": 1, "title": "Zustand", "type": "group", "options": [
         {"id": 6, "title": "Neu, mit Etikett", "description": "originalverpackt", "type": "default"},
         {"id": 2, "title": "Sehr gut", "description": "", "type": "default"},
         {"id": 3, "title": "Gut", "description": "", "type": "default"}]}]}}]}
SIZES = [{"id": 1, "code": "SMALL", "standard": True}, {"id": 2, "code": "MEDIUM", "standard": True},
         {"id": 3, "code": "LARGE", "standard": True}]


def test_the_tree_in_leaves_and_groups():
    assert (3010, "Elektronik > Computer & Zubehör > Laptops") in vl.leaves(TREE)
    assert (3100, "Elektronik > Smart Home") in vl.leaves(TREE)
    assert vl.groups(TREE)[:2] == [(3000, "Elektronik > Computer & Zubehör"), (3100, "Elektronik > Smart Home")]
    assert vl.subtree(TREE, 3000)["title"] == "Computer & Zubehör"
    assert vl.path_of(TREE, 3011) == "Elektronik > Computer & Zubehör > Desktop-Computer"


def test_attributes_only_from_the_offered_options():
    choices = vl.attribute_choices(ATTRS)
    assert [c["code"] for c in choices] == ["condition"] and choices[0]["required"]
    assert [o[0] for o in choices[0]["options"]] == [6, 2, 3]
    picked = vl.clean_attributes([{"code": "condition", "ids": [2, 3]}, {"code": "color", "ids": [1]},
                                  {"code": "condition", "ids": [99]}], choices)
    assert picked == [{"code": "condition", "ids": [2]}]          # single choice, no unknown code or option


def test_brand_size_and_errors():
    brands = [{"id": 1, "title": "Apple"}, {"id": 2, "title": "Apple TV"}, {"id": 3, "title": "HP"}]
    assert vl.match_brand(brands, "Apple TV 4K 64 GB")["id"] == 2
    assert vl.match_brand(brands, "HP EliteDesk 800")["id"] == 3
    assert vl.match_brand(brands, "Shpock-Ware") is None          # "hp" inside a word is not HP
    assert vl.package_size(SIZES, 0.4) == 1 and vl.package_size(SIZES, 1.5) == 3 and vl.package_size(SIZES, 9) == 3
    assert vl.package_size(SIZES, None) is None and vl.package_size([], 0.4) is None
    assert vl.errors_of('{"code":99,"message_code":"validation_error","errors":[{"field":"title","value":"zu kurz"}]}') \
        == "title: zu kurz"
    assert vl.photo_id({"id": 77}) == 77 and vl.photo_id({"photo": {"id": "78"}}) == 78 and vl.photo_id([]) is None


def test_the_draft_as_the_upload_page_sends_it():
    body = vl.draft_body(title="Mini PC i5 16 GB", description="Text", price=120.0, catalog_id=3011, session_id="s-1",
                         brand={"id": 3, "title": "HP"}, package_size_id=3, photo_ids=[11, 12],
                         attributes=[{"code": "condition", "ids": [2]}])
    d = body["draft"]
    assert body["upload_session_id"] == d["temp_uuid"] == "s-1" and d["id"] is None and d["currency"] == "EUR"
    assert d["price"] == "120" and d["catalog_id"] == 3011 and d["brand_id"] == 3 and d["package_size_id"] == 3
    assert d["assigned_photos"] == [{"id": 11, "orientation": 0}, {"id": 12, "orientation": 0}]
    assert vl.draft_body(title="x" * 5, description="", price=12.5, catalog_id=1, session_id="s")["draft"]["price"] == "12.5"


def test_the_new_draft_and_states_in_the_wardrobe():
    items = [{"id": 5, "title": "Mini PC i5", "is_draft": True}, {"id": 9, "title": "Mini PC i5", "is_draft": True}]
    assert vl.find_draft(items, "Mini PC i5", known={5})["id"] == 9
    assert vl.item_state({"is_draft": True}) == "draft" and vl.item_state({"is_closed": True}) == "sold"
    assert vl.item_state({"is_reserved": True}) == "reserved" and vl.item_state({}) == "active"


class FakeUpload:
    saved: list = []
    answers: list = []
    listed: list = []
    item: dict = {}
    opened: list = []
    edits: list = []
    hidden: list = []
    published: list = []

    def __init__(self):
        self.session_id, self.user_id, self.wardrobe_calls = "page-session", "1", 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def catalogs(self):
        return TREE

    async def attributes(self, cid):
        return ATTRS

    async def brands(self, cid):
        return [{"id": 3, "title": "HP"}]

    async def package_sizes(self, cid):
        return SIZES

    async def photos(self, files):
        assert all(Path(f).exists() for f in files)
        return [100 + i for i in range(len(files))]

    async def wardrobe(self):
        self.wardrobe_calls += 1
        return [] if self.wardrobe_calls == 1 else [{"id": 4242, "title": "HP EliteDesk 800 G4", "is_draft": True,
                                                    "url": "https://www.vinted.at/items/4242-hp"}]

    async def wardrobe_all(self):
        return FakeUpload.listed, True

    async def save_draft(self, body):
        FakeUpload.saved.append(body)
        return FakeUpload.answers.pop(0)

    async def suggestions(self, cid, title, text):
        return {"prefill_brand": None, "colors": [{"id": 12, "title": "Weiß"}]}

    async def suggested_package(self, cid, title, text):
        return 2

    async def open_edit(self, item_id):
        FakeUpload.opened.append(item_id)
        return dict(FakeUpload.item)

    async def save_item(self, item_id, body):
        FakeUpload.edits.append((item_id, body))

    async def set_hidden(self, item_id, hidden):
        FakeUpload.hidden.append((item_id, hidden))

    async def publish(self, item_id, body):
        FakeUpload.published.append((item_id, body))


def _setup(monkeypatch, tmp_path):
    with session() as db:
        db.add(MyAd(ad_id="501", heading="HP EliteDesk 800 G4", status="active", parcel={"weight_kg": 1.8}))
        db.commit()

    async def plan(s, ad_id):
        return {"ad": {"heading": "HP EliteDesk 800 G4", "description": "<p>i5, 16 GB</p>", "price": 120,
                       "categoryPath": {"path": [{"label": "Computer"}, {"label": "Desktop-PCs"}]},
                       "treeAttributes": [{"attributeNode": {"label": "Zustand"},
                                           "selectedAttributeValueNodes": [{"label": "Gebraucht"}]}]},
                "photos": ["https://cache.willhaben.at/mmo/a.jpg", "https://cache.willhaben.at/mmo/b.jpg"],
                "shipping": {"handover": "both"}}

    async def download(url, folder, name, **kw):
        p = Path(folder) / f"{name}.jpg"
        p.write_bytes(b"\xff\xd8x")
        return p
    asked = []

    class Backend:
        async def complete(self, *, system, prompt, schema, images=None):
            asked.append(prompt)
            if "attributes" in schema["properties"]:
                return LLMResult(data={"attributes": [{"code": "condition", "ids": [2]}]}, model="fake")
            return LLMResult(data={"id": 3000 if "Bereiche" in prompt else 3011, "reason": ""}, model="fake")
    from app.services import ad_images
    from app.willhaben import insert, session as wsession
    monkeypatch.setattr(insert, "copy_plan", plan)
    monkeypatch.setattr(ad_images, "download", download)
    monkeypatch.setattr(wsession, "get_session", lambda: object())
    monkeypatch.setattr(crosspost, "get_backend", lambda purpose=None: Backend())
    monkeypatch.setattr(vl, "Upload", FakeUpload)
    FakeUpload.saved, FakeUpload.opened, FakeUpload.edits, FakeUpload.hidden, FakeUpload.published = [], [], [], [], []
    FakeUpload.listed = [{"id": 4242, "is_draft": False, "is_closed": True}]
    return asked


async def test_an_own_ad_becomes_a_vinted_draft(monkeypatch, tmp_path):
    asked = _setup(monkeypatch, tmp_path)
    FakeUpload.answers = [(200, None, "")]          # the answer seen: empty, the draft is found in the wardrobe
    msg = await crosspost.to_vinted("501")
    assert "Desktop-Computer" in msg
    d = FakeUpload.saved[0]["draft"]
    assert d["catalog_id"] == 3011 and d["brand_id"] == 3 and d["package_size_id"] == 3 and d["price"] == "120"
    # (no brand suggested by Vinted: the category's list named in the title; the parcel by its weight)
    assert d["item_attributes"] == [{"code": "condition", "ids": [2]}] and len(d["assigned_photos"]) == 2
    assert d["temp_uuid"] == "page-session" and d["description"] == "i5, 16 GB"
    assert "Elektronik > Computer & Zubehör" in asked[0] and "Laptops" in asked[1] and "Zustand" in asked[2]
    assert "Gebraucht" in asked[0] and "Desktop-PCs" in asked[0]
    with session() as db:
        v = db.get(MyAd, "501").vinted
    assert v["id"] == "4242" and v["state"] == "draft" and v["url"].endswith("/items/4242/edit")   # a draft: its edit page
    assert d["color_ids"] == [12]                                    # Vinted's own colour suggestion
    assert "already on Vinted" in await crosspost.to_vinted("501")    # never a second draft
    assert len(FakeUpload.saved) == 1
    monkeypatch.setattr(crosspost, "_willhaben_content", _no_content)
    reserved = []
    from app.services import sold
    monkeypatch.setattr(sold, "reserve", lambda ad_id, buyer: reserved.append(ad_id))
    assert (await crosspost.sync())["reserved"] == 1                 # sold on Vinted: reserved on willhaben
    assert reserved == ["501"]
    with session() as db:
        assert db.get(MyAd, "501").vinted["state"] == "sold"


async def test_refused_attributes_still_save_the_draft(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.answers = [(400, {}, '{"errors":[{"field":"item_attributes","value":"ungültig"}]}'),
                          (200, {"draft": {"id": 77}}, "")]
    msg = await crosspost.to_vinted("501")
    assert FakeUpload.saved[1]["draft"]["item_attributes"] == [] and "condition" in msg
    with session() as db:
        assert db.get(MyAd, "501").vinted["id"] == "77"


async def test_a_refused_draft_says_why_and_links_nothing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    FakeUpload.answers = [(400, {}, '{"errors":[{"field":"price","value":"zu niedrig"}]}')]
    try:
        await crosspost.to_vinted("501")
        raise AssertionError("no error")
    except vl.ListingError as e:
        assert "price: zu niedrig" in str(e)
    with session() as db:
        assert db.get(MyAd, "501").vinted is None


def test_the_button_on_the_ad_page_queues_the_draft(monkeypatch):
    with session() as db:
        db.add(MyAd(ad_id="502", heading="Raspberry Pi 5 8 GB", status="active"))
        db.commit()
    from app.main import app
    queued = []
    monkeypatch.setattr(crosspost, "enqueue", lambda ad_id: queued.append(ad_id) or True)
    with TestClient(app) as c:
        set_setting("account_vinted", '{"logged_in": false}')
        assert "Also on Vinted" not in c.get("/myads?ad=502").text     # Vinted logged out
        set_setting("account_vinted", '{"logged_in": true}')
        assert "Also on Vinted" in c.get("/myads?ad=502").text
        c.post("/myads/502/vinted")
        assert queued == ["502"]
        with session() as db:
            row = db.get(MyAd, "502")
            row.vinted = {"id": "9", "state": "active", "url": "https://www.vinted.at/items/9", "catalog": "Elektronik"}
            db.add(row)
            db.commit()
        page = c.get("/myads?ad=502").text
        listing = page[page.index('data-listing="vinted"'):page.index('data-listing="vinted"') + 3000]
        assert "Also on Vinted" not in listing and "https://www.vinted.at/items/9" in listing
        assert 'formaction="/myads/bulk/vinted-sync"' in page             # the bulk bar: several at once


async def _no_content(ad_id):
    return None


def test_photo_ids_in_the_order_of_the_ad():
    """10-28: the answers came in another order and the second photo became the cover."""
    body = b'--b\r\nContent-Disposition: form-data; name="photo[file]"; filename="foto2.jpg"\r\n\r\n\xff\xd8'
    assert vl.upload_name(body) == "foto2.jpg" and vl.upload_name(None) is None
    got = {"foto2.jpg": 22, "foto1.jpg": 11}                         # foto2 answered first
    assert vl.ordered_photo_ids(["/tmp/x/foto1.jpg", "/tmp/x/foto2.jpg"], got) == [11, 22]
    try:
        vl.ordered_photo_ids(["/tmp/x/foto1.jpg", "/tmp/x/foto3.jpg"], got)
        raise AssertionError("no error")
    except vl.ListingError as e:
        assert "1 of 2" in str(e)


EDIT_PAYLOAD = (
    '1:"$Sreact"\n5:[{"item":{"id":777,"title":"Mini PC i5","description":"Zeile 1\\nZeile 2","colorIds":[12],'
    '"catalogId":3011,"packageSizeId":2,"brand":{"id":3,"title":"HP"},"brandId":3,"currency":"EUR","isbn":null,'
    '"price":50,"measurementWidth":null,"measurementLength":null,"isAiPhoto":false,'
    '"itemAttributes":[{"ids":[3],"code":"condition"}],"isDraft":false,"manufacturer":null,"manufacturerLabel":null,'
    '"domesticShipmentPrice":null,"internationalShipmentPrice":null},"parcelModel":"$undefined",'
    '"photos":[{"id":91,"url":"u1"},{"id":92,"url":"u2"}]}]')


def test_the_item_from_its_edit_page_and_the_body_that_saves_it():
    item = vl.item_from_payload(EDIT_PAYLOAD, 777)
    assert item["title"] == "Mini PC i5" and item["description"] == "Zeile 1\nZeile 2" and item["photo_ids"] == [91, 92]
    assert vl.item_from_payload(EDIT_PAYLOAD, 778) is None
    body = vl.edit_body(item, "s-9", price=45.5)
    it = body["item"]                                                # a live item: the "item" key, as the page sends
    assert body["upload_session_id"] == it["temp_uuid"] == "s-9" and body["push_up"] is False
    assert it["price"] == "45.5" and it["title"] == "Mini PC i5" and it["brand_id"] == 3 and it["brand"] == "HP"
    assert it["color_ids"] == [12] and it["package_size_id"] == 2 and it["catalog_id"] == 3011
    assert it["assigned_photos"] == [{"id": 91, "orientation": 0}, {"id": 92, "orientation": 0}]
    assert it["item_attributes"] == [{"code": "condition", "ids": [3]}]
    assert "draft" in vl.edit_body({**item, "isDraft": True}, "s")


def test_what_is_not_for_sale_on_willhaben():
    assert crosspost.not_for_sale(MyAd(ad_id="1", status="active")) is None
    assert crosspost.not_for_sale(MyAd(ad_id="1", status="active", sale_status="reserved")) == "reserved"
    assert crosspost.not_for_sale(MyAd(ad_id="1", status="expired")) == "expired"
    assert crosspost.not_for_sale(MyAd(ad_id="1", status="unknown")) is None           # unknown: left alone
    assert crosspost.not_for_sale(MyAd(ad_id="1", status="replaced", replaced_by="2")) is None


def _linked_ad(**kw):
    with session() as db:
        db.add(MyAd(ad_id="601", heading="Mini PC i5", status=kw.pop("status", "active"),
                    vinted={"id": "777", "state": kw.pop("state", "active"), **kw}))
        db.commit()


async def test_the_sync_hides_while_reserved_and_shows_again(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(crosspost, "_willhaben_content", _no_content)
    _linked_ad(status="reserved")
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "50.0"}}]
    assert await crosspost.sync() == {"hidden": 1}
    assert FakeUpload.hidden == [("777", True)]
    with session() as db:
        row = db.get(MyAd, "601")
        assert row.vinted["state"] == "hidden" and row.vinted["hidden_by"] == "app"
        row.status = "active"
        db.add(row)
        db.commit()
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "50.0"}, "is_hidden": True}]
    assert await crosspost.sync() == {"shown": 1}
    assert FakeUpload.hidden[-1] == ("777", False)


async def test_an_item_the_owner_hid_stays_hidden(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(crosspost, "_willhaben_content", _no_content)
    _linked_ad(state="hidden")
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "50.0"}, "is_hidden": True}]
    assert await crosspost.sync() == {} and FakeUpload.hidden == []


async def test_title_text_and_price_follow_willhaben(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _linked_ad()
    content = ("Mini PC i5", "Zeile 1\nZeile 2", 45.0)

    async def wh(ad_id):
        return content
    monkeypatch.setattr(crosspost, "_willhaben_content", wh)
    FakeUpload.item = vl.item_from_payload(EDIT_PAYLOAD, 777)
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "50.0"}}]
    assert await crosspost.sync() == {"updated": 1}
    item_id, body = FakeUpload.edits[0]
    assert item_id == "777" and body["item"]["price"] == "45" and body["item"]["assigned_photos"][0]["id"] == 91
    FakeUpload.item = {**FakeUpload.item, "price": 45}
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "45.0"}}]
    assert await crosspost.sync() == {} and len(FakeUpload.edits) == 1          # in step: nothing saved again
    with session() as db:
        assert db.get(MyAd, "601").vinted["url"] == "https://www.vinted.at/items/777"


async def test_publishing_the_draft(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _linked_ad(state="draft")
    FakeUpload.item = {**vl.item_from_payload(EDIT_PAYLOAD, 777), "isDraft": True}
    assert await crosspost.publish("601") == "Published on Vinted"
    assert FakeUpload.published[0][0] == "777" and "draft" in FakeUpload.published[0][1]
    with session() as db:
        v = db.get(MyAd, "601").vinted
    assert v["state"] == "active" and v["url"] == "https://www.vinted.at/items/777"


def test_a_renewed_ad_takes_its_vinted_item_along():
    from app.services import my_ads
    with session() as db:
        db.add(MyAd(ad_id="701", heading="Router", status="expired", vinted={"id": "9", "state": "active"}))
        db.commit()
    new = my_ads._move_to_new_id("701", "702")
    assert new.vinted == {"id": "9", "state": "active"}
    with session() as db:
        assert db.get(MyAd, "701").vinted is None


def test_the_page_data_comes_from_the_html():
    """The page's own variable is not visible to the app's scripts: the item is read from the HTML."""
    import json as _json
    html = ('<script>self.__next_f.push([1,' + _json.dumps(EDIT_PAYLOAD[:60]) + '])</script>'
            '<script>self.__next_f.push([1,' + _json.dumps(EDIT_PAYLOAD[60:]) + '])</script>')
    assert vl.next_payload(html) == EDIT_PAYLOAD
    assert vl.item_from_payload(vl.next_payload(html), 777)["photo_ids"] == [91, 92]
    assert vl.next_payload("<html></html>") == ""



def test_which_way_the_content_goes():
    d = crosspost.decide
    assert d("a", "a", None, None) == "same"
    assert d("w2", "v1", "w1", "v1") == "to_vinted"                     # changed on willhaben
    assert d("w1", "v2", "w1", "v1") == "to_willhaben"                  # changed on Vinted
    assert d("w2", "v2", "w1", "v1") == "conflict"                      # both: willhaben wins, the owner is told
    assert d("w1", "v1", "w1", "v1") == "keep"                          # the sites keep it differently: no ping-pong
    assert d("w1", "v1", None, None) == "to_vinted"                     # first sync: willhaben's version
    assert crosspost.content_key("T", "a  \r\nb\n\n\n\nc ", 5) == crosspost.content_key("T", "a\nb\n\nc", 5.0)


async def test_a_change_on_vinted_goes_to_willhaben(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    item = vl.item_from_payload(EDIT_PAYLOAD, 777)
    wh = ("Mini PC i5", "Zeile 1\nZeile 2", 50.0)
    _linked_ad(synced_wh=crosspost.content_key(*wh), synced_vin=crosspost.content_key(*wh))

    async def content(ad_id):
        return wh
    saved = []

    async def to_wh(ad_id, title, text, price):
        saved.append((ad_id, title, text, price))
        return title, text, price
    monkeypatch.setattr(crosspost, "_willhaben_content", content)
    monkeypatch.setattr(crosspost, "_to_willhaben", to_wh)
    FakeUpload.item = {**item, "price": 42}                              # the owner lowered it on Vinted
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "42.0"}}]
    assert await crosspost.sync() == {"to_willhaben": 1}
    assert saved == [("601", "Mini PC i5", "Zeile 1\nZeile 2", 42.0)] and FakeUpload.edits == []


async def test_changed_on_both_sites_willhaben_wins_and_says_so(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    item = vl.item_from_payload(EDIT_PAYLOAD, 777)
    _linked_ad(synced_wh="old-wh", synced_vin="old-vin")

    async def content(ad_id):
        return "Mini PC i5 16 GB", "Neu", 48.0
    told = []

    async def notify(kind, title, msg, **kw):
        told.append(title)
    from app.services import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", notify)
    monkeypatch.setattr(crosspost, "_willhaben_content", content)
    FakeUpload.item = {**item, "price": 42}
    FakeUpload.listed = [{"id": 777, "title": "Mini PC i5", "price": {"amount": "42.0"}}]
    assert await crosspost.sync() == {"updated": 1}
    assert FakeUpload.edits[0][1]["item"]["title"] == "Mini PC i5 16 GB" and told == ["Changed on both sites"]


async def test_items_only_on_vinted_show_up_in_my_ads(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(crosspost, "_vinted_on", lambda: True)
    FakeUpload.listed = [
        {"id": 900, "title": "Lego Technic 42115", "price": {"amount": "120.0"}, "photo": {"url": "https://i/1.jpg"}},
        {"id": 901, "title": "Alte Jacke", "is_closed": True},                     # sold long ago: not taken in
        {"id": 902, "title": "Kamera", "is_hidden": True}]
    assert (await crosspost.sync())["new"] == 2
    with session() as db:
        lego, cam = db.get(MyAd, "vinted-900"), db.get(MyAd, "vinted-902")
        assert lego.heading == "Lego Technic 42115" and lego.status == "active" and lego.image_url == "https://i/1.jpg"
        assert lego.vinted["origin"] == "vinted" and cam.status == "inactive" and db.get(MyAd, "vinted-901") is None
    FakeUpload.listed = [{"id": 900, "title": "Lego Technic 42115", "is_reserved": True}]
    assert await crosspost.sync() == {}
    with session() as db:
        assert db.get(MyAd, "vinted-900").status == "reserved" and db.get(MyAd, "vinted-902").status == "deleted"
    assert FakeUpload.hidden == []                                             # never hidden: it is only on Vinted


async def test_also_on_willhaben_copies_it_and_links_it_once_published(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    with session() as db:
        db.add(MyAd(ad_id="vinted-777", heading="Mini PC i5", status="active",
                    vinted={"id": "777", "state": "active", "origin": "vinted"}))
        db.commit()
    FakeUpload.item = {**vl.item_from_payload(EDIT_PAYLOAD, 777), "photo_urls": ["https://i/a.jpg", "https://i/b.jpg"]}
    from app.services import ad_creator
    monkeypatch.setattr(ad_creator, "save_images", lambda did, files: [f"/x/{n}" for n, _ in files])
    draft_id = await crosspost.to_willhaben("vinted-777")
    from app.db import AdDraft
    with session() as db:
        d = db.get(AdDraft, draft_id)
        assert d.title == "Mini PC i5" and d.description == "Zeile 1\nZeile 2" and d.price_suggestion == 50.0
        assert d.image_paths == ["/x/foto1.jpg", "/x/foto2.jpg"] and d.vinted_ad == "vinted-777"
        db.add(MyAd(ad_id="1999", heading="Mini PC i5", status="unknown"))
        db.commit()
    crosspost.link_willhaben("vinted-777", "1999")
    with session() as db:
        assert db.get(MyAd, "vinted-777") is None and db.get(MyAd, "1999").vinted["id"] == "777"
        assert "origin" not in db.get(MyAd, "1999").vinted


def test_the_page_shows_each_site_and_no_willhaben_steps_for_a_vinted_only_ad():
    with session() as db:
        db.add(MyAd(ad_id="vinted-950", heading="Lego Technic 42115", status="active",
                    vinted={"id": "950", "state": "active", "url": "https://www.vinted.at/items/950"}))
        db.add(MyAd(ad_id="503", heading="Router", status="active",
                    vinted={"id": "951", "state": "hidden", "hidden_by": "app", "url": "https://www.vinted.at/items/951"}))
        db.commit()
    from app.main import app
    with TestClient(app) as c:
        page = c.get("/myads?ad=vinted-950").text
        detail = page[page.index('id="myads-detail"'):]
        assert 'data-listing="willhaben"' in detail and "Also on willhaben" in detail and "Open" in detail
        assert "/myads/vinted-950/deactivate" not in detail and "auto-renew" not in detail and "myad-description" not in detail
        assert 'value="vinted-950"' not in page                            # no bulk checkbox: willhaben steps only
        page = c.get("/myads?ad=503").text
        detail = page[page.index('id="myads-detail"'):]
        assert "/myads/503/deactivate" in detail and "Sync now" in detail and "hidden while the ad is not for sale" in detail
        row = page[page.index('id="row-503"'):page.index('id="row-503"') + 3000]
        assert 'data-row-site="willhaben"' in row and 'data-row-site="vinted"' in row



def test_several_ads_at_once(monkeypatch):
    with session() as db:
        db.add(MyAd(ad_id="801", heading="A", status="active", vinted={"id": "1", "state": "active"}))
        db.add(MyAd(ad_id="802", heading="B", status="active"))
        db.commit()
    synced, created = [], []
    from app.services import jobs
    monkeypatch.setattr(jobs, "submit", lambda kind, label, run, **kw: synced.append((kind, label, kw.get("keys"))))
    monkeypatch.setattr(crosspost, "enqueue", lambda ad_id: created.append(ad_id) or True)
    assert crosspost.schedule_sync_many(["801", "802"]) == 1 and synced[0][0] == "vinted-sync"   # only the linked one
    assert crosspost.enqueue_many(["801", "802"]) == 1 and created == ["802"]                   # only the new one
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/myads/bulk/vinted-sync", data={"ids": ["801", "802"]}, follow_redirects=False)
        assert "Syncing" in r.headers["location"]
        r = c.post("/myads/bulk/vinted", data={"ids": ["801"]}, follow_redirects=False)
        assert "already" in r.headers["location"]
