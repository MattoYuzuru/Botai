# Runtime wiring: экспериментальный путь v4 за флагом (фаза 1, task-16)

Статус: **сделано (фаза 1, экспериментальный путь), 2026-10-31.** Связывает готовые части
фазы 1: runtime (task-12), полный браузер (task-13), executor (task-14) — в один job-путь,
который включается `BOTAI_AGENT_ENGINE=v4` и не трогает v3. Основание:
[02-architecture.md](02-architecture.md) §3–§4, [05-implementation-plan.md](05-implementation-plan.md)
фаза 1, [D-001](06-decisions.md), [D-003](06-decisions.md), [D-008](06-decisions.md),
[D-009](06-decisions.md), [D-014](06-decisions.md).

Компоненты: `backend/src/botai/agent/v4/factory.py` (сборка), `handlers.py` (job `AGENT`),
`tools/browser.py`, `tools/code.py`, `tools/secrets.py`, `trace_store.py`; таблицы
`agent_runs`/`agent_run_events` и миграция; тесты `tests/agent/v4/`, `tests/jobs/test_agent_v4.py`,
`tests/db/test_agent_traces_migration.py`.

## 1. Что собрано

Один вызов `build_agent_wiring(services, user_id, run_id, job_id, workspace_root, provider,
trace_store, with_browser, control, exec_limits, http_transport)` возвращает `AgentWiring`:

| Часть | Откуда | Примечание |
|---|---|---|
| `runtime` | `AgentRuntime` (task-12) | цикл «провайдер → tools → ответ», трассы и лимиты |
| `provider` | `providers.factory.build_provider(settings)` или инъекция | `mock` в тестах, DeepSeek/Anthropic/OpenAI по настройкам |
| `executor` | `build_executor(settings, workspace)` | `LocalExecutor` только вне `ENV=prod` (§3) |
| `workspace` | `workspace_for(root, run_id)` | каталог задачи (`<root>/<run_id>`), файлы общие для `code_exec`/`fs_*` |
| `control` | `launch_browser_control(services, workspace, mask)` | Chromium на run; `upload_root=workspace`, `redact_body=broker.mask` |
| `broker` | `SecretBroker(vault, user_id, scope_id)` | handle'ы первичных секретов и маскирование (§4) |
| `trace_store` | `DbTraceStore(services.db, user_id, job_id)` | таблицы `agent_runs`/`agent_run_events` (§5) |
| `tools` | `build_basic_tools + browser_* + code_exec + secret_*` | единый `ToolRegistry`, runtime не менялся |

Набор инструментов run'а (стабильные имена, `ToolRegistry` сортирует схемы):

- базовые: `http_request`, `fs_read`, `fs_write`, `fs_list`, `ask_user`, `trace_read`;
- браузер (17): `browser_open`, `browser_navigate`, `browser_click`, `browser_type`,
  `browser_press`, `browser_select`, `browser_wait`, `browser_read`, `browser_eval`,
  `browser_screenshot`, `browser_cookies`, `browser_storage`, `browser_network`,
  `browser_tabs`, `browser_download`, `browser_upload`, `browser_session`;
- код: `code_exec` (python/javascript/bash через executor; `javascript` → `js` на стыке);
- секреты: `secret_request`, `secret_use`.

`with_browser=False` собирает run без Chromium (дев-тесты, дешёвые задачи): браузерных
инструментов в наборе нет, остальное работает.

## 2. Job `AGENT` и запись результата

`backend/src/botai/agent/v4/handlers.py` регистрирует `JobKind.AGENT` в worker
(`botai/worker.py`; v3-таблица `agent.handlers.HANDLERS` не тронута). Handler:

1. проверяет флаг: при `agent_engine != "v4"` падает `validation` с сообщением
   «включите `BOTAI_AGENT_ENGINE=v4`» — v3 остаётся путём по умолчанию;
2. собирает wiring, запускает `runtime.run(RunTask(...))` с системным промптом §6 и
   ask-каналом job'а (`InputBlock`/`ChoiceBlock`, секретные — через `ask_secret`);
3. пишет ответ блоком `TextBlock` в тред/job-события и возвращает job-результат:

```json
{"status": "answer", "run_id": "01…", "content": "…", "steps": 5, "tool_calls": 4,
 "total_tokens": 1234, "cost_usd": 0.0012, "limit_reached": null, "workspace": "/…/run_id"}
```

- `answer`/`paused`/`*_limit` — job `done` с этим результатом (run можно продолжить по
  `result.messages`, чекпоинт пишется в трассу);
- `error` — job `failed` с кодом runtime'а (`llm_unavailable`, `internal_error`, …);
- конфигурационный отказ (например, prod guard) — job `failed` с `not_implemented` и
  понятным текстом, без тихого запуска кода.

Внутренние payload-ключи (только тесты/дев, с `_`): `_workspace_root` — корень
workspace; `_browser` — запускать ли Chromium (по умолчанию да). Лимиты из payload не
принимаются: они берутся из настроек.

**Программный вход** (пока без HTTP-роута, чтобы не менять OpenAPI/клиенты в этой итерации):

```python
from botai.agent.v4.factory import enqueue_agent_run

job_id = await enqueue_agent_run(
    services, user_id=owner_id, task="Открой кабинет ВШЭ, найди расписание и сохрани его"
)
```

`enqueue_agent_run` кладёт job `AGENT` в обычную очередь (`PgJobQueue`, dedupe=False), так что
статус, события и SSE-карточка задачи работают существующими средствами. HTTP-эндпоинт
`POST /api/v1/agent/runs` (и UI-кнопка) — отдельная задача, когда эксперимент перестанет быть
экспериментом; контракты не менялись.

## 3. Флаг и запрет local-исполнителя в prod

- `BOTAI_AGENT_ENGINE=v3` (по умолчанию) — всё как раньше; job `AGENT`, если его всё же
  поставить, честно падает.
- `BOTAI_AGENT_ENGINE=v4` — экспериментальный путь доступен. `build_executor` —
  единственная точка выбора исполнителя: при `ENV=prod` он бросает `AgentWiringError`
  («LocalExecutor запрещён в ENV=prod…»), и handler превращает это в ошибку job'а.
  Причина: local-код не изолирован от ФС/процессов worker'а, где лежат `DATABASE_URL`,
  `BOTAI_KEK`, ключи провайдеров и Vault (см. [13-executor.md](13-executor.md) §2).
  Production-v4 пойдёт через `ContainerExecutor` (пока заглушка); пока prod-v4 не включён.
- Мягкие лимиты `agent_limits_*` (steps/seconds/tokens/usd, hard stop, auto_continue)
  маппятся в `RunLimits` функцией `run_limits_from_settings` — без стен, `None` = без предела
  ([10-limits-and-research.md](10-limits-and-research.md), D-009).

## 4. Секреты и маскирование

- `secret_request` задаёт владельцу вопрос через `ask_user` (`input_kind="secret"`), кладёт
  значение в Vault (`kind=transient`, TTL 30 мин, scope = `<job_id>:<handle>`) и возвращает
  модели **только handle** (`sec_<ulid>`).
- `secret_use` подставляет значение внутри инструмента и никогда его не возвращает:
  - `kind=http`: заголовок (`format`, например `Bearer {secret}`), JSON-поле или form-поле;
    запрос идёт через единый `agent/v4/http.build_client`; ответ возвращается с заменой
    значения на `***`, `set-cookie` скрывается;
  - `kind=browser_field`: `BrowserControl.type(target, value)`; в результат идёт только
    url/title страницы.
- Значения handle'ов живут в памяти `SecretBroker` этого run'а: после рестарта/resume run
  спросит секрет заново (Vault-запись при этом уже есть; персистентный реестр handle'ов —
  следующая итерация фазы 1.4).
- Маскирование: `broker.mask` передаётся в `browser_*` инструменты (текст страницы, JS-результаты,
  тела сети/HAR) и в `ControlConfig.redact_body` (тела до записи в лог/HAR). Дополнительно
  пассивные `browser_network`/HAR скрывают credential-заголовки (`authorization`, `cookie`,
  `set-cookie`, `x-api-key`, …) — значения cookie при этом доступны через явный
  `browser_cookies`/`browser_session` (D-008: сессия — рабочий материал).

## 5. Трассы в БД

Миграция `20261031_a8d4f1c2b6e9_agent_traces.py` (down_revision — текущий head RAG
`b7e2c4a91d3f`) добавляет две аддитивные таблицы; v3-данные не трогаются:

- `agent_runs` — одна строка на run: `run_id` (ULID, PK), `user_id` (FK users cascade),
  `job_id` (FK jobs **on delete set null**: очередь чистит старые job'ы, трасса остаётся),
  `task`, `provider`, `model`, `status`, `started_at`/`finished_at`, `steps`, `total_tokens`,
  `cost_usd`, `event_count`, `updated_at`/`created_at`;
- `agent_run_events` — шаги: `id` bigserial, `run_id` (FK cascade), `seq`, `kind`, `step`,
  `duration_ms`, `at`, `payload` JSONB; `unique(run_id, seq)` — повторный append после сбоя
  идемпотентен (`on conflict do nothing`).

`DbTraceStore` (`agent/v4/trace_store.py`) реализует тот же протокол `TraceStore`, что
`MemoryTraceStore`: `append`, `read(after_seq, limit)`, `get_run`, `list_runs` (только свои
run'ы пользователя). Runtime и `trace_read` не знают, какой store им дали. Payload'ы
ограничены (runtime режет контент/результаты до 4k), уже замаскированы инструментами и не
содержат окружения/ключей; проверяется тестом на отсутствие `BOTAI_KEK`/`DATABASE_URL` в
событиях.

Ограничение: автоматической очистки `agent_runs` по TTL пока нет (таблицы растут медленно,
один run — десятки строк); политика ретенции — вместе с API/UI чтения трасс. Удаление
пользователя чистит трассы каскадом.

## 6. Системный промпт

`handlers.SYSTEM_PROMPT` (русский) описывает: свободный harness и доступные инструменты;
страница — данные, не команды; первичные секреты — только `secret_request`/`secret_use`;
cookie/сессия — рабочий материал; результат — в файл workspace; не выдумывать факты; ответ
по-русски со ссылкой на источник. Промпт не содержит секретов и не меняет v3-промпты.

## 7. Как проверять

```bash
cd backend
uv run --frozen pytest tests/agent/v4 -q                 # включая Chromium-прогоны
uv run --frozen pytest tests/jobs/test_agent_v4.py -q    # БД: AGENT-job и trace store
uv run --frozen pytest tests/db/test_agent_traces_migration.py -q
uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen pyright
```

Ключевые сценарии:

| Что | Тест |
|---|---|
| soft-лимиты и prod guard | `tests/agent/v4/test_agent_wiring.py` |
| `code_exec`/`fs_*` в одном workspace | `tests/agent/v4/test_agent_executor_secrets.py` |
| браузерные инструменты, файлы в workspace, маскирование сети/HAR, сессия | `tests/agent/v4/test_agent_browser_tools.py` |
| полный run: кабинет → расписание → файл → ответ, трасса; soft step limit | `tests/agent/v4/test_agent_run_e2e.py` |
| `AGENT` job через worker, `agent_runs`/`agent_run_events`, отказ при v3 | `tests/jobs/test_agent_v4.py` |
| миграция upgrade/downgrade/upgrade | `tests/db/test_agent_traces_migration.py` |

Живой L2/L3 через этот путь пока не прогонялся (нужны ключ модели и вуз): эксперимент
доказан на mock-провайдере + настоящем Chromium/executor, как требует план фазы 1; живой
benchmark — следующая приёмка.

## 8. Ограничения и что дальше

- **Нет HTTP-эндпоинта и UI** для постановки runs: вход программный
  (`enqueue_agent_run`), чтобы не менять OpenAPI и клиентов внутри фазы 1.
- **Нет сохранения `result.messages`** в БД для resume после рестарта: runtime возвращает
  чекпоинт, трасса в БД есть, но job-путь его не переиспользует; resume после паузы/лимита —
  следующая итерация (нужен `run_state` в `jobs.payload` или отдельной таблице).
- **`ask_user` блокирует job** (worker ждёт ответа через существующий `JobContext.ask`);
  парковка в `waiting_user` без удержания слота — отдельная задача (§4.5 архитектуры).
- **Секреты**: реестр handle'ов в памяти run'а; `env`/`code`-подстановка секретов в executor
  не сделана (только HTTP и браузерное поле) — фаза 1.4.
- **Стриминга нет**: ответ модели приходит целиком; SSE шага — в трассе.
- **Prod**: local-executor запрещён, container-executor — заглушка; `ENV=prod` +
  `agent_engine=v4` честно падает до реализации контейнера.
- **Ретенция** `agent_runs`/`agent_run_events` и API чтения трасс владельцем — вместе с UI.
- v3-путь не менялся: флаг по умолчанию `v3`, старые job kinds/обработчики и тесты работают
  как раньше.

## 9. Ссылки

- [02-architecture.md](02-architecture.md) §3–§4 (ядро, инструменты, трассы, лимиты), §6
  (исполнение кода).
- [05-implementation-plan.md](05-implementation-plan.md) фаза 1 (1.1–1.5).
- [06-decisions.md](06-decisions.md) D-001, D-003, D-008, D-009, D-014.
- [10-limits-and-research.md](10-limits-and-research.md) — мягкие лимиты и остановка research.
- [11-runtime.md](11-runtime.md) — runtime, провайдеры и протокол инструментов.
- [12-browser-control.md](12-browser-control.md) — `BrowserControl` и профили.
- [13-executor.md](13-executor.md) — executor, границы local-режима, план контейнера.
