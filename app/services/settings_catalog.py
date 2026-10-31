"""The map of the settings: groups, sections and the fields on every page.

One source of truth for the section nav and the overview (settings.html), the command palette (base.html) and the
settings search (/settings/search.json): what is where, and under which words it is found. Labels are the templates'
own English strings, translated at request time, so a search in German finds what the German page shows; keywords add
synonyms. An item's anchor is the id of its label or card on the section page (/settings/<section>#<anchor>): the page
scrolls there and marks it. The other sites' pages (settings/platform.html) share one set of items.

Order: by what you do with the app, from the connections outward: the sites and the VPN, how and how often they are
asked (pace, price rating, Geizhals), buying, selling (answers, your ads, appointments), shipping, Claude, the ways
the app reaches you, and the system.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.i18n import t as _t


@dataclass(frozen=True)
class Item:
    anchor: str          # id of the field's label or card on the section page
    label: str           # as the page shows it
    keywords: str = ""   # synonyms, comma separated


@dataclass(frozen=True)
class Section:
    id: str
    label: str
    description: str     # one line under the label on the overview; searched too
    items: tuple[Item, ...] = ()
    attention: str = ""  # the overview's "Needs attention" line when the section has a bad / warn flag


@dataclass(frozen=True)
class Group:
    label: str
    sections: tuple[Section, ...]


# the sections with a page of their own, in nav order (main.SETTINGS_SECTIONS); the other sites are added at runtime
SECTION_IDS = ("platforms", "willhaben", "quiet", "prices", "geizhals", "purchase", "paylivery", "selling", "chats", "myads",
               "calendar", "shipping", "printers", "claude", "rules", "ha", "telegram", "browser", "vpn", "backup",
               "timeline", "web", "language")
OVERVIEW = "overview"    # /settings: the overview, not a section of SECTION_IDS


def _plain(text: str, /, **kwargs: Any) -> str:
    """The English source string itself (the index carries both languages)."""
    return text.format(**kwargs) if kwargs else text


def _site_items(t: Callable[..., str], p: Any) -> tuple[Item, ...]:
    """settings/platform.html: the same fields for every site other than willhaben, plus the site's own ones."""
    items = [Item("s-account", t("{site} account", site=p.name), t("login, log in, logged in, messages")),
             Item("s-enabled", t("Enabled"), t("switch on, switch off, activate")),
             Item("s-gap", t("Minimum gap between requests (s)"), t("pace, pacing, throttle, rate")),
             Item("s-budget", t("Request budget per hour (0 = none, empty = as willhaben)"), t("rate limit, requests")),
             Item("s-product-search", t("Use for the product search")),
             Item("s-stats", t("Statistics"), t("requests in the last hour, blocked")),
             Item("s-vpn-site", t("Anonymous requests through the VPN"), t("VPN, WireGuard"))]
    if p.id == "kleinanzeigen":
        items.insert(4, Item("s-auth", t("App credentials (user:password)"),
                             t("API, 401, 403, KLEINANZEIGEN_API_AUTH, login")))
    if p.id == "facebook":
        items.insert(4, Item("s-fb-location", t("Location for keyword searches (product search): a Marketplace link from facebook.com"),
                             t("place, radius, Marketplace")))
    return tuple(items)


def build(sites: Iterable[Any], t: Callable[..., str] = _t) -> list[Group]:
    """All groups with their sections. `sites`: the platforms other than willhaben (app.platforms), each with a page
    next to willhaben's. `t`: the translator (the English source strings with _plain)."""
    willhaben = Section("willhaben", "willhaben", t("Account and browser session, the hourly request budget, use in the product search."),
                        (Item("s-account", t("{site} account", site="willhaben"), t("login, log in, logged in, browser session")),
                         Item("s-browser-window", t("Browser window and request recording"), t("remote view, session, recorder, API recording")),
                         Item("s-budget", t("Request budget per hour"), t("rate limit, requests, pace, 0 = no budget")),
                         Item("s-product-search", t("Use for the product search")),
                         Item("s-stats", t("Requests to willhaben"), t("statistics, used this hour")),
                         Item("s-vpn-site", t("Anonymous requests through the VPN"), t("VPN, WireGuard"))),
                        t("willhaben: not logged in"))
    others = tuple(Section(p.id, p.name, t("Switch the site on or off, its pace and request budget, its account for messages."),
                           _site_items(t, p)) for p in sites)
    vpn = Section("vpn", t("VPN"), t("WireGuard configs for the anonymous requests, which sites use them, the server test."),
                  (Item("s-vpn-configs", t("WireGuard configs"), t("upload, .conf, .zip, profile, collection, Mullvad, wireproxy")),
                   Item("vpn-test", t("Connection"), t("server, test now, search now, working servers")),
                   Item("s-vpn-hours", t("Hours between two regular searches"), t("interval")),
                   Item("vpn-sites", t("Sites through the VPN"), t("Geizhals, switch, anonymous requests")),
                   Item("s-vpn-upload-zip", t("Config collection (.zip)"), t("upload collection")),
                   Item("s-vpn-upload-conf", t("Dedicated profile (.conf)"), t("upload profile")),
                   Item("s-vpn-forget", t("Forget blocks"), t("skipped servers, refused")),
                   Item("s-vpn-remove", t("Remove configs"), t("delete WireGuard configs"))),
                  t("VPN: the connection has a problem"))
    quiet = Section("quiet", t("Quiet hours & pace"), t("Fewer requests at night, human-like timing and adaptive polling of the search agents."),
                    (Item("s-quiet", t("Quiet hours"), t("night, sleep, pause, fewer requests")),
                     Item("s-quiet-from", t("From"), t("start of the quiet hours, time")),
                     Item("s-quiet-until", t("Until"), t("end of the quiet hours, time")),
                     Item("s-quiet-mode", t("During quiet hours"), t("pause agents, check every")),
                     Item("s-quiet-interval", t("Interval during quiet hours (min)"), t("minutes")),
                     Item("s-human", t("Human-like timing"), t("jitter, random, automated")),
                     Item("s-breaks", t("Take a break now and then (skip about one check in twelve)"), t("breaks, skip checks")),
                     Item("s-adaptive", t("Adaptive polling"), t("learned profile, slow down, hours of the day, new ads per hour")),
                     Item("s-adaptive-max", t("Slowest pace in quiet hours of the day"), t("factor, interval"))))
    prices = Section("prices", t("Price rating"), t("From which share of the new price an ad counts as a bargain, cheap, fair or expensive."),
                     (Item("s-prices-used", t("Used items"), t("bargain, cheap, fair, expensive, zones, levels, thresholds, share of the new price")),
                      Item("s-prices-new", t("New items"), t("bargain, cheap, fair, expensive, zones, levels, thresholds, shop deals")),
                      Item("s-prices-reset", t("Reset to default values"), t("defaults"))))
    geizhals = Section("geizhals", "Geizhals", t("The Geizhals searches and product pages kept locally, and clearing that cache."),
                       (Item("s-geizhals-cache", t("Cached lookups"), t("clear cache, delete, new price, product pages")),))
    purchase = Section("purchase", t("Buying rules"), t("Purchase orders on or off, PayLivery preferred, your rules and the wish-price discount."),
                       (Item("s-purchase-enabled", t("Allow purchase orders (Buy in the product search and the ad viewer; includes PayLivery offers)"), t("buy, purchase, agent, negotiate")),
                        Item("s-purchase-paylivery", t("Prefer buying with PayLivery (preselected in the buy dialog)"), t("buy dialog")),
                        Item("s-purchase-rules", t("My rules for purchases"), t("private sellers, invoice, pickup")),
                        Item("s-purchase-discount", t("Discount for the suggested wish price (%)"), t("maximum price, asking price, offer"))))
    paylivery = Section("paylivery", "PayLivery", t("Pushes when a PayLivery purchase or sale moves to its next stage."),
                        (Item("s-paylivery-notify", t("Notify me about PayLivery stage changes"), t("pay now, paid, shipped, notification")),))
    selling = Section("selling", t("Selling preferences"), t("Handover, shipping costs, delivery, carrier and payment: Claude follows them with buyers and in new ads."),
                      (Item("s-sell-handover", t("Handover"), t("self pickup, shipping, both, preset for new ads")),
                       Item("s-sell-shipping-cost", t("Shipping costs"), t("paid by the buyer, included in the price")),
                       Item("s-sell-delivery", t("Delivery to the buyer"), t("parcel shop, locker, front door")),
                       Item("s-sell-carrier", t("Preferred carrier"), t("cheapest, Post, DPD, GLS")),
                       Item("s-sell-payment", t("Payment"), t("bank transfer, cash on pickup, PayLivery")),
                       Item("s-sell-notes", t("More selling facts for Claude"), t("invoice, weekdays, free text")),
                       Item("s-sell-always", t("Always included"), t("contact rule, e-mail addresses, links, censored"))))
    chats = Section("chats", t("Automatic answers"), t("Automatic answers and drafts, pushes for chats, how many conversations sync, human-like reply timing."),
                    (Item("s-chats-rules", t("General instruction for automatic answers"), t("auto-reply, instruction, prompt")),
                     Item("s-chats-draft", t("Draft replies automatically"), t("reply draft, new messages")),
                     Item("s-chats-notify", t("Notify me about new messages"), t("push, notification")),
                     Item("s-chats-push", t("Pushes for chats go to"), t("Telegram, Home Assistant, both, automatic")),
                     Item("s-chats-sync", t("Sync the latest conversations"), t("how many chats, limit")),
                     Item("s-chats-days", t("Only conversations active in the last … days"), t("older conversations, chat list")),
                     Item("s-chats-humanize", t("Human-like answers"), t("read receipt, delay, typing time, random")),
                     Item("s-chats-delay", t("Answer after at least (seconds)"), t("delay, at most, timing")),
                     Item("s-chats-fast", t("In ongoing chats (after my first reply): at least (seconds)"), t("delay, timing")),
                     Item("s-chats-test", t("Test chat connection"), t("sending via willhaben, confirmed"))))
    myads = Section("myads", t("My ads"), t("Expiry warnings for your own listings and how long expired ads stay listed."),
                    (Item("s-myads-warn", t("Warn before your own ads expire (days)"), t("expiry, expiring, listing, reactivation")),
                     Item("s-myads-expired", t("Show expired ads in My ads for (days)"), t("expired, inactive"))))
    calendar = Section("calendar", "Google Calendar", t("Handover appointments: busy calendars, the handover window, free times in replies."),
                       (Item("s-cal-connect", t("Connect Google Calendar"), t("Google Cloud, client, OAuth, sign in, Desktop app, JSON")),
                        Item("s-cal-busy", t("Calendars that count as busy"), t("free/busy")),
                        Item("s-cal-target", t("New appointments go into"), t("calendar for appointments")),
                        Item("s-cal-window", t("Handovers possible from"), t("until, time window, meetup")),
                        Item("s-cal-days", t("Look ahead (days)")),
                        Item("s-cal-duration", t("Default length of an appointment (minutes)"), t("duration")),
                        Item("s-cal-buffer", t("Buffer before and after each appointment (minutes, e.g. travel time)")),
                        Item("s-cal-drafts", t("Use my free times in reply drafts"), t("pickup times, suggest")),
                        Item("s-cal-auto", t("Also in automatic answers")),
                        Item("s-cal-add", t("Add agreed appointments to the calendar by myself"), t("undo")),
                        Item("s-cal-disconnect", t("Disconnect"), t("Google, remove"))))
    shipping = Section("shipping", t("Shipping address"), t("Sender address, pickup radius, carriers and their logins, automatic label purchase, payment card."),
                       (Item("s-ship-name", t("Name"), t("sender address")),
                        Item("s-ship-street", t("Street"), t("sender address, house number, stair, door")),
                        Item("s-ship-zip", t("Postcode"), t("sender address, ZIP")),
                        Item("s-ship-city", t("City"), t("sender address")),
                        Item("s-ship-phone", t("Phone"), t("sender address")),
                        Item("s-ship-email", t("E-mail"), t("sender address")),
                        Item("s-ship-pickup", t("Pickup only nearby"), t("distance, km from home, radius, filtered out")),
                        Item("s-ship-carriers", t("Carriers"), t("Post, DPD, GLS, Hermes, parcel")),
                        Item("s-ship-detect", t("Notice addresses in chats automatically and offer a label"), t("address detection")),
                        Item("s-ship-logins", t("Carrier logins"), t("user name, password, carrier page")),
                        Item("payment-card", t("Payment card and checkout playbooks"), t("credit card, card number, CVC, expiry")),
                        Item("s-ship-playbooks", t("Learned checkouts"), t("playbook, reset")),
                        Item("s-ship-auto", t("Automatic label purchase"), t("buy labels automatically, after payment")),
                        Item("s-ship-auto-price", t("Max. label price (€)")),
                        Item("s-ship-auto-wait", t("Wait before buying (min, 0 = at once)"), t("grace period")),
                        Item("s-ship-auto-daily", t("Max. automatic purchases per day")),
                        Item("s-ship-auto-carriers", t("Carriers allowed for automatic buying"))),
                       t("Sender address missing — parcels can't be prepared from chats yet."))
    printers = Section("printers", t("Printers"), t("Which printer gets the labels, a label or a normal printer per carrier, automatic printing."),
                       (Item("s-print-auto", t("Print every label automatically"), t("auto print")),
                        Item("s-print-label", t("Label printer"), t("IPP address, media, tray, Brother QL, CUPS")),
                        Item("s-print-normal", t("Normal printer"), t("IPP address, media, tray, A4, AirPrint")),
                        Item("s-print-crop", t("Per carrier: media and crop"), t("label size, 100 × 150 mm")),
                        Item("s-print-per-carrier", t("Which printer per carrier")),
                        Item("s-print-test", t("Test page"), t("check printer, state")),
                        Item("s-print-help", t("How the printers are reached"), t("IPP, CUPS, USB, media names"))))
    claude = Section("claude", t("Claude & usage"), t("Sign-in to Claude, the model per task, the monthly token limit and the token usage."),
                     (Item("s-claude-account", t("Claude subscription"), t("sign in, log in, test, code from claude.com")),
                      Item("s-claude-models", t("Model per task"), t("Haiku, Sonnet, Opus, model list, costs")),
                      Item("s-claude-limit", t("Monthly token limit"), t("budget, tokens / month, throttle")),
                      Item("s-claude-limit-mode", t("When the limit is reached"), t("pause optional tasks, switch to Haiku, only notify")),
                      Item("s-claude-usage", t("Token usage"), t("statistics, calls, API price, per task"))),
                     t("Claude is not signed in."))
    rules = Section("rules", t("AI rules and prompts"), t("Everything Claude is told: your rules, your writing style, the system prompts with their defaults, and the built-in prompts."),
                    (Item("s-rules", t("General rules"), t("instructions, prompt, free text, tone")),
                     Item("s-ai-sell-rules", t("Rules when selling"), t("selling, buyers, facts, invoice")),
                     Item("s-ai-auto-rules", t("Additionally for automatic answers"), t("auto-reply, instruction, prompt")),
                     Item("s-ai-purchase-rules", t("Rules for purchase orders (automatic negotiation)"), t("buying, private sellers, invoice, pickup")),
                     Item("style-card", t("Writing style"), t("style, how I write, derive")),
                     Item("s-ai-prompts", t("System prompts"), t("system prompt, default, reset, difference")),
                     Item("s-ai-builtin", t("Built-in prompts (read only)"), t("screening, product search, Geizhals, shipping")),
                     Item("s-rules-geizhals", t("Always add the Geizhals spec sheet to new listings when the product is found"), t("specifications, manufacturer data"))))
    ha = Section("ha", "Home Assistant", t("Connection to Home Assistant: pushes to your phone and events for automations."),
                 (Item("s-ha-connect", t("Connect"), t("Home Assistant, sign in, token, reconnect, disconnect, test event")),
                  Item("s-ha-url", "URL", t("web address of Home Assistant, homeassistant.local")),
                  Item("s-ha-service", t("Phone for pushes"), t("notify service, mobile app, push target")),
                  Item("s-ha-events", t("Events the app fires"), t("automations, willhaben_hit, willhaben_message"))))
    telegram = Section("telegram", "Telegram", t("The Telegram bot: link your chat and choose which notifications it sends."),
                       (Item("s-tg-bot", t("Bot token"), t("BotFather, connect bot, link, pairing, code, QR")),
                        Item("s-tg-test", t("Send test message"), t("unlink")),
                        Item("s-tg-remove", t("Remove bot")),
                        Item("s-tg-enabled", t("Send notifications to Telegram"), t("push")),
                        Item("s-tg-kinds", t("Which notifications"), t("hits, price drops, messages, kinds")),
                        Item("s-tg-commands", t("Open answer drafts"), t("/drafts, /auto, /status, commands, reply by swipe"))))
    browser = Section("browser", t("Browser"), t("New hits and price drops as notifications of this browser while a tab of the app is open."),
                      (Item("s-browser-notify", t("Show new hits as notifications"), t("desktop notifications, permission, this computer")),
                       Item("s-browser-test", t("Send a test notification"))))
    backup = Section("backup", t("Backup & restore"), t("Download everything as one ZIP and bring it back later, e.g. on a new server."),
                     (Item("s-backup-download", t("Download backup"), t("export, ZIP, save everything")),
                      Item("s-backup-profile", t("Include the willhaben login")),
                      Item("s-backup-passphrase", t("Passphrase (optional, encrypts the ZIP)"), t("encrypt, password")),
                      Item("s-backup-restore", t("Restore"), t("import, upload, bring back")),
                      Item("s-backup-auto", t("Automatic backups"), t("before imports, safety copies"))))
    timeline = Section("timeline", t("Timeline"), t("Recording and retention of the timeline, the stored requests to the sites, the fixture export."),
                       (Item("s-tl-record", t("Record the timeline"), t("Claude calls, prompt, log, history")),
                        Item("s-tl-cleanup", t("Delete old entries automatically"), t("retention, keep for days, size limit, MB")),
                        Item("s-tl-prune", t("Delete old entries now")),
                        Item("s-tl-requests", t("Requests to the sites"), t("recorder, record requests, replay, WH_REPLAY")),
                        Item("s-tl-delete-requests", t("Delete all stored requests")),
                        Item("s-tl-export", t("Export as test fixtures"), t("download ZIP, redacted"))))
    web = Section("web", t("Web address"), t("The address of this dashboard, used by links in notifications and messages."),
                  (Item("s-web-url", t("Address of this dashboard"), t("public URL, PUBLIC_URL, https, links in notifications, phone")),),
                  t("Web address: only this computer can open it, so notification links open the ad on its site."))
    language = Section("language", t("Language & appearance"), t("Language of the dashboard and notifications; light or dark theme."),
                       (Item("s-lang", t("Interface"), t("language, German, English, Deutsch")),
                        Item("s-theme", t("Theme"), t("dark mode, light, system, appearance"))))
    allp = Section("platforms", t("All platforms"), t("The request budget and the gap between requests every platform uses unless it has its own."),
                   (Item("s-all-budget", t("Request budget per hour (0 = none)"), t("default, rate limit, requests, every site")),
                    Item("s-all-gap", t("Minimum gap between requests (s)"), t("default, pace, pacing, throttle, every site"))))
    return [Group(t("Platforms"), (allp, willhaben, *others)),
            Group(t("Search & prices"), (quiet, prices, geizhals)),
            Group(t("Buying"), (purchase, paylivery)),
            Group(t("Selling"), (selling, chats, myads, calendar)),
            Group(t("Shipping & pickup"), (shipping, printers)),
            Group(t("AI"), (claude, rules)),
            Group(t("Notifications"), (ha, telegram, browser)),
            Group(t("System"), (vpn, backup, timeline, web, language))]


def sections(sites: Iterable[Any]) -> dict[str, tuple[Group, Section]]:
    """Section id -> (its group, the section), translated."""
    return {s.id: (g, s) for g in build(sites) for s in g.sections}


def _both(a: str, b: str) -> list[str]:
    return [a] if a == b else [a, b]


def index(sites: Iterable[Any]) -> dict[str, Any]:
    """The search index: every section with its items, each text in the UI language and in English (one entry when
    they are the same). The JSON of /settings/search.json."""
    sites = list(sites)
    out = []
    for g, ge in zip(build(sites), build(sites, _plain), strict=True):
        for s, se in zip(g.sections, ge.sections, strict=True):
            out.append({"id": s.id, "group": _both(g.label, ge.label), "label": _both(s.label, se.label),
                        "description": _both(s.description, se.description),
                        "items": [{"anchor": i.anchor, "label": _both(i.label, ie.label), "keywords": _both(i.keywords, ie.keywords)}
                                  for i, ie in zip(s.items, se.items, strict=True)]})
    return {"sections": out}
