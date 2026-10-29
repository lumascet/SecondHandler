"""SQLite persistence (SQLModel)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import JSON, Column, Index, LargeBinary, String, UniqueConstraint, event
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _platform_column() -> Column:
    """The site a row belongs to (app/platforms); old rows are willhaben's."""
    return Column(String, nullable=False, server_default="willhaben", index=True)


class SearchAgent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    platform: str = Field(default="willhaben", sa_column=_platform_column())
    source_url: str                     # the search as a browser link (= spec["url"]; what "on <site>" opens)
    webapi_url: str                     # willhaben: the JSON search URL (= spec["api"]); other sites: = source_url
    spec: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # app.platforms.SearchSpec; None = old row
    interval_s: int = 300
    enabled: bool = True
    notify: bool = True
    notify_price_drops: bool = True
    baseline_done: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_rows_found: int | None = None
    screen: bool | None = None          # let Claude check new hits before they push
    screen_brief: str | None = None     # what exactly the owner is looking for (for the screening)
    screen_min: str | None = None       # match | fair | good: lowest result that still pushes
    screen_hide: str | None = None      # comma list of screening issues that keep a hit from pushing; None = defaults, "-" = none
    geizhals_pct: int | None = None     # push only ads at least this many % below the Geizhals new price; None = off
    # or: push only ads in this price zone or a cheaper one (bargain | cheap | fair, app/services/price_zones.py);
    # set instead of geizhals_pct, never both
    geizhals_level: str | None = None
    geizhals_check: bool | None = None  # with Claude screening: Claude checks each new hit's Geizhals product and
                                        # looks for the right one (app/services/geizhals_check.py)
    # auto-buy (app/services/auto_buy.py): a hit that passes this agent and costs at most auto_buy_max (item,
    # shipping and buyer protection) is bought with PayLivery's "buy now" after a countdown
    auto_buy: bool | None = None
    auto_buy_max: float | None = None
    # created from a product search: several keyword searches per check (source_url/webapi_url mirror the first)
    # [{keyword, webapi_url, source_url, price_from, price_to, category_path, why}]
    queries: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {max_price, must_have, nice_to_have, exclude, summary}
    search_id: int | None = None        # origin ProductSearch
    max_km: int | None = None           # pickup-only hits farther from home than this are held back; None = any


class Hit(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("agent_id", "ad_id"),)
    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="searchagent.id", index=True)
    platform: str = Field(default="willhaben", sa_column=_platform_column())
    ad_id: str = Field(index=True)      # the site's own id; in links it travels as a ref (app.platforms.ad_ref)
    heading: str
    body: str | None = None
    price: float | None = None
    price_display: str | None = None
    old_price: float | None = None
    location: str | None = None
    postcode: str | None = None
    published_at: datetime | None = None
    end_date: datetime | None = None
    url: str
    image_url: str | None = None
    # the thumbnail kept locally once the hit is saved (data/hit-images): the sites drop the photos of sold or deleted
    # ads and Vinted's photo links expire, so the card would lose it
    image_path: str | None = None
    is_private: bool | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    state: str = "new"  # new | seen | saved | ignored | filtered (held back by the screening, no push)
    price_dropped_at: datetime | None = None
    bumped_count: int = 0
    screen_relevant: bool | None = None  # Claude: the item searched for? None = not screened
    screen_deal: str | None = None       # great | good | fair | overpriced | unknown
    screen_reason: str | None = None
    screen_issues: list[str] | None = Field(default=None, sa_column=Column(JSON))  # predefined reasons (screening.ISSUES)
    screen_at: datetime | None = None
    scam_flags: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # app/services/scam.py
    # the matching Geizhals product {name, url, price_from} for an agent's Geizhals rule; {} = none found, None = not asked
    geizhals: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # its similar ads' rating when Geizhals knows no new price (the agent's price-zone rule): {level, median, count, at}
    similar: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    photo_hash: str | None = None      # dHash of its first photo (services/photo_match), "-" = not loadable
    # the condition the site states (app/platforms/condition.py): from the list, or from the ad page once opened (Shpock)
    item_condition: str | None = None
    item_condition_label: str | None = None
    # what the site said when the ad itself was last loaded (sync button, ad viewer): active | sold | reserved |
    # expired | gone (no longer online) | the site's own word; None = never loaded
    site_status: str | None = None
    status_checked_at: datetime | None = None
    # shipping to the owner (app/platforms/shipping_info.py): price (0 = free) and "ship" | "pickup"; None = not known
    shipping_price: float | None = None
    shipping: str | None = None
    buyer_fee: float | None = None       # the buyer fee the site states (Vinted's buyer protection), app/platforms/fees.py
    # willhaben: PayLivery on (list or ad page); "ship" alone may be the ad's handover "Versand" without it. None = not known
    paylivery: bool | None = None
    # found by an agent's first check (or a new search's): judged like a new hit but never pushed (search_agents)
    quiet: bool | None = None
    # the ad's place: the site's coordinates (willhaben, Kleinanzeigen) or its postcode's centre (app/services/geo.py)
    lat: float | None = None
    lon: float | None = None


class Conversation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    platform: str | None = None      # None: willhaben; "vinted" (app/platforms/vinted_inbox.py)
    ad_id: str | None = Field(default=None, index=True)
    ad_title: str | None = None
    partner_name: str | None = None
    unread_count: int = 0
    last_message_text: str | None = None
    last_message_at: datetime | None = None
    role: str | None = None  # seller | buyer
    ad_context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    ad_context_at: datetime | None = None   # when ad_context was fetched (chats.ensure_ad_context refreshes it after a while)
    synced_at: datetime | None = None
    messages_synced_at: datetime | None = None
    # appointment agreed in this chat: {start (local ISO), minutes, location, summary, event_id?, event_link?}
    meetup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # PayLivery process of this chat (app/services/paylivery.py): role, stage, amounts, deadline, history, applied notices
    paylivery: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # hidden from the chat list (only here, not on willhaben) until the other person writes again
    hidden_at: datetime | None = None
    removed: bool | None = None    # also hidden, and its messages/drafts deleted here; the row stays so sync skips it
    drafting: bool | None = None   # Claude is writing an answer as a background job (chats.enqueue_draft)
    draft_error: str | None = None # why the last background draft failed (shown next to "Prepare answer")
    # when the owner last looked at this chat in the app: newer messages from the other person count as unread here
    # (the sidebar badge), whatever the site says (an automatic answer marks them read there before anyone looked)
    app_seen_at: datetime | None = None
    # marked finished by me ("Mark as finished"): no answer due any more; a newer message from them opens it again
    finished_at: datetime | None = None
    finished_by: str | None = None      # "claude" when Claude judged it over (services/chat_done), else me
    done_checked_at: datetime | None = None   # the last message Claude judged "over or not" for (asked once each)
    marked_unread: bool | None = None   # "Mark as unread": counts as unread here until I open it again
    partner_uuid: str | None = None     # the other person's willhaben user uuid: their online state (presence)
    ad_uuid: str | None = None          # willhaben's internal ad uuid (ad_info.uuid): with partner_uuid, their profile page
    partner_place: str | None = None    # where they are, from their public profile ("8530 Deutschlandsberg")
    partner_place_tried: bool | None = None   # their profile was read once (a place or none)
    # their account, from the same profile page (services/trust): created when, ratings received, reply time
    partner_since: datetime | None = None
    partner_ratings: int | None = None
    partner_reply_time: str | None = None
    partner_trust_at: datetime | None = None  # when the profile was read for these (read again after a week)


class HitVote(SQLModel, table=True):
    """The owner's verdict on a hit (swiped, or Save / Ignore): liked or not, kept after the hit is cleared – what the
    owner wants, per search agent (services/taste.py refines the agent's instruction from it)."""
    __table_args__ = (UniqueConstraint("agent_id", "platform", "ad_id"),)
    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(index=True)
    platform: str = "willhaben"
    ad_id: str
    heading: str
    price: float | None = None
    condition: str | None = None
    location: str | None = None
    body: str | None = None                        # the start of the ad's text
    reason: str | None = None                      # Claude's screening note at the time, if any
    liked: bool
    created_at: datetime = Field(default_factory=utcnow)


class AdTranslation(SQLModel, table=True):
    """An ad text in another language than German or English, translated once (services/translate.translate_ad)."""
    key: str = Field(primary_key=True)             # "<platform>:<ad id>"
    text_hash: str                                  # of the text translated: a changed description is asked again
    lang: str | None = None
    translation: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("conversation_id", "ext_id"),)
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ext_id: str
    direction: str  # in | out | unknown
    text: str
    created_at: datetime | None = None
    kind: str | None = None  # system | integration: willhaben's own notice (see app/services/paylivery.py); None = normal
    params: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # willhaben's structured `parameters`
    # my own messages (direction out): the other person has seen it - willhaben's read flag, the chat list's flag for
    # the newest message, or a live read event (chats.mark_seen). None = not known yet; a sent message never unsees.
    read: bool | None = None
    read_at: datetime | None = None   # when they saw it, if a live read event told (the history's flag has no time)
    lang: str | None = None           # its language (services/translate; "-" none), None = not asked yet
    translation: str | None = None    # into the app's language, when it is another


class Draft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    text: str
    intent: str | None = None
    needs_info: list[str] | None = Field(default=None, sa_column=Column(JSON))
    suggested_price: float | None = None
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    lang: str | None = None            # its language (services/translate), None = not asked yet
    translation: str | None = None     # into the app's language, when the draft is in another (the partner's own)
    status: str = "draft"  # draft | pending (auto reply "typing") | sending (queued send job) | sent | discarded
    dismissed: bool | None = None     # discarded by the owner (not by the app): the buyer's message counts as handled
    sent_at: datetime | None = None
    error: str | None = None
    auto: bool | None = None          # created by the auto-reply job
    auto_reason: str | None = None    # why it was / was not sent automatically
    feedback: str | None = None       # up | down, from Telegram buttons
    feedback_note: str | None = None  # what was wrong (goes into later automatic answers)
    meetup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # appointment this answer fixes
    alternatives: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # Claude's other decisions: label, instruction
    purchase: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # buyer side: Claude's purchase_state
    paylivery: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # the PayLivery step to go with it:
    #                                  {action: accept|counter|decline, amount, offer} (app/services/paylivery_advice.py)


class MyAd(SQLModel, table=True):
    ad_id: str = Field(primary_key=True)
    heading: str | None = None
    url: str | None = None
    image_url: str | None = None
    status: str = "unknown"  # active | expired | sold | reserved | deleted (gone from the account) | label | unknown
    published_at: datetime | None = None
    end_date: datetime | None = None
    auto_renew: bool = False
    notified_expiry_for: datetime | None = None
    last_checked_at: datetime | None = None
    last_action: str | None = None
    last_error: str | None = None
    auto_reply: bool | None = None                 # answer buyers of this ad automatically
    auto_reply_instructions: str | None = None     # ad-specific facts/limits for those answers
    auto_reply_before_sale: bool | None = None     # auto_reply as it was before "reserved"/"sold" switched it off
    #                                                (sold.finish); "Remove reservation" puts it back (sold.reset)
    min_price: float | None = None                 # floor for automatic answers: never agree below this (€, per piece)
    quantity: int | None = None                    # pieces offered in this ad; None = one item
    replaced_by: str | None = None    # republished (auto-renew) under this new ad id; old chats still point here
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # size/weight for shipping; source "user" = confirmed, "claude" = estimate
    hidden: bool | None = None        # hidden from the list by the user; the import keeps it hidden
    sale_status: str | None = None                 # sold | reserved, set by the sold workflow (/sold/{ad_id}); sticky locally
    sale_conversation_id: str | None = None        # the buyer's conversation
    sale_at: datetime | None = None
    pending_action: str | None = None              # activate | deactivate | delete | edit | photos | sold | reserved |
    #                                                unreserve: queued as a background job
    pending_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # what the job needs / left behind
    relist: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # a piece sold with PayLivery, more are
    #   left: willhaben takes the ad offline, the app puts it online again as a new ad (my_ads.relist). {since, checks,
    #   tries, draft: the willhaben draft of the copy once presaved}
    vinted: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # the ad also on Vinted
    #   (services/crosspost.py): {id, url, state draft|active|reserved|sold|hidden|gone, catalog, at, checked}


class Sale(SQLModel, table=True):
    """One sale of an own ad (the ledger, app/services/stock.py): an ad with a quantity sells piece by piece."""
    id: int | None = Field(default=None, primary_key=True)
    ad_id: str = Field(index=True)                 # the MyAd it was recorded on (a renewed ad: any copy of the chain)
    conversation_id: str | None = Field(default=None, index=True)   # the buyer's chat; None = sold outside willhaben
    pieces: int = 1
    price: float | None = None                     # total price of this sale (optional)
    note: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    undone_at: datetime | None = None              # "not sold after all": the row stays, counts no longer


class Shipment(SQLModel, table=True):
    """A parcel to a buyer: address from the chat, size/weight, chosen carrier option, label."""
    id: int | None = Field(default=None, primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    ad_id: str | None = None
    # draft (address/parcel being prepared, not paid) | awaiting (paid, needs the user's go) | buying (label is being
    # bought in the browser) | labelled | printed | sent | failed (buying/printing broke, see error) | discarded
    status: str = "draft"
    recipient: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # name, street, house_no, extra, zip, city, phone, email
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))     # length/width/height cm, weight_kg, contents, source
    option: str | None = None        # tariff key, e.g. "gls:S:shop"
    price: float | None = None       # published tariff price of the option
    label_path: str | None = None    # file in DATA_DIR/labels
    tracking: str | None = None
    note: str | None = None          # Claude's remark (missing data, oddities)
    # --- payment (the label is only bought once the buyer has paid) ---
    agreed_price: float | None = None          # price agreed in the chat (Claude reads it with the address)
    paid_at: datetime | None = None
    paid_via: str | None = None                # manual | paylivery | email | bank
    paid_amount: float | None = None           # amount seen by an automatic source (None for manual)
    payment_claimed_at: datetime | None = None # the buyer wrote "I have paid" (a hint, never a status)
    # --- approval and purchase ---
    approval: str | None = None                # pending | approved | auto | declined (buy_only: older rows)
    approved_at: datetime | None = None
    buy_log: list[Any] | None = Field(default=None, sa_column=Column(JSON))   # checkout steps [{step, url, action, note}]
    paid_price: float | None = None            # what the carrier's checkout actually charged
    error: str | None = None                   # last failure of buying/printing (status failed) or what the user must do
    # --- printing ---
    print_job: str | None = None               # printer job id/uri
    printed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class AdDraft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    input_text: str
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    title: str | None = None
    description: str | None = None
    category_hint: str | None = None
    condition: str | None = None
    attributes: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    search_keywords: str | None = None
    price_suggestion: float | None = None
    price_stats: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    comparables: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    model: str | None = None
    product_query: str | None = None   # exact product name Claude recognised (for Geizhals)
    geizhals: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))       # match + specs + candidates
    revisions: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # earlier versions, newest last
    wh_plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # willhaben category/fields to confirm
    wh_ad_id: str | None = None        # willhaben ad id once the insertion form created it (draft or live)
    status: str = "draft"  # draft | published | discarded
    error: str | None = None
    batch_id: int | None = None   # AdBatch this draft came from
    quantity: int | None = None        # pieces offered in the ad (price per piece); None = one item
    parcel: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # Claude's packed-size estimate, shipping.clean_parcel shape
    publishing: bool | None = None     # queued/running as a background publish job
    refining: bool | None = None       # Claude revises it as a background job (ad_creator.enqueue_refine)
    # what Claude could not tell from notes and photos: [{id, text, options}], answered on the draft page (never a
    # "(bitte prüfen)" in the text); None = nothing open
    questions: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    vinted_ad: str | None = None       # copied from an ad only on Vinted ("vinted-<id>"): published, the two are linked
    created_at: datetime = Field(default_factory=utcnow)


class AdBatch(SQLModel, table=True):
    """Photo dump of many articles: Claude sorts the photos into products, the user corrects, then one AdDraft per group."""
    id: int | None = Field(default=None, primary_key=True)
    notes: str = ""                     # applies to every article (pickup, origin, ...)
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # [{id, photos: [image index], product, product_query, observations, confidence, question, comment, include,
    #   draft_id, error}]; images in no group are unused
    groups: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = "sorting"  # sorting | review | creating | error
    error: str | None = None
    note: str | None = None             # Claude's remark on the sorting
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class DealCheck(SQLModel, table=True):
    """Price check of one ad (Produktsuche tab / ad viewer): last analysis and Claude's verdict, one row per ad.
    The key is the ad's ref: the bare id for willhaben, "<platform>:<id>" for other sites."""
    ad_id: str = Field(primary_key=True)
    platform: str | None = None
    title: str | None = None
    price: float | None = None
    price_display: str | None = None
    image_url: str | None = None
    url: str | None = None
    query: str | None = None
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # deals.analyze() output
    claude: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {deal, reason, model, at}
    checked_at: datetime | None = Field(default_factory=utcnow, index=True)


class ProductSearch(SQLModel, table=True):
    """Claude product search (Produktsuche tab): description -> planned willhaben searches -> ranked offers."""
    id: int | None = Field(default=None, primary_key=True)
    description: str = ""
    settings: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {depth, unusual, categories, sort, photos, ask}
    status: str = "planning"            # planning | searching | asking | ranking | photos | done | error
    plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))       # summary, max_price, criteria, queries, questions
    answers: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))    # {question id: answer}; {} = skipped
    cards: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # candidates while "asking"
    queries: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # per query: found, error
    results: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # ranked offers
    pool: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))     # offers left for "load more"
    passed: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))   # ranked, but Claude: no fit
    progress: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {done, total}
    requests: int | None = None         # willhaben searches this run sent
    candidates: int | None = None
    note: str | None = None             # Claude's overall remark
    warning: str | None = None          # e.g. stopped early after a willhaben block
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Purchase(SQLModel, table=True):
    """A purchase order (Kaufauftrag, app/services/purchase.py): the agent negotiates as buyer until the user pays."""
    id: int | None = Field(default=None, primary_key=True)
    ad_id: str = Field(index=True)
    ad_title: str | None = None
    ad_url: str | None = None
    image_url: str | None = None
    asking_price: float | None = None      # price of the ad when the order started
    median_price: float | None = None      # median of comparable ads (deals.analyze), if there are enough
    max_price: float | None = None         # code-enforced ceiling incl. shipping; default = asking price
    wish_price: float | None = None        # the user's wish price incl. shipping (and fee): the first offer; None = discount
    mode: str = "paylivery"                # paylivery | direct | pickup
    pickup_window: str | None = None       # pickup: the user's preferred time window (free text)
    pickup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # pickup: {start, minutes, location}
    questions: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # [{question, answer}] for the seller
    questions_sent_at: datetime | None = None  # when the answers went to the user with the agreement push
    status: str = "new"                    # see app/services/purchase.py STATUSES
    conversation_id: str | None = Field(default=None, index=True)
    opener_text: str | None = None         # the opening message, stored right before it is sent: never send it twice
    opener_sent_at: datetime | None = None
    agreed_price: float | None = None      # item price (without shipping)
    shipping_cost: float | None = None     # shipping in euro; the effective value is purchase.shipping_of(row)
    shipping_source: str | None = None     # "ad" (PayLivery price of the ad) | "chat" (named by the seller); else estimate
    agreed_total: float | None = None      # agreed item price + shipping (or the agreed total of the seller)
    fee: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # buyer fee rule of the ad's category (fees.FeeRule)
    market: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # {median, min, p25, count} at start
    payment: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # direct: {iban, holder, amount, reference}
    note: str | None = None                # last remark of Claude, or why it is paused / failed
    log: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))  # [{at, status, text}], newest last
    search_id: int | None = None           # ProductSearch the order came from
    agent_id: int | None = None            # the search agent whose auto-buy started it (app/services/auto_buy.py)
    query: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SimChat(SQLModel, table=True):
    """A test chat of the chat simulator (app/services/chat_sim.py): never sent, never pushed, no limits counted."""
    id: int | None = Field(default=None, primary_key=True)
    title: str = ""
    role: str = "seller"                   # seller: my ad, the partner buys | buyer: my purchase, the partner sells
    platform: str = "willhaben"            # willhaben | kleinanzeigen | vinted
    ad_id: str | None = None               # seller: the own ad (min. price, instructions, stock read live from it)
    purchase_id: int | None = None         # buyer: the purchase it was copied from
    setup: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # the ad / purchase as simulated
    persona: str | None = None             # chat_sim.PERSONAS key, "custom" (persona_text) or None
    persona_text: str | None = None
    model: str | None = None               # Claude model for my answers (None: Settings → Claude)
    partner_model: str | None = None       # … and for the other side Claude plays
    turns: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))   # see chat_sim
    scenario: bool = False                 # kept as a repeatable test with expectations
    expectations: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))   # [{kind, value}]
    last_run: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # {at, passed, results, turns, variant}
    proposal_run: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # the same with the open proposal
    # rounds of refining the system prompt on this chat (chat_sim.iterate): [{n, at, proposal_id, summary, turns,
    # results, passed, feedback}]
    iterations: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    busy: str | None = None                # what runs in the background right now (pending marker "sim:<id>")
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SimProposal(SQLModel, table=True):
    """A rule change Claude proposes from the simulator's ratings and failed scenarios, applied only on request."""
    id: int | None = Field(default=None, primary_key=True)
    role: str = "seller"
    target: str = "auto_reply_rules"       # auto_reply_rules | ad_instructions | purchase_rules
    ad_id: str | None = None               # ad_instructions: the ad
    old_text: str = ""
    new_text: str = ""
    summary: str = ""
    status: str = "open"                   # open | applied | discarded
    created_at: datetime = Field(default_factory=utcnow)


class TelegramAction(SQLModel, table=True):
    """What a Telegram button does: callback_data is `t:<token>` (Telegram allows only 64 bytes)."""
    token: str = Field(primary_key=True)
    verb: str
    ref: str
    created_at: datetime = Field(default_factory=utcnow, index=True)


class TelegramMessage(SQLModel, table=True):
    """Telegram message sent for a notification tag, so a later notification with that tag edits it."""
    tag: str = Field(primary_key=True)
    chat_id: int
    message_id: int
    photo: bool = False
    text: str | None = None          # last text we put there, so a status line can be appended by an edit


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class GeizhalsCache(SQLModel, table=True):
    """Geizhals answers kept locally (app/services/geizhals.py): a search's hits or a product page, by the URL asked.
    Used for CACHE_DAYS after `fetched_at`, then fetched again on the next lookup; gone CACHE_DAYS after `used_at`."""
    url: str = Field(primary_key=True)
    data: Any = Field(sa_column=Column(JSON))
    fetched_at: float = 0.0      # unix time of the last request to Geizhals for it
    used_at: float = 0.0         # unix time of the last lookup
    hits: int | None = None      # lookups it answered without a request (the cache page's statistics)
    fetches: int | None = None   # requests to Geizhals for it (the first and every refresh)


class LLMCall(SQLModel, table=True):
    """One Claude call (one row per model it used), for the token usage table in Settings → Claude."""
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    purpose: str = Field(index=True)          # a llm.base.PURPOSES key (chat | ads | screen | search | shipping | purchase | style) or other
    model: str
    ok: bool = True
    call_part: int | None = None              # 0 = first model of the call (counts as the call), 1.. = further models
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None             # the CLI's estimate; the subscription is not billed per token
    duration_ms: int | None = None
    error: str | None = None


class AccessCount(SQLModel, table=True):
    """willhaben requests per minute (app/willhaben/access_log.py): aggregated counters only, no paths, ids or texts."""
    minute: int = Field(primary_key=True)     # UTC epoch minute (unix time // 60)
    platform: str = Field(default="willhaben", primary_key=True)   # the site (app/platforms)
    channel: str = Field(primary_key=True)    # anonymous | account | chat
    kind: str = Field(primary_key=True)       # access_log.KINDS
    outcome: str = Field(primary_key=True)    # ok | client_error | blocked | error | cached
    count: int = 0


class EventLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    kind: str
    message: str
    url: str | None = None                 # the push's link (often willhaben); the dashboard prefers its own pages
    ad_id: str | None = None
    conversation_id: str | None = None
    quiet: bool | None = None              # sent without a push (a hit the screening held back): not in the inbox


class TimelineEvent(SQLModel, table=True):
    """One entry of the timeline (/timeline, app/services/timeline.py): a Claude call, a willhaben request, a user
    action, a background job or agent run, or a notification. Entity ids come from the timeline context, so one
    entry can belong to several things at once (a reply draft: chat + ad + purchase)."""
    __table_args__ = (Index("ix_timelineevent_kind_ts", "kind", "ts"),)
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    kind: str = Field(index=True)             # llm | wh | action | job | agent_run | sched | notify
    name: str | None = Field(default=None, index=True)   # llm: purpose; action: "chat.send"; job: Job.kind; wh: access kind
    actor: str | None = None                  # user | api | mcp | telegram | ha | scheduler | system
    title: str = ""
    ok: bool | None = Field(default=None, index=True)    # None: a run that is still going (record() sets it)
    error: str | None = None
    duration_ms: int | None = None
    run_id: int | None = Field(default=None, index=True)   # the job / agent run / scheduler entry this belongs to
    url: str | None = None
    ad_id: str | None = Field(default=None, index=True)
    conversation_id: str | None = Field(default=None, index=True)
    agent_id: int | None = Field(default=None, index=True)
    purchase_id: int | None = Field(default=None, index=True)
    search_id: int | None = Field(default=None, index=True)    # ProductSearch
    batch_id: int | None = Field(default=None, index=True)     # AdBatch
    draft_id: int | None = Field(default=None, index=True)     # AdDraft (chat reply drafts go to extra["reply_draft"])
    shipment_id: int | None = Field(default=None, index=True)
    payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))   # small facts: model, tokens, counts
    extra: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))     # rarer refs: hit_ids, reply_draft, mine
    has_blobs: bool = False


class TimelineBlob(SQLModel, table=True):
    """A long text of a timeline entry (system prompt, prompt, response), zlib-compressed when it is big."""
    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str                                 # system | prompt | schema | response
    encoding: str = "raw"                     # raw | zlib
    size: int = 0                             # uncompressed bytes
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))



class WhRecording(SQLModel, table=True):
    """One willhaben request with its answer (app/willhaben/recorder.py), for the timeline, offline replay
    (WH_REPLAY=1, app/willhaben/replay.py) and test fixtures (app/willhaben/export.py). Bodies are zlib-compressed;
    images are kept as size + hash only. Cookies and tokens in headers are redacted."""
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    event_id: int | None = Field(default=None, index=True)    # TimelineEvent.id (no FK: pruning order never matters)
    platform: str | None = Field(default=None, index=True)    # the site (app/platforms); None = willhaben (old rows)
    channel: str = Field(index=True)          # anonymous | account | chat | navigation | download | browser
    kind: str = Field(index=True)             # access_log.KINDS + chat_push | download
    method: str                               # GET/POST/PUT/DELETE | UPLOAD | WS | WSPUSH
    url: str
    final_url: str | None = None              # after redirects
    canonical_key: str = Field(index=True)    # recorder.canonical_key(): what replay matches on
    status: int | None = None                 # None: network error or WebSocket
    ws_status: str | None = None              # WebSocket: ok | success | error | timeout | ws_error | closed
    duration_ms: int | None = None
    outcome: str = "ok"                       # ok | client_error | blocked | error | cached
    cached: bool = False                      # answered from PublicClient's cache (no willhaben access)
    error: str | None = None
    req_headers: str | None = None            # JSON
    req_body: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    req_size: int = 0
    req_truncated: bool = False
    resp_headers: str | None = None           # JSON
    resp_body: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    resp_size: int = 0
    resp_truncated: bool = False
    content_type: str | None = None
    body_hash: str | None = None              # sha256 of the whole answer
    pruned: bool = False                      # bodies dropped to save space, the rest kept


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        # timeout: a writer waits up to 30 s for another one instead of failing at once with "database is locked";
        # WAL: readers never block the writer and the writer never blocks readers (the app writes from many tasks)
        _engine = create_engine(f"sqlite:///{s.db_path}", connect_args={"check_same_thread": False, "timeout": 30})
        event.listen(_engine, "connect", _sqlite_pragmas)
    return _engine


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")     # safe with WAL; a power cut may lose the last commit only
    finally:
        cur.close()


def set_engine(engine) -> None:
    """Used by tests to inject an in-memory database."""
    global _engine
    _engine = engine


def init_db() -> None:
    engine = get_engine()
    _rebuild_access_counts(engine)
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _follow_willhaben_budget()


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: new model fields become columns of existing tables when they are nullable or
    carry a server default (e.g. platform = 'willhaben' for rows from before there were other sites)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col_ in table.columns:
                if col_.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col_.name}" {col_.type.compile(engine.dialect)}'
                if col_.nullable:
                    conn.execute(text(ddl))
                elif col_.server_default is not None:
                    default = str(col_.server_default.arg).replace("'", "''")
                    conn.execute(text(f"{ddl} NOT NULL DEFAULT '{default}'"))
        for table in SQLModel.metadata.sorted_tables:
            if insp.has_table(table.name):
                for index in table.indexes:
                    index.create(conn, checkfirst=True)


def _rebuild_access_counts(engine) -> None:
    """accesscount got `platform` as part of its primary key: SQLite cannot add a key column, so the table is
    rebuilt once with the old counters kept as willhaben's."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("accesscount") or any(c["name"] == "platform" for c in insp.get_columns("accesscount")):
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE accesscount RENAME TO accesscount_old"))
    SQLModel.metadata.tables["accesscount"].create(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO accesscount (minute, platform, channel, kind, outcome, count) "
                          "SELECT minute, 'willhaben', channel, kind, outcome, count FROM accesscount_old"))
        conn.execute(text("DROP TABLE accesscount_old"))


def session() -> Session:
    return Session(get_engine(), expire_on_commit=False)


SETTING_PUBLIC_URL = "public_url"   # Settings → Web address; wins over PUBLIC_URL in .env


def apply_public_url() -> str:
    """Put the stored web address into the cached settings, so every get_settings().public_url sees it (at startup
    and after saving); without one, PUBLIC_URL from .env (or the default) applies again."""
    s = get_settings()
    s.public_url = get_setting(SETTING_PUBLIC_URL).strip() or type(s)().public_url
    return s.public_url


def log_event(kind: str, message: str, url: str | None = None, *, ad_id: str | None = None,
              conversation_id: str | None = None, quiet: bool = False) -> int | None:
    """Store a notification for the inbox and the timeline; returns its id. `quiet`: it went out without a push (the
    timeline keeps it, marked; the inbox, its badge and Home's activity leave it out)."""
    with session() as db:
        e = EventLog(kind=kind, message=message, url=url, ad_id=ad_id, conversation_id=conversation_id,
                     quiet=quiet or None)
        db.add(e)
        db.commit()
        event_id = e.id
    from app.services import timeline    # the notification also shows up in the timeline

    timeline.record("notify", message, name=kind, url=url, ad_id=ad_id, conversation_id=conversation_id,
                    extra={"push": False} if quiet else None)
    return event_id


DEFAULT_SETTINGS: dict[str, str] = {
    "reply_rules": (
        "- Privatverkauf, daher keine Garantie oder Rücknahme (nur erwähnen, wenn jemand danach fragt).\n"
        "- Abholung bevorzugt, Versand nur gegen Vorauskasse oder mit willhaben PayLivery.\n"
        "- Bei Preisverhandlung höchstens 10 % Nachlass, sonst freundlich ablehnen.\n"
        "- Du-Form, freundlich, kurz, keine Emojis."
    ),
    "auto_draft": "0",
    "notify_messages": "1",
    "weekly_digest": "1",
    "chat_translate": "1",
    "chat_auto_finish": "1",     # Claude marks chats it judges over as finished (services/chat_done)       # foreign chat messages: the translation under the original (services/translate)        # Mondays 8:00: the week in a few lines (services/digest)
    "chat_sync_limit": "10",    # sync only the N latest conversations
    "chat_sync_days": "",       # whole days: skip conversations whose last message is older; empty = no cutoff
    "expiry_warn_days": "3",
    "myads_expired_days": "30",
    "claude_token_limit_m": "10",   # Settings → Claude: monthly token limit in millions (sidebar meter); empty/0 = none
    "claude_limit_mode": "notify",
    "claude_limit_days": "7",       # Settings → Claude: the token limit's period in days (rolling)  # My ads: expired ads older than this are listed with the inactive ones; 0 = always shown
    "auto_reply_rules": (
        "- Automatisch nur einfache Fragen beantworten: ob der Artikel noch da ist, Abholung/Versand, Zustand laut Inserat.\n"
        "- Keine Preiszusagen und kein Handeln: bei Preisfragen auf den Inseratspreis verweisen oder nicht automatisch antworten.\n"
        "- Keine Termine, Adressen oder Telefonnummern zusagen, die nicht in der Anweisung zur Anzeige stehen."
    ),
    "send_verified": "0",
    # Settings → My rules → Writing style (app/services/style.py): the profile Claude derived from my own messages
    "reply_style": "",
    "reply_style_meta": "",       # JSON: {at, count, examples, model, note} of the last derivation
    # Settings → Selling preferences (app/services/selling.py): what Claude tells buyers and writes into new ads
    "sell_handover": "pickup",          # pickup | shipping | both
    "sell_shipping_cost": "buyer",      # buyer | included (in the price)
    "sell_delivery": "any",             # any | shop (parcel shop/locker) | home
    "sell_carrier": "",                 # shipping_tariffs.CARRIERS key; "" = cheapest
    "sell_payment": "transfer,paylivery,cash",
    "sell_notes": "",
    "reply_humanize": "1",      # auto replies: read receipt + randomized delay (app/services/humanize.py)
    "reply_delay_min_s": "40",
    "reply_delay_max_s": "180",
    "reply_fast_min_s": "5",    # ongoing chats (I answered before): reply within seconds
    "reply_fast_max_s": "20",
    "ui_lang": "auto",          # "auto" (browser), "en" or "de"; see app/i18n
    "ui_lang_seen": "",         # last language a browser asked for, used for pushes when ui_lang is auto
    # quiet hours for search agents (app/services/quiet_hours.py)
    "quiet_enabled": "1",
    "quiet_start": "01:00",
    "quiet_end": "07:00",
    "quiet_mode": "slow",
    "quiet_interval_min": "60",
    # adaptive polling: fewer checks in the hours that rarely bring new ads (app/services/adaptive_pace.py)
    "adaptive_enabled": "1",
    "watch_saved": "1",
    "pickup_max_km": "",        # pickup-only ads farther from home are not shown anywhere ("" = no limit; geo.py)         # saved hits: their ad pages about once a day (search_agents.watch_saved_job)
    "adaptive_max_factor": "4",
    # timeline (/timeline) and willhaben request recording: both opt-in (Settings → Timeline); entries are kept
    # forever unless pruning is switched on there
    "timeline_enabled": "0",
    "wh_recording": "0",
    "timeline_prune": "0",
    "timeline_days": "30",
    "timeline_max_mb": "500",
    # other platforms (app/platforms): switched on in Settings → Platforms; empty pacing values = the .env defaults
    "platform_kleinanzeigen_enabled": "0",
    "platform_kleinanzeigen_per_hour": "",
    "platform_kleinanzeigen_min_gap_s": "",
    "platform_shpock_enabled": "0",
    "platform_shpock_per_hour": "",
    "platform_shpock_min_gap_s": "",
    "platform_vinted_enabled": "0",
    "platform_vinted_per_hour": "",
    "platform_vinted_min_gap_s": "",
    "platform_preisjaeger_enabled": "0",
    "platform_preisjaeger_per_hour": "",
    "platform_preisjaeger_min_gap_s": "",
    "platform_ebay_enabled": "0",
    "platform_ebay_per_hour": "",
    "platform_ebay_min_gap_s": "",
    "platform_facebook_enabled": "0",
    "platform_facebook_per_hour": "",
    "platform_facebook_min_gap_s": "",
    "platform_willhaben_product_search": "1",
    "platform_kleinanzeigen_product_search": "1",
    "platform_shpock_product_search": "1",
    "platform_vinted_product_search": "1",
    "platform_preisjaeger_product_search": "1",
    "platform_ebay_product_search": "1",
    "platform_facebook_product_search": "1",
    "kleinanzeigen_api_auth": "",
}


def current_ad(db: Session, ad_id: str) -> MyAd | None:
    """The own ad, following renewals: chats keep the old ad id, the settings live on the newest copy."""
    ad, seen = db.get(MyAd, ad_id), {ad_id}
    while ad is not None and ad.replaced_by and ad.replaced_by not in seen:
        newer = db.get(MyAd, ad.replaced_by)
        if newer is None:
            break    # new copy not imported yet: keep the old row
        seen.add(newer.ad_id)
        ad = newer
    return ad


def requests_per_hour() -> int:
    """The default hourly request budget of every platform (Settings → All platforms; before it was willhaben's, the
    others followed it), else REQUESTS_PER_HOUR from .env (0 = none). A platform's own budget wins
    (platform_per_hour)."""
    raw = get_setting("requests_per_hour")
    return int(raw) if raw.isdigit() else get_settings().requests_per_hour


def platform_per_hour(platform: str) -> int:
    """The hourly request budget of a platform, willhaben included: its own (platform_own_per_hour), else the default
    of all platforms (requests_per_hour)."""
    own = platform_own_per_hour(platform)
    return requests_per_hour() if own is None else own


def platform_own_per_hour(platform: str) -> int | None:
    """A site's own hourly budget (Settings → Platforms, else PLATFORM_<ID>_PER_HOUR); None: it follows willhaben's.
    "-": the field was left empty there, so willhaben's, even for a site with a default of its own (Facebook)."""
    raw = get_setting(f"platform_{platform}_per_hour")
    if raw.isdigit():
        return int(raw)
    if raw == "-" or platform == "willhaben":
        return None
    own = getattr(get_settings(), f"platform_{platform}_per_hour", None)
    return None if own is None else int(own)


# the hourly budgets the sites had before they followed willhaben's: the settings form stored them on every Save
_OLD_SITE_BUDGETS = {"kleinanzeigen": "120", "shpock": "90", "vinted": "40", "preisjaeger": "60", "ebay": "40"}


def _follow_willhaben_budget() -> None:
    """Once: a stored site budget that is its old default goes, so the site follows willhaben's like a new one."""
    if get_setting("site_budgets_follow_wh"):
        return
    for pid, old in _OLD_SITE_BUDGETS.items():
        if get_setting(f"platform_{pid}_per_hour") == old:
            set_setting(f"platform_{pid}_per_hour", "")
    set_setting("site_budgets_follow_wh", "1")


def default_min_gap_s() -> float | None:
    """The minimum gap between two requests to one site for every platform without its own (Settings → All
    platforms); None: each site keeps its built-in pace (default_site_gap_s)."""
    try:
        raw = get_setting("platforms_min_gap_s").strip().replace(",", ".")
        return float(raw) if raw else None
    except ValueError:
        return None


def default_site_gap_s(platform: str) -> float:
    """A site's built-in pace (.env / config: slower where a bot manager watches, e.g. Vinted, eBay)."""
    if platform == "willhaben":
        return float(get_settings().request_min_gap_s)
    return float(getattr(get_settings(), f"platform_{platform}_min_gap_s", 3.0))


def platform_min_gap_s(platform: str) -> float:
    """The gap between two requests to a site: its own (Settings → the site), else the default of all platforms,
    else its built-in pace."""
    raw = get_setting(f"platform_{platform}_min_gap_s").strip().replace(",", ".")
    try:
        if raw:
            return float(raw)
    except ValueError:
        pass
    default = default_min_gap_s()
    return default if default is not None else default_site_gap_s(platform)


def get_setting(key: str) -> str:
    with session() as db:
        row = db.get(Setting, key)
        return row.value if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    with session() as db:
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()


def recent_events(limit: int = 30) -> list[EventLog]:
    """The latest notifications that went out with a push (inbox, Home's activity)."""
    with session() as db:
        return list(db.exec(select(EventLog).where(col(EventLog.quiet).is_not(True))
                            .order_by(EventLog.ts.desc()).limit(limit)))


def iter_all(model) -> Iterator[Any]:
    with session() as db:
        yield from db.exec(select(model))
