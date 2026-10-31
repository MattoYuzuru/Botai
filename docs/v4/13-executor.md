# Исполнение кода агентом: local executor и контейнерная песочница (фаза 1.2)

Статус: **сделано 2026-10-31.** Реализует пункт 1.2 плана
([05-implementation-plan.md](05-implementation-plan.md)) и инструмент `code_exec` из
[02-architecture.md](02-architecture.md) §6 / §4.4. Код:
`backend/src/botai/agent/v4/executor/`; тесты: `backend/tests/agent/v4/test_executor.py`
(local) и `backend/tests/agent/v4/test_executor_container.py` (Docker sandbox, docker-gated).
Local-режим — для дев-машины и тестов; в production (и в контейнере worker'а) код агента
исполняется **только** `ContainerExecutor` (Docker-песочница), [D-003](06-decisions.md).

Документ объясняет: API, что local-режим реально гарантирует и чего не гарантирует,
какие лимиты и как их проверять, как это подключается к runtime и что осталось до
контейнерной песочницы.

## 1. API

Пакет `botai.agent.v4.executor` (все имена экспортируются из `__init__.py`):

| Имя | Что это |
|---|---|
| `ExecRequest` | `language: "python"\|"js"\|"bash"`, `code: str`, `files: dict[str,str]` (записываются в workspace **до** запуска), `timeout_s: float\|None`, `memory_mb: int\|None` (запрос может **сузить** лимит на вызов; значение выше `ExecLimits` обрезается до лимита исполнителя); строгая pydantic-модель |
| `ExecResult` | `stdout`, `stderr`, `exit_code`, `duration_ms`, `files` (созданные этим запуском пути относительно workspace), `truncated`, `timed_out`; свойство `ok` = `exit_code == 0 and not timed_out` |
| `ExecLimits` | лимиты исполнителя: `timeout_s=30`, `memory_mb=256`, `cpu_s` (None → `ceil(timeout)`), `max_output_bytes=1 MiB` **на поток**, `max_files=1000`, `nofile=256`, `fsize_mb=64`, `max_file_bytes=1 MiB`, `max_request_bytes=4 MiB`, `max_request_files=100` |
| `Workspace` | каталог одной задачи агента; файлы переживают вызовы. `create(root=None)` (временный каталог) / `create(path)` (каталог вызывающего), `resolve`, `write_text`, `read_text`, `list_files`, `snapshot`, `internal_dir`, `cleanup`, контекстный менеджер |
| `Executor` (Protocol) | `workspace`, `limits`, `async run(ExecRequest) -> ExecResult`; local и будущий container реализуют один интерфейс |
| `LocalExecutor` | реализация на процессе хоста (см. §2) |
| `ContainerExecutor` | Docker-песочница production: один эфемерный контейнер на вызов `docker run --rm`, workspace — bind-mount, non-root uid 10001, read-only root, cgroups; без fallback на local |
| `ExecutorError` / `WorkspacePathError` / `ExecutorUnavailable` | отказ исполнителя/пути/отсутствующий runtime; это не ошибка кода агента |

```python
from botai.agent.v4.executor import ExecRequest, LocalExecutor, Workspace

workspace = Workspace.create()                      # или Workspace.create(path) в задаче
executor = LocalExecutor(workspace=workspace, limits=ExecLimits(timeout_s=60))
first = await executor.run(ExecRequest(language="python", code="open('data.json','w').write('{}')"))
second = await executor.run(
    ExecRequest(language="python", code="import json; print(json.load(open('data.json')))")
)
assert second.ok and second.stdout == "{}\n"
```

`run` асинхронный и безопасен для event loop: работа процесса уходит в `asyncio.to_thread`,
вызовы одного `LocalExecutor` сериализуются (один workspace — один запуск за раз).

`files` в `ExecResult` — **новые** файлы (созданные запуском); файлы из `ExecRequest.files`
и прошлых вызовов не попадают. Размер каждого ограничен `max_file_bytes`, всего запроса —
`max_request_bytes`. Очень большие/много файлов — отказ `ExecutorError` до запуска.

## 2. Модель угроз и честные границы local-режима

**Где его НЕЛЬЗЯ включать.** `LocalExecutor` — только для `ENV=dev`/`test` и локальной
машины разработчика. В production (`ENV=prod`) и в контейнере worker'а, где лежат
`DATABASE_URL`, `BOTAI_KEK`, ключи провайдеров и Vault-сессии, его включать нельзя:
локальный код не изолирован от файловой системы и процессов worker'а (§ниже) и может
прочитать то, что контейнерная песочница обязана скрыть. Wiring фазы 1 (task-16)
enforcing'ает это: при `ENV=prod` выбирается только контейнерный executor, попытка
собрать `LocalExecutor` в prod — явная ошибка конфигурации.

**Для чего он.** Фаза 1 должна дать агенту `code_exec` в дев- и локальном режиме, пока
нет контейнерного executor'а: агент пишет парсер, запускает его, читает stdout/stderr,
получает созданные файлы и продолжает работу с ними. Runtime при этом ничего не знает о
реализации — в будущем `LocalExecutor` заменяется на `ContainerExecutor` по конфигу.

**Что local-режим гарантирует:**

- каждый запуск — отдельный процесс в отдельной сессии; по таймауту убивается **вся
  группа** (`SIGKILL`), а «фоновые» дети, оставшиеся после нормального выхода, тоже
  добиваются в конце вызова: они не переживают задачу и не держат output-пайпы;
- окружение собирается с нуля по allowlist: `PATH` (чтобы работали установленные в
  worker'е инструменты), локаль/таймзона, и фиксированные безопасные переменные.
  **Родительские `BOTAI_*`, `DATABASE_URL`, KEK, `*_KEY`/`*_TOKEN`/`*_PASSWORD`/секреты
  не передаются.** Явный `extra_env` встраивающего приложения фильтруется: имя, похожее
  на секрет или backend-настройку, — `ValueError` (лучше упасть, чем утечь); имена,
  которыми владеет исполнитель (`HOME`, `PATH`, `PYTHONPATH`, `LD_PRELOAD`,
  `NODE_OPTIONS`, …), переопределить нельзя;
- `HOME`, `TMPDIR`, `XDG_*` — внутри workspace (`.botai-exec/home`, `.botai-exec/tmp`):
  программы не читают и не пишут настоящий home worker'а, думая, что это home;
- `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE`, `RLIMIT_CORE=0`; `RLIMIT_AS`/`RLIMIT_DATA`
  — где ОС их применяет (Linux), best-effort (macOS отказывает `setrlimit` для AS/DATA,
  см. §4);
- stdout/stderr ограничены **каждый** `max_output_bytes` (по умолчанию 1 MiB: больше —
  `truncated=True`, поток всё равно дочитывается и выбрасывается, процесс не убивается
  из-за собственного вывода);
- пути: файлы запроса, `Workspace.write_text/read_text` и всё, что делает runtime через
  `Workspace`, проходят `resolve`: абсолютные пути, `..` и симлинки, ведущие наружу,
  отклоняются **до** записи. Приватный `.botai-exec` скрыт от листинга/снапшота и
  зарезервирован.

**Чего local-режим НЕ гарантирует (принятые ограничения фазы 1):**

- **Это не OS-jail.** Сам исполняемый код — обычный процесс пользователя-worker'а: он
  может открыть `/etc/passwd`, файлы в домашнем каталоге worker'а, `~/.ssh` и т.п.
  Защита путей действует на API workspace и файлы запроса, а не на произвольный
  `open()` внутри кода. Границу обеспечит контейнер (read-only root, non-root,
  отдельный том, seccomp/no-new-privileges);
- **сеть не ограничена** — это осознанно (D-003: свободный harness), но значит, что код
  может ходить куда угодно; изоляция сети — тоже контейнер;
- **память на macOS не ограничивается** (`RLIMIT_AS`/`RLIMIT_DATA` не выставляются
  ядром); для `node`/V8 AS-лимит не ставится и на Linux — V8 резервирует огромное
  виртуальное пространство и падает при `RLIMIT_AS` даже 256 MiB (проверено на Linux).
  Лимит памяти для python/bash на Linux работает: `bytearray(512 MiB)` при лимите
  128 MiB даёт `MemoryError`;
- **`preexec_fn` не потокобезопасен** по документации Python; здесь он вызывается в
  отдельном потоке и делает только `setrlimit`. Для локального режима это приемлемо,
  для продакшена — контейнер;
- лимит на число процессов (`RLIMIT_NPROC`) не ставится (на macOS он общий для
  пользователя); fork-bomb в local-режиме — риск владельца дев-машины;
- local-режим POSIX-only (macOS/Linux), как и worker проекта.

Итог: local-режим — «песочница по договорённости» для кода, который пишет владелец/агент
на своей машине; недоверенный код исполняется только в контейнере (§5).

### Контейнерный режим (`ContainerExecutor`)

`ContainerExecutor` запускает **один эфемерный контейнер на один `ExecRequest`**
(`docker run --rm`, образ `BOTAI_SANDBOX_IMAGE`), а не один долгоживущий на задачу. Причина:
лимиты (время/память) задаются на вызов и могут сужаться запросом, таймаут обязан убивать
контейнер вместе с детьми; свежий контейнер повторяет семантику свежего процесса
`LocalExecutor` и не переносит процессы между вызовами. Файлы переживают вызовы через
bind-mount workspace, `HOME`/`TMPDIR`/`XDG_*` — внутри workspace, поэтому `pip --user`/кэш
остаются в задаче. Цена — старт контейнера ≈0.2–1 с на `code_exec`.

Гарантии (проверены docker-gated тестами):

- `--read-only` корень, `tmpfs` на `/tmp`, `--cap-drop ALL`, `no-new-privileges:true`;
- non-root `uid 10001`; workspace смонтирован `rw` в `/workspace` (файлы доступны и воркеру);
- сеть разрешена (D-003): песочница ходит в интернет, как и агент, но не видит БД/Vault;
- окружение контейнера — **только явный allowlist** (`PATH`, `HOME` внутри workspace, локаль,
  `PYTHON*`, `TZ`); родительские `BOTAI_*`, `DATABASE_URL`, KEK, ключи провайдеров не
  передаются; docker-сокет и docker CLI внутри песочницы отсутствуют;
- cgroups: `--memory`/`--memory-swap` без swap, `--pids-limit`, `--cpus`, `--ulimit`
  `cpu`/`nofile`/`fsize`; wall-таймаут — `docker kill` + `docker rm -f` (умирает init и все
  дети), `--rm` на случай штатного выхода; stdout/stderr обрезаются как в local-режиме;
- каждый контейнер помечен `botai.agent.sandbox=1` и workspace-hash'ем: деплой и оператор
  могут подмести остатки `docker rm -f $(docker ps -aq --filter label=botai.agent.sandbox=1)`;
- preflight один раз на executor: `docker image inspect` + пробный контейнер, который читает
  маркер и пишет файл в bind-mount — так проверяется, что демон видит **тот же абсолютный
  путь** и uid 10001 может писать; иначе — громкий `ExecutorUnavailable`, никогда не fallback
  на процесс хоста.

Разворачивание (production): образ `ghcr.io/mattoyuzuru/botai-sandbox:<SHA>` собирает CI
вместе с app-образом; `remote-deploy.sh` тянет его, сверяет `org.opencontainers.image.revision`
и убирает оставшиеся sandbox-контейнеры. API и worker получают Docker CLI (из
`docker:29.5.2-cli`, без демона), `/var/run/docker.sock` и `group_add: DOCKER_GID` (хост-группа
docker); каталоги `/srv/botai/agent-workspaces` и `/srv/botai/source-artifacts` (`0700`,
UID 10001) создаются один раз `bootstrap-server.sh` и монтируются в контейнеры по тем же
абсолютным путям. Настройки: `BOTAI_AGENT_EXECUTOR=auto|local|container` (в `ENV=prod`
допустим только container, local — громкая ошибка), `BOTAI_SANDBOX_IMAGE`,
`BOTAI_SANDBOX_DOCKER_HOST` (опционально), `BOTAI_AGENT_WORKSPACE_ROOT`. Детали деплоя —
[deployment.md](../plan/deployment.md#v4-агент-в-production-sandbox-docker-socket-включение-2026-10-31).

Границы: workspace песочницы — общий `rw` с воркером (агент пишет туда свои файлы, воркер их
читает/индексирует), поэтому «секреты воркера» защищены не монтированием, а тем, что их нет в
workspace. Docker-сокет у api/worker — root-эквивалент на хосте (осознанный обмен pet-проекта,
D-001/D-016); песочница сокета не получает. На macOS в контейнерном режиме нужен Docker,
видящий путь workspace (Colima делит `/Users`), и запись uid 10001 — иначе preflight честно
падает; CI и production Linux этих ограничений не имеют.

## 3. Языки и окружение запуска

| Язык | Интерпретатор | Как задаётся |
|---|---|---|
| `python` | `sys.executable` того же venv, что и worker | `python=` в конструкторе |
| `js` | `node` из `PATH` (v22+ по архитектуре) | `node=` в конструкторе; нет node → `ExecutorUnavailable` |
| `bash` | `bash` из `PATH`, иначе `/bin/bash` | `bash=` в конструкторе |

В окружении запуска: `PATH` родителя, `LANG`/`LC_ALL`/`LC_CTYPE`/`TZ` (если заданы),
`HOME`/`TMPDIR`/`XDG_*` в workspace, `NO_COLOR=1`, `PYTHONUNBUFFERED=1`,
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`, для python — `PYTHONPATH=<workspace>`
(агент может импортировать helper, написанный прошлым вызовом). Скрипт пишется в
`.botai-exec/runs/<id>/main.{py,js,sh}` и удаляется после вызова; cwd = корень workspace.

## 4. Лимиты, таймаут и вывод

| Лимит | Значение по умолчанию | Поведение |
|---|---|---|
| `timeout_s` | 30 (request ≤ 300) | wall-clock; по истечении `SIGKILL` группе, `timed_out=True`, `exit_code != 0` |
| `cpu_s` | `ceil(timeout_s)` | `RLIMIT_CPU` soft/hard = `cpu_s`/`cpu_s+1`: CPU-цикл умирает по `SIGXCPU` (`exit_code == -24`) даже при большом wall-таймауте |
| `memory_mb` | 256 | `RLIMIT_AS`/`RLIMIT_DATA` для python/bash там, где ОС применяет; node — нет (§2) |
| `max_output_bytes` | 1 MiB на поток | stdout/stderr обрезаются, лишнее дочитывается; `truncated=True` |
| `nofile` | 256 | `RLIMIT_NOFILE` |
| `fsize_mb` | 64 | `RLIMIT_FSIZE`: один файл не больше 64 MiB |
| `max_files` / `max_file_bytes` / `max_request_bytes` | 1000 / 1 MiB / 4 MiB | лимиты workspace-снапшота и файлов запроса |

`ExecLimits` валидирует значения в `__post_init__` (`ValueError` на бессмысленных).
`ExecRequest.timeout_s/memory_mb=None` — взять из `ExecLimits`; иначе действует
`min(запрос, ExecLimits)`: запрос может сузить лимит на один вызов, но не поднять
потолок, выставленный при сборке executor'а. CPU-лимит по умолчанию привязан к
эффективному (уже обрезанному) wall-таймауту, чтобы «вечный» CPU-цикл не жил дольше
разрешённого времени.

## 5. Как это подключается к runtime и что дальше

**Сейчас (runtime-engineer, `agent/v4/tools/`).** Runtime создаёт один `Executor` на run
(или на задачу), инструмент `code_exec` валидирует JSON-схему, собирает `ExecRequest`,
вызывает `await executor.run(...)`, кладёт `stdout/stderr/exit_code` в результат
инструмента, а созданные файлы — в артефакты/трассу (их модель `artifact://` появится в
слое трасс; исполнитель отдаёт пути в workspace). Имена языков: инструмент архитектуры
говорит `javascript`, `ExecRequest.language` — `"js"`; адаптер в инструменте.

**Контейнерный executor (`ContainerExecutor`).** Тот же `ExecRequest/ExecResult`: один
эфемерный контейнер на вызов, workspace — bind-mount абсолютного пути, код — non-root
uid 10001, read-only корень, `no-new-privileges`, без `docker.sock` и доступа к БД/Vault;
сеть разрешена; CPU/память/pids — cgroups (memory-лимит работает и для node). Runtime ничего
не меняет: исполнитель выбирается одним местом — `agent/v4/factory.py::build_executor`
(в `ENV=prod` это всегда `ContainerExecutor`; `BOTAI_AGENT_EXECUTOR=local` в prod — ошибка
конфигурации; `BOTAI_AGENT_EXECUTOR=container` включает песочницу и вне prod). `LocalExecutor`
остаётся для дев-машины и тестов. Секреты в env песочницы не попадают: агент получает их
только через `secret_use` (HTTP/поле формы), вывод маскируется слоем секретов.

## 6. Как проверять

```bash
cd backend && uv run --frozen pytest tests/agent/v4 -q
```

Покрыто (`tests/agent/v4/test_executor.py`):

- python/js/bash: успех, stdout/stderr, exit code, ошибка (traceback/nonzero);
- timeout: убийство группы и отсутствие оставшихся `sleep`-процессов;
- CPU-лимит: `while True` умирает по `SIGXCPU` при длинном wall-таймауте;
- POSIX-лимиты видны изнутри ребёнка (`RLIMIT_CPU`, `RLIMIT_NOFILE`; `RLIMIT_AS` — на
  Linux), memory-лимит на Linux (на macOS тест пропускается — см. §2/§4);
- env isolation: родительские `BOTAI_*`/`DATABASE_URL`/API-ключи не видны, `HOME` внутри
  workspace; `extra_env` с секретным именем — отказ;
- workspace: файлы переживают вызовы, `files` содержит только созданное этим вызовом,
  файлы запроса пишутся до запуска, приватный каталог скрыт;
- path traversal: `../`, абсолютные пути, симлинк наружу — отказ до записи;
- truncation: оба потока обрезаны, процесс не убит своим выводом;
- `LocalExecutor` проходит `runtime_checkable` протокол; self-created workspace чистится
  `close()`, после `close()` запуск — ошибка.

Валидацию Linux-лимитов (которую macOS не может прогнать локально) проверяли отдельно
в Linux-контейнере: `RLIMIT_AS`/`RLIMIT_DATA` 128 MiB применяются, `bytearray(512 MiB)`
→ `MemoryError`; `RLIMIT_AS` для node подтверждён как неприменимый (V8 падает на старте).

Контейнерная песочница (`tests/agent/v4/test_executor_container.py`, docker-gated; CI собирает
`botai-sandbox:ci` и гоняет их, локально нужен Docker/Colima и образ
`docker build -f ops/docker/sandbox.Dockerfile -t botai-sandbox:test .`):

```bash
cd backend && BOTAI_SANDBOX_IMAGE=botai-sandbox:test \
  uv run --frozen pytest tests/agent/v4/test_executor_container.py tests/agent/v4/test_executor.py -q
```

Покрыто: python/js/bash; персистентность файлов двумя вызовами; файлы запроса пишутся до
запуска; свежий pid-namespace на вызов (фоновый `setsid`-ребёнок не переживает вызов);
env isolation (`BOTAI_*`, `BOTAI_KEK`, `DATABASE_URL`, ключи провайдеров, docker-сокет и CLI
внутри песочницы отсутствуют; `HOME`/cwd в `/workspace`); read-only корень + writable
`/tmp` + uid 10001; `docker kill` по wall-таймауту (включая детей), executor остаётся
рабочим; cgroups memory/pids (аллокация 512 MiB убивается при лимите 128 MiB, fork-storm
останавливается `pids.max`); RLIMIT_CPU; truncation 1024/1024; параллельные executor'ы
изолированы; `close()` идемпотентен, контейнеров не остаётся; отсутствие Docker CLI/образа/
недоступный демон → `ExecutorUnavailable` с понятным текстом; prod/dev-матрица выбора
executor'а; сквозной `code_exec` через настоящий runtime пишет и читает файл в песочнице.

## 7. Ограничения и риски (сводка)

1. Local-режим не изолирует файловую систему/сеть/процессы — только лимиты и окружение;
   недоверенный код — только контейнер (§2, §5). В `ENV=prod` local — ошибка конфигурации.
2. Память на macOS не ограничена; в контейнерном режиме — cgroups для всех языков (включая
   node); для local-режима на Linux — `RLIMIT_AS`/`RLIMIT_DATA` best-effort.
3. `preexec_fn` не потокобезопасен; local-режим полагается на то, что код пишет владелец.
4. Нет stdin у процесса (DEVNULL) и нет стриминга вывода: только результат после
   завершения/таймаута (стриминг — `code_job`/трассы, следующая итерация).
5. Долгий код (`code_job`, отдельный run) не реализован: wall-таймаут — потолок (300 с).
6. Пакеты (`pip/npm install`) не ставятся исполнителем специально — код может ставить их
   сам через сеть; в контейнерном режиме кэш/`HOME` живут в workspace задачи (переживают
   вызовы), но не между задачами; тёплого пула контейнеров нет — старт ≈0.2–1 с на вызов.
7. Артефакты/RAG/трассы для созданных файлов — на стороне runtime; исполнитель отдаёт
   только пути в workspace.
8. Контейнерный workspace — общий `rw` с воркером (по нему читаются созданные файлы),
   поэтому граница — не монтирование, а отсутствие секретов в workspace и env песочницы.
9. Docker-сокет у api/worker даёт root-эквивалент на хосте; песочница сокета не получает.
   Это осознанный обмен pet-проекта (D-001), описан в deployment.md; альтернатива на будущее —
   отдельный sandbox-агент с узким API.
10. На macOS контейнерный режим требует Docker, видящего путь workspace (Colima делит
    `/Users`) и записи uid 10001; иначе preflight падает громко (это не production-путь).
11. Образ песочницы собирается CI под тот же commit SHA, что и app-образ; `--pull never`
    в executor'е: отсутствующий образ — ошибка деплоя, а не тихая загрузка в середине задачи.

## 8. Ссылки

- [02-architecture.md](02-architecture.md) §6 «Исполнение кода агентом», §4.4 `code_exec`,
  C5 sandbox.
- [05-implementation-plan.md](05-implementation-plan.md) фаза 1.2 «Полный набор
  инструментов» (`code_exec`, `fs_*`).
- [06-decisions.md](06-decisions.md) D-003 (агент — полноценный harness: произвольный код
  допустим), D-009 (мягкие лимиты, аварийный стоп).
- `docs/v4/11-runtime.md` — runtime, инструменты и трассы (пишется параллельно в фазе 1;
  здесь — только точка стыковки через `Executor`).
- `docs/v4/12-browser-control.md` — браузерный слой (соседняя часть инструментов фазы 1;
  пишется параллельно).
- [deployment.md](../plan/deployment.md) — sandbox-образ, docker-сокет, каталоги, включение
  v4 в production и откат.
- `ops/docker/sandbox.Dockerfile`, `ops/docker/app.Dockerfile`, `ops/deploy/remote-deploy.sh`,
  `ops/compose.prod.yaml` — разворачивание песочницы.
