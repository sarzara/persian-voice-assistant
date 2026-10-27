import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import CalendarEvent, Contact, MemoryFact, ToolCall, User
from app.db.session import session_scope
from app.tools import persian
from app.tools.google_calendar import CalendarAuthError, GoogleCalendarClient, event_id_for
from app.tools.registry import TOOLS, Exposure, ToolContext, invoke, schemas_for
from tests.fakes import FakeCalendar

TEHRAN = ZoneInfo("Asia/Tehran")
NOW = dt.datetime(2026, 10, 27, 9, 0, tzinfo=TEHRAN)  # Tuesday, 5 Aban 1405


# ---------------------------------------------------------------- pure ---

def test_persian_normalisation_matches_arabic_letters_zwnj_and_digits():
    assert persian.normalize("علي") == persian.normalize("علی")
    assert persian.normalize("آقای‌رضایی") == persian.normalize("آقای رضایی")
    assert persian.to_ascii_digits("ساعت ۱۵:۳۰") == "ساعت 15:30"
    assert persian.emails_in("به reza@Example.org بفرست") == {"reza@example.org"}


def test_jalali_conversions():
    assert persian.jalali_iso(dt.date(2026, 10, 27)) == "1405-08-05"
    assert persian.jalali_to_gregorian(1405, 8, 15) == dt.date(2026, 11, 6)
    assert persian.jalali_text(dt.date(2026, 10, 27)) == "سه‌شنبه ۵ آبان ۱۴۰۵"


def test_agent_sees_only_read_only_tools():
    names = {s["function"]["name"] for s in schemas_for(Exposure.AGENT)}
    assert names == {"get_current_datetime", "convert_jalali_date", "find_contact", "list_calendar_events"}
    assert {"send_email", "create_calendar_event", "save_contact", "remember"}.isdisjoint(names)


def test_every_tool_has_a_strict_json_schema():
    for tool in TOOLS.values():
        params = tool.openai_schema()["function"]["parameters"]
        assert params["type"] == "object" and params.get("additionalProperties") is False, tool.name
    send = TOOLS["send_email"].openai_schema()["function"]["parameters"]
    assert set(send["required"]) == {"to", "subject", "body"} and send["properties"]["to"]["type"] == "string"


def test_event_id_is_valid_base32hex_and_deterministic():
    eid = event_id_for("run-42")
    assert eid == event_id_for("run-42") != event_id_for("run-43")
    assert 5 <= len(eid) <= 1024 and set(eid) <= set("0123456789abcdefghijklmnopqrstuv")


# ------------------------------------------------------------ registry ---

async def _ctx(session, **kw) -> ToolContext:
    user = User(telegram_user_id=111111111, name="Sahar", email="owner@example.com")
    session.add(user)
    await session.flush()
    session.add_all([
        Contact(user_id=user.id, name="علی رضایی", aliases=["علی", "Ali Rezaei"], email="ali.rezaei@example.com"),
        Contact(user_id=user.id, name="سارا محمدی", aliases=["سارا", "Sara"], email="sara.mohammadi@example.com"),
    ])
    await session.flush()
    return ToolContext(session=session, user=user, settings=get_settings(), now=NOW, **kw)


async def test_find_contact_by_alias_and_arabic_spelling(db):
    async with session_scope() as s:
        ctx = await _ctx(s)
        r = await invoke("find_contact", {"name": "علي"}, ctx, allowed={Exposure.AGENT})
        assert r.ok and r.data["matches"][0]["email"] == "ali.rezaei@example.com"
        r = await invoke("find_contact", {"name": "Sara"}, ctx, allowed={Exposure.AGENT})
        assert r.data["matches"][0]["name"] == "سارا محمدی"
        r = await invoke("find_contact", {"name": "مهدی"}, ctx, allowed={Exposure.AGENT})
        assert r.ok and r.data["count"] == 0


async def test_agent_cannot_call_action_tools(db):
    async with session_scope() as s:
        ctx = await _ctx(s)
        r = await invoke("send_email", {"to": "a@example.com", "subject": "x", "body": "y"}, ctx, allowed={Exposure.AGENT})
        assert not r.ok and "not available" in r.error


async def test_validation_errors_are_reported_not_raised(db):
    async with session_scope() as s:
        ctx = await _ctx(s)
        r = await invoke("convert_jalali_date", {"jalali_date": "tomorrow"}, ctx, allowed={Exposure.AGENT})
        assert not r.ok and "jalali_date" in r.error
        r = await invoke("find_contact", {"name": "x", "extra": 1}, ctx, allowed={Exposure.AGENT})
        assert not r.ok and "extra" in r.error


async def test_tool_calls_logged_with_latency(db):
    from app.db.models import AgentRun

    async with session_scope() as s:
        run = AgentRun()
        s.add(run)
        await s.flush()
        ctx = await _ctx(s, run_id=run.id)
        await invoke("get_current_datetime", {}, ctx, allowed={Exposure.AGENT})
        await invoke("convert_jalali_date", {"jalali_date": "۱۴۰۵/۰۸/۱۵"}, ctx, allowed={Exposure.AGENT})
        rows = (await s.execute(select(ToolCall).order_by(ToolCall.id))).scalars().all()
    assert [r.tool_name for r in rows] == ["get_current_datetime", "convert_jalali_date"]
    assert rows[0].result_json["jalali_date"] == "1405-08-05" and rows[0].latency_ms is not None
    assert rows[1].result_json["gregorian_date"] == "2026-11-06"


async def test_create_event_in_tehran_time_and_idempotent(db):
    cal = FakeCalendar()
    async with session_scope() as s:
        ctx = await _ctx(s, calendar=cal, idempotency_key="run-1:execute")
        args = {"date": "2026-10-28", "time": "10:00", "duration_min": 30, "title": "جلسه با علی",
                "guests": ["ALI.rezaei@example.com"]}
        r1 = await invoke("create_calendar_event", args, ctx, allowed={Exposure.ACTION})
        r2 = await invoke("create_calendar_event", args, ctx, allowed={Exposure.ACTION})
        rows = (await s.execute(select(CalendarEvent))).scalars().all()
    assert r1.ok and not r1.data["duplicate"] and r2.data["duplicate"]
    assert len(cal.events) == 1 and len(rows) == 1
    ev = next(iter(cal.events.values()))
    assert ev["start"].isoformat() == "2026-10-28T10:00:00+03:30" and ev["timezone"] == "Asia/Tehran"
    assert ev["attendees"] == ["ali.rezaei@example.com"]


async def test_memory_tools_upsert(db):
    async with session_scope() as s:
        ctx = await _ctx(s)
        await invoke("remember", {"key": "default_meeting_minutes", "value": "45"}, ctx, allowed={Exposure.MEMORY})
        await invoke("remember", {"key": "default_meeting_minutes", "value": "60"}, ctx, allowed={Exposure.MEMORY})
        await invoke("save_contact", {"name": "رضا", "email": "Reza@Example.org"}, ctx, allowed={Exposure.MEMORY})
        facts = (await s.execute(select(MemoryFact))).scalars().all()
        reza = (await s.execute(select(Contact).where(Contact.name == "رضا"))).scalar_one()
    assert [(f.key, f.value) for f in facts] == [("default_meeting_minutes", "60")]
    assert reza.email == "reza@example.org"


# ---------------------------------------------------------- google http ---

def google_transport(log: list):
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.method == "POST":
            if sum(1 for r in log if r.method == "POST" and "events" in r.url.path) > 1:
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            return httpx.Response(200, json={"id": json.loads(request.content)["id"], "htmlLink": "https://cal/x"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": [
                {"summary": "قبلی", "start": {"dateTime": "2026-10-28T09:00:00+03:30"}, "end": {"dateTime": "2026-10-28T10:00:00+03:30"}},
                {"summary": "لغو", "status": "cancelled", "start": {}, "end": {}},
            ]})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})
    return httpx.MockTransport(handler)


async def test_google_insert_request_shape_and_409_replay():
    log: list[httpx.Request] = []
    client = GoogleCalendarClient(client_id="cid", client_secret="sec", refresh_token="rt",
                                  calendar_id="me@example.com", transport=google_transport(log))
    start = dt.datetime(2026, 10, 28, 10, 0, tzinfo=TEHRAN)
    kw = dict(event_id="pvaabc123", start=start, end=start + dt.timedelta(minutes=30),
              timezone="Asia/Tehran", title="جلسه", description="توضیح", attendees=["ali@example.com"])
    first = await client.insert_event(**kw)
    second = await client.insert_event(**kw)

    post = next(r for r in log if r.method == "POST" and "events" in r.url.path)
    body = json.loads(post.content)
    assert post.url.params["sendUpdates"] == "all"
    assert "/calendars/me%40example.com/events" in post.url.raw_path.decode()
    assert post.headers["authorization"] == "Bearer at-1"
    assert body["start"] == {"dateTime": "2026-10-28T10:00:00+03:30", "timeZone": "Asia/Tehran"}
    assert body["attendees"] == [{"email": "ali@example.com"}] and body["id"] == "pvaabc123"
    assert not first.duplicate and second.duplicate and second.id == "pvaabc123"
    assert sum(1 for r in log if r.url.host == "oauth2.googleapis.com") == 1  # token cached


async def test_google_list_events_skips_cancelled():
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r", transport=google_transport([]))
    events = await client.list_events(dt.date(2026, 10, 28), TEHRAN)
    assert events == [{"title": "قبلی", "start": "2026-10-28T09:00:00+03:30", "end": "2026-10-28T10:00:00+03:30"}]


async def test_google_revoked_refresh_token_gives_actionable_error():
    t = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r", transport=t)
    with pytest.raises(CalendarAuthError, match="google_auth.py"):
        await client.list_events(dt.date(2026, 10, 28), TEHRAN)
