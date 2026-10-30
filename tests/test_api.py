import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.agent import pipeline
from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.evaluation import NOW, SAMPLES
from app.api import health
from app.bot import notify
from app.db.models import AgentRun, CalendarEvent, Email, VoiceMessage
from app.db.session import session_scope
from tests.fakes import FakeCalendar, ScriptedProvider, tc
from tests.test_agent import owner  # noqa: F401  (fixture)

KEY = "test-admin-key-0123456789"
H = {"X-Admin-Key": KEY}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", KEY)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111111111")
    monkeypatch.setenv("DEBUG", "false")
    health._cache.clear()


def client(debug: bool = False, monkeypatch=None) -> AsyncClient:
    from app.api.main import create_app

    return AsyncClient(transport=ASGITransport(app=create_app(), raise_app_exceptions=False), base_url="http://t")


# ------------------------------------------------------------------ auth ---

@pytest.mark.parametrize("headers", [{}, {"X-Admin-Key": "wrong"}, {"X-Admin-Key": ""}])
@pytest.mark.parametrize("path", ["/runs", "/runs/1", "/events", "/emails", "/contacts"])
async def test_admin_endpoints_require_key(path, headers):
    async with client() as c:
        r = await c.get(path, headers=headers)
    assert r.status_code == 401 and r.json()["detail"] == "missing or invalid X-Admin-Key"


async def test_admin_disabled_without_configured_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    async with client() as c:
        assert (await c.get("/runs", headers={"X-Admin-Key": ""})).status_code == 503


async def test_retry_and_post_also_protected():
    async with client() as c:
        assert (await c.post("/runs/1/retry")).status_code == 401
        assert (await c.post("/contacts", json={"name": "x"})).status_code == 401


async def test_docs_only_in_debug(monkeypatch):
    async with client() as c:
        assert (await c.get("/docs")).status_code == 404 and (await c.get("/openapi.json")).status_code == 404
    monkeypatch.setenv("DEBUG", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    async with client() as c:
        assert (await c.get("/openapi.json")).status_code == 200


# ---------------------------------------------------------------- health ---

async def test_health_reports_each_dependency(monkeypatch, db):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    from app.config import get_settings

    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "api.telegram.org" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_bot"}})
        return httpx.Response(500)  # LLM down

    checks = await health.readiness(httpx.MockTransport(handler))
    health._cache.clear()

    def models_handler(request):
        if "api.telegram.org" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_bot"}})
        return httpx.Response(200, json={"data": [{"id": "deepseek-v4-pro"}]})

    retired = (await health.readiness(httpx.MockTransport(models_handler)))["llm"]
    assert not retired.ok and "not offered" in retired.detail and "deepseek-v4-pro" in retired.detail
    assert checks["database"].ok and checks["telegram"].ok and checks["telegram"].detail == "@my_bot"
    assert not checks["llm"].ok and checks["llm"].detail == "HTTP 500"
    assert "sk-test" not in str(checks) and "123:abc" not in str(checks)


async def test_health_endpoint_503_when_degraded(db):
    async with client() as c:
        r = await c.get("/health")  # no tokens configured in tests -> telegram/llm not ok
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["checks"]["database"]["ok"]
    async with client() as c:
        assert (await c.get("/health/live")).json() == {"status": "ok"}


# ------------------------------------------------------------------ data ---

async def make_runs(owner):
    ids = {}
    for sample in SAMPLES[:6]:
        async with session_scope() as s:
            vm = VoiceMessage(user_id=owner.id, source="text", transcript=sample.transcript, stt_confidence=1.0,
                              audio_path="/data/voice/secret.ogg")
            s.add(vm)
            await s.flush()
            vm_id = vm.id
        out = await Agent(ScriptedProvider(sample.script)).run(user=owner, message_id=vm_id,
                                                               transcript=sample.transcript, context=AgentContext(now=NOW))
        ids[sample.id] = out.run_id
    return ids


async def test_runs_filters_and_pagination(owner):
    ids = await make_runs(owner)
    async with client() as c:
        all_ = (await c.get("/runs", headers=H)).json()
        assert all_["total"] == 6 and all_["limit"] == 20 and [r["id"] for r in all_["items"]] == sorted(ids.values(), reverse=True)

        page = (await c.get("/runs", headers=H, params={"limit": 2, "offset": 2})).json()
        assert page["total"] == 6 and len(page["items"]) == 2

        sched = (await c.get("/runs", headers=H, params={"intent": "schedule"})).json()
        assert sched["total"] == 3 and {r["intent"] for r in sched["items"]} == {"schedule"}

        q = (await c.get("/runs", headers=H, params={"status": "needs_clarification"})).json()
        assert q["total"] == 1 and q["items"][0]["id"] == ids["missing_time"]

        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        assert (await c.get("/runs", headers=H, params={"since": future})).json()["total"] == 0

        for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "bogus"},
                    {"since": "2026-10-28T00:00:00Z", "until": "2026-10-27T00:00:00Z"}):
            assert (await c.get("/runs", headers=H, params=bad)).status_code == 422, bad


async def test_run_detail_has_transcript_action_tool_calls_but_no_audio(owner):
    ids = await make_runs(owner)
    async with client() as c:
        r = await c.get(f"/runs/{ids['schedule_tomorrow']}", headers=H)
        missing = await c.get("/runs/999999", headers=H)
    body = r.json()
    assert r.status_code == 200 and body["transcript"] == SAMPLES[0].transcript and body["source"] == "text"
    assert body["action_json"]["date"] == "2026-10-28"
    assert [t["tool_name"] for t in body["tool_calls"]] == ["get_current_datetime", "find_contact", "submit_schedule_action"]
    assert "audio_path" not in r.text and "secret.ogg" not in r.text
    assert missing.status_code == 404


async def test_events_and_emails_filters(owner):
    async with session_scope() as s:
        s.add_all([
            CalendarEvent(date=dt.date(2026, 11, 1), time=dt.time(10), duration_min=30, title="الف", guests=[]),
            CalendarEvent(date=dt.date(2026, 11, 9), time=dt.time(11), duration_min=60, title="ب", guests=["a@example.com"]),
            Email(to_address="a@example.com", subject="s", body="b", status="sent"),
            Email(to_address="b@example.com", subject="s", body="b", status="failed"),
        ])
    async with client() as c:
        ev = (await c.get("/events", headers=H, params={"date_from": "2026-11-05"})).json()
        assert ev["total"] == 1 and ev["items"][0]["title"] == "ب"
        assert (await c.get("/emails", headers=H, params={"status": "failed"})).json()["items"][0]["to_address"] == "b@example.com"
        assert (await c.get("/emails", headers=H, params={"to": "A@example.com"})).json()["total"] == 1


async def test_contacts_list_search_create_and_errors(owner):
    async with client() as c:
        assert (await c.get("/contacts", headers=H)).json()["total"] == 2
        found = (await c.get("/contacts", headers=H, params={"q": "Sara"})).json()
        assert [x["name"] for x in found["items"]] == ["سارا محمدی"]

        r = await c.post("/contacts", headers=H, json={"name": "رضا کریمی", "email": "Reza@Example.org", "aliases": ["رضا", "Reza", "رضا"]})
        assert r.status_code == 201 and r.json()["email"] == "reza@example.org"
        assert r.json()["aliases"] == ["رضا کریمی", "رضا", "Reza"]

        assert (await c.post("/contacts", headers=H, json={"name": "رضا کریمی"})).status_code == 409
        assert (await c.post("/contacts", headers=H, json={"name": "x", "email": "not-an-email"})).status_code == 422
        assert (await c.post("/contacts", headers=H, json={"name": "x", "role": "admin"})).status_code == 422
        assert (await c.get("/contacts", headers=H, params={"user_id": 999999})).status_code == 404


# ----------------------------------------------------------------- retry ---

async def test_retry_refuses_a_meeting_whose_time_has_passed(owner):
    from app.agent.executor import confirm_and_execute

    ids = await make_runs(owner)  # fixed-clock samples: "tomorrow" = 2026-10-28, already in the past
    run_id = ids["schedule_tomorrow"]
    await confirm_and_execute(run_id, 111111111, calendar=None, mailer=None)
    async with client() as c:
        r = await c.post(f"/runs/{run_id}/retry", headers=H)
    if dt.datetime.now(ZoneInfo("Asia/Tehran")) > dt.datetime(2026, 10, 28, 10, 0, tzinfo=ZoneInfo("Asia/Tehran")):
        assert r.status_code == 409 and "passed" in r.json()["detail"]


async def test_retry_rejects_non_failed_and_unknown(owner):
    ids = await make_runs(owner)
    async with client() as c:
        assert (await c.post(f"/runs/{ids['schedule_tomorrow']}/retry", headers=H)).status_code == 409
        assert (await c.post("/runs/999999/retry", headers=H)).status_code == 404


async def test_retry_failed_execution_runs_once_and_notifies(owner, monkeypatch):
    from app.agent.executor import confirm_and_execute

    # A meeting 30 days from the REAL today: retry refuses meetings whose time has already passed,
    # so a fixed calendar date here would make this test fail once that date is reached.
    real_now = dt.datetime.now(ZoneInfo("Asia/Tehran"))
    day = (real_now + dt.timedelta(days=30)).date().isoformat()
    out = await Agent(ScriptedProvider([[tc("submit_schedule_action", {
        "date": day, "time": "10:00", "duration_min": 30, "title": "جلسه با علی",
        "guests": [{"email": "ali.rezaei@example.com"}]})]])).run(
        user=owner, message_id=None, transcript="جلسه", context=AgentContext(now=real_now))
    run_id = out.run_id
    failed = await confirm_and_execute(run_id, 111111111, calendar=None, mailer=None)  # calendar not configured
    assert failed.status.value == "failed"

    cal, sent = FakeCalendar(), []
    monkeypatch.setattr(pipeline, "get_calendar", lambda: cal)
    monkeypatch.setattr(pipeline, "get_mailer", lambda: None)

    async def fake_send_text(chat_id, text):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(notify, "send_text", fake_send_text)
    async with client() as c:
        r = await c.post(f"/runs/{run_id}/retry", headers=H)
        again = await c.post(f"/runs/{run_id}/retry", headers=H)
    assert r.status_code == 200 and r.json()["kind"] == "execution" and r.json()["outcome"] == "executed"
    assert r.json()["notified"] and sent[0][0] == 111111111 and "✅" in sent[0][1]
    assert cal.inserts == 1 and again.status_code == 409  # now executed: nothing to retry


async def test_retry_failed_planning_replans_and_sends_preview(owner, monkeypatch):
    async with session_scope() as s:
        vm = VoiceMessage(user_id=owner.id, source="text", transcript="به سارا ایمیل بزن", stt_confidence=1.0)
        s.add(vm)
        await s.flush()
        run = AgentRun(user_id=owner.id, voice_message_id=vm.id, status="failed", intent="unknown", error="llm_error")
        s.add(run)
        await s.flush()
        run_id = run.id

    agent = Agent(ScriptedProvider([[tc("ask_user", {"question": "چه چیزی بنویسم؟"})]]))
    monkeypatch.setattr(pipeline, "get_agent", lambda: agent)
    pushed = []

    async def fake_send_outcome(chat_id, outcome):
        pushed.append((chat_id, outcome.kind))
        return True

    monkeypatch.setattr(notify, "send_outcome", fake_send_outcome)
    async with client() as c:
        r = await c.post(f"/runs/{run_id}/retry", headers=H)
    body = r.json()
    assert r.status_code == 200 and body["kind"] == "agent" and body["outcome"] == "question"
    assert body["new_run_id"] and body["new_run_id"] != run_id and pushed == [(111111111, "question")]


async def test_unhandled_errors_are_generic(monkeypatch, db):
    from app.api import admin

    async def boom(*a, **k):
        raise RuntimeError("password=hunter2 leaked?")

    monkeypatch.setattr(admin, "run_detail", boom)
    async with client() as c:
        r = await c.get("/runs/1", headers=H)
    assert r.status_code == 500 and r.json()["detail"] == "internal error" and "hunter2" not in r.text
