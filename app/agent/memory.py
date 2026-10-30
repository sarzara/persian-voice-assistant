"""Memory: what the agent is given per message, and what is learned after an executed action.

Per message the model sees only a bounded slice, never the whole history:
the current time, the owner's preferences (memory_facts), contacts whose
names occur in this transcript, and the last HISTORY_LIMIT messages.

After an action is executed, useful facts are saved through the MEMORY tools
(so every write is validated and appears in tool_calls):
- a recipient/guest the owner introduced with a name and an address becomes a contact;
- once a meeting length clearly dominates recent meetings, it becomes default_meeting_minutes.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.agent.actions import EmailAction, ScheduleAction, parse_action
from app.agent.agent import now_in
from app.agent.context import AgentContext
from app.config import get_settings
from app.db.models import AgentRun, CalendarEvent, Contact, MemoryFact, RunStatus, User, VoiceMessage
from app.db.session import session_scope
from app.tools import persian
from app.tools.registry import Exposure, ToolContext, contact_dict, invoke

log = logging.getLogger(__name__)

HISTORY_LIMIT = 5
MAX_PREFERENCES = 20
MAX_CONTEXT_CONTACTS = 10
MAX_TEXT = 500
# Learn a default meeting length only from enough, consistent evidence.
MEETING_LENGTH_MIN_SAMPLES = 3
MEETING_LENGTH_WINDOW = 10
MEETING_LENGTH_SHARE = 0.6


def contacts_in_text(contacts: list[Contact], text: str) -> list[Contact]:
    """Contacts with a name/alias appearing as whole words in the transcript (after Persian normalisation)."""
    haystack = f" {persian.normalize(text)} "
    found = []
    for c in contacts:
        spellings = {persian.normalize(s) for s in [c.name, *c.aliases] if s}
        if any(len(s) >= 2 and f" {s} " in haystack for s in spellings):
            found.append(c)
    return found[:MAX_CONTEXT_CONTACTS]


def _summarise_run(run: AgentRun | None) -> dict[str, Any]:
    if run is None or not run.action_json:
        return {"role": "assistant", "result": run.status if run else "no_answer"}
    a = run.action_json
    if a.get("type") == "clarify":
        return {"role": "assistant", "asked": a.get("question", "")[:MAX_TEXT]}
    summary = ({"proposed": "schedule", "date": a.get("date"), "time": a.get("time"), "title": a.get("title")}
               if a.get("type") == "schedule"
               else {"proposed": "email", "to": (a.get("to") or {}).get("email"), "subject": a.get("subject")})
    return {"role": "assistant", **summary, "status": run.status}


async def build_context(user: User, transcript: str, *, current_message_id: int | None = None,
                        editing: dict | None = None, now: dt.datetime | None = None,
                        language: str = "fa") -> AgentContext:
    async with session_scope() as s:
        facts = (await s.execute(
            select(MemoryFact).where(MemoryFact.user_id == user.id)
            .order_by(MemoryFact.updated_at.desc()).limit(MAX_PREFERENCES)
        )).scalars().all()
        contacts = (await s.execute(select(Contact).where(Contact.user_id == user.id))).scalars().all()

        q = (select(VoiceMessage).where(VoiceMessage.user_id == user.id, VoiceMessage.transcript.is_not(None))
             .order_by(VoiceMessage.id.desc()).limit(HISTORY_LIMIT + 1))
        recent = [m for m in (await s.execute(q)).scalars().all() if m.id != current_message_id][:HISTORY_LIMIT]
        runs: dict[int, AgentRun] = {}
        if recent:
            for r in (await s.execute(
                select(AgentRun).where(AgentRun.voice_message_id.in_([m.id for m in recent])).order_by(AgentRun.id)
            )).scalars():
                runs[r.voice_message_id] = r  # latest run per message wins

    history: list[dict[str, Any]] = []
    for m in reversed(recent):  # oldest first
        history.append({"role": "owner", "at": m.created_at.isoformat(timespec="minutes"), "text": m.transcript[:MAX_TEXT]})
        history.append(_summarise_run(runs.get(m.id)))

    return AgentContext(
        now=now or now_in(user.timezone or get_settings().default_timezone),
        owner_name=user.name,
        # "language" is a bot setting, given to the model as CONTEXT.language instead.
        preferences={f.key: f.value for f in facts if f.key != "language"},
        contacts=[contact_dict(c) for c in contacts_in_text(contacts, transcript)],
        history=history,
        editing=editing,
        language=language,
    )


async def _known_emails(session, user_id: int) -> set[str]:
    rows = (await session.execute(select(Contact.email).where(Contact.user_id == user_id))).scalars()
    return {e.lower() for e in rows if e}


async def preview_notes(user: User, action: ScheduleAction | EmailAction, language: str = "fa") -> list[str]:
    """Tell the owner, before confirming, which new contacts will be saved."""
    async with session_scope() as s:
        known = await _known_emails(s, user.id)
    people = [action.to] if isinstance(action, EmailAction) else action.guests
    if language == "en":
        return [f"After this is done, \"{p.name}\" <{p.email}> will be added to your contacts."
                for p in people if p.name and p.email not in known]
    return [f"پس از انجام، «{p.name}» با نشانی {p.email} به مخاطبان افزوده می‌شود."
            for p in people if p.name and p.email not in known]


async def after_execution(run_id: int) -> list[str]:
    """Save facts learned from an executed run, via the MEMORY tools. Returns the tool names called."""
    called: list[str] = []
    async with session_scope() as s:
        run = await s.get(AgentRun, run_id)
        if run is None or run.status != RunStatus.EXECUTED.value or run.user_id is None:
            return called
        user = await s.get(User, run.user_id)
        action = parse_action(run.action_json)
        ctx = ToolContext(session=s, user=user, settings=get_settings(), now=now_in(user.timezone), run_id=run_id)
        known = await _known_emails(s, user.id)

        people = [action.to] if isinstance(action, EmailAction) else action.guests
        for p in people:
            if p.name and p.email not in known:
                r = await invoke("save_contact", {"name": p.name, "email": p.email}, ctx, allowed={Exposure.MEMORY})
                called.append("save_contact")
                if r.ok:
                    known.add(p.email)

        if isinstance(action, ScheduleAction):
            durations = list((await s.execute(
                select(CalendarEvent.duration_min)
                .join(AgentRun, CalendarEvent.run_id == AgentRun.id)
                .where(AgentRun.user_id == user.id)
                .order_by(CalendarEvent.id.desc()).limit(MEETING_LENGTH_WINDOW)
            )).scalars())
            if len(durations) >= MEETING_LENGTH_MIN_SAMPLES:
                minutes, count = Counter(durations).most_common(1)[0]
                current = await s.scalar(select(MemoryFact.value).where(
                    MemoryFact.user_id == user.id, MemoryFact.key == "default_meeting_minutes"))
                if count / len(durations) >= MEETING_LENGTH_SHARE and current != str(minutes):
                    await invoke("remember", {"key": "default_meeting_minutes", "value": str(minutes)}, ctx,
                                 allowed={Exposure.MEMORY})
                    called.append("remember")
    if called:
        log.info("Run %s: memory updated via %s", run_id, called)
    return called
