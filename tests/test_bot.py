from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from telegram.constants import ChatType
from telegram.ext import ApplicationHandlerStop

from app.bot import handlers
from app.bot import messages as msg
from app.db.models import AuditEvent, VoiceMessage
from app.db.session import session_scope

ALLOWED = 111111111
STRANGER = 999999999


def make_update(user_id: int, *, text: str | None = None, voice=None, chat_type=ChatType.PRIVATE):
    message = SimpleNamespace(text=text, voice=voice, message_id=42, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, full_name="Test User"),
        effective_chat=SimpleNamespace(type=chat_type),
        effective_message=message,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_USER_IDS", str(ALLOWED))
    monkeypatch.setenv("VOICE_STORAGE_DIR", str(tmp_path / "voice"))


async def test_gate_lets_allowlisted_user_through():
    update = make_update(ALLOWED, text="hi")
    await handlers.allowlist_gate(update, None)  # no exception
    update.effective_message.reply_text.assert_not_called()


async def test_gate_refuses_stranger_politely_in_persian():
    update = make_update(STRANGER, text="hi")
    with pytest.raises(ApplicationHandlerStop):
        await handlers.allowlist_gate(update, None)
    update.effective_message.reply_text.assert_awaited_once_with(msg.REFUSAL)


async def test_gate_stays_silent_for_stranger_in_group():
    update = make_update(STRANGER, text="hi", chat_type=ChatType.GROUP)
    with pytest.raises(ApplicationHandlerStop):
        await handlers.allowlist_gate(update, None)
    update.effective_message.reply_text.assert_not_called()


async def test_help_is_persian():
    update = make_update(ALLOWED)
    await handlers.help_command(update, None)
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "راهنما" in reply


async def test_text_goes_through_agent_and_question_is_sent_plain(db, monkeypatch):
    from app.agent.agent import AgentOutcome

    seen = {}

    async def fake_process(user, message_id, transcript, **kw):
        seen.update(user=user.telegram_user_id, transcript=transcript, message_id=message_id)
        return AgentOutcome("question", run_id=1, question="<b>چه ساعتی؟</b>")

    monkeypatch.setattr(handlers.pipeline, "process_message", fake_process)
    update = make_update(ALLOWED, text="  فردا با علی جلسه  ")
    await handlers.text_message(update, None)
    assert seen["user"] == ALLOWED and seen["transcript"] == "فردا با علی جلسه" and seen["message_id"]
    update.effective_message.reply_text.assert_awaited_once_with("<b>چه ساعتی؟</b>")


async def test_start_creates_user_and_greets_in_persian(db):
    update = make_update(ALLOWED)
    await handlers.start(update, None)
    reply = update.effective_message.reply_text.await_args.args[0]
    assert reply.startswith("سلام Test User")


async def test_voice_is_downloaded_and_stored(db, tmp_path, monkeypatch):
    from app.stt.service import SttOutcome, SttResult

    seen = {}

    async def fake_stt(vm_id, path):
        seen["audio"] = Path(path).read_bytes()
        return SttResult(SttOutcome.OK, "فردا ساعت ده", 0.9)

    monkeypatch.setattr(handlers, "transcribe_voice_message", fake_stt)
    responded = {}

    async def fake_respond(update, context, db_user, message_id, transcript):
        responded["transcript"] = transcript

    monkeypatch.setattr(handlers, "respond_to", fake_respond)

    async def download_to_drive(custom_path):
        Path(custom_path).write_bytes(b"OggS-fake-audio")

    voice = SimpleNamespace(
        file_id="FILE_ID_1",
        file_unique_id="UNIQ1",
        duration=7,
        file_size=1234,
        get_file=AsyncMock(return_value=SimpleNamespace(download_to_drive=download_to_drive)),
    )
    update = make_update(ALLOWED, voice=voice)
    await handlers.voice(update, None)

    async with session_scope() as s:
        row = (await s.execute(select(VoiceMessage))).scalar_one()
        audits = await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "voice.received"))
    assert row.telegram_file_id == "FILE_ID_1" and row.duration_s == 7
    assert seen["audio"] == b"OggS-fake-audio"
    assert audits == 1
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "فردا ساعت ده" in reply and responded["transcript"] == "فردا ساعت ده"


async def test_low_confidence_asks_to_repeat_in_persian():
    from app.stt.service import SttOutcome, SttResult

    text = handlers.stt_problem_message(SttResult(SttOutcome.LOW_CONFIDENCE, "فردا", 0.3))
    assert "تکرار" in text and "فردا" in text
    assert "دوباره" in handlers.stt_problem_message(SttResult(SttOutcome.EMPTY))


async def test_too_long_voice_refused_before_download():
    voice = SimpleNamespace(file_size=1000, duration=600, get_file=AsyncMock())
    update = make_update(ALLOWED, voice=voice)
    await handlers.voice(update, None)
    voice.get_file.assert_not_called()
    assert "طولانی" in update.effective_message.reply_text.await_args.args[0]


async def test_oversized_voice_rejected_without_download(tmp_path):
    voice = SimpleNamespace(file_size=25 * 1024 * 1024, get_file=AsyncMock())
    update = make_update(ALLOWED, voice=voice)
    await handlers.voice(update, None)
    voice.get_file.assert_not_called()
    update.effective_message.reply_text.assert_awaited_once_with(msg.VOICE_TOO_LARGE)
