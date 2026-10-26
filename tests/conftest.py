"""Shared fixtures.

Database tests run against the PostgreSQL configured by POSTGRES_* (e.g. the
compose db). They are opt-in via RUN_DB_TESTS=1 because they TRUNCATE every
table, and are skipped when the database is unreachable.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import session as db_session

TABLES = (
    "users", "contacts", "voice_messages", "agent_runs", "tool_calls",
    "calendar_events", "emails", "memory_facts", "audit_events",
)


@pytest.fixture(autouse=True)
def _fresh_settings_and_engine():
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()
    yield
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


@pytest.fixture
async def db():
    """Migrated, empty database. WARNING: truncates every table; never point at production."""
    if os.environ.get("RUN_DB_TESTS") != "1":
        pytest.skip("database tests are opt-in: set RUN_DB_TESTS=1 (they TRUNCATE every table)")
    url = get_settings().database_url()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"PostgreSQL not reachable: {exc.__class__.__name__}")

    from alembic import command
    from alembic.config import Config

    def truncate() -> None:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))

    command.upgrade(Config("alembic.ini"), "head")
    truncate()
    yield
    await db_session.get_engine().dispose()
    truncate()
    engine.dispose()
