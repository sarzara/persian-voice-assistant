"""Persian + English: the agent writes in the owner's language, guardrails check that language,
previews, buttons and bot messages follow it, and /language stores the setting."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agent import pipeline
from app.agent.actions import EmailAction, ScheduleAction, check_action
from app.agent.agent import Agent, AgentOutcome
from app.agent.context import AgentContext
from app.agent.evaluation import NOW
from app.agent.prompts import system_prompt
from app.bot import handlers, preview
from app.bot import messages as msg
from app.db.models import MemoryFact, User
from app.db.session import session_scope
from app.i18n import set_user_language
from tests.fakes import ScriptedProvider, tc

TZ = ZoneInfo("Asia/Tehran")
TOMORROW = (NOW + dt.timedelta(days=1)).date().isoformat()


def schedule(title, description=""):
    return ScheduleAction(type="schedule", date=TOMORROW, time="10:00", duration_min=30,
                          title=title, description=description)


def email(subject, body):
    return EmailAction(type="email", to={"name": "Sara", "email": "sara@example.com"}, subject=subject, body=body)


# ---------------------------------------------------------------- guardrails ---

def test_english_action_accepted_when_language_is_en():
    action = schedule("Team sync with Ali", "Weekly planning")
    assert check_action(action, allowed_emails=set(), now=NOW, tz=TZ, language="en") == []


def test_persian_action_rejected_when_language_is_en_and_vice_versa():
    problems = check_action(schedule("جلسه با علی"), allowed_emails=set(), now=NOW, tz=TZ, language="en")
    assert problems and "English" in problems[0]
    problems = check_action(schedule("Team sync"), allowed_emails=set(), now=NOW, tz=TZ)  # default: Persian
    assert problems and "Persian" in problems[0]


def test_other_guardrails_still_apply_in_english():
    action = email("Report", "Hi Sara, the report is ready. Best, Sahar")
    problems = check_action(action, allowed_emails=set(), now=NOW, tz=TZ, language="en")
    assert any("invent" in p for p in problems)  # address not allowed, whatever the language


def test_prompt_v2_uses_context_language():
    text = system_prompt()
    assert "CONTEXT.language" in text and "English" in text
    assert "Persian, whatever the language of the request" not in text


def test_context_renders_language():
    assert '"language": "en"' in AgentContext(now=NOW, language="en").render()


# -------------------------------------------------------------- agent (db) ---

@pytest.fixture
async def owner(db):
    async with session_scope() as s:
        user = User(telegram_user_id=111111111, name="Sahar", email="owner@example.com")
        s.add(user)
        await s.flush()
    return user


async def test_agent_english_run_passes_guardrails_and_carries_language(owner):
    provider = ScriptedProvider([
        [tc("submit_schedule_action", {"date": TOMORROW, "time": "10:00", "duration_min": 30, "title": "جلسه تیم"})],
        [tc("submit_schedule_action", {"date": TOMORROW, "time": "10:00", "duration_min": 30, "title": "Team sync"})],
    ])
    outcome = await Agent(provider).run(user=owner, message_id=None, transcript="meeting tomorrow at 10",
                                        context=AgentContext(now=NOW, language="en"))
    assert "English" in provider.calls[1][-1]["content"]  # the Persian title was sent back
    assert outcome.kind == "action" and outcome.action.title == "Team sync" and outcome.language == "en"


async def test_reply_language_follows_setting_or_message(owner):
    assert await pipeline.reply_language(owner, "Book a meeting with Ali tomorrow") == "en"  # auto
    assert await pipeline.reply_language(owner, "فردا با علی جلسه بگذار") == "fa"
    async with session_scope() as s:
        await set_user_language(s, owner.id, "fa")
    assert await pipeline.reply_language(owner, "Book a meeting with Ali tomorrow") == "fa"


async def test_language_setting_is_not_shown_to_the_model_as_a_preference(owner):
    from app.agent import memory

    async with session_scope() as s:
        await set_user_language(s, owner.id, "en")
    ctx = await memory.build_context(owner, "hello", now=NOW, language="en")
    assert "language" not in ctx.preferences and ctx.language == "en"


# ------------------------------------------------------------------- preview ---

def test_english_preview_buttons_and_result():
    action = schedule("Team sync with Ali")
    text = preview.render_preview(action, lang="en")
    assert "Meeting preview" in text and "Go ahead?" in text and "تاریخ" not in text
    labels = [b.text for b in preview.keyboard(7, "en").inline_keyboard[0]]
    assert labels == ["✅ Confirm", "✏️ Edit", "❌ Cancel"]
    assert preview.render_executed(action, {}).startswith("✅ The meeting")       # language from the action
    assert preview.render_executed(schedule("جلسه تیم"), {}).startswith("✅ جلسه")
    assert preview.render_failure("calendar not configured", "en").startswith("❌ Not done.")


def test_english_email_preview_shows_full_body():
    body = "Hi Sara,\nThe report is ready.\nBest, Sahar"
    text = preview.render_preview(email("Report", body), lang="en")
    assert "Email preview" in text and body in text


def test_outcome_message_uses_outcome_language():
    out = AgentOutcome("failed", 3, language="en")
    assert preview.outcome_message(out)[0] == msg.t("AGENT_FAILED", "en")
    out = AgentOutcome("action", 4, action=schedule("Team sync"), language="en")
    text, markup = preview.outcome_message(out)
    assert "Meeting preview" in text and markup.inline_keyboard[0][0].text == "✅ Confirm"


# ---------------------------------------------------------------- bot (db) ---

def _update(text=None):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=111111111, full_name="Sahar"),
                           effective_chat=SimpleNamespace(type="private"), effective_message=message)


async def test_language_command_sets_and_shows_setting(owner):
    update = _update()
    await handlers.language_command(update, SimpleNamespace(args=["en"]))
    assert update.effective_message.reply_text.await_args.args[0] == msg.t("LANGUAGE_SET", "en", name="English")
    async with session_scope() as s:
        value = await s.scalar(select(MemoryFact.value).where(MemoryFact.user_id == owner.id,
                                                              MemoryFact.key == "language"))
    assert value == "en"

    update = _update()
    await handlers.language_command(update, SimpleNamespace(args=[]))
    assert "English" in update.effective_message.reply_text.await_args.args[0]  # shows current, in English

    update = _update()
    await handlers.language_command(update, SimpleNamespace(args=["auto"]))  # still English until auto applies
    assert update.effective_message.reply_text.await_args.args[0] == msg.t("LANGUAGE_SET", "en", name="automatic")


async def test_help_follows_language_setting(owner):
    async with session_scope() as s:
        await set_user_language(s, owner.id, "en")
    update = _update()
    await handlers.help_command(update, None)
    assert update.effective_message.reply_text.await_args.args[0] == msg.t("HELP", "en")


async def test_buttons_answer_in_language_of_the_preview(db, monkeypatch):
    from app.agent.executor import ExecResult, ExecStatus

    async def fake_reject(*a, **k):
        return ExecResult(ExecStatus.NOT_FOUND)

    monkeypatch.setattr(handlers.executor, "reject_run", fake_reject)
    reply = AsyncMock()
    query = SimpleNamespace(data="run:9:cancel", answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=SimpleNamespace(text="📅 Meeting preview\n• Title: Team sync", reply_text=reply))
    await handlers.on_callback(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1)), None)
    assert reply.await_args.args[0] == msg.t("RUN_NOT_FOUND", "en")


# ------------------------------------------------------- prompt v3 / schema ---

def test_prompt_v3_tells_model_to_write_subject_and_treat_tell_as_email():
    text = system_prompt()
    assert "Never ask the owner for a subject" in text
    assert "is ONE email" in text


def test_email_recipient_given_as_plain_address_is_accepted():
    from app.agent.actions import validate_terminal

    action = validate_terminal("submit_email_action",
                               {"to": "Zaree.Sahar@Gmail.com", "subject": "Test", "body": "Hi, this is a test."})
    assert action.to.email == "zaree.sahar@gmail.com" and action.to.name is None
