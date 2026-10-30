"""Language of a user and of a piece of text (Persian + English)."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemoryFact

SUPPORTED = ("fa", "en")
SETTINGS = ("fa", "en", "auto")
_PERSIAN_LETTER = re.compile(r"[\u0600-\u06FF]")


def detect_text_language(text: str) -> str:
    """'fa' if at least 30 % of the letters are Persian/Arabic script, otherwise 'en'.

    Mixed requests such as "meeting با Ali فردا" count as Persian, because the
    grammar (and the expected reply) is Persian.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "fa"
    persian = sum(1 for c in letters if _PERSIAN_LETTER.match(c))
    return "fa" if persian / len(letters) >= 0.3 else "en"


async def user_language(session: AsyncSession, user_id: int) -> str:
    """The user's setting: 'fa', 'en' or 'auto' (default when nothing is stored)."""
    value = await session.scalar(
        select(MemoryFact.value).where(MemoryFact.user_id == user_id, MemoryFact.key == "language")
    )
    return value if value in SETTINGS else "auto"


def resolve_language(setting: str, text: str | None = None) -> str:
    """Turn a setting into a concrete language. 'auto' needs the text to decide."""
    if setting in SUPPORTED:
        return setting
    return detect_text_language(text) if text else "fa"