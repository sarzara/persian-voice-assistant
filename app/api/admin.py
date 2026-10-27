"""Admin endpoints (X-Admin-Key). Read models only; the one write besides contacts is retry."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Admin, Paging, db_session
from app.api.schemas import ContactIn, ContactOut, EmailOut, EventOut, Page, RetryOut, RunDetail, RunSummary, ToolCallOut
from app.config import get_settings
from app.db.models import AgentRun, CalendarEvent, Contact, Email, RunStatus, ToolCall, User, VoiceMessage
from app.db.repo import audit

router = APIRouter(dependencies=[Admin], tags=["admin"])

RunStatusQ = Literal["running", "needs_clarification", "pending_confirmation", "confirmed", "executed", "rejected", "failed"]
IntentQ = Literal["schedule", "email", "unknown"]


async def _page(session: AsyncSession, stmt: Select, paging: Paging, model) -> Page:
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.limit(paging.limit).offset(paging.offset))).scalars().all()
    return Page(items=[model.model_validate(r) for r in rows], total=total or 0, limit=paging.limit, offset=paging.offset)


@router.get("/runs", response_model=Page[RunSummary])
async def list_runs(
    status_: RunStatusQ | None = Query(None, alias="status"),
    intent: IntentQ | None = None,
    model: str | None = Query(None, max_length=100),
    user_id: int | None = None,
    since: dt.datetime | None = Query(None, description="ISO datetime, inclusive"),
    until: dt.datetime | None = Query(None, description="ISO datetime, exclusive"),
    paging: Paging = Depends(),
    session: AsyncSession = Depends(db_session),
):
    if since and until and since >= until:
        raise HTTPException(422, "'since' must be before 'until'")
    stmt = select(AgentRun).order_by(AgentRun.id.desc())
    if status_:
        stmt = stmt.where(AgentRun.status == status_)
    if intent:
        stmt = stmt.where(AgentRun.intent == intent)
    if model:
        stmt = stmt.where(AgentRun.model == model)
    if user_id is not None:
        stmt = stmt.where(AgentRun.user_id == user_id)
    if since:
        stmt = stmt.where(AgentRun.created_at >= since)
    if until:
        stmt = stmt.where(AgentRun.created_at < until)
    return await _page(session, stmt, paging, RunSummary)


async def run_detail(session: AsyncSession, run_id: int) -> RunDetail:
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    detail = RunDetail.model_validate(run)
    if run.voice_message_id:
        vm = await session.get(VoiceMessage, run.voice_message_id)
        if vm:  # transcript only: the audio path is never exposed
            detail.source, detail.transcript, detail.stt_confidence = vm.source, vm.transcript, vm.stt_confidence
    calls = (await session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id))).scalars()
    detail.tool_calls = [ToolCallOut.model_validate(c) for c in calls]
    return detail


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: int, session: AsyncSession = Depends(db_session)):
    return await run_detail(session, run_id)


@router.post("/runs/{run_id}/retry", response_model=RetryOut)
async def retry_run(run_id: int, session: AsyncSession = Depends(db_session)):
    """Failed execution -> run the already-confirmed action again (idempotent).
    Failed planning -> re-run the agent on the same message; the new preview goes to Telegram for confirmation."""
    from app.agent import pipeline
    from app.agent.executor import ExecStatus, RetryNotAllowed, retry_failed_execution
    from app.bot import notify, preview

    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    if run.status != RunStatus.FAILED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, f"only failed runs can be retried (run is {run.status!r})")
    await session.commit()  # release the read transaction before long work

    if run.intent in ("schedule", "email") and run.action_json:
        try:
            result = await retry_failed_execution(run_id, actor="api:admin", calendar=pipeline.get_calendar(),
                                                  mailer=pipeline.get_mailer())
        except RetryNotAllowed as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        user = await session.get(User, run.user_id)
        notified = False
        if result.status is ExecStatus.EXECUTED:
            notified = await notify.send_text(user.telegram_user_id, preview.render_executed(result.action, result.data))
            await pipeline.after_execution(run_id)
        return RetryOut(run_id=run_id, kind="execution", outcome=result.status.value, notified=notified,
                        detail=result.error)

    if not run.voice_message_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "run has no message to re-plan")
    vm = await session.get(VoiceMessage, run.voice_message_id)
    if not vm or not vm.transcript:
        raise HTTPException(status.HTTP_409_CONFLICT, "message has no transcript to re-plan")
    user = await session.get(User, run.user_id)
    await audit(session, "run.retry", "api:admin", run_id=run_id, kind="agent")
    await session.commit()
    try:
        outcome = await pipeline.process_message(user, vm.id, vm.transcript)
    except ValueError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "LLM provider not configured") from exc
    notified = await notify.send_outcome(user.telegram_user_id, outcome) if outcome.kind != "failed" else False
    return RetryOut(run_id=run_id, kind="agent", outcome=outcome.kind if outcome.kind != "failed" else "failed",
                    new_run_id=outcome.run_id, notified=notified, detail=outcome.error)


@router.get("/events", response_model=Page[EventOut])
async def list_events(
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    paging: Paging = Depends(),
    session: AsyncSession = Depends(db_session),
):
    stmt = select(CalendarEvent).order_by(CalendarEvent.date.desc(), CalendarEvent.time.desc())
    if date_from:
        stmt = stmt.where(CalendarEvent.date >= date_from)
    if date_to:
        stmt = stmt.where(CalendarEvent.date <= date_to)
    return await _page(session, stmt, paging, EventOut)


@router.get("/emails", response_model=Page[EmailOut])
async def list_emails(
    status_: Literal["queued", "sent", "failed"] | None = Query(None, alias="status"),
    to: str | None = Query(None, max_length=320),
    paging: Paging = Depends(),
    session: AsyncSession = Depends(db_session),
):
    stmt = select(Email).order_by(Email.id.desc())
    if status_:
        stmt = stmt.where(Email.status == status_)
    if to:
        stmt = stmt.where(Email.to_address == to.strip().lower())
    return await _page(session, stmt, paging, EmailOut)


async def _owner(session: AsyncSession, user_id: int | None) -> User:
    if user_id is not None:
        user = await session.get(User, user_id)
    else:
        owner_tg = get_settings().owner_telegram_id
        user = (await session.execute(select(User).where(User.telegram_user_id == owner_tg))).scalar_one_or_none() \
            if owner_tg is not None else None
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found (run the seed, or pass user_id)")
    return user


@router.get("/contacts", response_model=Page[ContactOut])
async def list_contacts(
    q: str | None = Query(None, max_length=100, description="substring of name, alias or email"),
    user_id: int | None = None,
    paging: Paging = Depends(),
    session: AsyncSession = Depends(db_session),
):
    owner = await _owner(session, user_id)
    stmt = select(Contact).where(Contact.user_id == owner.id).order_by(Contact.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Contact.name.ilike(like), Contact.email.ilike(like),
                              func.array_to_string(Contact.aliases, " ").ilike(like)))
    return await _page(session, stmt, paging, ContactOut)


@router.post("/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(body: ContactIn, user_id: int | None = None, session: AsyncSession = Depends(db_session)):
    owner = await _owner(session, user_id)
    aliases = list(dict.fromkeys([body.name, *body.aliases]))
    stmt = (insert(Contact).values(user_id=owner.id, name=body.name, email=body.email, aliases=aliases)
            .on_conflict_do_nothing(constraint="uq_contacts_user_id_name").returning(Contact.id))
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"contact {body.name!r} already exists")
    await audit(session, "contact.created", "api:admin", contact_id=new_id)
    return ContactOut.model_validate(await session.get(Contact, new_id))
