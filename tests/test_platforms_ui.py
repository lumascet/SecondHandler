"""Hits of other sites in the dashboard: refs in links and pushes, the viewer with and without ad details, the
price check, the Telegram buttons, the hits filter. willhaben's bare-id routes keep working as aliases."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.db import EventLog, Hit, SearchAgent, TelegramAction, log_event, session
from app.main import event_link
from app.services import notify as notify_mod
from app.services import telegram
from tests.fake_platform import FakePlatform, detail_of, listing


@pytest.fixture
def fake():
    ad = listing("77", "Kinderrad 16 Zoll", 60.0, body="Fast neu, Abholung in Berlin")
    p = FakePlatform(details={"77": detail_of(ad, "Fast neu, Abholung in Berlin. Beschreibung lang.")})
    platforms.set_platform("fake", p)
    with session() as db:
        wh = SearchAgent(name="Räder AT", source_url="https://www.willhaben.at/iad/x?keyword=rad", webapi_url="https://x")
        fk = SearchAgent(name="Räder DE", platform="fake", source_url="https://fake.example/s/rad", webapi_url="https://fake.example/s/rad",
                         spec={"platform": "fake", "url": "https://fake.example/s/rad", "keyword": "rad"})
        db.add(wh)
        db.add(fk)
        db.commit()
        db.refresh(wh)
        db.refresh(fk)
        db.add(Hit(agent_id=wh.id, ad_id="77", heading="Kinderrad AT", url="https://www.willhaben.at/iad/d/kinderrad-77/", price=70.0))
        db.add(Hit(agent_id=fk.id, platform="fake", ad_id="77", heading="Kinderrad 16 Zoll", url="https://fake.example/ad/77",
                   price=60.0, body="Fast neu", location="10115 Berlin", image_url="https://img.fake.example/1.jpg"))
        db.commit()
    yield p
    platforms.reset()


@pytest.fixture
def client(fake):
    from app.main import app
    with TestClient(app) as c:
        yield c


def _hit(platform: str) -> Hit:
    with session() as db:
        return db.exec(select(Hit).where(Hit.platform == platform)).one()


def test_hits_page_shows_sites_and_filters(client):
    page = client.get("/hits?state=all").text
    assert 'data-ad="fake:77"' in page and 'data-ad="77"' in page
    assert page.count("Fake") >= 1 and "willhaben" in page                  # pills, because two sites are watched
    only = client.get("/hits?state=all&platform=fake").text
    assert 'data-ad="fake:77"' in only and 'data-ad="77"' not in only
    assert platforms.multi() is True
    assert 'name="platform"' in page and "All platforms" in page             # the site filter
    assert 'name="platform" value="fake" checked' in only and '<input type="hidden" name="platform" value="fake">' in only
    for h in (_hit("fake"), _hit("willhaben")):
        client.post(f"/hits/{h.id}/state", data={"state": "new"})
    r = client.post("/hits/mark-seen", data={"platform": "fake"}, follow_redirects=False)
    assert r.headers["location"] == "/hits?platform=fake"
    assert _hit("fake").state == "seen" and _hit("willhaben").state == "new"  # only the chosen site's hits


def test_viewer_with_details_from_the_site(client, fake):
    h = _hit("fake")
    frag = client.get(f"/hits/ad/fake/77?hit={h.id}").text
    assert "Beschreibung lang" in frag and "Fake ↗" in frag and "https://fake.example/ad/77" in frag
    assert "/purchases/new/" not in frag                                      # buying is willhaben's
    assert 'href="/deals/ad/fake/77?hit=' in frag and 'hx-get="/hits/ad/fake/77/deal?quick=1&hit=' in frag
    assert fake.detail_calls == ["77"] and _hit("fake").state == "seen"
    assert client.get("/hits/ad/nope/77").status_code == 404


def test_viewer_without_details_uses_the_hit(client, fake):
    fake.capabilities = frozenset({"search_url", "keyword_search"})
    h = _hit("fake")
    frag = client.get(f"/hits/ad/fake/77?hit={h.id}").text
    assert "Kinderrad 16 Zoll" in frag and "Fast neu" in frag and "shows no details here" in frag
    assert fake.detail_calls == []
    assert client.get("/hits/ad/fake/77").status_code == 200                 # no hit: "no longer online" text


def test_willhaben_aliases_and_route_order(client):
    import json

    import httpx

    from tests.conftest import load
    from app.willhaben import public as public_mod
    from app.willhaben.public import PublicClient, RateLimiter

    def handler(request: httpx.Request) -> httpx.Response:
        if "/webapi/ad-search/search" in request.url.path:
            return httpx.Response(200, json=load("search_fahrrad_rows3.json"))
        nd = load("detail_922008415_nextdata.json")
        return httpx.Response(200, text=f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>')

    c = PublicClient(transport=httpx.MockTransport(handler))
    c.limiter = RateLimiter(0)
    public_mod.set_public(c)
    try:
        frag = client.get("/hits/ad/77").text
        assert "willhaben ↗" in frag and "Buy" in frag
        assert client.get("/hits/ad/abc").status_code == 400
        # /hits/ad/77/deal must reach the price section, not the platform viewer with platform="77"
        r = client.get("/hits/ad/77/deal")
        assert r.status_code == 200 and "willhaben ↗" not in r.text
        r = client.get("/deals/ad/77/result")
        assert r.status_code == 200
        assert client.get("/deals/ad/fake/77").status_code == 200
        assert client.get("/deals/ad/nope/77").status_code == 404
    finally:
        public_mod.set_public(None)


def test_deals_search_box_understands_refs_and_links(client):
    r = client.get("/deals?q=kleinanzeigen:2712345678", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deals/ad/kleinanzeigen/2712345678"
    r = client.get("/deals?q=https://fake.example/ad/77", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/deals/ad/fake/77"
    r = client.get("/deals?q=1000000001", follow_redirects=False)
    assert r.headers["location"] == "/deals/ad/1000000001"
    assert "not an ad on a supported site" in client.get("/deals?q=https://fake.example/s/rad").text


def test_viewer_deep_link_accepts_refs(client):
    page = client.get("/hits?state=all&view=fake:77").text
    assert "/^(?:[a-z][a-z0-9_]*:)?[\\w-]+$/" in page


async def test_push_links_and_inbox_use_refs(fake):
    assert notify_mod.app_path("hit", "https://fake.example/ad/77", "fake:77") == "/hits?view=fake:77"
    assert notify_mod.app_path("hit", "https://www.willhaben.at/iad/d/x-77/", "77") == "/hits?view=77"
    await notify_mod.notify("hit", "Räder DE: 60 €", "Kinderrad", url="https://fake.example/ad/77",
                            data={"agent": "Räder DE", "platform": "fake", "ad_id": "77", "ref": "fake:77"}, push=False)
    with session() as db:
        e = db.exec(select(EventLog).order_by(EventLog.id.desc())).first()
    assert e.ad_id == "fake:77"
    assert event_link(e) == {"href": "/hits?view=fake:77", "ad": "fake:77", "external": False}
    log_event("hit", "old push", "https://www.willhaben.at/iad/d/x-77/", ad_id="77")
    assert event_link(EventLog(kind="hit", message="x", ad_id="77"))["href"] == "/hits?view=77"


def test_telegram_keep_is_scoped_to_the_site(fake):
    kb = telegram.keyboard("hit", {"ad_id": "77", "ref": "fake:77"}, [], "https://fake.example/ad/77")
    tokens = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    with session() as db:
        refs = {db.get(TelegramAction, t[2:]).verb: db.get(TelegramAction, t[2:]).ref for t in tokens}
    assert refs == {"H_SAVE": "fake:77", "H_IGNORE": "fake:77"}
    assert telegram._set_hit_state("fake:77", "saved") == 1
    assert _hit("fake").state == "saved" and _hit("willhaben").state == "new"
    assert telegram._set_hit_state("77", "ignored") == 1                     # an old push: the willhaben hit
    assert _hit("willhaben").state == "ignored" and _hit("fake").state == "saved"
    assert telegram._set_hit_state("Bad:1", "saved") == 0


def test_api_hits_carry_platform_and_ref(client):
    rows = client.get("/api/hits?state=all").json()
    assert {(r["platform"], r["ref"]) for r in rows} == {("willhaben", "77"), ("fake", "fake:77")}


def test_new_agent_page_for_another_site(client, fake):
    fake.pages = [[listing("1", "Rennrad Canyon", 900.0), listing("2", "Rennrad Rahmen", 200.0)]]
    page = client.get("/agents/new?platform=fake&keyword=rennrad&price_to=1000").text
    assert "Rennrad Canyon" in page and 'name="platform" value="fake"' in page and "View this search on Fake" in page
    assert 'name="price_to"' in page and "fahrraeder" not in page            # no willhaben filter aside
    assert fake.searched[-1].keyword == "rennrad" and fake.searched[-1].price_to == 1000
    page = client.get("/agents/new?url=https://fake.example/s/kinderrad").text
    assert 'value="https://fake.example/s/kinderrad"' in page and "kinderrad" in page
    from app.willhaben import public as public_mod
    from tests.test_platforms_contract import _willhaben_client

    public_mod.set_public(_willhaben_client())                         # a rejected link shows the willhaben builder
    try:
        page = client.get("/agents/new?url=https://evil.example/s/x").text
    finally:
        public_mod.set_public(None)
    assert "Not a search link of a supported site" in page
    assert client.get("/agents/new?platform=nope").status_code == 404
    page = client.get("/agents/new?platform=fake").text
    assert "Type a search term or paste a link" in page


def test_create_agent_by_link_keyword_and_api(client, fake):
    r = client.post("/agents", data={"url": "https://fake.example/s/kinderrad", "interval_min": "10"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agents"
    r = client.post("/agents", data={"platform": "fake", "keyword": "lastenrad", "price_from": "100", "price_to": "1.500,50",
                                     "name": "Lasten", "interval_min": "10"}, follow_redirects=False)
    assert r.headers["location"] == "/agents"
    r = client.post("/agents", data={"platform": "fake", "keyword": ""}, follow_redirects=False)
    assert "search+link+or+a+search+term" in r.headers["location"] or "search%20link" in r.headers["location"]
    r = client.post("/agents", data={"url": "https://evil.example/x"}, follow_redirects=False)
    assert "supported" in r.headers["location"]
    agents = client.get("/api/agents").json()
    created = {a["name"]: a for a in agents}
    assert created["kinderrad"]["platform"] == "fake" and created["Lasten"]["spec"]["price_to"] == 1500.5
    r = client.post("/api/agents", json={"platform": "fake", "keyword": "hollandrad", "price_to": 300})
    assert r.status_code == 200 and r.json()["platform"] == "fake" and r.json()["spec"]["price_to"] == 300
    assert client.post("/api/agents", json={"url": "https://example.com"}).status_code == 400
    assert client.post("/api/agents", json={"platform": "nope", "keyword": "x"}).status_code == 400
    assert client.post("/api/agents", json={}).status_code == 400


def test_platform_api_search_and_detail(client, fake):
    fake.pages = [[listing("5", "Rad", 10.0)]]
    r = client.get("/api/search?platform=fake&keyword=rad&sort=3").json()
    assert r["platform"] == "fake" and r["ads"][0]["ref"] == "fake:5" and fake.searched[-1].sort == "price_asc"
    assert client.get("/api/search?platform=nope&keyword=rad").status_code == 404
    d = client.get("/api/ads/fake/77").json()
    assert d["ref"] == "fake:77" and d["platform"] == "fake"
    assert client.get("/api/ads/fake/1").status_code == 502
    fake.capabilities = frozenset({"search_url"})
    assert client.get("/api/ads/fake/77").status_code == 404 and client.get("/api/search?platform=fake&keyword=x").status_code == 400
    ids = [p["id"] for p in client.get("/api/platforms").json()]
    assert ids[:2] == ["willhaben", "kleinanzeigen"] and "fake" in ids


def test_settings_platforms_page_and_save(client):
    from app.db import get_setting
    from app.platforms import kleinanzeigen as ka

    page = client.get("/settings/willhaben").text
    nav = page[page.index('<nav aria-label="Settings"'):]                 # the settings menu, not the account menu
    # the groups in order (settings_catalog): the sites first, then by what you do with the app, the VPN under System
    order = ['href="/settings"', "Platforms", 'href="/settings/willhaben"', 'href="/settings/kleinanzeigen"',
             "Search &amp; prices", 'href="/settings/quiet"', "Buying", 'href="/settings/purchase"', 'href="/settings/paylivery"',
             "Selling", 'href="/settings/chats"', 'href="/settings/myads"', 'href="/settings/calendar"',
             "Shipping &amp; pickup", 'href="/settings/shipping"', 'href="/settings/printers"', "AI", 'href="/settings/rules"',
             "Notifications", 'href="/settings/telegram"', "System", 'href="/settings/vpn"', 'href="/settings/language"']
    assert [nav.index(x) for x in order] == sorted(nav.index(x) for x in order)
    assert "Buying · willhaben" not in page and "Platforms · willhaben" not in client.get("/settings/paylivery").text
    assert '<div class="text-xs font-semibold tracking-wide text-muted uppercase mb-0.5">Buying</div><h2>PayLivery</h2>' in client.get("/settings/paylivery").text
    assert 'href="/settings/platforms"' in page and "<h2>willhaben</h2>" in page   # All platforms: the defaults
    allp = client.get("/settings/platforms").text
    assert 'action="/settings/platforms"' in allp and 'name="requests_per_hour"' in allp and "What each platform uses" in allp
    page = client.get("/settings/kleinanzeigen").text
    assert 'action="/settings/platform/kleinanzeigen"' in page and 'name="enabled"' in page and 'name="auth"' in page
    assert 'name="has_product_search"' not in page                     # willhaben's form is on its own page
    assert 'action="/settings/platform/kleinanzeigen/auth"' in page               # credentials on their own form
    r = client.post("/settings/platform/kleinanzeigen", data={"enabled": "on", "gap": "4,5", "budget": "90"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings/kleinanzeigen?msg=")
    assert get_setting("platform_kleinanzeigen_enabled") == "1" and get_setting("platform_kleinanzeigen_per_hour") == "90"
    assert get_setting("platform_kleinanzeigen_min_gap_s") == "4.5"
    assert ka.kleinanzeigen_limiter().per_hour == 90 and ka.kleinanzeigen_limiter().min_gap_s == 4.5
    assert get_setting("platform_kleinanzeigen_product_search") == "0"
    client.post("/settings/platform/kleinanzeigen/auth", data={"auth": "u:p", "op": "save"}, follow_redirects=False)
    assert get_setting("kleinanzeigen_api_auth") == "u:p"
    client.post("/settings/platform/kleinanzeigen/auth", data={"auth": "not valid", "op": "save"}, follow_redirects=False)
    assert get_setting("kleinanzeigen_api_auth") == "u:p"                          # a bad format changes nothing
    client.post("/settings/platform/kleinanzeigen/auth", data={"op": "forget"}, follow_redirects=False)
    assert get_setting("kleinanzeigen_api_auth") == ""                             # deleted
    client.post("/settings/platform/kleinanzeigen", data={}, follow_redirects=False)
    assert get_setting("platform_kleinanzeigen_enabled") == "0"
    assert ">off</small>" in client.get("/settings/kleinanzeigen").text          # the nav marks a switched-off site
    ka.kleinanzeigen_limiter().min_gap_s = 0
    assert client.post("/settings/platform/willhaben", data={}).status_code == 404     # willhaben has its own form
    assert client.get("/settings/nope").status_code == 404


def test_willhaben_settings_hold_its_product_search_switch(client):
    from app.db import get_setting

    page = client.get("/settings/willhaben").text
    assert 'name="product_search"' in page and 'name="has_product_search"' in page
    client.post("/settings/willhaben", data={"budget": "50", "has_product_search": "1"}, follow_redirects=False)
    assert get_setting("platform_willhaben_product_search") == "0" and get_setting("platform_willhaben_per_hour") == "50"
    client.post("/settings/willhaben", data={"requests_per_hour": "60", "next": "/agents"}, follow_redirects=False)
    assert get_setting("platform_willhaben_product_search") == "0"          # the usage popover's form leaves it alone
    client.post("/settings/willhaben", data={"requests_per_hour": "60", "has_product_search": "1", "product_search": "on"})
    assert get_setting("platform_willhaben_product_search") == "1"


def test_stats_page_per_platform(client):
    r = client.get("/stats/willhaben", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/stats/requests?platform=willhaben&range=24h"
    page = client.get("/stats/requests?platform=kleinanzeigen&range=1h").text
    assert "Kleinanzeigen requests" in page and 'value="kleinanzeigen" selected' in page
    assert client.get("/stats/requests?platform=nope").status_code == 404
    assert client.get("/api/stats/requests?platform=kleinanzeigen").json()["platform"] == "kleinanzeigen"
    assert client.get("/api/stats/willhaben").json()["platform"] == "willhaben"


def test_screening_prompt_names_the_site():
    from app.llm.prompts import SCREEN_SYSTEM, screen_system

    assert "willhaben.at (österreichisches" in SCREEN_SYSTEM
    assert "kleinanzeigen.de (deutsches" in screen_system("kleinanzeigen.de", "DE")


def test_an_empty_budget_follows_willhaben_even_for_a_site_with_a_default(client):
    """Facebook's own default (60 an hour) came back when the field was left empty: empty is willhaben's now."""
    from app.db import platform_own_per_hour, platform_per_hour, requests_per_hour

    assert platform_own_per_hour("facebook") == 60                    # never saved: the site's default
    client.post("/settings/platform/facebook", data={"enabled": "on", "budget": ""}, follow_redirects=False)
    assert platform_own_per_hour("facebook") is None and platform_per_hour("facebook") == requests_per_hour()
    assert "value=\"\"" in client.get("/settings/facebook").text.split('name="budget"')[1][:40]
    client.post("/settings/platform/facebook", data={"enabled": "on", "budget": "20"}, follow_redirects=False)
    assert platform_per_hour("facebook") == 20
