"""MCP server tests through a real MCP client session (in-process), with fake calendar and email clients."""

import datetime as dt
import json

import pytest
from mcp import Client
from sqlalchemy import func, select

from app.db.models import AgentRun, AuditEvent, CalendarEvent, Email, ToolCall
from app.db.session import session_scope
from app.mcp.server import CONFIRM_REQUIRED, build_server, http_app, write_key
from tests.fakes import FakeCalendar, FakeMailer
from tests.test_agent import owner  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111111111")


@pytest.fixture
def fakes():
    return FakeCalendar(), FakeMailer()


@pytest.fixture
def server(fakes):
    cal, mail = fakes
    return build_server(calendar_factory=lambda: cal, mailer_factory=lambda: mail)


def payload(result) -> dict:
    assert not result.is_error, result
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def audits(event_type: str) -> int:
    async with session_scope() as s:
        return await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == event_type))


async def test_lists_exactly_four_tools_with_write_annotations(server, owner):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        resources = [str(r.uri) for r in (await c.list_resources()).resources]
    assert set(tools) == {"find_contact", "list_calendar_events", "create_calendar_event", "send_email"}
    assert tools["find_contact"].annotations.read_only_hint is True
    assert tools["send_email"].annotations.destructive_hint is True
    assert "confirm" in tools["send_email"].input_schema["properties"]
    assert resources == ["calendar://upcoming"]


async def test_find_contact_reuses_registry(server, owner):
    async with Client(server) as c:
        data = payload(await c.call_tool("find_contact", {"name": "علي"}))
    assert data["ok"] and data["matches"][0]["email"] == "ali.rezaei@example.com"


async def test_write_without_confirm_is_refused_and_audited(server, fakes, owner):
    cal, mail = fakes
    async with Client(server) as c:
        r1 = payload(await c.call_tool("send_email", {"to": "a@example.com", "subject": "s", "body": "b"}))
        r2 = payload(await c.call_tool("create_calendar_event", {"date": "2030-01-01", "time": "10:00",
                                                                  "duration_min": 30, "title": "t", "confirm": False}))
    assert r1 == {"ok": False, "error": CONFIRM_REQUIRED} and not r2["ok"]
    assert mail.sent == [] and cal.inserts == 0
    assert await audits("mcp.send_email.refused") == 1 and await audits("mcp.create_calendar_event.refused") == 1


async def test_confirmed_event_created_once_logged_and_audited(server, fakes, owner):
    cal, _ = fakes
    args = {"date": "2030-01-02", "time": "14:30", "duration_min": 45, "title": "جلسه",
            "guests": ["Ali.Rezaei@example.com"], "confirm": True, "request_id": "abc-1"}
    async with Client(server) as c:
        first = payload(await c.call_tool("create_calendar_event", args))
        second = payload(await c.call_tool("create_calendar_event", args))
    assert first["ok"] and first["run_id"] and second["duplicate"] is True and second["event_id"] == first["event_id"]
    assert cal.inserts == 1
    ev = next(iter(cal.events.values()))
    assert ev["start"].isoformat() == "2030-01-02T14:30:00+03:30" and ev["attendees"] == ["ali.rezaei@example.com"]
    async with session_scope() as s:
        run = await s.get(AgentRun, first["run_id"])
        calls = (await s.execute(select(ToolCall).where(ToolCall.run_id == run.id))).scalars().all()
        assert await s.scalar(select(func.count()).select_from(CalendarEvent)) == 1
    assert run.model == "mcp" and run.status == "executed"
    assert [c.status for c in calls] == ["succeeded"] and calls[0].latency_ms is not None
    assert await audits("mcp.create_calendar_event") == 1 and await audits("mcp.create_calendar_event.replayed") == 1


async def test_confirmed_email_sent_once_without_request_id(server, fakes, owner):
    _, mail = fakes
    args = {"to": "sara.mohammadi@example.com", "subject": "سلام", "body": "متن", "confirm": True}
    async with Client(server) as c:
        a = payload(await c.call_tool("send_email", args))
        b = payload(await c.call_tool("send_email", args))
        other = payload(await c.call_tool("send_email", {**args, "body": "متن دیگر"}))
    assert a["ok"] and b["duplicate"] and other["ok"] and len(mail.sent) == 2
    assert mail.sent[0]["reply_to"] == "owner@example.com"
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(Email)) == 2


async def test_invalid_arguments_rejected_by_shared_validation(server, fakes, owner):
    _, mail = fakes
    async with Client(server) as c:
        r = payload(await c.call_tool("send_email", {"to": "not-an-email", "subject": "s", "body": "b", "confirm": True}))
    assert not r["ok"] and "to" in r["error"] and mail.sent == []


async def test_upcoming_events_resource(server, fakes, owner):
    cal, _ = fakes
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3, minutes=30))).date()
    cal.existing = [{"title": "قبلی", "start": f"{(today + dt.timedelta(days=1)).isoformat()}T09:00:00+03:30", "end": "x"}]
    async with Client(server) as c:
        res = await c.read_resource("calendar://upcoming")
    data = json.loads(res.contents[0].text)
    assert data["ok"] and len(data["days"]) == 7
    assert sum(len(d["events"]) for d in data["days"]) == 1


async def test_calendar_not_configured_is_a_clean_error(owner):
    server = build_server(calendar_factory=lambda: None, mailer_factory=lambda: None)
    async with Client(server) as c:
        r = payload(await c.call_tool("list_calendar_events", {"date": "2030-01-01"}))
    assert not r["ok"] and "not configured" in r["error"]


def test_write_key_is_stable_and_specific():
    a = write_key("send_email", {"to": "x", "body": "1"}, None)
    assert a == write_key("send_email", {"body": "1", "to": "x"}, None)
    assert a != write_key("send_email", {"to": "x", "body": "2"}, None)
    assert write_key("send_email", {"to": "x"}, "r1") == write_key("send_email", {"to": "y"}, "r1")


async def test_http_transport_requires_bearer(server):
    import httpx

    app = http_app(server, "secret-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001") as c:
        assert (await c.post("/mcp", json={})).status_code == 401
        assert (await c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})).status_code == 401


async def test_stdio_subprocess_works_like_claude_desktop(owner):
    """Launch the real entry point as a subprocess and talk MCP over stdin/stdout."""
    import os
    import sys

    from mcp import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp.server"],
                                   env={**os.environ, "OWNER_TELEGRAM_ID": "111111111"}, cwd=os.getcwd())
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        found = payload(await c.call_tool("find_contact", {"name": "سارا"}))
    assert names == {"find_contact", "list_calendar_events", "create_calendar_event", "send_email"}
    assert found["matches"][0]["email"] == "sara.mohammadi@example.com"


async def test_missing_owner_is_a_clean_tool_error(db):
    server = build_server(calendar_factory=lambda: None, mailer_factory=lambda: None)
    async with Client(server) as c:
        r = payload(await c.call_tool("find_contact", {"name": "x"}))
    assert r == {"ok": False, "error": "owner not found: set OWNER_TELEGRAM_ID and run the seed"}
