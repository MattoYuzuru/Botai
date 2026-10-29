# Рецепт v2: контракт фабрики

Статус: контракт подготовлен, реализация и выпуск проверяются отдельно. FR-008–FR-011,
FR-018, NFR-004–NFR-008, NFR-012–NFR-013. Авторитетная схема — `recipe.schema.json`.

## Совместимость и потребители

`version=1` сохраняет исходную строгую схему. Новые узлы разрешены только при `version=2`.
Затронуты W0 schemas/spec models, W2 Gateway/session, W3 author/lint/canary/recipe executor,
диагностика и synthetic fixtures. REST/SSE и доменные сущности не меняются. Старый binary
не может читать v2; новый reader обязан читать обе версии независимо от флага производства.
Автор начинает создавать v2 только после включения отдельного operator flag. До этого
author schema остаётся v1. Рецепты не обновляются автоматически и не переписываются backfill.

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

## Выпуск и откат

Сначала выпускается совместимый reader с producer flag off и проверяется CI/health. Затем
подготавливается rollback-образ, тоже читающий v2, и включается producer. Выключение producer
не отзывает поддержку чтения активированных v2. Откат на binary до v2 после активации требует
адресного отката ревизий/парковки подключений; нельзя просто подставить старый образ.

Основы времени: [Python zoneinfo](https://docs.python.org/3.13/library/zoneinfo.html),
[datetime.fold](https://docs.python.org/3.13/library/datetime.html#datetime.datetime.fold).
Playwright: [frames](https://playwright.dev/python/docs/frames); использование API сверяется
с закреплённой в проекте версией, а не только с обновляемым сайтом документации.
