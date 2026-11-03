"""Login details for the sites the app's browser logs in to (willhaben, Vinted), so a login after a logout is one
button: the login page opens in the remote view, the app types the e-mail and password and presses the button – as a
person would, character by character – and the owner finishes anything else there (a captcha, a code by e-mail).

Kept in DATA_DIR/login-<site>.json (mode 600), never logged, never shown again (the page only says that a password is
stored). Part of a backup like the other secrets of DATA_DIR.

Submitted on the owner's "Log in automatically" (login()), and by itself whenever the app finds the site logged out
(auto_login(): Settings switch `auto_login_<site>`, on by default; at most once every AUTO_LOGIN_EVERY_S, so a site
that keeps refusing is not hammered and the account is not locked). What it cannot finish alone – a captcha, a code
by e-mail – is pushed ("<site> needs you"): the owner finishes it in the remote view.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

SITES = ("willhaben", "vinted")
AUTO_LOGIN_EVERY_S = 3600          # automatic logins per site at most this often
AUTO_RETRY_UNSENT_S = 300          # ... but one that sent nothing (no form, no browser) may try again this soon
_auto_lock = asyncio.Lock()
_tasks: set = set()                # automatic logins started in the background (asyncio.Task)
# where each site asks for them (seen 2026-10-21: Vinted's e-mail login page, then POST /web/api/auth/oauth)
LOGIN_PAGES = {
    "willhaben": "https://www.willhaben.at/iad/myprofile/login?r=/iad/myprofile/myadverts",
    "vinted": "https://www.vinted.at/member/login/email?ref_url=%2Finbox",
}
USER_FIELDS = ('input[type="email"]', 'input[name="email"]', 'input[name="username"]', 'input[name="login"]',
               'input[autocomplete="username"]', 'input[id*="mail" i]', 'input[id*="user" i]')
PASSWORD_FIELDS = ('input[type="password"]',)
SUBMIT = ('button[type="submit"]', 'input[type="submit"]')


def _path(site: str):
    return get_settings().data_dir / f"login-{site}.json"


def get(site: str) -> dict[str, str] | None:
    """{"user", "password"} or None."""
    try:
        data = json.loads(_path(site).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("user") and data.get("password") else None


def stored(site: str) -> dict[str, Any]:
    """What a page may show: the user name, and whether a password is there (never the password)."""
    c = get(site)
    return {"user": c["user"] if c else "", "has_password": bool(c)}


def save(site: str, user: str, password: str | None) -> None:
    """A blank password keeps the stored one (the field is never filled in again)."""
    if site not in SITES:
        raise ValueError(site)
    user = (user or "").strip()
    old = get(site)
    password = password if password else (old["password"] if old else "")
    if not user or not password:
        raise ValueError("user and password needed")
    p = _path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"user": user, "password": password}, f)


def forget(site: str) -> None:
    _path(site).unlink(missing_ok=True)


async def _first(page, selectors: tuple[str, ...], timeout_ms: int = 15000):
    """The first visible element of these selectors (waiting for the page to render it)."""
    loc = page.locator(", ".join(selectors)).locator("visible=true").first
    await loc.wait_for(state="visible", timeout=timeout_ms)
    return loc


async def type_in(page, site: str) -> None:
    """Type the stored e-mail and password into the open login page and press its button. Typed key by key with
    small pauses: bot protections look at how a form is filled. Raises when a field is missing (the page shows
    something else: already logged in, a captcha) – the owner sees it in the remote view."""
    c = get(site)
    if c is None:
        raise ValueError("no login details stored")
    user = await _first(page, USER_FIELDS)
    await user.click()
    await user.fill("")
    await user.press_sequentially(c["user"], delay=70)
    pw = await _first(page, PASSWORD_FIELDS)
    await pw.click()
    await pw.press_sequentially(c["password"], delay=80)
    await page.wait_for_timeout(500)
    button = page.locator(", ".join(SUBMIT)).locator("visible=true")
    if await button.count():
        await button.first.click()
    else:
        await pw.press("Enter")
    log.info("%s: login details typed in and sent", site)


async def login(site: str) -> str:
    """Open the site's login page in the app's browser, type the stored details, send them and check. "ok" logged in,
    "already" (no form: logged in already), "sent" (sent, but the site asks for more: a captcha, a code), "no_form"
    (no login form and not logged in). Raises SessionUnavailable without a browser, ValueError without details."""
    from app.services import label_fill, site_accounts
    from app.willhaben.session import SessionUnavailable, get_session

    if get(site) is None:
        raise ValueError("no login details stored")
    sess = get_session()

    async def logged_in() -> bool:
        if site == "willhaben":
            return await sess.check_login()
        return bool((await site_accounts.check(site))["logged_in"])

    try:
        if site == "willhaben":
            await sess.open_login()
            page = sess.ui_page
        else:
            if site == "vinted":
                await _drop_dead_vinted_login(sess)
            await sess.open_site(site, LOGIN_PAGES[site])
            page = sess.site_pages[site]
        try:
            await label_fill.dismiss_consent(page)          # a cookie banner over the form
        except Exception:  # noqa: BLE001 - no banner
            pass
        await type_in(page, site)
        await page.wait_for_timeout(6000)
        return "ok" if await logged_in() else "sent"
    except SessionUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - no login form: already logged in, or the page shows a captcha/something else
        try:
            already = await logged_in()
        except Exception:  # noqa: BLE001 - the check itself failed
            already = False
        if already:
            log.info("login on %s: already logged in, no form to fill", site)
            return "already"
        log.info("login on %s stopped: %s", site, type(e).__name__)
        return "no_form"


async def _drop_dead_vinted_login(sess) -> None:
    """The browser still has the refused session: Vinted then sends even its login page to /session-refresh, which
    cannot renew it and never shows the form (11-03). Its login cookies go before a login; the bot protection's
    (datadome) stays. Not while the app's own login works (it is not refused: nothing to replace)."""
    import re as _re

    from app.platforms import vinted_messaging as vm
    if vm._alive or getattr(sess, "context", None) is None:
        return
    for name in (*vm.AUTH_COOKIES, "refresh_token_web"):
        try:
            await sess.context.clear_cookies(name=name, domain=_re.compile(r"vinted\."))
        except Exception as e:  # noqa: BLE001 - an older browser: the login page may still work
            log.info("vinted: old login cookie %s not cleared (%s)", name, type(e).__name__)


def auto_login_on(site: str) -> bool:
    from app.db import get_setting
    return get(site) is not None and get_setting(f"auto_login_{site}") != "0"


async def auto_login(site: str) -> str | None:
    """The site was found logged out: log in with the stored details by itself, at most every AUTO_LOGIN_EVERY_S.
    None when it did not try (switched off, no details, too soon, one running); else login()'s result. What it could
    not finish is pushed to the owner."""
    from app.db import get_setting, set_setting
    from app.i18n import t

    if site not in SITES or not auto_login_on(site) or _auto_lock.locked():
        return None
    try:
        last = float(get_setting(f"auto_login_{site}_at") or 0)
    except ValueError:
        last = 0.0
    unsent = get_setting(f"auto_login_{site}_result") in ("", "no_form", "error")   # nothing reached the site
    if time.time() - last < (AUTO_RETRY_UNSENT_S if unsent else AUTO_LOGIN_EVERY_S):
        return None
    async with _auto_lock:
        set_setting(f"auto_login_{site}_at", str(time.time()))      # before trying: a crash does not loop
        name = "willhaben" if site == "willhaben" else site.capitalize()
        try:
            result = await login(site)
        except Exception as e:  # noqa: BLE001 - no browser, no details: the owner hears it
            log.info("automatic login on %s not possible: %s", site, e)
            result = "error"
        log.warning("automatic login on %s: %s", site, result)
        set_setting(f"auto_login_{site}_result", result)
        if result not in ("ok", "already"):
            from app.services.notify import notify
            await notify("health", t("{site} needs you", site=name),
                         t("{site} logged out and the automatic login did not finish (a captcha or a code?) – "
                           "finish it in the browser window.", site=name),
                         url=f"/settings/{site}?remote={'1' if site == 'willhaben' else site}")
        return result


def auto_login_later(site: str) -> None:
    """auto_login() in the background (from code that just found the site logged out)."""
    try:
        task = asyncio.get_running_loop().create_task(auto_login(site))
    except RuntimeError:          # no event loop (scripts)
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
