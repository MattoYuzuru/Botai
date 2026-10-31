# 01. BotAI as-is: аудит ограничений

Дата аудита: **2026-10-31**. Документ фиксирует состояние до пивота — ограничения,
которые владелец отменяет, с цитатами и точками в коде. После удаления ADR и старых
планов ([README](README.md):3-8) это единственное место, где цитаты сохранятся.

База: `HEAD a23585a` на момент аудита (#88, «сколько угодно источников у вуза»); последний коммит с кодом
— `656c138` (#87), код рабочего дерева относительно HEAD не изменён. Во время пивота ветка ушла на
`99778b1` (#89, только тесты), рабочее дерево дополнительно меняется самим пивотом
(`AGENTS.md`, `README.md`, часть `docs/*` уже переписаны), поэтому
все цитаты документов — по строкам `a23585a` (AGENTS/README — `git show a23585a:...`); `docs/adr/*`
и старые планы на этой базе ещё существуют.

## 1. Метод и границы

**Проверено по документам** (полностью): `docs/v4/README.md`,
`docs/v4/_input/constraint-analysis.md` (140 строк — чек-лист), `AGENTS.md` (HEAD),
`README.md` (HEAD), `QUESTIONS.md`, все 13 ADR `docs/adr/0001`–`0013`,
`docs/architecture/{security,agent-harness,overview,contracts}.md`,
`docs/product/requirements.md`, `docs/plan/{many-sources,agent-hub-v3,benchmark}.md`,
`docs/development/testing.md`; фрагменты `contracts/*`, `factory-v2.md`,
`source-research.md` — только в объёме ссылок.

**Проверено по коду** (чтение и grep, без запуска): `settings.py`, `learn/policies.py`,
`agent/{actions,safety,harness,tools,tools_web,tools_sources,tools_manage,llm,conversations,env,turn_context}.py`
и `agent/prompts/*.md`, `browser/{egress,proxy,gateway,assets,login,screen,read_views,read_capabilities}.py`,
`learn/{lint,discovery,explorer,redact,observe,revisions}.py`,
`jobs/{queue,runner,answers}.py`, `vault/pg.py`,
`core/{vault,embed,actions,browser,errors,calendar_link,execution}.py`,
`sources/{fetch,runner,factory,readonly,calendar}.py`,
`sources/adapters/{moodle_ws,ical}.py`, `spaces/{service,conversations}.py`,
`db/models.py`, `usage/service.py`, `search/{safety,service}.py`;
точечно `apps/web/src/features/chat/{calendar-link,outbox}.ts`.

**Что не проверено.** Production и настоящие вузы (L3), фактическое прохождение C1–C19
(`security.md`:672-674: таблица — не отчёт), UI, миграции, сетевые прогоны, `make check`;
тесты не запускались; `.env` не открывался, секреты не печатались. Целиком не читались
`contracts/conversation.md`, `hld.md`, `lld.md`, `factory-v2.md`, `source-research.md`.
Классификация: **подтверждено**; **уточнено** (верно по сути, но формулировка/числа/
механизм неточны); **устарело** (было верно, перестало); **опровергнуто** (не совпадает
с кодом/доками).

## 2. Сводка: 44 утверждения

Итог: **32 подтверждено, 9 уточнено, 1 устарело, 2 опровергнуто.**

| № | Утверждение (кратко) | Статус | Доказательство |
|---|---|---|---|
| 1 | Два контура: обучение с LLM один раз, sync по рецепту без модели | ✅ | `overview.md`:10-20; `sources/runner.py`:603-633; `learn/explorer.py`:1429-1468 |
| 2 | Рецепт — **единственный** мост между контурами (ADR-0002) | ⚠ устарело | `adr/0002`:15-21; мостов больше: пресет входа `browser/presets.py`, карта `learn/sitemap.py`, rendered `sources/rendered.py`:40-44, ingest `sources/ingest.py`:319-331 |
| 3 | Агент не пишет код парсеров, JS, regexp; schema + lint | ✅ | `adr/0005`:34-38; `AGENTS.md`(HEAD):41-42; `learn/lint.py`:1-15; в `src/` нет `eval`/`exec` |
| 4 | Адаптеров четыре: `recipe`, `moodle_ws`, `ical`, `document`; новый — PR+ADR | ✅ | `sources/factory.py`:1-2,29; `adr/0005`:24-32 |
| 5 | В sync нет LLM | ⚠ уточнено | `sources/runner.py`:603 (LLM не импортируется); исключение — разбор документа `adr/0005`:40-42, `sources/ingest.py`:319 |
| 6 | В исследовании модель выбирает по ID и не придумывает URL/селекторы/клики/HTTP | ⚠ уточнено | `adr/0010`:18-22; `learn/discovery.py`:85-96; но у чат-агента URL — параметр (`tools_web.py`:274-282) |
| 7 | Бюджеты: 10 шагов, 12 представлений, 200 запросов, 90 с, 150k токенов + общий | ⚠ уточнено | `learn/discovery.py`:74-81 — на срез; общий — `settings.py`:58-65 (1800 с, 1,5 млн, 100 шагов, 2000 запросов), `explorer.py`:1441-1449 |
| 8 | Один рецепт на origin; lint против личных значений, абсолютных URL, чужих хостов | ✅ | `contracts.md`:54; `learn/lint.py`:45-79,707-731; `learn/revisions.py`:1-13; `db/models.py`:159-163 |
| 9 | Write только по одноразовому подтверждению (метод, URL, sha256 тела, ~5 мин) | ⚠ уточнено | `agent/actions.py`:55,127-139,252-323,353-400; исполнитель в production не зарегистрирован: `:17-18,111-117,384-398` |
| 10 | Deny-list необратимых действий даже с одобрением | ⚠ уточнено | `agent/actions.py`:57-62; regex уже формулировки: нет «смены почты/телефона» и `unenrol` |
| 11 | Рецепты — GET; POST только из allowlist в коде (два метода Moodle) | ✅ | `contracts.md`:99-100; `sources/readonly.py`:14-22; `adapters/moodle_ws.py`:28-40; `lint.py`:76-79 |
| 12 | У агента нет инструмента «отправить данные на URL» | ✅ | `agent/tools.py`:551-570; `tools_web.py`:1-15 |
| 13 | Адрес запроса всегда определяется origin подключения, а не текстом | ❌ опровергнуто | `browse_source(url)` `tools_web.py`:274-282; `fetch_public_page(url)` `:352-356`; `connect_source(url)` — ограничивают allowlist/грант, не «origin всегда» |
| 14 | `browse_source(connection_id, url)`: GET, адреса-действия отклоняются, ≤3 рендеров | ✅ | `tools_web.py`:71-76,274-286; `core/actions.py`:21-55 |
| 15 | FR-054: основной источник/отключение — только кнопкой приложения | ✅ | `tools_manage.py`:299-341,456-487; `agent-harness.md`:126-175 |
| 16 | EgressProxy: allowlist, HTTPS:443, без IP, приватные диапазоны, DNS-пин; route — не контур | ✅ | `browser/proxy.py`:1-31,57,139; `security.md`:330-347 |
| 17 | Новый хост только с согласия; sync/поиск/модель список не расширяют | ✅ | `security.md`:344; `browser/login.py`:383-396; `browser/egress.py`:89-113; `search/service.py`:34-35,145 |
| 18 | CDN-файлы: GET, без cookie/Authorization, ≤16 хостов на вход; агент — 0; `unsafe-url` теряет CSS | ✅ | `browser/proxy.py`:62-65; `browser/gateway.py`:2005-2014; `browser/assets.py`:118,197; `security.md`:420-582 |
| 19 | SW/SharedWorker/загрузки/разрешения выключены; кэш входа отключён | ✅ | `browser/gateway.py`:194-209,1879,2028-2034; `security.md`:581-582 |
| 20 | Тесты не ходят в настоящие вузы | ✅ | `testing.md`:8-13; `AGENTS.md`(HEAD):31-35; `tests/agent/conftest.py`:42-44 |
| 21 | LLM не видит пароль/MFA/cookie/bearer/iCal; API→Vault→воркер; у `connect_calendar` нет адреса | ✅ | `jobs/answers.py`:246; `tools_sources.py`:1064-1082; `agent/safety.py`:210-226; `security.md`:48 |
| 22 | Личная ссылка календаря: клиент не ставит в очередь, сервер 409 и не сохраняет | ✅ | `core/calendar_link.py`:126-157; `spaces/conversations.py`:88-103; `agent/conversations.py`:322; `errors.py`:45,62; `apps/web/src/features/chat/calendar-link.ts`:1-12 |
| 23 | Перед моделью вырезаются значения сессии; неполный инвентарь → наблюдение прекращается | ✅ | `learn/redact.py`:99-135; `learn/observe.py`:222-237; `security.md`:8-16 |
| 24 | Bearer — точный origin с портом; только отправленный браузером; чужие JWT не собираются | ✅ | `core/browser.py`:82-96; `sources/session.py`:118-135; `browser/gateway.py`:2246-2274; `security.md`:322 |
| 25 | Чувствительные эндпоинты (профиль) не описываются в рецептах | ⚠ уточнено | только документ `security.md`:635 (L6); отдельного blocklist в коде нет |
| 26 | Квоты LLM на пользователя, предохранитель пула, sync продолжают работать | ✅ | `settings.py`:74-80; `usage/service.py`:94-160; `agent/llm.py`:188 |
| 27 | Капча/проверка браузера/MFA не обходятся; stealth/отпечаток запрещены | ✅ | `AGENTS.md`(HEAD):18-19; `adr/0001`:58-63; `browser/login.py`:11-12,89-92 |
| 28 | Код статуса — `user_action_required` | ❌ опровергнуто | в `core/errors.py`:12-50 такого кода нет: есть `source_bot_protection` (`:17`) и блоки `LoginBlock`/`WaitBlock` |
| 29 | Вход read-only; POST только в окне шага (~15 с), до 16 «печатающих» и 4 после отправки | ✅ | `browser/gateway.py`:98-109,698-770; `security.md`:86-144 |
| 30 | 3 неудачных входа → пауза 10 минут | ✅ | `browser/login.py`:97-107 (`cooldown_s=600.0`, `max_attempts=3`), 947-962 |
| 31 | Удалённый экран включается, только если выдерживает SLA; иначе не включается | ⚠ уточнено | `adr/0001`:47-56; `requirements.md`:220; `browser/screen.py`:1-14 — цель и адаптация качества, гейта «не включать» нет |
| 32 | Ничего под конкретный вуз; справочник названий без адресов | ✅ | `AGENTS.md`(HEAD):36-40; `requirements.md`:174; в `src/` нет `hse.ru`/`centraluniversity`; `spaces/university-names.json` |
| 33 | Нет Redis/Kafka/векторного движка; очередь PG `SKIP LOCKED`, FTS `russian`, `NoopEmbedder` | ✅ | `jobs/runner.py`:1-2,245; `agent/tools.py`:344-358; `db/models.py`:406; `core/embed.py`:7-15 |
| 34 | Отложено: один сервер/пул, мультирегион, публичная регистрация/биллинг, нативные приложения, Telegram | ✅ | `overview.md`:312-315; `requirements.md`:232-233; `QUESTIONS.md`:48 (Q5) |
| 35 | Агент отказывает в задачах не про учёбу (FR-052) | ⚠ уточнено | `requirements.md`:194; `prompts/agent.md`:75 — правило промпта, не код-фильтр |
| 36 | Нельзя выдумывать проценты и выдавать ответ без источника за подтверждённый | ✅ | `prompts/system.md`:10-11; `AGENTS.md`(HEAD):64-65; `requirements.md`:158 |
| 37 | Пароль хранится; сервер может расшифровать; при выходе за круг — блокирующее | ✅ | `adr/0001`:105-108; `core/vault.py`:1-6; `vault/pg.py`:1-8; `security.md`:268-280 |
| 38 | Оператор сервера видит ввод при входе (T8) | ✅ | `security.md`:268-280; `browser/screen.py`:13 (кадры/ввод не пишутся) |
| 39 | Канал GET через CDN во время входа | ✅ | `security.md`:556-573; `browser/assets.py`:118-197 |
| 40 | POST в окне шага не сверяется с `form.action` | ✅ | `browser/gateway.py`:101-106; `security.md`:138-144 |
| 41 | Скрипты вуза в серверном Chromium; изоляция BrowserContext, не ОС-процесс на пользователя | ✅ | `security.md`:680-682; `browser/gateway.py`:2020-2037 |
| 42 | Промпты уходят в DeepSeek (Китай); к юристу; смягчение — минимизация ПДн | ⚠ уточнено | `security.md`:632-634; `settings.py`:39; минимизация — в `learn/redact.py`:1-12 и `search/safety.py`, в чате только секреты (`agent/safety.py`) |
| 43 | Один календарь на вуз — временное ограничение | ✅ | `sources/calendar.py`:108-178; `tools_sources.py`:1069; `prompts/agent.md`:31; `QUESTIONS.md`:32-38 |
| 44 | Поиск источника по origin — временное ограничение | ✅ | `spaces/service.py`:283-294,364-369,372-423; `tools_sources.py`:518-536; `db/models.py`:188-225; `plan/many-sources.md`:39-51 |

## 3. Ограничения по группам

Формат: **где закреплено** (документ:строка; код:строка) → зачем → цена → что сломается при
отмене. Номера — строки таблицы §2.

### 3.1 Код-запрет: агент пишет данные, не код

**3–4. Только декларативные спецификации; четыре фиксированных адаптера.**
Где: `adr/0005`:24-38 — «Агент **никогда** не пишет исполняемый код парсера. Он пишет
спецификацию, которая проходит JSON Schema и lint»; `AGENTS.md`(HEAD):41-42; код —
`sources/factory.py`:1-2 («one of four fixed executors»), `:29` `ADAPTER_KINDS`;
`learn/lint.py`:1-15; в `backend/src` нет `eval`/`exec`.
Зачем: «текст со страницы вуза не может стать кодом на сервере» (`adr/0005`:37-38), T6.
Цена: нестандартный источник ждёт DSL, честный `unsupported capability`
(`adr/0011`:113-115); нет полного RFC 5545 и произвольных действий (`adr/0012`:75-77).
Сломается: `learn/lint.py`, `learn/revisions.py` (canary/откат), `sources/factory.py`,
контракты адаптера/рецепта, тесты C4/C5 (`security.md`:655-656).

**5. В sync нет LLM (с исключением).**
Где: `overview.md`:10-15; `adr/0002`:17-21; код — `sources/runner.py`:603
(`sync_connection`) не импортирует LLM. Зачем: детерминизм и «почти нулевая цена»
(`adr/0002`:25). Цена: рассуждение над содержимым — отдельным путём, «менее надёжным»
(`adr/0002`:30-32). Исключение: разовый разбор документа моделью (`adr/0005`:40-42;
`sources/ingest.py`:319,331).
Сломается: контур дешевизны; тест «второй студент без модели» (`testing.md`:134-135),
NFR-008 (`requirements.md`:210), сквозной e2e sync.

**6. В исследовании модель выбирает действие по ID.**
Где: `adr/0010`:18-22 — «Модель не создаёт произвольные URL, селекторы, JavaScript или
HTTP-запросы»; код — `learn/discovery.py`:85-96 (`Decision` = action/ref/overrides),
`browser/read_views.py`:16 (`MAX_READ_VIEWS = 12`); схема плана не принимает URL
(`agent-harness.md`:395-398). Зачем: «не нажать что-то опасное». Цена: произвольный клик
запрещён, разрешены `role=tab`, `rel=next`, нативные контролы.
Сломается: `sources/read_capabilities.py`, `sources/read_views.py`, `learn/discovery.py`,
`learn/probe.py`, тесты replay (`testing.md`:164-165). Уточнение: это верно только для
исследования; у чат-агента URL — параметр инструмента (3.2).

### 3.2 Read-only по умолчанию и write по подтверждению

**9–10. Write только по одноразовому подтверждению; deny-list.**
Где: `security.md`:588-601 — «По умолчанию система **read-only**… Одобрение: одноразовое
(`used_at`), с TTL (предлагается 5 минут)»; `AGENTS.md`(HEAD):27-28; код —
`agent/actions.py`:55 `PROPOSAL_TTL = timedelta(minutes=5)`, `:58-62` `DENY_LIST`
(logout|delete|remove|revoke|unsubscribe|password|payment|pay|late-days|final-submit|
withdraw), `:252-323` атомарный `pending → approved`, `:353-400` проверка хеша и
одноразовое исполнение, `:145-160` `check_request` (метод, deny-list, allowlist).
Зачем: T5/T14 (`security.md`:257,266) — инъекция не становится write, отрицание
закрывается журналом. Цена: автоматизации действий нет.
Сломается: `agent/actions.py`, блок `approval` в `contracts/agent-ui.schema.json`,
`contracts/conversation.md`, тест C9 (`security.md`:660). Уточнения — в §5 п.4.

**11. Рецепты — GET; POST только из allowlist в коде.**
Где: `contracts.md`:99-100; `security.md`:598; код — `sources/readonly.py`:14-22
`READ_ONLY_POST` (AJAX-путь Moodle и два метода чтения); `lint.py`:76-79
`DESTRUCTIVE`; `adapters/moodle_ws.py`:28-40 `ALLOWED_FUNCTIONS`.
Зачем: чтение не меняет состояние источника; список — в коде, не в спецификации.
Цена: источники, где чтение требует POST, не читаются.
Сломается: `sources/readonly.py`, `learn/lint.py`, `sources/fetch.py`, тест C5
(`security.md`:656), инвариант «sync не пишет в вуз».

**12–13. Нет инструмента «отправить данные на URL»; адрес — от подключения.**
Где: `security.md`:602 — «„отправить куда-то“ инструмента не существует»; код — реестр
`agent/tools.py`:551-570 не содержит POST-инструмента; `tools_web.py`:1-15 — только
чтение. Опровергнута буквальная часть «адрес всегда определяется origin»:
`browse_source(url)` (`tools_web.py`:274-282), `fetch_public_page(url)` (`:352-356`),
`connect_source(url)` принимают адрес от модели; защита — allowlist/грант
(`sources/fetch.py`:142-150, `browser/egress.py`:89-113).
Зачем: T5/prompt injection и эксфильтрация. Цена: произвольный адрес без гранта
недоступен; публичная страница — разовый грант на один host (`security.md`:352).
Сломается: `browser/egress.py`, `browser/proxy.py`, `sources/fetch.py`, тесты C2/C3
(`security.md`:653-654).

**14. `browse_source`: GET, фильтр адресов-действий, ≤3 рендеров.**
Где: `agent-harness.md`:274-286 — «метода и тела в параметрах нет»; код —
`tools_web.py`:71-76 (`MAX_BROWSES = 12`, `MAX_RENDERS = 3`, `TOOL_BUDGET_S`),
`core/actions.py`:21-55 (`_ACTION_WORDS`: logout/delete/submit/…/sesskey),
`browser/gateway.py`:359-380 read-only guard, `:2069-2079` его установка, `:1985-1986`
чтение агента — только read-only background. Зачем: GET-ссылка не должна выполнить
действие (`core/actions.py`:1-6). Цена: часть страниц недоступна, рендер дорог.
Сломается: `agent/tools_web.py`, `core/actions.py`, `sources/rendered.py`, тест C17
(`security.md`:668), `tests/agent/test_tools_web_spa*`.

**15. FR-054: основной источник и отключение — только кнопкой.**
Где: `agent-harness.md`:126-175 — «Первый вызов ничего не меняет»; код —
`tools_manage.py`:299-341 `_authorize` (слово в сообщении не подтверждает; нужна
подпись кнопки в сообщении этого хода), `:456-487` `set_primary_source`, `:555-…`
`disable_source`, `:509-552` `ask_primary_source` (≤3 кнопки).
Зачем: FR-054 (`requirements.md`:196) — настройку меняет не модель и не текст страницы.
Цена: лишний щелчок. Сломается: `agent/tools_manage.py`, `agent/env.py`:136-139
(`quick_replies`/`pinned_replies`), `db/models.py`:586 `AgentChoice`, тесты FR-054
(`testing.md`:286-297), L2 `tests/agent/test_live_schedule_switch.py`.

### 3.3 Сеть наружу

**16. EgressProxy — единственный контур; allowlist, HTTPS:443, DNS-пин.**
Где: `security.md`:330-347; код — `browser/proxy.py`:1-31 («is the security contour …
not Playwright route interception (which does not see server redirects)»), `:57`
`ALLOWED_PORT = 443`, `:139` приватные сети (включая `169.254.0.0/16`), `:562-580`
`_pin_address` (resolve-then-connect). Зачем: SSRF, уход egress, rebinding (T4).
Цена: вход через внешний SSO — с согласием; часть страниц без файлов.
Сломается: `browser/proxy.py`, `browser/egress.py`, `browser/gateway.py`
(`new_context` с прокси `:2020-2027`), `sources/fetch.py`, тесты C2/C3 и
`tests/browser/test_proxy*`.

**17. Новый хост — только с согласия человека.**
Где: `security.md`:343-344 — «неизвестный хост сначала блокируется; LoginFlow показывает
его человеку и расширяет allowlist только после согласия… sync и поисковая выдача список
сами не расширяют»; код — `browser/login.py`:383-396 («Разрешить этому подключению
обращаться к этому адресу?»), `:697-710` вопрос о хосте-активе; `sources/fetch.py`:
142-150 `host_allowed`; `search/service.py`:34-35,145 — отдельные фиксированные хосты
Exa/DeepSeek, не allowlist подключения. Зачем: неизвестный хост = утечка и SSRF.
Цена: лишний вопрос при SSO/редиректе. Сломается: `browser/login.py`,
`browser/proxy.py`, `sources/fetch.py`, `ConfirmBlock` в UI, тесты
`tests/browser/test_assets.py`.

**18. CDN-файлы: GET, без учётных данных, ≤16 хостов на вход.**
Где: `security.md`:420-582 (§4.4); код — `browser/proxy.py`:62-65 (`ASSET_HOSTS = 16`,
«16 is three times the largest seen»), `browser/gateway.py`:2005-2014 (`agent_read` ⇒
`asset_cap = 0`), `browser/assets.py`:118,197 (`Referer` с query — `asset_referer`);
тест `test_only_the_browser_gateway_asks_for_an_allowance` (`security.md`:512).
Зачем: без файлов вход не работает, с ними — канал утечки, поэтому условия жёсткие.
Цена: `unsafe-url` теряет CSS; HTTP-кэш выключен (`security.md`:581-582); GET-канал
остаётся принятым риском (3.7). Сломается: `browser/assets.py`, `browser/proxy.py`
(`ARM`), `browser/gateway.py`, `SessionState.asset_hosts`, тест C19 (`security.md`:670),
`tests/browser/test_assets*`.

**19. Браузерные возможности выключены.**
Где: `security.md`:350; код — `browser/gateway.py`:194-209 `service_worker_refuser`
(по заголовку `Service-Worker: script`), `:1879` `--disable-blink-features=SharedWorker`,
`:2028-2034` `accept_downloads=False`, `permissions=[]`, `service_workers="allow"`
(из-за обхода `"block"`). Зачем: воркер/загрузки/устройства — каналы вне `route` и
прокси. Цена: нет загрузок из браузера, кэш не работает.
Сломается: `browser/gateway.py`, тесты `tests/browser/test_service_workers.py`
(`testing.md`:204), C19.

**20. Тесты не ходят в настоящие вузы.**
Где: `testing.md`:8-13 (L0/L1 — loopback `fakeuni`, L3 — владелец), `:15-21`;
`AGENTS.md`(HEAD):31-35; код: L2 пропускается без `BOTAI_LIVE_LLM=1`
(`tests/agent/conftest.py`:42-44). Зачем: не рисковать аккаунтами и не выдавать фикстуру
за живой вуз. Цена: качество на настоящих сайтах тестами не доказывается
(`testing.md`:20-21,224-229). Сломается: уровни тестов, `fakeuni`, CI-гейты,
`testing.md`; единственным доказательством станет живая приёмка.

### 3.4 Секреты и модель

**21. LLM не видит первичные секреты; ввод идёт мимо неё.**
Где: `AGENTS.md`(HEAD):20-24; `security.md`:48 — «Секретные ответы на блоки
(`input.secret`) проходят API → Vault (`transient`, TTL 10 мин) → воркер и в LLM не
попадают»; код — `jobs/answers.py`:246, `vault/pg.py`:37 `TRANSIENT_TTL = 10 мин`,
`agent/safety.py`:210-226; у `connect_calendar` в схеме только
`university_id`/`university_query`/`title`/`replace_link` (`tools_sources.py`:1064-1082).
Зачем: NFR-003/NFR-004. Цена: автовход требует Vault, календарь — защищённого поля.
Сломается: `vault/`, `jobs/answers.py`, Vault-ветка `browser/login.py`, `agent/safety.py`,
блок `secret` в `contracts/agent-ui.schema.json`, C1/C18.

**22. Личная ссылка календаря не входит в чат и историю.**
Где: `security.md`:374-418 (§4.3) — «В чат ссылка не попадает… сервер отвечает
`409 secret_link_calendar`: сообщение не сохраняется»; код — `core/calendar_link.py`:
126-157, `spaces/conversations.py`:88-103 `reject_secret_links`,
`agent/conversations.py`:322, `jobs/answers.py`:212-223 (ответ задачи тоже),
`core/errors.py`:45,62; клиент — `apps/web/src/features/chat/calendar-link.ts`:1-12,
`outbox.ts`:94-110; общие векторы `contracts/calendar-link-vectors.json`.
Зачем: ссылка — секрет класса токена; утечка = весь календарь. Цена: неудобно.
Сломается: `core/calendar_link.py`, `spaces/conversations.py`, outbox, C18,
`tests/jobs/test_calendar_chat.py`, `calendar-link.test.ts`.

**23. Наблюдения редактируются; неполный инвентарь закрывает наблюдение.**
Где: `security.md`:8-16 — «Ошибка экспорта, разбора защищаемого состояния или превышение
лимита прекращает наблюдение до вызова модели»; код — `learn/redact.py`:99-135
`protect_session`/`protect_values`, `:30-35` лимиты; `learn/observe.py`:222-237 — при
ошибке `raise BotaiError(SOURCE_UNAVAILABLE, ...)`; `browser/gateway.py`:1561-1625
`observation_secrets` отказывает при неполном инвентаре. Зачем: L5/152-ФЗ и T6.
Цена: наблюдение честно неполное (`security.md`:16). Сломается: `learn/redact.py`,
`learn/observe.py`, `browser/gateway.py` (inventory), `learn/lint.py` (`observed`),
`tests/learn/test_session_redaction.py` (`testing.md`:190).

**24. Bearer привязан к точному origin с портом.**
Где: `security.md`:322 — «Bearer берётся только тот, который **браузер сам отправлял**…
произвольные JWT из storage не собираются»; код — `core/browser.py`:82-84 («Exact HTTPS
credential audience, including a nondefault port; never a host suffix»),
`sources/session.py`:118-135 `scoped_bearer`, `browser/gateway.py`:2246-2274
`add_authorization` (только хост подключения + точный audience; хосту-активу — никогда).
Зачем: T1/T5. Цена: часть запросов без bearer, на cookie. Сломается:
`sources/session.py`, `sources/fetch.py` (`auth_scope_mismatch`:170),
`browser/gateway.py`, `tests/browser/test_scoped_auth.py` (`testing.md`:161).

**25. Чувствительные эндпоинты (L6) — только документ.** `security.md`:635: «эндпоинты с
чувствительными ПДн (`/api/student-hub/students/me`) **не описываются в рецептах и не
синкаются вовсе**». В коде отдельного blocklist нет (grep `student-hub`/`students/me` по
`src/` пуст); защита — lint/redaction. При отмене исчезнет только правило в документе.

**26. Квоты LLM и предохранитель пула.** `security.md`:614-616 — «при достижении — чат
отключается, **синки и просмотр данных продолжают работать**»; `QUESTIONS.md`:58 (Q15);
код — `settings.py`:74-80 (`quota_tokens_per_day = 20_000_000`,
`quota_requests_per_minute = 90`, `quota_pool_usd_per_day = 20.0`),
`usage/service.py`:94-160 `check_quota` (pool → user-day → minute; `quota_exceeded` 429),
вызовы `agent/llm.py`:188 и `search/service.py`:163; sync `check_quota` не вызывает.
Зачем: общий ключ платит владелец, защита от цикла (T7). Цена: чат получает 429, данные
обновляются. Сломается: `usage/service.py`, `agent/llm.py`, `/usage` API, тест C10.

**42. Промпты уходят в DeepSeek (Китай).** `security.md`:632-634 — трансграничная
передача ПДн, L5 «минимизация… в промптах», «к юристу»; `QUESTIONS.md`:59 (Q16);
`settings.py`:39 `llm_base_url = "https://api.deepseek.com"`; ключ — только в заголовок
(`agent/llm.py`:505). Уточнение: минимизация реализована в исследовании
(`learn/redact.py`:1-12) и поиске (`search/safety.py`), но не в чате (там только секреты
— `agent/safety.py`). Сломается: вопрос ПДн/152-ФЗ, L5 в документах, C14.

### 3.5 Капча и вход

**27–28. Капча, проверка браузера и MFA не обходятся.**
Где: `AGENTS.md`(HEAD):18-19 («Stealth-плагины и подделка отпечатка запрещены»);
`adr/0001`:58,62,120 («Капча и защита от ботов не обходятся никогда»);
`requirements.md`:44; код — `browser/login.py`:11-12 («A captcha, a bot-protection check
or a page nobody has taught the flow about is never automated»), `:89-92`
`_BOT_PROTECTION_DETAIL`, `:633,683,790` `SOURCE_BOT_PROTECTION`; UI — `LoginBlock`/
`WaitBlock`; `core/errors.py`:17. Зачем: аккаунт студента и сигнал «нужен человек»
(T12, `security.md`:264). Цена: часть входов всегда ручная (`adr/0001`:120).
Сломается: `browser/login.py`, `browser/screen.py`, контракт `LoginBlock`, C16/CAPTCHA
(`testing.md`:198), экран капчи `fakeuni`. Опровергнуто уточнение «код
`user_action_required`»: термин только в документе (`security.md`:264,699) и входном
анализе; в коде — `source_bot_protection` и блоки.

**29. Окно шага на странице входа.**
Где: `security.md`:86-144 — «Окно длится ≈15 с (`AUTH_WINDOW_S`)… до `AUTH_TYPING_POSTS`
(16) POST… дальше окно даёт `AUTH_WINDOW_POSTS` (4)»; код — `browser/gateway.py`:98-109,
`:698-770`, `:2069-2079`. Зачем: вход в ЦУ с JS-отправкой формы не работал при сверке с
`form.action` (`adr/0001`:69-72). Цена: `form.action` не сверяется — принятый риск (3.7).
Сломается: `browser/gateway.py`, `browser/login.py`, `fakeuni/login.py`, C16.

**30. Пауза после трёх неудачных входов.** `security.md`:264 (T12); код —
`browser/login.py`:97-107 `LoginTiming` (`cooldown_s = 600.0`, `max_attempts = 3`),
`:947-962` `_register` (`_pause(...)` и `LOGIN_FAILED` «…попробуйте через 10 мин»).
Зачем: не заблокировать аккаунт (T12). Цена: 10 минут недоступности после серии ошибок.
Сломается: `browser/login.py`, тест backoff'а, e2e «опечатка в пароле»
(`testing.md`:135).

**31. SLA удалённого экрана.** `adr/0001`:47-56 — «Требования к нему жёсткие, иначе он не
включается вовсе»: p95 ≤ 150 мс, первый кадр ≤ 2 с, 15–30 к/с, обрыв ≤ 3 с;
`requirements.md`:220 (NFR-018); код — `browser/screen.py`:1-14 (цели), `:60-73`
`ScreenConfig`, `:160` `SLOW_MS = 120.0`. Уточнение: SLA — измеряемая цель и адаптация
(`QualityController`), но нет условия «не включать экран, если SLA не выдерживается».
Сломается: `browser/screen.py`, `screen_remote.py`, `contracts/events.md` §2,
e2e запасного экрана (`testing.md`:142), NFR-018.

### 3.6 Архитектурные «нет»

**32. Ничего под конкретный вуз.** `AGENTS.md`(HEAD):36-40 — «В коде нет origin'ов,
эндпоинтов, селекторов конкретных вузов — только в фикстурах тестов (синтетический вуз
`fakeuni`)»; `requirements.md`:174 (FR-032); `QUESTIONS.md`:12-14; код: в `backend/src`
нет `hse.ru`/`centraluniversity`; исключение — справочник названий
`backend/src/botai/spaces/university-names.json` (без адресов, разрешён
`AGENTS.md`:38-40); фикстуры — `backend/tests/fakeuni/`. Зачем: вуз подключается
обучением, а не пресетом. Цена: даже ВШЭ и ЦУ — через обучение; «любой вуз» — гипотеза
(`requirements.md`:251-252). Сломается: `fakeuni`, весь `tests/*`, FR-032/NFR-015,
`spaces/universities` и каталог имён.

**33. Нет новых инфраструктурных узлов без ADR.** `AGENTS.md`(HEAD):62-63;
`adr/0003`:12,31 — «Свой воркер поверх Postgres… без Redis»; `adr/0006`:12-16;
код — `jobs/runner.py`:1-2,245 (`with_for_update(of=Job, skip_locked=True)`),
`agent/tools.py`:344-358 (FTS `russian`), `db/models.py`:406), `core/embed.py`:7-15;
в зависимостях и compose нет redis/kafka/celery. Зачем: один stateful-узел на VPS
(`adr/0003`:26-27).
Цена: очередь и поиск ограничены Postgres; эмбеддинги отложены (Q34). Сломается:
`jobs/queue.py`, `jobs/runner.py`, `core/embed.py`, миграции,
`docs/plan/deployment.md`, критерии «без нового узла».

**34. Осознанно отложено.** `overview.md`:312-315 — «Горизонтальное масштабирование:
один сервер, один воркер-пул. Мультирегион… Публичная регистрация и биллинг. Мобильные
нативные приложения — только PWA»; `requirements.md`:232-233 (Telegram вне продукта);
`QUESTIONS.md`:48 (Q5); код — один пул `RunnerConfig` (`jobs/runner.py`:83-92).
Зачем: pet-проект на двух человек (v4 README:12-15); цена — нет масштабирования и
публичного онбординга. Сломается: `overview.md`, deployment, `auth/invites`, PWA.

**35–36. Учебный фокус и честность.** `requirements.md`:194 (FR-052), `:158` (FR-016),
`:242` (метрика честности); `prompts/agent.md`:75 — «Вы помогаете только с учебой… На
просьбы вне учёбы… вежливо откажите»; `prompts/system.md`:10-11 — «Нет данных, источника
или основания — скажите об этом прямо: «Не знаю: …»… Не придумывайте…»;
`AGENTS.md`(HEAD):64-65. Уточнение: это правило промпта и требование, а не код-фильтр;
проверяется L2-тестами честности (`testing.md`:12, `benchmark.md`:45). Цена: интерфейс
иногда честно пишет «неизвестно». Сломается: `prompts/system.md`, `prompts/agent.md`,
L2 B4 (`benchmark.md`:31,45), FR-016/FR-052.

### 3.7 Принятые риски (обратная сторона)

**37. Пароль хранится, сервер может расшифровать.**
Где: `adr/0001`:105-108 («сервер может расшифровать пароль… Утечка БД без мастер-ключа
паролей не раскрывает; утечка БД вместе с мастер-ключом — раскрывает»);
`security.md`:268-280; `QUESTIONS.md`:51-52 (Q8–Q9); код — `core/vault.py`:1-6,
`vault/pg.py`:58-78,104-128, `db/models.py`:226 `SecretRow` («Ciphertext only»). Зачем:
автовход (FR-005); за кругом знакомых — **блокирующее** условие (`security.md`:277-280).
Сломается: автовход, FR-005/FR-034, `vault/`, онбординг, тест C7.

**38. Оператор сервера видит ввод при входе (T8).**
Где: `security.md`:268-280 — «оператор сервера технически способен прочитать пароль, код
MFA и содержимое экрана»; ограничители — только память, запрет логирования ввода и кадров
(`browser/screen.py`:13), TTL, tmpfs. Зачем: закрытый пул, оператор = владелец; при
расширении пула нужен client-side ввод или предупреждение. Сломается: модель угроз T8,
UI-предупреждение, C1.

**39. Канал GET через CDN во время входа.**
Где: `security.md`:556-573 — «Скрипт страницы входа может прочитать `input.value` и
отправить его картинкой на поручённый хост… Устранить канал можно только отказом от
правила»; код — `browser/assets.py`, `browser/gateway.py`:2039-2068. Зачем: иначе не
работает вход через внешние SSO (инцидент `yastatic.net`, `security.md`:422-427). Цена:
16 хостов, бюджеты, только публичные адреса; чтения канал не открывают
(`security.md`:566-568). Сломается: вход через SSO/CDN, `browser/assets.py`, C19.

**40. POST внутри окна шага не сверяется с `form.action`.**
Где: `security.md`:138-144 — «Внутри окна страница с хоста входа может отправить POST
любого пути», это «цена отказа от сравнения с `form.action`»; код —
`browser/gateway.py`:101-106. Зачем: живой вход в ЦУ (`adr/0001`:69-72). Цена: 15 с,
бюджет POST, хост входа, видимый auth UI для XHR. Сломается: `browser/gateway.py` auth
windows, C16, вход в Keycloak-подобные формы.

**41. Скрипты вуза исполняются в серверном Chromium.**
Где: `security.md`:680-682 — «Chromium создаёт отдельный BrowserContext… это не обещание
отдельного OS-процесса на пользователя»; код — `browser/gateway.py`:2020-2037 (один
`new_context`), `:1988-1998` один живой контекст на пользователя. Зачем: сайт вуза —
недоверенный код; изоляции контекста достаточно для pet-проекта. Сломается:
`browser/gateway.py`, `security.md` §8.1, требования к контейнеру.

**42. Трансграничная передача в DeepSeek** — см. 3.4; `security.md`:623-641, L3–L5.

### 3.8 Временные долги
**43–44. Один календарь на вуз и поиск источника по origin** — подробно в §6.

## 4. Карта кода: где физически живут ограничения

| Ограничение | Где в коде |
|---|---|
| Бюджеты исследования (срез и общий) и бюджеты харнеса | `learn/discovery.py`:74-81; `settings.py`:58-65; `learn/explorer.py`:1429-1468; `learn/progress.py`:230,279,299; `agent/harness.py`:59-82 |
| Лимиты `browse_source` (12 страниц, 3 рендера, 20 с/рендер) | `agent/tools_web.py`:71-86; `sources/rendered.py`:40-44 |
| Egress (allowlist, HTTPS:443, IP/приватные/DNS-пин) и хосты-активы (16/0) | `browser/proxy.py`:1-31,57,62-65,139,562-580; `browser/egress.py`:42,89-113; `browser/gateway.py`:2005-2014; `browser/assets.py` |
| Read-only guard и классификатор адресов-действий | `browser/gateway.py`:359-380,2069-2079; `core/actions.py`:15-60; `browser/read_capabilities.py` |
| Окно входа (15 с, 16+4 POST, followup 10 с); пауза (3 попытки / 600 с) | `browser/gateway.py`:98-120,689-770,790-865; `browser/login.py`:97-107,947-962 |
| Write-подтверждение (TTL 5 мин, хеш, одноразовость) и deny-list | `agent/actions.py`:55-62,127-139,252-323,353-400 |
| POST-allowlist чтения (два метода Moodle) | `sources/readonly.py`:14-22; `adapters/moodle_ws.py`:28-40 |
| Фабрика четырёх адаптеров; lint рецепта; canary/откат | `sources/factory.py`:29; `learn/lint.py`:45-79,139-143,193-215,707-731; `learn/revisions.py`:1-13,40-60 |
| Redaction наблюдений и результаты инструментов (секреты, инъекции) | `learn/redact.py`:30-78,99-135; `learn/observe.py`:222-237; `agent/safety.py`:50-309 |
| Bearer audience = точный origin; только отправленный браузером | `core/browser.py`:82-96; `sources/session.py`:118-135; `browser/gateway.py`:2246-2274 |
| Vault (AES-256-GCM, DEK/KEK, AAD, transient 10 мин) и секретный ввод | `core/vault.py`:1-75; `vault/pg.py`:1-8,37,58-128; `jobs/answers.py`:200-300; `core/execution.py`:75-92 |
| Личная ссылка календаря: распознать/409/не сохранить | `core/calendar_link.py`:126-157; `spaces/conversations.py`:88-103; `jobs/answers.py`:212-223 |
| Публичный поиск: минимизация, фиксированные хосты | `search/safety.py`:48-80; `search/service.py`:34-35,145 |
| Квоты LLM (день/минута/пул) | `settings.py`:74-80; `usage/service.py`:94-160; `agent/llm.py`:188 |
| Очередь Postgres (`SKIP LOCKED`), FTS `russian`, `NoopEmbedder` | `jobs/runner.py`:1-2,245; `agent/tools.py`:344-358; `db/models.py`:406; `core/embed.py`:7-15 |
| Один календарь на вуз; поиск источника по origin | `sources/calendar.py`:108-178; `tools_sources.py`:622-697,1064-1082; `spaces/service.py`:283-294,364-369,372-423 |
| Кнопки FR-054 (подпись/ход) | `tools_manage.py`:299-341; `db/models.py`:586 |
| Прочие лимиты: один браузер, 2 ручных входа, 2 ожидания ссылки, 30 источников в контексте, 120 запросов sync, 10 МиБ тело | `browser/gateway.py`:1988-2003; `jobs/runner.py`:83-92; `agent/turn_context.py`:58,112; `sources/fetch.py`:24-25,142-155 |
| Промпты: учебный фокус, честность, капча | `prompts/system.md`:5-14,31-38; `prompts/agent.md`:75 |
| Per-request auth bindings (policy, не значение) | `learn/policies.py`:11-69 `bind_parts` |

## 5. Уточнения и опровержения

1. **`user_action_required` не существует в коде (опровергнуто, №28).** В
   `core/errors.py`:12-50 такого кода нет; «нужен человек» — `SOURCE_BOT_PROTECTION`
   (`:17`) и блоки `LoginBlock`/`WaitBlock`. Термин только в документах
   (`security.md`:264,699) и входном анализе — это статус, а не машинный код.
2. **Адрес не всегда определяется origin (опровергнуто, №13).** `browse_source(url)`,
   `fetch_public_page(url)`, `connect_source(url)` получают адрес от модели; ограничение —
   allowlist и грант (`sources/fetch.py`:142-150, `browser/egress.py`:89-113). Устарел и
   `security.md`:602: он перечисляет `open(url)`, `list_links`, `extract`, которых нет.
3. **«Рецепт — единственный мост» устарело (№2).** Кроме рецепта переносятся пресет входа
   (`browser/presets.py`), карта сайта (`learn/sitemap.py`, `source_sitemaps`), browser plan
   v2 (`adr/0012`:30-32) и разовое извлечение документа (`sources/ingest.py`:319).
4. **Write-подтверждения не работают в production (уточнение №9).** Модель
   `ActionProposal`/`approval` и проверки есть, но исполнитель не зарегистрирован:
   `agent/actions.py`:17-18,111-117 («No executor is registered in production…»),
   `:384-398` — `HandoffBlock` вместо отправки; `system.md`:31 — «Вы только читаете».
   Deny-list (`:58-62`) уже формулировки: смена почты/телефона и `unenrol` не
   перечислены (уточнение №10).
5. **Бюджеты исследования — на срез (уточнение №7).** `DiscoveryBudget`
   (`learn/discovery.py`:74-81): 10/12/200/90 с/150k на срез; общий run — 100 шагов,
   2000 запросов, 1800 с, 1,5 млн (`settings.py`:58-65); срез переопределяется
   `botai_research_slice_*`.
6. **«В sync нет LLM» с исключением (уточнение №5).** Плановый sync рецепта модель не
   вызывает (`sources/runner.py`:603), но `INGEST` документа делает разовое извлечение
   моделью — разрешено `adr/0005`:40-42, `sources/ingest.py`:319,331.
7. **Чувствительные эндпоинты — только документ (уточнение №25).** L6
   (`security.md`:635) не имеет машинной проверки: grep `student-hub`/`students/me` по
   `backend/src` пуст; защита — lint/redaction. Помечено «Ф», но механизма в коде нет.
8. **SLA экрана — цель, а не гейт (уточнение №31).** `adr/0001`:47 говорит «иначе он не
   включается вовсе», но `browser/screen.py`:60-73,160 — метрики и адаптация (`окно
   кадров`, `stall_s`, `SLOW_MS`), а не отказ от включения; проверяется e2e
   (`testing.md`:142).
9. **«Отказ вне учёбы» — промпт (уточнение №35).** `agent.md`:75 задаёт правило, L2 B4
   проверяет поведение (`benchmark.md`:31,45); детерминированного классификатора нет.
10. **Минимизация ПДн в чате не реализована (уточнение №42).** L5 (`security.md`:634) —
    «П»; реализован в наблюдении (`learn/redact.py`) и поиске (`search/safety.py`), но не
    в чате (там только секреты — `agent/safety.py`).
11. **«Квоты… чат выключается» — отказ на запрос, не тумблер.** `usage/service.py`:
    117-160 поднимает `quota_exceeded` (429), проверка — `agent/llm.py`:188; sync идёт мимо
    `check_quota` и продолжает работать.
12. **`security.md` §5/§4 частично описывают старые органы управления.** В §5 нет
    `browse_source`/`fetch_public_page`; таблица C1–C19 (§8) — требования, не отчёт
    (`:672-674`), поэтому «проверено» по документу заключать нельзя.
13. **Документы HEAD и v4 расходятся по «as-is».** v4 README:3-8 объявляет удаление ADR;
    `AGENTS.md`/`README.md` уже переписаны в рабочем дереве (91 и 80 строк), хотя
    `docs/adr/*` ещё на месте; цитаты AGENTS/README здесь — из HEAD. Живой инцидент один:
    второй календарь на production дал замену ссылки (`testing.md`:322-324); пароль
    подтверждён кодом (`security.md`:658), но конфигурация сервера и бэкапы не проверялись.

## 6. Отдельно: ограничения, помеченные как временный долг

Эти ограничения **не** объявлены границей безопасности; они прямо помечены «текущее
ограничение, подлежит переработке» и должны исчезнуть в v4 (FR-055); остальные из §3 —
архитектурные: их отмена требует решения владельца, а не правки кода.

**43. Один календарь на вуз (`start_calendar_source`).**
Документы: `QUESTIONS.md`:32-38 — «Календарь вуза один (вторая ссылка предлагается как
замена первой, это выяснилось на production 31.10.2026)»; `requirements.md`:160 (FR-018)
и `:197` (FR-055) — «Текущее ограничение, подлежит переработке»; `overview.md`:98-100,
202-204; `plan/many-sources.md`:39-42, `:174-181` (меняющиеся тесты).
Код: `sources/calendar.py`:108-134 — «the calendar source of a university (one per
university)», `:125-134` — первый по `created_at` с `adapter == "ical"`; `:166-167` —
«left alone unless `replace_link`»; `tools_sources.py`:622-697 (`connect_calendar`,
`_calendar_exists`, `_by_link`), `:1069` — «Календарь вуза один»; для модели —
`prompts/agent.md`:31. Инцидент — `testing.md`:322-324. План: `many-sources.md` §3.2/§3.4
— `connect_calendar(connection_id=…)` без `replace_link`, новая идентичность календаря;
приёмка владельцем — `:207-208`.
Сломается: `sources/calendar.py`, описание `connect_calendar`, `prompts/agent.md`,
`tests/jobs/test_calendar_link.py`, `test_calendar_chat.py`,
`tests/e2e/test_calendar_link.py`, `tests/agent/test_live_calendar.py` (L2), UI
«Календарь (iCal)» (`apps/web/src/features/study/*`), фразы
`apps/web/src/features/data/chat-links.ts`. Риск: `replace_link` — единственный способ
заменить мёртвую ссылку; план это учитывает (`many-sources.md`:86-91).

**44. Идентичность источника = origin (`find_source`, `ensure_source`).**
Документы: `QUESTIONS.md`:36-38; `requirements.md`:178 (FR-036) — «"тот же" сегодня
определяется по сайту (origin) без пути и вида»; `overview.md`:98-101;
`agent-harness.md`:186-192; `plan/many-sources.md`:43-51.
Код: `spaces/service.py`:283-294 `find_source` — «The user's source on this site… oldest
first», ищет по `user_id + origin` **по всем пространствам** (нет `space_id`); `:372-423`
`ensure_source` — то же под `lock_origin`; `:302-326` `create_connection` дедуплицирует
по origin+kind, пока статус `draft`/`learning`; `tools_sources.py`:518-536 (поиск до
выбора вуза), `:543-576` (по названию); уникального ограничения в БД нет — только
логика (`db/models.py`:188-225). Последствия: вторая таблица на `docs.google.com` или
второй кабинет на одном хосте — не новый источник (`agent-harness.md`:186-192).
Сломается: `spaces/service.py` (`find_source`, `ensure_source`, `create_connection`,
`lock_origin`), `spaces/conversations.py` (`continue_source`, `_child_connection`),
`agent/tools_sources.py`, `tests/jobs/test_connect_source.py`
(`test_the_same_site_is_never_connected_twice`,
`test_a_source_asked_for_by_its_title_without_an_address_is_not_made_twice`),
`tests/spaces/test_university_units.py::test_find_source_by_origin_belongs_to_the_user`,
`tests/domain/test_merge.py` (склейка к N источникам готова),
`contracts/conversation.md` §«Календарь по ссылке». Открытый вопрос — рецепт на origin
(`many-sources.md`:228-229): два источника одного хоста делят ревизию; оставлено
осознанно (рецепт ≠ источник).

### Что помечено не как долг, но фактически им является

- **Исполнителя write-действий нет в production** (`agent/actions.py`:17-18), при этом
  FR-019/`approval` описаны как работающие: для v4 либо реализовать, либо переписать
  требования.
- **«Мои материалы» и split «документы и вложения»** — `overview.md`:88,102-108,
  `agent-hub-v3.md`:167-181; v4 README:82 отменяет, но пространство
  `university_id IS NULL` пока существует (`db/models.py`:105-133).
- **C1–C19 и L3 не подтверждены** (`security.md`:672-674; `testing.md`:224-226):
  таблицы — требования; живой вуз проверялся только для календаря (`testing.md`:320-324).
