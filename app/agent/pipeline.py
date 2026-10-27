"""Glue between the bot and the agent: one owner message in, one AgentOutcome out."""

from __future__ import annotations

import logging
from functools import lru_cache

from app.agent.agent import Agent, AgentOutcome, now_in
from app.agent.context import AgentContext
from app.agent.provider import provider_from_settings
from app.config import get_settings
from app.db.models import MessageSource, User, VoiceMessage
from app.db.session import session_scope
from app.tools.google_calendar import GoogleCalendarClient
from app.tools.resend_client import ResendClient

log = logging.getLogger(__name__)


@lru_cache
def get_calendar() -> GoogleCalendarClient | None:
    settings = get_settings()
    if not settings.google_calendar_configured:
        log.warning("Google Calendar is not configured; scheduling will fail at execution time")
        return None
    return GoogleCalendarClient.from_settings(settings)


@lru_cache
def get_mailer() -> ResendClient | None:
    try:
        return ResendClient.from_settings()
    except ValueError as exc:
        log.warning("Email is not configured: %s", exc)
        return None


async def take_pending_edit(user: User) -> dict | None:
    """If the owner tapped Edit on a recent preview, return that action once (then mark it consumed)."""
    from sqlalchemy import func, select

    from app.db.models import AgentRun, RunStatus

    ttl = get_settings().confirmation_ttl_min
    async with session_scope() as s:
        run = (await s.execute(
            select(AgentRun)
            .where(AgentRun.user_id == user.id, AgentRun.status == RunStatus.REJECTED.value,
                   AgentRun.error == "edit_requested",
                   AgentRun.updated_at > func.now() - func.make_interval(0, 0, 0, 0, 0, ttl))
            .order_by(AgentRun.id.desc()).limit(1).with_for_update()
        )).scalar_one_or_none()
        if run is None:
            return None
        run.error = "edit_consumed"
        return run.action_json


@lru_cache
def get_agent() -> Agent:
    return Agent(provider_from_settings(), calendar=get_calendar())


async def store_text_message(user: User, text: str) -> int:
    async with session_scope() as s:
        vm = VoiceMessage(user_id=user.id, source=MessageSource.TEXT.value, transcript=text, stt_confidence=1.0)
        s.add(vm)
        await s.flush()
        return vm.id


async def build_context(user: User, transcript: str, *, editing: dict | None = None) -> AgentContext:
    return AgentContext(now=now_in(user.timezone or get_settings().default_timezone),
                        owner_name=user.name, editing=editing)


async def process_message(user: User, message_id: int, transcript: str, *, editing: dict | None = None,
                          agent: Agent | None = None) -> AgentOutcome:
    if editing is None:
        editing = await take_pending_edit(user)
    context = await build_context(user, transcript, editing=editing)
    return await (agent or get_agent()).run(user=user, message_id=message_id, transcript=transcript, context=context)


async def after_execution(run_id: int) -> None:
    """Hook for post-execution memory updates (see memory batch)."""
    return None
