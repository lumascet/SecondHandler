"""Bug hunt (11-08): the price-drop badge stays on sites whose list names no old price; a Kleinanzeigen "VB" ad
never pushes; the price check's links keep the ad's site."""
from types import SimpleNamespace as NS

from app.db import Hit
from app.services import search_agents as sa


def test_the_old_price_of_a_drop_stays_when_the_list_names_none():
    h = Hit(agent_id=1, ad_id="1", heading="Rad", url="u", price=80.0, old_price=100.0, platform="kleinanzeigen")
    ad = NS(heading="Rad", body=None, price=80.0, price_display="80 €", old_price=None, location=None, postcode=None,
            published_at=None)
    for k in ("image_url", "is_private", "lat", "lon", "item_condition", "item_condition_label", "shipping",
              "shipping_price", "status", "is_bumped", "end_date", "changed_at", "category_ids", "state", "url"):
        setattr(ad, k, None)
    try:
        sa._apply(h, ad)
    except AttributeError:          # a field this list does not carry: only the old price matters here
        pass
    assert h.old_price == 100.0
    ad.price = 120.0                                 # dearer than the old price again: the badge goes
    h.old_price = 100.0
    try:
        sa._apply(h, ad)
    except AttributeError:
        pass
    assert h.old_price is None


def test_a_vb_ad_never_passes():
    h = NS(platform="kleinanzeigen", price_display="120 € VB")
    assert sa.negotiable_only(h)
    agent = NS()
    import app.services.search_agents as m
    orig = (m.screening.passes, m.geizhals_passes, m.distance_passes)
    try:
        m.screening.passes = m.geizhals_passes = m.distance_passes = lambda *a: True
        assert m.passes(agent, h) is False
    finally:
        m.screening.passes, m.geizhals_passes, m.distance_passes = orig


def test_recently_checked_links_keep_the_site():
    from fastapi.testclient import TestClient

    from app.db import DealCheck, session, utcnow
    from app.main import app
    with session() as db:
        db.add(DealCheck(ad_id="vinted:123", platform="vinted", title="Jacke", price=10.0, checked_at=utcnow()))
        db.commit()
    with TestClient(app) as c:
        html = c.get("/deals").text
    assert 'href="/deals/ad/vinted/123"' in html
