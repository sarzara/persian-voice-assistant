#!/usr/bin/env bash
# Return to the commit that was running before the last deploy.
#
#   deploy/rollback.sh                 # downgrade the schema if needed, then deploy the previous commit
#   deploy/rollback.sh --restore FILE  # instead restore the pre-deploy backup (use when a downgrade would lose data)
set -Eeuo pipefail
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
STATE_DIR=.deploy

[ -f "$STATE_DIR/previous_commit" ] || { echo "no previous deploy recorded in $STATE_DIR" >&2; exit 1; }
prev_commit=$(cat "$STATE_DIR/previous_commit")
prev_rev=$(cat "$STATE_DIR/previous_revision" 2>/dev/null || true)
cur_rev=$("${COMPOSE[@]}" exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT version_num FROM alembic_version"' | tr -d '[:space:]')
# The image of the commit that is running now (it knows the newer migrations).
APP_VERSION=$(git rev-parse --short HEAD)
export APP_VERSION

echo "Rolling back to $(git rev-parse --short "$prev_commit"); schema $cur_rev -> ${prev_rev:-unknown}"

if [ "${1:-}" = "--restore" ]; then
  [ -n "${2:-}" ] || { echo "usage: deploy/rollback.sh --restore /var/backups/pva/<file>.dump" >&2; exit 1; }
  git checkout --quiet --detach "$prev_commit"
  deploy/restore.sh "$2"
elif [ -n "$prev_rev" ] && [ "$prev_rev" != "$cur_rev" ]; then
  # The CURRENT image knows the newer migrations, so it performs the downgrade.
  echo "Downgrading schema to $prev_rev (some downgrades drop data; see migration files)"
  "${COMPOSE[@]}" run --rm --no-deps -T migrate alembic downgrade "$prev_rev"
fi

git checkout --quiet --detach "$prev_commit"
APP_VERSION=$(git rev-parse --short HEAD)
if docker image inspect "persian-voice-assistant:$APP_VERSION" >/dev/null 2>&1; then
  SKIP_PULL=1 SKIP_BUILD=1 deploy/deploy.sh   # the old image is still on the server: no rebuild
else
  SKIP_PULL=1 deploy/deploy.sh
fi
echo "Rolled back. To return to the branch later: git checkout main && deploy/deploy.sh"
