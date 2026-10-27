"""Aggregation queries behind the dashboard. Read-only, parameterised SQL.

Definitions (also shown in the UI):
- Tokens: prompt + completion tokens of llm_calls.
- Latency P50/P95: per LLM call (status ok), milliseconds.
- Success rate: finished agent runs that did not fail (planning or execution) / finished runs.
- Confirmation rate: proposals the owner confirmed / proposals the owner decided on
  (confirmed, edited, cancelled, expired).
- Edit rate: edited / decided proposals.
- JSON-valid rate: 1 - JSON/schema failures of final answers / all final-answer calls.
- Thumbs-up share: 👍 / all rated runs.
MCP-originated runs (model 'mcp') are excluded from quality metrics; their writes use no LLM.
Days are calendar days in the owner's timezone.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AGENT_RUN = "model IS DISTINCT FROM 'mcp' AND status <> 'running'"


def day_start(now: dt.datetime, tz: ZoneInfo, days_back: int = 0) -> dt.datetime:
    local_day = now.astimezone(tz).date() - dt.timedelta(days=days_back)
    return dt.datetime.combine(local_day, dt.time.min, tz)


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


async def _one(session: AsyncSession, sql: str, **params) -> dict[str, Any]:
    return dict((await session.execute(text(sql), params)).mappings().one())


async def _all(session: AsyncSession, sql: str, **params) -> list[dict[str, Any]]:
    return [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]


async def kpis(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30) -> dict[str, Any]:
    today, d7, dN = day_start(now, tz), day_start(now, tz, 6), day_start(now, tz, days - 1)
    tok = await _one(session, """
        SELECT
          COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (WHERE created_at >= :today), 0) AS tokens_today,
          COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (WHERE created_at >= :d7), 0)    AS tokens_7d,
          COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (WHERE created_at >= :dn), 0)    AS tokens_window,
          COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :today), 0) AS cost_today,
          COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= :dn), 0)    AS cost_window,
          percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE status = 'ok' AND created_at >= :dn) AS p50,
          percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE status = 'ok' AND created_at >= :dn) AS p95,
          COUNT(*) FILTER (WHERE created_at >= :dn) AS calls,
          COUNT(*) FILTER (WHERE created_at >= :dn AND status = 'error') AS errors
        FROM llm_calls WHERE created_at >= LEAST(:dn, :d7)""", today=today, d7=d7, dn=dN)
    q = await _one(session, f"""
        SELECT COUNT(*) AS runs,
               AVG((status <> 'failed')::int) AS success_rate,
               AVG((decision = 'confirmed')::int) FILTER (WHERE decision IN ('confirmed','edited','cancelled','expired')) AS confirmation_rate,
               AVG((decision = 'edited')::int)    FILTER (WHERE decision IN ('confirmed','edited','cancelled','expired')) AS edit_rate
        FROM run_quality WHERE created_at >= :dn AND {AGENT_RUN}""", dn=dN)
    return {
        "tokens_today": int(tok["tokens_today"]), "tokens_7d": int(tok["tokens_7d"]),
        "tokens_window": int(tok["tokens_window"]), "cost_today": _f(tok["cost_today"]),
        "cost_window": _f(tok["cost_window"]), "latency_p50": _f(tok["p50"]), "latency_p95": _f(tok["p95"]),
        "llm_calls": int(tok["calls"]), "llm_errors": int(tok["errors"]), "runs": int(q["runs"]),
        "success_rate": _f(q["success_rate"]), "confirmation_rate": _f(q["confirmation_rate"]),
        "edit_rate": _f(q["edit_rate"]), "days": days,
    }


async def daily_usage(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30) -> list[dict[str, Any]]:
    """One row per (day, model, purpose): tokens and cost."""
    return await _all(session, """
        SELECT (created_at AT TIME ZONE :tz)::date AS day, model, purpose,
               SUM(prompt_tokens + completion_tokens)::bigint AS tokens,
               COALESCE(SUM(cost_usd), 0)::float AS cost
        FROM llm_calls WHERE created_at >= :since
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", tz=str(tz), since=day_start(now, tz, days - 1))


async def daily_latency(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30) -> list[dict[str, Any]]:
    return await _all(session, """
        SELECT (created_at AT TIME ZONE :tz)::date AS day,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::float AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::float AS p95
        FROM llm_calls WHERE created_at >= :since AND status = 'ok'
        GROUP BY 1 ORDER BY 1""", tz=str(tz), since=day_start(now, tz, days - 1))


async def daily_quality(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30) -> list[dict[str, Any]]:
    return await _all(session, f"""
        SELECT (created_at AT TIME ZONE :tz)::date AS day, COUNT(*) AS runs,
               AVG((status <> 'failed')::int)::float AS success_rate,
               (AVG((decision = 'edited')::int) FILTER (WHERE decision IN ('confirmed','edited','cancelled','expired')))::float AS edit_rate
        FROM run_quality WHERE created_at >= :since AND {AGENT_RUN}
        GROUP BY 1 ORDER BY 1""", tz=str(tz), since=day_start(now, tz, days - 1))


async def quality_by_model(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30) -> list[dict[str, Any]]:
    rows = await _all(session, f"""
        SELECT model, COUNT(*) AS runs,
               AVG((status <> 'failed')::int)::float AS success_rate,
               (1 - SUM(json_failures)::float / NULLIF(SUM(terminal_calls), 0)) AS json_valid_rate,
               (AVG((decision = 'edited')::int) FILTER (WHERE decision IN ('confirmed','edited','cancelled','expired')))::float AS edit_rate,
               AVG(steps)::float AS avg_steps,
               AVG(COALESCE(cost_usd, 0))::float AS avg_cost,
               (AVG((rating = 1)::int) FILTER (WHERE rating IS NOT NULL))::float AS thumbs_up,
               COUNT(rating) AS rated,
               AVG(latency_ms)::float AS avg_run_latency
        FROM run_quality WHERE created_at >= :since AND {AGENT_RUN} AND model IS NOT NULL
        GROUP BY model ORDER BY runs DESC""", since=day_start(now, tz, days - 1))
    return rows


async def top_runs(session: AsyncSession, now: dt.datetime, tz: ZoneInfo, days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
    return await _all(session, f"""
        SELECT run_id, created_at, model, intent, status, steps, tokens, cost_usd::float AS cost
        FROM run_quality WHERE created_at >= :since AND {AGENT_RUN} AND cost_usd IS NOT NULL
        ORDER BY cost_usd DESC, run_id DESC LIMIT :limit""", since=day_start(now, tz, days - 1), limit=limit)


@dataclass
class LogFilters:
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    model: str | None = None
    status: str | None = None
    intent: str | None = None

    def where(self, tz: ZoneInfo) -> tuple[str, dict[str, Any]]:
        clauses, params = ["status <> 'running'"], {}
        if self.date_from:
            clauses.append("created_at >= :date_from")
            params["date_from"] = dt.datetime.combine(self.date_from, dt.time.min, tz)
        if self.date_to:
            clauses.append("created_at < :date_to")
            params["date_to"] = dt.datetime.combine(self.date_to + dt.timedelta(days=1), dt.time.min, tz)
        for col in ("model", "status", "intent"):
            if getattr(self, col):
                clauses.append(f"{col} = :{col}")
                params[col] = getattr(self, col)
        return " AND ".join(clauses), params


LOG_COLUMNS = ("run_id", "created_at", "model", "intent", "status", "decision", "steps", "llm_calls", "tokens",
               "cost_usd", "latency_ms", "json_failures", "guardrail_rejections", "tool_failures", "retries",
               "asked_question", "rating", "error")


async def logs(session: AsyncSession, filters: LogFilters, tz: ZoneInfo, *, limit: int | None = 50,
               offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    where, params = filters.where(tz)
    total = (await session.execute(text(f"SELECT COUNT(*) FROM run_quality WHERE {where}"), params)).scalar_one()
    page = "" if limit is None else "LIMIT :limit OFFSET :offset"
    rows = await _all(session, f"SELECT {', '.join(LOG_COLUMNS)} FROM run_quality WHERE {where} "
                               f"ORDER BY created_at DESC, run_id DESC {page}",
                      **params, **({} if limit is None else {"limit": limit, "offset": offset}))
    return rows, int(total)


async def filter_options(session: AsyncSession) -> dict[str, list[str]]:
    models = [r["model"] for r in await _all(session, "SELECT DISTINCT model FROM agent_runs WHERE model IS NOT NULL ORDER BY 1")]
    return {"models": models,
            "statuses": ["needs_clarification", "pending_confirmation", "confirmed", "executed", "rejected", "failed"],
            "intents": ["schedule", "email", "unknown"]}


async def run_detail(session: AsyncSession, run_id: int) -> dict[str, Any] | None:
    q = await _all(session, "SELECT * FROM run_quality WHERE run_id = :r", r=run_id)
    if not q:
        return None
    run = await _one(session, """
        SELECT r.action_json, r.prompt_version, vm.transcript, vm.source, vm.stt_confidence, vm.duration_s
        FROM agent_runs r LEFT JOIN voice_messages vm ON vm.id = r.voice_message_id WHERE r.id = :r""", r=run_id)
    return {
        **q[0], **run,
        "tool_calls": await _all(session, """SELECT tool_name, status, latency_ms, arguments_json, result_json, created_at
                                             FROM tool_calls WHERE run_id = :r ORDER BY id""", r=run_id),
        "llm_calls": await _all(session, """SELECT purpose, model, prompt_tokens, completion_tokens, cached_tokens,
                                                   cost_usd::float AS cost, latency_ms, attempts, status, error, created_at
                                            FROM llm_calls WHERE run_id = :r ORDER BY id""", r=run_id),
        # Full email content is only ever shown here, on the run detail page.
        "emails": await _all(session, "SELECT to_address, subject, body, status FROM emails WHERE run_id = :r", r=run_id),
        "events": await _all(session, """SELECT date, time, duration_min, title, guests, provider_event_id
                                         FROM calendar_events WHERE run_id = :r""", r=run_id),
    }


async def has_demo_data(session: AsyncSession) -> bool:
    return bool((await session.execute(text("SELECT EXISTS (SELECT 1 FROM users WHERE telegram_user_id = -1)"))).scalar())
