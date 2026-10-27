"""Create/refresh the dashboard's read-only database role. Idempotent; run after migrations.

    python -m app.db.roles

The role can SELECT every table and view (including future ones created by the owner
role) and nothing else. Its sessions also default to read-only transactions.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from app.config import get_settings

log = logging.getLogger(__name__)


def ensure_readonly_role(conn: psycopg.Connection, role: str, password: str, database: str) -> None:
    ident = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}")
                        .format(ident, sql.Literal(password)))
        else:
            cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}")
                        .format(ident, sql.Literal(password)))
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), ident))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(ident))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(ident))
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(ident))
    conn.commit()


def main() -> None:
    s = get_settings()
    password = s.dashboard_db_password.get_secret_value()
    if not password:
        print("DASHBOARD_DB_PASSWORD not set: skipping read-only role")
        return
    url = s.database_url().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        ensure_readonly_role(conn, s.dashboard_db_user, password, s.postgres_db)
    print(f"Read-only role {s.dashboard_db_user!r} is ready")


if __name__ == "__main__":
    main()
