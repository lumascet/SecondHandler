"""The settings through the AI agent's API (app/settings_api_routes.py, app/services/settings_api.py): the registry with
its allowlist, typed values with the settings pages' limits, all-or-nothing PATCH, side effects, parity with the pages'
own save routes, the bearer token and the timeline entry."""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import platforms
from app.config import get_settings
from app.db import Setting, get_setting, session, set_setting
from app.main import app
from app.services import agent_events, settings_api as sa, settings_catalog, timeline

TOKEN = "t0ken-for-the-agent"
BEARER = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locked(monkeypatch, tmp_path):
    """The dashboard has a password and the agent an API token."""
    monkeypatch.setattr(get_settings(), "dashboard_password", "geheim")
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    set_setting(agent_events.SETTING_TOKEN, TOKEN)


@pytest.fixture
def effects(monkeypatch):
    """Every side effect replaced by a recorder: {name: [changed keys of each call]}."""
    calls: dict[str, list[dict]] = {}

    def make(name):
        def fx(changed):
            calls.setdefault(name, []).append(dict(changed))
        return fx
    for name in sa.EFFECTS:
        monkeypatch.setitem(sa.EFFECTS, name, make(name))
    return calls


def patch(client, values, **kw):
    return client.patch("/api/settings", json={"values": values}, **kw)


def stored() -> dict[str, str]:
    with session() as db:
        return {s.key: s.value for s in db.exec(select(Setting))}


def wipe() -> None:
    with session() as db:
        for row in db.exec(select(Setting)):
            db.delete(row)
        db.commit()


# ------------------------------------------------------------------ the registry
def test_registry_is_consistent():
    reg = sa.registry()
    valid = sa.sections()
    assert len(reg) > 100
    for key, s in reg.items():
        assert key == s.key and s.section in valid and s.type in sa.TYPES and s.description.endswith((".", ")", "?")), key
        item = sa.item(s)
        v, d = item["value"], item["default"]
        for x in (v, d):
            if x is None:
                assert s.nullable or s.type == "text", key
            elif s.type == "bool":
                assert isinstance(x, bool), key
            elif s.type == "int":
                assert isinstance(x, int) and not isinstance(x, bool), key
            elif s.type == "float":
                assert isinstance(x, (int, float)), key
            elif s.type == "list":
                assert isinstance(x, list), key
            elif s.type in ("text", "enum"):
                assert isinstance(x, (str, int)), key
        if s.type == "enum":
            assert v in item["choices"] and d in item["choices"], key
        if s.type in ("int", "float") and v is not None:
            assert (s.min is None or v >= s.min) and (s.max is None or v <= s.max), key


def test_nothing_secret_is_exposed():
    secret = re.compile(r"token|secret|password|passwd|oauth|cookie|auth|login|card|session|seen|meta|credential|webhook", re.I)
    for key in sa.registry():
        # model/limit keys mention "token" as the unit
        if key == "claude_token_limit_m":
            continue
        assert not secret.search(key), key
    for key in ("agent_token", "agent_webhook_secret", "agent_webhook_url", "kleinanzeigen_api_auth", "ui_lang_seen",
                "reply_style_meta", "send_verified", "ha_url", "telegram_chat_id", "ship_sender_geo", "printers",
                "ship_logins", "account_vinted", "gcal_client", "inbox_seen_at", "llm_model", "public_url"):
        assert key not in sa.registry(), key


def test_sections_are_the_settings_pages():
    assert {s.section for s in sa.registry().values()} <= sa.sections()
    assert {"platforms", "willhaben", "quiet", "purchase", "selling", "chats", "myads", "calendar", "shipping", "claude",
            "rules", "telegram", "agent", "timeline", "language", "vpn", "kleinanzeigen", "vinted"} <= \
        {s.section for s in sa.registry().values()}
    assert set(settings_catalog.SECTION_IDS) <= sa.sections()


def test_every_site_gets_its_settings():
    reg = sa.registry()
    for p in platforms.all(enabled_only=False):
        for suffix in ("per_hour", "min_gap_s", "product_search", "vpn"):
            assert reg[f"platform_{p.id}_{suffix}"].section in (p.id, "vpn"), p.id
        assert (f"platform_{p.id}_enabled" in reg) == (p.id != "willhaben")


def test_every_setting_takes_its_own_value_and_its_default(client, effects):
    """What the API shows is what it accepts: its own value changes nothing, the default is a valid value."""
    for item in client.get("/api/settings").json()["settings"]:
        key = item["key"]
        r = patch(client, {key: item["value"]})
        assert r.status_code == 200 and r.json()["changed"] == {}, (key, r.text)
        r = patch(client, {key: item["default"]})
        assert r.status_code == 200, (key, r.text)
        assert client.get(f"/api/settings/{key}").json()["value"] == item["default"] or key in ("purchase_rules",), key


# ------------------------------------------------------------------ reading
def test_list_filter_and_single_key(client):
    everything = client.get("/api/settings").json()["settings"]
    quiet = client.get("/api/settings", params={"section": "quiet"}).json()["settings"]
    assert quiet and {i["section"] for i in quiet} == {"quiet"} and len(quiet) < len(everything)
    item = next(i for i in quiet if i["key"] == "quiet_start")
    assert (item["type"], item["value"], item["default"]) == ("text", "01:00", "01:00") and item["description"]
    one = client.get("/api/settings/quiet_interval_min").json()
    assert one["value"] == 60 and one["default"] == 60 and one["min"] == 15 and one["max"] == 1440
    assert client.get("/api/settings/quiet_mode").json()["choices"] == ["slow", "pause"]
    assert client.get("/api/settings", params={"section": "backup"}).json() == {"settings": []}    # a page without settings
    assert client.get("/api/settings", params={"section": "nope"}).status_code == 404


@pytest.mark.parametrize("key", ["agent_token", "agent_webhook_secret", "kleinanzeigen_api_auth", "ui_lang_seen",
                                 "reply_style_meta", "nope"])
def test_hidden_keys_are_unknown(client, key):
    set_setting(key, "value-that-must-not-leak")
    assert client.get(f"/api/settings/{key}").status_code == 404
    assert key not in {i["key"] for i in client.get("/api/settings").json()["settings"]}
    assert "value-that-must-not-leak" not in client.get("/api/settings").text
    r = patch(client, {key: "x"})
    assert r.status_code == 400 and r.json() == {"errors": {key: "unknown setting"}}
    assert get_setting(key) == "value-that-must-not-leak"


def test_values_follow_the_services_defaults(client):
    def value(key):
        return client.get(f"/api/settings/{key}").json()["value"]
    assert value("telegram_enabled") is True and value("paylivery_notify") is True       # on without any row
    assert value("agent_breaks") is True and value("ship_auto_need_sold") is True
    assert value("purchase_enabled") is False and value("platform_vinted_enabled") is False
    assert value("chat_sync_days") is None and value("platform_vinted_per_hour") is None
    assert value("telegram_kinds") == list(sa.registry()["telegram_kinds"].choices)
    assert value("agent_kinds") == ["purchase", "sale", "paylivery", "shipping", "message"]
    assert value("price_zones_used") == [35, 55, 75, 90]
    assert value("purchase_rules").startswith("-") or value("purchase_rules")               # the default text applies
    set_setting("telegram_enabled", "0")
    assert value("telegram_enabled") is False


# ------------------------------------------------------------------ changing: validation
def test_patch_changes_and_reports_only_what_changed(client):
    r = patch(client, {"quiet_interval_min": 90, "quiet_mode": "slow", "auto_draft": True})
    body = r.json()
    assert r.status_code == 200 and body["changed"] == {"quiet_interval_min": 90, "auto_draft": True}      # slow was already so
    assert {i["key"]: i["value"] for i in body["settings"]} == body["changed"]
    assert get_setting("quiet_interval_min") == "90" and get_setting("auto_draft") == "1"
    assert patch(client, {"quiet_interval_min": 90}).json() == {"changed": {}, "settings": []}
    assert client.get("/api/settings/auto_draft").json()["value"] is True


@pytest.mark.parametrize("key,value,message", [
    ("auto_draft", "yes", "true or false"), ("auto_draft", 1, "true or false"), ("auto_draft", None, "null"),
    ("chat_sync_limit", True, "whole number"), ("chat_sync_limit", 5.5, "whole number"), ("chat_sync_limit", "5", "whole number"),
    ("chat_sync_limit", 0, "between 1 and 50"), ("chat_sync_limit", 51, "between 1 and 50"),
    ("quiet_interval_min", 14, "between 15 and 1440"), ("purchase_discount_pct", 50.5, "between 0 and 50"),
    ("purchase_discount_pct", float("nan"), "number"), ("purchase_discount_pct", float("inf"), "number"),
    ("quiet_mode", "sleep", "one of: slow, pause"), ("push_channel", "sms", "one of"), ("adaptive_max_factor", 5, "one of: 2, 3, 4, 6, 8"),
    ("adaptive_max_factor", "4", "one of"), ("quiet_start", "7:00", "time like"), ("quiet_end", "24:00", "time like"),
    ("gcal_window_start", "9am", "time like"),
    ("ha_notify_service", "mobile app", "notify service"), ("ui_lang", "fr", "one of"), ("llm_model_chat", "gpt-4", "one of"),
    ("sell_handover", "post", "one of"), ("sell_payment", ["bitcoin"], "unknown"), ("sell_payment", "cash", "list"),
    ("telegram_kinds", ["nope"], "unknown"), ("reply_rules", 5, "string"), ("reply_rules", "x" * 4001, "at most 4000"),
    ("sell_notes", "x" * 2001, "at most 2000"), ("ship_sender_zip", "1" * 11, "at most 10"),
    ("price_zones_used", [30, 20, 70, 90], "higher than the one before"), ("price_zones_used", [30, 50, 70], "four"),
    ("price_zones_used", [0, 50, 70, 90], "range|between|number"), ("price_zones_used", "35,55,75,90", "list"),
    ("gcal_calendars", [], "1 to 20"), ("gcal_target", "", "empty"), ("pickup_max_km", 0, "between 1 and 2000"),
    ("platform_vinted_per_hour", -1, "between 0 and 2000"), ("platform_vinted_min_gap_s", "3", "number"),
    ("claude_limit_mode", "stop", "one of"), ("timeline_days", -1, "between 0 and 3650"), ("vpn_parallel", 9, "between 1 and 8"),
    ("expiry_warn_days", -1, "at least 0|between 0"), ("ship_auto_daily", 51, "between 0 and 50"),
])
def test_bad_values_are_refused(client, key, value, message):
    set_setting("quiet_interval_min", "75")
    before = stored()
    r = client.patch("/api/settings", content=_json({"values": {"quiet_interval_min": 80, key: value}}),
                     headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    errors = r.json()["errors"]
    assert set(errors) == {key} and re.search(message, errors[key]), errors
    assert stored() == before                                             # the valid key of the same request is not stored


def _json(obj) -> str:
    import json
    return json.dumps(obj)               # allows NaN / Infinity, which json= of the test client refuses


def test_all_errors_come_back_together_and_the_body_is_checked(client):
    r = patch(client, {"auto_draft": "x", "nope": 1, "chat_sync_limit": 99, "notify_messages": False})
    assert r.status_code == 400 and set(r.json()["errors"]) == {"auto_draft", "nope", "chat_sync_limit"}
    assert get_setting("notify_messages") == "1"
    for body in ({}, {"values": []}, {"values": "x"}, []):
        assert client.patch("/api/settings", json=body).status_code == 400
    assert client.patch("/api/settings", content="not json", headers={"content-type": "application/json"}).status_code == 400
    assert patch(client, {}).json() == {"changed": {}, "settings": []}


def test_values_between_settings_are_checked_as_they_will_be(client):
    assert patch(client, {"reply_delay_min_s": 200}).json()["errors"] == {"reply_delay_min_s": "reply_delay_max_s must not be below reply_delay_min_s"}
    assert patch(client, {"reply_delay_max_s": 10}).json()["errors"].keys() == {"reply_delay_max_s"}      # below the stored 40
    assert patch(client, {"reply_delay_min_s": 200, "reply_delay_max_s": 400}).status_code == 200
    assert patch(client, {"reply_fast_min_s": 30}).status_code == 400                                   # above the stored 20
    assert patch(client, {"reply_fast_min_s": 30, "reply_fast_max_s": 60}).json()["changed"] == {"reply_fast_min_s": 30, "reply_fast_max_s": 60}
    assert get_setting("reply_delay_min_s") == "200" and get_setting("reply_fast_max_s") == "60"


def test_payliveryis_preferred_only_with_purchase_orders_on(client):
    r = patch(client, {"purchase_prefer_paylivery": True})
    assert r.status_code == 400 and "purchase_enabled" in r.json()["errors"]["purchase_prefer_paylivery"]
    assert patch(client, {"purchase_prefer_paylivery": True, "purchase_enabled": True}).json()["changed"] == \
        {"purchase_enabled": True, "purchase_prefer_paylivery": True}
    # switching the orders off switches the preference off as the page does, and says so
    assert patch(client, {"purchase_enabled": False}).json()["changed"] == {"purchase_enabled": False, "purchase_prefer_paylivery": False}
    assert get_setting("purchase_prefer_paylivery") == "0"


def test_text_rules_and_lists_are_normalized(client):
    r = patch(client, {"reply_rules": "  - Du-Form\r\n- kurz  ", "sell_payment": ["cash", "transfer", "cash"],
                       "telegram_kinds": ["hit", "message"], "ship_sender_name": "  Max  "})
    assert r.json()["changed"] == {"reply_rules": "- Du-Form\n- kurz", "sell_payment": ["transfer", "cash"],
                                   "telegram_kinds": ["message", "hit"], "ship_sender_name": "Max"}
    assert get_setting("sell_payment") == "transfer,cash" and get_setting("telegram_kinds") == "message,hit"
    assert patch(client, {"telegram_kinds": []}).json()["changed"] == {"telegram_kinds": []}
    assert get_setting("telegram_kinds") == "-"                           # none chosen, not "all"
    assert client.get("/api/settings/telegram_kinds").json()["value"] == []
    # a purchase rule equal to the default is stored empty, the default applies
    default = client.get("/api/settings/purchase_rules").json()["default"]
    patch(client, {"purchase_rules": "Nur Abholung."})
    assert get_setting("purchase_rules") == "Nur Abholung."
    patch(client, {"purchase_rules": default})
    assert get_setting("purchase_rules") == "" and client.get("/api/settings/purchase_rules").json()["value"] == default


def test_models_accept_a_line_name(client):
    r = patch(client, {"llm_model_chat": "haiku"})
    assert r.status_code == 200 and r.json()["changed"]["llm_model_chat"].startswith("claude-haiku")
    assert get_setting("llm_model_chat") == r.json()["changed"]["llm_model_chat"]
    assert client.get("/api/settings/llm_model_ads").json()["value"] == "claude-opus-5-5"


# ------------------------------------------------------------------ side effects
def test_side_effects_run_once_per_kind(client, effects):
    patch(client, {"requests_per_hour": 300, "platform_vinted_per_hour": 20, "platform_vinted_min_gap_s": 4.5,
                   "agent_breaks": False, "adaptive_enabled": False,
                   "platform_vinted_vpn": True, "vpn_parallel": 3, "ship_sender_city": "Graz", "auto_draft": True})
    assert effects["limits"] == [{"requests_per_hour": 300, "platform_vinted_per_hour": 20, "platform_vinted_min_gap_s": 4.5}]
    assert effects["reschedule"] == [{"agent_breaks": False, "adaptive_enabled": False}]
    assert effects["vpn_site"] == [{"platform_vinted_vpn": True}] and effects["vpn_parallel"] == [{"vpn_parallel": 3}]
    assert effects["home"] == [{"ship_sender_city": "Graz"}]
    patch(client, {"auto_draft": False, "quiet_mode": "pause"})               # no effect of their own
    patch(client, {"quiet_mode": "pause"})                                    # unchanged: nothing runs
    assert {k: len(v) for k, v in effects.items()} == {"limits": 1, "reschedule": 1, "vpn_site": 1,
                                                       "vpn_parallel": 1, "home": 1}


def test_a_failing_side_effect_is_reported_not_hidden(client, monkeypatch):
    def broken(changed):
        raise RuntimeError("no scheduler")
    monkeypatch.setitem(sa.EFFECTS, "reschedule", broken)
    r = patch(client, {"agent_breaks": False})
    assert r.status_code == 200 and r.json()["changed"] == {"agent_breaks": False}
    assert r.json()["warnings"] == ["reschedule: no scheduler"] and get_setting("agent_breaks") == "0"


def test_limits_reach_the_limiters(client):
    patch(client, {"platform_vinted_per_hour": 11, "platform_vinted_min_gap_s": 7.5, "requests_per_hour": 400})
    lim = platforms.get("vinted").limiter
    assert (lim.per_hour, lim.min_gap_s) == (11, 7.5)
    patch(client, {"platform_vinted_per_hour": None})                         # follows the default of all platforms again
    assert get_setting("platform_vinted_per_hour") == "-" and platforms.get("vinted").limiter.per_hour == 400


def test_pace_and_flags_apply_at_once(client, monkeypatch):
    from app import scheduler
    from app.services import adaptive_pace, system_prompts, topic

    rescheduled = []
    monkeypatch.setattr(scheduler, "reschedule_agents", lambda: rescheduled.append(1))
    patch(client, {"adaptive_max_factor": 8, "agent_variance_pct": 60})
    assert adaptive_pace.max_factor() == 8 and rescheduled == [1]
    patch(client, {"timeline_enabled": True})
    assert timeline.enabled() is True                                         # the flag cache is cleared
    resets = []
    monkeypatch.setattr(topic, "reset_cache", lambda: resets.append(1))
    patch(client, {"prompt_auto_reply_system": "Antworte kurz."})
    assert system_prompts.custom("auto_reply") and resets == [1]
    patch(client, {"prompt_auto_reply_system": ""})
    assert not system_prompts.custom("auto_reply")


def test_the_web_address_cannot_be_changed_to_open_the_dashboard(monkeypatch):
    """Without a dashboard password the web address decides who needs a login: the token must not be able to move it
    away and so open the pages over the public host (nor redirect the Telegram button)."""
    from app.db import SETTING_PUBLIC_URL
    monkeypatch.setattr(get_settings(), "dashboard_password", "")
    monkeypatch.setattr(get_settings(), "public_url", get_settings().public_url)       # restored after the test
    set_setting(SETTING_PUBLIC_URL, "https://sh.example.org")         # the app's start applies it (apply_public_url)
    set_setting(agent_events.SETTING_TOKEN, "t0ken-for-the-agent")
    public = {"host": "sh.example.org", "authorization": "Bearer t0ken-for-the-agent"}
    with TestClient(app) as c:
        r = c.patch("/api/settings", json={"values": {"public_url": ""}}, headers=public)
        assert r.status_code == 400 and "public_url" in r.json()["errors"]
        assert c.get("/api/settings/public_url", headers=public).status_code == 404
        assert get_settings().public_url == "https://sh.example.org"
        assert c.get("/settings", headers={"host": "sh.example.org", "accept": "application/json"}).status_code == 401
    set_setting(agent_events.SETTING_TOKEN, "")
    set_setting(SETTING_PUBLIC_URL, "")


def test_vpn_switches_and_parallel_servers(client, monkeypatch):
    from app.platforms import vpn
    from app.services import vpn_check

    seen = []
    monkeypatch.setattr(vpn_check, "site_on", lambda pid: seen.append(("on", pid)))

    async def off(pid):
        seen.append(("off", pid))

    async def prune(idle_s=0):
        seen.append(("prune", idle_s))
    monkeypatch.setattr(vpn, "site_off", off)
    monkeypatch.setattr(vpn.conn, "prune_helpers", prune)
    patch(client, {"platform_vinted_vpn": True, "geizhals_vpn": True, "vpn_parallel": 3})
    assert sorted(seen) == [("on", "geizhals"), ("on", "vinted")] and vpn.enabled("vinted") and vpn.parallel() == 3
    seen.clear()
    patch(client, {"platform_vinted_vpn": False, "vpn_parallel": 1})
    assert seen == [("off", "vinted"), ("prune", 0)] and not vpn.enabled("vinted")


def test_new_sender_address_is_located_in_the_background(monkeypatch):
    from app.services import geo

    located = []

    async def home():
        located.append(1)
    monkeypatch.setattr(geo, "home_address", lambda: "Hauptplatz 1, 8010 Graz")
    monkeypatch.setattr(geo, "home_cached", lambda: None)
    monkeypatch.setattr(geo, "home", home)

    async def run():
        sa._fx_home({})
        await asyncio.gather(*sa._background)
    asyncio.run(run())
    assert located == [1]


# ------------------------------------------------------------------ parity with the pages' own save routes
def _same(client, page_post, form, values, keys=None, monkeypatch=None):
    """POST the page's form, snapshot what it stored; start over, PATCH the same values, compare the stored text."""
    wipe()
    r = client.post(page_post, data=form, follow_redirects=False)
    assert r.status_code in (200, 303), (page_post, r.status_code)
    via_page = stored()
    wipe()
    r = patch(client, values)
    assert r.status_code == 200, r.text
    via_api = stored()
    for key in keys or values:
        assert key in via_page, f"{key}: the page stored nothing"
        assert via_api.get(key) == via_page[key], f"{key}: page {via_page[key]!r}, API {via_api.get(key)!r}"
    wipe()


def test_parity_chats_and_my_ads(client):
    _same(client, "/settings", {"section": "chats", "auto_draft": "on", "chat_sync_limit": "25", "chat_sync_days": "14",
                                "push_channel": "both", "reply_delay_min_s": "10", "reply_delay_max_s": "90",
                                "reply_fast_min_s": "2", "reply_fast_max_s": "9"},
          {"auto_draft": True, "notify_messages": False, "weekly_digest": False, "chat_translate": False,
           "chat_auto_finish": False, "chat_sync_limit": 25, "chat_sync_days": 14, "push_channel": "both",
           "reply_humanize": False, "reply_delay_min_s": 10, "reply_delay_max_s": 90, "reply_fast_min_s": 2,
           "reply_fast_max_s": 9})


def test_parity_selling_and_rules(client):
    _same(client, "/settings", {"section": "selling", "sell_handover": "both", "sell_shipping_cost": "included",
                                "sell_delivery": "shop", "sell_carrier": "gls", "sell_payment": ["transfer", "cash"],
                                "sell_notes": "Rechnung gerne."},
          {"sell_handover": "both", "sell_shipping_cost": "included", "sell_delivery": "shop", "sell_carrier": "gls",
           "sell_payment": ["transfer", "cash"], "sell_notes": "Rechnung gerne."})
    _same(client, "/settings/ai/rule/reply_rules", {"text": "- Sie-Form"}, {"reply_rules": "- Sie-Form"})
    _same(client, "/settings/ai/rule/auto_reply_rules", {"text": "Nur Abholung."}, {"auto_reply_rules": "Nur Abholung."})
    _same(client, "/settings/ai/rule/purchase_rules", {"text": "Nur Privatverkäufer."}, {"purchase_rules": "Nur Privatverkäufer."})
    _same(client, "/settings/ai/prompt/purchase", {"text": "Du kaufst."}, {"prompt_purchase_system": "Du kaufst."})
    _same(client, "/settings/listings", {"geizhals_auto_specs": "on"}, {"geizhals_auto_specs": True})


def test_parity_quiet_hours_and_pace(client, monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "reschedule_agents", lambda: None)
    _same(client, "/settings/quiet", {"start": "02:30", "end": "06:15", "mode": "pause", "interval_min": "90"},
          {"quiet_enabled": False, "quiet_start": "02:30", "quiet_end": "06:15", "quiet_mode": "pause", "quiet_interval_min": 90})
    _same(client, "/settings/human-pace", {"variance_pct": "50"}, {"agent_variance_pct": 50, "agent_breaks": False})
    _same(client, "/settings/quiet", {"pace": "1", "jitter": "on", "breaks": "on", "adaptive_max": "6"},
          {"adaptive_enabled": False, "adaptive_max_factor": 6})


def test_parity_purchase_and_paylivery(client):
    _same(client, "/settings/purchase", {"enabled": "on", "discount_pct": "15", "prefer_paylivery": "on", "trust_seen": "1"},
          {"purchase_enabled": True, "purchase_discount_pct": 15, "purchase_prefer_paylivery": True, "auto_buy_trust": False})
    _same(client, "/settings/purchase", {"discount_pct": "12.5"}, {"purchase_discount_pct": 12.5})


def test_parity_platforms(client):
    _same(client, "/settings/platform/ebay", {"enabled": "on", "product_search": "on", "gap": "3", "budget": "9"},
          {"platform_ebay_enabled": True, "platform_ebay_min_gap_s": 3.0, "platform_ebay_per_hour": 9})
    client.post("/settings/platform/ebay", data={"gap": "", "budget": ""})                   # empty = the default: "" / "-"
    assert (get_setting("platform_ebay_min_gap_s"), get_setting("platform_ebay_per_hour")) == ("", "-")
    patch(client, {"platform_ebay_min_gap_s": 3.0, "platform_ebay_per_hour": 9})
    patch(client, {"platform_ebay_min_gap_s": None, "platform_ebay_per_hour": None})
    assert (get_setting("platform_ebay_min_gap_s"), get_setting("platform_ebay_per_hour")) == ("", "-")
    _same(client, "/settings/willhaben", {"budget": "50", "gap": "2.5", "has_product_search": "1"},
          {"platform_willhaben_per_hour": 50, "platform_willhaben_min_gap_s": 2.5, "platform_willhaben_product_search": False})
    _same(client, "/settings/platforms", {"requests_per_hour": "300", "gap": "1.5"},
          {"requests_per_hour": 300, "platforms_min_gap_s": 1.5})


def test_parity_shipping(client, monkeypatch):
    from app.services import geo

    async def home():
        return None
    monkeypatch.setattr(geo, "home", home)
    form = {"name": "Max Muster", "street": "Hauptplatz", "house_no": "1", "extra": "", "zip": "8010", "city": "Graz",
            "phone": "0664 1", "email": "max@example.org", "carrier": ["post", "gls"], "pickup_max_km": "30"}
    values = {"ship_sender_name": "Max Muster", "ship_sender_street": "Hauptplatz", "ship_sender_house_no": "1",
              "ship_sender_zip": "8010", "ship_sender_city": "Graz", "ship_sender_phone": "0664 1",
              "ship_sender_email": "max@example.org", "ship_carriers": ["post", "gls"], "ship_detect": False,
              "pickup_max_km": 30}
    wipe()
    client.post("/settings/shipping", data=form)
    page = stored()
    wipe()
    assert patch(client, values).status_code == 200
    api = stored()
    for key in ("ship_sender", "ship_carriers", "ship_detect", "pickup_max_km"):
        assert api[key] == page[key], key
    _same(client, "/settings/shipping/auto", {"on": "1", "max_price": "12.5", "need_parcel": "1", "auto_carrier": ["gls"],
                                              "grace_min": "20", "daily": "5"},
          {"ship_auto": True, "ship_auto_max_price": 12.5, "ship_auto_need_sold": False, "ship_auto_need_parcel": True,
           "ship_auto_carriers": ["gls"], "ship_auto_grace_min": 20, "ship_auto_daily": 5})


def test_parity_calendar_and_notifications(client):
    _same(client, "/settings/gcal", {"calendar": ["a@example.org", "b"], "target": "a@example.org", "days": "10",
                                     "window_start": "08:00", "window_end": "18:00", "duration": "45", "in_auto": "1",
                                     "buffer": "15", "auto_add": "1"},
          {"gcal_calendars": ["a@example.org", "b"], "gcal_target": "a@example.org", "gcal_days": 10,
           "gcal_window_start": "08:00", "gcal_window_end": "18:00", "gcal_duration": 45, "gcal_in_drafts": False,
           "gcal_in_auto": True, "gcal_buffer": 15, "gcal_auto_add": True})
    _same(client, "/settings/telegram/kinds", {"kind": ["message", "hit"]},
          {"telegram_enabled": False, "telegram_kinds": ["hit", "message"]})
    _same(client, "/settings/agent/webhook", {"url": "https://example.org/h", "kind": ["sale", "hit"]},
          {"agent_kinds": ["hit", "sale"]})
    _same(client, "/settings/ha/push", {"service": "mobile_app_pixel"}, {"ha_notify_service": "mobile_app_pixel"})
    _same(client, "/lang", {"lang": "de"}, {"ui_lang": "de"})


def test_parity_claude_prices_timeline_system(client, monkeypatch):
    from app.llm import models as llm_models

    async def no_refresh(*a, **kw):
        return llm_models.available()
    monkeypatch.setattr(llm_models, "refresh", no_refresh)
    _same(client, "/settings/timeline", {"record": "on", "enabled": "on", "days": "14", "max_mb": "200"},
          {"timeline_enabled": True, "timeline_prune": True, "timeline_days": 14, "timeline_max_mb": 200})
    _same(client, "/settings/vpn/interval", {"hours": "12"}, {"vpn_shuffle_h": 12})
    monkeypatch.setattr("app.platforms.vpn.conn.prune_helpers", lambda idle_s=0: asyncio.sleep(0))
    _same(client, "/settings/vpn/parallel", {"n": "3"}, {"vpn_parallel": 3})


def test_price_zones_are_stored_like_the_page_stores_them(client):
    """Only the groups that differ from the defaults are kept; the other groups stay as they are."""
    patch(client, {"price_zones_new": [60, 80, 95, 110]})
    patch(client, {"price_zones_used": [30, 50, 70, 90]})
    assert get_setting("price_zones") == '{"used": [30, 50, 70, 90], "new": [60, 80, 95, 110]}'
    patch(client, {"price_zones_used": [35, 55, 75, 90]})                       # back to the defaults: dropped
    assert get_setting("price_zones") == '{"new": [60, 80, 95, 110]}'
    assert client.get("/api/settings/price_zones_new").json()["value"] == [60, 80, 95, 110]


# ------------------------------------------------------------------ authentication and the timeline
def test_bearer_token_opens_it_and_nothing_else_does(client, locked):
    assert client.get("/api/settings").status_code == 401
    assert patch(client, {"auto_draft": True}).status_code == 401
    assert get_setting("auto_draft") != "1"
    assert client.get("/api/settings", headers=BEARER).status_code == 200
    assert client.get("/api/settings/quiet_mode", headers=BEARER).json()["value"] == "slow"
    r = patch(client, {"auto_draft": True}, headers=BEARER)
    assert r.status_code == 200 and r.json()["changed"] == {"auto_draft": True}
    bad = {"authorization": "Bearer nope"}
    assert patch(client, {"auto_draft": False}, headers=bad).status_code == 401 and get_setting("auto_draft") == "1"
    # the token itself cannot be reached through the API, not even to read it
    assert client.get("/api/settings/agent_token", headers=BEARER).status_code == 404
    assert TOKEN not in client.get("/api/settings", headers=BEARER).text


def test_a_change_shows_up_in_the_timeline(client, locked):
    timeline.set_recording(timeline=True)
    patch(client, {"quiet_mode": "pause", "reply_rules": "x" * 100, "auto_draft": True}, headers=BEARER)
    patch(client, {"quiet_mode": "pause"}, headers=BEARER)                               # nothing changed: no entry
    patch(client, {"quiet_mode": "nonsense"}, headers=BEARER)                            # refused: no entry
    rows, _ = timeline.query(timeline.Filters(kind="action"))
    rows = [r for r in rows if r.name == "PATCH /api/settings"]
    assert len(rows) == 1 and rows[0].actor == "api" and rows[0].ok
    assert rows[0].payload == {"changed": {"quiet_mode": "pause", "reply_rules": "<100 characters>", "auto_draft": True}}
    client.patch("/api/settings", json={"values": {"quiet_mode": "slow"}}, headers={**BEARER, "x-wh-client-kind": "mcp"})
    assert [r.actor for r in timeline.query(timeline.Filters(kind="action"))[0] if r.name == "PATCH /api/settings"][0] == "mcp"
