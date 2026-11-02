"""Token usage of every Claude call, per purpose (chat, ads, screen, …) and model.

`claude -p --output-format json` reports the exact usage of a call: `modelUsage` per model (a call can use more than
one model internally) and an overall `usage` block. One LLMCall row is stored per model and call; failed calls are
stored too (without tokens), so the dashboard can show error counts. Recording never breaks the call itself.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlmodel import col, func, select

from app.db import LLMCall, get_setting, session, set_setting, utcnow

log = logging.getLogger(__name__)
_tasks: set = set()   # notify tasks still running (asyncio.Task)


def _rows_from(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Per-model token counts from the CLI result (camelCase modelUsage, else the snake_case usage block)."""
    data = data or {}
    out = []
    for model, u in (data.get("modelUsage") or {}).items():
        if isinstance(u, dict):
            out.append({"model": model, "input": int(u.get("inputTokens") or 0), "output": int(u.get("outputTokens") or 0),
                        "cache_read": int(u.get("cacheReadInputTokens") or 0),
                        "cache_write": int(u.get("cacheCreationInputTokens") or 0),
                        "cost_usd": float(u.get("costUSD") or 0)})
    if not out and isinstance(data.get("usage"), dict):
        u = data["usage"]
        out.append({"model": None, "input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                    "cache_read": int(u.get("cache_read_input_tokens") or 0),
                    "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(data.get("total_cost_usd") or 0)})
    return out


def record(purpose: str | None, model: str, data: dict[str, Any] | None, *, ok: bool,
           duration_ms: int | None = None, error: str | None = None) -> None:
    try:
        rows = _rows_from(data) if ok else []
        with session() as db:
            if not rows:
                db.add(LLMCall(purpose=purpose or "other", model=model, ok=ok, duration_ms=duration_ms,
                               error=(error or "")[:300] or None))
            for i, r in enumerate(rows):
                db.add(LLMCall(purpose=purpose or "other", model=r["model"] or model, ok=ok,
                               duration_ms=duration_ms if i == 0 else None, input_tokens=r["input"],
                               output_tokens=r["output"], cache_read_tokens=r["cache_read"],
                               cache_write_tokens=r["cache_write"], call_part=i,
                               cost_usd=_cost(r["model"] or model, r)))
            db.commit()
        if rows:
            _limit_reached_push()
    except Exception as e:  # noqa: BLE001 - statistics must never break a Claude call
        log.warning("could not record LLM usage: %s", e)


def _cost(model: str | None, r: dict[str, Any]) -> float:
    """The call's price from its tokens and the API list prices (llm.base.cost_usd), else the CLI's costUSD."""
    from app.llm.base import cost_usd
    mine = cost_usd(model, r["input"], r["output"], r["cache_read"], r["cache_write"])
    return mine if mine is not None else r["cost_usd"]


SETTING_REPRICED = "llm_costs_repriced"     # reprice_calls() ran


def reprice_calls() -> int:
    """Once, at startup: the stored calls priced from their tokens (the CLI's costUSD was off for Haiku 5.5 and for
    Sonnet 5.5's cache reads). Returns how many rows changed."""
    from app.llm.base import cost_usd
    if get_setting(SETTING_REPRICED):
        return 0
    changed = 0
    with session() as db:
        for row in db.exec(select(LLMCall).where(LLMCall.ok == True)):  # noqa: E712
            c = cost_usd(row.model, row.input_tokens or 0, row.output_tokens or 0, row.cache_read_tokens or 0,
                         row.cache_write_tokens or 0)
            if c is not None and abs(c - (row.cost_usd or 0)) > 1e-9:
                row.cost_usd = c
                db.add(row)
                changed += 1
        db.commit()
    set_setting(SETTING_REPRICED, "1")
    return changed


LIMIT_MODES = ("pause", "haiku", "notify")   # over the limit: pause hit screening / everything on Haiku / only a push
HAIKU = "claude-haiku-5-5"      # fallback when the fetched model list has no Haiku line (see haiku())


def haiku() -> str:
    """The model everything runs on over the limit in "haiku" mode: the newest Haiku of the fetched list."""
    from app.llm import models

    return models.newest("haiku") or HAIKU


PERIOD_SETTING = "claude_limit_days"     # the token limit's period in days (Settings → Claude); rolling, default a week
DEFAULT_DAYS, MAX_DAYS = 7, 90


def period_days() -> int:
    try:
        return min(max(int(get_setting(PERIOD_SETTING) or DEFAULT_DAYS), 1), MAX_DAYS)
    except ValueError:
        return DEFAULT_DAYS


def period_start():
    """Start of the token limit's period: the last N days (rolling; Settings → Claude, a week by default)."""
    return utcnow() - timedelta(days=period_days())


month_start = period_start    # the per-task, per-model and cost totals below count over the same period


def month_tokens() -> int:
    """Input + output tokens of all Claude calls in the limit's period (period_start)."""
    with session() as db:
        return int(db.exec(select(func.coalesce(func.sum(col(LLMCall.input_tokens) + col(LLMCall.output_tokens)), 0))
                           .where(LLMCall.ts >= month_start())).one() or 0)


def limit() -> dict[str, Any]:
    """The token limit of the period (Settings → Claude): {"used", "limit", "mode", "over", "days"}; limit 0 = none."""
    try:
        lim = int(float(get_setting("claude_token_limit_m") or 0) * 1_000_000)
    except ValueError:
        lim = 0
    mode = get_setting("claude_limit_mode") or "notify"
    used = month_tokens()
    return {"used": used, "limit": lim, "mode": mode if mode in LIMIT_MODES else "notify", "over": bool(lim) and used >= lim,
            "days": period_days()}


TASK_COLORS = {"chat": "var(--brand)", "ads": "#8b5cf6", "screen": "#eb6834", "search": "#1baf7a", "shipping": "#3b82f6",
               "purchase": "#db4b9a", "style": "#d9a21b"}


def month_by_task() -> list[dict[str, Any]]:
    """This month's tokens per task for the Claude popover: [{label, tokens, color}], biggest first."""
    from app.i18n import t
    from app.llm.base import PURPOSES

    with session() as db:
        rows = db.exec(select(LLMCall.purpose, func.coalesce(func.sum(col(LLMCall.input_tokens) + col(LLMCall.output_tokens)), 0))
                       .where(LLMCall.ts >= month_start()).group_by(LLMCall.purpose)).all()
    out = [{"label": t(PURPOSES[p][1]) if p in PURPOSES else t("Other (tests, misc.)"), "tokens": int(n or 0),
            "color": TASK_COLORS.get(p, "var(--muted)")} for p, n in rows if n]
    return sorted(out, key=lambda r: -r["tokens"])


MODEL_COLORS = {"opus": "#6366f1", "fable": "#d946ef", "sonnet": "#0ea5e9", "haiku": "#22c55e"}   # per line


def month_by_model() -> list[dict[str, Any]]:
    """This month's tokens per model for the Claude popover: [{label, model, tokens, color}], biggest first.
    Names come from the fetched model list (app/llm/models.py), the colour from the model's line."""
    from app.llm import models

    with session() as db:
        rows = db.exec(select(LLMCall.model, func.coalesce(func.sum(col(LLMCall.input_tokens) + col(LLMCall.output_tokens)), 0))
                       .where(LLMCall.ts >= month_start()).group_by(LLMCall.model)).all()
    out = [{"label": models.label(m), "model": m, "tokens": int(n or 0),
            "color": MODEL_COLORS.get(models.line_of(m), "var(--muted)")} for m, n in rows if n]
    return sorted(out, key=lambda r: -r["tokens"])


def month_cost_by_model() -> list[dict[str, Any]]:
    """This month's API price per model (from the tokens and the API list prices, what the usage would cost pay-as-you-go) for the
    Claude popover: [{label, model, cost_usd, color}], biggest first."""
    from app.llm import models

    with session() as db:
        rows = db.exec(select(LLMCall.model, func.coalesce(func.sum(col(LLMCall.cost_usd)), 0.0))
                       .where(LLMCall.ts >= month_start()).group_by(LLMCall.model)).all()
    out = [{"label": models.label(m), "model": m, "cost_usd": float(c or 0),
            "color": MODEL_COLORS.get(models.line_of(m), "var(--muted)")} for m, c in rows if c]
    return sorted(out, key=lambda r: -r["cost_usd"])


def _limit_reached_push() -> None:
    """Once per period: a push when the token limit is reached (whatever the mode)."""
    import asyncio
    from datetime import datetime

    st = limit()
    last = get_setting("claude_limit_notified") or ""
    try:
        recent = bool(last) and datetime.fromisoformat(last) >= period_start()
    except ValueError:                  # a month ("2026-10") from before the period could be chosen
        recent = False
    if not st["over"] or recent:
        return
    set_setting("claude_limit_notified", utcnow().isoformat())
    from app.i18n import t
    from app.services.notify import notify

    text = {"pause": t("Hit screening is paused until usage drops below the limit."),
            "haiku": t("All tasks use Haiku until usage drops below the limit."),
            "notify": t("Nothing is throttled.")}[st["mode"]]
    try:
        task = asyncio.get_running_loop().create_task(notify(
            "health", t("Claude token limit reached"), t("{used} M of {limit} M tokens in the last {n} days.",
                                                         used=f"{st['used'] / 1e6:.1f}", limit=f"{st['limit'] / 1e6:g}",
                                                         n=st["days"]) + " " + text, url="/settings/claude"))
        _tasks.add(task)                  # a reference until it ran: an unreferenced task can be collected unsent
        task.add_done_callback(_tasks.discard)
    except RuntimeError:
        pass    # no event loop (scripts, tests): the dashboard meter still shows it


def summary() -> dict[str, Any]:
    """Totals per (purpose, model) for today, the last 7 days and overall, plus a grand total."""
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = {"calls": 0, "errors": 0, "today": 0, "week": 0, "all": 0, "cache_read": 0, "cost_usd": 0.0}
    with session() as db:
        rows = list(db.exec(select(LLMCall)))
    for r in rows:
        g = groups.setdefault((r.purpose, r.model), {"purpose": r.purpose, "model": r.model, "calls": 0, "errors": 0,
                                                     "today": 0, "week": 0, "all": 0, "input": 0, "output": 0,
                                                     "cache_read": 0, "cost_usd": 0.0, "last": None})
        ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=now.tzinfo)
        if not r.call_part:                     # count each call once, even when it used several models
            g["calls"] += 1
            total["calls"] += 1
            if not r.ok:
                g["errors"] += 1
                total["errors"] += 1
        tokens = (r.input_tokens or 0) + (r.output_tokens or 0)
        g["input"] += r.input_tokens or 0
        g["output"] += r.output_tokens or 0
        g["cache_read"] += r.cache_read_tokens or 0
        g["cost_usd"] += r.cost_usd or 0
        g["all"] += tokens
        total["all"] += tokens
        total["cache_read"] += r.cache_read_tokens or 0
        total["cost_usd"] += r.cost_usd or 0
        if ts >= week:
            g["week"] += tokens
            total["week"] += tokens
        if ts >= today:
            g["today"] += tokens
            total["today"] += tokens
        g["last"] = max(g["last"], ts) if g["last"] else ts
    order = {"chat": 0, "ads": 1, "screen": 2}
    return {"groups": sorted(groups.values(), key=lambda g: (order.get(g["purpose"], 9), g["purpose"], -g["all"])),
            "total": total}
