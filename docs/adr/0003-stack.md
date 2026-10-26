# ADR-0003: технологический стек

- Статус: **proposed** (ждёт [Q18–Q21](../../QUESTIONS.md))
- Дата: 2026-10-26

## Решение

| Слой | Выбор |
|---|---|
| Монорепо | pnpm workspaces, TypeScript strict, Biome, lefthook |
| Frontend | React 19, Vite, Tailwind v4, TanStack Router/Query, Zustand |
| Backend | Node 24, Fastify |
| БД | PostgreSQL 17 + pgvector |
| Очередь | pg-boss поверх Postgres |
| Браузер | Playwright + Chromium в отдельном контейнере |
| LLM | DeepSeek через OpenAI-совместимый API за интерфейсом `LlmProvider` |
| Запуск | docker compose на одном VPS в РФ |

## Почему

Playwright первого класса в Node; общий язык фронта и бэка упрощает контракты
и переиспользование типов. Postgres даёт и домен, и векторный поиск одним
узлом и выдерживает конкурентные воркеры, в отличие от SQLite. Стек фронтенда
совпадает с BPMN.Space, откуда берётся дизайн-метод.

## Отклонено

SQLite (конкурентность, pgvector), Redis (лишний узел в MVP), Kubernetes (один
сервер), Python на бэкенде (разрыв типов с фронтом, слабее интеграция с
Playwright для нашего сценария).
