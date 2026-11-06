"""Shpock, Vinted and Preisjäger in the dashboard: settings pages, the product-search hint on switched-off sites, the
viewer for a non-numeric id and for a site without details, and refs to their ads."""
import pytest
from fastapi.testclient import TestClient

from app import platforms
from app.db import Hit, SearchAgent, session, set_setting
from app.platforms import preisjaeger, shpock
from app.platforms.ratelimit import RateLimiter
from app.services.deals import parse_ad_ref
from tests.test_platforms_contract import PREISJAEGER_ROUTES, SHPOCK_ROUTES, FileTransport

NEW = ("shpock", "vinted", "preisjaeger", "ebay", "facebook")


@pytest.fixture
def sites():
    for mod, cls, routes in ((shpock, "ShpockClient", SHPOCK_ROUTES), (preisjaeger, "PreisjaegerClient", PREISJAEGER_ROUTES)):
        c = getattr(mod, cls)(transport=FileTransport(routes))
        c.limiter = RateLimiter(0)
        mod.set_client(c)
    platforms.reset()
    yield
    shpock.set_client(None)
    preisjaeger.set_client(None)
    platforms.reset()


@pytest.fixture
def client(sites):
    from app.main import app
    with TestClient(app) as c:
        yield c


def test_registered_off_by_default_with_a_settings_page(client):
    assert [p.id for p in platforms.all(enabled_only=False)] == ["willhaben", "kleinanzeigen", *NEW]
    assert [p.id for p in platforms.all()] == ["willhaben"]
    page = client.get("/settings/vinted").text
    assert "about 3 MB" in page and 'action="/settings/platform/vinted"' in page
    nav = page[page.index('<nav aria-label="Settings"'):]
    assert all(f'href="/settings/{pid}"' in nav for pid in NEW) and nav.count(">off</small>") == 6
    assert "the shop is shown where other sites show the place" in client.get("/settings/preisjaeger").text
    client.post("/settings/platform/shpock", data={"enabled": "on", "product_search": "on"})
    assert platforms.enabled("shpock") and platforms.get("shpock").limiter.min_gap_s == 4.0
    assert [p["id"] for p in client.get("/api/platforms").json()] == ["willhaben", "kleinanzeigen", *NEW]


def test_product_search_names_the_switched_off_sites(client):
    page = client.get("/deals").text
    assert "Kleinanzeigen, Shpock, Vinted, Preisjäger, eBay, Facebook Marketplace can also be searched once you switch them on" in page
    assert 'href="/settings/kleinanzeigen">Switch on' in page and 'name="sites"' not in page
    for pid in ("kleinanzeigen", "shpock"):
        set_setting(f"platform_{pid}_enabled", "1")
    page = client.get("/deals").text
    assert "Vinted, Preisjäger, eBay, Facebook Marketplace can also be searched" in page and 'href="/settings/vinted">Switch on' in page
    ai_form = page[page.index('id="ps-form"'):page.index("</form>", page.index('id="ps-form"'))]
    assert ai_form.count('name="sites"') == 3                            # willhaben, Kleinanzeigen, Shpock to tick
    for pid in ("vinted", "preisjaeger", "ebay", "facebook"):
        set_setting(f"platform_{pid}_enabled", "1")
    assert "can also be searched once you switch them on" not in client.get("/deals").text


def test_kleinanzeigen_posting_card_on_its_settings_page(client):
    """Kleinanzeigen's card for the own ads posted there: its own form (the default tick and the postcode), its
    searchable items; the other sites have no such card."""
    page = client.get("/settings/kleinanzeigen").text
    start = page.rindex("<form", 0, page.index('id="s-ka-post"'))
    form = page[start:page.index("</form>", start)]
    assert 'action="/settings/platform/kleinanzeigen"' in form and 'name="part" value="posting"' in form
    assert 'name="post_default"' in form and 'name="zip"' in form and 'maxlength="5"' in form
    assert 'id="s-ka-post-default"' in page and 'id="s-ka-zip"' in page
    assert 'id="s-ka-post"' not in client.get("/settings/vinted").text


def test_refs_with_letters_and_site_links():
    set_setting("platform_shpock_enabled", "1")
    set_setting("platform_vinted_enabled", "1")
    assert parse_ad_ref("shpock:asu6-t2hxq7bDRFz") == ("shpock", "asu6-t2hxq7bDRFz")
    assert parse_ad_ref("https://www.shpock.com/de-at/i/asu6-t2hxq7bDRFz/iphone") == ("shpock", "asu6-t2hxq7bDRFz")
    assert parse_ad_ref("www.vinted.at/items/10341874098-hulle") == ("vinted", "10341874098")
    assert parse_ad_ref("iphone 15 pro") is None


def test_viewer_for_a_shpock_ad_and_a_deal(client):
    for pid in ("shpock", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    with session() as db:
        a = SearchAgent(name="Deals", platform="preisjaeger", source_url="https://www.preisjaeger.at/search?q=iphone",
                        webapi_url="https://www.preisjaeger.at/search?q=iphone")
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(Hit(agent_id=a.id, platform="preisjaeger", ad_id="369807", heading="EKE Case 2.0", price=19.92,
                   url="https://www.preisjaeger.at/deals/eke-case-369807", location="EKE Case", body="420° · Elektronik"))
        db.commit()
        hit = db.exec(__import__("sqlmodel").select(Hit).where(Hit.ad_id == "369807")).one()
    frag = client.get("/hits/ad/shpock/asu6-t2hxq7bDRFz").text
    assert "Apple IPhone 15 Pro 128 GB Titan Schwarz" in frag and "Max M." in frag and "Shpock" in frag
    deal = client.get(f"/hits/ad/preisjaeger/369807?hit={hit.id}").text
    assert "EKE Case 2.0" in deal and "Preisjäger shows no details here" in deal
    page = client.get("/hits?state=all").text
    assert 'data-ad="preisjaeger:369807"' in page


def test_the_ad_page_condition_goes_to_every_hit_of_the_ad(client):
    """Shpock states the condition only on the ad page: opening it stores it on each hit of that ad (two agents);
    a Preisjäger deal shows its inferred "new" as a shop deal."""
    from sqlmodel import select

    from app.main import _keep_condition
    from app.platforms import AdDetail

    for pid in ("shpock", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    ids = []
    with session() as db:
        for name in ("A", "B"):
            a = SearchAgent(name=name, platform="shpock", source_url="https://www.shpock.com/de-at/results?q=iphone",
                            webapi_url="x")
            db.add(a)
            db.commit()
            db.refresh(a)
            h = Hit(agent_id=a.id, platform="shpock", ad_id="asu6-t2hxq7bDRFz", heading="iPhone 15 Pro", price=450.0,
                    url="https://www.shpock.com/de-at/i/asu6-t2hxq7bDRFz")
            db.add(h)
            db.commit()
            ids.append(h.id)
        db.add(Hit(agent_id=a.id, platform="preisjaeger", ad_id="369807", heading="EKE Case 2.0", price=19.92,
                   url="https://www.preisjaeger.at/deals/eke-case-369807", item_condition="new"))
        db.commit()
        deal = db.exec(select(Hit).where(Hit.ad_id == "369807")).one()
    frag = client.get(f"/hits/ad/shpock/asu6-t2hxq7bDRFz?hit={ids[0]}").text
    assert 'title="Condition according to the ad"' in frag and ">Gut</span>" in frag
    with session() as db:
        assert [(db.get(Hit, i).item_condition, db.get(Hit, i).item_condition_label) for i in ids] == [("used", "Gut")] * 2
    cards = client.get("/hits?state=all").text                                  # a pill next to the price
    assert cards.count('title="Condition according to the ad">Gut</span>') == 2 and " · Gut · " not in cards
    frag = client.get(f"/hits/ad/preisjaeger/369807?hit={deal.id}").text
    assert ">New (shop deal)</span>" in frag and "Condition assumed (Preisjäger lists shop deals)" in frag
    # an ad page without a condition shows what the hit knows
    d = AdDetail(ad_id="1", heading="x", description=None, price=1.0, price_display=None, status="active",
                 published_at=None, first_published_at=None, end_date=None, url="u", platform="shpock")
    with session() as db:
        _keep_condition("shpock", "asu6-t2hxq7bDRFz", d, db.get(Hit, ids[1]))
    assert (d.item_condition, d.item_condition_label) == ("used", "Gut")


def test_new_agent_shows_every_site(client):
    page = client.get("/agents/new").text
    sites = page[page.index('id="agent-sites"'):page.index('id="agent-sites"') + 4000]
    assert '<input type="checkbox" name="sites" value="willhaben" class="checkbox checkbox-sm" checked' in sites
    assert all(f'href="/settings/{pid}"' in sites for pid in ("kleinanzeigen", *NEW))       # off: lead to their switch
    assert "Kleinanzeigen, Shpock, Vinted, Preisjäger, eBay, Facebook Marketplace can also be watched once you switch them on" in page
    set_setting("platform_vinted_enabled", "1")
    sites = client.get("/agents/new?sites=vinted").text
    sites = sites[sites.index('id="agent-sites"'):sites.index('id="agent-sites"') + 4000]
    assert 'name="sites" value="vinted" class="checkbox checkbox-sm" checked' in sites and 'href="/settings/vinted"' not in sites


def test_direct_search_on_the_ticked_sites(client):
    for pid in ("shpock", "preisjaeger"):
        set_setting(f"platform_{pid}_enabled", "1")
    page = client.get("/deals?q=iphone&sites=shpock&sites=preisjaeger").text
    assert "{ mode: 'direct' }" in page and "8 ads found for „iphone“" in page
    assert "Shpock 4 · Preisjäger 4" in page and 'data-ad="shpock:asu6-t2hxq7bDRFz"' in page
    assert page.count(">New (shop deal)</span>") == 4                  # deals are shop offers; Shpock lists say nothing
    assert 'href="/deals/ad/preisjaeger/369807?q=iphone"' in page and 'href="/deals/ad/shpock/asu6-t2hxq7bDRFz?q=iphone"' in page
    form = page[page.index('id="ps-direct"'):]
    assert 'name="sites" value="shpock" class="checkbox checkbox-sm" checked' in form
    assert 'name="sites" value="willhaben" class="checkbox checkbox-sm">' in form                 # not ticked: not searched
    preisjaeger.set_client(preisjaeger.PreisjaegerClient(transport=FileTransport([])))
    preisjaeger.get_client().limiter = RateLimiter(0)
    page = client.get("/deals?q=iphone&sites=shpock&sites=preisjaeger").text
    assert "4 ads found" in page and "Preisjäger: Preisjäger search returned HTTP 404" in page   # one site down: the others
