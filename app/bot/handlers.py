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
from app.bot.ratelimit import RateLimiter
from app.budget import BudgetExceeded
from app.config import get_settings
from app.logging_setup import new_request_id, request_id_var, set_run_id
from app.db.models import VoiceMessage
from app.db.repo import audit, upsert_user
from app.db.session import session_scope
from app.i18n import user_language
from app.stt.service import SttOutcome, SttResult, transcribe_voice_message
from app.agent import pipeline
from app.agent.agent import AgentOutcome
from app.agent import executor
from app.agent.executor import ExecStatus
from app.bot import preview
from telegram.constants import ChatAction

log = logging.getLogger(__name__)

# Bot API limit for getFile downloads.
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


_rate_limiter: RateLimiter | None = None


def _limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        s = get_settings()
        _rate_limiter = RateLimiter(s.rate_limit_per_minute, s.rate_limit_per_hour)
    return _rate_limiter


async def allowlist_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Correlation: every log line for this update carries tg-<update_id>; run_id is added once a run exists.
    request_id_var.set(f"tg-{update.update_id}" if getattr(update, "update_id", None) is not None else new_request_id("tg"))
    set_run_id(None)
    user = update.effective_user
    allowed = get_settings().allowed_user_ids
    if user is not None and user.id in allowed:
        # Button taps are cheap and must always work (e.g. Confirm); only messages are limited.
        if getattr(update, "callback_query", None) is None and update.effective_message is not None:
            wait = _limiter().check(user.id)
            if wait:
                log.warning("Rate limited user_id=%s for %.0fs", user.id, wait)
                await update.effective_message.reply_text(
                    msg.RATE_LIMITED.format(minutes=msg.to_persian_digits(max(1, round(wait / 60)))))
                raise ApplicationHandlerStop
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


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: a one-time sign-in link for the usage dashboard (valid 10 minutes)."""
    from telegram import LinkPreviewOptions

    from app.dashboard import auth as dash_auth

    settings = get_settings()
    if update.effective_user.id != settings.owner_telegram_id:
        await update.effective_message.reply_text(msg.DASHBOARD_OWNER_ONLY)
        return
    try:
        link = dash_auth.login_link(update.effective_user.id)
    except RuntimeError:
        await update.effective_message.reply_text(msg.DASHBOARD_NOT_CONFIGURED)
        return
    # No link preview: Telegram's preview fetcher must not touch the login URL.
    await update.effective_message.reply_text(msg.DASHBOARD_LINK.format(link=link),
                                              link_preview_options=LinkPreviewOptions(is_disabled=True))


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
                          # The voice model follows the user's language setting: "en" -> English model,
              # "fa" or "auto" -> Persian model (Vosk confidences can't tell the languages apart).
            # "fa", "en" or "auto": Whisper detects the language itself (the setting is a hint);
            # Vosk can't, so it uses the English model only for "en".
            voice_language = await user_language(session, db_user.id)
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
    result = await transcribe_voice_message(record.id, path, language=voice_language)
    if not result.usable:
        await message.reply_text(stt_problem_message(result))
        return
    await message.reply_text(msg.TRANSCRIPT.format(text=result.text))
    await respond_to(update, context, db_user, record.id, result.text)


def stt_problem_message(result: SttResult) -> str:
    if result.outcome is SttOutcome.EMPTY:
        return msg.STT_EMPTY
    if result.outcome is SttOutcome.LOW_CONFIDENCE:
        return msg.STT_LOW_CONFIDENCE.format(text=result.text)
    return msg.STT_FAILED


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed requests go through the same agent pipeline as transcribed voice."""
    user = update.effective_user
    text = update.effective_message.text.strip()
    async with session_scope() as session:
        db_user = await upsert_user(session, user.id, name_if_new=user.full_name)
    message_id = await pipeline.store_text_message(db_user, text)
    await respond_to(update, context, db_user, message_id, text)


async def respond_to(update: Update, context: ContextTypes.DEFAULT_TYPE, db_user, message_id: int, transcript: str) -> None:
    chat = update.effective_chat
    if context is not None and chat is not None:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    try:
        outcome = await pipeline.process_message(db_user, message_id, transcript)
    except BudgetExceeded as exc:
        log.warning("Request refused: %s", exc)
        await update.effective_message.reply_text(
            msg.BUDGET_STOP_DAILY if exc.period == "daily" else msg.BUDGET_STOP_MONTHLY)
        return
    except ValueError as exc:  # e.g. DEEPSEEK_API_KEY missing
        log.error("Agent not available: %s", exc)
        await update.effective_message.reply_text(msg.AGENT_UNAVAILABLE)
        return
    await render_outcome(update, context, outcome)


async def render_outcome(update: Update, context, outcome: AgentOutcome) -> None:
    # Plain text (no parse_mode): model output can never inject formatting or links.
    text, markup = preview.outcome_message(outcome)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm / Edit / Cancel buttons under a preview."""
    query = update.callback_query
    parsed = preview.parse_callback(query.data)
    if parsed is None:
        await query.answer()
        return
    run_id, choice = parsed
    tg_id = update.effective_user.id

    if choice == preview.CONFIRM:
        await query.answer("در حال انجام…")
        # Remove the buttons first, so the preview cannot be tapped again while we work.
        await query.edit_message_reply_markup(reply_markup=None)
        result = await executor.confirm_and_execute(
            run_id, tg_id, calendar=pipeline.get_calendar(), mailer=pipeline.get_mailer()
        )
        text = {
            ExecStatus.EXECUTED: lambda: preview.render_executed(result.action, result.data),
            ExecStatus.ALREADY_DONE: lambda: msg.ALREADY_DONE,
            ExecStatus.CANCELLED: lambda: msg.ALREADY_CANCELLED,
            ExecStatus.EXPIRED: lambda: msg.EXPIRED,
            ExecStatus.NOT_FOUND: lambda: msg.RUN_NOT_FOUND,
            ExecStatus.FAILED: lambda: preview.render_failure(result.error),
        }[result.status]()
        markup = preview.feedback_keyboard(run_id) if result.status in (ExecStatus.EXECUTED, ExecStatus.FAILED) else None
        await query.message.reply_text(text, reply_markup=markup)
        if result.status is ExecStatus.EXECUTED:
            await pipeline.after_execution(run_id)
        return

    await query.answer()
    result = await executor.reject_run(run_id, tg_id, "edit_requested" if choice == preview.EDIT else "cancelled")
    await query.edit_message_reply_markup(reply_markup=None)
    if result.status is ExecStatus.ALREADY_DONE:
        await query.message.reply_text(msg.ALREADY_DONE)
    elif result.status is ExecStatus.NOT_FOUND:
        await query.message.reply_text(msg.RUN_NOT_FOUND)
    elif choice == preview.EDIT:
        await query.message.reply_text(msg.EDIT_PROMPT)
    else:
        await query.message.reply_text(msg.CANCELLED)


async def on_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """👍/👎 under a reply: one rating per run, the last tap wins."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.models import AgentRun, RunFeedback

    query = update.callback_query
    parsed = preview.parse_feedback(query.data)
    if parsed is None:
        await query.answer()
        return
    run_id, rating = parsed
    async with session_scope() as session:
        db_user = await upsert_user(session, update.effective_user.id)
        run = await session.get(AgentRun, run_id)
        if run is None or run.user_id != db_user.id:
            await query.answer(msg.RUN_NOT_FOUND)
            return
        stmt = pg_insert(RunFeedback).values(run_id=run_id, user_id=db_user.id, rating=rating)
        await session.execute(stmt.on_conflict_do_update(index_elements=[RunFeedback.run_id],
                                                         set_={"rating": stmt.excluded.rating}))
    await query.answer(msg.FEEDBACK_THANKS)


async def unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.UNSUPPORTED)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the full traceback; tell the user in Persian without leaking internals."""
    log.exception("Unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            # Short Persian text plus a reference code that matches request_id in the logs.
            await update.effective_message.reply_text(msg.GENERIC_ERROR.format(ref=request_id_var.get() or "-"))
        except Exception:  # noqa: BLE001 - the error reply itself must never raise
            log.exception("Failed to send error message to user")
