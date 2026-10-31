# 04. Живой бенчмарк свободного harness

Статус: **работает, живой прогон L3 выполнен 2026-10-31** на реальных учётных данных
владельца (ВШЭ) и на живой сессии Chrome (ВШЭ + ЦУ). Реализация — [`bench/live/`](../../bench/live/),
санитизированное свидетельство — [`_evidence/benchmark/2026-10-31/`](_evidence/benchmark/2026-10-31/).
Код: HEAD `a23585a` + незакоммиченный `bench/live/`.

Цель бенчмарка — не «красивая демонстрация», а **измерение**: скрипт реально вводит
креды владельца на сайт вуза, при этом агент и модель значений не видят; после
прогона остаются следы (HAR, cookie/сессия, скриншоты, сетевой лог, `results.json`),
по которым и агент, и владелец проверяют, что получилось, а где стоит барьер.

Порядок: [README пивота](README.md) → этот документ → [05-implementation-plan](05-implementation-plan.md)
(фаза 4: 4.1–4.4).

## 1. Уровни проверки L0–L3

| Уровень | Что проверяет | Сеть | Ключ модели | Где | Кто |
|---|---|---|---|---|---|
| **L0** — юнит/синтетика | Разбор ответов API, подсчёт сущностей, redaction (`Redactor`, `sanitize_har`), формат `results.json`; парсеры на фикстурах | Нет | Нет | локально, CI | все |
| **L1** — фикстуры | Сквозной контур продукта на `fakeuni` (loopback): вход, SPA, API, sync, repair, чат | loopback | нет | локально, CI | все |
| **L2** — живая модель | Качество агента с DeepSeek (`BOTAI_LIVE_LLM=1`): обучение вуза, починка, чат, инъекции; живой прогон модели по фикстурам/стенду | API модели | да | локально | владелец/агент |
| **L3** — живой вуз | Настоящий вход в ВШЭ/ЦУ, сессия, API discovery, динамическая страница, действие, артефакты | вузы + модель (если сценарий с агентом) | да | локально | владелец/агент по поручению |

Границы доказательства: L1/L2 не заменяют L3, а L3-прогон 31.10.2026 не проверяет
качество LLM-агента — он измеряет **примитивы harness** (секретный ввод, браузер,
сессия, сеть, чтение). Модель в этих прогонах не вызывалась; для честного сравнения
«агент vs внешний harness» (п. 5) нужен сценарий L3-agent (план 4.3).

## 2. Секреты: креды видны инструменту, но не модели

Первичные секреты — логин и пароль ВШЭ. Они лежат **только** в macOS Keychain:

| Сервис Keychain | Что | Кто читает |
|---|---|---|
| `botai.live.hse.mail` | логин владельца | `bench/live/secret_broker.py::get_secret` → код, который печатает в поле формы |
| `botai.live.hse.password` | пароль владельца | то же |

Правила, проверенные в прогоне:

* **Никакого вывода значений.** CLI broker'а печатает только имена сервисов и булевы
  «есть/нет»; значения возвращаются только коду-потребителю, в память процесса.
* **Никаких значений в argv.** `security add-generic-password -w` получает пароль через
  stdin (значение + повтор), иначе он был бы виден в списке процессов.
* **`.env` очищен.** Миграция `python -m bench.live.secret_broker migrate` перенесла
  пары `HSE_MAIL`/`HSE_PASSWORD` в Keychain и заменила строки комментарием; значения
  остались только в Keychain. Приложение и тесты эти переменные не читали и не читают
  (`python -m bench.live.secret_broker usage` — 0 ссылок в коде; проверено grep'ом).
* **Пароль-поля вычищаются из HAR и логов.** `bench/live/redact.py`: заголовки
  `Cookie/Set-Cookie/Authorization`, query-параметры `code/token/session_id/...`,
  `postData.text` **и `postData.params`** (найдено живым прогоном: Playwright кладёт
  поля формы ещё и в `params`), `Set-Cookie` ответов, тела ответов; последний проход
  вырезает зарегистрированные значения из всего JSON HAR.
* **Финальный sweep.** После прогона все текстовые артефакты проверяются на значения
  секретов; если что-то нашлось — файл перезаписывается с заменой, в `results.json`
  попадает список (`secretSweep.redactedFiles`, только имена файлов). В финальном
  прогоне — 0 правок; в первом живом прогоне sweep поймал утечку `postData.params`
  (см. п. 4) — это и есть работа предохранителя.
* **Сессия — рабочий материал, но вне репозитория.** Cookie, storage state, HAR,
  скриншоты живут в `~/.local/state/botai-v4-bench/<дата>/<запуск>/raw/` (0700,
  файлы 0600), не в git. В репозиторий едет только санитизированная сводка без
  cookie/токенов/почты/имён.

CU (Центральный университет): первичных кредов у бенчмарка нет. Источник — **живая
сессия браузера владельца**: копия профиля Chrome → cookie через CDP → новый
контекст. Сохранённая сессия skill `central-university-lms`
(`~/.config/yuzuru-codex-skills/central-university-lms/storage-state.json`, 0600)
проверяется как второй источник; на 31.10.2026 она протухла (`401 reauth_required`,
mtime 2026-09-28) — это зафиксированный барьер, а не отказ бенчмарка.

## 3. Как устроен pipeline

```
secret_broker (Keychain) ──► run.py
                              │
                              ├─ Chrome: --user-data-dir=<копия профиля> --remote-debugging-port
                              │    ├─ chrome-copy: сессия владельца (cookie, 3343 в профиле)
                              │    └─ chrome-fresh: пустой профиль, логин формой
                              │
                              ├─ Playwright connect_over_cdp → incognito-контекст на каждый сценарий
                              │    ├─ HAR-рекордер (тела ответов не пишутся), console, screenshots
                              │    ├─ сетевой лог: method/URL/status/тип, поиск XHR/API
                              │    ├─ логин: fill username+password из broker'а → submit → разбор исхода
                              │    ├─ SPA: settle + динамический рендер, DOM-счётчики
                              │    ├─ авторизованный GET внутри страницы (Bearer из localStorage)
                              │    ├─ одна безопасная форма (GET, без записи)
                              │    └─ storage_state → raw/ (0600)
                              │
                              ├─ эталонный harness (skill ЦУ) на том же storage_state
                              ├─ results.json (полный, вне репо) + sanitized summary (в репо)
                              └─ secret sweep + sanitize всех HAR
```

Браузерный приоритет (`bench/live/browser.py`):

1. **`chrome-copy`** — настоящий Google Chrome (`Chrome/156.0.8078.74`) с временной
   копией профиля владельца: отпечаток, локаль `ru-RU`, часовой пояс `Europe/Moscow`,
   TLS и cookie — как у устройства. Профиль копируется (`Local State`, `Default/Cookies`,
   `Preferences`, `Local Storage`), исходный не открывается; копия удаляется после
   сценария. Ключ Chrome Safe Storage не читается бенчмарком — расшифровкой cookie
   занимается сам Chrome.
2. **`chrome-fresh`** — тот же Chrome с пустым временным профилем: чистый incognito-
   контекст, куда код вводит креды из Keychain и наблюдает, что делает сайт.
3. **`chromium`** — встроенный Playwright Chromium, только если Chrome недоступен.

Режимы `chrome-*` работают headless (`--headless=new`) и по флагу `--headed`.

## 4. Сценарии

| Сценарий (`--scenario`) | Что делает | Состояние |
|---|---|---|
| `hse-login` (`login`, `hse`) | вход ВШЭ по кредам из Keychain в новом профиле Chrome: Keycloak `saml.hse.ru` → `lk.hse.ru`; затем расписание, API discovery, safe GET, форма Moodle, 62 курса через SSO | **работает, L3** |
| `hse-session` (`session`) | извлечение сессии из Chrome (cookie копии профиля) → тот же авторизованный reading без пароля: расписание, эндпоинты, Moodle, storage_state | **работает, L3** |
| `cu-tasks` (`cu`, `tasks`) | ЦУ «Actual Student Tasks» на живой сессии Chrome: эндпоинты, которые грузят задания, 174 задания через API, safe GET, проба формы, storage_state; сверка с эталонным harness | **работает, L3** |
| action/write | POST/form-отправка по политике владельца, журнал, идемпотентность | не реализовано (план 4.1); сейчас одна безопасная GET-форма |
| code parser | код парсера пишется агентом и запускается в песочнице | не реализовано; в этих прогонах данных не парсили агентом |
| RAG answer | ответ по данным вуза с источниками и честностью «не знаю» | только L2 (прежние B4 в `bench/results/`); в L3 не входит |
| repair | починка после смены эндпоинта/вёрстки | L1 (`fakeuni`); в живой L3 не проверялся |

### `hse-login` — шаги (из `results.json`)

1. `hse.entry` — открыт `https://lk.hse.ru/`, редирект на Keycloak SSO.
2. `hse.login.form` — форма IdP (`username`, `password`, `credentialId`, `login`).
3. `hse.login.fill` — креды из broker'а введены в реальные поля (значения не логируются;
   в `results.json` только `fieldsFilled: 2`).
4. `hse.login.submit` → ожидание исхода: `authenticated` (вернулись на `lk.hse.ru`),
   либо точный барьер `mfa_required` / `captcha_required` / `invalid_credentials` /
   `account_locked` / `rate_limited` / `timeout`.
5. `hse.schedule.open` — `/schedule?begin_date=2026-10-26` (динамическая SPA).
6. `hse.schedule.parse` — разбор сетевых ответов `get-myschedule`.
7. `hse.schedule.api` — авторизованный GET `/api/get-myschedule?begin_date=…` изнутри
   страницы (Bearer из localStorage не покидает вкладку): `200`, 1 день, 1 пара.
8. `hse.api.discover` — эндпоинты из трафика (`get-user`, `get-myschedule`,
   `get-adfs-entry-point`, `get-footer`, `keycloak-auth`, …).
9. `hse.api.safe_get` — один безопасный GET (только чтение).
10. `hse.moodle.sso_click`/`hse.moodle.read` — единый вход в `edu.hse.ru` (Moodle):
    62 курса, `Мои курсы` без повторного пароля.
11. `hse.moodle.form_action` — одна безопасная **GET-форма**: поиск курса (слово
    «математика»), 20 результатов; никакой записи/заявки не отправлялось.
12. `hse.login.confirm` — подтверждение сессии: cookie-домены, localStorage-ключи
    (`access_token`, `id_token`, …; значения не сохраняются), storage_state 0600.

### `cu-tasks` — шаги

1. `cu.session.extract` — cookie ЦУ из копии профиля Chrome (живая сессия).
2. `cu.tasks.open` — `https://my.centraluniversity.ru/learn/tasks/actual-student-tasks`;
   динамический рендер (замер `renderWaitMs`).
3. `cu.api.discover` — 12 эндпоинтов, включая `/api/micro-lms/tasks/student`,
   `/api/account/me`, `/api/micro-lms/students/me`.
4. `cu.tasks.parse` — подсчёт заданий.
5. `cu.api.safe_get` — авторизованный GET `/api/micro-lms/tasks/student` → `200`,
   JSON-массив из **174** заданий (у каждого есть `deadline`).
6. `cu.form.probe` — ввод в поле поиска без отправки (форма найдена, значение набрано).
7. `cu.reference_harness` — та же сессия проверена эталонным внешним harness (skill
   `central-university-lms`): `sessionCheck.ready=true`, 2 курса, 3 дедлайна.
8. storage_state/har/network/console/screenshots 0600/0700 вне репозитория.

## 5. Метрики

`results.json` (вне репозитория) и санитизированная сводка (в репозитории) содержат:

| Метрика | Как считается |
|---|---|
| `status` сценария | `success` / `partial` / `blocked` (внешний барьер: MFA, капча, протухшая сессия…) / `failed` (ошибка нашего кода) |
| `durationSec` | от старта сценария до отчёта; по шагам — `steps[].durationSec` |
| `loginDurationSec` | от открытия entry-страницы до подтверждённой авторизации (не включая парсинг) |
| `steps` | имя шага, `ok`, краткое описание, время; значения секретов в описания не попадают |
| `barriers` | `code` + санитизированный `detail` + evidence-URL (например `mfa_required`) |
| `apiEndpointCount` / `apiEndpoints` | уникальные `method + host+path` из XHR/API-трафика (без query-значений) |
| `scheduleApiDays/Pairs/Records` | из авторизованного ответа `get-myschedule`: дни, пары (массивы `lessons`), записи |
| `timePairsInDom` | число время-подобных строк в DOM (начало/конец пары) — грубая проверка рендера |
| `taskRecords` / `tasksApiDeadlineLikeRecords` | задания из `/api/micro-lms/tasks/student` и число записей с полем дедлайна |
| `renderWaitMs`, `bodyTextLength`, `taskLinksInDom`, `dom` | сколько ждали динамический рендер и что реально было в DOM |
| `har.entries`, `har.sanitized` | сколько запросов записано и что HAR очищен |
| `credentials.registeredSecretValues` | сколько первичных значений зарегистрировано для redaction (сами значения не пишутся) |
| `secretSweep.filesRedacted` | сколько файлов финальный проход переписал (цель — 0) |
| `referenceHarness` | результат внешнего harness на том же storage_state: `ready`, курсы, дедлайны |

Сравнивать прогоны между собой корректно по `status`, `barriers`, времени шагов и
counts при одинаковых `begin_date`/дате. «Пары» и «задания» — величины, зависящие от
расписания/курсов владельца в момент прогона; это не абсолютный эталон.

## 6. Команды

Из корня репозитория (Python сам перезапустится через `backend/.venv`, где есть Playwright):

```bash
# что вообще есть и можно ли запускать (ничего не открывает, секреты не читает)
python -m bench.live.run --list
python -m bench.live.run --dry-run

# полный живой L3-прогон: вход по Keychain, сессия, ЦУ
python -m bench.live.run --scenario all

# только живой вход ВШЭ (критерий плана 0.4: без утечек в stdout/лог)
python -m bench.live.run --scenario login
# только чтение на сессии Chrome / только ЦУ
python -m bench.live.run --scenario session
python -m bench.live.run --scenario cu

# с видимым окном Chrome (диагностика) или на Playwright Chromium
python -m bench.live.run --scenario login --headed
python -m bench.live.run --scenario all --chromium
```

Обслуживание секретов (значения не печатаются):

```bash
python -m bench.live.secret_broker status    # есть ли элементы в Keychain
python -m bench.live.secret_broker migrate   # .env → Keychain + очистка .env (однократно)
python -m bench.live.secret_broker usage     # grep: не читает ли код HSE_MAIL/HSE_PASSWORD
python -m bench.live.secret_broker purge     # удалить элементы из Keychain
```

Альтернативная форма входа — модулем `run_live.py` (то же самое):

```bash
python bench/live/run_live.py --scenario all
```

### Где лежат результаты

```
~/.local/state/botai-v4-bench/2026-10-31/<run>/          # 0700, вне репозитория
├── results.json                                          # полный отчёт
├── sanitized/summary.json                                # копия для репозитория
└── raw/<scenario>/
    ├── network.har          # санитизированный HAR (cookie/auth/password/тела вырезаны)
    ├── network.json         # сетевой лог + найденные API-эндпоинты
    ├── console.json         # console/pageerror, отредактированные
    ├── storage-state.json   # cookie/localStorage живой сессии (0600; НЕ коммитить)
    └── screenshots/*.png    # экраны входа, расписания, Moodle, Actual Student Tasks
```

В репозиторий едет только `docs/v4/_evidence/benchmark/2026-10-31/live-benchmark-summary.json`
(статусы, шаги, counts, эндпоинты; без cookie/токенов/почты/имён и без абсолютных путей).

## 7. Измерения 2026-10-31 (реальные прогоны)

Машина: macOS 26.7 arm64, Python 3.13.15, настоящий Chrome `156.0.8078.74`
(headless `--headless=new`), локаль `ru-RU`, TZ `Europe/Moscow`. Креды — из Keychain
(в логах 2 зарегистрированных значения, ни одного вывода).

| Запуск | Сценарий | Статус | Время | Что видно в артефактах |
|---|---|---|---|---|
| `run-20261031-130223` | hse-session | success | 58.0 с | 24 cookie HSE, 25 эндпоинтов, расписание API 200, Moodle 62 курса, HAR 580 |
| `run-20261031-130347` | hse-login | success | 67.2 с | **логин 12.1 с**; 14 эндпоинтов; 62 курса; 20 результатов поиска; HAR 631; sweep вырезал 1 файл (HAR `postData.params` — баг исправлен) |
| `run-20261031-130602` | all | 3 success | 149.0 с | login 19.2 с / session / CU: 174 задания; sweep 0 |
| `run-20261031-130924` | all | 3 success | 162.5 с | login 19.1 с, session 73.3 с, CU 19.9 с; эталонный harness: ready, 2 курса, 3 дедлайна; sweep 0 |
| `run-20261031-131305` (финальный, после заморозки кода и линта) | all | **3 success** | **154.3 с** | **login 13.3 с**, session 64.3 с, CU 20.5 с; 14/26/12 эндпоинтов; 174 задания ЦУ; sweep 0; именно этот прогон в `_evidence/benchmark/2026-10-31/` |

Точные числа и шаги последнего прогона — в
[`_evidence/benchmark/2026-10-31/live-benchmark-summary.json`](_evidence/benchmark/2026-10-31/live-benchmark-summary.json)
и в `results.json` соответствующего запуска.

### Что сработало

* **Живой вход ВШЭ с паролем из Keychain** — без человека: Keycloak `saml.hse.ru`
  принял логин/пароль, вернул на `lk.hse.ru`; MFA и капчи на этом пути не встретились.
  12.1–19.2 с в четырёх прогонах (разброс — редиректы SSO и первый прогрев Chrome).
* **Извлечение сессии из Chrome** — 3343 cookie в профиле, 24 HSE + 30 CU живых;
  авторизованное чтение без пароля работает.
* **Единый вход ВШЭ** — после входа в `lk.hse.ru` Moodle `edu.hse.ru` открылся без
  повторного пароля (62 курса).
* **API discovery** — 14–26 уникальных эндпоинтов на ВШЭ, 12 в ЦУ; авторизованные GET
  `get-myschedule` и `/api/micro-lms/tasks/student` → `200`.
* **Динамическая страница** — расписание и Actual Student Tasks читаются; данные
  подтверждены авторизованным API (ЦУ: 174 задания, все с полем дедлайна).
* **Действия только на чтение** — один безопасный GET и одна GET-форма (поиск курса):
  никаких заявок/записей.
* **Артефакты и санитизация** — HAR/сеть/консоль/скриншоты/storage_state на месте;
  HAR без cookie, `Authorization`, полей логина и тел ответов; финальный sweep 0.

### Что не сработало или осталось барьером

* **Сохранённая сессия skill ЦУ протухла** (`401 reauth_required`, 2026-09-28) — не
  блокер: ЦУ читается на живой сессии Chrome; но это значит, что старый state нельзя
  считать доказательством.
* **DOM «Actual Student Tasks» рендерится лениво**: через 10 с в DOM ~1.1 тыс. символов
  и 3 ссылки на задания, хотя API отдаёт 174. Чтение пока API-first; «как видит
  человек» проверено экраном, а не полным списком DOM. То же у расписания ВШЭ (682
  символа, 2 времени) — SPA рисует данные из XHR.
* **HAR-утечка в первом прогоне**: Playwright пишет поля формы ещё и в
  `postData.params`; санитайзер их не покрывал, значения вырезал только финальный
  sweep. Исправлено в `redact.py` (params + финальный проход по JSON), повторные
  прогоны — 0 правок.
* **MFA/капча на живом пути не встретились** — ветки обработки и коды барьеров есть,
  но живой проверки этих веток нет.
* **Профиль Chrome — снимок**: если владелец выйдет из вуза, `hse-session`/`cu-tasks`
  начнут падать в `blocked` с `hse_session_stale`/`cu_session_stale`. Это штатный
  точный барьер, а не ошибка бенчмарка.
* **Write-действий нет** — сценарий action/write не реализован (план 4.1).
* **LLM в L3-прогонах не участвовала** — бенчмарк измеряет инфраструктуру harness,
  а не качество агента.

## 8. Матрица сравнения с эталонным внешним harness

### 8.1. Измерено на одном и том же материале (2026-10-31)

| Проверка | Внешний harness (skill `central-university-lms`, Playwright/headless) | BotAI bench pipeline (`bench/live`) |
|---|---|---|
| Сессия ЦУ | `session-check` на своей сохранённой сессии → `reauth_required`; на **нашей** выгруженной сессии → `ready`, `publishedCourseCount=2` | `authenticated=true`, 12 API-эндпоинтов, `/api/micro-lms/tasks/student` 200 |
| Задания/дедлайны ЦУ | `list-courses` = 2, `deadlines` = 3 | `taskRecords=174` (все с `deadline`) |
| ВШЭ: вход по паролю | компонента нет (только CU) — не применимо | **success**, 12.1–19.1 с |
| ВШЭ: чтение сессии | не применимо | расписание API 200, 62 курса Moodle |
| API discovery | известные пути skill'а, без инвентаризации трафика | 14–26 эндпоинтов из трафика |
| Артефакты | storage state 0600 | HAR/network/console/screenshots/storage_state + `results.json` |
| Секретный ввод | `login` — headed, вводит человек | Keychain → код → форма, без человека и без вывода |

Вывод: на общей части (чтение ЦУ по одной сессии) оба harness видят одни и те же
данные; BotAI bench дополнительно умеет живой вход ВШЭ, извлечение сессии из Chrome и
инвентаризацию API. Сравнение честное только для ЦУ-чтения; на ВШЭ эталонного
внешнего harness в этом прогоне не было.

### 8.2. Целевая матрица «не хуже внешнего harness» (план 4.3)

Заполняется, когда появится v4-агент; сейчас измерены только примитивы.

| Сценарий | Внешний harness (Codex/Claude Code/ручной скрипт) | BotAI v4 агент | Метрики |
|---|---|---|---|
| login | эталон: владелец вводит пароль | bench: Keychain → форма (без человека) | успех, время логина, барьеры |
| session extraction | cookie из профиля/CDP | **сделано** (3343 cookie, live session) | успех, число cookie/доменов |
| API discovery | чтение трафика | **сделано** (14–26 эндпоинтов) | число эндпоинтов, покрытие данных |
| dynamic page | чтение SPA | **частично** (API-first; DOM ленивый) | counts, `renderWaitMs` |
| action (read-only) | GET/форма | **сделано** (safe GET + GET-форма) | статус, отсутствие записи |
| action (write) | POST по политике | не реализовано | — |
| code parser | пишет и запускает код | не реализовано | — |
| RAG answer | ответ по данным с источниками | только L2 (B4) | точность/честность/источники |
| артефакты/память | файлы/логи сессии | HAR/сессии/скриншоты/сети + RAG/память (план) | полнота следов |

Цель пивота — не хуже по успеху и времени, лучше по удобству/памяти/RAG. Пока
измеримая часть — «не хуже по примитивам»; сравнение качества агента впереди.

## 9. Ограничения доказательства

1. **Один владелец, один день, одна машина.** Выборка мала: 4 полных запуска
   31.10.2026; login измерен 3 раза (12.1 / 19.2 / 19.1 с) — медианы нет.
2. **Только ВШЭ (SmartPoint/ЕЛК + Moodle) и ЦУ.** Другие вузы, Moodle-WS, iCal,
   почта, документы не проверялись.
3. **Headless `--headless=new`**, а не видимое окно: отпечаток ближе к настоящему
   Chrome, но не идентичен headed-браузеру; прохождение антибота не заявляется.
4. **LLM вне контура L3.** Динамика «агент сам решает, куда кликать» не измерена;
   `bench/live` — детерминированный harness, не агент.
5. **Счётчики зависят от данных владельца.** «1 пара в неделе 26.10» — расписание
   владельца в момент снимка; в трафике SPA видели 2 окна (4 записи день+пара).
   Повторный прогон может дать другое число.
6. **DOM-чтение ограничено**: SPA-страницы рендерят не весь список за отведённое
   время; авторитетны API-ответы, DOM — дополнительная проверка.
7. **Сравнение с эталоном в п. 8.1** — на сессии, выгруженной самим бенчмарком;
   независимость сессий ограничена.
8. **MFA/капча/write не проверены живьём** — только ветки кода и барьер-коды.
9. **Сырые артефакты не коммитятся** и не являются доказательством для CI: CI не
   получает кредов и в живые вузы не ходит. Доказательство — этот документ + сводка
   в `_evidence/` + `results.json` вне репозитория на машине владельца.
10. **Профиль Chrome**: бенчмарк копирует профиль, пока Chrome запущен; файлы cookie
    могут быть на границе записи. Копия удаляется после сценария.

## 10. Как перезапускать и что смотреть

1. `python -m bench.live.secret_broker status` — элементы Keychain на месте.
2. `python -m bench.live.run --dry-run` — Chrome/Playwright/предпосылки.
3. Владелец: держать Chrome залогиненным в ЦУ (для `cu-tasks`) и в ВШЭ (для
   `hse-session`); пароль ВШЭ в Keychain (для `hse-login`).
4. `python -m bench.live.run --scenario all`; смотреть статусы и `barriers`.
5. Проверка следов: `results.json` → `raw/<scenario>/network.json` (эндпоинты),
   `network.har` (трафик), `screenshots/` (экраны), `storage-state.json` (сессия,
   только для проверки владельцем). Ничего из `raw/` не копировать в git.
6. Повтор после смены вёрстки/сессии — те же команды; сравнить `barriers` и counts.

Типовые барьеры: `hse_session_missing`/`hse_session_stale` (выйти/войти в Chrome),
`cu_session_missing`/`cu_session_stale`, `mfa_required`, `captcha_required`,
`login_rejected`, `safe_get_failed`, `schedule_page_failed`.

## 11. Что осталось (честно)

* L0-тесты самого бенчмарка (redaction/HAR/counts) как обычные unit-тесты и в CI.
* L3-agent: тот же набор задач, но агентом v4 (модель решает и действует);
  сравнение с Codex/Claude Code по успеху/времени/шагам/токенам (план 4.3).
* action/write с политикой и подтверждением; code-parser; RAG-ответ с судьёй.
* Повторы и медианы (3+ на ячейку), хранение истории прогонов и регресс по датам.
* Проверка веток MFA/капчи на живом сайте (если появятся) и документирование
  точного барьера как результата.
* Замена `bench/live`-браузерного слоя на runtime агента v4, когда он появится;
  секреты и артефакты переиспользуются как есть.
