"""Quality signals (run_quality view) and 👍/👎 feedback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.evaluation import ALI, NOW, SAMPLES
from app.agent.executor import confirm_and_execute, reject_run
from app.bot import handlers, preview
from app.db.models import RunFeedback
from app.db.session import session_scope
from tests.fakes import FakeCalendar, ScriptedProvider, tc
from tests.test_agent import owner  # noqa: F401  (fixture)

OWNER_TG = 111111111


async def run(owner, turns, transcript="x"):
    return await Agent(ScriptedProvider(turns)).run(user=owner, message_id=None, transcript=transcript,
                                                    context=AgentContext(now=NOW))


async def quality(run_id):
    async with session_scope() as s:
        return (await s.execute(text("SELECT * FROM run_quality WHERE run_id = :r"), {"r": run_id})).mappings().one()


async def test_signals_for_confirmed_edited_cancelled_and_question(owner):
    confirmed = await run(owner, SAMPLES[0].script, SAMPLES[0].transcript)
    await confirm_and_execute(confirmed.run_id, OWNER_TG, calendar=FakeCalendar(), mailer=None)
    edited = await run(owner, SAMPLES[0].script, SAMPLES[0].transcript)
    await reject_run(edited.run_id, OWNER_TG, "edit_requested")
    cancelled = await run(owner, SAMPLES[3].script, SAMPLES[3].transcript)
    await reject_run(cancelled.run_id, OWNER_TG, "cancelled")
    question = await run(owner, SAMPLES[5].script, SAMPLES[5].transcript)

    q = await quality(confirmed.run_id)
    assert q["decision"] == "confirmed" and q["exec_ok"] is True and q["steps"] == 2 and q["tool_calls"] == 2
    assert (await quality(edited.run_id))["decision"] == "edited"
    assert (await quality(cancelled.run_id))["decision"] == "cancelled"
    qq = await quality(question.run_id)
    assert qq["decision"] is None and qq["asked_question"] is True


async def test_json_failures_vs_guardrail_rejections(owner):
    out = await run(owner, [
        [tc("submit_email_action", {"to": "not-an-object", "subject": "س", "body": "م"})],          # schema
        [tc("submit_email_action", {"to": {"email": "x@invented.example"}, "subject": "س", "body": "م"})],  # guardrail
        [tc("submit_email_action", {"to": {"email": ALI}, "subject": "سلام", "body": "متن"})],
    ])
    q = await quality(out.run_id)
    assert (q["terminal_calls"], q["json_failures"], q["guardrail_rejections"]) == (3, 1, 1)
    assert q["decision"] == "pending"


async def test_failed_execution_still_counts_as_confirmed(owner):
    out = await run(owner, SAMPLES[0].script, SAMPLES[0].transcript)
    await confirm_and_execute(out.run_id, OWNER_TG, calendar=None, mailer=None)  # fails: no calendar
    q = await quality(out.run_id)
    assert q["status"] == "failed" and q["decision"] == "confirmed" and q["exec_ok"] is False


async def test_feedback_button_upserts_one_rating(owner):
    out = await run(owner, SAMPLES[5].script, SAMPLES[5].transcript)
    for data in (f"fb:{out.run_id}:up", f"fb:{out.run_id}:down"):
        query = SimpleNamespace(data=data, answer=AsyncMock())
        await handlers.on_feedback(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=OWNER_TG)), None)
    async with session_scope() as s:
        rows = (await s.execute(select(RunFeedback))).scalars().all()
    assert [(r.run_id, r.rating) for r in rows] == [(out.run_id, -1)]
    assert (await quality(out.run_id))["rating"] == -1


async def test_feedback_on_someone_elses_run_refused(owner):
    out = await run(owner, SAMPLES[5].script, SAMPLES[5].transcript)
    query = SimpleNamespace(data=f"fb:{out.run_id}:up", answer=AsyncMock())
    await handlers.on_feedback(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999)), None)
    async with session_scope() as s:
        assert (await s.execute(select(RunFeedback))).first() is None


def test_feedback_callback_parsing():
    assert preview.parse_feedback("fb:12:up") == (12, 1) and preview.parse_feedback("fb:12:down") == (12, -1)
    assert preview.parse_feedback("fb:x:up") is None and preview.parse_feedback("fb:1:meh") is None
    rows = preview.keyboard(5).inline_keyboard
    assert [b.callback_data for b in rows[1]] == ["fb:5:up", "fb:5:down"]
