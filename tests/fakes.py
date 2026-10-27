"""Test doubles for external services (Google Calendar, Resend)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from app.tools.google_calendar import CreatedEvent


@dataclass
class FakeCalendar:
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    inserts: int = 0
    existing: list[dict[str, Any]] = field(default_factory=list)

    async def list_events(self, day: dt.date, tz: dt.tzinfo) -> list[dict[str, Any]]:
        return [e for e in self.existing if e["start"].startswith(day.isoformat())]

    async def insert_event(self, *, event_id, start, end, timezone, title, description, attendees) -> CreatedEvent:
        self.inserts += 1
        if event_id in self.events:  # same semantics as Google's 409 on a reused id
            return CreatedEvent(event_id, None, duplicate=True)
        self.events[event_id] = {"start": start, "end": end, "timezone": timezone, "title": title,
                                 "description": description, "attendees": attendees}
        return CreatedEvent(event_id, f"https://calendar.test/{event_id}", duplicate=False)


@dataclass
class FakeMailer:
    sent: list[dict[str, Any]] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)

    async def send_email(self, to, subject, *, text, html=None, idempotency_key=None, reply_to=None):
        if idempotency_key in self.keys:  # Resend replays the original response for a reused key
            return SimpleNamespace(id=f"msg-{idempotency_key}")
        self.keys.add(idempotency_key)
        self.sent.append({"to": to, "subject": subject, "text": text, "reply_to": reply_to})
        return SimpleNamespace(id=f"msg-{idempotency_key}")


# ------------------------------------------------------------------ LLM ---

import json as _json  # noqa: E402

from app.agent.provider import LLMResponse, ToolCallRequest  # noqa: E402


def tc(name: str, args: dict | None = None, call_id: str | None = None) -> dict:
    """One scripted tool call."""
    return {"name": name, "args": args or {}, "id": call_id}


class ScriptedProvider:
    """Plays back a fixed list of turns. Each turn is a list of tool calls, or a str (plain text reply)."""

    model = "scripted"

    def __init__(self, turns: list):
        self.turns = list(turns)
        self.calls: list[list[dict]] = []  # messages seen at each step

    async def complete(self, messages, tools):
        self.calls.append(_json.loads(_json.dumps(messages)))
        self.tools = tools
        turn = self.turns.pop(0) if self.turns else "I keep talking instead of calling a tool."
        if isinstance(turn, str):
            return LLMResponse(content=turn, tool_calls=[], model=self.model, prompt_tokens=100,
                               completion_tokens=10, message={"role": "assistant", "content": turn})
        calls = [ToolCallRequest(id=c["id"] or f"call{len(self.calls)}_{i}", name=c["name"],
                                 arguments=_json.dumps(c["args"], ensure_ascii=False)) for i, c in enumerate(turn)]
        return LLMResponse(
            content=None, tool_calls=calls, model=self.model, prompt_tokens=100, completion_tokens=20,
            message={"role": "assistant", "content": "", "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]},
        )
