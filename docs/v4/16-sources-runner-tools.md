# 16. Sources v4: runner, агентские инструменты и REST/SSE

Статус: **реализовано 2026-10-31** (фаза 2.2–2.3 и добивка 31.10). Поверх схемы и сервиса
[14-sources-backend.md](14-sources-backend.md) добавлены playbook-runner, восемь инструментов
`source_*` для агентского runtime и REST/SSE `/api/v1/sources`; затем — **запись в домен с
`source_id`**, **Vault-сессии и браузерные playbook'и**, **network-first чтение** и каталог
артефактов. v3 не тронут: старые `sources/*.py`, `agent/tools*.py` и `connections` работают
как раньше. Модель — [03-sources-and-ux.md](03-sources-and-ux.md) §2–4, план —
[05-implementation-plan.md](05-implementation-plan.md) §2.2–2.3.

## 1. Что добавлено

| Слой | Файлы | Что делает |
|---|---|---|
| Runner | `backend/src/botai/sources/v4/runner.py` | probe/sync/repair/canary: читает endpoint'ы, парсит declarative-поля или исполняет код парсера в executor'е, сохраняет артефакты/состояние/статус, пишет домен при sync, отдаёт `RunReport` |
| Запись в домен | `backend/src/botai/sources/v4/domain.py` | upsert по `(source_id, external_id)` в 8 доменных таблиц с провенансом; v3-строки не трогаются |
| Браузер | `backend/src/botai/sources/v4/browser.py` | `format=browser` + `browser_plan` (goto/click/type/press/select/wait/read/eval/download), session из Vault/профиля |
| Сессии | `backend/src/botai/sources/v4/secrets.py` | `VaultSessionReader` (`session:`/`profile:` → Playwright storage state); bearer — `VaultSecretReader` |
| Wiring | `backend/src/botai/sources/v4/wiring.py` | production-сборка runner'а: Vault, артефакты, контейнерный executor в prod (LocalExecutor запрещён), Chromium |
| Инструменты | `backend/src/botai/agent/v4/tools/sources.py` | `SourceToolkit` и восемь `ToolHandler` (`source_*`) под протокол `agent/v4/protocol.py` |
| REST/SSE | `backend/src/botai/sources/v4/router.py` | `/api/v1/sources*`, playbook/версии, artifacts, state, priorities, SSE прогресса sync |
| Схемы API | `backend/src/botai/sources/v4/schemas.py` | входы/выходы REST + `RunReportOut` (camelCase, `domain`, `discoveredEndpoints`) |
| Хранилище артефактов | `backend/src/botai/sources/v4/artifacts.py` | `ArtifactStore`, `ArtifactBlob`, `FileArtifactStore` (content-addressed каталог, retention) |
| Книга прогонов | `service.py` (`mark_synced`, `mark_error`), `playbooks.py` (`record_canary`, `set_playbook_health`, `get_playbook_version`) | свежесть/ошибки источника, canary-результат и health playbook'а |
| Регистрация | `backend/src/botai/main.py` | `sources.v4.router` включён в приложение всегда (2 строки, согласовано с Lead) |
| Тесты | `backend/tests/sources/v4/*`, `tests/db/test_domain_source_id_migration.py` | L1 fakeuni: раннер, API, инструменты, домен, браузер, сессии, retention |

Включается одним env: **`BOTAI_AGENT_ENGINE=v4`** (`settings.agent_engine`). Роутер
зарегистрирован всегда (стабильный OpenAPI), но на v3 каждый его endpoint отвечает
`404 not_found` с `detail`: «Источники v4 выключены: включите BOTAI_AGENT_ENGINE=v4». Новых env нет.

## 2. Runner

`SourceRunner(session, *, user_id, transport=None, artifact_store=None, executor_factory=None,
clock=None, cookies=None, headers=None, secret_reader=None, workspace_root=None)` — один
runner на сессию/пользователя; методы `probe`, `sync`, `canary`, `repair`, `read_live`.
Сессия и её cookie/токены остаются внутри; в отчёт не попадают ни заголовки, ни cookie.
`secret_reader` — хук секрет-брокера: если у источника есть `auth_ref`, runner получает секрет и
добавляет `Authorization: Bearer` (значение не логируется и не возвращается).

### 2.1. Что исполняется

- **Declarative spec** (`version=1`): список `reads`; каждый read — `url`, `method`, `format`
  (`json|html`), `items_path` (JSON pointer) или `items_selector` (CSS), `fields` (JSON-указатели
  или HTML-селекторы с `attr/pattern/transform`), `kind` (вид данных), `open_url`, `min_items`.
  Поля приводятся к домену существующим `sources/normalize.coerce`, поэтому даты, состояния,
  формат занятий и обязательные поля — та же логика, что у v3.
- **Код парсера** (`playbook_versions.code`, язык `python`/`js` из `playbooks.language`): read'ы
  сначала скачиваются и кладутся в workspace (`reads/<name>.json|html|txt`), затем код исполняется
  в `agent/v4/executor` (`LocalExecutor` по умолчанию; интерфейс `Executor` — точка замены на
  контейнер). Контракт вывода: последняя JSON-строка stdout —
  `{"records":[{"kind","external_id","fields","source_url","open_url"}], "documents":[...],
  "cursor": {...}, "notes": "..."}`. Никакого `eval` в API/worker.
- **Browser-формат** пока не исполняется: у playbook-версии с `format=browser` раннер вернёт
  структурную ошибку, пока wiring не подключит `browser/control` (см. §6).

Пример declarative spec:

```jsonc
{
  "version": 1,
  "timeout_s": 30,
  "reads": [{
    "name": "timetable",
    "kind": "schedule",
    "url": "https://fakeuni.test/api/v2/timetable",
    "format": "json",
    "items_path": "/items",
    "fields": {
      "externalId": "/uid", "title": "/subject/name",
      "startsAt": "/slot/begin", "endsAt": "/slot/end",
      "location": "/place/room", "kind": "/type"
    }
  }, {
    "name": "deadlines", "kind": "deadlines",
    "url": "https://fakeuni.test/api/v2/tasks/deadlines", "format": "json", "items_path": "",
    "fields": {
      "externalId": "/taskId", "title": "/name",
      "dueAt": {"pointer": "/due", "transform": "unixSeconds"}
    }
  }]
}
```

### 2.2. Что сохраняется

- **Артефакты**: тело каждого read'а кладётся в `ArtifactStore` (content address `sha256`,
  `storage_ref`), строка `source_artifacts` пишется с санитизированным URL (секретные query
  выброшены), `content_type`, `size`, `metadata={"read","status","format"}`. При `persist=False`
  (probe по умолчанию) артефакты не пишутся.
- **Состояние**: `source_state.last_revision` = номер версии (или `None` для inline-кандидата),
  `endpoints` = `[{name, method, url}]` без секретов, `cursor` — из кода парсера.
- **Статус источника**: `sync` при успехе → `active`, `last_sync_at=now`, `last_error=None`; при
  401/403 → `needs_reauth`; иначе → `error` с `last_error={code, detail, at}`. `repair` меняет
  только артефакты/состояние: активная версия ещё старая, статус не выдаётся за исправленный.
  Отключённый/архивный источник раннер не включает.
- **Canary**: прогон версии (по умолчанию — самая новая) без смены статуса; результат
  (`ok`, `counts`, `errors`, `checked_at`) пишется в `playbook_versions.canary`, health playbook'а
  обновляется. Активация версии (`playbooks.activate_playbook_version`) требует
  `canary.ok is True`; обход — только явный `require_canary=False` (владелец/откат).

### 2.3. Отчёт

`RunReport` (в API — `RunReportOut`, camelCase): `source_id`, `run`
(`probe|sync|repair|canary`), `version`, `ok`, `source_status`, `started_at`/`finished_at`,
`reads` (status, seen/valid/invalid, artifact_id, error), `counts` по видам, `records` (до 200,
`records_truncated`), `errors` (`code`, `detail`, `read`, `retryable`), `artifact_ids`, `cursor`,
`notes`, `persisted`. Коды ошибок: `source_auth_required`, `source_unavailable`,
`source_rate_limited`, `read_failed`, `recipe_rejected`, `recipe_drift`, `playbook_required`,
`executor_failed`.

Домен пока не пишется: у v4-источника нет `connection_id`, а таблицы `courses/schedule_events/...`
требуют его. Отчёт возвращает нормализованные записи, артефакты и состояние; запись в домен — 
следующая миграция (`source_id` в доменных таблицах, нормализатор поверх `source_artifacts`).
Это осознанная граница задачи, а не «данные не найдены».

## 3. Агентские инструменты

`SourceToolkit(sm, *, transport=None, artifact_store=None, executor_factory=None, clock=None,
cookies=None, headers=None, secret_reader=None)`; `toolkit.tools()` → `list[ToolHandler]`,
`build_source_tools(toolkit)`. Инструменты берут `user_id` из `ToolContext`, открывают сессию из
`sm`, коммитят свои изменения и возвращают `ToolResult` с данными (без секретов).

| Инструмент | Политика | Аргументы (главные) | Что делает |
|---|---|---|---|
| `source_list` | read, idempotent | `space_id?`, `status?`, `kind?` | список источников и статусов |
| `source_create` | write_soft | `space_id`, `title`, `url/link/file_sha256/mailbox?`, `kinds?`, `playbook_id?` | создать источник; дубль → конфликт |
| `source_probe` | read | `source_id`, `spec?`, `code?`, `language?`, `version?`, `persist?` | проверить кандидата или версию, ничего не сохраняя (кроме `persist=true`) |
| `source_sync` | write_soft | `source_id`, `version?` | исполнить playbook, сохранить артефакты/состояние/статус |
| `source_repair` | write_soft | `source_id`, `spec?/code?/version?`, `activate?` | canary кандидата; `activate=true` активирует только успешный |
| `source_update_playbook` | write_soft | `source_id`, `spec?/code?`, `tests?`, `canary?`, `activate?` | новая версия; при `activate` сначала canary, активация только при успехе; playbook создаётся, если его нет |
| `source_disable` | write_soft | `source_id` | обратимо выключить источник |
| `source_read_live` | read | `source_id`, `url`, `method?`, `max_chars?`, `save_artifact?` | прочитать страницу/API сессией источника, текст + артефакт |

JSON-схемы строгие (`object_schema`, `additionalProperties:false`), проверяются runtime до вызова.
Неуспех инструмента — `ToolResult.failure(code, detail, data=...)`: модель видит код и отчёт, а не
исключение. `source_update_playbook`/`source_repair` при провале canary возвращают
`canary_failed`, уже созданную неактивную версию кладут в `data` для разбора.

## 4. REST и SSE

Роутер `botai.sources.v4.router`, prefix `/api/v1`. Все endpoint'ы закрыты зависимостью
`require_sources_v4` (флаг движка). Тесты подменяют `get_runner_builder` — функция
`(session, user_id) -> SourceRunner`, поэтому сеть и хранилище артефактов инжектируются без
патчей.

| Метод и путь | Назначение |
|---|---|
| `GET /sources` | список; фильтры `space_id`, `status`, `kind` |
| `POST /sources` | создать (`SourceCreateIn`); 409 на дубль идентичности |
| `GET /sources/{id}` · `PATCH /sources/{id}` | карточка; `SourcePatch` — только переданные поля |
| `POST /sources/{id}/disable` · `/enable` | обратимое выключение/включение |
| `POST /sources/{id}/probe` | проба кандидата/версии (`RunReportOut`) |
| `POST /sources/{id}/sync` | синхронизация; пишет статус/артефакты/состояние |
| `POST /sources/{id}/canary` | canary версии (по умолчанию — новой); пишет `canary`/health |
| `POST /sources/{id}/repair` | canary кандидата; `activate=true` — активировать успешный |
| `GET /sources/{id}/sync/stream` | SSE: `status`, `read`, `done`, `error` |
| `POST /sources/{id}/playbook` | создать и привязать playbook |
| `GET /sources/{id}/playbook` | playbook и `currentVersion` |
| `GET/POST /sources/{id}/playbook/versions` | версии; POST — новая (опционально `activate`, требует canary) |
| `POST /sources/{id}/playbook/versions/{v}/activate` | активация; `require_canary=true` по умолчанию |
| `GET /sources/{id}/artifacts` | артефакты, фильтр `kind`, `limit` |
| `GET /sources/{id}/state` | `lastRevision`, `cursor`, `siteMap`, `endpoints` |
| `GET/PUT /spaces/{space_id}/priorities/{kind}` | порядок источников для overlay |

SSE использует `sse_starlette.EventSourceResponse`: события `status` (старт прогона), `read`
(каждый endpoint с санитизированным URL), `done` (полный отчёт в camelCase) или `error`
(`code`, `detail`). Стрим открывает собственную сессию из `services.db` и отменяет задачу при
отключении клиента.

## 5. Тесты

```bash
cd backend
uv run --frozen pytest tests/sources/v4/test_runner.py tests/sources/v4/test_api.py \
  tests/sources/v4/test_tools.py tests/sources/v4/test_identity.py tests/sources/v4/test_sources.py \
  tests/sources/v4/test_playbooks.py tests/sources/v4/test_artifacts_state.py \
  tests/sources/v4/test_priorities.py -q
# 7 + 5 + 4 + остальные = зелёные
uv run --frozen ruff check src/botai/sources/v4 src/botai/agent/v4/tools/sources.py src/botai/main.py
uv run --frozen ruff format --check <те же файлы>
uv run --frozen pyright src/botai/sources/v4 src/botai/agent/v4/tools/sources.py src/botai/main.py
```

Покрыто (L1 fakeuni, ASGI-transport без сети): declarative-поля и код-парсер в executor'е;
два источника одного хоста синхронизируются и живут одновременно; артефакты в
content-addressed хранилище и `source_state`; canary пишет `canary`/health; drift (endpoint
переехал → 404) честно попадает в `error`, кандидат-ремонт проходит canary и активируется;
`read_live` сессией источника; ошибка сессии → `needs_reauth`; REST: флаг v3 → 404, CRUD, скоуп
двух пользователей, playbook/версии/canary/активация/sync/artifacts/state, SSE (`event: read`,
`event: done`), priorities; инструменты: полный агентский флоу create→probe→update_playbook→
sync→read_live→disable, repair с активацией после canary, чужие id → `not_found`, cookie сессии
не попадает в результаты. Плюс unit-тесты схемы: узкое правило секретных query (длинный
несекретный course id сохраняется в identity), параллельная вставка того же identity ловится
savepoint'ом и отвечает 409 с названием первого источника, активация версии требует
`canary.ok=true` (`require_canary=False` — только явный обход владельца). Реальной сети и модели нет.

Известное внешнее красное: `tests/db/test_schema.py::test_models_match_migrations` сейчас падает
из-за параллельной незавершённой работы (модели `agent_runs`/`agent_run_events` и их миграция в
другой задаче), к таблицам sources v4 отношения не имеет — свои 73 теста `tests/sources/v4` +
`tests/db` (кроме этой сверки) зелёные на момент отчёта.

## 6. Как включать и что осталось

**Включение.** `BOTAI_AGENT_ENGINE=v4` (роутер уже в приложении; при v3 — 404 с понятным
текстом). Инструменты подключает `build_agent_wiring` при `agent_engine=v4`:
`build_source_tools(SourceToolkit(...))` + `rag_*` (при `BOTAI_RAG_ENABLED`). Production-сборка
runner'а — `sources/v4/wiring.py` (Vault, `FileArtifactStore(BOTAI_SOURCE_ARTIFACTS_DIR)`,
контейнерный executor, Chromium); `source_executor_factory` держит workspace под
`BOTAI_AGENT_WORKSPACE_ROOT`.

**Сделано в этой итерации:** запись в домен с `source_id`/провенансом и идемпотентным
re-sync; Vault-сессии (bearer и storage state) и `secretHandle` в браузерной форме;
browser-формат playbook; контентные артефакты с retention; network-first (наблюдённые
XHR/fetch JSON парсятся без повторного запроса, endpoints попадают в state/report);
prod-guard (в `ENV=prod` без контейнерного executor — `executor_unavailable`, код не
исполняется на хосте worker'а); закрытие любого executor'а.

**Осталось (по приоритету):**

1. **Чтение v4-данных продуктовыми экранами.** Строки домена с `source_id` уже пишутся, но
   расписание/задания/оценки/новости читают v3-подключения; нужна склейка/overlay v4-источников
   (`source_priorities`, провенанс, отключённые источники) — следующий backend-шаг, вместе с
   удалением «Мои материалы» из модели.
2. **REST-sync в API остаётся inline.** Браузерный sync из REST поднимает Chromium в контейнере
   API (у него увеличены tmpfs/shm/mem); правильнее ставить `SYNC` job в worker — техдолг
   (сейчас API получает docker-сокет/ресурсы под это).
3. **RAG-автоиндексация после run** не подключена: `rag_ingest`/`python -m botai.ops.reindex_rag`
   — поддерживаемый путь; хук в runner — задел в `build_indexer`/`reindex_source_artifacts`.
4. **Retention и связь с RAG.** Ссылочный набор retention покрывает `source_artifacts` и
   `source_state`; когда RAG начнёт ссылаться на артефакт из чанка, эту ссылку нужно добавить в
   `referenced_storage_refs`.
5. **`upload` в browser playbook** структурно отклоняется (нет источника файла для загрузки);
   остальные действия формы покрыты.
6. **Планировщик.** `sync`/`canary` из UI — синхронные/SSE; фоновые расписания, автоповтор и
   health-watch — фаза 3.3 (не входила).
7. **SSE:** `Last-Event-ID`/повтор после реконнекта для долгих sync не реализован (поток
   одноразовый, отчёт дублируется в `done`).

## 7. Открытые вопросы

1. **Активация без canary** (`require_canary=False`) есть для откатов/ручных правок; нужен ли
   явный класс действия/политика для «активировать без canary» — открыто.
2. **SSE-повторы** для долгих sync (см. §6.7).
3. **Формат spec:** `PlaybookSpecV1` раннера и `contracts/playbook.schema.json` сведены;
   менять версию формата — только через контракт.
