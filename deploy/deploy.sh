#!/usr/bin/env bash
# Update (or first-time start) the production stack. Safe to run repeatedly.
#
#   deploy/deploy.sh             # pull the current branch, build, migrate, restart, verify
#   deploy/deploy.sh v1.0.0      # deploy a specific tag or commit
#   SKIP_BUILD=1 deploy/deploy.sh   # images were shipped from another machine (deploy/ship-images.sh)
#   SKIP_PULL=1  deploy/deploy.sh   # deploy the checked-out code as is
#
# Idempotent: git pull is a no-op when up to date, Docker reuses cached layers, migrations and the
# read-only role are idempotent, and `up -d` only recreates containers whose image or config changed.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
STATE_DIR=.deploy
mkdir -p "$STATE_DIR"

# Schema revision straight from the database (no image needed).
db_revision() {
  "${COMPOSE[@]}" exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"' 2>/dev/null | tr -d '[:space:]'
}

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# One deploy at a time.
exec 9>"$STATE_DIR/deploy.lock"
flock -n 9 || die "another deploy is running"

log "Preflight"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"
[ -f .env ] || die ".env is missing: cp .env.example .env and fill it in (docs/DEPLOY.md step 6)"
required=(TELEGRAM_BOT_TOKEN ALLOWED_USER_IDS OWNER_TELEGRAM_ID OWNER_EMAIL POSTGRES_PASSWORD DEEPSEEK_API_KEY
          RESEND_API_KEY EMAIL_FROM ADMIN_API_KEY DASHBOARD_SECRET_KEY DASHBOARD_DB_PASSWORD DASHBOARD_HOST ACME_EMAIL)
for var in "${required[@]}"; do
  value=$(grep -E "^${var}=" .env | tail -n1 | cut -d= -f2- || true)
  [ -n "$value" ] || die "$var is empty in .env"
  case "$value" in *replace-me*|*change-me*) die "$var still has the placeholder value";; esac
done
if [ "$(stat -c %a .env)" != "600" ]; then chmod 600 .env; echo "fixed .env permissions to 600"; fi

previous_commit=$(git rev-parse HEAD)
log "Code (currently $(git rev-parse --short HEAD))"
if [ "${SKIP_PULL:-0}" != "1" ]; then
  git fetch --tags --quiet origin
  if [ $# -ge 1 ]; then
    git checkout --quiet --detach "$1"
  else
    # After a rollback HEAD is detached: return to the branch before pulling.
    git symbolic-ref -q HEAD >/dev/null || git checkout --quiet "${DEFAULT_BRANCH:-main}"
    git pull --ff-only --quiet
  fi
fi
APP_VERSION=$(git rev-parse --short HEAD)
export APP_VERSION
echo "deploying $APP_VERSION ($(git log -1 --format=%s))"

if [ "$previous_commit" != "$(git rev-parse HEAD)" ] && "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx db; then
  log "Safety backup before migrating"
  deploy/backup.sh --local-only || die "pre-deploy backup failed; nothing was changed"
  echo "$previous_commit" > "$STATE_DIR/previous_commit"
  db_revision > "$STATE_DIR/previous_revision" || true
fi

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  log "Build image persian-voice-assistant:$APP_VERSION"
  "${COMPOSE[@]}" build --pull
else
  docker image inspect "persian-voice-assistant:$APP_VERSION" >/dev/null 2>&1 \
    || die "SKIP_BUILD=1 but image persian-voice-assistant:$APP_VERSION is not on this server"
fi

log "Start (migrations run first in the 'migrate' service)"
"${COMPOSE[@]}" up -d --remove-orphans

log "Wait for health"
deadline=$((SECONDS + 180))
while :; do
  migrate_state=$("${COMPOSE[@]}" ps -a --format '{{.Service}} {{.State}} {{.ExitCode}}' | awk '$1=="migrate"{print $2" "$3}')
  case "$migrate_state" in
    "exited 0"|"") ;;
    exited*) "${COMPOSE[@]}" logs --tail 50 migrate; die "migrations failed (see above). Roll back: deploy/rollback.sh";;
  esac
  unhealthy=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' | awk '$2!="healthy" && $2!=""{print $1}' | tr '\n' ' ')
  [ -z "$unhealthy" ] && [ "$migrate_state" = "exited 0" ] && break
  [ $SECONDS -ge $deadline ] && { "${COMPOSE[@]}" ps; die "not healthy after 180 s: $unhealthy"; }
  sleep 5
done
"${COMPOSE[@]}" ps

log "Checks"
curl -fsS http://127.0.0.1:8000/health/live >/dev/null && echo "api live: ok"
curl -sS http://127.0.0.1:8000/health | python3 -m json.tool || echo "WARN: /health reports a problem (see above)"
host=$(grep -E '^DASHBOARD_HOST=' .env | cut -d= -f2-)
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$host/login" || true)
echo "dashboard https://$host/login -> HTTP $code (expect 200)"

db_revision > "$STATE_DIR/current_revision" || true
git rev-parse HEAD > "$STATE_DIR/current_commit"
docker image prune -f --filter "until=168h" >/dev/null || true
log "Deployed $APP_VERSION"
