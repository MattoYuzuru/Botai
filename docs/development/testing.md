# Тесты и работа с ключом модели

Статус: **действующий порядок проверок**, обновлено 2026-10-29. Правила для всех, кто пишет код в этом репозитории,
включая агентов-исполнителей.

## Уровни тестов

| Уровень | Что проверяет | Сеть | Ключ модели | Где запускается | Кто запускает |
|---|---|---|---|---|---|
| **L0** — юнит | Чистая логика: разбор рецепта, нормализация, даты, lint, токены дизайна (`pytest`, `vitest`) | Нет | Нет | Локально, CI | Все |
| **L1** — интеграция на фикстурах | API + Postgres + воркер + Chromium против синтетического вуза `fakeuni` на loopback; LLM — `ScriptedLlm` с записанными ответами | Только loopback | Нет (`LLM_PROVIDER=mock`) | Локально, CI | Все |
| **L2** — живая модель | Что агент действительно справляется: черновик рецепта по записанному трафику фикстуры, классификация вопросов, честность ответа, отказ на инъекцию | Только API модели | `DEEPSEEK_API_KEY` | Только локально | Владелец или агент по явной просьбе |
| **L3** — живой вуз | Настоящий вход в ВШЭ/ЦУ, настоящее обучение рецепта, настоящий sync | Вуз + модель | Да | Production после сброса аккаунта владельца | Агент-оператор по поручению владельца (только чтение); пароль и коды вводит **только владелец** |

Сначала поведение проверяется на L0–L1. L2 разрешён только с явным опт-ином и бюджетом;
его может выполнять агент по разрешению владельца. С 30.10.2026 L3-сценарии по поручению владельца
проводит агент-оператор через браузер владельца (только чтение во вузе); ввод пароля вуза и кодов
подтверждения всегда делает сам владелец в защищённых полях Ботай. Перед прогоном данные аккаунта
сбрасываются `python -m botai.ops.reset_account` (см. [deployment](../plan/deployment.md)).
Результат одного уровня не заменяет другой; качество живой модели и успешное
подключение настоящего вуза не следуют из фикстурного теста.

Для текущей работы полный прогон выполняется в GitHub CI по просьбе владельца.
Локально допустимы только ограниченные проверки затронутых сценариев. Это правило
имеет приоритет над старым требованием запускать полный `make check` в каждом worktree.
Общий Postgres и тяжёлые проверки координируются через `workstation-dev`; retained
тома не сбрасываются. Тесты сами создают и удаляют отдельные БД.

## Ключ DeepSeek

- Ключ лежит в `.env` в корне репозитория как `DEEPSEEK_API_KEY`. Файл в
  `.gitignore`; шаблон без значений — [`.env.example`](../../.env.example).
- **При разработке ключ можно использовать** для локального запуска приложения
  и для тестов уровня L2.
- L0 и L1 ключ **не читают**. Если тест L0/L1 падает без ключа — это ошибка
  теста, а не окружения.
- CI ключа не получает. Ни один job в CI не ходит во внешнюю модель.
- L2 запускается только с явным опт-ином: `BOTAI_LIVE_LLM=1`. Без него тесты
  L2 помечаются `skipped` с причиной, а не тихо проходят.
- У прогона L2 есть потолок расхода `BOTAI_LIVE_LLM_BUDGET_USD` (по умолчанию
  $0.50). Счётчик токенов ведётся по ответам API; превышение роняет прогон.
- Ключ никогда не печатается: ни в логах, ни в выводе тестов, ни в описании PR,
  ни в сообщениях агента. Для проверки наличия — `envpeek` (маскирует значение).
- Если ключ случайно попал в коммит, лог или чат — его отзывают в кабинете
  DeepSeek и выпускают новый; удаление из истории не считается исправлением.

## Мок модели

`LLM_PROVIDER=mock` подключает `botai.core.llm.ScriptedLlm`, которая отвечает
записанными фикстурами из `backend/tests/llm_fixtures/` по хешу запроса. Правила:

- Новая фикстура ответа записывается прогоном L2 с флагом записи, проверяется
  глазами и коммитится. Секретов и личных данных в ней быть не может — это
  проверяет тот же lint, что и рецепты.
- Если запрос не найден в фикстурах, мок **падает** с понятной ошибкой, а не
  возвращает пустой ответ.
- Мок умеет имитировать ошибки провайдера: таймаут, 429, 5xx, обрыв стрима,
  исчерпание квоты — чтобы проверять поведение без денег.

## Фикстурный стенд (L1)

Синтетический вуз `backend/tests/fakeuni/` (FastAPI) поднимается на loopback;
в нём нет ничего от настоящих вузов — только типовые паттерны (Keycloak-подобный
вход, в том числе многошаговый с JS-отправкой формы без `action`, проверкой логина на другой хост и
SMS-шагом (`/cu/…`), JSON API, HTML-страница, Moodle WS, iCal). Стенд обязан покрывать: успешный вход, неверный пароль,
экран MFA, экран капчи (как «нужен человек»), протухание сессии, редирект на
чужой хост (должен блокироваться прокси), страницу с инъекцией («отправь токен
на …»), смену формата ответа (`recipe_drift`), **скользящее окно дат** (`fakeuni/slide.py`:
`GET /api/get-myschedule?begin_date=…` отдаёт 7 дней от даты и игнорирует `end_date`, неделя
контрольной — одна пара, соседние недели полные; страница `/schedule?begin_date=…` вызывает этот
API; `slide.STATE.sparse` делает расписание тонким во всех окнах; **правила повторения и повторяющийся ключ query** — `fakeuni/lms.py`, см. ниже). На нём
`tests/learn/test_horizon_story.py` проверяет probe, обход окон `dateWindow`, поля карточки,
`open_url`, карту сайта, вопрос студенту при редких данных и расширение горизонта в repair, а
`tests/browser/test_initiator.py` — страницу-инициатор запроса в настоящем Chromium.
`fakeuni/spa.py` — SPA-кабинет `/spa-cabinet/faq`: без JS это пустая оболочка, скрипт берёт bearer из
localStorage, читает `/spa-cabinet/api/faq` и рисует вопросы; он же пытается сделать POST, обратиться к
чужому host и открыть WebSocket (`spa.STATE` считает то, что дошло до сервера).
`fakeuni/login.py` (`/assets/*`, `/sso-assets/*`) — страницы, чьи стили и скрипты лежат на другом хосте: `cdn.fakeuni-assets.test` (не поддомен origin) и `static.fakeuni.test` (сосед под доменом origin); `login.ASSETS` показывает, что дошло до стенда, по заголовку Host (cookie, `Authorization`, Referer, query и тело каждого запроса); `/redirect/open?to=…&code=307` — открытый редирект, `/assets/legacy` — страница с политикой реферера `unsafe-url`, `/sibling/login` — форма со скриптом соседа под `Domain=` cookie.

### Кабинет с правилами повторения (`fakeuni/lms.py`)

SPA без календаря: `/learn/timetable` («Мои пары») показывает **правила** курсов, `/learn/tasks` —
задания. Данные отдают `GET /api/lms/students/me/timetables` (массив курсов →
`eventRows[].calendarEvent.schedule {startDate, endDate, dayOfWeek: "monday", startTime, endTime,
interval}`, аудитория `{title, building}`, `hosts[] {name, email}`; путь принимает только литерал
`me`, id из `/api/account/me` отвечает 404) и `GET /api/lms/tasks/student?state=…&state=…`
(**повторяющийся ключ**; запрос с частью значений получает только эти задания). Cookie-сессия, без
bearer; названия, аудитории и люди выдуманы. `lms.STATE` показывает, какие сегменты и значения
`state` спрашивали, и меняет правила (`remove_rule`, `add_rule`).

| Что | Где |
|---|---|
| Путь целиком: настоящий Chromium видит повторяющийся ключ и правила → автор → canary → sync → домен | `tests/learn/test_lms_browser.py` (модель скриптована, `-m browser`) |
| Тот же путь без Chromium: рецепт (weekly, `intervalWeeks` из поля, ключ-список), 36 занятий с карточкой без почты, статусы заданий, названия курсов; ошибки автора из инцидента (один `state` вместо пяти, `{accountId}` вместо `me`) возвращаются словами; удалённое правило снимает будущие занятия, добавленное появляется, сдвиг времени и интервала; слова для студента (что видела, чем остановилась, почему не вышло, что можно сделать) | `tests/learn/test_lms_story.py` (записанные ответы автора L2, `ScriptedLlm`-очередь) |
| Названия дней, интервал из источника, список ключа в compiler/lint/schema/typed-модели, probe не подменяет повторяющийся ключ, статус-фильтр query не «личное значение», а логин в camelCase — личное; слово `me` внутри сегмента и в значении query | `tests/sources/test_recipe_expressions.py`, `tests/learn/test_auth_bindings.py`, `test_data_lint.py`, `test_factory_compile.py`, `test_probe.py`, `tests/core/test_recipe_v2_contract.py` (+ `contracts/examples/*/weekly-rules-recipe.json`) |
| Экземпляры правила: скользящий горизонт не снимает то, мимо чего скользнул; сокращённый `horizon.days` не снимает дальние; внутри окна исчезнувший день снимается; неполный и «неверимый» sync ничего не снимает | `tests/sources/test_occurrences.py` (настоящий адаптер + normalizer на стенде), `test_normalize.py`, `test_lms_story.py` |
| Статусы заданий по целым словам: `notEvaluated`, `pendingEvaluation`, `awaiting_evaluation`, `reviewed`, `preview`, `failed` | `tests/sources/test_normalize.py` |
| Промпты, подсказки и описания схемы не содержат ключей стенда (`eventRows`, `calendarEvent`, `hosts`...): пример в них был бы ответом | `tests/learn/test_recipe_rules_prompt.py` |
| Фразы для студента: у каждого кода canary/compiler есть своя | `tests/learn/test_words.py` |

**L2 на этом стенде** — `tests/learn/test_lms_live.py` (`live_llm` + `browser`, без `BOTAI_LIVE_LLM=1`
пропускается): настоящий Chromium, EgressProxy, настоящая модель, планировщик и автор. Команда
(из `backend/`, ключ из env-файла не печатается):

~~~bash
DATABASE_URL=postgresql+asyncpg://botai:botai@127.0.0.1:55432/botai \
BOTAI_ENV_FILE=/путь/к/env-файлу BOTAI_LIVE_LLM=1 BOTAI_LIVE_LLM_BUDGET_USD=0.4 \
BOTAI_LIVE_RECORD_DIR=/куда/сложить/обмены \
  uv run --frozen pytest tests/learn/test_lms_live.py -s
~~~

`BOTAI_LIVE_RECORD_DIR` сохраняет каждый обмен (сообщения запроса и ответ) для сборки
`ScriptedLlm`-очередей; тест печатает кандидатов, находки lint/canary и блоки студенту. Прогоны
30.10.2026 (DeepSeek, профили из настроек): до исправлений провал воспроизвёлся (день недели словом
→ `fields`, один `state` вместо пяти → `evidence_query_mismatch`, затем `observed.value` на
`inProgress`; автор собрал `startsAt` из `startDate` и получил 5 записей из сентября); после —
расписание 36 занятий, курсы 3, задания 5, дедлайны 5, преподаватели 5. Восемь прогонов, по 11–13
вызовов модели, **≈ $0.17 суммарно** (счётчик `MeteredLlm` по ответам API; $0.013–0.034 на прогон,
лимит задачи $1.5). Вид «аудитории» из этого ответа не собирается (в онлайн-занятии нет аудитории,
а пропущенное обязательное поле отклоняет весь ответ), студенту это объясняется словами. Живой
кабинет настоящего вуза этим не проверен.

## Сквозной тест бэкенда (L1, `backend/tests/e2e/`)

Один процесс, настоящие компоненты, подменены только вуз (`fakeuni` по HTTPS на loopback) и
модель (`ScriptedLlm`): API, раннер заданий на Postgres с таблицей обработчиков воркера
(`botai.worker.handler_table()`), Chromium (`PlaywrightGateway`) и `LoginFlow`, `EgressProxy`
отдельным процессом (`python -m botai.browser.proxy`, токены по `BOTAI_PROXY_SECRET`, как в
compose), Explorer, sync, repair, домен и чат. Студента играет `Student`: идёт по SSE-потоку
задания и отвечает на блоки через `POST /api/v1/jobs/{id}/answers`.

Сценарии: приглашение → подключение (логин, секретные пароль и код, подтверждение) → расписание и
«Сегодня» с происхождением → перевход по паролю из Vault (с кодом и без) → sync по планировщику →
ответ чата с источником; сломавшийся адрес → repair без вопросов; второй студент того же вуза без
модели; опечатка в пароле; студент ушёл и остановил задание; недоступный вуз и повтор раннера. На
всех сценариях проверка C1 ([security.md](../architecture/security.md) §8): маркеры
`P@ssw0rd-FIXTURE`, `424242`, опечатка и cookie сессий не встречаются ни в одной таблице, ни в
логах и выводе процесса, ни в запросах к модели; в `secrets` только шифртекст.

Запуск: `cd backend && uv run pytest tests/e2e` (нужны Postgres — `make db` — и Chromium Playwright;
≈ 45 с для исходного набора). Полный проход запасного экрана по WebSocket, живую
модель, настоящие вузы, Moodle/iCal/документы через раннер, `make dev`-запуск процессов
(`uvicorn`, `python -m botai.worker`) по отдельности этот набор не подтверждает.

## Приёмка factory v2 (снимок 29.10.2026)

Foundation длительного исследования принят отдельно (PR #68): main
79c258064931dc6195980366dca4141ef4952690,
[CI 40912340821](https://github.com/MattoYuzuru/Botai/actions/runs/40912340821) и
[deploy 40914887383](https://github.com/MattoYuzuru/Botai/actions/runs/40914887383) успешны.
Операторская сверка сообщила этот revision у api/worker/proxy и schema e4a91b7c603d;
это evidence foundation, не factory v2. Контрактный PR #69 принят отдельно на 8d62fd5.
Auth/data/browser компоненты v2 имеют ограниченные локальные L0/L1-проверки и сквозные
фикстуры. Допуск полного CI и production-приёмка определяются для конкретного SHA.
Наличие test file, merge контракта или зелёный foundation run не подтверждают новый binary.
Числа/ссылки окончательного прогона привязываются к полному SHA в отчёте PR/выпуска.

| Граница | Synthetic проверки |
|---|---|
| V1 strict / v2 schema и compiler | tests/core/test_recipe_v2_contract.py, tests/learn/test_factory_compile.py: current request/browser refs, запрет поддельного execution payload, producer off, перенос dynamic date template |
| Per-request Cookie+Bearer и origin scope | tests/learn/test_auth_bindings.py, tests/browser/test_scoped_auth.py, tests/sources/test_session_bindings.py: observed proof, отказ чужого audience/redirect, собственные credentials |
| Nested/weekly data | tests/sources/test_recipe_expressions.py, test_recipe_data_v2.py: parent cardinality pagination, дата+время, DST gap/fold, recurrence parity/исключения/горизонт/стабильные ID, whole-endpoint overflow |
| Recursive lint | tests/learn/test_data_lint.py: alias/TZ до сети, required fields weekly, вложенные секретные pointers/attrs/regex/literals, v1 без ослаблений |
| Native browser replay | tests/browser/test_read_capabilities.py: дата/select/tab/frame/next, новый handle, late target/effect, POST/WS, timeout/cycle/cap, settled/explicit empty |
| Browser plan от автора до sync | tests/learn/test_browser_plan_story.py: настоящий native date → observed plan ref → canary/activation → новая дата в свежем Chromium без модели, стабильные ID и запрет POST |
| Личный acquisition context | tests/browser/test_private_entry.py и tests/e2e/test_factory_v2.py: только Vault, same-origin, новая явная ссылка, отзыв cookie → обычный перевход/новый bearer без модели, отдельные студенты и отсутствие секретов в открытых данных |
| Typed canary и correction | tests/learn/test_canary_findings.py и test_canary_recovery.py: static findings без exception text, следующие candidates, bounded авторское исправление |
| TTL и budget | tests/jobs/test_research_store.py и tests/learn/test_research_progress.py: атомарная usage projection, CAS/tenant/fence, сохранение cumulative расхода/reservations после TTL |
| Полный v2 путь | tests/e2e/test_factory_v2.py: Worker/API/Chromium/proxy/LoginFlow, наблюдаемый Cookie+Bearer, nested schedule в домене, fresh runtime sync без LLM, стабильные ID при изменении аудитории, второй студент со своей сессией, отсутствие plaintext credentials |
| Shutdown/resume и поздний результат | tests/e2e/test_research_resume.py, tests/jobs/test_source_fencing.py: proposal до canary, новый claim/браузер, activation receipt, изменившийся intent/spec, отказ поздней записи |

Сквозные v2 fixtures включены в интеграционную приёмку; полный итог CI проверяется на том же
SHA перед merge. Ни одна строка таблицы не заявляет L2/L3.
Доказательство выпуска требует полного CI того же SHA, health/revision/migration evidence,
затем отдельной проверки владельцем настоящего источника. Producer по умолчанию выключен;
совместимость reader и rollback — [deployment](../plan/deployment.md).

## Что требуется от PR

### Приёмка источников и публичного поиска

| Граница | Проверка |
|---|---|
| Общая просьба и явное ограничение видов | `tests/e2e/test_adaptive_flow.py`: сообщение CONNECT с глубокой ссылкой, automatic-kinds / explicit-schedule |
| Раздел после первых ссылок, SPA без смены URL | Тот же E2E: десятая ссылка → курс → проверенная вкладка; настоящий DOM и сетевые ответы после действия |
| Отложенные данные вкладки и ошибки API | `tests/browser/test_read_views.py`: смена aria/class до отложенного GET; `tests/learn/test_network_health.py`: 401/503, восстановление 200 и незавершённый повтор в обоих наблюдателях |
| Старый расширенный DOM и обновлённый ответ | E2E и `tests/learn/test_discovery.py`: ссылки на снимки не смешиваются, новый ответ инвалидирует старое раскрытие; неизменённая история не даёт прогресса |
| Сессия браузера после рестарта | E2E: cookies + localStorage/sessionStorage → настоящий Vault → новый Chromium; API-проба 200 без storage не подтверждает страницу браузера |
| Отражённые секреты сессии | `tests/learn/test_session_redaction.py`: cookies, bearer и вложенные storage-значения не попадают в planner/author, в том числе после refresh/reauth; ошибка экспорта останавливает наблюдение до модели |
| MFA, потеря сессии, повторный вход | `tests/e2e/test_full_flow.py` и `tests/browser/test_login.py`: свой пароль из Vault, человек вводит код, sync не вызывает модель |
| Гостевая API-проба и исходная страница входа | `tests/browser/test_login_entry.py`, `tests/learn/test_login_entry.py`: guest200/bare401 не заменяют вход; path/query и более глубокий отказ передаются LoginFlow, приватный путь не попадает в общий пресет |
| Ручной вход из чата и очередь | `tests/agent/test_chat_flow.py`, `tests/jobs/test_queue.py`, `tests/jobs/test_conversations.py`: owned ID, CONNECT в том же разговоре, сохранение intent/scope, дедупликация и порядок блокировок |
| Чат → защищённый экран → прежний рецепт | `tests/e2e/test_chat_manual_login.py`: настоящий worker/Chromium/proxy/Vault, WS и «Готово», исходный query, сохранённые recipe/scope, расписание через API; модель вызывается только обычным CHAT |
| Drift после ручного входа | `tests/jobs/test_manual_drift.py`: настоящий PgJobQueue, починка внутри CONNECT для готового/повторно используемого/нового рецепта; ошибка и отмена сохраняют строки/область и оставляют источник `stale` с повторной попыткой |
| Подключение в общем чате (один тред — один таймлайн) | `apps/web/e2e/connect.spec.ts`: задача в том же треде, вопрос входа у поля ввода, секрет через `jobs/answers` и нигде не сохраняется, неверный пароль, сбой и «Войти заново», отмена; `chat-tasks.test.tsx`, `timeline.test.ts`: серверный порядок, блок один до и после сохранения, план обновляется на месте |
| Чат на весь экран, панели, вложения | `apps/web/e2e/chat.spec.ts` (ноутбук 1280×800, телефон 390×844): шапка и кнопки на линии знака рейла, поле у низа с отступом 16–24 px, страница прокручивается целиком, список чатов (непрочитанное, «ждёт вас»), тост «Ботай просит войти», вложения и drag-and-drop; `chat-voice.spec.ts`: настоящая запись MediaRecorder с поддельным микрофоном; `chat-attachments.test.tsx`: загрузка, чипы, вьюер, запись на моке MediaRecorder |
| CAPTCHA | E2E: реальный ScreenRegistry, `waiting_user`, никакой отправки формы/решения, отмена закрывает экран и удаляет временные секреты; сами кадры/ввод — `tests/browser/test_screen.py` |
| Canary, сохранение, повторное исполнение и экраны | E2E: активная ревизия, `/schedule`, `/today`, `/courses`, происхождение, стабильные ID и следующий sync без модели |
| Персональная область общей спецификации | `tests/learn/test_explorer.py`, `tests/sources/test_scope.py`: shared recipe, новый более широкий рецепт, ограниченный fetch/canary, повторная проверка при записи |
| Миграция и откат схемы | `tests/db/test_data_scope_migration.py`: существующие строки, nullable scope, downgrade/re-upgrade только во временной БД |
| Exa → DeepSeek, 429, кредиты, timeout, оба отказали | `tests/search/test_search.py`: транспорт мокается, ключи не нужны; retries/cooldown/deadline и лимиты проверяются отдельно |
| Файлы страницы с чужих хостов (CDN, статика IdP): страница рисуется и «Далее» работает; XHR/fetch/POST/beacon/WebSocket/воркеры к хосту-активу и навигация на него не доходят до сервера (навигация даёт прежний `egress_blocked`); cookie, `Authorization` и `Referer` с query не уходят; сосед по домену под `Domain=` cookie отклоняется, а вход задаёт студенту вопрос о нём (согласие делает его хостом подключения, отказ не останавливает вход); лимит хостов; IP-литерал; сессия, `allowHosts` и пресет без хоста-актива, хосты-активы только в `SessionState.asset_hosts`; чтение по расписанию — только они, чтение агента — никаких; нет `SharedWorker`; вход через согласованный SSO-хост, чья кнопка включается скриптом с CDN (инцидент); серверный редирект на не поручённый хост по-прежнему отклоняет прокси; старый прокси (405 на `ARM`) оставляет страницу как была; диагностика только хешами, в том числе через настоящий закрытый словарь | `tests/browser/test_assets.py`, `test_assets_modes.py` (настоящий Chromium против `fakeuni/login.py` `/assets/*`, `/sso-assets/*`, `/sibling/*`: хосты `cdn.fakeuni-assets.test`, `static.fakeuni.test`), `tests/browser/test_assets_gate.py` (шаблоны URL, тип/метод/заголовок, `Referer`, cookie по домену, схема и документ-источник) |
| Редирект на поручённый хост (разбор безопасности): серверный редирект и iframe ставят документ на хост, отдавший ассет, — это не страница подключения: ничего не вводится (ни из Vault, ни из нативных полей), запросы из него отклонены даже без увода страницы, поток останавливается, студенту задаётся вопрос о хосте; открытый редирект XHR/fetch и `307 POST` на поручённый хост останавливают поток; ассет, перенаправленный на поручённый хост, тревоги не даёт | `tests/browser/test_assets_redirect.py` (+ `fakeuni` `/redirect/open`, `/assets/arm.js`, `/assets/x.png`) |
| Файлы страницы на уровне прокси: лимит поручённых хостов и параллельные `ARM`, приватный/loopback/CGNAT/смешанный ответ DNS у хоста-актива, пиннинг при перепривязке DNS, не поручённый хост (цель редиректа) отклонён до DNS, токены API-клиентов без разрешения, подделка/просрочка/отзыв, внешний прокси по сети и недоступный прокси, журнал без хоста и токена | `tests/browser/test_proxy_assets.py` (+ существующие `test_proxy.py`, `test_proxy_hardening.py`, `test_egress.py`) |
| Чтение SPA-кабинета агентом | `tests/agent/test_tools_web_spa.py` и `test_tools_web_spa_edges.py` (fake-браузер: сессия, лимит 3 за ход, занятый браузер, `needs_login` и его ложные случаи, спиннер, чтение текста сервера без браузера, инвентарь значений браузера и границы вырезания, hash-маршруты, другой хост и смена пароля, общий дедлайн, отбор данных страницы, карта сайта); `tests/agent/test_tools_web_spa_browser.py` (настоящий Chromium против `fakeuni/spa.py`: пустая оболочка → FAQ из Bearer API, cookie, выданный по ходу рендера, hash-страница, POST/WS/GET-действие/чужой host не уходят, забытая сессия, занятый браузер); `tests/browser/test_agent_read.py` (чтение агента уступает входу и исследованию, не вытесняет их, отказывает GET-действию) |
| Поиск в настоящем цикле и недоверенная выдача | `tests/agent/test_public_search_flow.py`: харнес, цитаты, затраты и injection audit; CONNECT-кандидаты — `tests/learn/test_public_search.py` |
| Диктовка: провайдер, эндпоинт, квота, ключ и звук не утекают | `backend/tests/speech`: Groq на подменённом HTTP-транспорте (запрос, повтор, 429, 4xx, редирект, тишина), эндпоинт (формат, размер, длина, выключено, квота, отсутствие записи в БД, ключ в ответах и журнале); живых вызовов в CI нет. Frontend: `dictation.test.tsx` (эквалайзер следует за громкостью, «Готово», отмена, ошибки), `chat-attachments.test.tsx` (MSW `/speech/transcribe`), `e2e/chat-voice.spec.ts` (поддельный микрофон Chromium). [speech-to-text.md](speech-to-text.md) |
| Сброс аккаунта и старая очередь клиента | `tests/auth/test_data_reset.py`, frontend `data-reset.test.ts`, `outbox.test.ts`, `sw/core.test.ts`: owner scope, live identity, in-flight ответы, соседние вкладки |
| Диагностика и выпуск | `tests/ops/test_deploy_diagnostics.py` и `tests/*/test_diagnostics.py`: keep/on/off, отказ/откат, identity образа и шифрованная точка восстановления |

Это карта сценариев, а не утверждение, что новый выпуск уже зелёный. Главный прогон
выполняет `.github/workflows/ci.yml`: backend lint/format/pyright/pytest с Postgres и
Chromium, web lint/types/tests/build, PWA Playwright, генерация контрактов и guards.
L2 без явного опт-ина пропускается с причиной; эти skips не являются проверкой живой модели.

Для конкретного выпуска доказательствами служат зелёный run `ci` того же полного SHA,
run `deploy` и операторская сверка `org.opencontainers.image.revision` всех трёх сервисов.
Ссылки на эти runs, результаты production-smoke и адресной очистки сохраняются в описании
PR и отчёте выпуска. [Текущие запуски CI](https://github.com/MattoYuzuru/Botai/actions/workflows/ci.yml)
и [выкладки](https://github.com/MattoYuzuru/Botai/actions/workflows/deploy.yml) не заменяют
сверку SHA: новый commit или старый зелёный run сами по себе production не подтверждают.

Живой проход настоящего вуза остаётся **не проверен** разработчиком. Подготовка для
владельца: включить `diagnostics=on`, открыть пустой аккаунт на новом выпуске, написать
«Подключи вуз, вот ссылка на мои курсы», пройти вход/MFA/CAPTCHA лично, дождаться canary,
подтвердить полученные данные, проверить календарь и повторный sync. Передать оператору
ссылку на чат и время, без учётных данных; затем выключить диагностику.

Ограниченный live-smoke публичного поиска разрешён отдельно от L2 модели: фиксированный
публичный query, ключи только на сервере/из `.env`, никакого закрытого кабинета. Команды
и границы доказательства — [public-search.md](public-search.md). Обычный CI не получает
поисковых секретов и не вызывает реальные поисковые API.

### Диалоговое подключение

Подключение начинается в любом треде: агент вызывает `connect_source`, задача CONNECT получает
`thread_id` этого треда (`tests/jobs/test_connect_source.py`). Проверки `tests/jobs/test_conversations.py` покрывают durable inbox,
идемпотентность, восстановление истории, защищённые ответы и переход к отдельному источнику другого
origin. `tests/learn/test_conversation.py` проверяет свободные уточнения и отсутствие необязательных
видов данных. `tests/learn/test_rendered_story.py` использует настоящий Chromium и синтетическую
SPA: исходный query, глубокий DOM, canary и повторный sync без модели. Серверный запрет POST/WS и
сохранение активного ручного входа проверяются в `tests/browser/test_gateway.py`.

### Календарь по ссылке

Личная ссылка календаря — секрет (C18, [security.md](../architecture/security.md) §4.3). `contracts/calendar-link-vectors.json`
— один файл случаев (`secret`, `ordinary`, `without`) для обеих реализаций правил: его читают `tests/core/test_calendar_link.py`
(L0) и `calendar-link.test.ts` (Vitest), поэтому сервер и клиент не расходятся. L0 проверяет, какие сообщения несут ссылку
экспорта (`webcal://`, `.ics`, `/export/ics`, `/ical/`, публикации iCloud и Outlook, ключ в query или в пути, без `https://`,
с невидимыми символами) и какие нет (публичный `.ics` без ключа, страницы календарных сервисов, разговор о календарях),
нормализацию (только порт схемы по умолчанию, IDN, сокращённые IP, `user:pass@`) и отказы без повтора ссылки.
`tests/sources/test_ical_series.py` (L0): серия занятий с концом датой, локальным временем и в UTC, весь день и «плавающее»
время, пропущенная неделя, перенесённое занятие, `EXDATE;VALUE=DATE` у серии со временем и сохранение местного часа при
переходе на зимнее время. `tests/jobs/test_calendar_link.py` (L1, fakeuni): инструмент `connect_calendar` (один календарь на вуз,
дедуп, замена, изоляция пользователей, `request_login`/`request_repair`/`sync_source` без браузера), вся история через агента
(поле → Vault → проверка → первая запись → сводка), неверная ссылка / не календарь / недоступный сайт / отозванная → повторный
вопрос, три ошибки, замена умершей ссылки, неудачная регистрация возвращает прежнюю ссылку; слот источников, потолок ожидающих
ссылку задач, взаимное исключение задач одного источника, черновик после отменённого ожидания; маркеры ключа не встречаются в БД
вне `secrets`, логах, событиях, запросах к модели. `tests/jobs/test_calendar_chat.py`: ссылка в чате → `409 secret_link_calendar`
раньше проверок цели, ничего не сохранено и не создано, тред спрашивает кнопками (вуз треда, «Мои материалы» при одном вузе,
выбор из нескольких, замена уже рабочей ссылки, задача в другом треде), ответ на вопрос задачи и через `/answers`, запрос
клиента `POST …/calendar-link`, прочие секретные ссылки по-прежнему `422`. `tests/e2e/test_calendar_link.py`: чтение через
EgressProxy-процесс с грантом на один host и ни следа ключа в журнале прокси. Фронтенд (Vitest): `calendar-link.test.ts` (общие
векторы), ссылка не попадает ни в очередь, ни в `localStorage`, а тред получает вопрос; скрытое поле ссылки; «Календарь (iCal)» в
«Учёбе»; Playwright: `data-study.spec.ts`. L2 (`BOTAI_LIVE_LLM=1`, около $0.0005 за ход): `tests/agent/test_live_calendar.py` — студент нажал «Да» под вопросом о календаре
(один вуз; два вуза, кнопка одного из них), и настоящая модель вызывает `connect_calendar`, не трогает `connect_source`,
`request_login`, `request_repair`, `sync_source`, не переспрашивает и не просит ссылку в чат; календарь создан в нужном вузе,
задача `CONNECT` стоит в этом треде.

Браузерные проверки PWA в `apps/web/e2e` используют MSW, поэтому сами по себе не доказывают
связку API → очередь → воркер. Сквозной стенд выше проверяет эту связку отдельно. Оба уровня
остаются L1; успешное подключение настоящего вуза и качество ответа живой модели ими не доказаны.

- Новое поведение — тест L0 или L1. PR без теста не мержится.
- Если поведение проверено только моком, в описании PR это сказано прямо, и
  перечислено, что должен прогнать владелец на L2/L3.
