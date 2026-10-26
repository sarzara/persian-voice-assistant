"""Seed the owner and two sample contacts. Safe to run repeatedly (upserts).

    docker compose run --rm api python -m app.db.seed

Sample contacts use example.com addresses (RFC 2606), so nothing is ever
delivered to a real person by accident.
"""

import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db.models import Contact, MemoryFact
from app.db.repo import audit, upsert_user
from app.db.session import get_engine, session_scope

log = logging.getLogger(__name__)

SAMPLE_CONTACTS = [
    {
        "name": "علی رضایی",
        "aliases": ["علی", "آقای رضایی", "Ali", "Ali Rezaei", "Rezaei"],
        "email": "ali.rezaei@example.com",
    },
    {
        "name": "سارا محمدی",
        "aliases": ["سارا", "خانم محمدی", "Sara", "Sarah", "Sara Mohammadi"],
        "email": "sara.mohammadi@example.com",
    },
]

DEFAULT_FACTS = {"default_meeting_minutes": "30"}


async def seed() -> None:
    settings = get_settings()
    owner_id = settings.owner_telegram_id
    if owner_id is None:
        raise SystemExit("OWNER_TELEGRAM_ID is not set in .env")
    if owner_id not in settings.allowed_user_ids:
        log.warning("OWNER_TELEGRAM_ID %s is not in ALLOWED_USER_IDS; the owner could not use the bot", owner_id)

    async with session_scope() as session:
        owner = await upsert_user(
            session,
            owner_id,
            name=settings.owner_name,
            email=settings.owner_email,
            timezone=settings.default_timezone,
        )
        for contact in SAMPLE_CONTACTS:
            stmt = insert(Contact).values(user_id=owner.id, **contact)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_contacts_user_id_name",
                set_={"aliases": stmt.excluded.aliases, "email": stmt.excluded.email},
            )
            await session.execute(stmt)
        for key, value in DEFAULT_FACTS.items():
            stmt = insert(MemoryFact).values(user_id=owner.id, key=key, value=value, source="seed")
            # Never overwrite a preference the user has since changed.
            await session.execute(stmt.on_conflict_do_nothing(constraint="uq_memory_facts_user_id_key"))
        await audit(session, "seed.applied", "cli", contacts=len(SAMPLE_CONTACTS))

    await get_engine().dispose()
    print(f"Seeded owner telegram_user_id={owner_id} and {len(SAMPLE_CONTACTS)} contacts.")


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(seed())


if __name__ == "__main__":
    main()
