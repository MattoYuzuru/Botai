# Контракты

**Статус: v3-контракты — legacy, v4-контракты добавлены 2026-10-31.** Контракты v3
описывают прежнюю архитектуру (два контура, рецепт, read-only, allowlist) и живут в коде,
пока идёт миграция. Новые контракты v4 (`tools`, `run`, `playbook`) описывают свободный
harness: инструменты, трассы задач и playbook'и источников. Решения, отменяющие прежние
контракты, — [docs/v4/06-decisions.md](../docs/v4/06-decisions.md), смысл и обоснование —
[docs/v4/02-architecture.md](../docs/v4/02-architecture.md) (§4, §5, §9).

| Файл | Статус | Потребители |
|---|---|---|
| `tools.schema.json` | **v4** | `agent/v4/**` (tool layer, реестр инструментов), API трасс, UI работы агента |
| `run.schema.json` | **v4** | `agent/v4/runtime.py` + `traces.py`, worker/очередь, API `/runs` и `/traces`, UI работы агента |
| `playbook.schema.json` | **v4** | `sources/v4/**`, playbook runner, карточка/редактор источника в UI, canary и health |
| `agent-ui.schema.json` | v3 → v4 | ChatAgent, PWA; блоки плана/действия/трассы/стоимости дополняются в v4 |
| `conversation.md` | v3 | API, очередь, Explorer, ChatAgent, PWA |
| `domain.schema.json` | v3 | Normalizer, API, PWA |
| `recipe.schema.json` | v3 legacy | Explorer, RecipeStore, Runner (в v4 — частный случай playbook `format=recipe`) |
| `login-preset.schema.json` | v3 legacy | BrowserSvc, Explorer |
| `adapter-spec.schema.json` | v3 legacy | Explorer, RecipeStore, Runner (заменяется playbook'ом) |
| `recipe-v2.md` | v3 legacy | Explorer, Runner, lint (описание рецепта v2) |
| `account-reset.md` | v3 | API, PWA (сброс данных аккаунта) |
| `calendar-link-vectors.json` | v3 | Сервер и веб-клиент (случаи для личной ссылки календаря) |
| `events.md` | v3 | API, PWA, BrowserSvc |

REST описывает не файл в этом каталоге, а `backend/openapi.json`: его создаёт FastAPI, `./scripts/gen.sh` регенерирует.

## v4: контракты, примеры и сверка с реализацией

Схемы сверены с кодом 2026-10-31: `botai.agent.v4.protocol`, `agent/v4/traces.py`,
`agent/v4/runtime.py`, `sources/v4/playbooks.py`, `db/models.py` (словари).

- `contracts/tools.schema.json` — `ToolSpec` (имя, описание, JSON Schema аргументов,
  `timeout_s`, класс политики `read|write_soft|write_destructive|money|auth`,
  `idempotent`, `max_result_chars`, `serial_only`) и `ToolResult` (`ok`, `summary`,
  `data`, `artifacts`, `truncated`, `next`, `error`, `cost.ms`). Совпадает с
  `agent/v4/protocol.py`. Значения первичных секретов не попадают ни в спеку, ни в
  результат.
- `contracts/run.schema.json` — фактический runtime-контракт: `RunTask` (вход цикла),
  `RunLimits` (мягкие лимиты + `hard_stop_usd`), `RunResult` (статус, токены,
  стоимость, чекпоинт `messages`, `trace`), `TraceEvent`/`RunTrace`/`RunInfo`
  (`agent/v4/traces.py`, `runtime.py`), а также плановая запись `run` для таблицы
  `runs` (kind/state/input/policy, 02-architecture §9). `ToolCall`/`Artifact`/`Usage` —
  конвенции содержимого `TraceEvent.payload`. Первичные секреты не сохраняются;
  cookie/сессия — по решению задачи.
- `contracts/playbook.schema.json` — ревизия playbook'а: `language`
  `declarative|python|js` (словарь `PLAYBOOK_LANGUAGES`), `spec` и/или `code`
  (минимум одно — как в `sources/v4/playbooks.py`), `tests`, `canary`, `health`,
  `provenance`, `version`, `active`, `checksum`. `reads.dataKind` — `SOURCE_DATA_KINDS`;
  `artifacts.kind` — `ARTIFACT_KINDS`; виды источника — `SOURCE_NATURE_KINDS`.
  `origin`/`auth`/`schedule`/`entrypoints`/`reads` остаются знанием внутри playbook
  (docs 03 §2); в таблицах `playbooks`/`playbook_versions` их колонок пока нет —
  они живут в `spec`/`code` и конфиге источника.
- Примеры: `contracts/examples/{valid,invalid}/{tools.contract,run.trace,playbook}.json`
  (массивы экземпляров, формат как у остальных контрактов); проверены `jsonschema`.
  Подключены в `backend/tests/core/test_contracts.py` (`SCHEMA_CASES`,
  `test_v4_valid_examples_pass_schema`, `test_v4_invalid_examples_fail_schema`) и
  гоняются в CI; `test_every_contract_schema_is_registered` требует, чтобы новая схема
  была добавлена в `botai.contracts.schemas.SCHEMA_FILES`.
- TypeScript-типы для v4-схем пока не генерируются: `json2ts` настроен только на
  `agent-ui.schema.json`. Добавление целей `gen:tools`/`gen:run`/`gen:playbook` —
  отдельная задача вместе с потребителями в `apps/web`.

### Расхождения, зафиксированные при сверке

1. `ToolArtifact.kind` в `protocol.py` — свободная строка; схема и БД рекомендуют
   `ARTIFACT_KINDS`. Валидацию вида стоит добавить в протокол/сервис (follow-up).
2. `run` (kind/state/input/policy, таблица `runs`) ещё не реализован: runtime
   возвращает `RunTask`/`RunResult`/`RunTrace`, DB-адаптер и API `/runs` — фаза 2.
   Контракт описывает оба уровня, примеры валидируют оба.
3. `sources/v4/playbooks.py` не связывает `language` с содержимым (declarative без
   spec допустим, если есть code) — схема повторяет это правило (`anyOf`), а не
   навязывает конвенцию.
4. `PlaybookVersion.code` в БД — `TEXT`; ветка artifact-ссылки в схеме оставлена
   вперёд-совместимой (код в content-addressed store — цель §6.4), но пока не
   используется.
5. `origin`, `auth`, `schedule`, `entrypoints`, `reads` пока не имеют колонок в
   `playbooks` — это контракт знания; runner должен читать их из `spec`/`code` и
   `sources.config` (follow-up для `sources/runner`).
6. **Свежий playbook и ревизия — разные документы.** Строка `playbooks` имеет
   `current_version=0` и не имеет `active`; `playbook_versions` — `version≥1`,
   `active`, `canary`. `playbook.schema.json` сейчас описывает документ ревизии
   (`version≥1`, `active`) и не принимает playbook без ревизий. Нужен отдельный
   `$defs/playbookIdentity` (`id/title/language/currentVersion/health/provenance`)
   или поле `currentVersion` — до появления `GET /playbooks` (follow-up task-20/17).

## Правила

1. Изменение контракта — отдельный PR с перечислением, кого оно ломает, и с
   записью в журнале изменений `docs/v4/02-architecture.md` §12.4.
2. Типы генерируются из схем, а не пишутся руками:
   `openapi-typescript` для REST, `json-schema-to-typescript` для схем.
   Сгенерированные файлы коммитятся и проверяются в CI на расхождение.
   Для v4-схем генерация TS ещё не подключена (см. выше).
3. Каждая схема сопровождается валидными и невалидными фикстурами в
   `contracts/examples/valid/` и `contracts/examples/invalid/`; CI проверяет обе стороны.
   Для v4-схем фикстуры добавлены и подключены к тесту (`SCHEMA_CASES`).
4. Никакое поле ответа API не содержит секрет, cookie, пароль или токен.
   Это проверяется тестом, а не соглашением.
