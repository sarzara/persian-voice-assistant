#!/usr/bin/env bash
# Restore a backup.
#
#   deploy/restore.sh --check FILE   # restore into a throwaway database, print row counts, drop it (no downtime)
#   deploy/restore.sh FILE           # REPLACE the live database with FILE (asks for confirmation)
#
# FILE: a .dump from backup.sh (decrypt .age files first: age -d -i key.txt -o x.dump x.dump.age).
set -Eeuo pipefail
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
check=0
[ "${1:-}" = "--check" ] && { check=1; shift; }
file=${1:?usage: deploy/restore.sh [--check] FILE}
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 1; }

psql_db() { "${COMPOSE[@]}" exec -T db sh -c "psql -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d $1 -qAt"; }

if [ $check -eq 1 ]; then
  scratch="restore_check_$(date +%s)"
  echo "CREATE DATABASE $scratch;" | psql_db postgres
  trap 'echo "DROP DATABASE IF EXISTS $scratch;" | psql_db postgres' EXIT
  "${COMPOSE[@]}" exec -T db sh -c "pg_restore -U \"\$POSTGRES_USER\" -d $scratch --no-owner --exit-on-error" < "$file"
  echo "Restored into $scratch. Row counts:"
  psql_db "$scratch" <<'SQL'
SELECT 'users', count(*)::text FROM users UNION ALL SELECT 'contacts', count(*)::text FROM contacts
UNION ALL SELECT 'agent_runs', count(*)::text FROM agent_runs UNION ALL SELECT 'llm_calls', count(*)::text FROM llm_calls
UNION ALL SELECT 'calendar_events', count(*)::text FROM calendar_events UNION ALL SELECT 'emails', count(*)::text FROM emails
UNION ALL SELECT 'schema revision', version_num FROM alembic_version;
SQL
  echo "Restore check passed (database $scratch is dropped on exit)."
  exit 0
fi

echo "This REPLACES the live database with $file."
read -r -p "Type 'restore' to continue: " answer
[ "$answer" = "restore" ] || { echo "aborted"; exit 1; }
deploy/backup.sh --local-only   # last-chance copy of the current state
"${COMPOSE[@]}" stop bot api dashboard mcp caddy 2>/dev/null || true
"${COMPOSE[@]}" exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
"${COMPOSE[@]}" exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < "$file"
"${COMPOSE[@]}" up -d   # migrate runs first: brings an older dump up to the current schema
echo "Restored. Check: curl -s http://127.0.0.1:8000/health"
