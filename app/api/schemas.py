"""Response/request models. Only fields listed here leave the API: no audio paths, no secrets."""

from __future__ import annotations

import datetime as dt
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools import persian

T = TypeVar("T")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class RunSummary(_Out):
    id: int
    user_id: int | None
    voice_message_id: int | None
    intent: str
    status: str
    model: str | None
    prompt_version: str | None
    steps: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    error: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ToolCallOut(_Out):
    id: int
    tool_name: str
    arguments_json: dict[str, Any]
    result_json: dict[str, Any] | None
    status: str
    latency_ms: int | None
    created_at: dt.datetime


class RunDetail(RunSummary):
    source: str | None = None
    transcript: str | None = None
    stt_confidence: float | None = None
    action_json: dict[str, Any] | None
    tool_calls: list[ToolCallOut] = []


class EventOut(_Out):
    id: int
    run_id: int | None
    provider_event_id: str | None
    date: dt.date
    time: dt.time
    duration_min: int
    title: str
    description: str | None
    guests: list[str]
    created_at: dt.datetime


class EmailOut(_Out):
    id: int
    run_id: int | None
    to_address: str
    subject: str
    body: str
    provider_message_id: str | None
    status: str
    created_at: dt.datetime


class ContactOut(_Out):
    id: int
    user_id: int
    name: str
    aliases: list[str]
    email: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = persian.to_ascii_digits(v).strip().lower()
        if not persian.EMAIL_RE.fullmatch(v):
            raise ValueError("not a valid email address")
        return v

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: list[str]) -> list[str]:
        cleaned = [a.strip() for a in v if a.strip()]
        if any(len(a) > 100 for a in cleaned):
            raise ValueError("alias too long")
        return list(dict.fromkeys(cleaned))


class RetryOut(BaseModel):
    run_id: int
    kind: str = Field(description="'execution' (re-ran a confirmed action) or 'agent' (re-planned the message)")
    outcome: str = Field(description="executed | failed | already_done | action | question")
    new_run_id: int | None = None
    notified: bool = False
    detail: str | None = None


class Check(BaseModel):
    ok: bool
    latency_ms: int | None = None
    detail: str | None = None


class Health(BaseModel):
    status: str
    checks: dict[str, Check]
