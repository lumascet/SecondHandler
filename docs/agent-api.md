# AI agent interface

Everything an AI agent (Claude Code, your own script, n8n, …) needs to follow and steer SecondHandler, without Home
Assistant or Telegram:

- **Events**: every notification the app sends is also a structured event. Pull them from `GET /api/events`, follow them
  live with `GET /api/events/stream`, or let the app push them to your **webhook**.
- **Purchases and sales**: `GET /api/purchases` (your purchase orders and what you bought with PayLivery in a
  willhaben chat) and `GET /api/sales` (your own items sold).
- **Settings**: read and change them with `GET /api/settings` and `PATCH /api/settings`, checked like on the
  settings pages.
- **MCP**: the same as tools for Claude, either over HTTP at `/mcp` (served by the app itself) or as a local process
  (`app/mcp_server.py`): `get_events`, `list_purchases`, `get_purchase`, `cancel_purchase`, `list_sales`,
  `get_settings` and `update_settings` next to the existing ones.

Set it up under **Settings → AI agent**: create an API token, enter the webhook URL and pick the event kinds.

## Authentication

`Authorization: Bearer <token>` with the token from Settings → AI agent. It opens the whole JSON API (`/api/…`) and
the MCP endpoint (`/mcp`), the routes that change something too (send a reply, create a search agent, publish an ad,
change settings), but never the web pages. HTTP Basic auth with the dashboard password keeps working as before.
Without a dashboard password the API is open inside the home network, like the dashboard (see Security). The local
MCP process takes the token as `WH_AGENT_TOKEN`.

## Security

The token protects the API only together with a dashboard password. Without `DASHBOARD_PASSWORD` the app counts every
request that does not come in over the configured web address (Settings → Web address) as "home network" and lets it
in without any login or token – the API included, sending messages and stopping purchases too. A port forwarded in
the router to the app's IP, or a client that simply sends another `Host` header, gets in that way. Settings → AI agent
shows a warning while no password is set.

1. **Set `DASHBOARD_PASSWORD`** in `.env`. Then every request needs the login (browser), the password (HTTP Basic)
   or the agent's token (`/api/…` and `/mcp` only).
2. **From outside only over HTTPS**, through a reverse proxy, so the token cannot be read on the way.
3. **Don't forward port 8000 in the router.** Reach the app through the proxy or a VPN.

Treat the token like the password: whoever has it can use the whole API. "Create a new token" in Settings → AI agent
replaces it at once. Bug reports blank the token and the webhook secret out; backups contain them, like every other
login of the app (keep backups private).

## Event

```json
{
  "id": 1234,
  "ts": "2026-10-29T14:03:12+00:00",
  "kind": "purchase",
  "title": "Bought: Sony WH-1000XM4",
  "message": "180 € incl. shipping, PayLivery",
  "url": "https://www.willhaben.at/iad/…",
  "app_url": "https://agent.example.org/purchases/17",
  "ad_id": "123456789",
  "conversation_id": "abc-def",
  "quiet": false,
  "data": { "...": "the kind's fields, see below" }
}
```

- `id` grows with every event: keep the last one you saw and ask for `since=<id>`.
- `app_url` is the page in this app; null when Settings → Web address is not reachable from outside.
- `quiet`: the event went out without a phone push (e.g. a hit the screening held back, a sale you entered yourself).
- `data` holds what the notification knows as fields; keys that are not known are left out. Amounts are euro numbers.

### `data` per kind

**`purchase`** – your purchase orders (Kaufauftrag) and auto-buy, and purchases you made yourself with PayLivery in a
willhaben chat (`source: "chat"`, below):

| key | meaning |
|---|---|
| `stage` | `status`: the order's status changed (every change, quiet, with `from`); the others come with a phone push and its text: `announced` (auto-buy starts buying after a countdown), `pay_now` (agreed, waiting for your payment), `bought` (done), `failed`, `cancelled`, `update` (anything else the order tells you: the seller answered, a counter offer, paused, …) |
| `purchase_id` | id of the order (`GET /api/purchases/{id}`) |
| `status`, `from` | the order's status (`new`, `contacting`, `negotiating`, `agreed`, `meetup`, `pay_now`, `paid`, `shipped`, `done`, `paused`, `failed`, `cancelled`); `from`: the one before (stage `status` only) |
| `conversation_id` | the chat with the seller (left out while auto-buy has not opened one yet) |
| `ad_id`, `platform`, `ad_title`, `ad_url`, `category` | the ad; `category` is the site's category path ("Elektronik > Audio > Kopfhörer"), stored when the order starts (older orders: only when the fee rule names one) |
| `mode` | `paylivery`, `direct` or `pickup` |
| `asking_price`, `max_price`, `agreed_price`, `shipping`, `fee`, `total` | prices in euro |
| `cancel_by`, `cancel_url` | `announced` only: until when the auto-buy can be stopped – by the agent with `POST /api/purchases/{id}/cancel`, by a person on the page `cancel_url` (`auto_buy` and `price` are set there too) |
| `agent_id` | the search agent whose auto-buy started it |

A purchase you made directly on willhaben with PayLivery, without a purchase order (the app follows the PayLivery
notices of your chats as buyer), is announced quietly when it reaches a deal step and on every step after it:
`stage: "status"`, `source: "chat"`, `status` = the PayLivery step (`offer_accepted`, `paid`, `released`,
`label_ready`, `shipped`, `delivered`, `received`; `problem` and `cancelled` after one of them), `from`,
`conversation_id`, `ad_id`, `platform`, `ad_title`, `ad_url`, `category`, `agreed_price`, `shipping`, `total`. Steps
older than 3 days that a first chat sync finds are listed but not announced.

**`sale`** – one of your own items sold (also when you enter it yourself; pushes nothing to your phone). Taking a sale
back or a reservation sends nothing:

| key | meaning |
|---|---|
| `sale_id`, `ad_id`, `ad_title`, `ad_url`, `category` | the sale and your ad; `category` comes from the ad draft the ad was created from in this app (none for other ads) |
| `pieces`, `price`, `pieces_left` | pieces in this sale, total price, pieces still for sale |
| `conversation_id`, `buyer` | the buyer's chat and name, when known |
| `via` | `paylivery`, `manual`, … |

**`paylivery`** – PayLivery steps: `stage` (PayLivery's step, or `bank_approval` when you have to approve a payment in
your bank app), `role` (`seller` / `buyer`), `ad_id`, `conversation_id`, `amount`.

**`shipping`** – parcels: `shipment_id`, `status`, `ad_id`, `conversation_id`, `price`, `tracking`.

Other kinds (`message`, `hit`, `price_drop`, `meetup`, `ad_expiring`, …) carry the ids they already had (`ad_id`,
`conversation_id`, `agent`, `price`, …).

## Endpoints

| | |
|---|---|
| `GET /api/events?since=<id>&limit=50&kind=purchase,sale` | events after `since`, oldest first; without `since` the latest `limit`. Answer: `{"events": [...], "next_since": <id>}` |
| `GET /api/events/stream?since=<id>&kind=…` | Server-Sent Events: one `event: <kind>` / `id: <id>` / `data: <event JSON>` per event, a comment line every 15 s to keep the connection; without `since` only new events; reconnects resume with `Last-Event-ID` |
| `GET /api/purchases?status=<s>&active=0\|1&source=order\|chat&limit=20` | purchases, newest change first: purchase orders (`source: "order"`) and PayLivery purchases from your chats (`source: "chat"`); `active=1` open ones (orders not done/failed/cancelled, chat purchases before `delivered`), `active=0` the rest; `status` is an order status or a PayLivery step; unknown values are a 400 |
| `GET /api/purchases/{id}` | one order with its log (oldest first), questions, pickup, payment details and market prices |
| `POST /api/purchases/{id}/cancel` | stop a purchase: during auto-buy's countdown nothing is bought (`{"stopped": "countdown"}`), an open order is cancelled (`"order"`); 409 when it is finished |
| `GET /api/sales?limit=20&include_undone=0` | your sales, newest first; sales taken back only with `include_undone=1` |

A purchase order in the list: `source` (`order`), `id`, `conversation_id`, `ad_id`, `platform`, `ad_title`, `ad_url`,
`image_url`, `category`, `status`, `mode`, `asking_price`, `max_price`, `wish_price`, `agreed_price`, `shipping`
(with `shipping_source`: `ad`, `chat`, `estimate`), `fee`, `total` (null while no price is known), `created_at`,
`updated_at`, `note`, `app_path`, `app_url`. A chat purchase: `source` (`chat`), `id` (null), `conversation_id`,
`ad_id`, `platform`, `ad_title`, `ad_url`, `image_url`, `category` (from the ad as the chat stored it, else null),
`status` (the PayLivery step), `mode` (`paylivery`), `agreed_price` (the accepted offer, else the amount PayLivery
named), `shipping`, `shipping_source`, `fee`, `total`, `created_at`, `updated_at`, `app_path` (`/chats/<id>`),
`app_url`.
A sale: `id`, `ad_id`, `ad_title`, `ad_url`, `image_url`, `category`, `pieces`, `price`, `note`, `created_at`,
`undone_at`, `conversation_id`, `buyer`, `shipment` (`status`, `paid_at`, `paid_via`, `tracking`), `pieces_left`
(null for a single item).

Purchases without PayLivery (cash at pickup, bank transfer) that you arranged yourself, and purchases on other sites
than willhaben, are not known to the app and do not show up.

## Settings

| | |
|---|---|
| `GET /api/settings?section=<id>` | every setting the API exposes, or those of one section of the settings pages: `{"settings": [{"key", "section", "type", "value", "default", "description", "choices"?, "min"?, "max"?, "max_length"?, "nullable"?}]}`; unknown section 404 |
| `GET /api/settings/{key}` | one setting; 404 for unknown and hidden keys |
| `PATCH /api/settings` | `{"values": {"<key>": <value>, …}}`: every value is checked first, with the same rules and limits as the settings pages; any error is a 400 `{"errors": {"<key>": "<why>"}}` and nothing is stored. Otherwise `{"changed": {…}, "settings": [the changed ones]}` (and `warnings` if a follow-up step failed after storing) |

- `type` is `bool`, `int`, `float`, `enum`, `text` or `list`, and values are real JSON types (`true`, `25`, `["a"]`).
  A value outside `min`/`max` is refused, not clamped.
- Covered: the sites (on/off, request budget, pause, product search, VPN), quiet hours and pace, price rating,
  buying and PayLivery, selling, chats and automatic answers with their delays, own ads, calendar, shipping and
  automatic labels, Claude models and limits, the AI rules and system prompts, Telegram and Home Assistant
  notifications, the agent's event kinds, timeline, language.
- Never readable nor changeable: tokens, passwords, secrets and logins (the API token, the webhook and its secret,
  Home Assistant, Telegram, Google, carrier logins, the payment card, site credentials), `.env` values, internal
  bookkeeping, and the web address (`public_url`): without a dashboard password it decides which requests need a
  login, and the Telegram button opens the app there, so only the settings page changes it. Printers and the Facebook location are left to the settings pages (they need a test run).
- A change does what saving the page does (e.g. new request budgets take effect at once) and shows up in the timeline
  with the changed keys.

### All endpoints

The same token opens every route of the JSON API. The app also describes them itself: `/docs` (interactive) and
`/openapi.json`, behind the same login as the API.

| | |
|---|---|
| **Events, purchases, sales, settings** | |
| `GET /api/events`, `GET /api/events/stream` | the notification feed (above) |
| `GET /api/purchases`, `GET /api/purchases/{id}`, `POST /api/purchases/{id}/cancel` | purchase orders (above) |
| `GET /api/sales` | own items sold (above) |
| `GET /api/settings`, `GET /api/settings/{key}`, `PATCH /api/settings` | settings (above) |
| **Overview** | |
| `GET /api/status` | the willhaben login (`session`: started, logged_in, user, error), `unread_messages`, `new_hits` |
| `GET /api/platforms` | the sites the app knows, whether each is switched on and what it can do |
| `GET /api/timeline?…`, `GET /api/timeline/{id}` | the timeline (Claude calls, site requests, actions, notifications): the filters of the /timeline page; `next_before` pages further back |
| `GET /api/stats/requests?range=24h&platform=all\|<id>&by=`, `GET /api/stats/willhaben` | requests to the sites per time slot |
| **Searching** | |
| `GET /api/search?keyword=&platform=willhaben&price_from=&price_to=&rows=20&sort=1` | live search on one site; sort 1 newest, 3 price ascending, 4 price descending, 7 relevance |
| `GET /api/ads/{platform}/{ad_id}`, `GET /api/ads/{ad_id}` | one ad with its details (the second: willhaben); 404 when it is not online |
| `GET /api/convert-url?url=` | a willhaben search link as the API link the search agent uses |
| **Search agents and hits** | |
| `GET /api/agents` | the search agents |
| `POST /api/agents` | create one: `{"url": "<search link of any supported site>"}` or `{"platform", "keyword", "price_from", "price_to"}`, optionally `name`, `interval_s`, `sites: [...]` |
| `GET /api/hits?agent_id=&state=new&limit=50` | hits; `state`: new, seen, saved, ignored, filtered or all |
| **Chats** | |
| `GET /api/chats` | the conversations, latest message first |
| `GET /api/chats/{id}` | one conversation with its messages and reply drafts |
| `POST /api/chats/{id}/draft` | Claude writes a reply draft: `{"hint": "…"}` optional |
| `POST /api/chats/{id}/drafts` | store a reply written elsewhere as a draft: `{"text", "intent"}` |
| `POST /api/drafts/{id}/send` | **sends** the draft to the other person: `{"text": "<final text>"}`; 409 when it cannot be sent, 503 without a willhaben login |
| **Own ads** | |
| `GET /api/myads` | every own ad with `stock` (pieces) |
| `POST /api/myads` | track an own ad: `{"ref": "<id or link>", "auto_renew": false}` |
| `GET /api/myads/{ad_id}/editor-json` | an own ad as willhaben's editor sees it |
| `POST /api/ad-drafts` | Claude writes an ad draft from notes: `{"notes": "…"}` |
| `GET /api/ad-drafts/{id}/willhaben-plan` | what willhaben's form suggests for the draft (category, required fields) |
| `POST /api/ad-drafts/{id}/willhaben-publish` | **publishes** the draft as a free private ad; `stop_after` (`presave`, `photos`, `save`, `delivery`, `cart`) tries it step by step |
| `GET /api/willhaben/categories?q=&limit=` | willhaben's ad categories matching `q` |

## MCP

The app serves its MCP server at `/mcp` (Streamable HTTP, POST only), with the same token or login as the API; no
local process or copy of the repository needed:

```bash
claude mcp add --transport http secondhandler https://<your-host>/mcp --header "Authorization: Bearer <token>"
```

Clients with a JSON config: `{"type": "http", "url": "https://<your-host>/mcp", "headers": {"Authorization": "Bearer <token>"}}`.
The tools call the JSON API inside the app with the caller's credentials and show up in the timeline as "mcp". The
local variant (`python -m app.mcp_server`, see the README) offers the same tools.

## Webhook

For every new event of a selected kind the app sends `POST <your URL>` with the event JSON as body and these headers:

| header | |
|---|---|
| `X-SecondHandler-Event` | the kind |
| `X-SecondHandler-Delivery` | id of this delivery (the same on retries) |
| `X-SecondHandler-Timestamp` | unix seconds when this attempt was signed |
| `X-SecondHandler-Signature` | `sha256=` + hex HMAC-SHA256 of `"<timestamp>.<body>"` with the webhook secret |

Answer with any 2xx. Anything else, or no answer within 10 s, is retried after 30 s, 2 min, 10 min, 30 min and then
hourly; after 24 h the delivery is given up (Settings → AI agent shows it). The feed (`/api/events?since=`) still has
every event, so an agent that was offline catches up there.

Check the signature (Python):

```python
import hashlib, hmac

def valid(secret: str, headers, body: bytes) -> bool:
    msg = headers["X-SecondHandler-Timestamp"].encode() + b"." + body
    sig = "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, headers["X-SecondHandler-Signature"])
```

## Settings keys

| key | |
|---|---|
| `agent_token` | the API token (empty: no bearer access) |
| `agent_webhook_url` | the webhook (empty: no pushes) |
| `agent_webhook_secret` | the signing secret, created with the first webhook URL |
| `agent_kinds` | comma-separated kinds sent to the webhook; default `purchase,sale,paylivery,shipping,message` |
