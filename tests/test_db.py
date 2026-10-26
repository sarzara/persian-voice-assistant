from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from app.config import get_settings
from app.db.models import Contact, User
from app.db.seed import seed
from app.db.session import session_scope

EXPECTED_TABLES = {
    "users", "contacts", "voice_messages", "agent_runs", "tool_calls",
    "calendar_events", "emails", "memory_facts", "audit_events",
}


def test_migration_creates_all_tables_with_timestamps(db):
    insp = inspect(create_engine(get_settings().database_url()))
    assert EXPECTED_TABLES <= set(insp.get_table_names())
    for table in EXPECTED_TABLES:
        cols = {c["name"] for c in insp.get_columns(table)}
        assert {"created_at", "updated_at"} <= cols, table


def test_models_match_migration(db):
    """Fails if a model changed without a new Alembic revision."""
    command.check(Config("alembic.ini"))


def test_status_check_constraint_enforced(db):
    engine = create_engine(get_settings().database_url())
    with engine.begin() as conn:
        try:
            conn.execute(text("INSERT INTO agent_runs (status) VALUES ('bogus')"))
        except Exception as exc:  # noqa: BLE001
            assert "ck_agent_runs_status_valid" in str(exc)
        else:
            raise AssertionError("invalid status was accepted")


async def test_seed_is_idempotent(db, monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "111111111")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111111111")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    await seed()
    await seed()
    async with session_scope() as s:
        users = (await s.execute(select(User))).scalars().all()
        contacts = (await s.execute(select(Contact))).scalars().all()
    assert len(users) == 1 and users[0].timezone == "Asia/Tehran"
    assert len(contacts) == 2
    assert all(c.email.endswith("@example.com") for c in contacts)
    assert any("Ali" in c.aliases and "علی" in c.aliases for c in contacts)
