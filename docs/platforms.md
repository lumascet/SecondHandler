# Adding a second-hand platform

Search agents, the ad viewer, the Claude screening, the scam check and the price check talk to a site only through
its adapter in `app/platforms/`. The adapters today: willhaben (`willhaben.py`), kleinanzeigen.de (`kleinanzeigen.py`,
the app's JSON API), Shpock (`shpock.py`, the website's GraphQL API, asked with GET), Vinted (`vinted.py`, the
React Server Components data of the website's own pages), Preisjäger (`preisjaeger.py`, the Pepper Android app's
signed REST API) and eBay (`ebay.py`, the result cards of ebay.at's search page). A new site is one module plus fixtures and tests. Buying, chats and your own ads stay willhaben's.
Facebook Marketplace needs a login; a site like that would need the browser transport described below.

## The contract (`app/platforms/base.py`)

A `Platform` subclass with:

- `id` (lowercase, used in settings keys, refs and routes), `name` (shown in the UI), `host`, `home_country` (ISO
  code: the scam check treats an IBAN from there as normal; the screening prompt names the country), `currency`,
  `hosts` (host suffixes a pasted link may have), `image_hosts` (photo CDN hosts, counted as the site's accesses),
  `locale`, `capabilities` ⊆ `search_url, keyword_search, filters, detail, images`, `access` (`http` | `browser`),
  `min_interval_s` (a floor for agent intervals when the site caches searches).
- `limiter`: the site's own `RateLimiter` (`app/platforms/ratelimit.py`), built from its settings.
- `parse_search_url(url) -> SearchSpec`: a pasted result-list link, with volatile parameters (page, session ids)
  dropped; raises `InvalidSearchUrl` with a message a person understands. `spec.url` is the canonical browser link
  (what "on <site>" opens), `spec.api` an opaque request target if the site has one.
- `keyword_search(keyword, price_from, price_to, *, category, sort) -> SearchSpec`.
- `async search(spec, *, rows, cache_s) -> SearchPage` of `Listing`s (`app/platforms/types.py`): `ad_id`,
  `heading`, `price`, `price_display`, `location`, `published_at`, `url`, `image_url`, `is_private` (None when the
  site does not say), `is_bumped` (None when the site has no bumps), `platform`.
- `async detail(ad_id, *, url) -> AdDetail | None` when `detail` is a capability (None = the ad is gone).
- `ad_url(ad_id)`, `parse_ad_url(url)`, `describe(spec)`, `classify(url)` (access-log kind).
- `extract_ad_id(text)` when the site's ids are not numbers (Shpock: `asu6-t2hxq7bDRFz`); refs and routes take any
  `[\w-]` id.
- `about()`: a sentence or two for the site's settings page (how it is reached, what its listings lack).

Ads are addressed by **refs** everywhere a bare id travels (links, `?view=`, pushes, Telegram buttons, the timeline):
`app.platforms.ad_ref(platform, ad_id)` gives the bare id for willhaben and `<platform>:<id>` otherwise;
`split_ref()` reverses it. Routes with a site are `/hits/ad/<platform>/<id>` and `/deals/ad/<platform>/<id>`.

## Requests

Build the client on `HttpPlatformClient` (`app/platforms/httpclient.py`): it gives the limiter, a short cache,
the offline replay (`WH_REPLAY=1`), the access log and the request recorder (both per platform), a fresh cookie jar
after a while and after a block, and the 403/429 handling. Override `_check_status` when a code means something
else on the site (kleinanzeigen: 401/403 = rotated app credentials; eBay: a 403 before the visitor's first answer
is its bot check setting cookies, so the client keeps them and asks again). The transport is `HttpxTransport` or, for sites
whose bot protection checks the TLS fingerprint, `CurlTransport` (curl_cffi impersonating Chrome). A browser-backed
transport for sites that need a login or a real browser (a separate Chromium profile per platform, never the
willhaben one) would implement the same `Transport` protocol.

Give the client its own `get_client()/set_client()` so tests can hand in a fake transport.

The VPN (`app/platforms/vpn.py`, Settings → VPN) comes with `HttpPlatformClient`: with the site's switch on, each
request goes through the local proxy of the site's server (one all sites share, or one of two). A refused server
lets the site wait (`VpnWait`) while a search in the background finds it another one; with no working server the
request fails and is never sent directly. Things to set for a new site:
- `api_host` on the adapter: the host its requests go to. A new server is checked with a `CONNECT` to it.
- `_exit_blocked(r)` on the client, when 403 or 429 means something else there. kleinanzeigen counts only Akamai's
  HTML 403 and 429 as a refused server; its JSON 401/403 still reports rotated credentials.
- The VPN search (`app/services/vpn_check.py`) runs `search(keyword_search("fahrrad"), rows=1)` through each server
  it tests, as a visitor of its own (a `Trial`: its own transport and cookies, the live requests keep theirs; each
  request at most `TRIAL_TIMEOUT_S`). `search()` must raise `PlatformError` for a 200 that is not the result list: a
  JSON answer the parser rejects counts as the site's error, any other page as a bot check. Raise `NotTheExit` when an
  answer says the IP is not the problem (rotated credentials).
- Set `vpn_test_requests` on the adapter when one search may send more than one request through a server (eBay: its
  warm-up). Such a test is cancelled once its server can no longer help. State that belongs to one visitor (eBay's
  warm-up) lives in `self._visitor().extra`, so a test never changes the live visitor's.

A transport you build yourself must take the route's `proxy` (see `transport_factory`) and must not read proxy
settings from the environment.

## Registering

- `app/platforms/registry.py`: add `"<id>": "app.platforms.<module>:<Class>"` to `_FACTORIES`.
- `app/config.py`: `platform_<id>_min_gap_s`, `platform_<id>_per_hour`; `PLATFORMS_ENABLED` lists the ids that
  exist (default `willhaben,kleinanzeigen`).
- `app/db.py` `DEFAULT_SETTINGS`: `platform_<id>_enabled` ("0": the user switches it on in Settings → Platforms),
  `platform_<id>_per_hour`, `platform_<id>_min_gap_s`, `platform_<id>_product_search`.
- Settings → Platforms lists willhaben and then every other registered site as its own page, `/settings/<id>`
  (`app/templates/settings/platform.html`, saved by `POST /settings/platform/<id>` in `app/main.py`): the on/off
  switch, pacing, product-search use and status. Add fields there when the site needs more (credentials, a login).
- `tests/test_anonymous_public.py` `ANONYMOUS_MODULES`: the new module must never import the logged-in session.

## Fixtures and tests

- `tests/fixtures/<id>_*.json|html`: hand-written at first, replaced by a redacted recording once the site was used
  live (switch on "Record willhaben requests" in Settings → Logbuch, run the agent, export with
  `python -m app.willhaben.export`; `app/willhaben/redact.py` strips names, e-mails, phones and IBANs).
- `tests/test_platforms_contract.py`: add a `CASES` entry (a search link, a volatile variant, invalid links incl. an
  ad link, an ad link with its id, a keyword, an install function that serves the fixtures through a fake transport;
  `FileTransport` with a list of URL parts and fixture files does that for most sites).
  The contract test then checks the round trip of links, the keyword search, the listings, the detail and the ids.
- A `tests/test_<id>.py` for the site's own parsing and its error cases.

## Wording

- The screening prompt names the site and its country (`screen_system(site, country)` in `app/llm/prompts.py`).
- The scam rules' "off platform" vocabulary (`OFF_PLATFORM` in `app/services/scam.py`) and the IBAN neighbours
  (`NEAR_IBAN`) may need the site's words and country.
- Every new `_()` / `t()` string needs a German entry (`app/i18n/de/platforms.json`); `tests/test_i18n.py` fails
  otherwise. README: the "Supported platforms" table and the status row.
