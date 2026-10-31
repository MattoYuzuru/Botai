# 18. RAG: эмбеддинги, гибридный поиск, индекс артефактов источников

Статус: **фаза 2.5 реализована, pgvector включён (2026-10-31).** Добавлено аддитивно рядом
с v3-FTS: новые таблица и миграции, модули `search/embeddings.py`, `search/pgvector.py`,
`search/hybrid.py`, `search/rag.py`, инструменты `rag_search`/`rag_ingest` в
`agent/v4/tools/rag.py`, ops-команда `python -m botai.ops.reindex_rag`, тесты в
`backend/tests/search/` и `backend/tests/db/`. Существующий полнотекстовый поиск v3 не изменён.

Гибридный поиск использует настоящий pgvector (`ORDER BY embedding_vec <=> …`) там, где есть
extension и колонка нужной ширины, и честно откатывается на JSONB-bounded-scan иначе
(`vector_backend` в результате: `pgvector` | `jsonb` | `none`). Критерий фазы 2.5
(«вопрос по документу/странице отвечается со ссылкой; эмбеддинги реально используются»)
закрыт тестами: семантически близкий чанк, который JSONB-скан с `vector_scan_limit` не видит,
векторная нога возвращает первой. Живой прогон API-провайдера эмбеддингов не выполнен:
в окружении нет ключа эмбеддингов (DeepSeek не отдаёт `/embeddings`) — это честно записано
в §11.

## 1. Что где лежит

| Часть | Файл | Что делает |
|---|---|---|
| Провайдеры эмбеддингов | `search/embeddings.py` | интерфейс `EmbeddingProvider`, локальный hashing, API (OpenAI-совместимый), mock, LRU-кэш, батчи, ретраи, стоимость |
| Гибридный поиск | `search/hybrid.py` | FTS + vector, reciprocal-rank fusion, rerank (лексика + свежесть + trust), провенанс в результате |
| Индексация и сервис запроса | `search/rag.py` | `RagSettings`, чанкинг, `RagIndexer` (артефакты/документы/текст), `RagQueryService`, фича-флаг |
| Модель | `db/models.py` → `ChunkEmbedding` | `chunk_embeddings`: JSONB-вектор (канон) + `embedding_vec vector(N)` (pgvector), провенанс, `(chunk_id, model)` уникален |
| pgvector | `search/pgvector.py` | `VectorType`, детект extension/колонки/ширины раз за процесс, SQL-запись и поиск `<=>`, backfill JSONB→vector |
| Миграции | `db/migrations/versions/20261031_b7e2c4a91d3f_chunk_embeddings.py`, `20261031_f2b7d9c4a6e8_chunk_embeddings_pgvector.py` | аддитивно: таблица, затем extension `vector` + колонка + HNSW (CONCURRENTLY) + backfill; downgrade оставляет extension |
| Инструменты | `agent/v4/tools/rag.py` | `rag_search`, `rag_ingest`, `build_rag_tools` |
| Ops-команда | `botai.ops.reindex_rag` | `python -m botai.ops.reindex_rag --email E [--source-id S]` — индексация артефактов и backfill векторов, без вывода содержимого |
| Тесты | `tests/search/*`, `tests/db/test_pgvector_migration.py` | миграция/детект/векторная нога/деградация/артефакты |

```
alembic heads  ->  f2b7d9c4a6e8 (head)
цепочка:        a8d4f1c2b6e9 -> c3f7a2b9d4e1 (sources domain) -> d6b8f2a14c73 (actions)
                -> f2b7d9c4a6e8 (pgvector)
```

## 2. Таблица `chunk_embeddings`

| Колонка | Тип | Зачем |
|---|---|---|
| `id` | ULID pk | |
| `user_id` | FK users CASCADE, index | tenant-предикат строкой, как у v3-таблиц |
| `chunk_id` | FK chunks CASCADE, index | чанк v3 (`chunks`), текст которого векторизован |
| `document_id` | FK documents CASCADE, index | документ/артефакт для цитаты |
| `source_id` | FK sources SET NULL | v4-источник, если текст пришёл из артефакта |
| `artifact_id` | FK source_artifacts SET NULL | конкретный артефакт (снапшот/страница/файл) |
| `model` | text 120 | имя провайдера/модели; несколько моделей сосуществуют |
| `dimension` | int | длина вектора |
| `content_hash` | text 64 | sha256 текста: дедуп и ключ кэша, не секрет |
| `embedding` | JSONB | сам вектор (канон, портируемо) |
| `embedding_vec` | `vector(N)` | pgvector-копия для HNSW-поиска; `NULL` у строк, вписанных до миграции/с другой шириной (backfill заполняет совпадающие) |
| `url`, `title`, `kind` | text / 300 / 20 | цитата и вид материала (artifact kind или document kind) |
| `captured_at` | timestamptz | когда материал прочитан источником |
| `trust` | text 10 | trust источника на момент снимка (`high/normal/low`) |
| `created_at` | timestamptz | |

Индексы: `(user_id, model)`, `source_id`, `content_hash`, плюс FK-индексы. Уникальность
`(chunk_id, model)` — повторная индексация другим провайдером не затирает прежний вектор.

**Почему не `chunks.embedding`.** Колонка в `chunks` не поддерживает несколько моделей и
переиндексацию без потери данных, а `chunks` — общая v3-таблица, которую нельзя менять без
падения существующего FTS. Отдельная таблица даёт: разные модели рядом, провенанс на строку,
дедуп по `content_hash`, и аддитивную миграцию без касания v3.

## 3. pgvector: сделано (2026-10-31)

**Решение: pgvector включён; JSONB остаётся каноном и fallback.** Исторический контекст:
сначала был только JSONB-bounded-scan, потому что dev/CI/prod жили на `postgres:17-alpine`
без extension, а миграция с `CREATE EXTENSION` уронила бы rollout. Теперь образы переключены
(`pgvector/pgvector:pg17` в `ops/compose.dev.yaml`, `ops/compose.prod.yaml`, CI; тот же PG 17,
`pgdata` переиспользуется), а `remote-deploy.sh` поднимает Postgres до миграций — поэтому
extension-миграция безопасна.

Что реализовано:

1. **Миграция `f2b7d9c4a6e8`** (после `d6b8f2a14c73`): `CREATE EXTENSION IF NOT EXISTS vector`,
   `chunk_embeddings.embedding_vec vector(BOTAI_RAG_DIMENSION|384)`, one-pass backfill JSONB→vector
   для строк той же ширины (остальные остаются `NULL` по дизайну), HNSW
   `USING hnsw (embedding_vec vector_cosine_ops)` строится CONCURRENTLY (сначала удаляется
   невалидный одноимённый индекс). Downgrade удаляет колонку/индекс и **оставляет** extension:
   другие данные могут им пользоваться, а снятие extension — отдельное решение.
2. **Модель** `ChunkEmbedding.embedding_vec` (свой `VectorType`, без Python-зависимости
   pgvector), `RagIndexer` пишет оба представления; JSONB — источник правды, `embedding_vec`
   можно перезаполнить backfill'ом/переиндексацией.
3. **`search/pgvector.py`** — детект доступности один раз на процесс (extension + колонка +
   ширина провайдера), SQL-запись `CAST(CAST(:v AS real[]) AS vector(N))`, backfill, детект
   несовпадения ширины с одним предупреждением (без значений).
4. **`hybrid.vector_candidates`** — при доступном pgvector один SQL
   `ORDER BY embedding_vec <=> CAST(CAST(:q AS real[]) AS vector(N)) LIMIT :k`; иначе прежний
   JSONB bounded scan. Результат честно помечает `vector_backend` (`pgvector|jsonb|none`),
   наружу в payload инструмента это не добавлено (контракт `rag_search` не менялся).
5. **Ops-команда** `python -m botai.ops.reindex_rag --email E [--source-id S]` — индексация
   артефактов источников и backfill векторов, батчами, без вывода содержимого.
6. **Тесты**: миграционный round-trip (+EXPLAIN доказывает использование HNSW), векторная нога
   реально включена и находит семантически близкий чанк, который JSONB-скан с
   `vector_scan_limit=2` не видит; деградация при несовпадении ширины с одним warning; двойная
   запись/backfill/артефакты/ops.

**Размерность фиксируется миграцией.** `BOTAI_RAG_DIMENSION` должен быть выставлен до первой
миграции/переиндексации; смена провайдера на другую ширину требует новой миграции и
переиндексации (JSONB при этом не теряется и продолжает работать через fallback).

## 4. Провайдеры эмбеддингов (`search/embeddings.py`)

Интерфейс: `name`, `dimension`, `embed(texts)`, `estimate_cost_usd(texts)`, `usage()`.

| Провайдер | Сеть/ключ | Назначение |
|---|---|---|
| `HashingEmbeddings` (`local-hash-v1`) | нет/нет | дефолт: детерминированное feature hashing слов + char-trigram'ов (терпит склонения «дедлайн/дедлайны»), L2-нормализация, zero cost |
| `ApiEmbeddings` | да/`BOTAI_EMBEDDING_API_KEY` по имени env | OpenAI-совместимый `/embeddings`: батчи (`batch_size`), ретраи 429/5xx/timeout (экспонента, honor `Retry-After`), оценка стоимости |
| `ScriptedEmbeddings` | нет | mock для тестов: заданные тексты → заданные векторы, остальное — hashing; этим тестом доказывается, что vector-нога реально предпочитает семантически близкий чанк |
| `CachedEmbeddings` | — | LRU по `content_hash` поверх любого провайдера; повторный текст не эмбеддится второй раз |

Безопасность: API-ключ читается из env по имени и кладётся только в `Authorization`; не хранится
на объекте, не попадает в `repr`, ошибки содержат только статус/причину (без тела ответа, которое
может эхоить входной текст). HTTP идёт через разрешённую фабрику `core.http.build_client`
(guard-тест `tests/core/test_architecture.py`), поэтому сеть/таймауты/редиректы в одном месте.

`usage()` отдаёт `calls`, `texts`, `cache_hits`, `estimated_tokens`, `cost_usd` — это попадает в
`results` инструментов и в будущую телеметрию.

## 5. Индексация (`search/rag.py`)

* Чанкинг: `split_text(chunk_chars=1200, overlap=150)`, границы абзац/строка/предложение/пробел,
  ограничение `MAX_CHUNKS_PER_UNIT=200`.
* Источники текста артефакта (`DefaultArtifactTextResolver`):
  `metadata["text"]` → `storage_ref` под настроенным `BOTAI_RAG_ARTIFACT_ROOT` (путь канонизируется
  и не имеет права выйти за root) → иначе артефакт пропускается с причиной. HTML очищается от
  тегов/скриптов, JSON pretty-print'ится, binary-виды (`screenshot`, `har`, …) не индексируются.
* `RagIndexer.ingest_source(user_id, source_id, kinds?, limit?)` — все текстовые артефакты источника;
  `ingest_artifact` — один; `ingest_text` — свободный текст; `ingest_document` — чанки существующего
  v3-документа (эмбеддинги + провенанс, без повторного чанкинга).
* Идемпотентность: `Document.hash = sha256(текста)`; повторный ingest того же текста не создаёт
  дублей (`deduplicated`), для новой версии текста создаётся новый документ — старый снапшот
  остаётся читаемым со своим `captured_at` (ретенция — отдельное решение, а не молчаливое удаление).
* Провенанс в `chunk_embeddings`: `source_id`, `artifact_id`, `url`, `title`, `kind`, `captured_at`,
  `trust` — из них строятся цитата и свежесть.

## 6. Гибридный поиск (`search/hybrid.py`)

Две независимые ноги, затем слияние:

1. **FTS** — `websearch_to_tsquery('russian', query)` + lenient OR-фолбэк (как в v3-чате),
   `ts_rank_cd`, live-документы (`LIVE_DOCUMENT`), фильтры по видам/источникам/свежести.
2. **Vector** — bounded scan `chunk_embeddings` (user + model), косинус, top-k по score.
3. **Fusion** — reciprocal-rank fusion (`1/(60+rank)`), нормализация; затем rerank:
   `0.65*rrf + 0.20*лексическое_пересечение + 0.15*свежесть (+0.03 high trust)`.
   Свежесть — экспоненциальное затухание от `captured_at` (или `fetched_at`) с полураспадом
   `BOTAI_RAG_HALF_LIFE_DAYS`; `stale` = старше `BOTAI_RAG_STALE_AFTER_DAYS`. Rerank можно выключить
   (`rerank=false`) — тогда только RRF.
4. **Деградация** — если провайдер эмбеддингов недоступен, возвращается FTS-only
   (`mode="fts_only"`, `degraded="vector_unavailable: …"`), найденный текст не скрывается.
5. **Провенанс** — в каждом результате `title`, `url`, `kind`, `source_id`, `artifact_id`,
   `document_id`, `captured_at`, `age_days`, `stale`, `reasons` (`fts`, `vector`, `lexical`, `fresh`,
   `high_trust`), `score`, `fts_rank`, `vector_score`.

Ограничение: vector-нога не поднимает чанки без эмбеддинга, но FTS их находит; при большом корпусе
растёт стоимость bounded scan — это и есть аргумент за pgvector (п. 3).

## 7. Инструменты для runtime (`agent/v4/tools/rag.py`)

`rag_search` (policy `read`): аргументы `query` (1..2000), `limit` (1..20), `kinds?`, `source_ids?`,
`fresh_after?` (ISO 8601), `rerank`. Возвращает `{query, mode, degraded, count, results[], usage}`;
каждый результат — цитата с провенансом; `summary` честно говорит «только FTS», если вектор упал.

`rag_ingest` (policy `write_soft`, `idempotent=True`): ровно один из `source_id` / `artifact_id` /
`text` (+`space_id` для текста), плюс `title?`, `url?`, `kind`, `limit`. Возвращает счётчики
`documents/chunks/embedded/deduplicated/skipped`, `reasons`, `usage`. Секретов не печатает.

Включение: фабрика `build_rag_tools(sessions)` возвращает `[rag_search, rag_ingest]` только при
`BOTAI_RAG_ENABLED=1`, иначе `[]`. Рантайм регистрирует их рядом с `build_basic_tools()`; файлы
runtime-инженера для этого править не нужно (импорт только публичного протокола). `sessions` —
`async_sessionmaker[AsyncSession]`.

## 8. Настройки (env, без ключей в репозитории)

| Переменная | Дефолт | Что |
|---|---|---|
| `BOTAI_RAG_ENABLED` | `0` | фича-флаг: инструменты подключаются только при `1/true/yes/on` |
| `BOTAI_RAG_PROVIDER` | `local` | `local` (hashing, offline) или `api` |
| `BOTAI_RAG_DIMENSION` | `384` | размерность локального провайдера |
| `BOTAI_RAG_BATCH_SIZE` | `64` | батч API |
| `BOTAI_RAG_CHUNK_CHARS` / `_CHUNK_OVERLAP` | `1200` / `150` | чанкинг |
| `BOTAI_RAG_VECTOR_SCAN_LIMIT` | `500` | предел JSONB-скана |
| `BOTAI_RAG_HALF_LIFE_DAYS` / `_STALE_AFTER_DAYS` | `30` / `30` | свежесть и устаревание |
| `BOTAI_RAG_API_ENDPOINT` | — | URL `/embeddings` (https) |
| `BOTAI_RAG_API_MODEL` | `text-embedding-3-small` | имя модели |
| `BOTAI_RAG_API_KEY_ENV` | `BOTAI_EMBEDDING_API_KEY` | имя env с ключом (не значение) |
| `BOTAI_RAG_ARTIFACT_ROOT` | — | корень хранилища `storage_ref` |

Настройки живут в `RagSettings.from_env()`, а не в `botai.settings.Settings`: фаза 2.5 писалась
параллельно с правками `settings.py` (task-12). Перенос в `Settings` — механическая правка одного
файла, поведение не меняется.

## 9. API поиска

Сервисный слой готов: `RagQueryService.search(user_id, query, limit, kinds, source_ids, fresh_after,
rerank)` возвращает `RagAnswer` с цитатами/провенансом/деградацией. REST-эндпоинт не добавлен:
`api/`-слой и его регистрация в `main.py` в этой задаче не наши файлы. Подключение — один роутер:
`POST /api/rag/search` (auth user_id из зависимости, `RagQueryService` из wiring) и
`POST /api/rag/ingest` по политике владельца; контракт — те же поля, что у инструментов.

## 10. Тесты

```
cd backend && uv run --frozen pytest tests/search -q        # 78 passed (31 из них RAG)
uv run --frozen pytest tests/search -q -k rag               # 31 passed
uv run --frozen pytest tests/db/test_schema.py -q           # 4 passed (модель == миграция)
uv run --frozen pytest tests/core/test_architecture.py -q   # 2 passed (HTTP-guard)
```

Что покрыто:

* эмбеддинги: детерминизм/нормализация hashing; близкие склонения ближе несвязанного текста;
  mock-семантика (синонимы в одном концепте); кэш без повторного вызова; API: батчи, 429→retry,
  5xx→фейл, отсутствие ключа в ошибке/`repr`, ключ не эхоится из тела ответа;
* индексация: схема таблицы; артефакты источника → документ+чанки+векторы с провенансом
  (`source_id`, `artifact_id`, `captured_at`, `trust`, `kind`, `content_hash`); идемпотентный
  повтор (`deduplicated`); пропуск binary-артефакта с причиной; HTML из `storage_ref` без тегов и
  скриптов; отказ читать путь за пределом root; `ingest_text`, `ingest_document`, чужой артефакт;
* гибрид: vector-нога ставит семантически близкий чанк выше несвязанного; FTS работает без
  эмбеддингов; провенанс/свежесть/`stale`; фильтры `kinds`/`source_ids`/`fresh_after`; изоляция
  пользователей; деградация до FTS с честным `degraded`; RRF-слияние;
* инструменты: валидация аргументов, `no_user`, ровно одна цель у `rag_ingest`, счётчики и
  summary, политики `read`/`write_soft`, фабрика по флагу.

## 11. Ограничения и что осталось

* **Живого прогона API-провайдера эмбеддингов нет**: в окружении нет ключа эмбеддингов
  (DeepSeek не отдаёт `/embeddings`), поэтому `ApiEmbeddings` проверен только mock-transport'ом
  (батчи/ретраи/парсинг/отсутствие утечки ключа). Что докажет: владелец выставляет
  `BOTAI_RAG_PROVIDER=api`, `BOTAI_RAG_API_ENDPOINT`, `BOTAI_EMBEDDING_API_KEY`,
  `BOTAI_RAG_DIMENSION=<ширина модели>` **до** миграции/переиндексации, затем
  `python -m botai.ops.reindex_rag --email E --source-id S` и один `rag_search` с
  `vector_backend="pgvector"` и ненулевым `usage.costUsd`.
* **Локальный провайдер — не семантика**, а hashing+trigrams (лексическая близость). Настоящая
  семантика — только с API-провайдером.
* **Автоиндексации после run нет**: артефакты индексируются инструментом `rag_ingest` или
  ops-командой `reindex_rag`; хук после sync источника сознательно не подключён в этой итерации
  (`build_indexer`/`reindex_source_artifacts` — готовые точки подключения). Это осознанный
  остаток, а не забытый вызов.
* **REST/RAG-эндпоинта нет**: поиск доступен агенту инструментом и через сервис; отдельный
  публичный роут — не делали (контракт не менялся).
* **Recall/latency на реальном корпусе не измерялись**: порядок доказывается синтетическим
  тестом, где векторная нога находит то, что JSONB-скан с `vector_scan_limit` не видит; на
  pet-масштабе этого достаточно, при росте корпуса — замер по снапшоту.
* **Ширина вектора фиксируется миграцией**: смена провайдера на другую размерность требует новой
  миграции и переиндексации; JSONB при этом не теряется и продолжает работать fallback'ом.
* **Retention**: старые снапшоты одного источника после изменения текста не удаляются (это
  сознательно: свежесть видна); очистка — отдельная задача вместе с retention артефактов.
* **Ранжирование эвристическое** (RRF + лексика + свежесть), без cross-encoder rerank; веса
  вынесены в константы, при живых прогонах их надо подбирать по метрикам.
* **Очередь/задания**: индексация не ставится фоновой job'ой; сборка чанков идёт в процессе
  вызывающего (инструмент/ops), большие корпуса стоит вынести в job при необходимости.
