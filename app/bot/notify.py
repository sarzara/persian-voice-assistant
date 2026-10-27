"""Send a message to the owner outside an update (e.g. after an admin retry from the API)."""

from __future__ import annotations

import logging

from telegram import Bot

from app.bot import preview
from app.config import get_settings

log = logging.getLogger(__name__)


async def send_outcome(chat_id: int, outcome) -> bool:
    token = get_settings().telegram_bot_token.get_secret_value()
    if not token:
        return False
    text, markup = preview.outcome_message(outcome)
    try:
        async with Bot(token) as bot:
            await bot.send_message(chat_id, text, reply_markup=markup)
        return True
    except Exception:  # noqa: BLE001 - notification is best effort
        log.exception("Could not notify chat %s", chat_id)
        return False


async def send_text(chat_id: int, text: str) -> bool:
    token = get_settings().telegram_bot_token.get_secret_value()
    if not token:
        return False
    try:
        async with Bot(token) as bot:
            await bot.send_message(chat_id, text)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Could not notify chat %s", chat_id)
        return False
