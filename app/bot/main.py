"""Telegram worker: long polling.

    python -m app.bot.main
"""

import logging

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, MessageHandler, TypeHandler, filters

from app.bot import handlers
from app.bot.messages import COMMANDS
from app.config import get_settings
from app.db.session import get_engine


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s [%(name)s] %(message)s", level=level)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])


async def _post_shutdown(app: Application) -> None:
    await get_engine().dispose()


def build_application() -> Application:
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not settings.allowed_user_ids:
        raise SystemExit("ALLOWED_USER_IDS is empty: nobody could use the bot")

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, handlers.allowlist_gate), group=-1)
    app.add_handler(CommandHandler("start", handlers.start, filters=private))
    app.add_handler(CommandHandler("help", handlers.help_command, filters=private))
    app.add_handler(MessageHandler(private & filters.VOICE, handlers.voice))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, handlers.text_message))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, handlers.unsupported))
    app.add_error_handler(handlers.on_error)
    return app


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info(
        "Starting bot (long polling), %d allowlisted user(s)", len(settings.allowed_user_ids)
    )
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
