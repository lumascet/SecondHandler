"""Home Assistant notifications: a bus event per hit/message plus optional notify service."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.config import address_check, get_settings
from app.db import log_event
from app.services import agent_events

log = logging.getLogger(__name__)

_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport


PUSH_CHANNELS = ("auto", "ha", "telegram", "both")

_WH_AD_ID = re.compile(r"willhaben\.at/.*?(?:adId=|-)(\d{6,})(?:/?$|[/?&#])")
# willhaben's own chat list / chat / my-adverts pages (older rows and pushes without ids)
_WH_PAGE = re.compile(r"^https://www\.willhaben\.at/iad/myprofile/(chat|myadverts)(?:/([\w-]+))?/?(?:[?#].*)?$")


def _relative(url: str | None) -> bool:
    return bool(url) and url.startswith("/") and not url.startswith("//")


def _reachable() -> bool:
    """public_url can be opened from a phone (not localhost; Settings → Web address shows the same check)."""
    return address_check(get_settings().public_url)["reachable"]


def app_path(kind: str, url: str | None, ad_id: Any = None, conversation_id: Any = None) -> str | None:
    """The page in this app for a notification/activity entry (relative path), None when only `url` fits
    (e.g. a calendar link). Hits/price drops: /hits?view=<id> opens the ad viewer (_ad_panel.html)."""
    url = url or ""
    base = get_settings().public_url.rstrip("/")
    if base and url.startswith(base):
        return url[len(base):] or "/"
    if _relative(url):                    # already a page here (e.g. "/settings/claude")
        return url
    if conversation_id:
        return f"/chats/{conversation_id}"
    ad_id = str(ad_id) if ad_id else (m.group(1) if (m := _WH_AD_ID.search(url)) else None)
    if ad_id and kind in ("hit", "price_drop", "saved"):
        return f"/hits?view={ad_id}"
    if ad_id and kind.startswith("ad_"):
        return f"/myads?ad={ad_id}"
    if kind in ("hit", "price_drop") and not url:
        return "/hits"                    # "+N more new hits"
    if m := _WH_PAGE.match(url):
        return "/myads" if m.group(1) == "myadverts" else "/chats" + (f"/{m.group(2)}" if m.group(2) else "")
    return None


def app_link(kind: str, url: str | None, data: dict[str, Any] | None = None) -> str | None:
    """Full link to the app page of a notification (public_url + app_path), None if no page fits."""
    data = data or {}
    path = app_path(kind, url, data.get("ref") or data.get("ad_id"), data.get("conversation_id"))
    return get_settings().public_url.rstrip("/") + path if path else None


def push_url(kind: str, url: str | None, data: dict[str, Any] | None = None) -> str | None:
    """What a phone push opens: the app page when public_url is reachable from the phone (not localhost),
    otherwise the original link (willhaben); a bare app path is nothing a phone can open then."""
    if not _reachable():
        return None if _relative(url) else url
    return app_link(kind, url, data) or url


def push_channel() -> str:
    """Where phone pushes go. "auto": Telegram once a chat is linked, otherwise Home Assistant – never both."""
    from app.db import get_setting
    from app.services import telegram

    v = get_setting("push_channel") or "auto"
    if v == "auto":
        return "telegram" if telegram.active() else "ha"
    return v if v in PUSH_CHANNELS else "ha"


async def notify(kind: str, title: str, message: str, *, url: str | None = None,
                 image: str | None = None, data: dict[str, Any] | None = None,
                 actions: list[dict[str, Any]] | None = None, tag: str | None = None, push: bool = True,
                 channel: str | None = None) -> bool:
    """Fire `willhaben_<kind>` on the HA event bus and, if configured, a push via notify.<service>.

    Always records the event in the dashboard log and, if linked, posts it to Telegram (with buttons).
    Returns True if HA accepted the event. `channel` overrides the push channel setting (the connection
    watchdog uses "both" when one channel is broken).
    """
    d = data or {}
    ids = {k: str(v) for k in ("ad_id", "conversation_id") if (v := d.get(k))}
    if d.get("ref"):
        ids["ad_id"] = str(d["ref"])          # other sites: "<platform>:<id>", so the inbox opens the right viewer
    event_id = log_event(kind, f"{title}: {message}", url, quiet=not push, title=title, data=d or None,
                         **ids)       # quiet: no inbox entry, no notice on a page
    try:                              # the AI agent channel (webhook) needs neither Home Assistant nor Telegram
        agent_events.dispatch(event_id, kind)
    except Exception as e:  # noqa: BLE001 - never lets the agent channel break a notification
        log.warning("agent event dispatch failed: %s", e)
    from app.services import telegram
    from app.services.ha_auth import ha_token, ha_url, notify_service

    channel = channel if channel in ("ha", "telegram", "both") else push_channel()
    link = push_url(kind, url, data)      # the app page when the phone can reach it, else the willhaben link
    # push=False: log + HA event only (e.g. a hit the Claude screening rejected)
    if push and channel in ("telegram", "both"):      # Telegram buttons take https links only
        await telegram.send(kind, title, message, url=link if link and link.startswith("https://") else url,
                            image=image, data=data, actions=actions, tag=tag)

    base, token, service = ha_url(), ha_token(), notify_service()
    if channel not in ("ha", "both"):
        service = None          # HA still gets the event (automations), just no phone push
    if not (base and token):
        return False
    # "url" stays the original link (user automations), "app_url" is the page in this app (None if a phone can't open it)
    payload = {"title": title, "message": message, "url": url, "image": image,
               "app_url": app_link(kind, url, data) if _reachable() else None, **(data or {})}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport) as c:
            r = await c.post(f"{base}/api/events/willhaben_{kind}", json=payload, headers=headers)
            ok = r.status_code == 200
            if not ok:
                log.warning("HA event %s failed: %s %s", kind, r.status_code, r.text[:200])
            if service and push:
                push_data: dict[str, Any] = {}
                if link:
                    push_data.update({"url": link, "clickAction": link})
                if image:
                    push_data["image"] = image
                if actions:          # buttons in the HA companion app push
                    push_data["actions"] = actions
                if tag:              # same tag replaces / clears the push later
                    push_data["tag"] = tag
                await c.post(
                    f"{base}/api/services/notify/{service}",
                    json={"title": title, "message": message, "data": push_data},
                    headers=headers,
                )
            return ok
    except httpx.HTTPError as e:
        log.warning("HA notify failed: %s", e)
        return False


_later: set[asyncio.Task] = set()     # notify_later() tasks on their way (the loop keeps only weak references)


def notify_later(kind: str, title: str, message: str, **kw: Any) -> None:
    """notify() from code that must not wait for it or fail by it (a ledger write, a status change): a task on the
    running loop, or right here outside one. Errors are logged, never raised."""
    async def send() -> None:
        try:
            await notify(kind, title, message, **kw)
        except Exception as e:  # noqa: BLE001 - what triggered it is done by now
            log.warning("%s notification failed: %s", kind, e)

    try:
        task = asyncio.get_running_loop().create_task(send())
    except RuntimeError:                  # no running loop (a thread, a script)
        asyncio.run(send())
        return
    _later.add(task)
    task.add_done_callback(_later.discard)


async def clear_push(tag: str, telegram_buttons: bool = True) -> None:
    """Remove a push from the phone (HA companion app: message "clear_notification" with the tag); in Telegram the
    message stays and its buttons go, unless `telegram_buttons` is False (a hit seen: keep, ignore and open still
    make sense there)."""
    from app.services import telegram
    from app.services.ha_auth import ha_token, ha_url, notify_service

    if telegram_buttons:
        await telegram.clear(tag)

    base, token, service = ha_url(), ha_token(), notify_service()
    if not (base and token and service):
        return
    try:
        async with httpx.AsyncClient(timeout=15, transport=_transport) as c:
            await c.post(f"{base}/api/services/notify/{service}", headers={"Authorization": f"Bearer {token}"},
                         json={"message": "clear_notification", "data": {"tag": tag}})
    except httpx.HTTPError as e:
        log.info("clear push failed: %s", e)
