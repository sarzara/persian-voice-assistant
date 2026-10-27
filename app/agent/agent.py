"""The agent loop: transcript -> exactly one validated action, or one clarifying question.

Guarantees enforced in code, independent of what the model does:
- At most ``agent_max_steps`` LLM calls per message.
- Only read-only tools are callable while planning; nothing is sent or scheduled here.
- The final action passes schema validation and guardrails (no invented emails,
  no past dates, Persian text) before it can reach the user for confirmation.
- The run and every tool call are persisted with tokens and latency.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select

from app.agent.actions import (
    TERMINAL_SCHEMAS,
    TERMINAL_TOOLS,
    Clarification,
    EmailAction,
    ScheduleAction,
    check_action,
    format_validation_error,
    validate_terminal,
)
from app.agent.context import AgentContext, user_message
from app.agent.prompts import PROMPT_VERSION, system_prompt
from app.agent.provider import LLMError, LLMProvider
from app.config import Settings, get_settings
from app.db.models import AgentRun, Contact, Intent, RunStatus, ToolCall, ToolCallStatus, User
from app.db.session import session_scope
from app.tools import persian
from app.tools.registry import CalendarBackend, Exposure, ToolContext, invoke, schemas_for

log = logging.getLogger(__name__)

NUDGE = ("Reply only by calling one of: submit_schedule_action, submit_email_action, ask_user. "
         "Plain-text answers are not shown to the owner.")


@dataclass
class AgentOutcome:
    kind: str  # "action" | "question" | "failed"
    run_id: int
    action: ScheduleAction | EmailAction | None = None
    question: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)  # shown under the preview (e.g. a contact that will be saved)


class Agent:
    def __init__(self, provider: LLMProvider, *, settings: Settings | None = None,
                 calendar: CalendarBackend | None = None) -> None:
        self.provider = provider
        self.settings = settings or get_settings()
        self.calendar = calendar
        self.tools = schemas_for(Exposure.AGENT) + TERMINAL_SCHEMAS

    async def run(self, *, user: User, message_id: int | None, transcript: str, context: AgentContext) -> AgentOutcome:
        started = time.perf_counter()
        tz = ZoneInfo(user.timezone or self.settings.default_timezone)
        now = context.now.astimezone(tz)

        async with session_scope() as s:
            run = AgentRun(voice_message_id=message_id, user_id=user.id, status=RunStatus.RUNNING.value,
                           intent=Intent.UNKNOWN.value, model=self.provider.model, prompt_version=PROMPT_VERSION)
            s.add(run)
            await s.flush()
            run_id = run.id
            contact_emails = set((await s.execute(
                select(Contact.email).where(Contact.user_id == user.id, Contact.email.is_not(None))
            )).scalars())
        # Addresses the model may use: saved contacts, or ones the owner said/typed.
        allowed_emails = {e.lower() for e in contact_emails} | persian.emails_in(transcript) | context.mentioned_emails()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_message(context, transcript)},
        ]
        totals = {"prompt": 0, "completion": 0}
        outcome: AgentOutcome | None = None
        steps = 0

        try:
            for steps in range(1, self.settings.agent_max_steps + 1):
                response = await self.provider.complete(messages, self.tools)
                totals["prompt"] += response.prompt_tokens
                totals["completion"] += response.completion_tokens
                messages.append(response.message)

                if not response.tool_calls:
                    messages.append({"role": "user", "content": NUDGE})
                    continue

                async with session_scope() as s:
                    db_user = await s.get(User, user.id)
                    ctx = ToolContext(session=s, user=db_user, settings=self.settings, now=now,
                                      calendar=self.calendar, run_id=run_id)
                    for call in response.tool_calls:
                        if outcome is not None:
                            # Already finished: answer remaining calls so the transcript stays valid.
                            messages.append(_tool_msg(call.id, {"ok": False, "error": "ignored: a final answer was already accepted"}))
                            continue
                        if call.name in TERMINAL_TOOLS:
                            result, outcome = self._terminal(call, run_id, allowed_emails, now, tz, s)
                            messages.append(_tool_msg(call.id, result))
                        else:
                            try:
                                args = call.parsed_arguments()
                            except ValueError:
                                args, res = {}, {"ok": False, "error": "arguments are not valid JSON"}
                            else:
                                res = (await invoke(call.name, args, ctx, allowed={Exposure.AGENT},
                                                    call_key=f"run{run_id}:s{steps}:{call.id}"[:128])).for_model()
                            messages.append(_tool_msg(call.id, res))
                if outcome is not None:
                    break
        except LLMError as exc:
            log.error("Run %s: LLM error: %s", run_id, exc)
            outcome = AgentOutcome("failed", run_id, error=f"llm_error: {exc}")

        if outcome is None:
            outcome = AgentOutcome("failed", run_id, error=f"no valid final answer within {self.settings.agent_max_steps} steps")

        await self._finish(run_id, outcome, steps, totals, int((time.perf_counter() - started) * 1000))
        return outcome

    def _terminal(self, call, run_id, allowed_emails, now, tz, session):
        raw: dict[str, Any] = {}
        try:
            raw = call.parsed_arguments()
            parsed = validate_terminal(call.name, raw)
        except ValidationError as exc:
            err = format_validation_error(exc)
            self._log_terminal(session, run_id, call, raw, False, err)
            return {"ok": False, "error": err}, None
        except ValueError:
            err = "arguments are not valid JSON"
            self._log_terminal(session, run_id, call, raw, False, err)
            return {"ok": False, "error": err}, None

        if isinstance(parsed, Clarification):
            self._log_terminal(session, run_id, call, raw, True, None)
            return {"ok": True}, AgentOutcome("question", run_id, question=parsed.question)

        problems = check_action(parsed, allowed_emails=allowed_emails, now=now, tz=tz)
        if problems:
            err = " ".join(problems)
            self._log_terminal(session, run_id, call, raw, False, err)
            log.info("Run %s: action rejected by guardrails: %s", run_id, err)
            return {"ok": False, "error": err}, None
        self._log_terminal(session, run_id, call, raw, True, None)
        return {"ok": True, "status": "shown to the owner for confirmation"}, AgentOutcome("action", run_id, action=parsed)

    @staticmethod
    def _log_terminal(session, run_id, call, raw, ok, error):
        session.add(ToolCall(
            run_id=run_id, tool_name=call.name, arguments_json=raw if isinstance(raw, dict) else {},
            result_json={"accepted": True} if ok else {"error": error},
            status=(ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED).value,
            idempotency_key=f"run{run_id}:{call.name}:{uuid.uuid4().hex}"[:128], latency_ms=0,
        ))

    @staticmethod
    async def _finish(run_id: int, outcome: AgentOutcome, steps: int, totals: dict[str, int], latency_ms: int) -> None:
        async with session_scope() as s:
            run = await s.get(AgentRun, run_id)
            run.steps = steps
            run.prompt_tokens = totals["prompt"]
            run.completion_tokens = totals["completion"]
            run.latency_ms = latency_ms
            if outcome.kind == "action":
                run.intent = outcome.action.type
                run.action_json = outcome.action.model_dump(mode="json")
                run.status = RunStatus.PENDING_CONFIRMATION.value
            elif outcome.kind == "question":
                run.action_json = {"type": "clarify", "question": outcome.question}
                run.status = RunStatus.NEEDS_CLARIFICATION.value
            else:
                run.status = RunStatus.FAILED.value
                run.error = outcome.error
        log.info("Run %s finished: %s in %d steps, %d+%d tokens, %d ms",
                 run_id, outcome.kind, steps, totals["prompt"], totals["completion"], latency_ms)


def _tool_msg(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, ensure_ascii=False, default=str)}


def now_in(tz_name: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz_name))
