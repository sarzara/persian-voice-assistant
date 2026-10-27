"""Telegram update handlers.

Order of execution
1. ``allowlist_gate`` (group -1) runs for every update and stops anything from
   a user not in ALLOWED_USER_IDS before any other handler sees it.
2. Command / voice / text handlers (group 0).
"""

import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app.bot import messages as msg
from app.config import get_settings
from app.db.models import VoiceMessage
from app.db.repo import audit, upsert_user
from app.db.session import session_scope
from app.stt.service import SttOutcome, SttResult, transcribe_voice_message

log = logging.getLogger(__name__)

# Bot API limit for getFile downloads.
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


async def allowlist_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    allowed = get_settings().allowed_user_ids
    if user is not None and user.id in allowed:
        return  # let the update continue to the normal handlers

    log.info("Rejected update from non-allowlisted user_id=%s", user.id if user else None)
    chat = update.effective_chat
    # Reply only in private chats, so the bot never spams a group it was added to.
    if update.effective_message is not None and chat is not None and chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(msg.REFUSAL)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    async with session_scope() as session:
        db_user = await upsert_user(session, user.id, name_if_new=user.full_name)
        await audit(session, "bot.start", f"telegram:{user.id}")
        name = db_user.name
    await update.effective_message.reply_text(msg.START.format(name=f" {name}" if name else ""))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.HELP)


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    voice_obj = message.voice

    if voice_obj.file_size and voice_obj.file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
        await message.reply_text(msg.VOICE_TOO_LARGE)
        return
    max_s = get_settings().stt_max_duration_s
    if voice_obj.duration and voice_obj.duration > max_s:
        await message.reply_text(msg.VOICE_TOO_LONG.format(limit=msg.to_persian_digits(max_s)))
        return

    # file_unique_id is stable and filesystem-safe; the user folder keeps audio separable per owner.
    target_dir = Path(get_settings().voice_storage_dir) / str(user.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{message.message_id}_{voice_obj.file_unique_id}.ogg"

    tg_file = await voice_obj.get_file()
    await tg_file.download_to_drive(custom_path=path)

    try:
        async with session_scope() as session:
            db_user = await upsert_user(session, user.id, name_if_new=user.full_name)
            record = VoiceMessage(
                user_id=db_user.id,
                telegram_file_id=voice_obj.file_id,
                duration_s=voice_obj.duration,
                audio_path=str(path),
            )
            session.add(record)
            await session.flush()
            await audit(
                session,
                "voice.received",
                f"telegram:{user.id}",
                voice_message_id=record.id,
                duration_s=voice_obj.duration,
                size_bytes=voice_obj.file_size,
            )
    except Exception:
        # Don't leave an orphaned audio file without a database row.
        path.unlink(missing_ok=True)
        raise

    log.info("Stored voice_message id=%s user=%s duration=%ss", record.id, user.id, voice_obj.duration)
    result = await transcribe_voice_message(record.id, path)
    if not result.usable:
        await message.reply_text(stt_problem_message(result))
        return
    await message.reply_text(msg.TRANSCRIPT.format(text=result.text))


def stt_problem_message(result: SttResult) -> str:
    if result.outcome is SttOutcome.EMPTY:
        return msg.STT_EMPTY
    if result.outcome is SttOutcome.LOW_CONFIDENCE:
        return msg.STT_LOW_CONFIDENCE.format(text=result.text)
    return msg.STT_FAILED


async def echo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Sent as plain text (no parse_mode), so user input can never inject formatting or links.
    await update.effective_message.reply_text(msg.ECHO.format(text=update.effective_message.text))


async def unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.UNSUPPORTED)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the full traceback; tell the user in Persian without leaking internals."""
    log.exception("Unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(msg.GENERIC_ERROR)
        except Exception:  # noqa: BLE001 - the error reply itself must never raise
            log.exception("Failed to send error message to user")
