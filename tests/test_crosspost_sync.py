"""The two-way sync of an ad's title, text and price between willhaben and Vinted: the side that changed since the
last sync wins; both changed -> willhaben wins and the owner is told."""
import pytest

from app.services import crosspost as cp


def test_decide_which_way():
    a, b = "A", "B"
    assert cp.decide(a, a, None, None) == "same"
    assert cp.decide(a, b, a, a) == "to_willhaben"            # only Vinted changed since the last sync
    assert cp.decide(b, a, a, a) == "to_vinted"               # only willhaben changed
    assert cp.decide("C", b, a, a) == "conflict"              # both changed: willhaben wins, the owner hears it
    assert cp.decide(a, b, a, None) == "keep"                 # nobody changed: a site keeps it its own way


class FakeUpload:
    session_id = "s1"

    def __init__(self, item):
        self.item, self.saved = dict(item), []

    async def open_edit(self, vid):
        return dict(self.item)

    async def save_item(self, vid, body):
        self.saved.append(body)
        self.item.update(title=body.get("title", self.item["title"]))


@pytest.fixture
def stored(monkeypatch):
    out = {}
    monkeypatch.setattr(cp, "_set", lambda ad_id, **kw: out.update(kw))
    return out


async def test_a_change_on_vinted_goes_to_willhaben(monkeypatch, stored):
    wh = ("Rad", "Gutes Rad.", 100.0)
    vin = {"title": "Rad", "description": "Gutes Rad, neu bereift.", "price": 100}
    sent = []

    async def to_wh(ad_id, title, text, price):
        sent.append((title, text, price))
        return title, text, price
    monkeypatch.setattr(cp, "_to_willhaben", to_wh)
    counts = {"to_willhaben": 0, "updated": 0}
    last = cp.content_key(*wh)
    await cp._sync_content(FakeUpload(vin), "77", {"id": "9", "synced_wh": last, "synced_vin": last}, wh, counts)
    assert sent == [("Rad", "Gutes Rad, neu bereift.", 100.0)] and counts["to_willhaben"] == 1


async def test_both_changed_willhaben_wins_and_the_owner_is_told(monkeypatch, stored):
    wh = ("Rad", "Gutes Rad, 90 €.", 90.0)
    vin = {"title": "Rad", "description": "Gutes Rad, neu bereift.", "price": 100}
    told = []

    async def notify(kind, title, text, **kw):
        told.append(kind)
    monkeypatch.setattr("app.services.notify.notify", notify)
    monkeypatch.setattr(cp.vl, "edit_body", lambda cur, sid, title, description, price: {
        "title": title, "description": description, "price": price})
    up = FakeUpload(vin)
    counts = {"to_willhaben": 0, "updated": 0}
    old = cp.content_key("Rad", "Gutes Rad.", 100.0)
    await cp._sync_content(up, "77", {"id": "9", "synced_wh": old, "synced_vin": old}, wh, counts)
    assert up.saved and up.saved[0]["description"] == "Gutes Rad, 90 €." and counts["updated"] == 1
    assert told == ["ad_renewed"] and stored["synced_wh"] == cp.content_key(*wh)
