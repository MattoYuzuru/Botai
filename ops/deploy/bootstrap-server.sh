#!/usr/bin/env bash
# One-time server setup on mnema, run with sudo by the coordinator (docs/plan/deployment.md).
# Idempotent: existing secrets are kept. Does not touch Mnema, Caddy or other users.
# Usage: sudo bash bootstrap-server.sh "<CI public key>"
set -euo pipefail
PUBKEY="${1:?CI public key}"
USER_NAME=botai-deploy
ROOT=/srv/botai

id "$USER_NAME" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null
usermod -aG docker "$USER_NAME"

install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/${USER_NAME}/.ssh"
# `restrict` forbids port/agent/X11 forwarding and PTYs; plain commands and scp still work.
printf 'restrict %s\n' "$PUBKEY" > "/home/${USER_NAME}/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "/home/${USER_NAME}/.ssh/authorized_keys"
chmod 600 "/home/${USER_NAME}/.ssh/authorized_keys"

install -d -m 750 -o "$USER_NAME" -g "$USER_NAME" "$ROOT" "$ROOT/backups"

# v4 agent runtime: persistent per-run workspaces (code_exec/fs_* files, source parser artifacts)
# and content-addressed source artifacts. Owned by the app UID 10001 (the containers run as that
# user); the worker bind-mounts these host paths at the SAME absolute path, so the container
# executor can bind-mount the same path for sandbox containers (docs/v4/13-executor.md §5).
install -d -m 700 -o 10001 -g 10001 "$ROOT/agent-workspaces" "$ROOT/source-artifacts"

umask 077
if [ ! -f "$ROOT/app.secrets" ]; then
  {
    printf 'BOTAI_KEK=%s\n' "$(openssl rand -base64 32)"
    printf 'BOTAI_KEK_ID=k1\n'
    printf 'BOTAI_PROXY_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'DEEPSEEK_API_KEY=\n'
  } > "$ROOT/app.secrets"
fi
if [ ! -f "$ROOT/compose.vars" ]; then
  {
    printf 'IMAGE_TAG=latest\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
  } > "$ROOT/compose.vars"
fi
# The v4 sandbox runs as a sibling container: the worker needs the host docker group id to talk to
# /var/run/docker.sock. Non-secret; kept in compose.vars so `compose` can pass it as group_add.
if ! grep -q '^DOCKER_GID=' "$ROOT/compose.vars"; then
  docker_gid="$(getent group docker | cut -d: -f3 || true)"
  printf 'DOCKER_GID=%s\n' "${docker_gid:?docker group must exist}" >> "$ROOT/compose.vars"
fi
chown "$USER_NAME:$USER_NAME" "$ROOT/app.secrets" "$ROOT/compose.vars"
chmod 600 "$ROOT/app.secrets" "$ROOT/compose.vars"

# Nightly database dump, 14 days retention. The KEK is not in the dump (it lives in app.secrets).
cat > "/etc/cron.d/botai-backup" <<CRON
SHELL=/bin/bash
15 3 * * * ${USER_NAME} cd ${ROOT} && docker compose -p botai -f compose.prod.yaml --env-file compose.vars exec -T postgres pg_dump -U botai botai | gzip > backups/botai-\$(date +\%F).sql.gz && find backups -name 'botai-*.sql.gz' -mtime +14 -delete
CRON
chmod 644 /etc/cron.d/botai-backup

echo "bootstrap done: user ${USER_NAME}, root ${ROOT}"
