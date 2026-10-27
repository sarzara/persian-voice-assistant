#!/usr/bin/env bash
# Nightly PostgreSQL backup: pg_dump (custom format) -> optional encryption -> copy off the server -> prune.
#
#   deploy/backup.sh                # full run (cron)
#   deploy/backup.sh --local-only   # just a local dump (used by deploy.sh before migrations)
#
# Settings (environment or deploy/backup.env):
#   BACKUP_DIR=/var/backups/pva     local directory (keep it outside the repo)
#   KEEP_DAYS=14                    local retention
#   BACKUP_AGE_RECIPIENT=age1...    encrypt with this age public key (private key stays on your laptop)
#   BACKUP_RCLONE_REMOTE=remote:bucket/pva   copy to object storage with rclone (off-server copy)
#   REMOTE_KEEP_DAYS=30             retention on the remote
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/backup.env ] && . deploy/backup.env
COMPOSE=(docker compose -f docker-compose.prod.yml)
BACKUP_DIR=${BACKUP_DIR:-/var/backups/pva}
KEEP_DAYS=${KEEP_DAYS:-14}
REMOTE_KEEP_DAYS=${REMOTE_KEEP_DAYS:-30}
if ! mkdir -p "$BACKUP_DIR" 2>/dev/null || [ ! -w "$BACKUP_DIR" ]; then
  BACKUP_DIR="$HOME/pva-backups"   # /var/backups not prepared yet (docs/DEPLOY.md step 12)
  mkdir -p "$BACKUP_DIR"
fi
chmod 700 "$BACKUP_DIR"

ts=$(date -u +%Y%m%dT%H%M%SZ)
file="$BACKUP_DIR/pva-$ts.dump"
tmp="$file.partial"

# pg_dump runs inside the db container; the archive streams out over stdout.
"${COMPOSE[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --compress=9' > "$tmp"
# Integrity check: the archive's table of contents must be readable.
"${COMPOSE[@]}" exec -T db pg_restore --list < "$tmp" > /dev/null
mv "$tmp" "$file"
echo "dump: $file ($(du -h "$file" | cut -f1))"

if [ "${1:-}" = "--local-only" ]; then
  exit 0
fi

out="$file"
if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  age -r "$BACKUP_AGE_RECIPIENT" -o "$file.age" "$file"
  rm -f "$file"
  out="$file.age"
  echo "encrypted: $out"
fi

if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
  rclone copy --no-traverse "$out" "$BACKUP_RCLONE_REMOTE"
  rclone delete --min-age "${REMOTE_KEEP_DAYS}d" "$BACKUP_RCLONE_REMOTE" || true
  echo "copied off-server: $BACKUP_RCLONE_REMOTE/$(basename "$out")"
else
  echo "WARNING: BACKUP_RCLONE_REMOTE is not set; pull $BACKUP_DIR to another machine (docs/DEPLOY.md step 12)" >&2
fi

find "$BACKUP_DIR" -maxdepth 1 -name 'pva-*' -mtime +"$KEEP_DAYS" -delete
