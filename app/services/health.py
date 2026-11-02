"""Connection watchdog: is everything the app depends on still connected?

A scheduler job (`check`, every CHECK_INTERVAL_S) looks at willhaben login, Claude, Home Assistant, Telegram,
live chat, Google Calendar and free disk space. It only reads state the app already keeps in memory or on
disk – it makes no calls to willhaben, Claude, HA, Telegram or Google itself:

  willhaben   BrowserSession.logged_in / last_check (the session-check job does the real check)
  Claude      token present + outcome of the last `claude -p` call (app/llm/base.last_call)
  HA          URL + token present + push-button websocket connected (ha_actions.listener)
  Telegram    telegram.active() + last Bot API outcome (telegram.api_health)
  Live chat   BrowserSession.chat_live_ok()
  Calendar    gcal client + refresh token present (a revoked Google grant shows only when the calendar is used)
  Disk        free space of DATA_DIR
  VPN         the shared WireGuard connection has a server no site with its VPN switch on is paused on
  Accounts    each other site's login in the browser (Kleinanzeigen, Vinted: site_accounts.state) once the owner
              logged in there; a logout stops that site's messages, which otherwise nothing reported

Each item is "ok", "problem", "off" (not set up / switched off) or "unknown" (no data yet, ignored).
Optional services (Home Assistant, Telegram, Google Calendar) are "off" while they are not (fully) set up; they are a
"problem" only when a configured connection fails.
A problem must be seen on DEBOUNCE consecutive checks before it counts; then one `notify(kind="health")`.
Items whose source refreshes less often than this job runs (willhaben session check, Claude calls, Telegram
API calls) carry a `sample` id; re-reading the same failed sample does not count as another check, so one
transient failure is never reported. With no evidence since startup (no Claude call / Telegram API call yet)
an item is "unknown", so a restart never sends a "restored" message without a real successful call.
Recovery from a reported problem is reported once, right away. The confirmed state lives in the
`health_state` setting (JSON), so a restart does not repeat warnings. The very first observation of an item
is a silent baseline.

Channel fallback: if Home Assistant or Telegram is itself not ok and the normal push channel would use it,
the warning goes out on "both" channels, so a broken channel never swallows the warning on the other one.

Needs live verification: only tested with fakes. Not yet observed for real: an HA websocket drop, a Telegram
outage / revoked bot token, and the fallback push actually arriving on the phone.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config import get_settings
from app.db import get_setting, set_setting, utcnow
from app.i18n import t
from app.services import quiet_hours

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 300
DEBOUNCE = 2                     # consecutive checks a problem must persist before it is reported
DISK_MIN_FREE_MB = 1024          # setting `health_disk_min_mb` overrides
STATE_KEY = "health_state"


@dataclass
class Status:
    key: str
    label: str
    state: str                   # ok | problem | off | unknown
    reason: str = ""
    href: str | None = None      # settings panel for the tile
    sample: str | None = None    # id of the underlying observation; same id = same evidence, not a new check


# ------------------------------------------------------------------ individual checks
def _willhaben() -> Status:
    from app.willhaben.session import get_session

    s = get_settings()
    st = Status("willhaben", "willhaben", "unknown", href="/settings/willhaben")
    if not s.browser_enabled:
        st.state, st.reason = "off", t("Browser switched off")
        return st
    sess = get_session()
    if not sess.started:
        st.state, st.reason = "problem", t("Browser is not running")
        return st
    if sess.logged_in is None:
        st.reason = t("Not checked yet")
        return st
    if not sess.logged_in:
        st.state, st.reason = "problem", t("Not logged in")
        st.sample = sess.last_check.isoformat() if sess.last_check else None
        return st
    overdue = timedelta(seconds=3 * s.session_check_interval_s + 120)
    last, quiet, quiet_end = sess.last_check, quiet_hours.state(), quiet_hours.last_end()
    if last and quiet_end and quiet_end > last:
        last = quiet_end            # quiet hours held the login check: it is due again from their end
    if last and not (quiet and quiet.active) and utcnow() - last > overdue:
        st.state, st.reason = "problem", t("Login check overdue")
        return st
    nick = (sess.user or {}).get("nickname")
    st.state, st.reason = "ok", (t("Logged in as {name}", name=nick) if nick else t("Logged in"))
    return st


def _claude() -> Status:
    from app.llm import base
    from app.llm.claude_login import claude_token

    st = Status("claude", "Claude", "unknown", href="/settings/claude")
    if not claude_token():
        st.state, st.reason = "problem", t("Not signed in")
        return st
    last = base.last_call
    at = last.get("at")
    st.sample = at.isoformat() if hasattr(at, "isoformat") else (str(at) if at else None)
    if last.get("ok") is False:
        st.state, st.reason = "problem", t("Last call failed: {error}", error=(last.get("error") or "")[:120])
    elif last.get("ok"):
        st.state, st.reason = "ok", t("Last call OK")
    else:
        st.reason = t("Signed in")           # no call since startup: no evidence either way -> "unknown"
    return st


def _ha() -> Status:
    from app.services import ha_actions
    from app.services.ha_auth import ha_token, ha_url

    st = Status("ha", "Home Assistant", "ok", href="/settings/ha")
    base, token = ha_url(), ha_token()
    if not base and not token:
        st.state, st.reason = "off", t("Not set up")
        return st
    if not (base and token):      # set up only half (e.g. HA_URL without signing in): optional, not an error
        st.state, st.reason = "off", t("Not set up")
        return st
    lst = ha_actions.listener
    if get_settings().ha_listener and lst.get("running") and not lst.get("connected"):
        st.state, st.reason = "problem", t("Push buttons not connected")
        return st
    st.reason = t("Connected")
    return st


def _telegram() -> Status:
    from app.services import telegram

    st = Status("telegram", "Telegram", "ok", href="/settings/telegram")
    if not telegram.token() or get_setting("telegram_enabled") == "0":
        st.state, st.reason = "off", t("Not set up")
        return st
    if not telegram.active():     # bot added, no chat linked yet: setup unfinished, not a failure
        st.state, st.reason = "off", t("Not linked yet")
        return st
    h = telegram.api_health
    if not h.get("ok_at") and not h.get("error_at"):
        st.state, st.reason = "unknown", t("Not checked yet")    # no Bot API call since startup
        return st
    if h.get("error_at") and (not h.get("ok_at") or h["error_at"] > h["ok_at"]):
        st.state, st.reason = "problem", t("Telegram not reachable: {error}", error=h.get("error") or "")
        st.sample = h["error_at"].isoformat() if hasattr(h["error_at"], "isoformat") else str(h["error_at"])
        return st
    st.reason = t("Connected")
    return st


def _live_chat() -> Status:
    from app.willhaben.session import get_session

    s = get_settings()
    st = Status("live_chat", t("Live chat"), "ok", href="/settings/chats")
    sess = get_session()
    if not (s.chat_live and s.browser_enabled):
        st.state, st.reason = "off", t("Switched off")
        return st
    if not (sess.started and sess.logged_in):
        # unknown, not off: the willhaben item reports the logout; the live chat keeps its last state, so a live chat
        # that fails to connect after the owner logged in again is reported (off -> problem would stay silent)
        st.state, st.reason = "unknown", t("Needs willhaben login")
        return st
    if not sess.chat_live_ok():
        st.state, st.reason = "problem", t("Not connected, polling instead")
        return st
    st.reason = t("Connected")
    return st


def _gcal() -> Status:
    from app.services import gcal

    st = Status("gcal", "Google Calendar", "ok", href="/settings/calendar")
    if not gcal.has_client():
        st.state, st.reason = "off", t("Not set up")
    elif not gcal.connected():    # client saved, sign-in not finished: optional, not a failure
        st.state, st.reason = "off", t("Not connected yet")
    else:
        st.reason = t("Connected")
    return st


def _disk() -> Status:
    st = Status("disk", t("Disk"), "ok")
    try:
        usage = shutil.disk_usage(get_settings().data_dir)
    except OSError as e:
        st.state, st.reason = "unknown", str(e)[:100]
        return st
    try:
        min_mb = int(get_setting("health_disk_min_mb") or DISK_MIN_FREE_MB)
    except ValueError:
        min_mb = DISK_MIN_FREE_MB
    free_gb = f"{usage.free / 1024 ** 3:.1f}"
    if usage.free < min_mb * 1024 ** 2:
        st.state, st.reason = "problem", t("Only {n} GB free", n=free_gb)
    else:
        st.reason = t("{n} GB free", n=free_gb)
    return st


def _vpn() -> Status:
    """The anonymous requests' WireGuard connections (app/platforms/vpn.py): off unless a site has its VPN switch
    on; a problem when there is no working server or such a site has none (its requests wait, they never go out
    directly); unknown while the search looks for a server for a waiting site."""
    from app.platforms import vpn

    state, reason = vpn.health()
    return Status("vpn", "VPN", state, reason, href="/settings/vpn")


def _site_login(pid: str) -> Status:
    """A site account's login (site_accounts): off while the site is off or the owner never logged in there; a
    problem once a check (or a refused API call) found it logged out."""
    from app import platforms
    from app.services import site_accounts

    site = site_accounts.SITES[pid]
    st = Status(f"account_{pid}", site.name, "off", href=f"/settings/{pid}")
    if not platforms.enabled(pid):
        st.reason = t("Switched off")
        return st
    logged_in = site_accounts.state(pid)["logged_in"]
    if logged_in is None:
        st.reason = t("Not set up")
    elif logged_in:
        st.state, st.reason = "ok", t("Logged in")
    else:
        st.state, st.reason = "problem", t("Not logged in – messages from {site} do not arrive", site=site.name)
    return st


def _site_logins() -> list[Status]:
    from app.services import site_accounts
    return [_site_login(pid) for pid in site_accounts.SITES]


CHECKS = [_willhaben, _claude, _ha, _telegram, _live_chat, _gcal, _disk, _vpn, _site_logins]


def collect() -> list[Status]:
    """Current status of every item (cheap, no network): used by the job and the home page tiles."""
    out: list[Status] = []
    for fn in CHECKS:
        try:
            got = fn()
            out.extend(got if isinstance(got, list) else [got])
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            log.warning("health check %s failed: %s", fn.__name__, e)
            out.append(Status(fn.__name__.strip("_"), fn.__name__.strip("_"), "unknown", str(e)[:100]))
    return out


# ------------------------------------------------------------------ state machine
def load_state() -> dict[str, Any]:
    try:
        data = json.loads(get_setting(STATE_KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def step(state: dict[str, Any], current: list[Status], now: datetime | None = None
         ) -> tuple[dict[str, Any], list[Status], list[Status]]:
    """Advance the debounced state by one check. Returns (new state, newly confirmed problems, recoveries)."""
    now_s = (now or utcnow()).isoformat()
    problems: list[Status] = []
    recovered: list[Status] = []
    for st in current:
        if st.state == "unknown":
            continue
        prev = state.get(st.key)
        if prev is None:                                     # first observation: silent baseline
            state[st.key] = {"state": st.state, "reason": st.reason, "since": now_s, "count": 0, "notified": False}
            continue
        if st.state == prev["state"]:
            prev.update(reason=st.reason, count=0, pending=None, sample=None)
            continue
        if st.state == "problem":
            if prev.get("pending") == "problem" and st.sample is not None and st.sample == prev.get("sample"):
                continue                                     # same observation read again: not a new check
            prev["count"] = prev.get("count", 0) + 1 if prev.get("pending") == "problem" else 1
            prev["pending"] = "problem"
            prev["sample"] = st.sample
            if prev["count"] >= DEBOUNCE:
                was_ok = prev["state"] == "ok"
                state[st.key] = {"state": "problem", "reason": st.reason, "since": now_s, "count": 0,
                                 "notified": was_ok}
                if was_ok:
                    problems.append(st)
            continue
        # ok or off: takes effect right away; a reported problem that is fixed gets a recovery message
        if st.state == "ok" and prev["state"] == "problem" and prev.get("notified"):
            recovered.append(st)
        state[st.key] = {"state": st.state, "reason": st.reason, "since": now_s, "count": 0, "notified": False}
    return state, problems, recovered


def channel_for(current: list[Status]) -> str | None:
    """Push channel for a health message: the usual one, or "both" if the usual one is itself not ok."""
    from app.services.notify import push_channel

    by_key = {s.key: s.state for s in current}
    usual = push_channel()
    uses = {"ha": ["ha"], "telegram": ["telegram"], "both": ["ha", "telegram"]}.get(usual, ["ha"])
    if all(by_key.get(c) == "ok" for c in uses):
        return None                                          # notify() picks it as usual
    return "both"


async def _send(title: str, items: list[Status], channel: str | None) -> None:
    from app.services.notify import notify

    message = "\n".join(f"{s.label}: {s.reason}" for s in items)
    url = get_settings().public_url.rstrip("/") + "/"
    try:
        await notify("health", title, message, url=url, channel=channel,
                     data={"items": [s.key for s in items]})
    except Exception:  # noqa: BLE001 - the watchdog must keep running
        log.exception("health notification failed")


async def check() -> list[Status]:
    """Scheduler job: collect, debounce, notify on ok -> problem and on recovery."""
    current = collect()
    state, problems, recovered = step(load_state(), current)
    state["_checked_at"] = utcnow().isoformat()
    set_setting(STATE_KEY, json.dumps(state))
    if problems or recovered:
        channel = channel_for(current)
        if problems:
            log.warning("connection problem: %s", ", ".join(f"{s.key} ({s.reason})" for s in problems))
            await _send(t("Connection problem"), problems, channel)
        if recovered:
            log.info("connection restored: %s", ", ".join(s.key for s in recovered))
            await _send(t("Connection restored"), recovered, channel)
    return current
