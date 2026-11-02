"""Settings → Backup: download a backup, upload one, preview, confirm, self-restart (logic in services/backup.py)."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask

from app.config import get_settings
from app.i18n import t
from app.services import backup

log = logging.getLogger(__name__)
router = APIRouter()
_restore_lock = asyncio.Lock()


def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/settings/backup?msg={quote(msg, safe='')}", status_code=303)


def downloads_allowed() -> bool:
    """A backup file carries every token (and maybe the willhaben login). Without DASHBOARD_PASSWORD anyone on the
    network (or a DNS-rebinding web page) could fetch it with one request, so downloads need the password set."""
    return bool(get_settings().dashboard_password)


def panel() -> dict:
    """Template context for the Backup panel on the settings page."""
    backup._purge_pending()          # abandoned previews (decrypted backups) older than PENDING_TTL_S
    return {"crypto": backup.has_crypto(), "safety": backup.safety_backups(), "keep": backup.KEEP_SAFETY,
            "downloads": downloads_allowed()}


async def _quiesce(app) -> None:
    """Stop everything that writes to the database, the tokens or willhaben before a restore replaces them.

    The process restarts right after the restore, so nothing here has to be undone."""
    from app import main, scheduler
    from app.services import chats
    from app.willhaben.session import get_session

    tasks: list[asyncio.Task] = []
    # running scheduler jobs: APScheduler's AsyncIOExecutor cancels them on shutdown but does not wait for them
    executor = scheduler.scheduler._executors.get("default") if scheduler.scheduler.running else None
    tasks += [f for f in getattr(executor, "_pending_futures", ()) if isinstance(f, asyncio.Future)]
    scheduler.shutdown()
    # HA / Telegram listeners and the chat watchdog (started in main.lifespan)
    tasks += list(getattr(app.state, "listener_tasks", ()))
    # live chat: no more callbacks, no more coalesced syncs, no delayed auto replies, no fire-and-forget work
    sess = get_session()
    sess._chat_cb = None
    if chats._live.get("task") is not None:
        tasks.append(chats._live["task"])
    tasks += list(chats._humanized.values())
    tasks += list(main._background)
    # the jobs (agent checks, Claude calls, purchases: services/jobs) and the Vinted live stream write too
    from app.services import jobs, vpn_check
    tasks += [j.task for j in list(jobs._active) if getattr(j, "task", None) is not None]
    try:
        from app.platforms import vinted_inbox
        if vinted_inbox._live.get("task") is not None:
            tasks.append(vinted_inbox._live["task"])
    except ImportError:  # pragma: no cover
        pass
    try:
        await vpn_check.shutdown()              # a running VPN search (its tunnels, its state in the settings)
    except Exception as e:  # noqa: BLE001 - restore anyway
        log.warning("VPN search stop before restore failed: %s", e)
    current = asyncio.current_task()
    tasks = [x for x in dict.fromkeys(tasks) if x is not current and not x.done()]
    for x in tasks:
        x.cancel()
    if tasks:
        done, still = await asyncio.wait(tasks, timeout=15)
        if still:
            log.warning("restore: %d background task(s) did not stop in time", len(still))
    # the browser goes in any case: it holds the chat WebSocket and (maybe) the profile being replaced
    try:
        await sess.stop()
    except Exception as e:  # noqa: BLE001 - restore anyway, the restart starts a fresh browser
        log.warning("browser stop before restore failed: %s", e)


@router.post("/settings/backup/export")
async def backup_export(include_profile: str | None = Form(None), passphrase: str = Form(""),
                        passphrase2: str = Form("")):
    if not downloads_allowed():
        return _back(t("Set DASHBOARD_PASSWORD to enable backup downloads."))
    backup._purge_pending()
    if passphrase and not backup.has_crypto():
        return _back(t("Encryption is not available in this installation."))
    if passphrase != passphrase2:
        return _back(t("The two passphrases do not match."))
    if passphrase and len(passphrase) < 8:
        return _back(t("Use a passphrase with at least 8 characters."))
    backup.purge_exports()
    name = backup.export_name(encrypted=bool(passphrase))
    dest = backup.backups_dir() / f"export-{name}"
    try:
        await asyncio.to_thread(backup.create_backup, dest, bool(include_profile), passphrase or None)
    except (OSError, backup.BackupError) as e:
        log.exception("backup export failed")
        dest.unlink(missing_ok=True)
        return _back(t("Backup failed: {error}", error=e))
    return FileResponse(dest, media_type="application/octet-stream" if passphrase else "application/zip",
                        filename=name, background=BackgroundTask(dest.unlink, missing_ok=True))


@router.post("/settings/backup/import")
async def backup_import(request: Request):
    from app.main import render
    # Starlette spools file parts to /tmp without a limit: refuse oversized bodies before parsing the form
    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:
        length = -1
    if not 0 <= length <= backup.MAX_UPLOAD + (1 << 20):
        return _back(t("Backup rejected: {error}", error=t("The backup is too large to restore.")) if length > 0
                     else t("Backup rejected: {error}", error=t("The upload has no size.")))
    form = await request.form()
    file, passphrase = form.get("file"), form.get("passphrase") or ""
    if not hasattr(file, "read") or not isinstance(passphrase, str):
        await form.close()
        return _back(t("Choose a backup file first."))
    try:
        pending = await asyncio.to_thread(backup.inspect_upload, file.file, passphrase or None)
    except backup.NeedsPassphrase as e:
        return _back(str(e))
    except backup.BackupError as e:
        return _back(t("Backup rejected: {error}", error=e))
    finally:
        await form.close()
    return render(request, "backup_preview.html", p=pending.summary, token=pending.token, filename=file.filename or "")


@router.get("/settings/backup/preview/{token}")
async def backup_preview(request: Request, token: str):
    from app.main import render
    try:
        pending = await asyncio.to_thread(backup.load_pending, token)
    except (backup.BackupError, OSError, ValueError) as e:
        return _back(str(e) if isinstance(e, backup.BackupError) else t("This import is no longer available. Upload the backup again."))
    return render(request, "backup_preview.html", p=pending.summary, token=token, filename="")


@router.post("/settings/backup/cancel")
async def backup_cancel(token: str = Form("")):
    backup.cancel_pending(token)
    return _back(t("Import cancelled, nothing was changed."))


@router.post("/settings/backup/restore")
async def backup_restore(request: Request, token: str = Form(""), confirm: str | None = Form(None)):
    from app.main import render

    if not confirm:
        return RedirectResponse(f"/settings/backup/preview/{quote(token, safe='')}", status_code=303)
    if _restore_lock.locked():
        return _back(t("An import is already running."))
    async with _restore_lock:
        try:
            await asyncio.to_thread(backup.load_pending, token)
        except (backup.BackupError, OSError, ValueError):
            return _back(t("This import is no longer available. Upload the backup again."))
        # nothing may write to the database, the tokens or the browser profile while they are replaced
        await _quiesce(request.app)
        error, safety = None, None
        try:
            res = await asyncio.to_thread(backup.restore, token)
            safety = res["safety"]
        except Exception as e:  # noqa: BLE001 - show it; the restart brings back a consistent process
            log.exception("backup restore failed")
            error = t("Restore failed: {error}", error=e)
    # the old process still holds caches, the scheduler and (maybe) a stopped browser: start fresh in any case
    backup.schedule_restart()            # fires ~2 s later, after this page went out
    return render(request, "backup_restart.html", error=error, safety=safety)


@router.get("/settings/backup/file/{name}")
async def backup_file(name: str):
    if not downloads_allowed():
        raise HTTPException(403, t("Set DASHBOARD_PASSWORD to enable backup downloads."))
    p = backup.safety_path(name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="application/zip", filename=p.name)
