#!/usr/bin/env bash
# Runs on mnema as botai-deploy, called by .github/workflows/deploy.yml.
# stdin: GHCR user, GHCR token, DeepSeek key, Exa key, Groq key (one per line). The last two may be
# missing or empty: a key that is not sent stays as it is on the server (to revoke one, delete its
# line on the server, docs/plan/deployment.md). The Groq key goes to speech.secrets, read by api only.
# Touches only the `botai` compose project; Mnema containers, images and volumes are never touched.
set -euo pipefail
umask 077

TAG="${1:?image tag}"
DIAGNOSTICS="${2:-keep}"
EXPECTED_REVISION="${3:-}"
case "$DIAGNOSTICS" in keep|on|off) ;; *) echo "diagnostics must be keep, on or off" >&2; exit 64 ;; esac
[[ "$TAG" =~ ^[a-f0-9]{7,40}$ ]] || { echo "image tag must be a commit SHA" >&2; exit 64; }
if [ -n "$EXPECTED_REVISION" ]; then
  [[ "$EXPECTED_REVISION" =~ ^[a-f0-9]{40}$ && "$EXPECTED_REVISION" == "$TAG"* ]] || {
    echo "expected revision must match the image tag" >&2; exit 64;
  }
fi
cd /srv/botai
# v4 runtime host dirs are created once by bootstrap-server.sh with sudo, owned by UID 10001, and
# bind-mounted into the worker at the same path. CI runs as botai-deploy and cannot chown them, so
# a missing dir is a setup error, not something to "fix" from a deploy.
DEPLOY_ROOT="$(pwd)"
for d in agent-workspaces source-artifacts; do
  [ -d "$DEPLOY_ROOT/$d" ] || {
    echo "missing $DEPLOY_ROOT/$d: run ops/deploy/bootstrap-server.sh once with sudo" >&2
    exit 78
  }
done
# Serialize CI and operator rollouts before reading/writing shared configuration.
exec 9>.deploy.lock
flock -w 300 9 || { echo "another BotAI deployment holds the lock" >&2; exit 75; }

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
IFS= read -r EXA_KEY || EXA_KEY=''
IFS= read -r SPEECH_KEY || SPEECH_KEY=''

# NAME=value replaces the NAME line of FILE (or adds it); an empty value changes nothing.
# Values are never printed and never appear in a command line.
set_secret() {
  local file="$1" name="$2" value="$3"
  [ -n "$value" ] || return 0
  grep -v "^${name}=" "$file" > "$file.tmp" || true
  printf '%s=%s\n' "$name" "$value" >> "$file.tmp"
  mv "$file.tmp" "$file"
}
set_secret app.secrets DEEPSEEK_API_KEY "$DEEPSEEK_KEY"
set_secret app.secrets EXA_API_KEY "$EXA_KEY"

# The speech key lives in speech.secrets, which only the api container reads (compose.prod.yaml):
# the worker runs a browser on foreign pages, the proxy and the migration do not need it. A key an
# earlier run left in app.secrets is moved here (idempotent), and the file always exists, because
# Compose refuses a service whose env_file is missing.
touch speech.secrets
if grep -q '^GROQ_API_KEY=' app.secrets; then
  legacy="$(sed -n 's/^GROQ_API_KEY=//p' app.secrets | tail -n 1)"
  grep -q '^GROQ_API_KEY=' speech.secrets || set_secret speech.secrets GROQ_API_KEY "$legacy"
  grep -v '^GROQ_API_KEY=' app.secrets > app.secrets.tmp || true
  mv app.secrets.tmp app.secrets
  unset legacy
fi
set_secret speech.secrets GROQ_API_KEY "$SPEECH_KEY"
unset DEEPSEEK_KEY EXA_KEY SPEECH_KEY
chmod 600 app.secrets speech.secrets

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
if [ -n "$EXPECTED_REVISION" ]; then
  actual_revision=$(docker image inspect "ghcr.io/mattoyuzuru/botai-app:${TAG}" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}') || rollback "image identity"
  [ "$actual_revision" = "$EXPECTED_REVISION" ] || rollback "image revision mismatch"
fi
# The v4 sandbox image is built per commit too. Compose has no service for it (the worker starts
# sibling containers through the host daemon), so pull it explicitly and verify the same revision
# label before any container can run agent code (docs/v4/13-executor.md §5).
SANDBOX_IMAGE="ghcr.io/mattoyuzuru/botai-sandbox:${TAG}"
docker pull "$SANDBOX_IMAGE" || rollback "sandbox image pull"
if [ -n "$EXPECTED_REVISION" ]; then
  sandbox_revision=$(docker image inspect "$SANDBOX_IMAGE" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}') || rollback "sandbox image identity"
  [ "$sandbox_revision" = "$EXPECTED_REVISION" ] || rollback "sandbox image revision mismatch"
fi
# A database-only encrypted recovery point before DDL. The encryption key is kept
# separately from backups and is unrelated to the Vault KEK. Never output either.
if [ -n "$(dc ps -q postgres)" ]; then
  install -d -m 700 backups .rollback-keys
  backup_name="pre-${TAG}-$(date -u +%Y%m%dT%H%M%SZ)"
  backup_key=".rollback-keys/${backup_name}.key"
  openssl rand -hex 32 > "$backup_key"
  chmod 600 "$backup_key"
  dc exec -T postgres pg_dump -U botai -Fc botai |
    openssl enc -aes-256-cbc -salt -pbkdf2 -pass "file:${backup_key}" \
      -out "backups/${backup_name}.dump.enc" || rollback "database backup"
fi
# Postgres may itself change image in this deploy (postgres:17-alpine -> pgvector/pgvector:pg17 for
# the `vector` extension): recreate it BEFORE migrations, otherwise a migration that needs the new
# image's extensions would run against the old server. `up -d postgres` is a no-op when unchanged.
wait_postgres() {
  for _ in $(seq 1 45); do
    if dc exec -T postgres pg_isready -U botai >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}
dc up -d postgres || rollback "postgres update"
wait_postgres || rollback "postgres health check"
dc --profile tools run --rm migrate || rollback "migration"
# A crashed/timed-out run can leave a labeled sandbox container behind; they are safe to remove
# before the new worker starts (the executor also removes its own container on every call).
docker ps -aq --filter label=botai.agent.sandbox=1 | xargs -r docker rm -f >/dev/null 2>&1 || true
runtime_changed=true
dc up -d --remove-orphans || rollback "container update"
healthy || rollback "health check"
echo "deployed ${TAG}; diagnostics=${next_diagnostics}"

# Keep the three newest app and sandbox image tags; never prune other projects' images.
for image in ghcr.io/mattoyuzuru/botai-app ghcr.io/mattoyuzuru/botai-sandbox; do
  docker image ls "$image" --format '{{.Tag}} {{.CreatedAt}}' |
    grep -v '^latest ' | sort -k2 -r | awk 'NR>3 {print $1}' |
    while read -r old; do
      [ "$old" = "$TAG" ] || docker image rm "${image}:${old}" >/dev/null 2>&1 || true
    done
done
