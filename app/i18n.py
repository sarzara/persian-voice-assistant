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


async def set_user_language(session: AsyncSession, user_id: int, setting: str) -> None:
    """Store the /language setting ('fa', 'en' or 'auto') as memory fact "language"."""
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if setting not in SETTINGS:
        raise ValueError(f"unknown language setting {setting!r}")
    stmt = pg_insert(MemoryFact).values(user_id=user_id, key="language", value=setting, source="user")
    await session.execute(stmt.on_conflict_do_update(
        constraint="uq_memory_facts_user_id_key", set_={"value": setting, "updated_at": func.now()}))


def resolve_language(setting: str, text: str | None = None) -> str:
    """Turn a setting into a concrete language. 'auto' needs the text to decide."""
    if setting in SUPPORTED:
        return setting
    return detect_text_language(text) if text else "fa"