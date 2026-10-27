#!/usr/bin/env bash
# Runs on mnema as botai-deploy, called by .github/workflows/deploy.yml.
# stdin: GHCR user, GHCR token, DeepSeek key (one per line). Nothing secret is echoed.
# Touches only the `botai` compose project; Mnema containers, images and volumes are never touched.
set -euo pipefail
umask 077

TAG="${1:?image tag}"
cd /srv/botai
IFS= read -r GHCR_USER
IFS= read -r GHCR_TOKEN
IFS= read -r DEEPSEEK_KEY

if [ -n "$DEEPSEEK_KEY" ]; then
  grep -v '^DEEPSEEK_API_KEY=' app.secrets > app.secrets.tmp || true
  printf 'DEEPSEEK_API_KEY=%s\n' "$DEEPSEEK_KEY" >> app.secrets.tmp
  mv app.secrets.tmp app.secrets
fi
unset DEEPSEEK_KEY

printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null
unset GHCR_TOKEN

prev="$(sed -n 's/^IMAGE_TAG=//p' compose.vars)"
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=${TAG}/" compose.vars

dc() { docker compose -p botai -f compose.prod.yaml --env-file compose.vars "$@"; }

healthy() {
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 3 http://127.0.0.1:18090/api/health >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

dc pull --quiet
dc --profile tools run --rm migrate
dc up -d --remove-orphans

if healthy; then
  echo "deployed ${TAG}"
else
  echo "health check failed for ${TAG}; rolling back to ${prev}" >&2
  sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=${prev}/" compose.vars
  dc up -d --remove-orphans
  docker logout ghcr.io >/dev/null
  exit 1
fi
docker logout ghcr.io >/dev/null

# Keep the three newest BotAI image tags; never prune other projects' images.
docker image ls ghcr.io/mattoyuzuru/botai-app --format '{{.Tag}} {{.CreatedAt}}' |
  grep -v '^latest ' | sort -k2 -r | awk 'NR>3 {print $1}' |
  while read -r old; do
    [ "$old" = "$TAG" ] || docker image rm "ghcr.io/mattoyuzuru/botai-app:${old}" >/dev/null 2>&1 || true
  done
