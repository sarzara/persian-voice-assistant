#!/usr/bin/env bash
# Outbound reachability from the server, BEFORE Docker is installed (uses curl and any HTTPS_PROXY in the env).
# After deployment, run the in-app check instead (same proxy settings as the app):
#   docker compose -f docker-compose.prod.yml run --rm --no-deps api python -m app.cli check-outbound
set -uo pipefail
targets=(
  "Telegram Bot API|https://api.telegram.org"
  "DeepSeek API|https://api.deepseek.com/models"
  "Resend API|https://api.resend.com"
  "Google OAuth|https://oauth2.googleapis.com/token"
  "Google Calendar API|https://www.googleapis.com/calendar/v3/colors"
  "Let's Encrypt|https://acme-v02.api.letsencrypt.org/directory"
  "GitHub|https://github.com"
  "Docker Hub registry|https://registry-1.docker.io/v2/"
  "Docker apt repo|https://download.docker.com/linux/ubuntu/"
  "PyPI|https://pypi.org/simple/pip/"
  "Debian packages|http://deb.debian.org/debian/"
  "Vosk models|https://alphacephei.com/vosk/models"
)
printf '%-22s %-6s %s\n' "SERVICE" "HTTP" "VERDICT"
for t in "${targets[@]}"; do
  name=${t%%|*}; url=${t#*|}
  code=$(curl -s -o /dev/null -m 12 -w '%{http_code}' "$url")
  case "$code" in
    000) verdict="BLOCKED or unreachable (timeout / DNS / reset)";;
    403|451) verdict="REFUSED ($code): likely a geo or sanctions block";;
    *) verdict="reachable";;
  esac
  printf '%-22s %-6s %s\n' "$name" "$code" "$verdict"
done
