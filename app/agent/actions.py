"""The agent's only possible outputs, and the guardrails applied to them in code.

Schema validation (Pydantic) checks shape; ``check_action`` checks the action
against facts the model cannot be trusted with: allowed email addresses, the
current time, and the output language.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.i18n import detect_text_language
from app.tools import persian
from app.tools.schema_utils import clean_schema

MAX_GUESTS = 10


def _email(v: str) -> str:
    v = persian.to_ascii_digits(v).strip().lower()
    if not persian.EMAIL_RE.fullmatch(v):
        raise ValueError(f"not a valid email address: {v!r}")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Person(_Strict):
    name: str | None = Field(default=None, max_length=200)
    email: str = Field(max_length=320)
    _e = field_validator("email")(lambda cls, v: _email(v))


class ScheduleAction(_Strict):
    type: Literal["schedule"] = "schedule"
    date: dt.date = Field(description="Gregorian YYYY-MM-DD")
    time: dt.time = Field(description="24-hour HH:MM, Asia/Tehran")
    duration_min: int = Field(ge=5, le=600)
    title: str = Field(min_length=1, max_length=200, description="In CONTEXT.language")
    description: str = Field(default="", max_length=2000, description="In CONTEXT.language, may be empty")
    guests: list[Person] = Field(default_factory=list, max_length=MAX_GUESTS)
    warnings: list[str] = Field(default_factory=list, max_length=3, description="Short notes in CONTEXT.language, e.g. a conflict")

    @field_validator("time", mode="before")
    @classmethod
    def _time(cls, v: Any) -> Any:
        return persian.to_ascii_digits(v) if isinstance(v, str) else v

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> Any:
        return persian.to_ascii_digits(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _unique_guests(self) -> ScheduleAction:
        seen: set[str] = set()
        self.guests = [g for g in self.guests if not (g.email in seen or seen.add(g.email))]
        return self

    def emails(self) -> set[str]:
        return {g.email for g in self.guests}

    def tool_args(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "time": self.time.strftime("%H:%M"), "duration_min": self.duration_min,
                "title": self.title, "description": self.description or None, "guests": sorted(self.emails())}


class EmailAction(_Strict):
    type: Literal["email"] = "email"
    to: Person = Field(description="Exactly one recipient")
    subject: str = Field(min_length=1, max_length=200, description="In CONTEXT.language")
    # Capped so the full body always fits in one Telegram preview: the owner confirms exactly what is sent.
    body: str = Field(min_length=1, max_length=3000, description="In CONTEXT.language")

    def emails(self) -> set[str]:
        return {self.to.email}

    def tool_args(self) -> dict[str, Any]:
        return {"to": self.to.email, "subject": self.subject, "body": self.body}


class Clarification(_Strict):
    question: str = Field(min_length=1, max_length=500, description="One short question in CONTEXT.language")


Action = Annotated[ScheduleAction | EmailAction, Field(discriminator="type")]


def parse_action(data: dict[str, Any]) -> ScheduleAction | EmailAction:
    return ScheduleAction.model_validate(data) if data.get("type") == "schedule" else EmailAction.model_validate(data)


# ------------------------------------------------------- terminal tool schemas ---

def _params(model: type[BaseModel], drop: tuple[str, ...] = ("type",)) -> dict[str, Any]:
    return clean_schema(model, drop_properties=drop)


TERMINAL_TOOLS = {
    "submit_schedule_action": ScheduleAction,
    "submit_email_action": EmailAction,
    "ask_user": Clarification,
}

TERMINAL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "submit_schedule_action",
        "description": "Final answer: propose one calendar event. The owner must confirm before anything happens.",
        "parameters": _params(ScheduleAction)}},
    {"type": "function", "function": {
        "name": "submit_email_action",
        "description": "Final answer: propose one email to exactly one recipient. The owner must confirm before it is sent.",
        "parameters": _params(EmailAction)}},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Final answer: ask the owner one short clarifying question.",
        "parameters": _params(Clarification, drop=())}},
]


def validate_terminal(name: str, raw: dict[str, Any]) -> ScheduleAction | EmailAction | Clarification:
    model = TERMINAL_TOOLS[name]
    data = dict(raw)
    if model is not Clarification:
        data["type"] = "schedule" if model is ScheduleAction else "email"
    return model.model_validate(data)


def format_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())


# ------------------------------------------------------------------ guardrails ---

def check_action(
    action: ScheduleAction | EmailAction,
    *,
    allowed_emails: set[str],
    now: dt.datetime,
    tz: dt.tzinfo,
    language: str = "fa",
) -> list[str]:
    """Return a list of problems (empty = acceptable). Messages go back to the model."""
    problems: list[str] = []

    invented = sorted(action.emails() - allowed_emails)
    if invented:
        problems.append(
            f"email address(es) {invented} are neither a saved contact nor said by the owner in this conversation. "
            "Never invent addresses: use find_contact, or ask_user for the address."
        )

    if isinstance(action, ScheduleAction):
        start = dt.datetime.combine(action.date, action.time, tz)
        if start < now - dt.timedelta(minutes=1):
            problems.append(f"{start.isoformat()} is in the past (now is {now.isoformat(timespec='minutes')}). "
                            "Re-resolve the date, or ask_user.")
        if start > now + dt.timedelta(days=730):
            problems.append("date is more than two years ahead; re-check the date conversion or ask_user.")
        texts = {"title": action.title, "description": action.description}
    else:
        texts = {"subject": action.subject, "body": action.body}

    for field_name, text in texts.items():
        if not text:
            continue
        if language == "en":
            if detect_text_language(text) != "en":
                problems.append(f"{field_name} must be written in English (CONTEXT.language is en).")
        elif not persian.has_persian(text):
            problems.append(f"{field_name} must be written in Persian.")
    return problems
