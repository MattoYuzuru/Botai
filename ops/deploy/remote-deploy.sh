#!/usr/bin/env bash
# Runs on mnema as botai-deploy, called by .github/workflows/deploy.yml.
# stdin: GHCR user, GHCR token, DeepSeek key (one per line). Nothing secret is echoed.
# Touches only the `botai` compose project; Mnema containers, images and volumes are never touched.
set -euo pipefail
umask 077

TAG="${1:?image tag}"
DIAGNOSTICS="${2:-keep}"
case "$DIAGNOSTICS" in keep|on|off) ;; *) echo "diagnostics must be keep, on or off" >&2; exit 64 ;; esac
[[ "$TAG" =~ ^[a-f0-9]{7,40}$ ]] || { echo "image tag must be a commit SHA" >&2; exit 64; }
cd /srv/botai

prev="$(sed -n 's/^IMAGE_TAG=//p' compose.vars)"
previous_diagnostics="$(sed -n 's/^BOTAI_DIAGNOSTICS=//p' compose.vars)"
case "$previous_diagnostics" in true|false|'') ;; *) echo "invalid stored diagnostics mode" >&2; exit 64 ;; esac
case "$DIAGNOSTICS" in
  on) next_diagnostics=true ;;
  off) next_diagnostics=false ;;
  keep) next_diagnostics="${previous_diagnostics:-false}" ;;
esac

rollback_vars="$(mktemp .compose.vars.rollback.XXXXXX)"
cp compose.vars "$rollback_vars"
registry_logged_in=false
runtime_changed=false
cleanup() {
  rm -f "$rollback_vars"
  if "$registry_logged_in"; then docker logout ghcr.io >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

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
registry_logged_in=true
unset GHCR_TOKEN

# Write atomically; keep unrelated settings and never print compose.vars (it holds a DB secret).
awk -v tag="$TAG" -v diagnostics="$next_diagnostics" '
  /^IMAGE_TAG=/ { next }
  /^BOTAI_DIAGNOSTICS=/ { next }
  { print }
  END { print "IMAGE_TAG=" tag; print "BOTAI_DIAGNOSTICS=" diagnostics }
' compose.vars > compose.vars.tmp
mv compose.vars.tmp compose.vars

dc() { docker compose -p botai -f compose.prod.yaml --env-file compose.vars "$@"; }

healthy() {
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 3 http://127.0.0.1:18090/api/health >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

rollback() {
  echo "$1 failed for ${TAG}; restoring ${prev} and its diagnostics mode" >&2
  cp "$rollback_vars" compose.vars
  if "$runtime_changed"; then
    if ! dc up -d --remove-orphans || ! healthy; then
      echo "rollback health check failed; operator action required" >&2
    fi
  fi
  exit 1
}

dc pull --quiet || rollback "image pull"
dc --profile tools run --rm migrate || rollback "migration"
runtime_changed=true
dc up -d --remove-orphans || rollback "container update"
healthy || rollback "health check"
echo "deployed ${TAG}; diagnostics=${next_diagnostics}"

# Keep the three newest BotAI image tags; never prune other projects' images.
docker image ls ghcr.io/mattoyuzuru/botai-app --format '{{.Tag}} {{.CreatedAt}}' |
  grep -v '^latest ' | sort -k2 -r | awk 'NR>3 {print $1}' |
  while read -r old; do
    [ "$old" = "$TAG" ] || docker image rm "ghcr.io/mattoyuzuru/botai-app:${old}" >/dev/null 2>&1 || true
  done
