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
        return [{"id": 4242, "is_draft": False, "is_closed": True}], True

    async def save_draft(self, body):
        FakeUpload.saved.append(body)
        return FakeUpload.answers.pop(0)


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
    FakeUpload.saved = []
    return asked


async def test_an_own_ad_becomes_a_vinted_draft(monkeypatch, tmp_path):
    asked = _setup(monkeypatch, tmp_path)
    FakeUpload.answers = [(200, None, "")]          # the answer seen: empty, the draft is found in the wardrobe
    msg = await crosspost.to_vinted("501")
    assert "Desktop-Computer" in msg
    d = FakeUpload.saved[0]["draft"]
    assert d["catalog_id"] == 3011 and d["brand_id"] == 3 and d["package_size_id"] == 3 and d["price"] == "120"
    assert d["item_attributes"] == [{"code": "condition", "ids": [2]}] and len(d["assigned_photos"]) == 2
    assert d["temp_uuid"] == "page-session" and d["description"] == "i5, 16 GB"
    assert "Elektronik > Computer & Zubehör" in asked[0] and "Laptops" in asked[1] and "Zustand" in asked[2]
    assert "Gebraucht" in asked[0] and "Desktop-PCs" in asked[0]
    with session() as db:
        v = db.get(MyAd, "501").vinted
    assert v["id"] == "4242" and v["state"] == "draft" and v["url"].endswith("4242-hp")
    assert "already on Vinted" in await crosspost.to_vinted("501")    # never a second draft
    assert len(FakeUpload.saved) == 1
    assert await crosspost.refresh() == 1                            # sold on Vinted: read back
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
        assert "Save as a draft on Vinted" not in c.get("/myads?ad=502").text     # Vinted not logged in
        set_setting("account_vinted", '{"logged_in": true}')
        assert "Save as a draft on Vinted" in c.get("/myads?ad=502").text
        c.post("/myads/502/vinted")
        assert queued == ["502"]
        with session() as db:
            row = db.get(MyAd, "502")
            row.vinted = {"id": "9", "state": "active", "url": "https://www.vinted.at/items/9", "catalog": "Elektronik"}
            db.add(row)
            db.commit()
        page = c.get("/myads?ad=502").text
        assert "Save as a draft on Vinted" not in page and "https://www.vinted.at/items/9" in page
