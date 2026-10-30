# syntax=docker/dockerfile:1.7
# One image for api, worker and egress proxy. Built in CI: GitHub runners reach the Playwright CDN.

FROM node:22-bookworm-slim AS web
WORKDIR /src
RUN npm i -g pnpm@12.4.2
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc biome.json ./
COPY apps/web/package.json apps/web/package.json
COPY packages/design/package.json packages/design/package.json
RUN pnpm install --frozen-lockfile
COPY apps apps
COPY packages packages
COPY contracts contracts
RUN pnpm --filter @botai/web build

FROM python:3.13-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright PATH=/opt/venv/bin:$PATH
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
RUN playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY backend/alembic.ini ./
COPY backend/src src
RUN uv sync --frozen --no-dev
COPY --from=web /src/apps/web/dist /app/static
RUN useradd --system --user-group --uid 10001 --home /app botai && chown -R botai /app \
    && install -d -m 700 -o botai -g botai /var/log/botai/diagnostics
USER botai
ENV STATIC_DIR=/app/static
EXPOSE 8000
CMD ["uvicorn", "botai.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
