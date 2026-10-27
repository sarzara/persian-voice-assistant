#!/usr/bin/env bash
# Build the images on THIS machine (e.g. your laptop, where Docker Hub, PyPI and alphacephei.com are
# reachable) and stream them to the server over SSH. Then deploy there with SKIP_BUILD=1.
#
#   deploy/ship-images.sh deploy@SERVER_IP            # from Linux, macOS, WSL or Git Bash
#
# The server must have the same commit checked out (git pull there first).
set -Eeuo pipefail
cd "$(dirname "$0")/.."
target=${1:?usage: deploy/ship-images.sh user@server}
APP_VERSION=$(git rev-parse --short HEAD)
export APP_VERSION
docker compose -f docker-compose.prod.yml build --pull
images=("persian-voice-assistant:$APP_VERSION" "${POSTGRES_IMAGE:-postgres:16-bookworm}" "${CADDY_IMAGE:-caddy:2.10.2}")
docker pull "${images[1]}" >/dev/null
docker pull "${images[2]}" >/dev/null
echo "Shipping ${images[*]} to $target ..."
docker save "${images[@]}" | gzip -1 | ssh "$target" 'gunzip | docker load'
echo "Done. On the server: cd ~/persian-voice-assistant && git pull && SKIP_BUILD=1 deploy/deploy.sh"
