#!/bin/sh
# One-shot start-up step: schema, read-only dashboard role, optional demo data. Safe to run repeatedly.
set -eu
alembic upgrade head
python -m app.db.roles
if [ "${DEMO_DATA:-false}" = "true" ]; then
  python -m app.dashboard.demo --if-empty
fi
