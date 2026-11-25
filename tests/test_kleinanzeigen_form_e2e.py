"""The whole posting of an own ad on Kleinanzeigen (services/crosspost_kleinanzeigen.to_kleinanzeigen with the real
listing module's Upload) in a real browser, against a made-up copy of the ad form served at the site's own address.
The copy follows kleinanzeigen-bot's reading of the site (cookie banner, detected category, a native <dialog> as the
category picker that opens late, choices with an sr-only suffix and a breadcrumb "Alle Kategorien" below the first
level, leaves marked with aria-pressed, the condition
dialog, the price menu, shipping radios, zip with a city menu, photo markers, an upsell dialog after the submit, the
confirmation page). Only Claude's answers are given. It shows the flow works end to end, not that the live site still
looks like this. The data is made up."""
import json

import pytest

from sqlmodel import select

from app.db import AdDraft, session, set_setting
from app.platforms import kleinanzeigen_listing as kl
from app.services import crosspost_kleinanzeigen as ka
from tests.test_crosspost_kleinanzeigen import FakeBackend, _ka, _setup

AD_ID = "3210987654"
REAL_UPLOAD = kl.Upload                                  # taken before any test swaps it for a fake

FORM = """<!doctype html><html lang="de"><meta charset="utf-8"><title>Anzeige aufgeben</title>
<style>.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
.hidden{display:none}</style>
<div id="consent"><p>Wir nutzen Cookies</p><button id="c-ok">Alle akzeptieren</button>
  <button onclick="consent.remove(); S.consent='declined'">Alle ablehnen</button></div>

<label for="ad-description">Beschreibung</label><textarea id="ad-description"></textarea>
<p>Kategorie: <span id="ad-category-path"></span>
  <a href="#" aria-describedby="ad-category-path" id="cat-link">Ändern</a></p>
<dialog id="cat-dialog"><h2>Kategorie wählen</h2><nav aria-label="Kategoriennavigation"><ol id="crumbs"></ol></nav>
  <ul id="cat-list"></ul>
  <button id="cat-apply">Übernehmen</button></dialog>

<div id="condition-box" class="hidden">
  <button type="button" data-kab-condition-trigger="true" id="cond-open">Zustand wählen</button></div>
<dialog id="cond-dialog"><h2>Zustand</h2>
  <input type="radio" class="sr-only" name="condition" id="c-new" value="new"><label for="c-new">Neu</label>
  <input type="radio" class="sr-only" name="condition" id="c-like" value="like_new"><label for="c-like">Sehr gut</label>
  <input type="radio" class="sr-only" name="condition" id="c-ok" value="ok"><label for="c-ok">Gut</label>
  <input type="radio" class="sr-only" name="condition" id="c-alr" value="alright"><label for="c-alr">In Ordnung</label>
  <input type="radio" class="sr-only" name="condition" id="c-def" value="defect"><label for="c-def">Defekt</label>
  <button type="button" id="cond-ok">Bestätigen</button></dialog>

<button type="button" id="ad-price-type">Preistyp</button>
<ul id="ad-price-type-menu" class="hidden">
  <li id="ad-price-type-menu-option-0" data-v="FIXED">Festpreis</li>
  <li id="ad-price-type-menu-option-1" data-v="NEGOTIABLE">VB</li>
  <li id="ad-price-type-menu-option-2" data-v="GIVE_AWAY">Zu verschenken</li></ul>
<input id="ad-price-amount">

<input type="radio" class="sr-only" name="shipping" id="ad-shipping-enabled-yes" value="yes">
<label for="ad-shipping-enabled-yes">Versand möglich</label>
<input type="radio" class="sr-only" name="shipping" id="ad-shipping-enabled-no" value="no">
<label for="ad-shipping-enabled-no">Nur Abholung</label>

<input id="ad-zip-code"><ul id="ad-city-menu" class="hidden"></ul>
<input type="file" id="photos" multiple accept="image/*"><div id="markers"></div>
<input id="ad-title">
<button type="button" id="submit">Anzeige aufgeben</button>
<dialog id="upsell"><p>Mehr Sichtbarkeit?</p><button id="up-top">Top-Anzeige buchen</button>
  <button id="up-skip">Ohne Top-Anzeige weiter</button></dialog>

<script>
const S = window.S = {consent: null, category: null, condition: null, priceType: null, city: null, photos: 0};
const $ = id => document.getElementById(id);
const TREE = {
  "Elektronik": {"Notebooks": null, "Handys & Telefone": {"Handys": null, "Zubehör": null}},
  "Haus & Garten": {"Werkzeug": null, "Garten": null}};
let level, trail, picked;

$("ad-description").addEventListener("input", () =>      // the site's detection: wrong on purpose here
  setTimeout(() => { $("ad-category-path").textContent = "Haus & Garten > Werkzeug"; }, 300));

function render() {
  // below the first level a breadcrumb leads back, a button in an li like the choices (as on the live site)
  $("crumbs").innerHTML = trail.length
    ? '<li><button type="button" id="crumb-all"><span>Alle Kategorien</span><span aria-hidden="true">/</span></button></li>'
      + trail.map(t => `<li><span aria-current="page">${t}</span></li>`).join("") : "";
  if (trail.length) $("crumb-all").onclick = () => { level = TREE; trail = []; picked = null; render(); };
  $("cat-list").innerHTML = "";
  for (const k of Object.keys(level)) {
    const li = document.createElement("li"), b = document.createElement("button");
    b.type = "button";
    if (!level[k]) b.setAttribute("aria-pressed", "false");            // a leaf
    b.innerHTML = k + (level[k] ? '<span class="sr-only">, öffnet Unterkategorien</span>' : "")
                    + '<svg aria-hidden="true" width="8" height="8"></svg>';
    b.onclick = () => {
      if (level[k]) { trail.push(k); level = level[k]; render(); }
      else { picked = [...trail, k]; b.setAttribute("aria-pressed", "true"); }
    };
    li.append(b); $("cat-list").append(li);
  }
}
$("cat-link").onclick = e => {
  e.preventDefault(); level = TREE; trail = []; picked = null;
  setTimeout(() => { $("cat-dialog").show(); render(); }, 1200);          // the picker renders late
};
$("cat-apply").onclick = () => {
  if (!picked) return;
  S.category = picked.join(" > ");
  $("ad-category-path").textContent = S.category;
  $("cat-dialog").close();
  $("condition-box").classList.remove("hidden");                         // dependent fields after the category
};

$("cond-open").onclick = () => $("cond-dialog").show();
$("cond-ok").onclick = () => {
  const r = document.querySelector("input[name=condition]:checked");
  S.condition = r && r.value; $("cond-dialog").close();
};

$("ad-price-type").onclick = () => $("ad-price-type-menu").classList.remove("hidden");
for (const li of document.querySelectorAll("#ad-price-type-menu li"))
  li.onclick = () => { S.priceType = li.dataset.v; $("ad-price-type-menu").classList.add("hidden"); };

$("ad-zip-code").addEventListener("input", e => {
  if (e.target.value.length < 4) return;
  setTimeout(() => {
    $("ad-city-menu").innerHTML = '<li role="option">Wien</li><li role="option">Wien-Umgebung</li>';
    $("ad-city-menu").classList.remove("hidden");
    for (const li of $("ad-city-menu").children)
      li.onclick = () => { S.city = li.textContent; $("ad-city-menu").classList.add("hidden"); };
  }, 400);
});

$("photos").addEventListener("change", e => {
  [...e.target.files].forEach((f, i) => setTimeout(() => {               // the upload takes a moment per photo
    const m = document.createElement("input");
    m.type = "hidden"; m.name = `adImages[${i}].url`; m.value = `https://img.example/${i}.jpg`;
    $("markers").append(m); S.photos += 1;
  }, 400 * (i + 1)));
});

$("submit").onclick = () => setTimeout(() => $("upsell").show(), 300);
$("up-skip").onclick = async () => {
  $("upsell").close();
  const shipping = document.querySelector("input[name=shipping]:checked");
  const ad = {...S, description: $("ad-description").value, price: $("ad-price-amount").value,
              shipping: shipping && shipping.value, zip: $("ad-zip-code").value, title: $("ad-title").value};
  await fetch("/p-anzeige-aufgeben-speichern.json", {method: "POST", body: JSON.stringify(ad)});
  location.href = "/p-anzeige-aufgeben-bestaetigung.html?adId=__AD_ID__";
};
</script></html>""".replace("__AD_ID__", AD_ID)


@pytest.fixture
async def site(monkeypatch):
    """A browser context whose kleinanzeigen.de is the made-up form; what the form posts lands in `posted`."""
    pw_mod = pytest.importorskip("playwright.async_api")
    async with pw_mod.async_playwright() as pw:
        browser = None
        for kwargs in ({}, {"channel": "chrome"}):              # bundled Chromium, else the installed Google Chrome
            try:
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:  # noqa: BLE001 - not installed under this name
                continue
        if browser is None:
            pytest.skip("no Chromium or Google Chrome for Playwright")
        ctx = await browser.new_context()
        posted: list[dict] = []

        async def handle(route):
            url = route.request.url
            if url.startswith(kl.FORM_URL):
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=FORM)
            elif "/p-anzeige-aufgeben-speichern.json" in url:
                posted.append(json.loads(route.request.post_data or "{}"))
                await route.fulfill(status=200, content_type="application/json", body="{}")
            elif "/p-anzeige-aufgeben-bestaetigung.html" in url:
                await route.fulfill(status=200, content_type="text/html", body="<h1>Deine Anzeige ist online</h1>")
            else:
                await route.fulfill(status=404, body="")

        await ctx.route("https://www.kleinanzeigen.de/**", handle)

        async def new_tab(url):
            return await ctx.new_page()

        async def drop_tab(page):
            if page is not None:
                await page.close()

        monkeypatch.setattr(kl, "_new_tab", new_tab)
        monkeypatch.setattr(kl, "_drop_tab", drop_tab)
        yield posted
        await browser.close()


OWN_NOTE = "Versand aus Österreich möglich: 7,90 € (Versand durch mich, nicht über Kleinanzeigen)."


@pytest.mark.parametrize("mode, form_shipping, note", [("own", "no", OWN_NOTE), ("pickup", "no", None),
                                                       ("kleinanzeigen", "yes", None)])
async def test_an_ad_goes_through_the_whole_form(monkeypatch, tmp_path, site, mode, form_shipping, note):
    img = _setup(monkeypatch, tmp_path)                 # ad 601 from a published draft: 120 €, "Gebraucht", shipping
    monkeypatch.setattr(kl, "Upload", REAL_UPLOAD)      # the real form, not _setup's fake
    set_setting("kleinanzeigen_zip", "1010")
    set_setting("kleinanzeigen_ship_mode", mode)
    set_setting("kleinanzeigen_ship_medium", "7.90")
    with session() as db:                               # the willhaben ad ships with PayLivery, parcel size M
        d = db.exec(select(AdDraft).where(AdDraft.wh_ad_id == "601")).first()
        d.wh_plan = {"shipping": {"handover": "both", "paylivery": True, "size": "M", "carrier": "Post"}}
        db.add(d)
        db.commit()
    FakeBackend.answers = [1, 0, 0]          # the detected "Haus & Garten > Werkzeug" does not fit; Elektronik; Notebooks
    steps = []
    job = type("Job", (), {"progress": lambda self, *a: steps.append(a[1])})()

    msg = await ka.to_kleinanzeigen("601", job)

    assert len(site) == 1, "the form was not submitted"
    ad = site[0]
    assert ad["consent"] == "declined"                                  # never accepted
    assert ad["category"] == "Elektronik > Notebooks"
    assert ad["condition"] == "ok"                                      # "Gebraucht"
    assert ad["priceType"] == "FIXED" and ad["price"] == "120"
    assert ad["shipping"] == form_shipping              # "Versand möglich" only for Kleinanzeigen's own shipping
    assert ad["zip"] == "1010" and ad["city"] == "Wien"
    assert ad["photos"] == 1
    assert ad["title"] == "Lenovo ThinkPad T14 i5" and ad["description"].startswith("i5, 16 GB")
    assert ad["description"] == ("i5, 16 GB, 14 Zoll" + (f"\n\n{note}" if note else ""))
    # Claude saw the cleaned labels: no sr-only suffix, and the breadcrumb is no choice
    assert "Elektronik" in FakeBackend.prompts[1] and "öffnet Unterkategorien" not in FakeBackend.prompts[1]
    assert "Alle Kategorien" not in FakeBackend.prompts[2] and "Notebooks" in FakeBackend.prompts[2]
    rec = _ka()
    assert rec["id"] == AD_ID and rec["state"] == "active" and rec["category"] == "Elektronik > Notebooks"
    assert rec["pending"] is None and rec["url"].endswith(f"/s-anzeige/{AD_ID}")
    assert "Lenovo" in msg and img.exists()
