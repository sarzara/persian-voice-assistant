import asyncio

import pytest
from sqlalchemy import func, select, update

from app.agent import pipeline
from app.agent.actions import EmailAction, ScheduleAction
from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.executor import ExecStatus, confirm_and_execute, execution_key, reject_run
from app.bot import preview
from app.db.models import AgentRun, CalendarEvent, Email, ToolCall
from app.db.session import session_scope
from app.agent.evaluation import NOW, SAMPLES
from tests.fakes import FakeCalendar, FakeMailer, ScriptedProvider
from tests.test_agent import owner  # noqa: F401  (fixture)

OWNER_TG = 111111111
SCHEDULE = SAMPLES[0]   # tomorrow 10:00 with Ali
EMAIL = SAMPLES[3]      # email to Sara


async def propose(owner, sample):
    agent = Agent(ScriptedProvider(sample.script))
    out = await agent.run(user=owner, message_id=None, transcript=sample.transcript, context=AgentContext(now=NOW))
    assert out.kind == "action"
    return out


async def test_double_tap_creates_exactly_one_event(owner):
    out = await propose(owner, SCHEDULE)
    cal = FakeCalendar()
    results = await asyncio.gather(*[confirm_and_execute(out.run_id, OWNER_TG, calendar=cal, mailer=None) for _ in range(3)])

    statuses = sorted(r.status.value for r in results)
    assert statuses == ["already_done", "already_done", "executed"]
    assert cal.inserts == 1 and len(cal.events) == 1
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(CalendarEvent)) == 1
        run = await s.get(AgentRun, out.run_id)
        exec_call = (await s.execute(select(ToolCall).where(ToolCall.idempotency_key == execution_key(out.run_id)))).scalar_one()
    assert run.status == "executed" and exec_call.status == "succeeded" and exec_call.latency_ms is not None
    ev = next(iter(cal.events.values()))
    assert ev["attendees"] == ["ali.rezaei@example.com"] and ev["start"].isoformat() == "2026-10-28T10:00:00+03:30"

    later = await confirm_and_execute(out.run_id, OWNER_TG, calendar=cal, mailer=None)
    assert later.status is ExecStatus.ALREADY_DONE and cal.inserts == 1


async def test_email_sent_once_with_idempotency_key_and_reply_to(owner):
    out = await propose(owner, EMAIL)
    mailer = FakeMailer()
    r1 = await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=mailer)
    r2 = await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=mailer)
    assert (r1.status, r2.status) == (ExecStatus.EXECUTED, ExecStatus.ALREADY_DONE)
    assert len(mailer.sent) == 1 and mailer.keys == {execution_key(out.run_id)}
    assert mailer.sent[0]["to"] == "sara.mohammadi@example.com" and mailer.sent[0]["reply_to"] == "owner@example.com"
    async with session_scope() as s:
        row = (await s.execute(select(Email))).scalar_one()
    assert row.status == "sent" and row.provider_message_id and row.run_id == out.run_id


async def test_cancel_then_confirm_does_nothing(owner):
    out = await propose(owner, EMAIL)
    mailer = FakeMailer()
    assert (await reject_run(out.run_id, OWNER_TG, "cancelled")).status is ExecStatus.CANCELLED
    assert (await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=mailer)).status is ExecStatus.CANCELLED
    assert mailer.sent == []


async def test_other_user_cannot_confirm(owner):
    out = await propose(owner, EMAIL)
    mailer = FakeMailer()
    assert (await confirm_and_execute(out.run_id, 999, calendar=None, mailer=mailer)).status is ExecStatus.NOT_FOUND
    assert mailer.sent == []


async def test_stale_preview_expires(owner):
    out = await propose(owner, EMAIL)
    async with session_scope() as s:
        await s.execute(update(AgentRun).where(AgentRun.id == out.run_id)
                        .values(created_at=func.now() - func.make_interval(0, 0, 0, 0, 2)))
    mailer = FakeMailer()
    assert (await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=mailer)).status is ExecStatus.EXPIRED
    assert mailer.sent == []


async def test_provider_failure_marks_run_failed(owner):
    out = await propose(owner, SCHEDULE)
    result = await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=None)
    assert result.status is ExecStatus.FAILED and "not configured" in result.error
    async with session_scope() as s:
        assert (await s.get(AgentRun, out.run_id)).status == "failed"
    assert "تنظیم نشده" in preview.render_failure(result.error)


async def test_edit_feeds_previous_action_into_next_message_once(owner):
    out = await propose(owner, SCHEDULE)
    await reject_run(out.run_id, OWNER_TG, "edit_requested")
    first = await pipeline.take_pending_edit(owner)
    assert first["type"] == "schedule" and first["time"] == "10:00:00"
    assert await pipeline.take_pending_edit(owner) is None


def test_preview_is_readable_persian():
    action = ScheduleAction.model_validate({
        "date": "2026-10-28", "time": "10:00", "duration_min": 45, "title": "جلسه با علی",
        "guests": [{"name": "علی رضایی", "email": "ali.rezaei@example.com"}], "warnings": ["با جلسهٔ دیگری تداخل دارد"]})
    text = preview.render_preview(action)
    assert "چهارشنبه ۶ آبان ۱۴۰۵" in text and "۱۰:۰۰ تا ۱۰:۴۵" in text and "۴۵ دقیقه" in text
    assert "علی رضایی ‹ali.rezaei@example.com›" in text and "⚠️" in text and text.endswith("انجام شود؟")

    email = EmailAction.model_validate({"to": {"email": "x@example.com"}, "subject": "موضوع", "body": "متن " * 750})
    text = preview.render_preview(email)
    assert len(text) < 4096 and email.body in text  # full body shown, within Telegram's limit


def test_callback_data_roundtrip_and_rejects_garbage():
    kb = preview.keyboard(42)
    datas = [b.callback_data for b in kb.inline_keyboard[0]]
    assert [preview.parse_callback(d) for d in datas] == [(42, "confirm"), (42, "edit"), (42, "cancel")]
    assert all(len(d.encode()) <= 64 for d in datas)  # Telegram limit
    for bad in ["", "run:x:confirm", "run:1:delete", "other:1:confirm", "run:1"]:
        assert preview.parse_callback(bad) is None
