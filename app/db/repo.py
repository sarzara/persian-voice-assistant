"""Small data-access helpers shared by the bot, the seed script and the CLI."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent, User


async def upsert_user(
    session: AsyncSession,
    telegram_user_id: int,
    *,
    name: str | None = None,
    email: str | None = None,
    timezone: str | None = None,
) -> User:
    """Insert the user, or update only the fields that were provided. Race-safe."""
    values: dict[str, Any] = {"telegram_user_id": telegram_user_id}
    updates: dict[str, Any] = {}
    for column, value in (("name", name), ("email", email), ("timezone", timezone)):
        if value is not None:
            values[column] = value
            updates[column] = value

    stmt = insert(User).values(**values)
    stmt = (
        stmt.on_conflict_do_update(index_elements=[User.telegram_user_id], set_=updates)
        if updates
        else stmt.on_conflict_do_nothing(index_elements=[User.telegram_user_id])
    )
    await session.execute(stmt)
    result = await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    return result.scalar_one()


async def audit(session: AsyncSession, event_type: str, actor: str, **payload: Any) -> None:
    session.add(AuditEvent(event_type=event_type, actor=actor, payload_json=payload))
