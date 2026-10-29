# Рецепт v2: контракт фабрики

Статус: реализовано, выпуск проверяется отдельно. FR-008–FR-011, FR-018, FR-039–FR-042,
NFR-004–NFR-008, NFR-012–NFR-013. Авторитетная схема — `recipe.schema.json`.

## Совместимость и потребители

`version=1` сохраняет исходную строгую схему. Новые узлы разрешены только при `version=2`.
Затронуты W0 schemas/spec models, W2 Gateway/session, W3 author/lint/canary/recipe executor,
диагностика и synthetic fixtures. Старый binary не может читать v2; новый reader читает обе версии. Автор всегда получает
схему v2 (флаг производства удалён). Рецепты не обновляются автоматически и не переписываются
backfill. Окна дат, `uiUrl`, `links` и `attributes` ниже существуют только в v2; v1 их не знает и
отклоняет схемой и typed-моделью. Доменные сущности расширены в `domain.schema.json`
(`openUrl`, поля карточки занятия).

## Авторизация

`auth.type=session`, `version=1`, `bindings` содержит до четырёх сочетаний `cookies:boolean`
и `bearer:none|observedAuthorization`. Request/GET-bootstrap ссылается через `authBinding`.
Audience всегда exact `recipe.origin`, а не allowlist: credentials не отправляются на другой
origin даже при разрешённом redirect. Значений токена, storage key, произвольного header или
refresh URL в спецификации нет. Автор выбирает только ephemeral ID наблюдаемого запроса;
executor проверяет происхождение/метод/путь/параметры, компилирует binding и удаляет evidence ID.
Отказ конкретного endpoint не доказывает утрату браузерного входа. Refresh использует LoginFlow.

## Вложенные коллекции, время и повторения

Ответ выбирает прежний `itemsPath`/`itemsSelector` либо `collections` глубиной 1–4, с уникальными
alias `as` (кроме `item/root`). Каждый уровень разворачивается только внутри предыдущего.
`from` выбирает leaf `item`, `root` или ancestor alias. `compose.type=dateTime` соединяет
наблюдаемые дату и время в явной IANA timezone; `dayOffset` и `fold` задаются явно.
Произвольных выражений, кода и рекурсивного поиска нет. Reference проверяется до чтения.

`expand.type=weekly` разрешён только для занятий: source start/until, ISO weekdays, start time,
end time либо duration, explicit timezone, horizon до 93 дней, до 512 событий серии. Интервал
2–4 недели требует source anchor date; исключения и дополнительные даты берутся из источника.
ID экземпляра зависит от стабильного ID серии и номинальных локальных даты/времени, не от
индекса или окна. Неизвестная/неоднозначная дата и переполнение не заменяются догадкой.

Лимиты: 10 000 узлов коллекций, 5000 leaf/instance записей endpoint и 20 HTTP страниц.
Пагинация считает верхнюю коллекцию, не количество развёрнутых занятий. Limit/cycle не
обозначаются полной выборкой; предыдущие отсутствующие строки не помечаются пропавшими.

## Browser replay

`request.browserPlan` версии 1 разрешён только у rendered GET HTML, без body. План — до
восьми структурных шагов с frame chain до трёх same-origin frames: проверенная ARIA tab,
native date, native select, наблюдаемый `rel=next` и bounded ready wait. Date использует
existing date builtin; select — наблюдаемую опцию. Date/select требуют owning region и
проверяемый GET/effect. Custom calendar или неизвестная кнопка не получают generic click.

Модель выбирает current action/plan reference, executor компилирует structural descriptors.
В новом браузере перед каждым действием повторно проверяются uniqueness, тип, семантика,
frame/origin и ожидаемый эффект. CAPTCHA/POST/WS не становятся разрешёнными от наличия плана.
Пустой ответ требует settled structure/empty evidence; timeout/cap/cycle остаётся неполным.

## Окна дат и адрес страницы

`pagination.type=dateWindow` — `{param, stepDays 1..31, windows 1..12, startOffset -4..0}`.
Окно i двигает **все** date-шаблоны query на `(startOffset + i) × stepDays` дней в часовом поясе
источника, поэтому `{weekStart}` сдвинутой даты — понедельник той недели. `param` — ключ query, чей
шаблон состоит только из date-builtin (`{today}`, `{weekStart}`, `{plusDays:N}`, `{minusDays:N}`).
Обход обязан содержать окно 0 (`startOffset + windows − 1 ≥ 0`): компилятор проверяет, что одно из
окон воспроизводит значение, которое браузер реально отправил. Только GET без `render`,
`browserPlan`, `body` и `expand`; параметры `limit`, `maxPages`, `cursorPath` и `totalPath` вместе с
окном недопустимы. Пустое окно не завершает обход; ответ, повторяющий окно целиком, —
`no_progress`; сбой окна — сбой endpoint; полный обход без записей — `empty`. Для расписания
автор по умолчанию берёт `startOffset: -1, windows: 6, stepDays: 7`.

`endpoint.uiUrl: {path, query}` — страница источника, где студент видит записи (шаблоны те же, что у
запроса, сдвигаются тем же окном). Выводит компилятор из страницы, чей скрипт сделал запрос;
автор его не задаёт. Сегменты пути — слова без идентификаторов; значения query — date-шаблоны,
короткие числа или слова. Executor кладёт результат в `Record.open_url` → `open_url` доменной
строки → `provenance.openUrl` API. У HTML-endpoint `open_url` — сама загруженная страница.

## Секции карточки занятия

Только `scheduleEvents`, только v2, рядом с `fields` в `response`:

- `links` — до 4 элементов `{url: <скаляр>, label: <скаляр> | "общий текст ≤40"}`; пустой url
  пропускается; в записи остаются только http(s)-ссылки без дублей;
- `attributes` — до 8 элементов `{label: "общий текст ≤40", path|selector, from?, …}`; подпись не
  содержит значения из данных (lint сверяет с наблюдёнными).

В `fields` добавлены скаляры `kindText`, `address`, `group`, `lessonNumber`; имена `links` и
`attributes` в `fields` запрещены.

## Выпуск и откат

Reader и producer v2 выпускаются вместе (флаги `BOTAI_RESEARCH_ENABLED` и
`BOTAI_FACTORY_V2_ENABLED` удалены, оба режима были включены в production). Откат на binary до v2
после активации требует адресного отката ревизий/парковки подключений; нельзя просто подставить
старый образ.

Основы времени: [Python zoneinfo](https://docs.python.org/3.13/library/zoneinfo.html),
[datetime.fold](https://docs.python.org/3.13/library/datetime.html#datetime.datetime.fold).
Playwright: [frames](https://playwright.dev/python/docs/frames); использование API сверяется
с закреплённой в проекте версией, а не только с обновляемым сайтом документации.
