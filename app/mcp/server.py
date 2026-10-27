"""MCP server exposing the assistant's tools to MCP clients (Claude Desktop, IDEs).

One implementation, two interfaces: every tool here calls the same registry
functions the agent uses (app/tools/registry.py), with the same Pydantic
validation and tool_calls logging.

Write tools (create_calendar_event, send_email) require ``confirm=true`` and
every attempt, refused or executed, is written to audit_events. ``confirm`` is a
guard against accidental calls, not human approval: rely on the MCP client's
own tool-approval prompt (the tools are annotated as non-read-only and
destructive so clients ask). Writes are idempotent: the same ``request_id``
(or, without one, the same arguments) never acts twice.

    python -m app.mcp.server            # stdio (Claude Desktop)
    python -m app.mcp.server --http     # streamable HTTP on :8001, Bearer MCP_API_KEY
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.logging_setup import configure_logging, new_request_id, set_run_id
from app.db.models import AgentRun, RunStatus, ToolCall, ToolCallStatus, User
from app.db.repo import audit
from app.db.session import session_scope
from app.tools.registry import CalendarBackend, Exposure, MailBackend, ToolContext, invoke

log = logging.getLogger(__name__)

UPCOMING_DAYS = 7
CONFIRM_REQUIRED = ("Confirmation required. Show the user exactly what will happen (recipients, date/time, text) "
                    "and call again with confirm=true only after they agree.")

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


class OwnerMissing(RuntimeError):
    pass


async def _owner(session) -> User:
    tg = get_settings().owner_telegram_id
    user = (await session.execute(select(User).where(User.telegram_user_id == tg))).scalar_one_or_none() \
        if tg is not None else None
    if user is None:
        raise OwnerMissing("owner not found: set OWNER_TELEGRAM_ID and run the seed")
    return user


def _now(user: User) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(user.timezone or get_settings().default_timezone))


def write_key(tool: str, args: dict[str, Any], request_id: str | None) -> str:
    basis = f"req:{request_id}" if request_id else "args:" + json.dumps(args, sort_keys=True, ensure_ascii=False)
    return f"mcp-{tool}-" + hashlib.sha256(basis.encode()).hexdigest()[:40]


def build_server(
    *,
    calendar_factory: Callable[[], CalendarBackend | None],
    mailer_factory: Callable[[], MailBackend | None],
) -> MCPServer:
    server = MCPServer(
        name="persian-voice-assistant",
        version="1.0.0",
        instructions=("Tools of a personal assistant (timezone Asia/Tehran). Look up contacts before emailing; "
                      "never guess addresses. Writes need confirm=true after the user has approved the details."),
    )

    def safe(fn):
        """Expected failures become a tool result the client can show; details stay in the logs."""
        import functools

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            new_request_id("mcp")
            set_run_id(None)
            try:
                return await fn(*a, **kw)
            except OwnerMissing as exc:
                return {"ok": False, "error": str(exc)}
        return wrapper

    @safe
    async def read(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with session_scope() as s:
            user = await _owner(s)
            ctx = ToolContext(session=s, user=user, settings=get_settings(), now=_now(user),
                              calendar=calendar_factory())
            return (await invoke(tool, args, ctx, allowed={Exposure.AGENT})).for_model()

    @safe
    async def write(tool: str, args: dict[str, Any], *, confirm: bool, request_id: str | None) -> dict[str, Any]:
        key = write_key(tool, args, request_id)
        async with session_scope() as s:
            user = await _owner(s)
            if not confirm:
                await audit(s, f"mcp.{tool}.refused", "mcp", reason="confirm=false", arguments=args)
                return {"ok": False, "error": CONFIRM_REQUIRED}
            previous = (await s.execute(select(ToolCall).where(ToolCall.idempotency_key == key))).scalar_one_or_none()
            if previous is not None:
                await audit(s, f"mcp.{tool}.replayed", "mcp", run_id=previous.run_id, idempotency_key=key)
                return {**(previous.result_json or {}), "ok": previous.status == ToolCallStatus.SUCCEEDED.value,
                        "duplicate": True, "run_id": previous.run_id}
            user_id = user.id

        # Reserve: one run row per MCP write (so it shows up next to agent runs), key committed first.
        try:
            async with session_scope() as s:
                run = AgentRun(user_id=user_id, intent="schedule" if tool == "create_calendar_event" else "email",
                               action_json={"source": "mcp", "tool": tool, **args}, status=RunStatus.CONFIRMED.value,
                               model="mcp")
                s.add(run)
                await s.flush()
                run_id = run.id
                set_run_id(run_id)
                s.add(ToolCall(run_id=run_id, tool_name=tool, arguments_json=args,
                               status=ToolCallStatus.PENDING.value, idempotency_key=key))
        except IntegrityError:  # a concurrent identical call won the race
            return {"ok": False, "duplicate": True, "error": "an identical request is already being processed"}

        async with session_scope() as s:
            user = await s.get(User, user_id)
            row = (await s.execute(select(ToolCall).where(ToolCall.idempotency_key == key))).scalar_one()
            ctx = ToolContext(session=s, user=user, settings=get_settings(), now=_now(user),
                              calendar=calendar_factory(), mailer=mailer_factory(), run_id=run_id, idempotency_key=key)
            result = await invoke(tool, args, ctx, allowed={Exposure.ACTION}, log_row=row)
            run = await s.get(AgentRun, run_id)
            run.status = (RunStatus.EXECUTED if result.ok else RunStatus.FAILED).value
            run.error = result.error
            await audit(s, f"mcp.{tool}", "mcp", run_id=run_id, ok=result.ok, arguments=args)
        return {**result.for_model(), "run_id": run_id}

    @server.tool(annotations=READ)
    async def find_contact(name: str) -> dict[str, Any]:
        """Find the owner's contacts by name (Persian or English spelling). Returns names and email addresses."""
        return await read("find_contact", {"name": name})

    @server.tool(annotations=READ)
    async def list_calendar_events(date: str) -> dict[str, Any]:
        """List the owner's Google Calendar events on a Gregorian date (YYYY-MM-DD), Asia/Tehran time."""
        return await read("list_calendar_events", {"date": date})

    @server.tool(annotations=WRITE)
    async def create_calendar_event(
        date: str,
        time: str,
        duration_min: int,
        title: str,
        description: str | None = None,
        guests: list[str] | None = None,
        confirm: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Google Calendar event (Asia/Tehran) and email invitations to guests.
        date: YYYY-MM-DD, time: HH:MM (24h). Requires confirm=true after the user approved the details.
        request_id: optional; reuse it when retrying so the event is never created twice."""
        args = {"date": date, "time": time, "duration_min": duration_min, "title": title,
                "description": description, "guests": guests or []}
        return await write("create_calendar_event", args, confirm=confirm, request_id=request_id)

    @server.tool(annotations=WRITE)
    async def send_email(to: str, subject: str, body: str, confirm: bool = False,
                         request_id: str | None = None) -> dict[str, Any]:
        """Send one email (one recipient) from the assistant's address; replies go to the owner.
        Requires confirm=true after the user approved recipient, subject and body."""
        return await write("send_email", {"to": to, "subject": subject, "body": body},
                           confirm=confirm, request_id=request_id)

    @server.resource("calendar://upcoming", name="upcoming_events", mime_type="application/json",
                     description=f"The owner's calendar events for the next {UPCOMING_DAYS} days (Asia/Tehran).")
    async def upcoming_events() -> str:
        async with session_scope() as s:
            try:
                user = await _owner(s)
            except OwnerMissing as exc:
                return json.dumps({"ok": False, "error": str(exc)})
            today = _now(user).date()
        days = []
        for i in range(UPCOMING_DAYS):
            day = (today + dt.timedelta(days=i)).isoformat()
            result = await read("list_calendar_events", {"date": day})
            if not result.get("ok"):
                return json.dumps({"ok": False, "error": result.get("error")}, ensure_ascii=False)
            days.append({"date": day, "events": result["events"]})
        return json.dumps({"ok": True, "timezone": user.timezone, "days": days}, ensure_ascii=False)

    return server


def default_server() -> MCPServer:
    from app.agent import pipeline

    return build_server(calendar_factory=pipeline.get_calendar, mailer_factory=pipeline.get_mailer)


def http_app(server: MCPServer, api_key: str):
    """Streamable HTTP app guarded by a static Bearer token (compared in constant time)."""
    import secrets

    from starlette.responses import JSONResponse

    inner = server.streamable_http_app(host="0.0.0.0")
    expected = f"Bearer {api_key}".encode()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not secrets.compare_digest(auth, expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.mcp.server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    settings = get_settings()
    # stdout carries the MCP protocol in stdio mode: logs must go to stderr.
    configure_logging("mcp", settings.log_level, settings.log_format)
    server = default_server()
    if not args.http:
        server.run("stdio")
        return

    key = settings.mcp_api_key.get_secret_value()
    if not key:
        raise SystemExit("MCP_API_KEY must be set for --http")
    import uvicorn

    uvicorn.run(http_app(server, key), host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
