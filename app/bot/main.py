"""Telegram worker: long polling.

    python -m app.bot.main
"""

import asyncio
import logging

from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, TypeHandler, filters

from app.bot import handlers
from app.bot.messages import COMMANDS
from app.config import get_settings
from app.logging_setup import configure_logging
from app.db.session import get_engine
from app.stt.cleanup import cleanup_loop
from app.stt.service import get_recognizer




async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    app.bot_data["audio_cleanup"] = asyncio.create_task(cleanup_loop(), name="audio-cleanup")
    app.bot_data["heartbeat"] = asyncio.create_task(_heartbeat(), name="heartbeat")
    # Load the Vosk model now (a few seconds) instead of on the first voice message,
    # and fail loudly in the logs if it is missing.
    for lang in ("fa", "en"):
        try:
            await asyncio.to_thread(get_recognizer(lang)._model)
        except Exception:  # noqa: BLE001 - text messages still work without STT
            logging.getLogger(__name__).exception("Vosk %s model could not be loaded", lang)


HEARTBEAT_FILE = "/tmp/bot-heartbeat"


async def _heartbeat() -> None:
    """Touch a file every 30 s; the container healthcheck fails if the event loop stops."""
    from pathlib import Path

    while True:
        try:
            Path(HEARTBEAT_FILE).touch()
        except OSError:
            pass
        await asyncio.sleep(30)


async def _post_shutdown(app: Application) -> None:
    for name in ("audio_cleanup", "heartbeat"):
        task = app.bot_data.pop(name, None)
        if task:
            task.cancel()
    await get_engine().dispose()


def build_application() -> Application:
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not settings.allowed_user_ids:
        raise SystemExit("ALLOWED_USER_IDS is empty: nobody could use the bot")

    base = settings.telegram_api_base_url.rstrip("/")
    app = (
        Application.builder()
        .token(token)
        .base_url(f"{base}/bot")
        .base_file_url(f"{base}/file/bot")
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, handlers.allowlist_gate), group=-1)
    app.add_handler(CommandHandler("start", handlers.start, filters=private))
    app.add_handler(CommandHandler("help", handlers.help_command, filters=private))
    app.add_handler(CommandHandler("language", handlers.language_command, filters=private))
    app.add_handler(CommandHandler("dashboard", handlers.dashboard_command, filters=private))
    app.add_handler(CallbackQueryHandler(handlers.on_callback, pattern=r"^run:"))
    app.add_handler(CallbackQueryHandler(handlers.on_feedback, pattern=r"^fb:"))
    app.add_handler(MessageHandler(private & filters.VOICE, handlers.voice))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, handlers.text_message))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, handlers.unsupported))
    app.add_error_handler(handlers.on_error)
    return app


def main() -> None:
    settings = get_settings()
    configure_logging("bot", settings.log_level, settings.log_format)
    logging.getLogger(__name__).info(
        "Starting bot (long polling), %d allowlisted user(s)", len(settings.allowed_user_ids)
    )
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
