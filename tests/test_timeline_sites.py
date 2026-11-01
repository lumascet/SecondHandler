"""The Logbuch (timeline) with requests to several sites: the site on each row, a site filter in the requests tab,
stored requests per site in Settings, the export of one site, and the redaction of the other sites' formats."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.services import timeline
from app.willhaben import export, recorder, redact
from tests.conftest import FIXTURES

WH = "https://www.willhaben.at/webapi/ad-search/search/atz/seo/kaufen-und-verkaufen/marktplatz?keyword=rad"
VINTED = "https://www.vinted.at/catalog?search_text=rad&order=newest_first"


@pytest.fixture(autouse=True)
def recording_on():
    timeline.set_recording(timeline=True, willhaben=True)


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def _seed():
    recorder.record_http("anonymous", "search", "GET", WH, status=200, resp_body="{}",
                         resp_headers={"content-type": "application/json"})
    recorder.record_http("anonymous", "search", "GET", VINTED, status=200, platform="vinted",
                         resp_body=(FIXTURES / "vinted_catalog.rsc").read_text(encoding="utf-8"),
                         resp_headers={"content-type": "text/x-component"})
    recorder.record_http("anonymous", "detail", "GET", "https://www.vinted.at/items/10341874098", status=200,
                         platform="vinted", resp_body=(FIXTURES / "vinted_item.rsc").read_text(encoding="utf-8"),
                         resp_headers={"content-type": "text/x-component"})


def test_rows_name_the_site_and_the_requests_tab_filters_by_site(client):
    _seed()
    page = client.get("/timeline?kind=wh").text
    assert "Requests" in page and ">Vinted<" in page and "[vinted]" not in page        # the pill, not the old prefix
    assert 'href="/timeline?kind=wh&amp;site=vinted"' in page and "All sites" in page
    only = client.get("/timeline?kind=wh&site=vinted").text
    assert len(re.findall(r'id="tl-\d+"', only)) == 2 and "/catalog?search_text=rad" in only and "keyword=rad" not in only
    wh = client.get("/timeline?site=willhaben").text
    assert len(re.findall(r'id="tl-\d+"', wh)) == 1 and "keyword=rad" in wh
    assert timeline.counts_by_site() == {"vinted": 2, "willhaben": 1}
    api = client.get("/api/timeline?site=vinted").json()["entries"]
    assert len(api) == 2 and all(e["payload"]["platform"] == "vinted" for e in api)
    ev = api[0]
    detail = client.get(f"/timeline/{ev['id']}").text
    assert "Request · Vinted" in detail and ">Vinted</dd>" in detail
    assert client.get("/timeline?site=../x").status_code == 200                       # junk is ignored


def test_settings_count_stored_requests_per_site(client):
    _seed()
    page = client.get("/settings/timeline").text
    assert "Requests to the sites" in page and "Record requests to the sites" in page
    assert 'href="/timeline?kind=wh&amp;site=vinted">Vinted</a> 2' in page and '<option value="vinted">Vinted</option>' in page
    assert recorder.stats()["by_platform"] == {"vinted": 2, "willhaben": 1}
    r = client.post("/settings/timeline/recordings/delete", follow_redirects=False)
    assert "3%20stored%20requests%20deleted" in r.headers["location"]


def test_export_of_one_site_and_its_folder(client, tmp_path):
    _seed()
    r = client.post("/settings/timeline/recordings/export", data={"since": "", "site": "vinted"})
    assert r.headers["content-disposition"] == 'attachment; filename="vinted-fixtures.zip"'
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and all(n.startswith("vinted/") for n in names)
    files = export.export_recordings(tmp_path, platforms={"willhaben"})
    assert [f.relative_to(tmp_path).parts[0] for f in files] == ["search"]               # willhaben: no site folder
    item = json.loads(next(f for f in export.export_recordings(tmp_path / "v", platforms={"vinted"}) if "items" in f.name).read_text())
    assert "seller123" not in json.dumps(item) and "Erika M." in item["response"]["body"]


def test_redaction_of_the_other_sites_formats():
    ctx = redact.RedactContext()
    flight = (FIXTURES / "vinted_item.rsc").read_text(encoding="utf-8").replace("seller123", "Real Person")
    out = redact.redact_flight(flight, ctx)
    assert "Real Person" not in out and "Erika M." in out and "Handyhüllen Iphone 16" in out
    shpock = (FIXTURES / "shpock_item.html").read_text(encoding="utf-8")
    out = redact.redact_html(shpock, redact.RedactContext())
    assert "Max M." not in out and "Erika M." in out and "userA0000000001" not in out
    assert '"latitude":47.5' in out and "47.486998" not in out and "Apple IPhone 15 Pro" in out
    import json as _json
    pj = _json.loads((FIXTURES / "preisjaeger_search.json").read_text(encoding="utf-8"))
    clean = redact.redact_json(pj, redact.RedactContext())
    dump = _json.dumps(clean, ensure_ascii=False)
    assert "dealjaeger" not in dump and "EKE Case" in dump          # the poster's name is redacted, the shop kept
    from app.platforms import preisjaeger
    assert [a.ad_id for a in preisjaeger.parse_search(clean).ads] == ["369807", "369875", "369687", "369564"]   # still parses
    page = (FIXTURES / "ebay_search.html").read_text(encoding="utf-8").replace("verkaeufer-2", "Echter.Haendler")
    out = redact.redact_html(page, redact.RedactContext())
    assert "Echter.Haendler" not in out and out.count("% positiv") == page.count("% positiv")
    from app.platforms import ebay
    assert len(ebay.parse_search(out).ads) == 5 and "verkaeufer-" in ebay.parse_search(out).ads[1].body
