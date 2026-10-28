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

