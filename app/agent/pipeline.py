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

log = logging.getLogger(__name__)


@lru_cache
def get_calendar() -> GoogleCalendarClient | None:
    settings = get_settings()
    if not settings.google_calendar_configured:
        log.warning("Google Calendar is not configured; scheduling will fail at execution time")
        return None
    return GoogleCalendarClient.from_settings(settings)


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
    context = await build_context(user, transcript, editing=editing)
    return await (agent or get_agent()).run(user=user, message_id=message_id, transcript=transcript, context=context)
