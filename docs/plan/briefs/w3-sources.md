# W3 — Источники, обучение, синхронизация, починка

Сначала прочитай [общие правила](README.md), затем [ADR-0002](../../adr/0002-recipe-as-bridge.md),
[ADR-0005](../../adr/0005-source-adapter-factory.md), [HLD §2–3](../../architecture/hld.md),
[contracts/adapter-spec](../../../contracts/adapter-spec.schema.json) и
[recipe](../../../contracts/recipe.schema.json).

## Владеешь

`backend/src/botai/{sources,learn}/`, `backend/tests/{sources,learn}/`,
`backend/tests/fakeuni/{api,pages,moodle,ical}.py`.

## Цель

Любой вуз и любой документ подключаются **спецификацией**, а не кодом. Агент один раз пишет
спецификацию, дальше sync идёт без модели. Если вуз что-то поменял — агент сам чинит.

## Срезы

1. **Фабрика и адаптеры** (`sources/factory.py`, `sources/adapters/`): протокол `SourceAdapter`
   (`fetch(ctx) -> AdapterResult` с записями по видам и происхождением). `recipe`: шаблоны
   `{today}`, `{nowUnix}`, `{plusDays:N}`, `{minusDays:N}`, параметры из `bootstrap`; пагинация
   offset/page/cursor; ответы JSON (JSON Pointer) и **HTML** (`selectolax`, `itemsSelector`,
   `selector`, `attr`, `pattern`); трансформации. `moodle_ws`: только функции из allowlist
   схемы, токен из Vault (`kind=token`). `ical`: ссылка из Vault (`kind=ical_url`), VEVENT →
   занятие или дедлайн по `classify`. `document`: PDF (`pypdf`), DOCX (`python-docx`), XLSX/CSV
   (`openpyxl`), HTML, текст → `documents` + `chunks`; таблица → строки по спецификации, фильтр
   строк студента — по параметру профиля. Все HTTP — через `core.http` (`build_client` с
   allowlist подключения и `egress_proxy_url`, `request_with_retries`), сессия — из Vault.
2. **Runner, Normalizer, sync** (`sources/runner.py`, `sources/normalize.py`, обработчик
   `JobKind.SYNC` в `sources/handlers.py`): идемпотентный upsert в таблицы домена по
   `(connection_id, external_id)`; `external_id` из поля или хеша `identity`; `seen_at`; записи,
   не встреченные в полном успешном прогоне, помечаются (не удаляются молча); частичная ошибка не
   трогает прежний снимок. Признак протухания сессии (401/403/редирект на вход по пресету) →
   `services.sessions.ensure(...)` и одна повторная попытка; ответ не сошёлся со спецификацией →
   статус `repairing` и задание `repair`. `next_sync_at` по `freshness`.
3. **Lint спецификаций** (`learn/lint.py`): JSON Schema (`botai.contracts.schemas.errors`), затем
   запрет личных значений: e-mail, телефоны, JWT, числа из ≥ 6 цифр, любая строка, равная
   наблюдавшемуся значению из ответов этого пользователя; запрет абсолютных URL, `..`, хостов вне
   origin; имена параметров по схеме. Отказ — `recipe_rejected` с причинами без значений.
4. **Explorer** (`learn/explorer.py`, обработчик `JobKind.CONNECT` и `LEARN` в
   `learn/handlers.py`). Модель здесь — **автор спецификации**, а не свободный браузерный агент:
   (а) `services.sessions.ensure` → сессия; (б) детерминированный обход: стартовая страница,
   ссылки меню с того же origin (ограничение по числу), сбор `network_log` и снимков; (в)
   редактирование наблюдений: личные значения → плейсхолдеры, обрезка; (г) `services.llm.complete`
   с `purpose="learn"` и `json_schema` спецификации — по одному виду данных за вызов (расписание,
   дедлайны, …), промпты — в `learn/prompts/*.md`, без названий вузов; (д) lint → canary на этой
   сессии → `SummaryBlock` «вот что собрано» и `ChoiceBlock`/`ConfirmBlock` для сомнительного
   (поток рассуждения — `ThinkingBlock` на каждом этапе); (е) подтверждение → ревизия `active`,
   подключение `active`, первый sync. Ничего не нашёл — честный вопрос пользователю
   (`InputBlock` «дай ссылку на страницу с дедлайнами»). Реестр вуза: название, origins,
   предупреждение о сторонних клиентах, если агент его нашёл на сайте.
5. **Ревизии, canary, откат, repair** (`learn/revisions.py`, обработчик `JobKind.REPAIR`): одна
   активная ревизия на `(origin, adapter)`; canary — прогон новой ревизии на сессии автора и
   сравнение ключевых полей с последним подтверждённым снимком; repair — сравнение наблюдаемых
   запросов и разметки с текущей ревизией, новая ревизия через тот же путь, что и обучение;
   короткий итог текстом (`TextBlock`: что поменялось у вуза, что исправлено). Ежедневная
   канарейка — задание `repair` в режиме проверки.
6. **Документы (приоритет 2)** — `sources/router.py` и обработчик `JobKind.INGEST`: ссылка или
   загрузка → `document`-спецификация (тип определяется автоматически, для таблиц агент
   предлагает `table`-спецификацию и спрашивает про столбцы через `ChoiceBlock`).

## Тесты (на `fakeuni` и `ScriptedLlm`)

Рецепт JSON и HTML для `fakeuni` даёт ожидаемые записи; повторный sync не создаёт дублей;
протухшая сессия → `ensure` → успех; смена пути эндпоинта в `fakeuni` → `repairing` → repair с
записанным ответом модели → новая ревизия → данные снова на месте; lint отвергает спецификацию с
e-mail студента или чужим хостом; Moodle WS и iCal на фикстурах. Для `BrowserGateway` и
`SessionProvider` используй тестовые реализации из `core` (W2 пишет настоящие параллельно).

## Не делаешь

Вход в вуз и браузер (W2), очередь и SSE (W1), чат (W5), интерфейс (W4).
