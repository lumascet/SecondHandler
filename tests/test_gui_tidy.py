"""Small GUI fixes of 10-28: an agent card keeps its name with many sites, one line per kind of search error, each
recent product search once."""
from app.db import ProductSearch, session
from app.services import product_search, search_agents


def test_one_line_per_kind_of_search_error():
    lines = search_agents.failure_lines([(9, "eBay: no list"), (10, "eBay: no list"), (12, "eBay: no list"),
                                         (3, "Shpock: 503")], 12)
    assert lines == ["3 of 12 searches failed: eBay: no list", "search 3/12 failed: Shpock: 503"]
    assert search_agents.failure_lines([(1, "blocked")], 1) == ["blocked"]


def test_each_recent_search_once():
    with session() as db:
        for text in ("Ubiquiti Cloud gateway", "ubiquiti  cloud gateway", "Raspberry Pi 5"):
            db.add(ProductSearch(description=text, status="done"))
        db.commit()
    texts = [r.description for r in product_search.recent()]
    assert len(texts) == 2 and "Raspberry Pi 5" in texts


def test_the_agent_card_keeps_its_name_with_many_sites(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import SearchAgent
    from app.main import app, templates
    with session() as db:
        db.add(SearchAgent(name="Echo Show 8", source_url="https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword=echo", webapi_url="/webapi/x",
                           queries=[{"keyword": "echo show 8", "platform": p} for p in ("willhaben", "vinted", "ebay")]))
        db.commit()
    monkeypatch.setitem(templates.env.globals, "platforms_multi", lambda: True)
    with TestClient(app) as c:
        page = c.get("/agents").text
    assert ">Echo Show 8</a>" in page and "data-agent-sites" in page and 'data-site-mark="vinted"' in page



async def test_new_ads_in_review_are_looked_at_again_until_online(monkeypatch):
    """10-29: a new ad stayed "in review" until the daily update."""
    from datetime import timedelta

    from app.db import MyAd, utcnow
    from app.services import crosspost, my_ads
    with session() as db:
        db.add(MyAd(ad_id="1700", heading="Neu", status="Passiv", published_at=utcnow() - timedelta(minutes=30)))
        db.add(MyAd(ad_id="1701", heading="Alt", status="Passiv", published_at=utcnow() - timedelta(days=3)))
        db.commit()
    assert my_ads.in_review() == ["1700"]                       # an old passive ad is not "in review"
    reads, synced = [], []

    async def imp(ads=None):
        reads.append(1)
        with session() as db:
            row = db.get(MyAd, "1700")
            row.status = "active"
            db.add(row)
            db.commit()
    monkeypatch.setattr(my_ads, "import_from_account", imp)
    monkeypatch.setattr(crosspost, "schedule_sync_many", lambda ids: synced.append(ids) or 0)
    await my_ads.review_check_job()
    assert reads == [1] and synced == [["1700"]] and my_ads.in_review() == []
    await my_ads.review_check_job()
    assert reads == [1]                                          # nothing waits: no request


def test_the_new_ad_page_shows_where_a_published_draft_stands():
    from fastapi.testclient import TestClient

    from app.db import AdDraft, MyAd
    from app.main import app
    with session() as db:
        db.add(AdDraft(input_text="x", title="Router", status="published", wh_ad_id="1800"))
        db.add(MyAd(ad_id="1800", heading="Router", status="Passiv"))
        db.commit()
    with TestClient(app) as c:
        page = c.get("/new-ad").text
    assert "In review" in page
