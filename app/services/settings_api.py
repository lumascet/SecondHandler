"""The settings as data for the AI agent (docs/agent-api.md): a registry of the settings the agent may read and change,
with their type and limits, and the apply logic behind PATCH /api/settings.

The registry is an allowlist: a key that is not in it does not exist for the API (tokens, passwords, secrets, OAuth
data, cookies, the carrier logins, the payment card, internal bookkeeping such as `ui_lang_seen` or `reply_style_meta`
and anything from .env stay out of reach, readable or writable). The web address (`public_url`) stays out too: without
a dashboard password it decides which requests need a login (auth.login_needed) and where the Telegram button opens
the Mini App. Keys are the names the settings are stored under.

The rules are the settings pages': every value is checked against the range the page clamps to (the API refuses what
the page would silently clamp), a bad value changes nothing at all (all values are checked before the first one is
stored), and a change runs the same service function and the same side effect as the page (rescheduling the agents,
the limiters of the sites, …). Values keep the stored format ("1"/"0", "HH:MM", …); the API
speaks real JSON types. Where a page's save function lives inline in app/main.py (`settings_save`, the platform and
shipping routes) the rule is re-implemented here and tests/test_settings_api.py compares both paths.
"""
from __future__ import annotations

import inspect
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.config import get_settings
from app import i18n
from app.db import DEFAULT_SETTINGS, get_setting, set_setting

log = logging.getLogger(__name__)

TYPES = ("bool", "int", "float", "enum", "text", "list")
HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
SERVICE = re.compile(r"-|[a-z0-9_]*")                      # main.ha_push (the service name, "-" = no push, "" = .env decides)
MAX_RULES = 4000                                            # ai_rules.MAX_CHARS, the purchase page's cap


class SettingsError(ValueError):
    """A value the settings page would refuse; the message says why (one per key in the 400 answer)."""


class ValidationErrors(Exception):
    """PATCH refused: {key: message}; nothing was stored."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(errors)
        self.errors = errors


@dataclass(frozen=True)
class Spec:
    key: str                                   # the stored key, also the API's name for it
    section: str                               # settings_catalog section id (the settings page it is on)
    type: str                                  # bool | int | float | enum | text | list
    description: str
    default: Any = None                        # the value while nothing is stored; a callable for a computed one
    min: float | None = None
    max: float | None = None
    choices: tuple[Any, ...] | None = None     # enum: the values; list: the items it may hold
    choices_fn: Callable[[], list[Any]] | None = None
    max_len: int | None = None                 # text: characters
    min_len: int = 0
    pattern: re.Pattern[str] | None = None     # text: must match as a whole
    check: Callable[[Any], None] | None = None # more rules for the normalized value; raises SettingsError
    nullable: bool = False                     # null is a value ("no limit", "as the default of all platforms")
    null_store: str = ""                       # how null is stored
    item_type: str | None = None               # list: "int" for a list of whole numbers
    read: Callable[[], Any] | None = None      # the effective value, as the service reads it
    write: Callable[[Any], None] | None = None # the page's save function, with this one value merged in
    fmt: Callable[[Any], str] | None = None    # generic write: value -> stored text
    effects: tuple[str, ...] = ()              # side effects, see EFFECTS
    affects: tuple[str, ...] = ()              # other keys this one's write can change as well

    def default_value(self) -> Any:
        return self.default() if callable(self.default) else self.default

    def choice_list(self) -> list[Any] | None:
        if self.choices_fn is not None:
            return list(self.choices_fn())
        return list(self.choices) if self.choices is not None else None

    def current(self) -> Any:
        return self.read() if self.read else _generic_read(self)


# ------------------------------------------------------------------ generic read and write
def _flag(key: str, default: bool) -> bool:
    """A switch the way the services read it: on only with "1" when a default row exists or it is off by default,
    on unless "0" for the switches that are on without any row."""
    raw = get_setting(key)
    return raw != "0" if default and key not in DEFAULT_SETTINGS else raw == "1"


def _number(raw: str, kind: str) -> float | int | None:
    raw = raw.strip().replace(",", ".")
    try:
        return int(raw) if kind == "int" and re.fullmatch(r"-?\d+", raw) else (float(raw) if kind == "float" else None)
    except ValueError:
        return None


def _generic_read(s: Spec) -> Any:
    raw = get_setting(s.key)
    if s.type == "bool":
        return _flag(s.key, bool(s.default_value()))
    if s.type in ("int", "float"):
        if s.nullable and raw.strip() in ("", "-"):
            return None
        v = _number(raw, s.type)
        if v is None:
            return s.default_value()
        if s.min is not None:
            v = max(v, s.min)
        if s.max is not None:
            v = min(v, s.max)
        return v
    if s.type == "enum":
        for c in s.choice_list() or []:
            if str(c) == raw.strip():
                return c
        return s.default_value()
    return raw


def _stored(s: Spec, value: Any) -> str:
    if s.fmt:
        return s.fmt(value)
    if value is None:
        return s.null_store
    if s.type == "bool":
        return "1" if value else "0"
    if s.type == "float":
        return f"{value:g}"
    return str(value)


def _write(s: Spec, value: Any) -> None:
    if s.write:
        s.write(value)
    else:
        set_setting(s.key, _stored(s, value))


# ------------------------------------------------------------------ checking a value
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def coerce(s: Spec, raw: Any) -> Any:
    """The value in its normalized form, or a SettingsError (no clamping: the page's limits are the limits)."""
    value = _coerce(s, raw)
    if s.check is not None and value is not None:
        s.check(value)
    return value


def _coerce(s: Spec, raw: Any) -> Any:
    if raw is None:
        if s.nullable:
            return None
        raise SettingsError("null is not allowed here")
    if s.type == "bool":
        if not isinstance(raw, bool):
            raise SettingsError("must be true or false")
        return raw
    if s.type in ("int", "float"):
        if not _is_number(raw) or (isinstance(raw, float) and not math.isfinite(raw)):
            raise SettingsError("must be a whole number" if s.type == "int" else "must be a number")
        if s.type == "int":
            if isinstance(raw, float) and not raw.is_integer():
                raise SettingsError("must be a whole number")
            raw = int(raw)
        else:
            raw = float(raw)
        _in_range(s, raw)
        return raw
    if s.type == "enum":
        choices = s.choice_list() or []
        value = raw
        if choices and all(isinstance(c, int) for c in choices):
            if not _is_number(raw) or int(raw) != raw:
                raise SettingsError("must be one of: " + ", ".join(map(str, choices)))
            value = int(raw)
        elif s.choices_fn is not None and isinstance(raw, str):
            value = _resolve_model(raw)
        if value not in choices or isinstance(value, bool):
            raise SettingsError("must be one of: " + ", ".join(map(str, choices)))
        return value
    if s.type == "text":
        if not isinstance(raw, str):
            raise SettingsError("must be a string")
        text = raw.replace("\r\n", "\n").strip()
        if len(text) < s.min_len:
            raise SettingsError("must not be empty")
        if s.max_len is not None and len(text) > s.max_len:
            raise SettingsError(f"is {len(text)} characters long, at most {s.max_len} are allowed")
        if s.pattern is not None and not s.pattern.fullmatch(text):
            raise SettingsError(_PATTERN_HINTS.get(s.pattern.pattern, "has the wrong format"))
        return text
    if s.type == "list":
        return _coerce_list(s, raw)
    raise SettingsError("unsupported type")


def _in_range(s: Spec, v: float) -> None:
    if s.min is not None and v < s.min or s.max is not None and v > s.max:
        lo = "" if s.min is None else f"{s.min:g}"
        hi = "" if s.max is None else f"{s.max:g}"
        raise SettingsError(f"must be between {lo} and {hi}" if lo and hi else f"must be at least {lo}" if lo else f"must be at most {hi}")


def _coerce_list(s: Spec, raw: Any) -> list[Any]:
    if not isinstance(raw, list):
        raise SettingsError("must be a list")
    if s.item_type == "int":
        if len(raw) != 4 or not all(_is_number(x) and int(x) == x for x in raw):
            raise SettingsError("must be a list of four whole numbers")
        return [int(x) for x in raw]
    if not all(isinstance(x, str) for x in raw):
        raise SettingsError("must be a list of strings")
    items = [x.strip() for x in raw]
    choices = s.choice_list()
    if choices is None:                                    # free items (calendar ids)
        if not 1 <= len(items) <= 20 or any(not x or len(x) > 200 for x in items):
            raise SettingsError("holds 1 to 20 items of 1 to 200 characters")
        return list(dict.fromkeys(items))
    unknown = [x for x in items if x not in choices]
    if unknown:
        raise SettingsError("unknown: " + ", ".join(unknown) + "; allowed: " + ", ".join(choices))
    return [c for c in choices if c in items]              # in the page's order, once each


_PATTERN_HINTS = {HHMM.pattern: "must be a time like 07:30",
                  SERVICE.pattern: 'must be a notify service name (letters, digits, "_") or "-" for no push'}


def _resolve_model(raw: str) -> str:
    """"haiku" / "sonnet" / "opus" (the page accepts them) -> the newest model of that line."""
    from app.llm import models as llm_models

    return llm_models.resolve_alias(raw.strip())


# ------------------------------------------------------------------ the registry
def _mk(type_: str):
    def make(key: str, section: str, description: str, default: Any = None, **kw: Any) -> Spec:
        return Spec(key, section, type_, description, default, **kw)
    return make


B, I, F, E, T, L = _mk("bool"), _mk("int"), _mk("float"), _mk("enum"), _mk("text"), _mk("list")   # the spec makers


def registry() -> dict[str, Spec]:
    """Every exposed setting, by key, in the order of the settings navigation. Built per call: the sites come from the
    platform registry."""
    specs: list[Spec] = []
    add = specs.extend
    add(_platform_specs())
    add(_quiet_specs())
    add(_price_specs())
    add(_purchase_specs())
    add(_selling_specs())
    add(_chat_specs())
    add(_calendar_specs())
    add(_shipping_specs())
    add(_claude_specs())
    add(_rule_specs())
    add(_notification_specs())
    add(_system_specs())
    return {s.key: s for s in specs}


def sections() -> set[str]:
    """The section ids a listing may ask for: the settings pages, the other sites' pages included."""
    from app import platforms
    from app.services import settings_catalog

    return set(settings_catalog.SECTION_IDS) | {p.id for p in platforms.all(enabled_only=False)}


# --- platforms ---------------------------------------------------------------------------------------------------------
def _platform_specs() -> list[Spec]:
    from app import platforms
    from app.db import (default_min_gap_s, platform_own_per_hour, requests_per_hour)

    out = [
        I("requests_per_hour", "platforms", "Hourly request budget every platform uses unless it has its own (0 = none).",
          lambda: get_settings().requests_per_hour, min=0, max=2000, read=requests_per_hour, effects=("limits",)),
        F("platforms_min_gap_s", "platforms", "Minimum gap between two requests to one site, for every platform without its "
          "own (null = each site keeps its built-in pace).", None, min=0, max=3600, nullable=True, read=default_min_gap_s,
          effects=("limits",)),
    ]
    for p in platforms.all(enabled_only=False):
        sec, site = p.id, p.name
        if p.id != "willhaben":
            out.append(B(f"platform_{p.id}_enabled", sec, f"{site} is switched on (searches, chats, product search).", False))
        out += [
            I(f"platform_{p.id}_per_hour", sec, f"{site}: own request budget per hour (0 = none, null = the default of all "
              "platforms).", None, min=0, max=2000, nullable=True, null_store="-",
              read=(lambda pid=p.id: platform_own_per_hour(pid)), effects=("limits",)),
            F(f"platform_{p.id}_min_gap_s", sec, f"{site}: own minimum gap between two requests in seconds (null = the "
              "default of all platforms).", None, min=0, max=3600, nullable=True, effects=("limits",)),
            B(f"platform_{p.id}_product_search", sec, f"Use {site} for the product search.", True),
        ]
    return out


# --- quiet hours and pace ----------------------------------------------------------------------------------------------
def _quiet_write(field: str):
    def write(value: Any) -> None:
        from app.services import quiet_hours

        c = quiet_hours.config()
        vals = {"enabled": c.enabled, "start": c.start.strftime("%H:%M"), "end": c.end.strftime("%H:%M"), "mode": c.mode,
                "interval_min": c.interval_min, field: value}
        quiet_hours.save(vals["enabled"], vals["start"], vals["end"], vals["mode"], vals["interval_min"])
    return write


def _quiet_read(field: str):
    def read() -> Any:
        from app.services import quiet_hours

        c = quiet_hours.config()
        return {"enabled": c.enabled, "start": c.start.strftime("%H:%M"), "end": c.end.strftime("%H:%M"), "mode": c.mode,
                "interval_min": c.interval_min}[field]
    return read


def _pace_write(field: str):
    def write(value: Any) -> None:
        from app.services import adaptive_pace, human_pace

        if field in ("variance_pct", "breaks"):
            c = human_pace.config()
            human_pace.save(value if field == "variance_pct" else c["variance_pct"], value if field == "breaks" else c["breaks"])
        else:
            adaptive_pace.save(value if field == "on" else adaptive_pace.enabled(),
                               value if field == "factor" else adaptive_pace.max_factor())
    return write


def _quiet_specs() -> list[Spec]:
    from app.services import adaptive_pace, human_pace, search_agents

    return [
        B("quiet_enabled", "quiet", "Quiet hours: search agents pause or check much less at night.", True,
          read=_quiet_read("enabled"), write=_quiet_write("enabled")),
        T("quiet_start", "quiet", "Start of the quiet hours, HH:MM (shifts randomly by up to 20 minutes each night).", "01:00",
          pattern=HHMM, read=_quiet_read("start"), write=_quiet_write("start")),
        T("quiet_end", "quiet", "End of the quiet hours, HH:MM.", "07:00", pattern=HHMM, read=_quiet_read("end"),
          write=_quiet_write("end")),
        E("quiet_mode", "quiet", "During quiet hours: slow = check every quiet_interval_min minutes, pause = no checks.",
          "slow", choices=("slow", "pause"), read=_quiet_read("mode"), write=_quiet_write("mode")),
        I("quiet_interval_min", "quiet", "Interval between checks during quiet hours, in minutes.", 60, min=15, max=1440,
          read=_quiet_read("interval_min"), write=_quiet_write("interval_min")),
        I("agent_variance_pct", "quiet", "Human-like timing: random jitter on every agent interval, in % (0 = off).", 30,
          min=0, max=human_pace.MAX_VARIANCE, read=lambda: human_pace.config()["variance_pct"],
          write=_pace_write("variance_pct"), effects=("reschedule",)),
        B("agent_breaks", "quiet", "Take a break now and then: about one scheduled check in twelve is skipped.", True,
          read=lambda: human_pace.config()["breaks"], write=_pace_write("breaks"), effects=("reschedule",)),
        B("adaptive_enabled", "quiet", "Adaptive polling: check less often in the hours that rarely bring new ads.", True,
          read=adaptive_pace.enabled, write=_pace_write("on"), effects=("reschedule",)),
        E("adaptive_max_factor", "quiet", "Slowest pace of adaptive polling: up to this many times the agent's interval.", 4,
          choices=adaptive_pace.MAX_FACTORS, read=adaptive_pace.max_factor, write=_pace_write("factor"),
          effects=("reschedule",)),
        B("watch_saved", "quiet", "Load saved hits' ad pages about once a day for price drops and status changes.", True),
        I("hits_keep_days", "quiet", "Clear hits (all but saved ones) after this many days (0 = never).",
          search_agents.KEEP_DAYS_DEFAULT, min=0, max=3650, read=search_agents.keep_days,
          write=lambda v: search_agents.set_keep_days(v)),
    ]


# --- price rating ------------------------------------------------------------------------------------------------------
def _zone_specs() -> list[Spec]:
    from app.services import price_zones

    def make(group: str, text: str) -> Spec:
        def write(value: Any) -> None:
            price_zones.save({group: value})

        def check(value: Any) -> None:
            try:
                price_zones.parse(value, group)
            except price_zones.ZoneError as e:
                raise SettingsError(str(e)) from e
        return L(f"price_zones_{group}", "prices", f"Price rating, {text}: four ascending limits in % ({price_zones.LOW}-"
                 f"{price_zones.HIGH}) where bargain / cheap / fair / expensive / very expensive start.",
                 list(price_zones.DEFAULTS[group]), item_type="int", read=lambda: list(price_zones.config()[group]),
                 write=write, check=check)
    return [make("used", "used items (share of the new price)"), make("new", "new items (share of the new price)"),
            make("similar", "ads rated by similar ads (share of their median)")]


def _price_specs() -> list[Spec]:
    return _zone_specs() + [
        B("compare_claude", "prices", "Let Claude name the product and pick similar ads for the price check.", True),
    ]


# --- buying ------------------------------------------------------------------------------------------------------------
def _purchase_specs() -> list[Spec]:
    from app.services import auto_buy, paylivery, purchase

    def write_enabled(value: bool) -> None:
        set_setting(purchase.SETTING_ENABLED, "1" if value else "0")
        if not value:
            set_setting(purchase.SETTING_PREFER_PAYLIVERY, "0")      # purchase_routes.settings_purchase: only with orders on

    return [
        B(purchase.SETTING_ENABLED, "purchase", "Allow purchase orders (Buy in the product search and the ad viewer, PayLivery "
          "offers included). Off also switches purchase_prefer_paylivery off.", False, write=write_enabled,
          affects=(purchase.SETTING_PREFER_PAYLIVERY,)),
        B(purchase.SETTING_PREFER_PAYLIVERY, "purchase", "Prefer PayLivery in the buy dialog (needs purchase_enabled).", False),
        F(purchase.SETTING_DISCOUNT, "purchase", "Discount for the suggested wish price, in % below the asking price.",
          purchase.DEFAULT_DISCOUNT_PCT, min=0, max=50,
          read=lambda: min(max(_float_or(get_setting(purchase.SETTING_DISCOUNT), purchase.DEFAULT_DISCOUNT_PCT), 0.0), 50.0)),
        B(auto_buy.SETTING_TRUST, "purchase", "Auto-buy also from young accounts and accounts without a rating (off = it "
          "does not).", True),
        B(paylivery.NOTIFY_SETTING, "paylivery", "Notify me about PayLivery stage changes.", True,
          write=lambda v: paylivery.save_settings(v)),
    ]


def _float_or(raw: str, default: float) -> float:
    v = _number(raw or "", "float")
    return default if v is None else float(v)


# --- selling -----------------------------------------------------------------------------------------------------------
def _selling_specs() -> list[Spec]:
    from app.services import selling
    from app.services.shipping_tariffs import CARRIERS

    return [
        E("sell_handover", "selling", "Usual handover: pickup, shipping or both (preset for new ads, told to buyers).",
          "pickup", choices=selling.HANDOVERS),
        E("sell_shipping_cost", "selling", "Who pays the shipping: the buyer, or it is included in the price.", "buyer",
          choices=selling.COSTS),
        E("sell_delivery", "selling", "Delivery to the buyer: any, a parcel shop / locker, or the front door.", "any",
          choices=selling.DELIVERIES),
        E("sell_carrier", "selling", 'Preferred carrier ("" = the cheapest).', "", choices=("", *CARRIERS)),
        L("sell_payment", "selling", "Payment ways offered to buyers (none = no payment line for Claude).",
          ["transfer", "paylivery", "cash"], choices=selling.PAYMENTS, read=lambda: selling.config()["payment"],
          write=lambda v: set_setting("sell_payment", ",".join(v))),
        T("sell_notes", "selling", "More selling facts for Claude, free text (invoice, weekdays, …).", "",
          max_len=selling.MAX_NOTES, read=lambda: selling.config()["notes"], write=lambda v: ai_rules_save("sell_notes", v)),
    ]


def ai_rules_save(key: str, text: str) -> None:
    from app.services import ai_rules

    ai_rules.save_rule(key, text)


# --- chats -------------------------------------------------------------------------------------------------------------
def _chat_specs() -> list[Spec]:
    from app.services.notify import PUSH_CHANNELS

    sec = "chats"
    return [
        B("auto_draft", sec, "Draft replies to new messages automatically.", False),
        B("notify_messages", sec, "Notify me about new chat messages.", True),
        B("weekly_digest", sec, "Monday 8:00: the week in a few lines.", True),
        B("chat_translate", sec, "Translate foreign chat messages into the app's language.", True),
        B("chat_auto_finish", sec, "Claude marks chats it judges over as finished.", True),
        I("chat_sync_limit", sec, "Sync only the latest N conversations.", 10, min=1, max=50),
        I("chat_sync_days", sec, "Only conversations active in the last N days (0 = today only, null = no limit).", None,
          min=0, max=3650, nullable=True),
        E("push_channel", sec, "Where phone pushes go: auto (Telegram once linked, else Home Assistant), ha, telegram or both.",
          "auto", choices=PUSH_CHANNELS),
        B("reply_humanize", sec, "Human-like automatic answers: read receipt and a random delay.", True),
        I("reply_delay_min_s", sec, "Answer after at least this many seconds.", 40, min=0, max=3600),
        I("reply_delay_max_s", sec, "Answer after at most this many seconds (not below the minimum).", 180, min=0, max=3 * 3600),
        I("reply_fast_min_s", sec, "In ongoing chats (after my first reply): at least this many seconds.", 5, min=0, max=300),
        I("reply_fast_max_s", sec, "In ongoing chats: at most this many seconds (not below the minimum).", 20, min=0, max=600),
        I("expiry_warn_days", "myads", "Warn this many days before your own ads expire (0 = no warning).", 3, min=0, max=365),
        I("myads_expired_days", "myads", "Show expired ads in My ads for this many days (0 = always).", 30, min=0, max=3650),
    ]


# --- calendar ----------------------------------------------------------------------------------------------------------
def _gcal_write(field: str):
    def write(value: Any) -> None:
        from app.services import gcal

        c = {**gcal.config(), field: value}
        gcal.save_config(c["calendars"], c["target"], c["days"], c["window_start"], c["window_end"], c["duration"],
                         c["in_drafts"], c["in_auto"], c["buffer"], c["auto_add"])
    return write


def _calendar_specs() -> list[Spec]:
    from app.services import gcal

    def mk(field: str, key: str, make, desc: str, default: Any, **kw: Any) -> Spec:
        return make(key, "calendar", desc, default, read=(lambda: gcal.config()[field]), write=_gcal_write(field), **kw)

    return [
        mk("calendars", "gcal_calendars", L, "Calendars that count as busy (Google calendar ids).", ["primary"]),
        mk("target", "gcal_target", T, "The calendar new appointments go into.", "primary", min_len=1, max_len=200),
        mk("days", "gcal_days", I, "Look ahead this many days for free times.", 7, min=1, max=21),
        mk("window_start", "gcal_window_start", T, "Handovers possible from, HH:MM.", "09:00", pattern=HHMM),
        mk("window_end", "gcal_window_end", T, "Handovers possible until, HH:MM.", "20:00", pattern=HHMM),
        mk("duration", "gcal_duration", I, "Default length of an appointment, in minutes.", 30, min=5, max=480),
        mk("buffer", "gcal_buffer", I, "Buffer before and after each appointment, in minutes.", 0, min=0, max=gcal.BUFFER_MAX),
        mk("in_drafts", "gcal_in_drafts", B, "Use my free times in reply drafts.", True),
        mk("in_auto", "gcal_in_auto", B, "Use my free times in automatic answers too.", False),
        mk("auto_add", "gcal_auto_add", B, "Add agreed appointments to the calendar by myself.", False),
    ]


# --- shipping ----------------------------------------------------------------------------------------------------------
def _auto_write(field: str):
    def write(value: Any) -> None:
        from app.services import shipping

        shipping.save_auto({**shipping.auto_config(), field: value})
    return write


def _shipping_specs() -> list[Spec]:
    from app.services import geo, shipping
    from app.services.shipping_tariffs import CARRIERS

    carriers = tuple(CARRIERS)

    def sender(field: str, label: str, max_len: int) -> Spec:
        return T(f"ship_sender_{field}", "shipping", f"Sender address: {label}.", "", max_len=max_len,
                 read=lambda: shipping.sender()[field],
                 write=lambda v: shipping.save_sender({**shipping.sender(), field: v}), effects=("home",))

    def auto(field: str, key: str, make, desc: str, default: Any, **kw: Any) -> Spec:
        return make(key, "shipping", desc, default, read=lambda: shipping.auto_config()[field], write=_auto_write(field), **kw)

    return [
        sender("name", "name", 100), sender("street", "street", 100), sender("house_no", "house number", 20),
        sender("extra", "stair, door, extra line", 100), sender("zip", "postcode", 10), sender("city", "city", 100),
        sender("phone", "phone", 40), sender("email", "e-mail", 120),
        I("pickup_max_km", "shipping", "Pickup-only ads farther from home than this many km are not shown (null = no limit).",
          None, min=1, max=2000, nullable=True, read=geo.pickup_radius),
        L("ship_carriers", "shipping", "Carriers offered for parcels (empty = none).", list(carriers), choices=carriers,
          read=lambda: shipping.enabled_carriers(), write=lambda v: shipping.save_carriers(v)),
        B("ship_detect", "shipping", "Notice addresses in chats automatically and offer a label.", True),
        auto("on", "ship_auto", B, "Buy shipping labels automatically.", False),
        auto("max_price", "ship_auto_max_price", F, "Automatic label purchase: highest label price in euro.", 10.0, min=0, max=1000),
        auto("need_sold", "ship_auto_need_sold", B, "Automatic label purchase: only once the ad is sold.", True),
        auto("need_parcel", "ship_auto_need_parcel", B, "Automatic label purchase: only when the parcel size is known.", True),
        auto("carriers", "ship_auto_carriers", L, "Automatic label purchase: carriers allowed (default: the enabled carriers).",
             lambda: shipping.enabled_carriers(), choices=carriers),
        auto("grace_min", "ship_auto_grace_min", I, "Automatic label purchase: wait this many minutes first (0 = at once).",
             10, min=0, max=120),
        auto("daily", "ship_auto_daily", I, "Automatic label purchase: at most this many purchases per day.", 3, min=0, max=50),
    ]


# --- Claude ------------------------------------------------------------------------------------------------------------
def _claude_specs() -> list[Spec]:
    from app.llm import models as llm_models
    from app.llm import usage as llm_usage
    from app.llm.base import PURPOSE_DEFAULTS, PURPOSES

    def model(purpose: str) -> Spec:
        key, label, hint = PURPOSES[purpose]
        return E(key, "claude", f"Claude model for {label.lower()} ({hint}). A line name (haiku, sonnet, opus) is "
                 "accepted and becomes its newest model.", PURPOSE_DEFAULTS.get(purpose, ""),
                 choices_fn=lambda: [m["id"] for m in llm_models.available()],
                 read=lambda: llm_models.resolve_alias(get_setting(key) or PURPOSE_DEFAULTS.get(purpose, "")))

    def limit_read() -> float:
        v = _number(get_setting("claude_token_limit_m"), "float")
        return min(max(float(v or 0), 0.0), 1000.0)

    return [model(p) for p in PURPOSES] + [
        F("claude_token_limit_m", "claude", "Token limit per period in millions of tokens (0 = none).", 10.0, min=0, max=1000,
          read=limit_read, fmt=lambda v: f"{v:g}" if v else ""),
        I(llm_usage.PERIOD_SETTING, "claude", "The token limit's period in days (rolling).", llm_usage.DEFAULT_DAYS,
          min=1, max=llm_usage.MAX_DAYS, read=llm_usage.period_days),
        E("claude_limit_mode", "claude", "When the limit is reached: pause = pause hit screening, haiku = everything on "
          "Haiku, notify = only a push.", "notify", choices=llm_usage.LIMIT_MODES),
    ]


# --- rules and prompts -------------------------------------------------------------------------------------------------
def _rule_specs() -> list[Spec]:
    from app.services import ai_rules, style, system_prompts

    def rule(key: str, desc: str) -> Spec:
        r = next(x for x in ai_rules.RULES if x.key == key)
        return T(key, "rules", desc, r.default() if r.empty_is_default else DEFAULT_SETTINGS.get(key, ""), max_len=r.max_chars,
                 read=lambda: ai_rules.rule_value(r), write=lambda v: ai_rules.save_rule(key, v))

    def prompt(kind: str) -> Spec:
        label = system_prompts.KINDS[kind][0].lower()
        return T(f"prompt_{kind}_system", "rules", f"System prompt for {label}; the built-in default applies when it is "
                 "empty or equals the default.", lambda: system_prompts.default(kind), max_len=system_prompts.MAX_CHARS,
                 read=lambda: system_prompts.get(kind), write=lambda v: system_prompts.save(kind, v))

    return [
        rule("reply_rules", "General rules for every chat answer and new listing (tone, form of address, what always holds)."),
        rule("auto_reply_rules", "Additional rules for automatic answers when selling."),
        rule("purchase_rules", "Rules for purchase orders (the automatic negotiation); empty = the built-in default."),
        T("reply_style", "rules", "My writing style: the profile Claude writes replies in.", "", max_len=style.MAX_PROFILE,
          write=lambda v: set_setting("reply_style", v)),
        B("geizhals_auto_specs", "rules", "Always add the Geizhals spec sheet to new listings when the product is found.", False),
        prompt("auto_reply"), prompt("purchase"), prompt("sim_partner"),
    ]


# --- notifications -----------------------------------------------------------------------------------------------------
def _notification_specs() -> list[Spec]:
    from app.services import agent_events, telegram

    tg_kinds = tuple(telegram.kinds())
    return [
        B("telegram_enabled", "telegram", "Send notifications to Telegram.", True),
        L("telegram_kinds", "telegram", "Which notifications go to Telegram (empty = none).", list(tg_kinds), choices=tg_kinds,
          read=lambda: [k for k in tg_kinds if k in telegram.enabled_kinds()], write=lambda v: telegram.set_kinds(v)),
        T("ha_notify_service", "ha", 'Home Assistant notify service for phone pushes ("-" = no push; empty while none is '
          'chosen, then .env decides).', "", pattern=SERVICE),
        L(agent_events.SETTING_KINDS, "agent", "Event kinds sent to the AI agent's webhook.",
          DEFAULT_SETTINGS["agent_kinds"].split(","), choices=agent_events.ALL_KINDS,
          read=lambda: [k for k in agent_events.ALL_KINDS if k in agent_events.kinds()],
          write=lambda v: set_setting(agent_events.SETTING_KINDS, ",".join(v))),
    ]


# --- system ------------------------------------------------------------------------------------------------------------
def _system_specs() -> list[Spec]:
    from app import platforms
    from app.platforms import vpn
    from app.services import timeline, vpn_check
    from app.services.timeline import SETTING_TIMELINE, SETTING_WH

    def tl_save(field: str):
        def write(value: Any) -> None:
            c = timeline.config()
            c[field] = value
            timeline.save(c["enabled"], c["days"], c["max_mb"])
        return write

    vpn_sites = [(p.id, p.name) for p in platforms.all(enabled_only=False)] + [(vpn.GEIZHALS, "Geizhals")]
    out = [
        B(SETTING_TIMELINE, "timeline", "Record the timeline (Claude calls, actions, runs).", False,
          read=lambda: timeline.config()["record"], write=lambda v: timeline.set_recording(timeline=v)),
        B(SETTING_WH, "timeline", "Store willhaben requests with their answers (for the fixture export).", False,
          read=lambda: timeline.config()["record_wh"], write=lambda v: timeline.set_recording(willhaben=v)),
        B("timeline_prune", "timeline", "Delete old timeline entries automatically.", False,
          read=lambda: timeline.config()["enabled"], write=tl_save("enabled")),
        I("timeline_days", "timeline", "Keep timeline entries this many days (0 = no age limit).", 30, min=0, max=3650,
          read=lambda: timeline.config()["days"], write=tl_save("days")),
        I("timeline_max_mb", "timeline", "Keep the timeline under this size in MB (0 = no size limit).", 500, min=0,
          max=1_000_000, read=lambda: timeline.config()["max_mb"], write=tl_save("max_mb")),
        E("ui_lang", "language", "Language of the dashboard and notifications.", "auto", choices=("auto", *i18n.LANGS)),
        I(vpn_check.INTERVAL_KEY, "vpn", "Hours between two regular searches for a better VPN server.", vpn_check.INTERVAL_H,
          min=1, max=168, read=vpn_check.interval_h, write=lambda v: vpn_check.set_interval(v)),
        I(vpn.PARALLEL_KEY, "vpn", "VPN servers one site's parallel work (the product search) may use at once.", 1,
          min=1, max=vpn.MAX_PARALLEL, read=vpn.parallel, write=lambda v: vpn.set_parallel(v), effects=("vpn_parallel",)),
    ]
    for pid, name in vpn_sites:
        key = "geizhals_vpn" if pid == vpn.GEIZHALS else f"platform_{pid}_vpn"
        out.append(B(key, "vpn", f"{name}: anonymous requests through the VPN.", False, read=(lambda pid=pid: vpn.enabled(pid)),
                     write=(lambda v, pid=pid: vpn.set_enabled(pid, v)), effects=("vpn_site",)))
    return out


# ------------------------------------------------------------------ side effects
# Each takes {key: new value} of the changed settings that name it and may be async; the tests replace entries.
_background: set[Any] = set()


def _spawn(coro) -> None:
    import asyncio

    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _fx_limits(changed: dict[str, Any]) -> None:
    """main._apply_limits: every site's limiter takes its budget and gap now."""
    from app import main

    main._apply_limits()


def _fx_reschedule(changed: dict[str, Any]) -> None:
    """The pace settings: every search agent is scheduled again with the new timing."""
    from app import scheduler

    scheduler.reschedule_agents()


def _fx_home(changed: dict[str, Any]) -> None:
    """main.settings_shipping: a new sender address is located in the background (distance sort of the product search)."""
    from app.services import geo

    if geo.home_address() and geo.home_cached() is None:
        _spawn(geo.home())


async def _fx_vpn_site(changed: dict[str, Any]) -> None:
    """main.settings_vpn_site: a search asks the site through the server in use first / a server nobody needs stops."""
    from app.platforms import vpn
    from app.services import vpn_check

    for key, on in changed.items():
        pid = vpn.GEIZHALS if key == "geizhals_vpn" else key[len("platform_"):-len("_vpn")]
        if on:
            vpn_check.site_on(pid)
        else:
            await vpn.site_off(pid)


async def _fx_vpn_parallel(changed: dict[str, Any]) -> None:
    """main.settings_vpn_parallel: off (1 server): the helper servers stop now."""
    from app.platforms import vpn

    if vpn.parallel() == 1:
        await vpn.conn.prune_helpers(idle_s=0)


EFFECTS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "limits": _fx_limits, "reschedule": _fx_reschedule, "home": _fx_home,
    "vpn_site": _fx_vpn_site, "vpn_parallel": _fx_vpn_parallel,
}


# ------------------------------------------------------------------ reading
def item(s: Spec) -> dict[str, Any]:
    """One setting as the API shows it."""
    out: dict[str, Any] = {"key": s.key, "section": s.section, "type": s.type, "value": s.current(),
                           "default": s.default_value(), "description": s.description}
    choices = s.choice_list()
    if choices is not None:
        out["choices"] = choices
    if s.min is not None:
        out["min"] = s.min
    if s.max is not None:
        out["max"] = s.max
    if s.type == "text" and s.max_len is not None:
        out["max_length"] = s.max_len
    if s.nullable:
        out["nullable"] = True
    return out


def listing(section: str | None = None) -> list[dict[str, Any]]:
    return [item(s) for s in registry().values() if section in (None, s.section)]


def get(key: str) -> dict[str, Any] | None:
    s = registry().get(key)
    return item(s) if s else None


# ------------------------------------------------------------------ changing
def _cross_checks(reg: dict[str, Spec], clean: dict[str, Any]) -> dict[str, str]:
    """Rules between settings, checked on the values as they will be (the new ones over the stored ones)."""
    def final(key: str) -> Any:
        return clean[key] if key in clean else reg[key].current()

    errors: dict[str, str] = {}
    for lo, hi in (("reply_delay_min_s", "reply_delay_max_s"), ("reply_fast_min_s", "reply_fast_max_s")):
        if (lo in clean or hi in clean) and final(lo) > final(hi):
            errors[hi if hi in clean else lo] = f"{hi} must not be below {lo}"
    if "purchase_prefer_paylivery" in clean and clean["purchase_prefer_paylivery"] and not final("purchase_enabled"):
        errors["purchase_prefer_paylivery"] = "needs purchase_enabled (PayLivery is preselected only with purchase orders on)"
    return errors


async def patch(values: dict[str, Any]) -> dict[str, Any]:
    """Check every value, then store the ones that change and run their side effects. Raises ValidationErrors (nothing
    stored) with a message per key. Returns {"changed": {key: new value}, "settings": [items], "warnings"?: [...]}."""
    reg = registry()
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        s = reg.get(key)
        if s is None:
            errors[key] = "unknown setting"
            continue
        try:
            clean[key] = coerce(s, raw)
        except SettingsError as e:
            errors[key] = str(e)
    if not errors:
        errors.update(_cross_checks(reg, clean))
    if errors:
        raise ValidationErrors(errors)

    touched = list(dict.fromkeys([*clean, *(a for k in clean for a in reg[k].affects)]))
    before = {k: reg[k].current() for k in touched}
    for key, value in clean.items():
        if value != before[key]:
            _write(reg[key], value)
    after = {k: reg[k].current() for k in touched}
    changed = {k: after[k] for k in touched if after[k] != before[k]}

    warnings: list[str] = []
    order = list(dict.fromkeys(e for k in changed for e in reg[k].effects))
    for name in order:
        keys = {k: v for k, v in changed.items() if name in reg[k].effects}
        try:
            result = EFFECTS[name](keys)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001 - the values are stored; say what did not run
            log.warning("settings API: side effect %s failed: %s", name, e)
            warnings.append(f"{name}: {e}")
    out: dict[str, Any] = {"changed": changed, "settings": [item(reg[k]) for k in changed]}
    if warnings:
        out["warnings"] = warnings
    return out
