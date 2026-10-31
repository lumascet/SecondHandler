# SecondHandler

Self-hosted, LLM-powered agent for second-hand marketplaces (willhaben, Vinted, Kleinanzeigen and more): it searches and curates listings, estimates which one is the best deal, and handles buying and selling for you.

> [!WARNING]
> **This tool was written entirely by AI (Claude).** It has been tested, but its correctness and safety cannot be
> guaranteed. You use it at your own risk and are responsible for everything it does in your name: it can send chat
> messages, publish, edit and delete ads on your willhaben account. Watch out for **prompt injection**: listings and
> chat messages from strangers end up in Claude's prompts, and a crafted text could try to steer an automatic reply or
> a screening verdict. Keep automatic replies limited to ads you are comfortable with and review what is sent.

A self-hosted assistant for [willhaben.at](https://www.willhaben.at), Austria's classifieds marketplace.
It runs on your own server, from your home internet connection, and you control it through a web dashboard
(English or German).

- **Search agents**: build a search with willhaben's own filters, or paste a search link or type a search term for
  another supported site (kleinanzeigen.de, Shpock, Vinted, Preisjäger, eBay, Facebook Marketplace; see "Supported platforms"); one agent can
  watch several sites at once (the same search term and price range on each) and several search terms (add them and
  tick the sites in the agent's settings), and get notified about new listings and price drops, usually much faster
  than the sites' e-mail alerts. Hits open in a detail view inside the dashboard.
  Optionally Claude screens every new hit against a description of what you're looking for (right item? good deal?
  predefined reasons such as "accessory only" or "wrong variant"), a rule-based scam check flags suspicious offers,
  and an agent can push only ads at least a chosen percentage below the lowest new price on Geizhals;
  only hits that pass reach your phone, the others go to their own "Filtered out" list instead of New. Agents check
  at irregular, human-like intervals and slow down at night.
  A search agent can also be created from a finished product search: it then runs all of that search's keyword
  searches on every check and drops new ads over the plan's budget or with an exclusion word before they are stored.
  **Auto-buy** (opt-in per agent, willhaben PayLivery "buy now"): a hit that passes the agent and costs at most your
  maximum (item, shipping and buyer protection) is bought after a two-minute countdown that a Telegram button or the
  jobs list cancels; the price is checked again right before buying. The **search agent simulator** replays an
  agent's past hits and test ads Claude writes for it, and shows whether each would have been a hit, pushed and bought.
- **Chats**: your willhaben conversations, synced live. Claude drafts replies in your style and nothing is sent
  until you click. A draft answers exactly what the buyer asked (it never asks back what they already said, and ends
  with a question only when the next step needs their decision), and your rules are boundaries, not text to recite:
  "no warranty, private sale" is said only when a buyer asks for a warranty or a return. Your style comes from the
  messages you typed yourself: a profile Claude derives from them on request (Settings → Selling → My rules → Writing
  style, editable) plus the most characteristic ones as examples. What you told the other buyers of the same ad
  ("works fine", "only this one") is known in every chat of that ad; prices and agreements with others stay private.
  For your own ads you can opt in to automatic answers to simple questions, with a minimum price
  Claude never goes below, human-like timing and read receipts; your thumbs-up/down feedback goes into later answers. Chats you are done with can
  be hidden (they come back when the other person writes again) or deleted, which deletes them on willhaben too
  (Vinted chats are only removed from the app).
  Claude knows how many pieces of an ad are left and follows your selling preferences (both described below).
- **Create ads quickly**: drop in notes and photos and Claude writes the title, description and category. You also get
  a price suggestion from comparable listings and the product's spec sheet from Geizhals. Upload photos of several
  items at once and Claude sorts them into one draft per item. Publish directly on willhaben (free private ads; the
  category comes from willhaben's suggestion or your own pick). Claude also reads how many identical items you sell
  ("3 Stück" in the notes, identical items on the photos; the batch sorter counts them per group) and estimates the
  packed parcel size. A Pcs. field on every draft card corrects the count (yours wins; empty or 1 = one item).
  Publishing copies the count and the size to My ads; the size stays an estimate, and an automatic label purchase
  waits, until you confirm it in a shipment. A published draft links to its My ads entry and the sold page.
- **Product search** (tab "Produktsuche"): describe what you need (item, size, budget, condition, location) and
  Claude plans a few searches, the app runs them anonymously on willhaben and on every other site switched on for it
  (Settings → Platforms), page after page up to the number of ads per site you choose (30 / 100 / 200 / 500), and
  Claude ranks every candidate in batches; the best offers appear while the next batch is still being ranked, each
  with a fit score, a short reason, red flags, the price bar and the scam check, and the offers Claude found unfitting
  stay folded below. The options (depth, ads per site, order, new/used, how far over the budget, a Geizhals limit,
  the sites and Claude's switches) fold behind a one-line summary under the description. While ranking, Claude also
  names each offer's product as one searches it on Geizhals – or, for an ad made of several products such as a
  self-built PC, its parts – and the app looks up the new price (for parts: their sum), so the list can sort by the
  discount on the new price ("Rabatt") and offers above a Geizhals limit fold away; a search agent created from the
  search (offered at the top of the result) keeps that limit. An optional photo check (on by default)
  shows Claude the photos of the best offers (at most 10) and weighs damage, wear, wrong variants, stock photos or a
  different item against what you care about (a scratch barely counts if you prefer the lower price). When the
  description leaves something open (e.g. whether scratches are fine for a lower price), Claude may ask up to three
  short questions; the searches already run meanwhile, and the answers steer the ranking, the photo check and a
  search agent created from the search. Toggles for unusual names (misspellings, nicknames, generic descriptions) and other categories
  (mis-categorised ads, bundles) dig for hidden deals; the depth (≈3 / 6 / 12 searches) is the number of willhaben
  requests, Claude is called two or three times (plan, ranking, photo check).
- **Purchase orders** ("Buy" on a product search result or in the ad viewer opens the buy dialog): the ad on the
  left, on the right a three-step buy dialog (step chips: how to buy, the maximum, start): how to buy (PayLivery with
  the ad's shipping price, a bank transfer or pickup, the PayLivery card is preselected when the setting asks for it),
  the maximum including shipping and, with PayLivery, willhaben's buyer protection fee of 4.5 % (min. € 0.99, max.
  € 49.99; the field always holds the asking price plus the shipping and fee of the chosen mode
  until you change it, a cleared field is refilled; one pill next to the field rates it as percent and level, and below
  it one price-fairness hint as on the product search card: the deal bar with the asking price and the maximum, whose
  ring you can drag to set the price, the cheapest and the most expensive comparable price at its ends, the level, the
  distance to the median of the comparable ads, and at most three chips for the cheapest, the lower-quartile and the
  median price when they lie below the asking price), and a summary with the start button. Explanations are tooltips. A
  preferred pickup time is asked only for pickup. The agent contacts the seller,
  negotiates as a buyer within your maximum and your rules, and then sends a PayLivery offer, your shipping address
  (bank transfer) or agrees a pickup appointment (added to your calendar if connected). When payment is due you get a
  push and a "Pay now!" card in the chat, and every order is listed under Purchases. **Nothing is paid automatically:**
  you pay in the willhaben app or with the PayLivery button, where the 3-D Secure approval happens on your phone, or you
  transfer the amount yourself and press Paid.
- **Direct search and price check** (same tab, "Direct search" next to the AI search): a search text lists the ads of
  every ticked site with their price position; paste an ad link of any supported site and see whether the price is cheap or expensive: a
  bar rated by the comparable listings of the **same item** (searched by its model on willhaben and on the ad's own
  site; same model, variant and generation by their titles, in any category, without accessories, extreme prices and
  ads in another condition; you can pick a category) when a used item has 5 or more, else by the ad's share of the
  Geizhals new price per your zones for used and new items (Settings → Search & prices; the condition the site states picks
  the zones), with the comparables' usual range drawn under the bar, and, on click, Claude's verdict. "Ask Claude"
  also finds the right Geizhals product when the automatic lookup has none; an ad made of several products (a
  self-built PC, a console with games) is priced as the sum of its parts, each listed with its new price. Also in the hit
  viewer, with the zone label and Claude's screening verdict on hit cards.
- **Your ads**: imported from your account, with a reminder before they expire. A list with a detail pane per ad and
  three toggles (Active, Deactivated, Sold / reserved, each with its count). Per ad: auto-renew when it expires,
  automatic answers with an instruction and a minimum price (switches and text save as you type), the number of pieces
  (next bullet), edit text and photos (Claude can propose a better text), deactivate, delete, mark sold or reserved
  in one click: here and on willhaben together, from the ad or from the buyer's chat (the sale page keeps the stock
  ledger and the notice drafts for the other buyers; a mark made on willhaben itself is picked up by the next update).
  Ads that expired longer ago than a cutoff (Settings → Selling → My ads, 30 days by default) move to Deactivated; the old copy
  of a renewed ad is never listed again and its chats use the new ad's settings.
- **Stock per ad** ("Lagerstand"): in My ads → detail pane → Pieces you enter how many identical items an ad offers
  (empty = one item; the list then shows "2/3 pcs", the pane "1 sold · 2 available"). Every sale is recorded on the sold
  page (from My ads or a chat): pieces, an optional total price (prefilled with the price agreed in the chat) and the
  buyer from your chats. Each sale has an Undo there, and the chat page shows what a buyer took. While pieces are
  left, the ad stays online on willhaben and keeps answering buyers as before, and the other buyers hear nothing. With
  the last piece the usual sold steps open: mark the ad sold on willhaben, notice drafts to the other buyers,
  automatic answers off, each on your click. Claude sees the remaining stock in chat drafts and automatic
  answers ("noch 2 von 3 Stück verfügbar", the price counts per piece), and an automatic answer that names more pieces
  than are left is held back for your approval. The stock lives only in the app, because willhaben has no quantity
  field: a new ad's text says "N Stück verfügbar, Preis pro Stück", but when the count changes you edit the live text
  yourself. A reservation stays ad-wide. Undoing the sale that sold an ad out lifts only the local sold status (willhaben
  cannot undo "sold"). A renewed ad keeps its stock, minimum price and parcel size, and sales on any of its copies count.
  A piece bought with PayLivery is recorded by itself once the buyer has paid (a cancelled purchase gives it back).
  willhaben takes the ad offline after a PayLivery sale; with pieces left the app puts it online again in the
  background – in place when willhaben offers that, otherwise as a new ad copied from it (title, text, price, category,
  photos, shipping and PayLivery option) – and sends a push. A reserved ad is left alone (it is still listed), and an
  ad you deactivate or delete in the app is never put back; after 48 h without the ad going offline nothing happens.
- **Selling preferences** (Settings → Selling → Selling preferences): your usual handover (pickup, shipping or both),
  who pays the shipping (the buyer, or included in the price), preferred delivery (parcel shop or locker, front door)
  and carrier, the payment ways you accept (bank transfer, PayLivery, cash) and free notes. Claude gets them as lines in
  every reply to a buyer of your ads (manual and automatic) and in every new or revised ad text, naming only what fits
  the handover: no shipping lines with pickup only, PayLivery only with shipping, cash only with pickup. They are sent
  with their defaults (pickup only, the buyer pays, all three payment ways) even if you never open the page, which
  shows what Claude gets. With PayLivery the buyer pays the shipping at willhaben, so "included in the price" applies
  to bank transfers. Your free-text rules (Settings → AI → My rules), the ad's own text and your instruction for
  an ad still apply on top and win where they disagree. The shipping card on a chat page starts with the preferred
  carrier and delivery, then the cheapest (in the default price sort), so its primary button follows the preference;
  the app's automatic choice of a shipping option and the publish form's handover start from it too.
  **Always on, no setting:**
  willhaben censors e-mail addresses and links in chat messages and ads, so Claude is told never to write one, even on
  request; an automatic answer containing one is held back, the chat composer warns about one in the open draft
  ("Check before sending"), and publishing an ad with one in its title or description is refused. Phone numbers are
  not treated as censored.
- **Shipping**: when a buyer sends an address, the app compares parcel prices of Post, GLS, Hermes and DPD and shows
  the nearest drop-off points. The label is bought only after the buyer has paid: you mark the payment (a buyer's
  "I have paid" only becomes a reminder to check your account), then approve the purchase on the chat page or with a
  push button, or let the app buy automatically under your rules (ad marked sold to this buyer, confirmed parcel size,
  price cap of 10 € unless you change it, chosen carriers, a stored payment card, a grace period to cancel, a daily
  limit; never an option against your delivery preference). A "→ Shop" option delivers to a parcel shop or locker near
  the buyer: the chat page lists the carrier's shops around the buyer's address, you pick one (the nearest unless you
  choose another) and it goes into the carrier's form; such labels are never bought automatically until the shop choice
  in the carrier's form is confirmed live. The purchase runs in the app's own browser with
  your stored payment card, a per-carrier playbook learned from the first run, code guards (price check, never
  insurance/newsletter/account changes) and a stop for bank confirmations. The label goes to a label printer
  (100×150 / 100×200 mm) or a normal printer over IPP, per carrier, with tray and crop settings; the carrier form in the
  remote browser stays as the manual fallback. One switch prints every label automatically (bought, downloaded from
  a carrier form, or from willhaben PayLivery), or none.
- **PayLivery**: willhaben's own notices in a chat (paid, label, shipped, delivered, payout, offers) are tracked for
  both roles, the willhaben label is downloaded (and printed when automatic printing is on), and each PayLivery step (accept or decline an offer, confirm receipt,
  buy or make an offer as a buyer, only with your confirmation) runs in the browser window with the API recording on,
  since willhaben's PayLivery calls are not documented yet.
- **Google Calendar** (optional): free times go into reply drafts, agreed appointments become calendar entries on a tap.
- **Telegram Mini App** (`/tg`, opened from the bot's menu button): the dashboard on the phone inside Telegram –
  hits with filters and bulk actions, chats with drafts, my ads and the sale page, new ads, product search and price
  check, purchases, search agents, the notification inbox and background jobs – in Telegram's own look and colours,
  signed in with your Telegram account (only the owner's chat id is let in).
- **Notifications** through Home Assistant (events always, phone pushes with action buttons: send / revise with Claude /
  discard) and/or a **Telegram bot** with the same buttons, feedback buttons, commands and a typing indicator. One
  Telegram message per matter: the draft, Claude's progress and the result are edited into the notification; a
  swipe-reply answers the chat without any button, Claude's alternative decisions for the situation (accept,
  counter-offer, hold the price …) are one-tap buttons, and a search hit can become a purchase order with one tap.
- **Connection watchdog**: tiles on the home page and a warning push when the willhaben login, Claude, Home Assistant,
  Telegram, live chat or disk space fails.
- **Polite by design**: every willhaben request goes through one shared rate limiter (minimum gap plus an
  hourly budget). Quiet hours slow the agents down at night. Nothing is ever written to willhaben without your click,
  and no confirm dialogs: every willhaben action is queued, shows a **Cancel** until it runs, and the irreversible ones
  (delete, sold, send a message, publish) wait a few seconds in the queue first, with an **Undo** notice.
- **Timeline** (opt-in, Settings → Timeline): what happened, for which ad, chat, search agent, product search or
  purchase. Every Claude call with its full prompt and answer, your actions (also via Telegram, Home Assistant, the API
  and MCP), background jobs, agent runs and notifications, each with its errors and duration; jobs and agent runs open
  up into their steps. A second switch records every willhaben request with its answer (cookies and tokens left out),
  for the timeline, an offline replay mode (`WH_REPLAY=1`) and redacted test fixtures. Both are off by default and kept
  until you switch on automatic deletion (days and/or a size). Neither ever goes into a bug report.
- **MCP server**, so Claude Desktop or Claude Code can use the agent as a tool.
- **Interface**: a collapsible sidebar with badges (new hits, unread chats, payments due), a notification inbox, live
  meters for Claude tokens this month and willhaben requests in the last hour (each with a popup: per task / per
  channel, the last 6 hours, the hourly budget), a command palette (Ctrl K or `/`), light and dark theme, and a phone
  layout with a bottom tab bar and list → detail screens.

Claude runs through the Claude Code CLI with your Claude subscription. You don't need an API key.

## Screenshots

*Demo data – all names, ads and photos are made up (`scripts/demo_screenshots.py` makes them anew).*

| | |
|---|---|
| ![Home page with counters, connection status, latest hits and activity](docs/images/dashboard.png) | ![Search agents with their newest hits](docs/images/search-agents.png) |
| **Home**: counters, connection watchdog, latest hits, activity | **Search agents**: Claude screening, Geizhals limit, auto-buy |
| ![Hits with Claude's verdict, the Geizhals price zone and price drops](docs/images/hits.png) | ![Ad viewer with photos, price bar against the Geizhals new price and Claude's assessment](docs/images/ad-viewer.png) |
| **Hits**: Claude's verdict, Geizhals price zone, price drops | **Ad viewer**: price bar, Geizhals new price, Claude's assessment |
| ![Product search result with Claude's criteria, sorting and ranked offers](docs/images/product-search.png) | ![Chat with a reply drafted by Claude](docs/images/chat.png) |
| **Product search**: Claude's criteria, offers ranked, sort by discount | **Chats**: Claude drafts the answer, you send it |
| ![My ads with auto-renew, automatic answers and a minimum price](docs/images/my-ads.png) | ![Telegram Mini App: the hits list on a phone](docs/images/telegram.png) |
| **My ads**: auto-renew, automatic answers with a minimum price | **Telegram Mini App**: the dashboard on the phone |

## Supported platforms

Search agents, the ad viewer, the Claude screening, the scam check and the price check work for every site with an
adapter (`app/platforms/`, see `docs/platforms.md` for adding one). Buying, chats and your own ads are willhaben's.
Sites other than willhaben are off until you switch them on in Settings → Platforms, where each has its own request
pacing and budget.

| Site | Country | Access | Search agents | Ad viewer | Price check | Buying, chats, own ads | Status |
|---|---|---|---|---|---|---|---|
| willhaben.at | AT | direct requests + the logged-in browser | own filters, link, keyword | yes | yes | yes | done |
| kleinanzeigen.de | DE | the Kleinanzeigen app's JSON API (direct requests) | link, keyword with a price range, product search | yes | against willhaben listings | no | built; search, ad details and places checked once against the live API (2026-10-11), no agent has run for days yet |
| shpock.com | AT, DE | the GraphQL API of the Shpock website (direct requests, no login) | link, keyword with a price range, product search | yes (the ad's page: posting time, condition, seller) | against willhaben listings | no | built; search and ad pages checked once against the live site (2026-10-12), no agent has run for days yet |
| vinted.at, vinted.de | AT, DE | the catalog data the website renders for a browser (direct requests, no login, about 3 MB per search, agents every 5 min at most) | link with the site's own filters, keyword with a price range, product search | yes (the ad's page) | against willhaben listings | no | built; search and ad pages checked once against the live site (2026-10-12), no agent has run for days yet |
| preisjaeger.at | AT | the deal cards of the search page (direct requests, no login); deals from shops, not private listings | link, keyword (the app applies the price range), product search | the listing only | against willhaben listings | no | built; search checked once against the live site (2026-10-12), no agent has run for days yet |
| facebook.com/marketplace | AT | the public (logged-out) Marketplace pages, never a Facebook account; the newest ~24 listings per search, agents every 10 min at most | link (location, radius, price kept), keyword around the location set in Settings → Facebook Marketplace, product search | yes (the listing page: description, condition, all photos, category) | against willhaben listings | no | built; search and listing pages checked against the live site (2026-10-15), no agent has run for days yet |
| ebay.at | AT | the result cards of the search page (direct requests, no login, no API keys); only Buy It Now offers located in Austria, no auctions; agents every 5 min at most | link (category, condition and a wider place are kept), keyword with a price range, product search | the listing only | against willhaben listings | no | built; searches checked against the live site (2026-10-13), no agent has run for days yet |

Facebook Marketplace is read without a login, so it gives only the first ~24 listings of a search (newest first for
agents) and mixes in a few from outside the radius; its result list carries no coordinates to filter them out. Facebook's
terms do not allow automated access: it has its own, slower pacing, and a refusal pauses it. Shpock and Vinted listings come without a date (opening one
loads it), Vinted's also without a place; on Preisjäger the shop is shown where the other sites show the place.
eBay's cards name no place either (only "aus <country>" for items from abroad). eBay sits behind a bot check that
refuses a new visitor's first request or two; the app keeps that visitor's cookies and asks again, and only a refusal
after a search went through counts as a block.

Links from any of these sites can be pasted on "New search agent"; hits of other sites are listed with the site's
name, and their links in pushes and the dashboard carry the site (`kleinanzeigen:<id>`, `shpock:<id>`, …).

## Quick start

Requirements: Docker with Compose, on a machine with a **home internet connection**. willhaben blocks
datacenter and VPN IP ranges.

```bash
mkdir secondhandler && cd secondhandler
curl -fsSLO https://raw.githubusercontent.com/lumascet/secondhandler/master/compose.yaml
docker compose up -d
```

This pulls the prebuilt image `ghcr.io/lumascet/secondhandler:latest`, which GitHub Actions builds for every release
(also as `0.2.1` and `0.2`). A push to master that raises the version in `pyproject.toml` is a release: the workflow
builds the image, creates the tag `v0.2.1` and publishes a GitHub release listing the changes since the previous version.
Update later with `docker compose pull && docker compose up -d`. To build from source instead, clone the repository and run
`docker compose up -d --build`. An optional `.env` next to `compose.yaml` (see `.env.example`) holds overrides such as
the dashboard password.

Open `http://<your-server>:8000` and finish the setup in the browser:

1. **Settings → Platforms → willhaben → Log in to willhaben.** The app runs its own Chromium. You see it right on
   the page and log in by clicking and typing into it. The session is stored in `data/` and survives restarts.
2. **Settings → Claude → Sign in with Claude.** Open the link, sign in with your Claude account and paste
   the code shown back into the page. The token is valid for a year and stored in `data/`.
3. **Settings → Home Assistant → Connect** (optional). Enter your Home Assistant address and log in. You're sent
   back automatically, and the app creates its own long-lived token in your HA profile. Pick the phone that should
   get pushes.
4. **Settings → Telegram** (optional): create a bot with @BotFather (`/newbot`) and paste its token. Then open the bot
   in Telegram on any device (link or QR code) and press Start; the chat shows up in Settings and you confirm it with
   one tap; compare the 4-digit code the bot sent you, since anyone can write to a bot. Settings → Selling → Automatic answers
   decides whether pushes go to Home Assistant, Telegram or both.
5. **Search agents → New search agent.** Type what you're looking for, click filters, and create the agent.
   Optionally switch on "Claude screening" and describe exactly what you want.

Settings → Selling → Selling preferences records how you usually sell (pickup or shipping, who pays the shipping,
preferred carrier and delivery, payment ways); Claude follows it in answers to your buyers and in new ad texts. The
free-text rules stay under Settings → AI → My rules; the same page holds your writing style: "Derive from my
messages" lets Claude describe how you write (address, length, tone, greetings, punctuation, dialect, emojis, typical
phrases) from at least five messages you typed yourself on willhaben, and every reply draft follows the profile (a
purchase order's negotiation keeps its own buyer persona), which you can edit like the rules.

Settings → Claude & usage shows the token usage per task, lets you pick a model per task (default: Opus for writing
listings, Sonnet for chat replies, screening, the product search and the style profile) and sets a monthly token limit (10 M by default)
for the sidebar meter. When the limit is reached the app can pause hit screening, switch every task to Haiku or only
notify you; chat drafts you ask for are never blocked. Your subscription is not billed per token either way.

The interface follows your browser's language (English or German). You can switch it in the account menu (the
avatar at the bottom of the sidebar) or under Settings → Language & appearance.

**Settings → Backup & restore** downloads everything (database, tokens, ad photos, optionally the willhaben login) as one
ZIP, optionally encrypted with a passphrase, and restores such a file after a preview and a confirm click. The file
contains your logins: keep it private. With the timeline or the willhaben recording switched on, the database also
holds Claude prompts and willhaben answers (chat texts, addresses).

**Requests to the sites:** the willhaben meter in the sidebar opens a summary (last hour per channel, the last 6 hours, the
hourly budget, which you can change right there) and links to `/stats/willhaben`. `/stats/requests` charts every request
the app sent in the last 1 h / 6 h / 24 h / 7 days: all sites together (bars by type, a row per site with its last hour
against its own budget, blocked answers, cache hits and a trend line) or one site (bars by type: search, ad detail,
other, on willhaben also chats & messages and my ads & account; willhaben also by channel: anonymous vs. logged-in
account vs. live chat), blocked answers highlighted. Only counts per minute are stored (no addresses, ids or texts), for
8 days. Home Assistant can read the same numbers from `GET /api/stats/requests?platform=all|willhaben|kleinanzeigen|…&range=24h`
(`&by=type|channel`); `GET /api/stats/willhaben?range=24h` keeps its shape. The meter leaves out failed live-chat
reconnects, which the rate limit does not pace; on the account channel a 403 ("blocked") is often just an expired login,
and on eBay a new visitor's first 403s are its bot protection warming up.

That's it. Setting a dashboard password is strongly recommended, because the app can send messages in your name
(`DASHBOARD_PASSWORD` in `.env`).

### Optional configuration (`.env`)

Every variable is optional. Values set in the dashboard win over the environment.

| Variable | Purpose |
|---|---|
| `DASHBOARD_PASSWORD`, `DASHBOARD_USER` | Login page for the dashboard (session cookie, optionally 30 days); API clients and the MCP server use HTTP basic auth with the same credentials (recommended) |
| `DASHBOARD_PORT` | Host port of the dashboard (default 8000) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude token from `claude setup-token`, instead of signing in through the dashboard |
| `HA_URL`, `HA_TOKEN`, `HA_NOTIFY_SERVICE` | Home Assistant without the connect flow |
| `REQUEST_MIN_GAP_S`, `REQUESTS_PER_HOUR` | Rate limit for all willhaben requests (defaults 3 s, 240/h) |
| `PLATFORMS_ENABLED`, `PLATFORM_KLEINANZEIGEN_MIN_GAP_S`, `PLATFORM_KLEINANZEIGEN_PER_HOUR`, `KLEINANZEIGEN_API_AUTH` | Which other sites exist (default `willhaben,kleinanzeigen,shpock,vinted,preisjaeger,ebay,facebook`), kleinanzeigen's pacing (3 s, 120/h) and, in case the bundled ones stop working, the Kleinanzeigen app's API credentials (`user:password`); all also in Settings → Platforms |
| `PLATFORM_SHPOCK_MIN_GAP_S`, `PLATFORM_SHPOCK_PER_HOUR`, `PLATFORM_VINTED_MIN_GAP_S`, `PLATFORM_VINTED_PER_HOUR`, `PLATFORM_PREISJAEGER_MIN_GAP_S`, `PLATFORM_PREISJAEGER_PER_HOUR`, `PLATFORM_EBAY_MIN_GAP_S`, `PLATFORM_EBAY_PER_HOUR`, `PLATFORM_FACEBOOK_MIN_GAP_S`, `PLATFORM_FACEBOOK_PER_HOUR` | Pacing of the other sites (Shpock 4 s, 90/h; Vinted 8 s, 40/h; Preisjäger 4 s, 60/h; eBay 8 s, 40/h; Facebook 8 s, 60/h); also in Settings → Platforms |
| `DEFAULT_AGENT_INTERVAL_S`, `MIN_AGENT_INTERVAL_S`, `CHAT_POLL_INTERVAL_S` | Polling intervals |
| `VNC_PASSWORD`, `NOVNC_URL` | Optional noVNC view of the login browser (port 6080, LAN only). Without `VNC_PASSWORD` a random one is generated into `data/vnc-password` |
| `PROXY_VIA`, `PROXY_HOSTS` | Map hostnames (e.g. your HA domain) to a reverse-proxy container on a shared network |
| `WIREPROXY_BIN` | The wireproxy binary for the VPN of the anonymous requests (default `wireproxy`, part of the image); the configs are uploaded in Settings → VPN |
| `WH_REPLAY` | `1`: answer willhaben requests from the recorded ones (Settings → Timeline); unknown requests still go to willhaben. With `BROWSER_ENABLED=0` the app runs without any willhaben access for what was recorded |

See `.env.example` for all of them. Host-specific compose additions (extra networks, proxy settings) belong in
a `compose.override.yaml`. Compose merges it automatically, and it is git-ignored.

### Reporting a bug

**Report a bug** in the account menu opens a page where you describe what happened and click **Create report**. The app
builds an anonymised report from its own state: version, connection states, counts, recent warnings and errors
(with tracebacks) and, optionally, harmless settings. Tokens, passwords, e-mail addresses, phone numbers, IP addresses,
your server's host names (including other subdomains of your domain), people's names, carrier logins, chat
messages, addresses, ad titles (also from willhaben URLs) are removed, and ids become placeholders such as `ad#1`. Creating it costs no willhaben or Claude request, and the app never sends it anywhere: copy it or download
it as `.txt`/`.json` and share it yourself. Read it before sharing.

## Home Assistant

The app fires events on the HA event bus: `willhaben_hit`, `willhaben_price_drop`, `willhaben_message`,
`willhaben_auto_reply`, `willhaben_auto_reply_pending`, `willhaben_ad_expiring`, `willhaben_ad_expired`,
`willhaben_ad_renewed`, `willhaben_meetup`, `willhaben_shipping`, `willhaben_purchase`, `willhaben_paylivery`,
`willhaben_sale`, `willhaben_health`, `willhaben_test`. `willhaben_purchase` also fires, without a push, on every
status change of a purchase order (`stage: status`) and `willhaben_sale` for every sale; an automation that should only
react to the pushes filters on `trigger.event.data.stage`. The payload has
`title`, `message`, `url` (the original link, often willhaben), `app_url` (the matching page in this dashboard, only
when Settings → Web address is reachable from a phone, else empty) and `image`, plus `agent`, `ad_id`, `price` or
`conversation_id` depending on the event. Hits also carry `platform` (`willhaben`, `kleinanzeigen`, …) and `ref`
(the ad as it appears in links: the bare id for willhaben, `kleinanzeigen:<id>` otherwise); the event names stay
`willhaben_hit` and `willhaben_price_drop` for every site.
Hits also carry the screening result (`screen_relevant`, `screen_deal`, `screen_reason`, `screen_issues`) and the scam
check (`scam_flags`, `scam_level`). Events always fire, even for hits the screening holds back from your phone. If you picked a phone in
Settings, the app also sends pushes directly. Otherwise you can build your own automation:

```yaml
alias: willhaben hits to my phone
triggers:
  - trigger: event
    event_type: willhaben_hit
  - trigger: event
    event_type: willhaben_message
actions:
  - action: notify.mobile_app_YOUR_PHONE
    data:
      title: "{{ trigger.event.data.title }}"
      message: "{{ trigger.event.data.message }}"
      data:
        image: "{{ trigger.event.data.image }}"
        clickAction: "{{ trigger.event.data.app_url or trigger.event.data.url }}"
        url: "{{ trigger.event.data.app_url or trigger.event.data.url }}"
```

## MCP (Claude Desktop / Claude Code)

### Remote: the app itself is the MCP server

The running app serves its MCP server over HTTP at `/mcp` (Streamable HTTP). Nothing to install or run on your PC,
no repo needed. Create the API token under Settings → AI agent, then:

```bash
claude mcp add --transport http secondhandler https://<your-host>/mcp --header "Authorization: Bearer <token>"
```

Other MCP clients that take a server URL and headers (Claude Desktop's connector config, Cursor, …):

```json
{
  "mcpServers": {
    "secondhandler": {
      "type": "http",
      "url": "https://<your-host>/mcp",
      "headers": {"Authorization": "Bearer <token>"}
    }
  }
}
```

`/mcp` is protected like the JSON API: with `DASHBOARD_PASSWORD` set it needs the bearer token (or HTTP Basic auth with the
password), without one the home-network rule of the API applies. A wrong or missing token gets `401`. From outside the home
network use HTTPS only (reverse proxy), the token is sent with every request; inside it `http://<server>:8000/mcp` works.

### Local: stdio

The same tools can also run as a local process on your PC, a thin client of the agent's JSON API. Log in with the API
token from Settings → AI agent (`WH_AGENT_TOKEN`) or, as before, with the dashboard password (`WH_AGENT_PASSWORD`):

```bash
claude mcp add willhaben -e WH_AGENT_URL=http://<your-server>:8000 -e WH_AGENT_TOKEN=... \
  -- uv --directory <path-to-repo> run python -m app.mcp_server
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "willhaben": {
      "command": "uv",
      "args": ["--directory", "<path-to-repo>", "run", "python", "-m", "app.mcp_server"],
      "env": {"WH_AGENT_URL": "http://<your-server>:8000", "WH_AGENT_TOKEN": "..."}
    }
  }
}
```

Tools: `status`, `list_platforms`, `search_willhaben`, `search_listings` (any platform), `get_ad`, `list_search_agents`,
`create_search_agent` (a link of any supported site, or a keyword on one platform), `list_hits`,
`list_conversations`, `get_conversation`, `draft_reply`, `save_reply_draft`, `send_reply`, `list_my_ads`,
`track_my_ad`, `get_events` (every notification, structured), `list_purchases` / `get_purchase` (your purchase orders
with category, prices and status; also purchases made directly in a buyer chat with PayLivery), `cancel_purchase`,
`list_sales` (your own items sold), `get_settings` / `update_settings` (read and change the app's settings).

## AI agent interface

Without Home Assistant or Telegram an AI agent can follow everything the app notifies: pull the events from
`GET /api/events?since=<id>`, follow them live with `GET /api/events/stream`, or let the app push them to a webhook
(signed with HMAC, retried for 24 h). Purchases and sales can be queried with `GET /api/purchases` and `GET /api/sales`.
Set the token, webhook and event kinds up under **Settings → AI agent**; the format is in
[docs/agent-api.md](docs/agent-api.md).

## Status

| Feature | Status |
|---|---|
| Search agents, hits, price drops, quiet hours, human pace | done |
| Search agent simulator (past hits and Claude's test ads) | done |
| Auto-buy (PayLivery buy-now after a countdown, per-agent maximum) | built and tested with fakes; **no real purchase yet** (Vinted not yet) |
| Search agents on kleinanzeigen.de (platform adapter framework) | built; the API answers were checked once live, **no agent has run against it for days yet** |
| Search agents on Shpock, Vinted and Preisjäger | built; searches and ad pages were checked once live (2026-10-12), **no agent has run against them for days yet** |
| Search agents on Facebook Marketplace (logged out, newest ~24 per search) | built; searches and listing pages were checked live (2026-10-15), **no agent has run against it for days yet** |
| Search agents on eBay (ebay.at, Buy It Now, located in Austria) | built; searches were checked live (2026-10-13), **no agent has run against it for days yet** |
| Claude screening of hits (with reasons), scam check | done (thresholds still being tuned on real hits) |
| willhaben login in the dashboard, session check, silent re-login | done |
| Chat sync (live), history, Claude reply drafts, sending, read receipts | done |
| Automatic replies (opt-in per ad, limits, minimum price, feedback) | done |
| Own ads: import, expiry reminders, deactivate, delete | done |
| Own ads: auto-renew (activate / republish) | built from willhaben's web app; the first real renewal is still to be observed |
| Own ads: edit text and photos, sold / reserved | built; saving is **not yet live-tested** |
| Ad drafts with Claude, comparable prices, Geizhals specs, batch upload | done |
| Publishing ads directly | done for free private ads with pickup (batch cards and single draft page, category picker); shipping/PayLivery built from the bundle but **not yet live-tested** (also when Selling preferences preset it) |
| Stock per ad: pieces on My ads and new ads, sales ledger with undo, selling piece by piece, stock in Claude's answers | built and tested with fakes; the stock is local to the app, so only the existing sold step on willhaben ("sold / reserved" above) is unverified |
| Stock per ad: PayLivery sales counted, the ad put online again (as a new ad) while pieces are left | built and tested with fakes; **not yet live-tested** (which status willhaben shows after a PayLivery sale, and copying a sold ad) |
| Selling preferences (handover, shipping costs, carrier, payment, notes) and the e-mail/link rule | done |
| Price check and product search (Produktsuche) | done (comparables of the same item by title, category selectable; Geizhals new price per offer, sums of parts) |
| Home Assistant events, pushes with action buttons | done |
| Telegram bot and Telegram Mini App | done (in daily use) |
| Google Calendar, parcel labels | built and tested with fakes; **not yet live-tested** |
| Paid status, label purchase approval and auto rules, label purchase in the browser, printing over IPP | built and tested with fakes and a fake IPP printer; **no live carrier checkout or real printer yet**; the parcel shop for "→ Shop" labels is looked up and passed to the form, but whether the carrier form takes it is **not live-tested** (so shop labels stay manual) |
| Purchase orders (buyer side): contact, negotiation, PayLivery offer, pay-now push, pickup appointments | built and tested with fakes; **not yet live-tested** |
| PayLivery notices and process (both roles) | built from the chat message types only; notice wording, label links and every action **unverified** – record them once (Settings → Platforms → willhaben → Advanced → API recording) |
| Connection watchdog, backup, bug report, request statistics | done |
| Timeline (Claude prompts and answers, actions, jobs), willhaben request recording, offline replay, fixture export | built and tested with fakes; **not yet used against live willhaben** |
| MCP server | done |

willhaben's interfaces used by the app are documented in `docs/willhaben-api.md`. To check a response shape, the
dashboard can record the built-in browser's requests (Settings → Platforms → willhaben → Advanced → API recording).

## Development

```bash
uv sync
uv run playwright install chromium
uv run pytest            # on a server without a matching venv: scripts/test.sh (tests + lint in the app image)
uv run python -m app
```

Locally a real Chromium window opens for the login. Use `BROWSER_HEADLESS=1` for headless mode and
`BROWSER_ENABLED=0` to run without a browser. Set `CLAUDE_BIN` if `claude` isn't on your PATH.

- **Languages**: source strings are English. German lives in `app/i18n/de/*.json`. Use `t("…")` in Python and
  `{{ _("…") }}` in templates. `tests/test_i18n.py` fails if a German translation is missing. Prompts for Claude stay
  German on purpose, since Claude writes Austrian German ads and replies.
- **Fixtures** are real willhaben responses. Redact new recordings before committing them:
  `python tests/redact_fixture.py tests/fixtures/<file>.json`. Never commit `data/`, `.env` or `compose.override.yaml`.
  With "Record willhaben requests" on, `uv run python -m app.willhaben.export --out tests/fixtures/rec --since 7d`
  writes redacted fixtures from what the app recorded, and `tests/fake_willhaben.py` serves them to the clients.
- **Offline**: after recording for a while, `WH_REPLAY=1 BROWSER_ENABLED=0 uv run python -m app` answers the recorded
  willhaben requests from the database (see `docs/willhaben-api.md`, "Recording, replay and fixtures").
- **Screenshots**: `python scripts/demo_screenshots.py` (with Playwright's Chromium) fills a temporary database with
  made-up demo data, starts the app offline (sites, Geizhals and the login answered from that data, no request
  leaves the machine) and writes `docs/images/*.png`; `--serve` keeps the demo running on port 8093.
- **CI**: `.github/workflows/docker.yml` runs lint and tests, then builds the image and pushes it to GitHub
  Container Registry. The default branch gets `latest`, tags `v1.2.3` get `1.2.3` and `1.2`, and every build gets
  `sha-<commit>`. Pull requests build without pushing. To release, bump the version in `app/__init__.py` and
  `pyproject.toml` (the workflow fails when they differ) and push to master: the image is built, `vX.Y.Z` is tagged
  and a GitHub release is published. Its text is `docs/releases/vX.Y.Z.md` when that file exists (pictures in
  `docs/releases/vX.Y.Z/`, linked at the tag; `scripts/demo_screenshots.py --out docs/releases/vX.Y.Z --only …` makes
  them), with the commits folded below; without it, the commits since the previous version. A `vX.Y.Z` tag pushed by
  hand gets its image and release too.

How the app talks to willhaben (endpoints, verification status) is documented in `docs/willhaben-api.md`.

## Account separation (search agents never use your login)

Everything that only **reads** willhaben runs as an **anonymous visitor**, completely separate from your
willhaben account: search agents, the Claude screening of hits, the product search and price check (Produktsuche), the scam check
and the ad viewer. They use their own HTTP client (`app/willhaben/public.py`, `PublicClient`) with its own cookie
jar — never the cookies of the logged-in browser. That anonymous session is replaced by a fresh one every
6 hours and immediately after willhaben blocks a request (403/429), so a blocked or rate-limited search does
not touch your account. A test (`tests/test_anonymous_public.py`) fails if any of these modules imports the
logged-in session.

Only actions that need your account go through the logged-in browser (`app/willhaben/session.py`): chats and
replies, "Meine Anzeigen", publishing, reserving/sold, reactivating. Each of these happens on your click or for
ads where you switched automation on.

What is still shared: the **home IP address**, unless the anonymous requests go through the VPN (next section). That is
why *all* willhaben requests, anonymous and logged-in, go through one rate limiter with a minimum gap and an hourly
budget, and back off on 403/429.

## VPN for the anonymous requests (WireGuard)

The anonymous requests can leave through a WireGuard VPN instead of your home connection. That covers search agents,
the Claude screening, the price check, the product search and the ad viewer, with their photos. The logged-in browser
never uses it, so chats, your ads and publishing keep your own IP. Set it up in **Settings → Platforms → VPN** with one
of two uploads:

- **Config collection (.zip):** many configs, e.g. Mullvad's "all servers, Linux" download as it comes. The app uses
  one server (two when no single one suits every site) and only servers in EU countries, Austria first, then Germany
  and the neighbours, then the rest of the EU. Never one outside the EU: through a Swiss server eBay.at shows every price with Swiss VAT (+8.1 %), and
  the price checks would see drops that are none.
- **Dedicated profile (.conf):** one config, used as it is (also outside the EU: your choice).

Each site has its own switch, on its settings page and on the VPN page; Geizhals (product and new-price lookups) has
one on the VPN page, off by default. The first upload switches it on for willhaben; the others stay off until you
switch them on.

**One server for all, or two:** the sites with the switch on (and Geizhals with its switch) share one EU server.
A search in the background looks for one that every site answers: each site sends one search through a server with
its own client and checks (eBay up to 3 requests: its warm-up). When no single server answers them all, two share the
sites, each site on one that answers it (e.g. one for most sites and one for Preisjäger); when not even two cover
all, the pair that covers the most stays and the rest pause with a note until the next regular search. Searches run
every 6 hours (Settings → VPN, "Every … hours"; a regular search also changes the server now and then), on "Search
now", after an upload, when you switch a site on (it is asked through the server in use first), and when a site
refuses its server (403/429, a bot check) or gets no answer through it.

**In the background:** the search tests servers on tunnels of its own, beside the live ones, while the sites keep
working; it never starts a server that is in use. A site whose server refused it waits (its agent searches are
skipped without an error) until the search finds it one; the others go on. Every better result goes live at once
(a server found for the waiting site becomes the second connection) and the search goes on for a single server.
"Stop" ends it; what it found stays.

**Fast and frugal:** all EU servers in the collection are searched, Austria first, then the neighbours, random within
each tier; those a waiting site answered through lately first. A site is asked only while its answer can still
matter: the sites without a server first, then the one that refused most (one that has to wait for its budget lets
the next one go first). A server the pickiest site refuses costs one request; one a site refused lately (a day when
blocked, an hour for a rate limit, a bot check or no answer) is skipped without a request when that site's answer is
needed. Up to 8 tunnels start at once (no site request; a dead server shows within 5 s by its missing WireGuard
handshake) and up to 3 servers are asked at once, each site through one server at a time; a test request gives up
after 10 s, and a slow one (eBay's warm-up) stops as soon as its server can no longer help.

**Careful with failures:** a server without a handshake is marked dead for a day only at the end of a search, once
it failed again on its own and while the internet works (at most 10 per search); until then the search just leaves
it out. When three tunnels in a row (or half of the last ones) get no handshake, the search looks closer: if other
tunnels came up lately, your internet works, so a server in use that no longer gets through is dead and its sites
get another one; if none came up and the server in use does not get through either, your internet is down, so the
search stops, marks nothing and tries again 5 minutes later. Otherwise it goes on one tunnel at a time. A search
that ends with a run of servers without a handshake pauses no site for good but runs again 5 minutes later. When
nothing else is left, servers marked only for not answering are tried anyway.

Every test request counts towards the site's hourly budget: the search sends a site at
most a quarter of it an hour and never takes it past 90 % with the agents' requests; it waits for the site rather
than skipping it, and the page says for which one. Automatic searches start at least 5 minutes after the last one and
at most 3 an hour, and a site starts at most one between two regular searches. A restart resumes a search it cut
off. A dedicated profile is only tested, never swapped.

**Fail closed:** when a site's switch is on and there is no working server, or while the site waits for one, its
anonymous requests are not sent: search agents skip them without an error, a page that needs one says why, and the
connection tile on Home turns red when no search is looking for a server for it. They never fall back to your home
connection. "Forget blocks" gives every server a new chance and ends every pause.

How it works: each server in use runs as a [wireproxy](https://github.com/windtf/wireproxy) process (userspace
WireGuard, part of the image: no root, no tun device, no `NET_ADMIN`). It offers an HTTP proxy on `127.0.0.1` that
only the anonymous clients are given. Host names are resolved inside the tunnel. Before a server is tested, its
handshake and a `CONNECT` to a site check that it gets through, without sending an HTTP request to it. A switch-over
lets requests under way finish before the old server stops. A refused server does not trigger willhaben's (or
Geizhals') back-off, because those limiters are about your home IP.

Configs live in `DATA_DIR/wireguard` (readable only by the app) and are part of backups. All configs of one Mullvad
download share one key. The app never connects it twice to the same server (not even while an old connection still
finishes its requests), which would drop both connections. Connecting it to several different servers at once (the
ones in use and the ones under test) works: verified on 2026-10-15, a second wireproxy with the same key on another
server ran beside the live one for 2 min 40 s (past WireGuard's 120 s rekey), and 60 of 60 requests through the two
proxies, alternating every 5 s, were answered, each through its own exit, without disturbing the live tunnel. Give
the app its **own Mullvad device**: if the same key is also connected from another device to the same server, both
connections drop.

## Risks

- willhaben's robots.txt and terms forbid automated access. The app polls sparingly, serializes every request,
  backs off exponentially on 403/429 and never writes anything without your click (or your per-ad opt-in).
  Your account can still be suspended. Use at your own risk.
- willhaben's internal endpoints change without notice. Errors show up per search agent in the dashboard, and the
  parser tests use real (redacted) responses.
- `claude -p` with a subscription token is meant for personal use. The CLI version is pinned in the Dockerfile,
  because Anthropic has announced `--bare` (which ignores the token) as a future default.

---

SecondHandler is an independent project by lumascet and is not affiliated with willhaben, Vinted or any other site it talks to.
