# ADR-0003: технологический стек

- Статус: **accepted** (решения владельца Q18–Q21, 2026-10-27; заменяет редакцию с Node-бэкендом)

## Решение

| Слой | Выбор |
|---|---|
| Бэкенд | **Python 3.13**, FastAPI, Pydantic v2, SQLAlchemy 2 (async) + asyncpg, Alembic |
| Пакеты бэкенда | `uv` (lock-файл в репозитории), `ruff` (lint + format), `pyright` (strict для `src/`), `pytest` + `pytest-asyncio` |
| Браузер | Playwright for Python + Chromium в отдельном процессе-сервисе |
| Фоновые задания | Свой воркер поверх Postgres (`SELECT … FOR UPDATE SKIP LOCKED`), без Redis |
| HTTP к вузам | `httpx` (async) через EgressProxy, с таймаутами и ретраями (`tenacity`) |
| Извлечение документов | `pypdf`, `python-docx`, `selectolax` (HTML), `openpyxl` |
| LLM | DeepSeek через OpenAI-совместимый API за протоколом `LlmProvider` |
| Поиск | Postgres FTS (`russian`) в MVP; эмбеддинги за протоколом `Embedder` ([ADR-0006](0006-retrieval-mvp.md)) |
| Фронтенд | TypeScript, React 19, Vite, Tailwind v4, TanStack Router + Query, Zustand, Biome |
| Пакеты фронтенда | `pnpm` workspaces |
| Контракты | JSON Schema в `contracts/` → Pydantic (`datamodel-code-generator`) и TS (`json-schema-to-typescript`); REST — OpenAPI из FastAPI → `openapi-typescript` |
| Запуск | docker compose на одном VPS; локально — compose для Postgres и прокси, процессы — нативно |

## Почему

Python выбран владельцем. Для нашей задачи он ещё и удобен: Playwright for Python зрелый, а
разбор PDF, DOCX и HTML в Python богаче, чем в Node. Pydantic даёт валидацию контрактов на
границе бесплатно. Postgres держит и домен, и полнотекстовый поиск, и очередь заданий — один
stateful-узел на маленьком VPS.

## Отклонено

Node-бэкенд (решение владельца), Redis и Celery (лишний узел: очередь на Postgres достаточна
для пула из десятка человек), Kubernetes (один сервер), stealth-форки браузера (запрещено).
