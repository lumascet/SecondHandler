"""Background jobs (APScheduler, asyncio)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from app.config import get_settings
from app.i18n import t
from app.db import SearchAgent, session, utcnow
from app.services import adaptive_pace, agent_events, chats, site_accounts, geizhals, health, human_pace, my_ads, quiet_hours, timeline, vpn_check
from app.services.search_agents import (is_checking, locate_job, retention_job, run_agent, status_sweep_job,
                                         watch_saved_job)
from app.willhaben import access_log
from app.willhaben.session import get_session

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Europe/Vienna")


def _agent_job(agent_id: int) -> str:
    return f"agent-{agent_id}"


async def _run_agent_job(agent_id: int) -> None:
    """Scheduled agent run; quiet hours may skip it (manual runs call run_agent directly)."""
    with session() as db:
        agent = db.get(SearchAgent, agent_id)
    if agent is None:
        return
    if quiet_hours.should_skip(agent):
        log.debug("agent %s: quiet hours, skipped", agent_id)
        return
    if adaptive_pace.should_skip(agent):
        log.debug("agent %s: a slow hour for new ads, skipped", agent_id)
        return
    if human_pace.take_break():
        log.info("agent %s: skipping this check (human-like break)", agent_id)
        return
    if is_checking(agent_id):            # "Run now" is still at it: the next turn comes soon enough
        log.info("agent %s: a check is still running, skipped", agent_id)
        return
    with timeline.fresh(actor="scheduler"):
        await run_agent(agent_id)


def schedule_agent(agent: SearchAgent, run_soon: bool = True) -> None:
    job_id = _agent_job(agent.id)
    if not agent.enabled:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        return
    extra = {"next_run_time": datetime.now().astimezone() + timedelta(seconds=5)} if run_soon else {}
    # Note: passing next_run_time=None would add the job *paused* in APScheduler 3.
    base, jitter = human_pace.trigger(agent.interval_s, get_settings().min_agent_interval_s)
    scheduler.add_job(
        _run_agent_job, IntervalTrigger(seconds=base, jitter=jitter),
        args=[agent.id], id=job_id, replace_existing=True, max_instances=1, coalesce=True, **extra,
    )


def reschedule_agents() -> None:
    """After the human-pace settings changed: re-register every agent with the new timing."""
    with session() as db:
        agents = list(db.exec(select(SearchAgent)))
    for a in agents:
        schedule_agent(a, run_soon=False)


FOLLOW_UP_S = 120


def follow_up(agent_id: int) -> None:
    """A check left hits pending (Claude screens at most screening.MAX_PER_RUN per check): one more check of the
    agent shortly, which judges them first (quiet hours and adaptive polling do not apply)."""
    if not scheduler.running:
        return
    log.info("agent %s: hits still pending, another check in %s s", agent_id, FOLLOW_UP_S)
    scheduler.add_job(_follow_up_job, "date", run_date=datetime.now().astimezone() + timedelta(seconds=FOLLOW_UP_S),
                      args=[agent_id], id=f"{_agent_job(agent_id)}-follow-up", replace_existing=True)


async def _follow_up_job(agent_id: int) -> None:
    if is_checking(agent_id):
        return
    with timeline.fresh(actor="scheduler"):
        await run_agent(agent_id)


def unschedule_agent(agent_id: int) -> None:
    if scheduler.get_job(_agent_job(agent_id)):
        scheduler.remove_job(_agent_job(agent_id))


async def _session_check() -> None:
    sess = get_session()
    if sess.started and not quiet_hours.holds(sess.last_check):
        try:
            with timeline.fresh(actor="scheduler"):
                await sess.check_login()
        except Exception as e:  # noqa: BLE001
            log.warning("session check failed: %s", e)


async def _native_keep() -> None:
    from app.willhaben import native

    with timeline.fresh(actor="scheduler"):
        await native.keep_job()


_described = [False]


async def _park_idle_ui() -> None:
    sess = get_session()
    if sess.started:
        if not _described[0] and hasattr(sess, "describe"):     # once after the start: where the processes come from
            _described[0] = True
            log.info("browser after start: %s", sess.describe())
        try:
            await sess.park_idle_ui()
        except Exception as e:  # noqa: BLE001
            log.warning("parking the idle browser page failed: %s", e)


LIVE_FALLBACK = timedelta(minutes=15)


async def _chat_sync() -> None:
    # The live WebSocket triggers syncs as messages arrive; polling is only the safety net then.
    last = chats.last_sync_at[0]
    if get_session().chat_live_ok() and last and utcnow() - last < LIVE_FALLBACK:
        return
    if quiet_hours.holds(last):     # no polling at night; messages the live socket brings still sync
        return
    try:
        with timeline.fresh(actor="scheduler"):
            async with timeline.run("sched", t("Chat sync"), name="chat-sync"):
                await chats.sync()
    except Exception as e:  # noqa: BLE001 - keep the job alive
        log.warning("chat sync failed: %s", e)


async def _myads_check() -> None:
    with timeline.fresh(actor="scheduler"):
        await my_ads.scheduled_update()


def start() -> None:
    s = get_settings()
    with session() as db:
        agents = list(db.exec(select(SearchAgent)))
    for a in agents:
        schedule_agent(a, run_soon=False)
    scheduler.add_job(_session_check, IntervalTrigger(seconds=s.session_check_interval_s), id="session-check",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(_chat_sync, IntervalTrigger(seconds=s.chat_poll_interval_s, jitter=30), id="chat-sync",
                      replace_existing=True, max_instances=1, coalesce=True,
                      next_run_time=datetime.now().astimezone() + timedelta(seconds=30))
    if not s.wh_replay:
        scheduler.add_job(_native_keep, IntervalTrigger(minutes=30, jitter=120), id="willhaben-native",
                          replace_existing=True, max_instances=1, coalesce=True,
                          next_run_time=datetime.now().astimezone() + timedelta(seconds=90))
    scheduler.add_job(health.check, IntervalTrigger(seconds=health.CHECK_INTERVAL_S), id="health-check",
                      replace_existing=True, max_instances=1, coalesce=True,
                      next_run_time=datetime.now().astimezone() + timedelta(seconds=120))
    scheduler.add_job(agent_events.deliver_due, IntervalTrigger(seconds=30), id="agent-webhook",
                      replace_existing=True, max_instances=1, coalesce=True)   # AI agent webhook: retries
    scheduler.add_job(_park_idle_ui, IntervalTrigger(minutes=5), id="park-idle-ui",
                      replace_existing=True, max_instances=1, coalesce=True)   # idle CPU: see session.park_idle_ui
    scheduler.add_job(access_log.flush_job, IntervalTrigger(seconds=access_log.FLUSH_INTERVAL_S), id="access-log",
                      replace_existing=True, max_instances=1, coalesce=True)   # write + prune willhaben access counts
    scheduler.add_job(_myads_check, CronTrigger(hour=s.myads_check_hour, minute=7), id="myads-check",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(timeline.prune_job, IntervalTrigger(hours=1, jitter=120), id="timeline-prune",
                      replace_existing=True, max_instances=1, coalesce=True,   # only when switched on in Settings
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=10))
    scheduler.add_job(geizhals.prune_job, IntervalTrigger(hours=6, jitter=300), id="geizhals-cache-prune",
                      replace_existing=True, max_instances=1, coalesce=True,   # cached answers unused for a week
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=15))
    scheduler.add_job(retention_job, IntervalTrigger(hours=6, jitter=300), id="hits-retention",
                      replace_existing=True, max_instances=1, coalesce=True,   # Hits: retention, saved ones kept
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=20))
    scheduler.add_job(site_accounts.check_job, IntervalTrigger(hours=4, jitter=900), id="site-accounts",
                      replace_existing=True, max_instances=1, coalesce=True,   # Kleinanzeigen, Vinted: still logged in?
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=40))
    scheduler.add_job(watch_saved_job, IntervalTrigger(hours=2, jitter=600), id="hits-watch-saved",
                      replace_existing=True, max_instances=1, coalesce=True,   # saved hits: price drops, sold, gone
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=30))
    from app.services import my_ads as my_ads_svc
    scheduler.add_job(my_ads_svc.review_check_job, IntervalTrigger(minutes=10, jitter=60), id="new-ads-review",
                      replace_existing=True, max_instances=1, coalesce=True)   # new ads: online yet?
    from app.services import crosspost
    scheduler.add_job(crosspost.scheduled_sync, IntervalTrigger(hours=1, jitter=300), id="vinted-sync",
                      replace_existing=True, max_instances=1, coalesce=True)   # the ads also on Vinted: in step
    from app.services import chat_done
    scheduler.add_job(chat_done.check_job, IntervalTrigger(minutes=30, jitter=300), id="chat-done",
                      replace_existing=True, max_instances=1, coalesce=True,   # chats Claude judges over
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=20))
    from app.services import digest
    scheduler.add_job(digest.weekly_job, CronTrigger(day_of_week="mon", hour=8, minute=0), id="weekly-digest",
                      replace_existing=True, max_instances=1, coalesce=True)    # the week in a few lines
    from app.services import photo_match
    scheduler.add_job(photo_match.photo_job, IntervalTrigger(hours=1, jitter=600), id="hits-photos",
                      replace_existing=True, max_instances=1, coalesce=True,   # the same photo in another ad
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=50))
    scheduler.add_job(status_sweep_job, IntervalTrigger(hours=1, jitter=600), id="hits-status-sweep",
                      replace_existing=True, max_instances=1, coalesce=True,   # recent hits: sold or gone (sold prices)
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=45))
    scheduler.add_job(locate_job, IntervalTrigger(hours=1, jitter=300), id="hits-locate",
                      replace_existing=True, max_instances=1, coalesce=True,   # hit cards: distance from home
                      next_run_time=datetime.now().astimezone() + timedelta(minutes=25))
    scheduler.add_job(vpn_check.tick, IntervalTrigger(minutes=1), id="vpn-search",    # the VPN's schedule, stored
                      replace_existing=True, max_instances=1, coalesce=True,
                      next_run_time=datetime.now().astimezone() + timedelta(seconds=90))
    scheduler.start()
    # Stagger the first agent runs so a restart does not burst requests.
    for i, a in enumerate(x for x in agents if x.enabled):
        job = scheduler.get_job(_agent_job(a.id))
        if job:
            job.modify(next_run_time=datetime.now().astimezone() + timedelta(seconds=15 + i * 20))


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def next_run(agent_id: int):
    job = scheduler.get_job(_agent_job(agent_id))
    return job.next_run_time if job else None
