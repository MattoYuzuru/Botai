# Фаза 0.5: независимый аудит реализации (lane E, verifier)

**Дата:** первичный аудит — 2026-10-31, ~17:50–18:00 MSK на `c7710a3` (тогда часть кода была
в worktree без коммита); **перепроверка — 2026-10-31, ~18:50 MSK на интеграционном коммите
`e8b8602`** («Пивот v4: свободный harness в продукте, источники, pgvector и действия»).
На момент перепроверки `git status --porcelain` пуст: всё ниже — про **закоммиченный** код
`e8b8602`, а не про WIP. Источник правды — файлы и строки в этом коммите; product-код аудит
не менял (только чтение + read-only ssh на prod).

Что требовалось по [09-prod-login-incidents.md](09-prod-login-incidents.md) и плану
[05](05-implementation-plan.md) 0.5.4–0.5.7; что реально есть в коде; чем это доказывается.

| Пункт | Статус на `e8b8602` | Главное доказательство |
|---|---|---|
| 0.5.4 CONNECT: фазы login/research/sync + остановка + честный статус | **done (criterion met), с оговоркой**: фаза видна как строка статуса; контрактного поля `phase`/ETA нет | `worker.py:19-36`, `agent/v4/job_runtime.py:52-113,288,297`, `agent/v4/dispatch.py:68-144,267-336,384,810,876-899`; остановка `jobs/router.py:61`, `jobs/runner.py:527`, `timeline-view.tsx:120-122`, `timeline.ts:115`; L1: `tests/jobs/test_v4_dispatch_table.py:23,37,50`, `tests/jobs/test_agent_dispatch_v4.py:223,365,427` |
| 0.5.5 Caddy access-лог, таймауты, алерты | **done** (закоммичено в `e8b8602`, prod live перепроверен 18:49 MSK) | `ops/deploy/caddy-botai.conf`, `ops/deploy/caddy-access-report.sh`, `docs/plan/deployment.md` §Caddy; live-проверка на `mnema` (ниже) |
| 0.5.6 Network-first чтение источников | **partial+**: backend-механика API/JSON чтения закоммичена и в toolset v4; живой ЦУ-критерий v4-агентом не проверен; чтение v4-данных экранами — в работе (lane H) | `sources/v4/runner.py:98,443`, `agent/v4/tools/sources.py:619`, `agent/v4/factory.py:229,265,359,367`; L1 `tests/sources/v4/test_runner.py:26,41,58,357` |
| 0.5.7 Секрет из чата | **done для сценария инцидента**, ужесточение не всеобъемлющее | `agent/conversations.py:269,405-423`, `agent/router.py:88`, `tests/agent/test_secret_guard.py`, `tests/jobs/test_conversations.py:831,833` |

## 0.5.4. Явные фазы CONNECT и остановка

**Что есть в `e8b8602` (всё закоммичено).**

- **Переключение движка.** `backend/src/botai/worker.py:19-36`: `handler_table(settings)` собирает
  v3-таблицы и при `agent_engine == "v4"` накладывает `make_v4_handlers()` (строки 33-36);
  при `v3` таблица побайтово прежняя. Дубликатов kind'ов нет — тест
  `tests/jobs/test_v4_dispatch_table.py:50`.
- **Фазы login → research → sync.** `backend/src/botai/agent/v4/job_runtime.py`:
  `TOOL_PHASES` (`:52-70`) сопоставляет инструменты фазам (secret_*/browser_* → login,
  source_create/probe/read_live/update_playbook/repair → research, source_sync/rag_ingest → sync),
  `PHASE_ORDER`/`PHASE_LABELS` (`:71-76`), `PhaseReporter` (`:88-113`) двигает фазу только вперёд
  и пишет её в job-статус: `await self._ctx.status(f"{prefix}: {label}")` (`:108`) — то есть
  в `job.stage` и в событие `status`, которое видит UI (`jobs/events.py:40`).
  CONNECT использует префикс «Подключение» (`dispatch.py:68,71,124,138`), REPAIR — «Починка»
  (`dispatch.py:69,316,330`), SYNC — `handle_sync` (`dispatch.py:196`).
- **Явные фазы только у CONNECT/REPAIR**: `AGENT`-job идёт без фаз (у свободного агента нет
  фиксированной последовательности фаз — это допустимо).
- **Остановка** (была и до v4, работает на любом job):
  `POST /api/v1/jobs/{job_id}/cancel` (`jobs/router.py:61-63` → `answers.cancel_job`), воркер
  отменяет asyncio-задачу (`jobs/runner.py:704-708`), обработчик получает `JobCancelled`;
  терминальное состояние `cancelled` с честной строкой «Остановлено»
  (`jobs/runner.py:527`). В UI — `cancelTask` (`apps/web/src/features/chat/timeline-view.tsx:120-122`,
  `agent-blocks/task-block.tsx:41,55`) и текст «Остановлено. Уже найденные данные сохранены.»
  (`features/chat/timeline.ts:115`).
- **Честный статус.** `job_runtime.py:288 stage_of` (готово / остановлен лимитом / статус),
  `:297 error_of` (код ошибки runtime → `BotaiError`), `:269 run_result_dict` (итог в
  job-результат). Конфигурационный отказ (prod-local-executor, нет DB/RAG) — `BotaiError
  NOT_IMPLEMENTED` с понятным текстом, а не тихий запуск.

**L1/fakeuni-доказательство:** `tests/jobs/test_agent_dispatch_v4.py` — CONNECT исследует fakeuni
через `source_*` и ставит первый sync (`:223`), SYNC проигрывает сохранённый playbook без модели
(`:365`), REPAIR падает обратно в агента и активирует исправленный playbook (`:427`); таблица
обработчиков — `tests/jobs/test_v4_dispatch_table.py:23,37,50`.

**Оговорки (честно):**
1. «Фаза» — это **строка** `job.stage` (≤120 симв.), а не поле контракта: `TaskState` в
   `contracts/agent_ui.py:186` остаётся `queued|running|waiting_user|done|failed|cancelled`,
   поля `phase` в состоянии/событиях/чате нет. UI показывает текст, но не может рисовать
   фазовый прогресс/ETA программно.
2. **Прогресс/ETA не реализованы** — их нет ни в v3, ни в v4; план упоминал их как часть 0.5.4.
   Критерий приёмки 0.5.4 («видно, что идёт — вход или исследование; 22-минутный лоудер после
   входа невозможен») выполнен за счёт фаз + `botai_research_no_progress_slices` (0.2).
3. Отдельного **живого** CONNECT-прогона v4 на вузе после включения флага ещё не было
   (сертификационный L3-прогон — job-путь `AGENT`, не CONNECT).

## 0.5.5. Caddy access-лог, таймауты, алерты

**Закоммичено в `e8b8602`** (`git log -1 -- ops/deploy/caddy-botai.conf ops/deploy/caddy-access-report.sh`
→ `e8b8602`):

- `ops/deploy/caddy-botai.conf` — справочный блок `botai.mnema.app`: `log { output file
  /var/log/caddy/botai-access.log { roll_size 20MiB; roll_keep 10; roll_keep_for 720h } format
  filter { request>uri regexp \?.* ?REDACTED; request>remote_ip ip_mask 24 48;
  request>client_ip ip_mask 24 48; request>headers>Cookie delete; …Authorization delete;
  …X-Api-Key delete; …X-Forwarded-For delete; resp_headers>Set-Cookie delete } }`;
  `reverse_proxy 127.0.0.1:18090 { flush_interval -1; transport http { dial_timeout 2s;
  response_header_timeout 30s } }`.
- `ops/deploy/caddy-access-report.sh` — сводка: 499/aborted, 5xx, 403 на WS, медленные ответы, WS;
  exit code 2 при превышении порогов (cron/journal), 66 — нет лога.
- `docs/plan/deployment.md` §«Caddy: access-лог, таймауты, наблюдаемость (0.5.5)» — runbook:
  бэкап, `caddy adapt`, `caddy validate` **от пользователя caddy** (validate от root создаёт
  root-файл лога и reload падает), `systemctl reload` (не restart), откат.

**Проверено на production (read-only, `mnema`):**

- 17:50 MSK: `grep` по `/etc/caddy/Caddyfile` — блок с `botai-access.log`, `?REDACTED`,
  `ip_mask 24 48`, `dial_timeout 2s`, `response_header_timeout 30s` (строки 93-115);
  `caddy-access-report.sh --all` → `entries 5, 2xx=5, 403=0 ws403=0 499=0, 5xx=0, result: ok`;
  поля живой строки: `uri=/api/health?REDACTED`, `remote_ip=… .0` (маска /24),
  Cookie/Authorization/Set-Cookie в заголовках отсутствуют.
- 18:49 MSK (перепроверка после `e8b8602`): Caddyfile-блок на месте (8 совпадающих строк),
  отчёт снова `entries 5, 2xx=5, 4xx=0, 5xx=0, 499=0, result: ok` — лог жив, новых ошибок нет;
  выборка мала, потому что в окне только health-check'и (реального пользовательского трафика не
  было), — ограничение наблюдения, а не отказ.

**Честные ограничения:** (а) «алерт» — exit code 2 скрипта + запись в journal; отдельного канала
уведомлений в проекте нет (D-001), доставку владельцу настраивать отдельно; (б) WS close-code в
access-лог не попадает (только `101` и длительность) — причина закрытия остаётся в приложенческой
диагностике (`BOTAI_DIAGNOSTICS`), это записано в runbook; (в) объём выборки мал (5 записей) —
распределение 499/504 появится по мере трафика.

## 0.5.6. Network-first чтение источников

**Закоммичено и подключено к v4-runtime в `e8b8602`:**

- `backend/src/botai/sources/v4/runner.py:98` — `ReadSpec.format: json|html|text`;
  `:443 read_live` вызывает URL сессией источника (cookie/token остаются внутри);
  инструмент `source_read_live` — `agent/v4/tools/sources.py:619`.
- `agent/v4/factory.py:229 build_source_toolkit`, `:265 build_rag_toolset`, `:359,367` — при
  `agent_engine == "v4"` `source_*`/`rag_*` входят в toolset run'а (в моём L3-прогоне они
  недоступны только потому, что bench намеренно отдаёт offline DB-фабрику — не product bug).
- L1-тесты fakeuni читают именно API: `tests/sources/v4/test_runner.py:26,41,58`
  (`/api/v2/timetable`, `/api/v2/tasks/deadlines`), `:357` «live read uses the source session».

**Чего нет как доказательства:** критерий плана «ЦУ читает задания через
`GET /api/micro-lms/tasks/student` (174 записи)» доказан только детерминированным
`bench/live` ([04-benchmark.md](04-benchmark.md) §8.1, 09 §6), **не** v4-агентом. Живой L3 по ЦУ
не входил в scope (только ВШЭ). По [README v4](../../README.md) чтение v4-данных производными
экранами (расписание/задания/оценки из строк домена `source_id`) — **в работе (lane H)**: строки
домена уже пишутся, но экраны пока читают v3-подключения.

**Что докажет:** живой ЦУ-прогон v4-агента с чтением `/api/micro-lms/tasks/student` (174 записи)
и сверка counts с `bench/live` на той же сессии. Для ВШЭ это уже сделано: сертификационный
прогон `e8b8602` нашёл `GET lk.hse.ru/api/get-myschedule` и сохранил 2779 записей
(см. [README](README.md) «Приёмка e8b8602»).

## 0.5.7. Секрет из чата

**В `e8b8602` (файлы те же, что на HEAD `c7710a3`):** детектор `looks_like_secret`
(`agent/conversations.py:269-290`: токены/cookie-строки через `scrub_secrets`, коды из цифр,
«пароль: …», короткие password-like строки), проверка **до сохранения** сообщения:
`conversations.py:405-423` — если задача ждёт защищённый ввод (`_waiting_secret`) или её ответ,
секретоподобный текст отклоняется с `409 secret_in_chat` («Пароль и код вводятся только в
защищённое поле, не в чат»); сообщение в `ChatMessage` не попадает
(`agent/router.py:88` → `conversations.post_message`). Клиент обрабатывает 409:
`apps/web/src/features/chat/outbox.ts:83-103` (`conflictKind`), сообщение стирается из
очереди/хранилища и не повторяется (`outbox.test.ts:270`, `chat-tasks.test.tsx:106`).
Тесты: `tests/agent/test_secret_guard.py`, `tests/jobs/test_conversations.py:831,833`.

**Честные ограничения:** (а) детект срабатывает, когда задача ждёт защищённый ввод; сообщение
«мой пароль …» в обычном чате без ожидающего job не отклоняется; (б) уже сохранённые ранее
сообщения не вычищаются задним числом (в инциденте 09 §2.4 сообщение 07:48:48 осталось в
`chat_messages`) — скрипта миграции/удаления нет; (в) код ошибки `secret_in_chat`, а план называл
`secret_detected_in_chat` — несовпадение имени, не поведения.

## Сертификация L3 на `e8b8602` (независимая приёмка)

Живой product-path прогон v4-агента (job-путь `AGENT`, `build_agent_wiring`, DeepSeek, Chromium,
Keychain-креды) на чистом дереве `e8b8602`:

- **status=answer → CERTIFIED**, security gate чистый: `messagesLeak=false`, `traceLeak=false`,
  raw sweep — 0 правок (маскирование `SecretBroker.mask` теперь покрывает **все** tool results,
  включая `code_exec`: `agent/v4/runtime.py:250 mask_result`, `:716,:738`, wiring
  `factory.py:379 AgentRuntime(..., mask=broker.mask)`).
- 28 шагов / 32 tool calls, 768 202 токена, **$0.013229**, 165.7 с.
- Вход ВШЭ через `secret_request`/`secret_use` (модель видела только handle); SSO → `keycloak-auth`;
  найден endpoint `GET https://lk.hse.ru/api/get-myschedule`; в workspace сохранены
  `hse_schedule.json` (3 498 770 Б, **2779 записей**), `hse_schedule.csv` (879 135 Б),
  `README_schedule.md`, `sessions/lk_hse.json`; финальный ответ 1689 символов с источником.
- Свидетельство: [README.md](README.md) «Приёмка e8b8602» +
  `live-hse-01M69AEFE4A99RYKTTNCSDNC1Q.json`; сырьё — вне репозитория.

## Production-readiness: включаем `BOTAI_AGENT_ENGINE=v4`

Состояние на `e8b8602` (всё перечисленное **закоммичено**, отдельной WIP-части больше нет):

| Что | Где (файл:строка) | Статус |
|---|---|---|
| ContainerExecutor: контейнер на вызов, uid 10001, read-only rootfs, cap-drop ALL, no-new-privileges, cgroups, timeout→`docker kill`, bind-mount workspace, env-allowlist | `agent/v4/executor/container.py`, `ops/docker/sandbox.Dockerfile` | готово |
| Выбор исполнителя: prod → только контейнер, `local` в prod — громкий отказ | `agent/v4/factory.py build_executor` | готово |
| Workspace/артефакты из настроек (`BOTAI_AGENT_WORKSPACE_ROOT`, `BOTAI_SOURCE_ARTIFACTS_DIR`), иначе temp | `agent/v4/factory.py workspace_root_from_settings/artifact_root_from_settings` | готово |
| Маскирование первичных секретов во всех tool results + чекпоинте/трассе | `agent/v4/runtime.py:250,716,738`, `factory.py:379` | готово, **проверено живым прогоном** |
| Task-injection fix (задача в диалоге, resume-safe) | `agent/v4/runtime.py:85-95,390-391` | готово, **проверено живым прогоном** |
| CI собирает/пушит sandbox-образ тем же тегом + `latest` + revision label; app-образ тоже | `.github/workflows/deploy.yml` (build job) | готово |
| Deploy: pull sandbox-образа, сверка revision label, зашифрованный `pg_dump` до DDL, «postgres → pg_isready → migrate → сервисы», удаление остатков `label=botai.agent.sandbox=1`, retention app+sandbox | `ops/deploy/remote-deploy.sh` | готово |
| compose.prod: `BOTAI_AGENT_ENGINE=${BOTAI_AGENT_ENGINE:-v3}`, `BOTAI_AGENT_EXECUTOR=container`, workspace/artifacts root, `BOTAI_SANDBOX_IMAGE` из `IMAGE_TAG`, hard stop $2, auto_continue=false | `ops/compose.prod.yaml` | готово |
| api+worker: `/var/run/docker.sock`, `group_add: DOCKER_GID`, оба хостовых каталога по одинаковому абсолютному пути | `ops/compose.prod.yaml`, `ops/deploy/bootstrap-server.sh` | готово |
| pgvector-образ Postgres, порядок «postgres-first-then-migrate», collation-процедура, проверенная точка восстановления | `ops/compose.prod.yaml`, `remote-deploy.sh`, `docs/plan/deployment.md` §pgvector | готово (restore заявлен Lead: users=4/connections=7/chat_messages=287/schedule_events=169, `alembic_version=a8d4f1c2b6e9`) |
| Runbook включения/проверок/откат v4 | `docs/plan/deployment.md` §v4-агент в production | готово |

**Состояние production на 18:50 MSK (read-only ssh):**

```
IMAGE_TAG:     c7710a3f918d
worker image:  ghcr.io/mattoyuzuru/botai-app:c7710a3f918d
```

То есть `e8b8602` в prod **ещё не выкачен** (CI/deploy шёл на момент отчёта). План: дождаться
деплоя `e8b8602` (pgvector + миграции + sandbox-образ + новый app-образ) при флаге **v3**,
проверить collation/`alembic head`/sandbox image revision, затем smoke и включение флага — по
чек-листу (не изменился), плюс:

1. **Задать soft-лимиты** в `/srv/botai/compose.vars`: `BOTAI_AGENT_LIMITS_STEPS/SECONDS/TOKENS/USD`
   (сейчас в compose только `BOTAI_AGENT_HARD_STOP_USD=2.0` и `AUTO_CONTINUE=false`; дефолты
   `None` = без предела, D-009) — решение владельца об эксплуатационных предохранителях.
2. RAG: `BOTAI_RAG_ENABLED` по умолчанию **off**; если нужен — включить и задать
   `BOTAI_RAG_PROVIDER`/`BOTAI_RAG_ARTIFACT_ROOT` (миграция pgvector уже едет).
3. Включить `BOTAI_AGENT_ENGINE=v4` в `compose.vars` → перезапуск api+worker → проверки из
   runbook (`Settings().agent_engine`, `docker version` в worker, sandbox image tag/revision) →
   smoke `AGENT` job: `done`, строки в `agent_runs`/`agent_run_events`, `code_exec` реально в
   контейнере, нет остатков `docker ps -a --filter label=botai.agent.sandbox=1`, файл появился в
   `/srv/botai/agent-workspaces/<run>`; затем один живой CONNECT владельцем через UI.
4. Откат: вернуть `BOTAI_AGENT_ENGINE=v3`, перезапустить api/worker; незавершённые v4-jobs
   завершатся понятной ошибкой; `agent_runs` остаются; каталоги workspace/artifacts не удалять.

**Остаточные риски/долги:** docker-сокет в api/worker = root-эквивалент на хосте (принято
владельцем, записано в runbook; sandbox-контейнер сокета и секретов не получает); API тоже
исполняет inline v4 source sync и может поднимать Chromium (долг, задокументирован
`16-sources-runner-tools.md`); sandbox-образ собирается по каждому SHA (без прав CI на ghcr push
деплой упадёт на pull); soft-лимиты/RAG-флаги — единственные «пустые» env-пункты.

## Tool-UX рекомендации (не блокеры, по итогам живых прогонов)

1. **`code_exec` и путаница `command` vs `code`.** В живых прогонах модель (deepseek-flash,
   thinking high) присылала `{"language":"bash","command":"…"}` вместо обязательного `code`;
   получала `invalid_arguments` и повторяла ошибку 1–10 раз за прогон, сжигая шаги.
   Рекомендация: hint «используйте поле `code`» в тексте ошибки и/или алиас `command` → `code`.
2. **Код ошибки таймаута браузерных действий.** `browser/control.py:1676` отдаёт
   `SOURCE_UNAVAILABLE` («Нажать: страница не ответила») — в живых прогонах 5–20% вызовов
   (клик по лендингу HSE, ожидание network_idle после SSO). Модель читает `source_unavailable`
   как проблему источника/БД. Рекомендация: отдельный код `browser_timeout`/`action_timeout`
   или hint «страница не ответила на действие; подождите/повторите».
3. **`browser_download` по динамической кнопке.** В сертификационном прогоне одна попытка
   скачивания по селектору вернула `not_found`; агент честно перешёл на fetch через `browser_eval`
   и выполнил задачу (ответ это упоминает). Не дефект, но полезен hint: для SPA-кнопок сначала
   дождаться элемента/состояния.
4. **`ask_user` для учётных данных.** Модель иногда просит логин обычным текстом (D-008 это
   допускает); в bench-прогонах это приводило к выдуманному username. Для продукта не баг, но в
   системном промпте/описании `ask_user(secret)` стоит явно предпочесть `secret_request`.

## Метод и границы аудита

- Первичный аудит (17:50–18:00 MSK) читал `c7710a3` + worktree; перепроверка (18:50 MSK) —
  закоммиченный `e8b8602` при пустом `git status`; строки/статусы сверены по файлам коммита.
- Prod проверялся только read-only (`ssh mnema`, `sudo -n` чтение Caddyfile и запуск
  `caddy-access-report.sh`, `docker inspect`, `sed` compose.vars); ничего не менялось.
- Полный `make check` и чужие тесты не запускались; ссылки на тесты — по их наличию в коммите;
  живой L3/L2 v4-harness — в [README.md](README.md) этого каталога (сертификационный прогон
  `e8b8602`: answer, 2779 записей, чистый security gate, $0.013229).
