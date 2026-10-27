"""Runs a confirmed action exactly once.

Three independent layers make a double tap (or a retry after a crash) harmless:
1. Compare-and-set on agent_runs.status (pending_confirmation -> confirmed) in a
   single UPDATE ... RETURNING: only one caller can win, even concurrently.
2. A tool_calls row with the unique key ``run-<id>-execute`` is committed
   *before* the provider is called.
3. The same key goes to the provider: a deterministic Google event id (409 on
   replay) and Resend's Idempotency-Key header.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.agent.actions import EmailAction, ScheduleAction, parse_action
from app.config import get_settings
from app.logging_setup import set_run_id
from app.db.models import AgentRun, RunStatus, ToolCall, ToolCallStatus, User
from app.db.repo import audit
from app.db.session import session_scope
from app.tools.registry import CalendarBackend, Exposure, MailBackend, ToolContext, invoke

log = logging.getLogger(__name__)


class ExecStatus(str, enum.Enum):
    EXECUTED = "executed"
    ALREADY_DONE = "already_done"   # a previous tap already ran (or is running) it
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass
class ExecResult:
    status: ExecStatus
    action: ScheduleAction | EmailAction | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def execution_key(run_id: int) -> str:
    return f"run-{run_id}-execute"


async def _owned_run(session, run_id: int, telegram_user_id: int) -> tuple[AgentRun | None, User | None]:
    row = (await session.execute(
        select(AgentRun, User).join(User, AgentRun.user_id == User.id)
        .where(AgentRun.id == run_id, User.telegram_user_id == telegram_user_id)
    )).first()
    return (row[0], row[1]) if row else (None, None)


async def confirm_and_execute(
    run_id: int,
    telegram_user_id: int,
    *,
    calendar: CalendarBackend | None,
    mailer: MailBackend | None,
    now: dt.datetime | None = None,
) -> ExecResult:
    settings = get_settings()
    ttl = dt.timedelta(minutes=settings.confirmation_ttl_min)
    set_run_id(run_id)

    # 1) Compare-and-set: exactly one caller moves the run to "confirmed".
    async with session_scope() as s:
        run, user = await _owned_run(s, run_id, telegram_user_id)
        if run is None:
            return ExecResult(ExecStatus.NOT_FOUND)
        won = (await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.PENDING_CONFIRMATION.value,
                   AgentRun.created_at > func.now() - ttl)
            .values(status=RunStatus.CONFIRMED.value)
            .returning(AgentRun.id)
        )).scalar_one_or_none()
        if won is None:
            await s.refresh(run)
            if run.status in (RunStatus.CONFIRMED.value, RunStatus.EXECUTED.value):
                return ExecResult(ExecStatus.ALREADY_DONE)
            if run.status == RunStatus.REJECTED.value:
                return ExecResult(ExecStatus.CANCELLED)
            if run.status == RunStatus.PENDING_CONFIRMATION.value:
                run.status = RunStatus.REJECTED.value
                run.error = "expired"
                return ExecResult(ExecStatus.EXPIRED)
            return ExecResult(ExecStatus.NOT_FOUND)
        action = parse_action(run.action_json)
        await audit(s, "run.confirmed", f"telegram:{telegram_user_id}", run_id=run_id)

    return await _execute(run_id, user.id, action, row_key=execution_key(run_id),
                          calendar=calendar, mailer=mailer, now=now)


async def _execute(run_id: int, user_id: int, action: ScheduleAction | EmailAction, *, row_key: str,
                   calendar: CalendarBackend | None, mailer: MailBackend | None,
                   now: dt.datetime | None) -> ExecResult:
    """Steps 2 and 3. ``row_key`` names this attempt; the provider always gets execution_key(run_id),
    so a retry of an attempt that secretly succeeded is deduplicated by Google/Resend."""
    settings = get_settings()
    tool_name = "create_calendar_event" if isinstance(action, ScheduleAction) else "send_email"

    # 2) Reserve the idempotency key before touching any provider.
    try:
        async with session_scope() as s:
            s.add(ToolCall(run_id=run_id, tool_name=tool_name, arguments_json=action.tool_args(),
                           status=ToolCallStatus.PENDING.value, idempotency_key=row_key))
    except IntegrityError:
        return ExecResult(ExecStatus.ALREADY_DONE, action)

    # 3) Execute with the run's key forwarded to the provider.
    async with session_scope() as s:
        db_user = await s.get(User, user_id)
        row = (await s.execute(select(ToolCall).where(ToolCall.idempotency_key == row_key))).scalar_one()
        ctx = ToolContext(session=s, user=db_user, settings=settings,
                          now=now or dt.datetime.now(dt.timezone.utc), calendar=calendar, mailer=mailer,
                          run_id=run_id, idempotency_key=execution_key(run_id))
        result = await invoke(tool_name, action.tool_args(), ctx, allowed={Exposure.ACTION}, log_row=row)
        run = await s.get(AgentRun, run_id)
        run.status = (RunStatus.EXECUTED if result.ok else RunStatus.FAILED).value
        run.error = None if result.ok else result.error
        await audit(s, "run.executed" if result.ok else "run.failed", "system", run_id=run_id, tool=tool_name)

    if not result.ok:
        log.error("Run %s execution failed: %s", run_id, result.error)
        return ExecResult(ExecStatus.FAILED, action, error=result.error)
    return ExecResult(ExecStatus.EXECUTED, action, data=result.data)


class RetryNotAllowed(Exception):
    pass


async def retry_failed_execution(run_id: int, *, actor: str, calendar: CalendarBackend | None,
                                 mailer: MailBackend | None, now: dt.datetime | None = None) -> ExecResult:
    """Re-run a confirmed action whose execution failed. The owner already confirmed it once."""
    set_run_id(run_id)
    async with session_scope() as s:
        run = await s.get(AgentRun, run_id)
        if run is None:
            return ExecResult(ExecStatus.NOT_FOUND)
        if run.status != RunStatus.FAILED.value or run.intent not in ("schedule", "email") or not run.action_json:
            raise RetryNotAllowed(f"run {run_id} is {run.status!r}, not a failed execution")
        action = parse_action(run.action_json)
        if isinstance(action, ScheduleAction):
            user = await s.get(User, run.user_id)
            from zoneinfo import ZoneInfo

            start = dt.datetime.combine(action.date, action.time, ZoneInfo(user.timezone))
            if start < (now or dt.datetime.now(dt.timezone.utc)):
                raise RetryNotAllowed("the event time has already passed")
        won = (await s.execute(
            update(AgentRun).where(AgentRun.id == run_id, AgentRun.status == RunStatus.FAILED.value)
            .values(status=RunStatus.CONFIRMED.value).returning(AgentRun.id)
        )).scalar_one_or_none()
        if won is None:
            return ExecResult(ExecStatus.ALREADY_DONE, action)
        attempts = await s.scalar(select(func.count()).select_from(ToolCall).where(
            ToolCall.run_id == run_id, ToolCall.idempotency_key.like(f"{execution_key(run_id)}%")))
        await audit(s, "run.retry", actor, run_id=run_id, attempt=attempts + 1)
        user_id = run.user_id
    return await _execute(run_id, user_id, action, row_key=f"{execution_key(run_id)}-retry{attempts}",
                          calendar=calendar, mailer=mailer, now=now)


async def reject_run(run_id: int, telegram_user_id: int, reason: str) -> ExecResult:
    """Cancel, or mark for editing (reason='edit_requested'). Only pending runs can be rejected."""
    async with session_scope() as s:
        run, _ = await _owned_run(s, run_id, telegram_user_id)
        if run is None:
            return ExecResult(ExecStatus.NOT_FOUND)
        changed = (await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == RunStatus.PENDING_CONFIRMATION.value)
            .values(status=RunStatus.REJECTED.value, error=reason)
            .returning(AgentRun.id)
        )).scalar_one_or_none()
        if changed is None:
            await s.refresh(run)
            return ExecResult(ExecStatus.ALREADY_DONE if run.status in ("confirmed", "executed") else ExecStatus.CANCELLED)
        await audit(s, f"run.{reason}", f"telegram:{telegram_user_id}", run_id=run_id)
        return ExecResult(ExecStatus.CANCELLED, parse_action(run.action_json))
