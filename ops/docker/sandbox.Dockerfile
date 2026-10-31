# syntax=docker/dockerfile:1.7
# Sandbox image for the v4 agent code executor (`ContainerExecutor`): Python 3.13, Node.js 22 and
# bash for agent-written code, running as the non-root uid 10001 (docs/v4/13-executor.md §5,
# docs/v4/02-architecture.md C5). The image bakes in no secrets and no repository content: the
# task workspace is bind-mounted at /workspace at run time, and the executor passes every
# environment variable explicitly.
#
# Build (from the repository root; the file needs no build context):
#   docker build -f ops/docker/sandbox.Dockerfile -t botai-sandbox:dev .
# Smoke check:
#   docker run --rm --user 10001:10001 botai-sandbox:dev \
#       sh -c 'python3 --version && node --version && bash --version | head -1'
# Tag and push (CI does this for deploy; the tag must match BOTAI_SANDBOX_IMAGE on the worker):
#   docker tag botai-sandbox:dev ghcr.io/mattoyuzuru/botai-sandbox:<sha-or-tag>
#   docker push ghcr.io/mattoyuzuru/botai-sandbox:<sha-or-tag>
#
# The app image (ops/docker/app.Dockerfile) carries only the Docker CLI; it does not include
# this sandbox. Keep both images small: the sandbox ships Node.js and Python because agent code
# needs them, nothing else.

# Node.js 22 is built for the same Debian release; only the runtime binary and npm are copied.
FROM node:22-bookworm-slim AS node

FROM python:3.13-slim-bookworm

# ca-certificates/curl/git: what agent-written code reasonably reaches for (D-003 free harness).
# libstdc++6: the Node.js binary from the official image links libstdc++/libgcc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && python3 --version && node --version && npm --version

# The executor runs agent code as this user (uid 10001) and bind-mounts the task workspace over
# /workspace. /workspace exists in the image so a hand-run container also works; the mount hides it.
RUN useradd --system --user-group --uid 10001 --home-dir /workspace --shell /bin/bash sandbox \
    && install -d -o sandbox -g sandbox -m 0777 /workspace

# Fixed runtime environment of the image (no secrets). The executor repeats the relevant
# variables on every `docker run`, because only what it passes reaches the container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /workspace
USER sandbox

# A keepalive for a hand-started container; the executor always supplies its own command.
CMD ["sleep", "infinity"]
