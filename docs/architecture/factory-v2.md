# Фабрика источников v2

Статус на 29.10.2026: **код собран в интеграционной ветке, выпуск проверяется**.
Ограниченные L0/L1-проверки компонентов выполнены; полный CI, сквозной v2-сценарий и
production-приёмка фиксируются отдельно в [testing](../development/testing.md).
Настоящие вузы этим документом не объявляются проверенными.
[ADR-0012](../adr/0012-verified-source-factory-v2.md) дополняет
[длительное исследование](source-research.md). Авторитетный публичный контракт —
[recipe.schema.json](../../contracts/recipe.schema.json) и [его описание](../../contracts/recipe-v2.md).

## Контур обучения и контур исполнения

Модель выбирает наблюдаемый способ чтения и описывает данные. Доверенный код компилирует
выбор, проверяет спецификацию и исполняет canary. Общая ревизия содержит только правила;
каждый студент исполняет их со своей зашифрованной сессией. Sync не вызывает модель.

~~~mermaid
flowchart LR
    Browser[Свежие DOM и запросы] --> Evidence[Executor-owned evidence]
    Evidence --> Model[Выбор reference и структуры данных]
    Model --> Compile[Compiler: auth binding или browser plan]
    Compile --> Lint[Schema и lint]
    Lint --> Canary[Свежая scoped canary]
    Canary -->|static findings| Model
    Canary -->|прошла| Confirm[Итог и подтверждение]
    Confirm --> Recipe[Общая ревизия v1 или v2]
    Recipe --> Replay[Детерминированный sync]
    Vault[Своя сессия из Vault] --> Replay
    Replay --> Domain[Normalizer и provenance]
~~~

**BOTAI_FACTORY_V2_ENABLED=false** по умолчанию ограничивает **производство** новых v2-рецептов.
Чтение уже сохранённых v1/v2 не зависит от этого флага. Отдельный
**BOTAI_RESEARCH_ENABLED=false** управляет возобновляемым исследованием и парковкой его
незавершённых Job; он не заменяет producer gate.

## Авторизация подтверждается для конкретного запроса

Observer сохраняет временные executor-owned факты запроса: exact origin, метод, path/query,
успешный status, наличие Cookie и Authorization, схему Authorization и признак полной
проверки заголовков. Значения credentials в наблюдение модели не передаются.

При включённом producer модель указывает **requestEvidenceId** на уровне endpoint;
GET-bootstrap требует собственного evidence ID. Compiler сопоставляет его с текущими
фактами и проверяет origin, метод и path/query. Авторский auth не становится разрешением.
Compiler сам создаёт auth.type=session, version=1, до четырёх bindings:
cookies:boolean и bearer:none|observedAuthorization. Request/bootstrap получает
authBinding; временный evidence ID удаляется до lint/checkpoint/сохранения рецепта.

Так выражаются Cookie, Bearer, оба вместе и отсутствие credentials. Наличие токена в
storage само по себе не доказывает, что конкретный endpoint принимает Bearer.
Граница credentials — exact recipe.origin, включая порт, а не весь allowlist подключения.
Bearer хранится с origin scope в Vault; на другом origin, в том числе после redirect,
он не появляется. Комбинация разрешений не содержит storage key, токена, Cookie header
или произвольного refresh URL. Повторный вход остаётся задачей LoginFlow.
При расширении существующего recipe прежняя policy переносится в эквивалентный per-request
binding; это не новое предположение о credentials. Непредставимый старый auth mode отклоняется,
а итоговый scoped recipe проходит общую canary.

До закрытия исследовательского браузера свежая сессия экспортируется в Vault. Canary и
первый sync используют её: SPA могла обновить cookies, bearer и storage после входа.
Отказ одного endpoint не доказывает утрату браузерной сессии.

## Детерминированные данные

| Узел v2 | Исполнение и ограничения |
|---|---|
| collections | Одна цепочка из 1–4 уровней JSON-массивов или HTML-коллекций; следующий уровень читается внутри предыдущего. Выбирается вместо itemsPath/itemsSelector, без Cartesian join и рекурсивного поиска |
| as / from | Уникальные aliases, кроме зарезервированных item/root. Поле читает leaf item, корень или объявленного родителя; неизвестный alias отклоняется до сети |
| compose.type=dateTime | Scalar date + scalar time, явная IANA timezone, dayOffset 0/1. Требуется наблюдаемый четырёхзначный год; время — часы/минуты с необязательными секундами |
| fold | reject по умолчанию; неоднозначное локальное время допускается только с earlier/later. Несуществующее время при переводе часов отклоняется |
| expand.type=weekly | Только scheduleEvents: source start/until, ISO weekdays 1–7, start time, end time либо duration 1–1440 минут; явная timezone |
| Горизонт recurrence | Движущийся date builtin today/weekStart/plusDays/minusDays, 1–93 дня, пересечённый с диапазоном источника. Интервал 1–4 недели; для 2–4 нужен наблюдаемый anchorDate |
| Исключения | Наблюдаемые excludeDates/includeDates; исключение имеет приоритет. Ночной конец требует явного endDayOffset, полный RFC 5545 не заявляется |

Скалярные операнды не содержат вложенного compose, ссылок на вычисляемые поля, eval,
произвольного кода или сетевых вызовов. Lint рекурсивно проверяет JSON Pointer, CSS,
атрибуты, ограниченные regex, aliases и timezone. Личные значения не должны попадать
в новые вложенные selector/path/alias nodes общей спецификации.

На endpoint допускается 10 000 посещённых узлов коллекций и 5000 leaf/instance записей,
на серию — до 512 occurrences. Переполнение DSL отклоняет весь endpoint, без усечения
успешного результата. Пагинация считает **верхнюю коллекцию**, а не развёрнутые занятия;
её предел — 20 HTTP-страниц в пределах общего бюджета SourceClient.

В v2 явно заданный identity имеет приоритет перед externalId; в v1 сохранён прежний
порядок. Стабильность выбранного source ID проверяется canary/фикстурами, а не обещается
самим наличием поля. ID weekly instance зависит от ID серии, номинальной локальной
даты/времени и timezone, без индекса или query horizon. Изменение конца занятия не
создаёт новый instance ID. Сохраняются обычные source_url/fetched_at/spec_revision.

## Проверенные браузерные действия

Planner получает короткоживущие references; он не пишет selectors, JavaScript или общий
click-план. После успешного действия executor может сохранить структурный BrowserPlan
версии 1. Author выбирает browserPlanRef, compiler переносит проверенный план в
rendered GET HTML recipe. Простой render=true без плана требует evidence document-запроса
той же страницы. HTTP body и произвольные browser actions здесь не допускаются.

| Capability | Что действительно поддерживается |
|---|---|
| activate_tab | Наблюдаемая ARIA tab с принадлежащим ей panel; повторная проверка типа, target и видимости |
| set_date | Нативный input[type=date] вне формы, с label и aria-controls на owning region; поддерживаемое date builtin в пределах min/max |
| select_option | Нативный одиночный select вне формы, owning region и наблюдаемая разрешённая option |
| next_link | Наблюдаемая same-origin ссылка rel=next, без download; ограниченная пагинация |
| wait_ready | Наблюдаемая region/table/grid/panel и проверка завершения чтения |
| frame | Переход в наблюдаемый same-origin frame; portable chain глубиной до трёх |

План содержит до восьми шагов. При replay в новом handle заново проверяются uniqueness,
tag/role/label/ownership, frame и origin. Date/select требуют наблюдаемого сетевого чтения
и эффекта в owning region; изменение значения control само по себе не является успехом.
Проверяются in-flight requests, aria-busy, изменение/стабилизация содержимого. Пустой
результат portable replay требует наблюдаемого explicit empty target и settled state.

Предел rendered fetch общий для endpoint-ов: три страницы и 30 секунд, HTML до 2 МиБ.
Повтор страницы, существующая next-ссылка за пределом cap, отсутствие эффекта и timeout
означают неполноту. Такой kind не получает отметку полной выборки и не помечает старые
строки пропавшими.

Custom date picker, contenteditable, произвольная кнопка, неизвестный JS callback,
cross-origin frame и произвольный POST/WS не получают разрешение от наличия плана.
Браузер исполняет код самой страницы внутри прежних read-only/egress границ; модель
не может расширить их или представить write-действие как read capability.

## Проверка, исправление и восстановление

На вид данных допускаются до трёх кандидатов и два author/canary-раунда. Внутри author
есть до двух попыток JSON/lint. Обратная связь canary ограничена 12 findings: статический
код, kind, HTTP/browser transport, status, retryable и счётчики, без URL, тела и exception
text. Проверяются следующие кандидаты; в legacy v1 остаётся ограниченная проверка
поддерживаемого альтернативного auth mode. У v2 auth policy компилируется из evidence.

Auth rejection, shape failure, normalization rejection, empty result, limit,
no_progress и unsupported capability различаются. Структурная пустота источника не
равна пригодной пустой canary: для требуемого вида нужны нормализуемые записи; sync
отдельно сравнивает пустой ответ с прежним снимком и не удаляет его молча.
После отказов выполняется ограниченное исправление, затем сообщается конкретное
ограничение или запрашивается действительно недостающее действие человека.

Сохранённый candidate после рестарта остаётся предложением: свежие lint/canary обязательны.
DOM и p/r/l/t refs не восстанавливаются как доказательства. Фазы, frontier и portable
descriptors живут в зашифрованном checkpoint; сессия отдельно в Vault.

TTL checkpoint удаляет знания, **но не расход того же Job**. В той же fenced/CAS-транзакции
сохраняется несекретная projection _research_usage: tokens, active_ms, requests, steps,
включая неопределённые reservations. После TTL начинается новое наблюдение с остатком
общего бюджета; активационная квитанция тоже не зависит от TTL.
Формат, неизменяемый claim и input revision проверяются до публикации. Подробности —
[source-research](source-research.md); порядок включения и совместимый откат —
[deployment](../plan/deployment.md).
