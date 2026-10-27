# W1 — Платформа и API

Сначала прочитай [общие правила](README.md).

## Владеешь

`backend/src/botai/{auth,vault,spaces,domain,jobs,usage}/`, `backend/src/botai/db/` (модели и
миграции после этапа 0), `backend/tests/{auth,vault,spaces,domain,jobs,usage}/`.

## Цель

Всё, на чём стоят остальные потоки: пользователи пула, Vault, пространства и подключения, чтение
домена, очередь заданий с SSE и ответами на блоки, квоты модели. Заглушки с финальными
сигнатурами уже есть — замени `not_implemented` на реализацию, сигнатуры и пути не меняй (их
сгенерированные TS-типы уже использует фронтенд).

## Срезы (по порядку, каждый — отдельный PR)

1. **Миграция и Vault.** `alembic revision --autogenerate -m initial` уже сделана в этапе 0 —
   проверь, что `upgrade head` создаёт все таблицы. `vault/pg.py`: `PgVault` по протоколу
   `core.vault.Vault` (таблица `secrets`, `seal/unseal` из `core.vault`, upsert по
   `(user_id, connection_id, kind)`, `expires_at` для `transient`, фоновая очистка истёкших).
   `vault/factory.py` → `PgVault`. Тесты: шифртекст в БД не содержит маркера
   `P@ssw0rd-FIXTURE`; строка, перенесённая на другого пользователя, не расшифровывается.
2. **Auth.** Регистрация только по приглашению (код показывается один раз, в БД — sha256),
   argon2 для пароля BotAI, cookie-сессия (`httpOnly`, `SameSite=Lax`, `Secure` вне dev, токен —
   32 байта, в БД — sha256), `current_user` / `owner_user` в `auth/deps.py`. CLI:
   `python -m botai.auth.cli bootstrap-owner --email … --name …` (пароль через `getpass`) и
   `create-invite`. Тесты: без cookie — 401; чужая сессия не работает; инвайт одноразовый.
3. **Пространства и подключения.** Роутер `spaces/router.py` полностью. Реестр `universities`
   выученный: при создании пространства по названию/ссылке ищется или создаётся строка; акцент —
   детерминированный тон из окна 235…345 по хешу slug (стайлгайд §2); предупреждение
   `third_party_warning` — поле, которое заполняет агент обучения. `create_connection` создаёт
   подключение `draft` и ставит задание `connect` через `services.queue` (дедупликация).
   `put_secret` кладёт токен/iCal/ключ в Vault; `forget_password` удаляет `password` и `login`.
   `has_password` в ответе — через `Vault.has`, значение никогда не возвращается.
   Тесты на тенант-изоляцию: пользователь B не видит и не трогает пространство и подключение A
   (404, не 403 — не раскрываем существование).
4. **Чтение домена.** `domain/repo.py` и `domain/router.py`: `today`, `schedule` (с
   `conflicts_with` — пересечения по времени между **разными** пространствами), `deadlines`
   (срок — `effective_due_at` или `due_at`), остальное. `provenance.stale` — по
   `connection.status in (stale, needs_reauth, repairing)` или по свежести спецификации.
   `list_sections` — счётчики по видам данных пространства.
5. **Очередь заданий и SSE.** `jobs/queue.py`: `PgJobQueue` (дедупликация queued/running по
   `(kind, connection_id)`). `jobs/runner.py`: `run_forever(handlers)` — `SELECT … FOR UPDATE SKIP
   LOCKED`, `PgJobContext` (события в `job_events` с монотонным `seq`, `ask` переводит задание в
   `waiting_user` и ждёт строку в `job_answers` через `LISTEN/NOTIFY` с запасным опросом, TTL
   ожидания 30 мин → `cancelled`; `ask_secret` ждёт секрет в Vault `kind=transient`,
   `connection_id = block_id`, читает и удаляет), ретраи задания по [LLD §5](../../architecture/lld.md)
   (1, 5, 30 мин для `source_unavailable`/`source_rate_limited`), планировщик sync (раз в минуту:
   задания `sync` для активных подключений с `next_sync_at <= now`). SSE
   `GET /jobs/{id}/events`: `sse-starlette`, `Last-Event-ID`, `:keepalive` каждые 15 с, только
   владелец задания. `POST /jobs/{id}/answers`: для блока `input` с `secret=true` значение — в
   Vault `transient` (TTL 10 мин), в `job_answers` — только факт ответа (`is_secret=true`,
   `values=null`); повторный ответ — 409 `block_answered`. `jobs/factory.py` → `PgJobQueue`.
   Тесты: два воркера не берут одно задание; секрет не попадает в `job_events` и `job_answers`;
   SSE отдаёт пропущенное после `Last-Event-ID`.
6. **Квоты.** `usage/service.py`: `check_quota` (суточный бюджет токенов и запросов в минуту из
   настроек; `quota_exceeded` с временем сброса), `record_usage` (строка `llm_usage`, стоимость
   через `cost_usd`, `peak` — по окну DeepSeek: 01:00–04:00 и 06:00–10:00 UTC пн–пт).
   `usage/router.py`. `auth.me` отдаёт квоту.

## Готово, когда

Все срезы смёржены; `make check` зелёный; заглушек `not_implemented` в твоих модулях нет; сквозной
тест: регистрация по инвайту → пространство → подключение → задание `connect` в очереди →
SSE отдаёт события фиктивного обработчика → ответ на блок, включая секретный.

## Не делаешь

Логику входа в вуз (W2), обучение и sync (W3), чат (W5), интерфейс (W4).
