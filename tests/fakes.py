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
