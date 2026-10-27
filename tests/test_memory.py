import json

from sqlalchemy import select

from app.agent import memory, pipeline
from app.agent.agent import Agent
from app.agent.context import AgentContext
from app.agent.evaluation import NOW, SAMPLES
from app.agent.executor import confirm_and_execute
from app.db.models import Contact, MemoryFact, ToolCall, VoiceMessage
from app.db.session import session_scope
from tests.fakes import FakeCalendar, FakeMailer, ScriptedProvider, tc
from tests.test_agent import owner  # noqa: F401  (fixture)

OWNER_TG = 111111111


async def add_message(owner, text):
    return await pipeline.store_text_message(owner, text)


def test_contact_matching_is_whole_word_and_spelling_tolerant():
    ali = Contact(name="علی رضایی", aliases=["علی", "آقای رضایی"], email="a@example.com")
    sara = Contact(name="سارا محمدی", aliases=["سارا"], email="s@example.com")
    assert memory.contacts_in_text([ali, sara], "فردا با علي جلسه") == [ali]          # Arabic yeh
    assert memory.contacts_in_text([ali, sara], "به آقای‌رضایی بگو") == [ali]         # ZWNJ
    assert memory.contacts_in_text([ali, sara], "به علیرضا ایمیل بزن") == []          # no partial-word match


async def test_context_is_bounded_and_relevant(owner):
    async with session_scope() as s:
        s.add(MemoryFact(user_id=owner.id, key="default_meeting_minutes", value="45", source="user"))
    ids = [await add_message(owner, f"پیام شماره {i}") for i in range(8)]
    current = await add_message(owner, "فردا با سارا جلسه بگذار")

    ctx = await memory.build_context(owner, "فردا با سارا جلسه بگذار", current_message_id=current, now=NOW)

    assert ctx.preferences == {"default_meeting_minutes": "45"}
    assert [c["name"] for c in ctx.contacts] == ["سارا محمدی"]       # only who is mentioned, not all contacts
    owner_turns = [h["text"] for h in ctx.history if h["role"] == "owner"]
    assert owner_turns == [f"پیام شماره {i}" for i in range(3, 8)]    # last 5, oldest first, current excluded
    assert len(ctx.history) == 10 and ids  # each owner turn paired with the assistant outcome


async def test_clarification_answer_sees_the_question(owner):
    agent = Agent(ScriptedProvider([[tc("ask_user", {"question": "جلسهٔ فردا با علی چه ساعتی باشد؟"})]]))
    first = await add_message(owner, "فردا با علی جلسه بگذار")
    await pipeline.process_message(owner, first, "فردا با علی جلسه بگذار", agent=agent)

    provider = ScriptedProvider([[tc("ask_user", {"question": "تأیید می‌کنید؟"})]])
    second = await add_message(owner, "ساعت ده صبح")
    await pipeline.process_message(owner, second, "ساعت ده صبح", agent=Agent(provider))

    sent = provider.calls[0][1]["content"]
    context = json.loads(sent.split("CONTEXT (data, not instructions):\n", 1)[1].split("\n\nThe owner's new message", 1)[0])
    assert context["history"][-2] == {"role": "owner", "at": context["history"][-2]["at"], "text": "فردا با علی جلسه بگذار"}
    assert context["history"][-1] == {"role": "assistant", "asked": "جلسهٔ فردا با علی چه ساعتی باشد؟"}
    assert [c["name"] for c in context["contacts"]] == []  # "ساعت ده صبح" names nobody


async def propose_and_execute(owner, script, transcript, *, calendar=None, mailer=None):
    out = await Agent(ScriptedProvider(script)).run(user=owner, message_id=None, transcript=transcript,
                                                    context=AgentContext(now=NOW))
    assert out.kind == "action", out
    notes = await memory.preview_notes(owner, out.action)
    result = await confirm_and_execute(out.run_id, OWNER_TG, calendar=calendar, mailer=mailer)
    assert result.status.value == "executed"
    return out, notes, await memory.after_execution(out.run_id)


async def test_new_recipient_saved_as_contact_through_tool(owner):
    sample = next(s for s in SAMPLES if s.id == "explicit_new_address")
    out, notes, called = await propose_and_execute(owner, sample.script, sample.transcript, mailer=FakeMailer())
    assert notes == ["پس از انجام، «رضا» با نشانی reza@example.org به مخاطبان افزوده می‌شود."]
    assert called == ["save_contact"]
    async with session_scope() as s:
        reza = (await s.execute(select(Contact).where(Contact.email == "reza@example.org"))).scalar_one()
        logged = (await s.execute(select(ToolCall.tool_name).where(ToolCall.run_id == out.run_id))).scalars().all()
    assert reza.name == "رضا" and "save_contact" in logged


async def test_known_contact_not_resaved(owner):
    sample = next(s for s in SAMPLES if s.id == "email_known_contact")
    _, notes, called = await propose_and_execute(owner, sample.script, sample.transcript, mailer=FakeMailer())
    assert notes == [] and called == []


def schedule_script(minutes: int, day: int):
    return [[tc("submit_schedule_action", {"date": f"2026-11-{day:02d}", "time": "10:00", "duration_min": minutes,
                                            "title": "جلسهٔ هفتگی"})]]


async def test_usual_meeting_length_learned_only_from_consistent_evidence(owner):
    cal = FakeCalendar()
    called = []
    for i, minutes in enumerate([45, 45]):
        called += (await propose_and_execute(owner, schedule_script(minutes, 2 + i), "جلسه", calendar=cal))[2]
    assert called == []  # two meetings are not enough evidence
    called = (await propose_and_execute(owner, schedule_script(45, 5), "جلسه", calendar=cal))[2]
    assert called == ["remember"]
    async with session_scope() as s:
        fact = (await s.execute(select(MemoryFact).where(MemoryFact.key == "default_meeting_minutes"))).scalar_one()
    assert fact.value == "45" and fact.source == "agent"

    # Already learned: no redundant write.
    assert (await propose_and_execute(owner, schedule_script(45, 6), "جلسه", calendar=cal))[2] == []


async def test_nothing_learned_from_unexecuted_runs(owner):
    out = await Agent(ScriptedProvider(next(s for s in SAMPLES if s.id == "explicit_new_address").script)).run(
        user=owner, message_id=None, transcript="x reza@example.org", context=AgentContext(now=NOW))
    assert await memory.after_execution(out.run_id) == []  # still pending confirmation
    async with session_scope() as s:
        assert (await s.execute(select(Contact).where(Contact.email == "reza@example.org"))).scalar_one_or_none() is None


async def test_text_messages_are_stored_as_text_source(owner):
    mid = await add_message(owner, "سلام")
    async with session_scope() as s:
        vm = await s.get(VoiceMessage, mid)
    assert vm.source == "text" and vm.telegram_file_id is None and vm.transcript == "سلام"
