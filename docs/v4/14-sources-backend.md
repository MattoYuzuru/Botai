# 14. Бэкенд источников v4: схема и сервис

Статус: **реализовано аддитивно, 2026-10-31** (фаза 2.1–2.3 плана). Пакет
`backend/src/botai/sources/v4/` хранит источники, playbook'и, артефакты, состояние чтения и
приоритеты; v3 (`connections`, `adapter_specs`, `documents`) не тронут и продолжает работать.
Модель — [03-sources-and-ux.md](03-sources-and-ux.md) §1–2, данные — [02-architecture.md](02-architecture.md)
§9, план — [05-implementation-plan.md](05-implementation-plan.md) §2.1–2.3. Runner, REST,
агентские инструменты, запись в домен, сессии/браузерные playbook'и и RAG реализованы
отдельно: [16](16-sources-runner-tools.md), [18](18-rag.md), [19](19-actions.md).

## 1. Что добавлено

| Слой | Файлы | Что делает |
|---|---|---|
| Модели | `backend/src/botai/db/models.py` (блок «sources v4» в конце) | 6 таблиц, константы словаря |
| Миграция | `backend/src/botai/db/migrations/versions/20261031_e5a1c9b7d342_sources_v4.py` | upgrade + downgrade, `down_revision=d9f3a1c7e582` |
| Идентичность | `sources/v4/identity.py` | нормализация адреса, `link:`/`file:`/`mail:`/`draft:` ключи |
| Словарь | `sources/v4/kinds.py` | природа/данные, статусы, доверие, языки playbook, артефакты |
| Источники | `sources/v4/service.py`, `store.py`, `schemas.py` | create/list/get/update/disable/enable/set_status, `SourcePatch` |
| Playbook'и | `sources/v4/playbooks.py` | create, версии, активация, активная версия, список |
| Артефакты | `sources/v4/artifacts.py` | add/list артефактов с content address |
| Состояние | `sources/v4/state.py` | upsert/get `source_state` (cursor, site map, endpoints) |
| Приоритеты | `sources/v4/priorities.py` | порядок источников на «вуз × вид», первый живой — основной |
| Тесты | `backend/tests/sources/v4/`, `backend/tests/db/test_sources_v4_migration.py` | 56 зелёных, включая сверку схемы и upgrade/downgrade |

`__init__.py` пакета экспортирует публичные функции — импортировать надо оттуда:
`from botai.sources.v4 import create_source, add_playbook_version, ...`.

## 2. Таблицы

Миграция `20261031_e5a1c9b7d342`, ревизия `e5a1c9b7d342`, `down_revision = d9f3a1c7e582`
(прежний head). Всё новыми таблицами рядом; данные v3 не переносятся (D-016 разрешает
пересоздание, перенос — отдельная задача). Downgrade удаляет ровно новые таблицы.

### `sources` — живая запись источника

`id`, `user_id` (для tenant-предиката), `space_id` → `spaces` (`CASCADE`), `university_id` →
`universities` (`SET NULL`, удобный join), `title` (человеческое имя, не адрес), `entry_url`
(страница интерфейса без секретных query), `identity_key` (см. §3), `kinds` JSONB-массив,
`status` (см. §4), `playbook_id` → `playbooks` (`SET NULL`), `config` JSONB, `auth_ref`
(имя секрета в Vault, не значение), `priority` int, `trust`, `last_sync_at`, `last_error` JSONB,
`disabled_from` (состояние до выключения), `created_at`, `updated_at`.

Ограничения и индексы:

- `uq_sources_space_identity_key` — **частичный уникальный** индекс `(space_id, identity_key)`
  `WHERE identity_key IS NOT NULL`: один источник на нормализованную идентичность внутри вуза,
  не на origin. Два кабинета, две таблицы, два календаря на одном хосте — разные строки.
- `ix_sources_space_id_status`, `ix_sources_playbook_id`, `ix_sources_user_id`.
- `CHECK status IN (...)` и `CHECK trust IN (...)` — словарь продублирован в БД, сервис валидирует
  те же значения (тест на невалидный статус ловит расхождение).

### `playbooks` и `playbook_versions` — знание «как читать»

`playbooks`: `id`, `university_id` (`SET NULL`), `owner_id` (`SET NULL`; `NULL` = общий по
D-018), `title`, `language` (`declarative|python|js`), `current_version` (номер активной; 0 — нет),
`health` JSONB, `provenance` JSONB, `created_at`.

`playbook_versions`: `id`, `playbook_id` (`CASCADE`), `version`, `spec` JSONB, `code` TEXT,
`tests` JSONB, `canary` JSONB, `active` bool, `checksum` (sha256 от spec+code+tests), `created_by_run`
→ `jobs` (`SET NULL`), `created_at`. Ограничения: `UNIQUE(playbook_id, version)`, частичный
уникальный `uq_playbook_versions_active` по `playbook_id WHERE active` (ровно одна активная версия),
`CHECK spec IS NOT NULL OR code IS NOT NULL`. Старые версии остаются для отката: откат — это
активация прежней версии, ничего не удаляется.

### `source_artifacts` — прочитанное

`id`, `user_id`, `source_id` (`CASCADE`), `kind` (`page|json|file|har|screenshot|log|doc|other`),
`url`, `sha256` (content address), `content_type`, `size`, `storage_ref` (путь/ключ вне БД),
`metadata` JSONB, `captured_at`, `run_id` → `jobs` (`SET NULL`), `step`. Индексы:
`(source_id, captured_at)`, `sha256`, `user_id`. Байты в БД не пишутся.

### `source_state` — маленькое изменяемое состояние

`source_id` PK/FK (`CASCADE`), `last_revision`, `cursor` JSONB, `site_map` JSONB, `endpoints` JSONB,
`updated_at`. Одна строка на источник; upsert обновляет только переданные поля.

### `source_priorities` — порядок для overlay

PK `(space_id, kind)` → `spaces` (`CASCADE`), `source_ids` JSONB (упорядоченный список),
`updated_at`. Первый живой (`active|syncing|stale`) источник порядка — основной для своего вида;
это настройка, а не архитектурный потолок (03 §4.1). Список валидируется сервисом: все
источники должны принадлежать тому же пространству и пользователю, дубликаты отбрасываются.

## 3. Идентичность

`identity_key` — то, чем один источник отличается от соседа (03 §1.4):

| Что | Ключ | Пример |
|---|---|---|
| сайт/кабинет/LMS/API | нормализованный адрес: `https`, хост в нижнем регистре (IDN → punycode), порт по умолчанию убран, путь без хвостового `/`, фрагмент убран, секретные query удалены, остальные отсортированы | `https://lk.example.org/schedule?group=2101&view=week` |
| календарь по личной ссылке | `link:<sha256(нормализованной ссылки)>` — ссылка в БД не попадает | `link:9f2c…` |
| файл | `file:<sha256(содержимого)>` | `file:c0ff…` |
| почта | `mail:<ящик в нижнем регистре>` | `mail:student@example.org` |
| черновик без адреса | `draft:<id источника>` — заменяется при `update_source` | `draft:01J…` |

Секретными считаются имена query вида token/key/secret/password/signature/session/csrf/ticket/
code/state и значения, похожие на токен (≥24 символов). Поэтому одна и та же страница с разными
сессионными параметрами — один источник, а токен никогда не влияет на идентичность и не попадает
в `identity_key`/`entry_url`. Логин:пароль в адресе отвергается: их место в Vault, не в ссылке.
`webcal://`/`webcals://` нормализуются в `https`.

Ограничения: длина ключа ≤ 400 (колонка `String(400)`; слишком длинный адрес честно отвергается,
а не обрезается), ключ с префиксом из таблицы выше либо `http(s)://`.

## 4. Словарь

- **Природа:** `site`, `portal`, `lms`, `api`, `calendar`, `sheet`, `doc`, `file`, `mail`, `feed`,
  `other`.
- **Данные:** `schedule`, `deadlines`, `assignments`, `courses`, `grades`, `people`, `locations`,
  `news`, `documents`, `messages`.
- **Статусы:** `draft`, `probing`, `learning`, `waiting_user`, `active`, `syncing`, `stale`,
  `needs_reauth`, `needs_link`, `error`, `disabled`, `archived` (03 §6.2).
- **Доверие:** `high`, `normal`, `low`. **Языки playbook:** `declarative`, `python`, `js`.
- **Артефакты:** `page`, `json`, `file`, `har`, `screenshot`, `log`, `doc`, `other`.
- `kinds_from_legacy()` переводит v3 `connections.kind` в v4 (`document` → `file` + `documents`,
  `scheduleEvents` → `schedule`) — пригодится при переносе данных.

## 5. API сервиса

Все функции принимают `AsyncSession` первым аргументом и `user_id` (или `user_id`+`space_id`) в
качестве обязательного предиката. **Транзакцию держит вызывающий:** функции делают `flush`, но не
`commit`; API/runner коммитит сам. Чужой id — `BotaiError(NOT_FOUND)`, не `forbidden`: один студент
не узнаёт о существовании чужого источника. Ошибки — `botai.core.errors.BotaiError`
(`VALIDATION`, `NOT_FOUND`, `CONFLICT`).

```python
from botai.sources.v4 import (
    create_source, list_sources, get_source, update_source,
    disable_source, enable_source, set_status, SourcePatch,
    create_playbook, add_playbook_version, activate_playbook_version,
    active_playbook_version, list_playbook_versions, get_playbook, list_playbooks,
    add_artifact, list_artifacts, get_source_state, upsert_source_state,
    set_priority, get_priority, primary_source,
    identity,  # site_key/calendar_key/file_key/mail_key/normalize_url
)
```

**Источники.** `create_source(session, *, user_id, space_id, title, entry_url=None,
identity_key=None, link=None, file_sha256=None, mailbox=None, kinds=(), status="draft",
playbook_id=None, config=None, auth_ref=None, priority=0, trust="normal") -> Source` — ровно один
способ идентичности; дубль в пространстве → `CONFLICT` с названием первого источника; без адреса
создаётся с `draft:<id>`; `playbook_id` проверяется на видимость. `list_sources(..., space_id=None,
status=None, kind=None)` — старые первыми, фильтр по виду через JSONB `@>`. `get_source`.
`update_source(..., patch: SourcePatch)` — меняет только поля, реально переданные в `SourcePatch`
(`model_fields_set`): `entry_url` пересчитывает идентичность, если `identity` не задана явно;
`status="disabled"` запоминает `disabled_from`, включение возвращает прежнее состояние.
`disable_source`/`enable_source`/`set_status` (для runner'а; `last_error` пишется при ошибке).

**Playbook'и.** `create_playbook(..., title, university_id=None, language="declarative",
shared=False, provenance=None)` — `shared=True` даёт `owner_id=NULL` (виден обоим, D-018).
`add_playbook_version(..., playbook_id, spec=None, code=None, tests=(), canary=None,
created_by_run=None)` — блокирует строку playbook на время подсчёта версии, требует spec или code,
считает `checksum`; `created_by_run` проверяется на владельца. `activate_playbook_version(...,
version)` — переключает ровно одну активную версию и `current_version`, прошлая остаётся.
`active_playbook_version`, `list_playbook_versions` (новые первыми), `get_playbook`,
`list_playbooks(university_id=None)`.

**Артефакты.** `add_artifact(..., source_id, kind, sha256, url=None, content_type=None, size=None,
storage_ref=None, metadata=None, captured_at=None, run_id=None, step=None)` — проверяет владельца
источника и задания. `list_artifacts(..., source_id, kind=None, limit=100)` — новые первыми.

**Состояние.** `get_source_state(...)` возвращает `None`, пока ничего не прочитано;
`upsert_source_state(..., last_revision=None, cursor=None, site_map=None, endpoints=None)` —
переданные поля заменяются целиком (`{}`/`[]` очищают), отсутствующие не трогаются.

**Приоритеты.** `set_priority(..., space_id, kind, source_ids)` — порядок, дубликаты убираются,
чужие/из другого пространства источники → `NOT_FOUND`; `get_priority` — сохранённый список;
`primary_source` — первый живой источник порядка (draft/disabled/error не основной).

## 6. Тесты и проверки

```bash
cd backend
uv run --frozen pytest tests/sources/v4 tests/db/test_schema.py tests/db/test_sources_v4_migration.py -q
# 56 passed
uv run --frozen ruff check src/botai/sources/v4 src/botai/db/models.py \
  src/botai/db/migrations/versions/20261031_e5a1c9b7d342_sources_v4.py tests/sources/v4 \
  tests/db/test_sources_v4_migration.py
uv run --frozen ruff format --check <те же файлы>
uv run --frozen pyright src/botai/sources/v4 src/botai/db/models.py   # 0 errors
```

Покрыто: нормализация идентичности (пути/порты/IDN/секретные query/fragment, `link:`/`file:`/
`mail:`/`draft:`), два кабинета и два календаря одного вуза, дубль адреса → `CONFLICT`, скоуп
двух пользователей и двух пространств, `SourcePatch`/`disable`/`enable`, валидация словаря,
видимость и версии playbook'ов, общий/личный playbook, checksum, `created_by_run`, артефакты и
их привязка к источнику/заданию, состояние, приоритеты и основной источник, миграция
upgrade→downgrade→upgrade, CHECK/уникальные индексы и каскады, сверка моделей и миграций
(`tests/db/test_schema.py`). Сеть и модель не используются.

## 7. Чего здесь ещё нет (следующая задача)

- **Runner** (`sources/v4/runner.py`): `PROBE`/`SYNC`/`REPAIR`/`CANARY`, выбор активной версии
  playbook, исполнение spec/кода в песочнице (`agent/v4/executor`), браузер/сессии, запись
  артефактов/состояния и нормализация в домен. Сейчас сервис только хранит.
- **Агентские инструменты** `source_create/source_probe/source_sync/source_repair/
  source_update_playbook/source_disable/source_list/...` — обёртки над этим API.
- **REST** `/api/v1/sources*` и OpenAPI/сгенерированные типы; `SourcePatch` уже готов как вход.
- **RAG**: индексация `source_artifacts` в `chunks` (task-19); поле `kind`/`sha256`/`storage_ref`
  для этого уже есть.
- **Миграция v3-данных**: `connections` → `sources` (+`kinds_from_legacy`), `adapter_specs` →
  `playbook_versions(format=recipe)`, `documents` → артефакты; отдельная задача, сейчас таблицы
  пустые и никем не читаются.
- **UI** «Учёбы» (task-18) и фича-флаг: пакет аддитивен и не вызывается из v3; включать нечего —
  wiring появится в task-16/17.
- **Политика переходов статусов** (какие переходы разрешены runner'у) — сейчас сервис принимает
  любой статус из словаря; ограничения появятся вместе с runner'ом.
- **Ретенция артефактов** и хранилище `storage_ref` (файловая система/объектное хранилище) — вне
  этой задачи; в БД только метаданные.

## 8. Открытые вопросы

1. **`university_id` в `sources`** денормализован ради фильтров; при переносе данных его надо
   заполнять из `space.university_id` (сейчас `create_source` делает это сам).
2. **Общие playbook'и:** `shared=True` разрешает запись обоим аккаунтам; если понадобится
   «только чтение для второго», нужен отдельный флаг/роль — сейчас этого нет.
3. **Чистка `source_priorities`** при архивации/удалении источника: JSONB-список не чистится
   автоматически, `primary_source` просто пропускает несуществующие id; при переносе на
   реляционные строки можно добавить FK.
4. **Один источник = один playbook?** Схема допускает `playbook_id` у источника и общий playbook
   у нескольких источников; правило «какой источник какую версию берёт» — за runner'ом.
5. **Формат `spec`** пока не зафиксирован схемой: `spec JSONB` принимает любой JSON; контракт
   `playbook.schema.json` появится в task-20/17 и начнёт валидировать содержимое.
