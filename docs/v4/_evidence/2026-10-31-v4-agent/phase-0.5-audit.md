# Фаза 0.5: независимый аудит реализации (lane E, verifier)

Дата аудита: **2026-10-31, ~17:50–18:00 MSK**.
База: HEAD `c7710a3` («Фазы 1–2: свободный runtime и источники v4 за флагом», PR #91) +
**незакоммиченные** изменения других lane в общем worktree (lane A — v4 CONNECT/SYNC/REPAIR
dispatch; lane B — ContainerExecutor; lane C — sources v4; Lead — Caddy/0.5.5 и pgvector).
Источник правды — код в рабочем дереве; там, где состояние HEAD и worktree расходятся, это
указано явно. Аудит **ничего не менял** в продукте: только чтение файлов, `git show HEAD:…`,
read-only ssh на prod.

Что требовалось по [09-prod-login-incidents.md](09-prod-login-incidents.md) и плану
[05](05-implementation-plan.md) 0.5.4–0.5.7; что реально есть в коде; чем это доказывается.

| Пункт | HEAD `c7710a3` | Текущий worktree (на момент аудита) | Главное доказательство |
|---|---|---|---|
| 0.5.4 CONNECT: фазы login/research/sync + остановка + честный статус | **partial**: остановка и честный статус есть (v3), именованных фаз нет | **done-with-caveats**: v4 dispatch + PhaseReporter (не закоммичено), остановка прежняя | `jobs/router.py:61`, `jobs/runner.py:503-504`, `web/.../timeline.ts:115`, `agent/v4/job_runtime.py:52-125`, `agent/v4/dispatch.py:94-144`, `worker.py` (diff) |
| 0.5.5 Caddy access-лог, таймауты, алерты | **missing** | **done** (deploy в prod выполнен, файлы в worktree) | `ops/deploy/caddy-botai.conf`, `ops/deploy/caddy-access-report.sh`, `docs/plan/deployment.md` §Caddy; live-проверка на `mnema` (ниже) |
| 0.5.6 Network-first чтение источников | **partial**: механика v4 sources есть (API/JSON reads), но в toolset v4-runtime не подключена | **partial+**: factory подключает `source_*`/`rag_*` (worktree), live-проверки на ЦУ нет | `sources/v4/runner.py:80-90,376`, `tests/sources/v4/test_runner.py:26,41,58,357`, `agent/v4/factory.py` (diff) |
| 0.5.7 Секрет из чата | **done для сценария инцидента**, ужесточение не всеобъемлющее | то же | `agent/conversations.py:269,405-423`, `agent/router.py:88`, `tests/agent/test_secret_guard.py`, `tests/jobs/test_conversations.py:831,833` |

## 0.5.4. Явные фазы CONNECT и остановка

**Что есть.**

- **Остановка** (была и до v4, работает на любом job): `POST /api/v1/jobs/{job_id}/cancel`
  (`backend/src/botai/jobs/router.py:61-63` → `answers.cancel_job`), воркер отменяет asyncio-задачу
  (`jobs/runner.py:704-708`), обработчик получает `JobCancelled`; терминальное состояние
  `cancelled` с честной строкой «Остановлено» (`jobs/runner.py:503-504`). В UI кнопка «Продолжить/
  Остановить» идёт через `cancelTask` (`apps/web/src/features/chat/timeline-view.tsx:120-122`,
  `agent-blocks/task-block.tsx:41,55`), текст после остановки: «Остановлено. Уже найденные данные
  сохранены.» (`features/chat/timeline.ts:115`). Это **done**.
- **Фазы login → research → sync.** В HEAD именованных фаз нет: есть только свободный текст
  `job.stage` (`core/jobs.py:65` `status(stage)`, `jobs/events.py:40`, UI `job-state.ts:35,138`).
  Честная видимость «что идёт» в v3 была: `learn/explorer.py:1059` «Вхожу в кабинет»,
  `:1434` «Смотрю страницы кабинета», `browser/login.py:596,1161,1222,1295,1317`,
  `sources/runner.py:548,634`. Но это не фазы контракта: `TaskState` в
  `contracts/agent_ui.py:186` — только `queued|running|waiting_user|done|failed|cancelled`,
  поля `phase` в состоянии/событиях/чате нет.
- **В worktree (не закоммичено, lane A)** добавлен v4-путь с явными фазами:
  `agent/v4/job_runtime.py:52-77` (`TOOL_PHASES`: secret_*/browser_* → login, source_* → research,
  source_sync/rag_ingest → sync; `PHASE_ORDER`; подписи «вход/исследование/синхронизация»),
  `:88-125` `PhaseReporter` пишет phase в `ctx.status` (job.stage + событие `status`),
  forward-only; CONNECT/REPAIR используют `phase_prefix` (`agent/v4/dispatch.py:94-144`),
  а `worker.py` (diff) при `BOTAI_AGENT_ENGINE=v4` регистрирует `make_v4_handlers()` поверх v3
  таблицы (v3-мосты для календаря и старых соединений — `dispatch.py:875-899`). Это отвечает
  требованию «видно, идёт вход или исследование», и `source_no_progress` (см. ниже) делает
  22-минутный лоудер после входа невозможным на v3-пути; на v4 — мягкие лимиты D-009.

**Чего не хватает для «done» в продуктовом смысле.**

1. Явная фаза — это **строка статуса**, а не поле контракта: клиент не может отрисовать
   фазовый прогресс/ETA программно; `job.stage` свободный текст (≤120 символов). Если нужен
   UI-прогресс, нужен `phase` в `contracts/agent_ui` (отдельный PR, ломает клиенты — правило 9).
2. Код фаз — **worktree-only** (untracked `job_runtime.py`, `dispatch.py`; diff `worker.py`):
   на HEAD приёмки фаза v4 CONNECT ещё не существует. Приёмка должна фиксировать SHA, где это
   закоммичено, и прогнать CONNECT на fakeuni/L3.
3. Кнопка «Остановить» в UI останавливает job; отдельной остановки «только research» нет —
   для v4 это нормально (лимиты/стоп задачи).

**Что докажет prod-готовность:** закоммиченный SHA с `dispatch`+`phase`, L1-тест «CONNECT на
fakeuni: stage меняется login→research→sync и не откатывается», L2/L3 CONNECT-прогон с
остановкой на фазе research (job → cancelled, stage «Остановлено…», данные не теряются), и
ручная проверка UI, что фаза видна в карточке задачи.

## 0.5.5. Caddy access-лог, таймауты, алерты

**HEAD:** отсутствует (в репозитории нет ни access-лога, ни runbook; инцидент 09 §4 оставался
недоказуемым).

**Worktree (Lead, сегодня):**

- `ops/deploy/caddy-botai.conf` — справочный блок `botai.mnema.app`: `log { output file
  /var/log/caddy/botai-access.log { roll_size 20MiB; roll_keep 10; roll_keep_for 720h } format
  filter { request>uri regexp \?.* ?REDACTED; request>remote_ip ip_mask 24 48;
  request>client_ip ip_mask 24 48; request>headers>Cookie delete; …Authorization delete;
  …X-Api-Key delete; …X-Forwarded-For delete; resp_headers>Set-Cookie delete } }`;
  `reverse_proxy 127.0.0.1:18090 { flush_interval -1; transport http { dial_timeout 2s;
  response_header_timeout 30s } }`.
- `ops/deploy/caddy-access-report.sh` — сводка по логу: 499/aborted, 5xx, 403 на WS, медленные
  ответы, WS; exit code 2 при превышении порогов (для cron/journal), 66 — нет лога.
- `docs/plan/deployment.md` §«Caddy: access-лог, таймауты, наблюдаемость (0.5.5)» — runbook:
  бэкап, `caddy adapt`, `caddy validate` **от пользователя caddy** (validate от root создаёт
  root-файл лога и reload падает), `systemctl reload` (не restart), откат.

**Проверено мной на production (read-only, 2026-10-31 ~17:50 MSK):**

```
$ ssh mnema 'sudo -n grep -nE "botai-access|dial_timeout|response_header_timeout|ip_mask|log \\{" /etc/caddy/Caddyfile'
93: log {
94:   output file /var/log/caddy/botai-access.log {
100:  request>uri regexp \?.* ?REDACTED
101:  request>remote_ip ip_mask 24 48
102:  request>client_ip ip_mask 24 48
114:  dial_timeout 2s
115:  response_header_timeout 30s
```

```
$ ssh mnema 'sudo -n bash /usr/local/sbin/caddy-access-report.sh --all'
BotAI Caddy access report · /var/log/caddy/botai-access.log
window: all lines · entries 5 · websockets 0
2026-10-31: total=5 2xx=5 3xx=0 4xx=0 (403=0 ws403=0 499=0) 5xx=0 aborted=0 ws=0 slow>=10s=0 health_ok=5
result: ok
```

Поля живой строки (JSON, проверено `tail -1`): `ts, logger, msg, level, request{uri,
remote_ip, headers{Accept,User-Agent}, …}, status, size, duration, resp_headers, user_id`;
`uri` записан как `/api/health?REDACTED`, `remote_ip` — `… .0` (/24), Cookie/Authorization/
Set-Cookie в заголовках отсутствуют. **Цель 0.5.5 достигнута: 499/504/WS-403 считаются, а не
гадаются.**

**Честные ограничения:** (а) «алерт» здесь — exit code 2 скрипта + запись в journal (отдельного
канала уведомлений в проекте нет), доставку уведомлений владельцу надо настроить отдельно;
(б) WS close-code в access-лог не попадает (только `101` и длительность) — причина закрытия
WS остаётся в приложенческой диагностике (`BOTAI_DIAGNOSTICS`), это записано в runbook;
(в) объём выборки мал (5 записей сразу после reload) — распределение 499/504 появится по мере
трафика; (г) файлы `caddy-*` пока **не закоммичены** — приёмка должна зафиксировать их в SHA
вместе с runbook.

## 0.5.6. Network-first чтение источников

**Что есть (HEAD, закоммичено):** v4-источники умеют читать данные данными, а не только DOM:
`backend/src/botai/sources/v4/runner.py:80-90` — `ReadSpec` с `format: json|html|text`,
`read_live` (`:376`) вызывает URL сессией источника (`httpx`, cookie/token подставляет
`SourceRunner`, в ответ — `format="json"` при JSON content-type); L1-тесты на fakeuni читают
именно API (`tests/sources/v4/test_runner.py:26,41,58` — `/api/v2/timetable`,
`/api/v2/tasks/deadlines`; `:357` — «live read uses the source session»); инструмент
`source_read_live` есть в `agent/v4/tools/sources.py` (HEAD).
**Чего нет на HEAD:** `build_agent_wiring` HEAD **не подключает** `source_*`/`rag_*` к v4-run
(в HEAD `factory.py` собирает basic+browser+code+secrets; подключение источников — diff
worktree lane A/C, где `build_source_toolkit`/`build_rag_toolset` появляются и требуют
`services.db`). То есть на HEAD фича есть в сервисе и тестах, но модель v4 ею ещё не пользуется;
в worktree — уже подключена.
**Чего нет нигде как доказательства:** критерий плана «ЦУ читает задания через
`GET /api/micro-lms/tasks/student` (174 записи)» доказан только детерминированным
`bench/live` ([04-benchmark.md](04-benchmark.md) §8.1, 09 §6), **не** v4-агентом; L3 по ЦУ в
моём scope не было (HSE only).
**Что докажет:** L1 `fakeuni` тест «playbook читает API без DOM» уже есть — надо добавить L2/L3
прогон v4-агента, где он сам доходит до API расписания/заданий и сохраняет записи, плюс
сверка counts с эталонным `bench/live` на той же сессии.

## 0.5.7. Секрет из чата

**Что есть (HEAD, закоммичено):** детектор `looks_like_secret` (`agent/conversations.py:269-290`:
токены/cookie-строки через `scrub_secrets`, коды из цифр, «пароль: …», короткие password-like
строки), проверка **до сохранения** сообщения: `conversations.py:405-423` — если задача ждёт
защищённый ввод (`_waiting_secret`) или её ответ, секретоподобный текст отклоняется с
`409 secret_in_chat` («Пароль и код вводятся только в защищённое поле, не в чат»);
сообщение в `ChatMessage` не попадает вообще (`agent/router.py:88` → `conversations.post_message`).
Клиент обрабатывает 409: `apps/web/src/features/chat/outbox.ts:83-103` (`conflictKind`),
сообщение стирается из очереди/хранилища и не повторяется (`outbox.test.ts:270`,
`chat-tasks.test.tsx:106`). Тесты: `tests/agent/test_secret_guard.py` (классификатор),
`tests/jobs/test_conversations.py:831,833` (409 через API). Это закрывает именно сценарий
инцидента 09 §2.4 (пароль, отправленный в чат при открытом вводе).
**Честные ограничения:** (а) детект срабатывает, когда задача ждёт защищённый ввод; сообщение
«мой пароль hunter2» в обычном чате без ожидающего job не отклоняется (это шире плана, но
бьёт по UX); (б) уже сохранённые ранее сообщения не вычищаются задним числом — в инциденте
сообщение 07:48:48 осталось в `chat_messages`; скрипта миграции/удаления нет; (в) код ошибки
`secret_in_chat`, а план называл `secret_detected_in_chat` — несовпадение имени, не поведения.
**Что докажет prod:** прогон L1/API-теста уже есть; для полной готовности — решение владельца,
нужен ли глобальный детект (и/или разовая чистка истории инцидента).

## Production-readiness: включаем `BOTAI_AGENT_ENGINE=v4`

Раздел **переаудирован в ~18:00 MSK** после доработок Lead: CI-сборка sandbox-образа,
pull/revision/остатки в `remote-deploy.sh`, compose-обвязка (socket/group_add/тома для api+worker),
хостовые каталоги, `BOTAI_AGENT_ENGINE=v3` по умолчанию, pgvector-образ и порядок
«postgres → migrate → сервисы», проверенная точка восстановления. Ниже — фактическое состояние.

**Готово в репозитории/worktree (проверено по файлам):**

| Что | Где (файл:строка) | Статус |
|---|---|---|
| ContainerExecutor: контейнер на вызов, uid 10001, read-only rootfs, cap-drop ALL, no-new-privileges, cgroups, timeout→`docker kill`, bind-mount workspace, env-allowlist | `agent/v4/executor/container.py` (весь модуль), `ops/docker/sandbox.Dockerfile` | готово в worktree (не закоммичено на момент аудита) |
| Выбор исполнителя: prod → только контейнер, `local` в prod — громкий отказ | `agent/v4/factory.py` `build_executor` | готово в worktree |
| Workspace/артефакты из настроек (`BOTAI_AGENT_WORKSPACE_ROOT`, `BOTAI_SOURCE_ARTIFACTS_DIR`), иначе temp | `agent/v4/factory.py:181-210,330-340`, `settings.py:140,133` | готово в worktree; без этого bind-mount песочницы в prod был бы нерабочим |
| Task-injection fix (задача в диалоге, resume-safe) | `agent/v4/runtime.py:84-95,318-325` | готово в worktree; **подтверждено живым прогоном** (моя приёмка #4) |
| CI собирает и пушит sandbox-образ тем же тегом + `latest` + label `org.opencontainers.image.revision` | `.github/workflows/deploy.yml:32,103-115` | готово в worktree |
| Deploy: pull sandbox-образа, сверка revision label, зашифрованный `pg_dump` до DDL, «postgres → pg_isready → migrate → сервисы», удаление остатков `label=botai.agent.sandbox=1`, retention app+sandbox образов | `ops/deploy/remote-deploy.sh:127-172` | готово в worktree |
| compose.prod: `BOTAI_AGENT_ENGINE=${BOTAI_AGENT_ENGINE:-v3}`, `BOTAI_AGENT_EXECUTOR=container`, workspace/artifacts root, `BOTAI_SANDBOX_IMAGE` из `IMAGE_TAG`, hard stop $2, auto_continue=false | `ops/compose.prod.yaml:43-53` | готово в worktree |
| api+worker: `/var/run/docker.sock`, `group_add: DOCKER_GID`, оба хостовых каталога по одинаковому абсолютному пути | `ops/compose.prod.yaml:113-116,142-145`; `ops/deploy/bootstrap-server.sh` (DOCKER_GID, каталоги 0700 uid 10001) | готово в worktree |
| pgvector-образ Postgres во всех контурах; порядок деплоя «postgres-first-then-migrate»; collation-процедура | `ops/compose.prod.yaml:63-68`, `remote-deploy.sh:152-166`, `docs/plan/deployment.md` §pgvector | готово в worktree |
| Точка восстановления проверена: `pg_dump -Fc` восстановлен в temp DB, совпали `users=4, connections=7, chat_messages=287, schedule_events=169`, `alembic_version=a8d4f1c2b6e9`; temp DB удалена | `docs/plan/deployment.md` §pgvector | заявлено Lead (read-only проверка на сервере возможна, я её не повторял) |
| Runbook включения/проверок/откат v4 | `docs/plan/deployment.md` §v4-агент в production | готово в worktree |

**Состояние production на момент аудита (read-only ssh, 2026-10-31 ~18:00 MSK):**

```
worker image:  ghcr.io/mattoyuzuru/botai-app:c7710a3f918d   (= HEAD SHA, задеплоен)
IMAGE_TAG:     c7710a3f918d
postgres:      postgres:17-alpine                            (pgvector ещё НЕ выкачен)
alembic head:  a8d4f1c2b6e9                                  (= последняя закоммиченная миграция)
Settings в worker: нет поля agent_executor                    (образ = HEAD, worktree-изменения не выкачены)
```

Вывод: production сейчас — ровно HEAD `c7710a3` с движком **v3**; вся v4-обвязка (sandbox,
pgvector, флаг) существует в репозитории/worktree, но **не выкачена**. До выкладки следующего
SHA включать флаг нельзя (в образе ещё нет container executor'а и настроек workspace).

**Чек-лист перед включением `BOTAI_AGENT_ENGINE=v4` (порядок):**

1. Закоммитить worktree-работу lane A/B/C + Lead (dispatch/phases, container, source/rag wiring,
   runtime fix, compose/CI/docs) и получить зелёный CI на SHA.
2. Обычный деплой **с `BOTAI_AGENT_ENGINE=v3`**: поднимутся pgvector-образ, миграции
   (`…_chunk_embeddings_pgvector`, `…_domain_source_id`, `…_actions_policy_journal`), app+sandbox
   образы. Проверить: collation-предупреждения Postgres (`datcollversion`/логи), `alembic head`,
   `docker image inspect ...botai-sandbox:<tag>` → revision label = SHA, `docker version` в
   worker проходит. Откат: флаг не включали, данные не менялись (кроме аддитивных миграций).
3. **Задать soft-лимиты** в `/srv/botai/compose.vars`:
   `BOTAI_AGENT_LIMITS_STEPS/SECONDS/TOKENS/USD` (сейчас в compose только
   `BOTAI_AGENT_HARD_STOP_USD=2.0` и `AUTO_CONTINUE=false`; дефолты `None` = без предела, D-009).
   Это решение владельца об эксплуатационных предохранителях, не архитектурная стена.
4. RAG: `BOTAI_RAG_ENABLED` по умолчанию **off**; если нужен — включить и задать
   `BOTAI_RAG_PROVIDER`/`BOTAI_RAG_ARTIFACT_ROOT` (миграция pgvector уже едет).
5. Включить `BOTAI_AGENT_ENGINE=v4` в `compose.vars` → перезапуск api+worker → проверки из
   runbook: `Settings().agent_engine`, `docker version` в worker, образ песочницы, smoke `AGENT`
   job (несколько тысяч токенов): `done`, строки в `agent_runs`/`agent_run_events`, `code_exec`
   реально в контейнере (`docker ps -a --filter label=botai.agent.sandbox=1` пусто), файл
   появился в `/srv/botai/agent-workspaces/<run>` на хосте.
6. Живая проверка: владелец делает один CONNECT/вход через UI; смотреть `job.stage`
   (фазы), трассу, стоимость, отсутствие секретов в логах. После — один L3-прогон моей
   приёмки на живом вузе (скрипт/квоты уже готовы).
7. Откат: вернуть `BOTAI_AGENT_ENGINE=v3`, перезапустить api/worker; незавершённые v4-jobs
   завершатся понятной ошибкой (не тихим переходом на v3); `agent_runs` остаются; каталоги
   workspace/artifacts не удалять.

**Остаточные риски/долги, которые надо помнить:**

- Docker-сокет в api/worker = root-эквивалент на хосте (принято владельцем, записано в runbook);
  sandbox-контейнер сокета и секретов не получает.
- API тоже исполняет inline v4 source sync и может поднимать Chromium — архитектурный долг
  (задокументирован `16-sources-runner-tools.md`), не блокер включения, но нагрузка/память.
- Docker-socket-доступ у api расширяет поверхность: любой RCE в api = root на хосте; pet-проект
  и принятый риск, но стоит зафиксировать в ретро.
- Sandbox-образ собирается по каждому SHA: без доступа CI к ghcr push деплой упадёт на
  `docker pull` — проверить права пакета перед первым деплоем.
- Soft-лимиты и RAG-флаги — единственные «пустые» пункты env, которые я нашёл; всё остальное
  для v4 в compose/workflow присутствует.

## Tool-UX рекомендации (не блокеры, по итогам живых прогонов)

1. **`code_exec` и путаница `command` vs `code`.** В трёх живых прогонах модель (deepseek-flash,
   thinking high) многократно присылала `{"language":"bash","command":"…"}` вместо обязательного
   `code`; получала `invalid_arguments` («Additional properties are not allowed ('command')») и
   повторяла ошибку 4–10 раз за прогон, сжигая шаги. Продукт прав по схеме, но текст ошибки
   ничего не подсказывает модели. Рекомендация: добавить в `ToolResult` hint вида «используйте
   поле `code` (строка с кодом), поле `command` не поддерживается» и/или принять `command` как
   алиас `code` в `_CodeExecArgs` (обратная совместимость для модели, не для API). Оценка
   экономии: 5–15% шагов на типовой задаче.
2. **Код ошибки таймаута браузерных действий.** `browser/control.py:1676` отдаёт
   `SOURCE_UNAVAILABLE` («Нажать: страница не ответила») — в живых прогонах это 4–20% вызовов
   (клик по лендингу HSE, ожидание network_idle после SSO). Модель читает `source_unavailable`
   как проблему источника/БД и меняет стратегию. Рекомендация: отдельный код
   `browser_timeout`/`action_timeout` (ErrorCode — аддитивно) или хотя бы hint «страница не
   ответила на действие; подождите/повторите».
3. **`ask_user` для учётных данных.** Модель иногда просит логин обычным текстом (продуктовая
   политика D-008 это допускает), а в live-прогонах это приводило к выдуманному username и
   отказу входа. Для продукта это не баг; но если хочется устойчивости — в системном промпте
   стоит явно сказать, что логин/пароль предпочтительно запрашивать через `secret_request`
   (hint в описании `ask_user` для `input_kind=secret`).

4. **`code_exec` не маскируется SecretBroker'ом (hardening gap, наблюдено).** В финальном
   живом прогоне профильная почта владельца (поле `email` из `get-user` API ЛК) попала в контекст
   модели через `stdout` `code_exec`: `browser_*`-наблюдения маскируются `broker.mask`, а
   `code_exec` — нет. Пароль не утёк (механизма передачи секретов в `code_exec` нет: `secret_use`
   возвращает только url/title). По D-008 профильные/сессионные данные агенту видимы, так что это
   не нарушение решения, но: (а) bench-политика «mail только в Keychain» ловит такой прогон
   security-gate'ом; (б) если в фазе 1.4 секреты начнут передаваться в `code_exec` (env/файлы),
   нужен mask stdout/stderr/созданных файлов, иначе первичный секрет сможет утечь. Рекомендация:
   либо не передавать первичные секреты в `code_exec` вообще, либо маскировать его вывод тем же
   `broker.mask` (как browser_*).

## Метод и границы аудита

- Читал код на HEAD (`git show HEAD:…`) и в общем worktree; worktree менялся другими lane
  во время аудита — строки/статусы зафиксированы на 2026-10-31 ~17:50–18:00 MSK.
- Prod проверял только read-only (`ssh mnema`, `sudo -n` чтение Caddyfile и запуск
  `caddy-access-report.sh`); ничего не менял.
- Не запускал полный `make check` и чужие тесты; ссылки на тесты — по их наличию в дереве,
  прогон тестов — задача соответствующих lane.
- Живой L3 и L2 v4-harness — в [README.md](README.md) этого каталога.
