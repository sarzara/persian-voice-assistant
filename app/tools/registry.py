"""Tool registry: JSON schemas from Pydantic, validation, execution and logging.

Exposure tiers (enforced here, not by the prompt):
- AGENT:  read-only tools the LLM may call while planning.
- ACTION: side-effecting tools; only the confirmation executor may call them.
- MEMORY: internal writes; called by code after an executed action.

Every invocation inside a run is written to tool_calls with arguments,
result, status and latency.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import CalendarEvent, Contact, Email, MemoryFact, ToolCall, ToolCallStatus, User
from app.tools import persian
from app.tools.google_calendar import CalendarNotConfigured, CreatedEvent, event_id_for

log = logging.getLogger(__name__)


class Exposure(str, enum.Enum):
    AGENT = "agent"
    ACTION = "action"
    MEMORY = "memory"


# ---------------------------------------------------------------- backends ---

class CalendarBackend(Protocol):
    async def list_events(self, day: dt.date, tz: dt.tzinfo) -> list[dict[str, Any]]: ...

    async def insert_event(self, *, event_id: str, start: dt.datetime, end: dt.datetime, timezone: str,
                           title: str, description: str | None, attendees: list[str]) -> CreatedEvent: ...


class MailBackend(Protocol):
    async def send_email(self, to: str | list[str], subject: str, *, text: str, html: str | None = None,
                         idempotency_key: str | None = None, reply_to: str | None = None) -> Any: ...


@dataclass
class ToolContext:
    session: AsyncSession
    user: User
    settings: Settings
    now: dt.datetime  # tz-aware, in the user's timezone
    calendar: CalendarBackend | None = None
    mailer: MailBackend | None = None
    run_id: int | None = None
    # Set by the executor for ACTION tools; also forwarded to the provider.
    idempotency_key: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.user.timezone or self.settings.default_timezone)


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def for_model(self) -> dict[str, Any]:
        return {"ok": True, **self.data} if self.ok else {"ok": False, "error": self.error}


class ToolFailure(Exception):
    """Expected failure with a message safe to show the model."""


# ------------------------------------------------------------ arg schemas ---

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _email(value: str) -> str:
    value = persian.to_ascii_digits(value).strip()
    if not persian.EMAIL_RE.fullmatch(value):
        raise ValueError(f"not a valid email address: {value!r}")
    return value.lower()


class NoArgs(_Args):
    pass


class ConvertJalaliArgs(_Args):
    jalali_date: str = Field(description="Jalali (Solar Hijri) date as YYYY-MM-DD, e.g. 1405-08-15")

    @field_validator("jalali_date")
    @classmethod
    def _parse(cls, v: str) -> str:
        v = persian.to_ascii_digits(v).replace("/", "-")
        parts = v.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("expected YYYY-MM-DD")
        if not 1300 <= int(parts[0]) <= 1500:
            raise ValueError("Jalali year out of range")
        return v


class FindContactArgs(_Args):
    name: str = Field(min_length=1, max_length=100, description="Name as spoken, Persian or English")


class SaveContactArgs(_Args):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320)
    _v = field_validator("email")(lambda cls, v: _email(v))


class RememberArgs(_Args):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,49}$", description="snake_case key")
    value: str = Field(min_length=1, max_length=500)


class ListEventsArgs(_Args):
    date: dt.date = Field(description="Gregorian date YYYY-MM-DD")


class CreateEventArgs(_Args):
    date: dt.date
    time: dt.time
    duration_min: int = Field(ge=5, le=600)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    guests: list[str] = Field(default_factory=list, max_length=20)
    _g = field_validator("guests")(lambda cls, v: [_email(x) for x in v])


class SendEmailArgs(_Args):
    to: str = Field(max_length=320, description="Exactly one recipient address")
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    _t = field_validator("to")(lambda cls, v: _email(v))


# ---------------------------------------------------------------- handlers ---

async def get_current_datetime(ctx: ToolContext, args: NoArgs) -> dict[str, Any]:
    now = ctx.now.astimezone(ctx.tz)
    today = now.date()
    return {
        "timezone": str(ctx.tz),
        "now": now.isoformat(timespec="minutes"),
        "date": today.isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday_fa": persian.WEEKDAYS_FA[today.weekday()],
        "jalali_date": persian.jalali_iso(today),
        "jalali_text": persian.jalali_text(today),
        # Lets the model resolve "next Tuesday" / "the 15th" without arithmetic.
        "next_14_days": [
            {"date": d.isoformat(), "weekday_fa": persian.WEEKDAYS_FA[d.weekday()], "jalali": persian.jalali_iso(d)}
            for d in (today + dt.timedelta(days=i) for i in range(14))
        ],
    }


async def convert_jalali_date(ctx: ToolContext, args: ConvertJalaliArgs) -> dict[str, Any]:
    y, m, d = (int(p) for p in args.jalali_date.split("-"))
    try:
        g = persian.jalali_to_gregorian(y, m, d)
    except ValueError as exc:
        raise ToolFailure(f"invalid Jalali date: {exc}") from exc
    return {"gregorian_date": g.isoformat(), "weekday_fa": persian.WEEKDAYS_FA[g.weekday()]}


async def find_contacts_matching(session: AsyncSession, user_id: int, name: str, limit: int = 5) -> list[Contact]:
    """Exact (normalised) name/alias matches first, then partial matches."""
    needle = persian.normalize(name)
    if not needle:
        return []
    rows = (await session.execute(select(Contact).where(Contact.user_id == user_id))).scalars().all()
    exact, partial = [], []
    for c in rows:
        spellings = {persian.normalize(s) for s in [c.name, *c.aliases]}
        if needle in spellings:
            exact.append(c)
        elif any(needle in s or (len(s) >= 2 and s in needle) for s in spellings):
            partial.append(c)
    return (exact + partial)[:limit]


def contact_dict(c: Contact) -> dict[str, Any]:
    return {"name": c.name, "email": c.email, "aliases": list(c.aliases)}


async def find_contact(ctx: ToolContext, args: FindContactArgs) -> dict[str, Any]:
    matches = await find_contacts_matching(ctx.session, ctx.user.id, args.name)
    return {"matches": [contact_dict(c) for c in matches], "count": len(matches)}


async def save_contact(ctx: ToolContext, args: SaveContactArgs) -> dict[str, Any]:
    stmt = insert(Contact).values(user_id=ctx.user.id, name=args.name, email=args.email, aliases=[args.name])
    stmt = stmt.on_conflict_do_update(constraint="uq_contacts_user_id_name", set_={"email": stmt.excluded.email})
    await ctx.session.execute(stmt)
    return {"saved": {"name": args.name, "email": args.email}}


async def remember(ctx: ToolContext, args: RememberArgs) -> dict[str, Any]:
    stmt = insert(MemoryFact).values(user_id=ctx.user.id, key=args.key, value=args.value, source="agent")
    stmt = stmt.on_conflict_do_update(
        constraint="uq_memory_facts_user_id_key", set_={"value": stmt.excluded.value, "source": "agent"}
    )
    await ctx.session.execute(stmt)
    return {"remembered": {args.key: args.value}}


async def list_calendar_events(ctx: ToolContext, args: ListEventsArgs) -> dict[str, Any]:
    if ctx.calendar is None:
        raise ToolFailure("Google Calendar is not configured; continue without checking conflicts")
    events = await ctx.calendar.list_events(args.date, ctx.tz)
    return {"date": args.date.isoformat(), "events": events}


async def create_calendar_event(ctx: ToolContext, args: CreateEventArgs) -> dict[str, Any]:
    if ctx.calendar is None:
        raise CalendarNotConfigured("Google Calendar is not configured")
    if not ctx.idempotency_key:
        raise ToolFailure("create_calendar_event requires an idempotency key")
    start = dt.datetime.combine(args.date, args.time, ctx.tz)
    end = start + dt.timedelta(minutes=args.duration_min)
    created = await ctx.calendar.insert_event(
        event_id=event_id_for(ctx.idempotency_key), start=start, end=end, timezone=str(ctx.tz),
        title=args.title, description=args.description, attendees=args.guests,
    )
    if not created.duplicate:
        ctx.session.add(CalendarEvent(
            run_id=ctx.run_id, provider_event_id=created.id, date=args.date, time=args.time,
            duration_min=args.duration_min, title=args.title, description=args.description, guests=args.guests,
        ))
    return {"event_id": created.id, "html_link": created.html_link, "duplicate": created.duplicate}


def _email_html(body: str) -> str:
    import html

    return ('<div dir="rtl" style="font-family:Tahoma,Vazirmatn,sans-serif;line-height:1.9;text-align:right">'
            + html.escape(body).replace("\n", "<br>") + "</div>")


async def send_email(ctx: ToolContext, args: SendEmailArgs) -> dict[str, Any]:
    if ctx.mailer is None:
        raise ToolFailure("email is not configured")
    if not ctx.idempotency_key:
        raise ToolFailure("send_email requires an idempotency key")
    sent = await ctx.mailer.send_email(
        args.to, args.subject, text=args.body, html=_email_html(args.body),
        idempotency_key=ctx.idempotency_key,
        reply_to=ctx.user.email,  # replies go to the owner, not to the sending domain
    )
    ctx.session.add(Email(run_id=ctx.run_id, to_address=args.to, subject=args.subject, body=args.body,
                          provider_message_id=sent.id, status="sent"))
    return {"provider_message_id": sent.id}


# ---------------------------------------------------------------- registry ---

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    handler: Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]
    exposure: Exposure

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        schema.setdefault("properties", {})
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": schema}}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_current_datetime", "Current date and time in the user's timezone (Asia/Tehran), with the Jalali date "
         "and a 14-day Gregorian/Jalali/weekday table for resolving relative dates.", NoArgs, get_current_datetime, Exposure.AGENT),
    Tool("convert_jalali_date", "Convert a Jalali (Solar Hijri) date YYYY-MM-DD to Gregorian.",
         ConvertJalaliArgs, convert_jalali_date, Exposure.AGENT),
    Tool("find_contact", "Look up the user's saved contacts by name (Persian or English spelling). "
         "Returns matches with their email addresses.", FindContactArgs, find_contact, Exposure.AGENT),
    Tool("list_calendar_events", "List the user's calendar events on a Gregorian date, to spot conflicts.",
         ListEventsArgs, list_calendar_events, Exposure.AGENT),
    Tool("save_contact", "Save or update a contact's email address.", SaveContactArgs, save_contact, Exposure.MEMORY),
    Tool("remember", "Store a user preference (e.g. default_meeting_minutes).", RememberArgs, remember, Exposure.MEMORY),
    Tool("create_calendar_event", "Create a Google Calendar event in the user's timezone and invite guests.",
         CreateEventArgs, create_calendar_event, Exposure.ACTION),
    Tool("send_email", "Send one email via Resend.", SendEmailArgs, send_email, Exposure.ACTION),
]}


def schemas_for(exposure: Exposure) -> list[dict[str, Any]]:
    return [t.openai_schema() for t in TOOLS.values() if t.exposure is exposure]


async def invoke(
    name: str,
    raw_args: dict[str, Any],
    ctx: ToolContext,
    *,
    allowed: set[Exposure],
    call_key: str | None = None,
    log_row: ToolCall | None = None,
) -> ToolResult:
    """Validate, run and (inside a run) log one tool call. Never raises for tool errors.

    ``log_row``: a tool_calls row the caller already inserted (and committed) to
    reserve an idempotency key; it is updated in place instead of inserting.
    """
    started = time.perf_counter()
    tool = TOOLS.get(name)
    if tool is None or tool.exposure not in allowed:
        # The model asked for something it is not allowed to do (or that doesn't exist).
        result = ToolResult(False, error=f"tool '{name}' is not available")
    else:
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            result = ToolResult(False, error="invalid arguments: " + "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors()))
        else:
            try:
                async with ctx.session.begin_nested():  # a failing tool rolls back only its own writes
                    result = ToolResult(True, await tool.handler(ctx, args))
            except ToolFailure as exc:
                result = ToolResult(False, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - provider errors become tool errors
                log.exception("Tool %s failed", name)
                result = ToolResult(False, error=f"{exc.__class__.__name__}: {exc}"[:500])

    latency_ms = int((time.perf_counter() - started) * 1000)
    if log_row is not None:
        log_row.result_json = result.data if result.ok else {"error": result.error}
        log_row.status = (ToolCallStatus.SUCCEEDED if result.ok else ToolCallStatus.FAILED).value
        log_row.latency_ms = latency_ms
        await ctx.session.flush()
    elif ctx.run_id is not None:
        ctx.session.add(ToolCall(
            run_id=ctx.run_id,
            tool_name=name,
            arguments_json=raw_args if isinstance(raw_args, dict) else {"_raw": str(raw_args)},
            result_json=result.data if result.ok else {"error": result.error},
            status=(ToolCallStatus.SUCCEEDED if result.ok else ToolCallStatus.FAILED).value,
            idempotency_key=call_key or f"run{ctx.run_id}:{name}:{uuid.uuid4().hex}",
            latency_ms=latency_ms,
        ))
        await ctx.session.flush()
    log.info("tool=%s ok=%s latency_ms=%d run=%s", name, result.ok, latency_ms, ctx.run_id)
    return result

