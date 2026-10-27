"""ORM models for every table in docs/SPEC.md.

Design notes
- Status / intent columns are VARCHAR + CHECK rather than native PG ENUMs:
  adding a value later is a one-line constraint change, not an ALTER TYPE.
- JSON payloads use JSONB (indexable, compact).
- User-owned rows cascade on user delete; run-owned rows cascade on run delete.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin


class Intent(str, enum.Enum):
    SCHEDULE = "schedule"
    EMAIL = "email"
    UNKNOWN = "unknown"


class RunStatus(str, enum.Enum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    REJECTED = "rejected"
    FAILED = "failed"


class ToolCallStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EmailStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


def _in(column: str, enum_cls: type[enum.Enum]) -> str:
    values = ", ".join(f"'{e.value}'" for e in enum_cls)
    return f"{column} IN ({values})"


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'Asia/Tehran'")
    )

    contacts: Mapped[list[Contact]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Contact(IdMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_contacts_user_id_name"),
        # GIN index so "find contact by any spelling" (alias = ANY(aliases)) stays fast.
        Index("ix_contacts_aliases_gin", "aliases", postgresql_using="gin"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Alternative spellings, Persian and English (e.g. ["علی", "Ali", "Ali Rezaei"]).
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    email: Mapped[str | None] = mapped_column(String(320))

    user: Mapped[User] = relationship(back_populates="contacts")


class VoiceMessage(IdMixin, TimestampMixin, Base):
    __tablename__ = "voice_messages"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    # Local path of the downloaded audio; cleared when the file is deleted after STT.
    audio_path: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        CheckConstraint(
            "stt_confidence IS NULL OR (stt_confidence >= 0 AND stt_confidence <= 1)",
            name="stt_confidence_range",
        ),
    )


class AgentRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(_in("intent", Intent), name="intent_valid"),
        CheckConstraint(_in("status", RunStatus), name="status_valid"),
    )

    voice_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_messages.id", ondelete="SET NULL"), index=True
    )
    intent: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{Intent.UNKNOWN.value}'")
    )
    action_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{RunStatus.PENDING_CONFIRMATION.value}'"),
        index=True,
    )
    model: Mapped[str | None] = mapped_column(String(100))
    # Split so the token dashboard (batch 04) can price input and output separately.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class ToolCall(IdMixin, TimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (CheckConstraint(_in("status", ToolCallStatus), name="status_valid"),)

    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{ToolCallStatus.PENDING.value}'")
    )
    # Unique: a retried confirmation can never create a second event or send a second email.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class CalendarEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "calendar_events"
    __table_args__ = (CheckConstraint("duration_min > 0", name="duration_positive"),)

    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    provider_event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Guest email addresses.
    guests: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class Email(IdMixin, TimestampMixin, Base):
    __tablename__ = "emails"
    __table_args__ = (CheckConstraint(_in("status", EmailStatus), name="status_valid"),)

    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    # Column is "to_address": TO is a reserved word in PostgreSQL.
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{EmailStatus.QUEUED.value}'")
    )


class MemoryFact(IdMixin, TimestampMixin, Base):
    __tablename__ = "memory_facts"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_memory_facts_user_id_key"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the fact came from, e.g. "user", "agent", "seed".
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'user'"))


class AuditEvent(IdMixin, TimestampMixin, Base):
    __tablename__ = "audit_events"

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # e.g. "telegram:111111111", "system", "cli".
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
