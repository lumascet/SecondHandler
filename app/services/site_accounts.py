"""Accounts on other sites (Kleinanzeigen, Vinted) in the app's browser, for their messages.

The owner logs in once in the remote view (the same browser profile as willhaben, so the session stays like
willhaben's). Searches never use it: the search agents, the price check and the product search keep using the
anonymous clients (app/platforms/*), so they are not linkable to these accounts either.

Whether a session is still valid is seen by loading a page that needs one in a short-lived hidden tab: a real page,
so the sites' bot protection (DataDome on Vinted) lets it through, where a bare request would be refused. A redirect
to the login page means logged out. The state is kept in the setting `account_<site>` ({logged_in, checked_at,
error}); it is checked on demand and every few hours once the owner logged in.

A login a site keeps in session cookies (no expiry) is gone when the browser restarts – every update of the app
logged out of Kleinanzeigen. So a check that finds the owner logged in keeps the site's session cookies in
data/site-cookies-<site>.json (owner-only), and the browser gets them back when it starts (restore_cookies); "Reset"
deletes the file with the cookies.
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.db import get_setting, set_setting, utcnow
from app.i18n import t

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SiteAccount:
    id: str
    name: str
    login_url: str
    check_url: str                   # a page only a logged-in visitor gets (else a redirect to the login)
    logged_out: tuple[str, ...]      # parts of the URL a logged-out visitor lands on
    hosts: tuple[str, ...]           # what the request recorder keeps of this site
    logged_out_text: str = ""        # text of a logged-out page that keeps the address (regex)


SITES: dict[str, SiteAccount] = {
    "kleinanzeigen": SiteAccount(
        "kleinanzeigen", "Kleinanzeigen", "https://www.kleinanzeigen.de/m-einloggen.html",
        "https://www.kleinanzeigen.de/m-nachrichten.html", ("m-einloggen", "/login"),
        ("kleinanzeigen.de",)),
    "vinted": SiteAccount(
        "vinted", "Vinted", "https://www.vinted.at/member/signup/select_type?ref_url=%2Finbox",
        # /session-refresh: the inbox sends there to renew the token and back; a page still there after it settled
        # could not renew it – logged out (it counted as logged in, 11-02)
        "https://www.vinted.at/inbox", ("/member/signup", "/member/login", "/auth/", "/session-refresh"),
        ("vinted.at", "vinted.de", "vinted.com"), r"Registrieren\s*\|\s*Einloggen|Weiter mit Google"),
}


def get(pid: str) -> SiteAccount | None:
    return SITES.get(pid)


def state(pid: str) -> dict[str, Any]:
    """{logged_in: True | False | None (never checked), checked_at: ISO | None, error: str | None}."""
    try:
        data = json.loads(get_setting(f"account_{pid}") or "{}")
    except ValueError:
        data = {}
    return {"logged_in": data.get("logged_in"), "checked_at": data.get("checked_at"), "error": data.get("error")}


def checked_at(pid: str) -> datetime | None:
    raw = state(pid)["checked_at"]
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _save(pid: str, logged_in: bool | None, error: str | None = None) -> dict[str, Any]:
    data = {"logged_in": logged_in, "checked_at": utcnow().isoformat(), "error": error}
    set_setting(f"account_{pid}", json.dumps(data))
    return data


# the bot protection's block page or captcha (DataDome on Vinted: "Deine Sitzung wurde blockiert", seen 2026-10-21);
# it stays on the page's own address, so only its text tells
_BLOCKED = re.compile(r"Sitzung wurde blockiert|session (?:has been|was) blocked|captcha-delivery\.com|"
                      r"Zugriff verweigert|Access (?:denied|blocked)", re.I)


def logged_out_url(site: SiteAccount, url: str) -> bool:
    return any(part in (url or "") for part in site.logged_out)


async def open_login(pid: str) -> None:
    """The site's login page in the visible browser window (the remote view)."""
    from app.willhaben.session import get_session

    site = SITES[pid]
    await get_session().open_site(pid, site.login_url)        # its own tab: the willhaben window stays


def seen_in_tab(pid: str, url: str | None) -> None:
    """The site's own tab shows its login or sign-up page: logged out, no request needed (a stored "logged in" from
    an earlier check would say otherwise)."""
    site = SITES.get(pid)
    if site is not None and url and logged_out_url(site, url) and state(pid)["logged_in"] is not False:
        _save(pid, False)


def _cookie_file(pid: str):
    return get_settings().data_dir / f"site-cookies-{pid}.json"


def _site_pattern(site: SiteAccount) -> re.Pattern:
    return re.compile(r"(?:^|\.)(?:" + "|".join(re.escape(h.split(".")[0]) for h in site.hosts) + r")\.[a-z.]+$")


async def keep_cookies(pid: str) -> int:
    """The site's session cookies (no expiry: the browser forgets them on restart) into its file; how many."""
    from app.willhaben.session import get_session

    site, sess = SITES[pid], get_session()
    if not sess.context:
        return 0
    pattern = _site_pattern(site)
    keep = [c for c in await sess.context.cookies()
            if pattern.search((c.get("domain") or "").lstrip(".")) and (c.get("expires") or -1) < 0]
    path = _cookie_file(pid)
    if not keep:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep))
    path.chmod(0o600)
    return len(keep)


async def restore_cookies(context: Any) -> int:
    """After the browser started: the kept session cookies of each site it does not have now; how many."""
    have = {(c.get("domain"), c.get("path"), c.get("name")) for c in await context.cookies()}
    back = []
    for pid in SITES:
        path = _cookie_file(pid)
        try:
            kept = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            continue
        back += [c for c in kept if (c.get("domain"), c.get("path"), c.get("name")) not in have]
    if back:
        await context.add_cookies(back)
        log.info("browser: %d kept session cookies of other sites restored", len(back))
    return len(back)


async def forget(pid: str) -> int:
    """Delete every cookie of the site in the app's browser (login, the bot protection's mark): a fresh start after
    the site blocked this browser ("Deine Sitzung wurde blockiert"). Returns how many went."""
    from app.willhaben.session import get_session

    site = SITES[pid]
    sess = get_session()
    sess._require()
    pattern = re.compile(r"(?:^|\.)(?:" + "|".join(re.escape(h.split(".")[0]) for h in site.hosts) + r")\.[a-z.]+$")
    gone = [c for c in await sess.context.cookies() if pattern.search((c.get("domain") or "").lstrip("."))]
    await sess.context.clear_cookies(domain=re.compile(pattern.pattern))
    _cookie_file(pid).unlink(missing_ok=True)
    _save(pid, False)
    return len(gone)


async def check(pid: str) -> dict[str, Any]:
    """Load the site's members-only page in a hidden tab; the state as stored. Never raises."""
    from app.willhaben.session import SessionUnavailable, get_session

    site = SITES[pid]
    sess = get_session()
    try:
        if pid == "vinted":           # the page borrows the app's login and gives back what it left (one holder)
            from app.platforms import vinted_messaging
            async with vinted_messaging.browser_turn(sess):
                final, text = await sess.load_hidden(site.check_url, with_text=True)
                if not (logged_out_url(site, final) or _BLOCKED.search(text or "")):
                    await _close_login_tab(sess, pid)
        else:
            final, text = await sess.load_hidden(site.check_url, with_text=True)
    except SessionUnavailable as e:
        return _save(pid, None, str(e))
    except Exception as e:  # noqa: BLE001 - a timeout or a closed browser: unknown, said so
        log.info("%s login check failed: %s", site.name, e)
        return _save(pid, None, t("The check did not finish: {error}", error=str(e) or type(e).__name__))
    if _BLOCKED.search(text or ""):
        return _save(pid, False, t("{site} blocks this browser – use “Reset {site}” on the session page, then log in "
                                   "again", site=site.name))
    if logged_out_url(site, final) or (site.logged_out_text and re.search(site.logged_out_text, text or "")):
        return _save(pid, False)
    try:
        await keep_cookies(pid)          # logged in: its session cookies survive the next restart
    except Exception as e:  # noqa: BLE001 - keeping them is a nicety
        log.info("%s session cookies not kept: %s", site.name, e)
    return _save(pid, True)


async def _close_login_tab(sess: Any, pid: str) -> None:
    """The login is done: the site's login tab goes, so its web app does not keep refreshing the token behind the
    app's back (Vinted: one holder, vinted_messaging)."""
    page = sess.site_pages.pop(pid, None) if getattr(sess, "site_pages", None) else None
    if page is not None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001 - already gone
            pass


async def check_job() -> None:
    """Every few hours: the sites the owner logged in on (others are never asked)."""
    from app.willhaben.session import get_session

    if not get_session().started:
        return
    for pid in SITES:
        if state(pid)["logged_in"] is not None:
            await check(pid)
