# Botai — контракты

Статус: **accepted**, 2026-10-27. Это замораживаемая часть: пять исполнителей
работают параллельно именно потому, что эти схемы согласованы до кода.
Изменение контракта — отдельный PR с ревью и отметкой в разделе 8.

## 0. Общие правила

- Все идентификаторы — ULID-строки. Все времена — ISO 8601 с таймзоной.
- Учебное время хранится в UTC, отображается в таймзоне пространства
  (`Europe/Moscow` по умолчанию).
- Ошибка API — `application/problem+json`: `{type, title, status, code, detail, traceId}`.
  `code` — машинный, из перечня в разделе 7.
- Никакое поле ответа API никогда не содержит секрет, cookie, пароль или токен.

## 1. Домен (`contracts/domain.schema.json`)

Общие поля происхождения у всех сущностей:

```ts
type Provenance = {
  sourceId: string;      // connectionId
  sourceUrl: string;     // исходная страница/эндпоинт
  fetchedAt: string;     // когда получено
  recipeRev?: number;    // какой ревизией рецепта
};
```

| Сущность | Поля |
|---|---|
| `Space` | `id, userId, universityId, title, accentToken, timeZone, createdAt` |
| `Connection` | `id, spaceId, origin, kind, title, status, lastSyncAt, nextSyncAt, recipeId, error?` |
| `Course` | `id, connectionId, externalId, title, code?, term?, description?, url?, provenance` |
| `ScheduleEvent` | `id, connectionId, externalId, courseId?, title, kind (lecture/seminar/exam/other), startsAt, endsAt, location?, teacherIds[], format (offline/online/hybrid), recurrence?, provenance`. Хранится по источникам; в ответе чтения склеивается (`ScheduleEventView`: `sources[]`, `disagreement?`, FR-054) |
| `Assignment` | `id, connectionId, externalId, courseId?, title, description?, url?, maxScore?, provenance` |
| `Deadline` | `id, connectionId, externalId, assignmentId?, courseId?, title, dueAt, state (open/submitted/graded/missed/unknown), provenance` |
| `Grade` | `id, connectionId, externalId, courseId?, assignmentId?, value, scale?, weight?, gradedAt?, provenance` |
| `Person` | `id, connectionId, externalId, name, role (teacher/assistant/other), contacts?, provenance` |
| `Location` | `id, connectionId, externalId, title, building?, address?, provenance` |
| `NewsItem` | `id, connectionId, externalId, title, body?, publishedAt, url?, provenance` |
| `Document` | `id, spaceId, connectionId?, kind (file/sheet/page), title, url?, mime?, fetchedAt, hash` |
| `Chunk` | `id, documentId, ord, text, embedding, tokens` |
| `Fact` | `id, spaceId, question, answer, sources[], confirmedAt, staleAfter, confidence` |

`Connection.status`: `draft | learning | active | syncing | stale | needs_reauth | error | disabled`.
`Connection.kind`: `lms | schedule | portal | sheet | document | mail | other`.

Правило уникальности: `(connectionId, externalId)` уникально для каждой
сущности. Если у источника нет стабильного id, Normalizer строит
`externalId = sha256(стабильные поля)` и это фиксируется в рецепте.

## 2. Рецепт (`contracts/recipe.schema.json`)

Один рецепт = один origin. Личных значений в рецепте нет никогда.

```json
{
  "version": 1,
  "origin": "https://my.example.ru",
  "revision": 3,
  "learnedAt": "2026-10-26T10:00:00.000Z",
  "auth": { "type": "cookie" },
  "bootstrap": [
    { "id": "account", "method": "GET", "path": "/api/account/me",
      "extract": { "accountId": "/sub" } }
  ],
  "endpoints": [
    {
      "id": "schedule",
      "kind": "scheduleEvents",
      "request": {
        "method": "GET",
        "path": "/api/students/me/timetable",
        "query": { "from": "{today}", "to": "{plusDays:14}" }
      },
      "pagination": { "type": "offset", "param": "offset", "limit": 50, "maxPages": 5 },
      "response": {
        "itemsPath": "/items",
        "fields": {
          "externalId": { "path": "/id" },
          "title":      { "path": "/course/name" },
          "startsAt":   { "path": "/start", "transform": "iso" },
          "endsAt":     { "path": "/end",   "transform": "iso" },
          "location":   { "path": "/room/title" }
        }
      }
    }
  ],
  "validation": { "requireKinds": ["scheduleEvents"], "minItems": 1 },
  "freshness": { "syncEveryMinutes": 180, "staleAfterMinutes": 720 }
}
```

- `kind` ∈ сущности домена (`courses`, `scheduleEvents`, `deadlines`, `grades`,
  `assignments`, `people`, `locations`, `news`).
- Пути значений — JSON Pointer. Шаблоны: `{today}`, `{nowUnix}`, `{plusDays:N}`,
  `{param}` из `bootstrap.extract`.
- `transform` ∈ `iso | unixSeconds | unixMillis | trim | number | bool | html2text`.
- Разрешены `GET`; `POST` — только из явного allowlist read-only методов в коде
  (например AJAX-чтение Moodle), с пометкой `readOnlyPost: true`.
- **Lint рецепта (обязательный, блокирует сохранение):** нет значений длиннее
  N, похожих на токен; нет подписанных URL; нет email, ФИО, числовых id
  конкретного человека; нет заголовков `Cookie`/`Authorization`.

### JSON и HTML

`response.format` — `json` (поля по JSON Pointer) или `html` (элементы списка по
`itemsSelector`, поля по CSS-селектору, необязательно `attr` и `pattern`). Схема —
[`recipe.schema.json`](../../contracts/recipe.schema.json). HTML хрупче JSON: вёрстка меняется
чаще API, поэтому у HTML-рецептов канарейка ежедневная, а поломка сразу запускает repair
([ADR-0005](../adr/0005-source-adapter-factory.md)).

Рецепт — один из четырёх видов спецификации адаптера (`recipe`, `moodle_ws`, `ical`,
`document`), см. [`adapter-spec.schema.json`](../../contracts/adapter-spec.schema.json).

### Версия 2

V1 остаётся строгим исходным контрактом. V2 добавляет per-request authBinding, compiler-owned
Cookie/Bearer policy, nested collections/from, dateTime composition, bounded weekly expansion
и portable browserPlan; с 2026-10-30 также окна дат (`pagination.type=dateWindow`), адрес страницы
(`endpoint.uiUrl`) и секции карточки занятия (`response.links`, `response.attributes`). Новый reader
принимает обе версии; автор всегда создаёт v2 (флаги `BOTAI_RESEARCH_ENABLED` и
`BOTAI_FACTORY_V2_ENABLED` удалены). Значения credentials и временные evidence refs
в recipe не сохраняются. Подробности — [контракт v2](../../contracts/recipe-v2.md) и
[реализация/границы](factory-v2.md).

### Жизненный цикл ревизии

`draft` → `canary` (прогон на сессии автора, сравнение с подтверждённым итогом)
→ `active` → `deprecated` / `rolled_back`. Активная ревизия у origin одна.

## 3. Пресет входа (`contracts/login-preset.schema.json`)

```json
{
  "id": "example-lms",
  "name": "Пример — LMS",
  "start": { "url": "https://lms.example.ru/" },
  "allowHosts": ["lms.example.ru", "id.example.ru"],
  "success": { "probe": { "method": "GET", "url": "https://lms.example.ru/api/me", "expectStatus": 200 } },
  "expired": { "probe401": ["/api/me"], "redirectPathPrefix": ["/login/"] },
  "mfa": { "possible": "unknown", "automate": false },
  "captcha": { "possible": true, "automate": false },
  "capture": { "cookiesFor": ["lms.example.ru"], "bearerFrom": "none", "storage": "encrypted" },
  "mode": "assisted",
  "steps": [
    { "id": "credentials", "match": { "urlHost": "id.example.ru" },
      "fields": [
        { "role": "username", "selector": "#username", "label": "Почта" },
        { "role": "password", "selector": "#password", "label": "Пароль" }
      ],
      "submit": "button[type=submit]" },
    { "id": "otp", "match": { "selectorPresent": "input[name=otp]" },
      "fields": [ { "role": "otp", "selector": "input[name=otp]", "label": "Код из SMS" } ],
      "submit": "button[type=submit]" }
  ]
}
```

- `automate: false` неизменяемо для `mfa` и `captcha`: их всегда проходит человек.
- `mode`: `assisted` (по умолчанию: поля шагов зеркалятся нативными полями BotAI,
  сервер вводит значения; пароль сохраняется в Vault для автовхода) или `screen`
  (шаги проходит человек на запасном экране).
- `steps` — выученные шаги формы: где поля логина, пароля, кода и кнопка отправки.
  Без личных значений; общие на origin. Схема —
  [`login-preset.schema.json`](../../contracts/login-preset.schema.json).
- `allowHosts` становится allowlist'ом EgressProxy для этого подключения.

## 4. Блоки интерфейса агента (`contracts/agent-ui.schema.json`)

Агент не задаёт вопросы свободным текстом, когда нужен структурный ответ.
Клиент рисует блок своими компонентами из дизайн-системы.

```ts
type AgentUiBlock =
  | { type: "text";     id: string; markdown: string; quickReplies?: string[] }
  | { type: "thinking"; id: string; stage: string; detail?: string; at: string }
  | { type: "choice";   id: string; question: string; mode: "single" | "multi";
      options: { value: string; label: string; hint?: string }[]; required: boolean }
  | { type: "input";    id: string; question: string; placeholder?: string;
      inputKind: "text" | "url" | "number" | "date" | "email" | "password" | "code";
      required: boolean; secret?: boolean; autocomplete?: string }
  | { type: "confirm";  id: string; question: string;
      confirmLabel: string; cancelLabel: string }
  | { type: "summary";  id: string; title: string;
      groups: { title: string; items: { label: string; value: string; sourceUrl?: string }[] }[] }
  | { type: "approval"; id: string; proposal: ActionProposal }
  | { type: "wait";     id: string; message: string; expiresAt?: string }
  | { type: "handoff";  id: string; title: string; url: string; text: string }
  | { type: "login";    id: string; connectionId: string; ticket: string; wsUrl: string }
  | { type: "plan";     id: string; title: string;
      steps: { id: string; title: string; state: "pending" | "active" | "done" | "skipped" }[] }
  | { type: "task";     id: string; jobId: string; kind: "connect" | "repair" | "sync" | "ingest";
      title: string; state: string; threadId: string; connectionId?: string }
  | { type: "attachment"; id: string; attachmentId: string; name: string; mime: string;
      size: number; kind: "image" | "document" | "audio" | "other" };

type AgentUiAnswer = { blockId: string; values: string[] | string | boolean };
```

`quickReplies` (≤4 по ≤60 знаков) — кнопки быстрых ответов под последним ответом; `plan` обновляется
блоком с тем же `id`; `task` — карточка фоновой задачи, ведущая в тред, где она живёт; `attachment` —
вложение сообщения студента. В `markdown` допустимы GFM-таблицы и блок кода ` ```chart ` с JSON
графика; ссылки приложения — `botai://threads|connections|documents|attachments/{id}`.

Правила: `secret: true` — значение идёт API → Vault → воркер, мимо LLM и событий; `login` —
только запасной экран; `handoff` — write-действие, которое студент отправит сам. Клиент никогда не исполняет код из блока; `markdown` рендерится без
HTML и без ссылок-действий; блок с `required: true` блокирует продолжение
задания, и это видно в UI.

## 5. Предложение действия (write)

```ts
type ActionProposal = {
  id: string;
  connectionId: string;
  intent: string;              // «подать заявку на транскрипт на английском»
  request: {
    method: "POST" | "PUT" | "PATCH";
    url: string;               // должен попадать в allowHosts подключения
    headers: Record<string, string>;   // без секретов; секреты подставит broker
    bodyKind: "form" | "json" | "multipart";
    fields: { name: string; label: string; value: string; editable: boolean }[];
  };
  reversible: boolean;
  expiresAt: string;
};
```

Исполнение: `POST /actions/{id}/approve` с хешем подтверждённых полей.
Подтверждение одноразовое; истекает; расхождение хеша — отказ `action_changed`.

## 6. HTTP и потоки

### REST (`contracts/openapi.yaml`, набросок)

| Метод и путь | Назначение |
|---|---|
| `POST /api/v1/auth/invite/redeem` | Активация приглашения |
| `GET /api/v1/me` | Профиль, пространства, квоты |
| `POST /api/v1/spaces` · `GET /api/v1/spaces` | Пространство вуза студента (повтор возвращает то же, `200`) · все его вузы с `shortTitle` и `aliases` |
| `GET /api/v1/spaces/{id}/connections` | Источники вуза: человеческий `title`, `kind`, `entryUrl`, `threadId`, `activeTask`. Источник создаёт агент в треде, а не REST |
| `GET /api/v1/connections/{id}` · `DELETE` | Состояние, отключение |
| `POST /api/v1/connections/{id}/login-session` | Открыть сессию входа (вернёт `wsUrl`) |
| `POST /api/v1/connections/{id}/sync` | Ручная синхронизация |
| `GET /api/v1/schedule?from&to&spaceId?` | Общий календарь; занятия нескольких источников одного вуза склеены (`sources`, `disagreement`, FR-054) |
| `PUT /api/v1/spaces/{id}/primary-source` · `POST /api/v1/connections/{id}/disable` · `/enable` | Основной источник расписания вуза; обратимое отключение и включение источника (FR-054) |
| `GET /api/v1/deadlines` · `/courses` · `/grades` · `/people` | Домен |
| `POST /api/v1/chat/threads` · `POST /api/v1/chat/threads/{id}/messages` | Чат |
| `POST /api/v1/chat/threads/{id}/answers` | Ответ на `AgentUiBlock` |
| `POST /api/v1/spaces/{id}/documents/url` · `/documents/upload` · `DELETE /api/v1/documents/{id}` | Подключить таблицу/документ/ссылку (в том числе Google по ссылке) или файл |
| `POST /api/v1/attachments` · `GET /{id}` · `GET /{id}/content` · `DELETE /{id}` | Файлы чата до 20 МиБ: тип по байтам, отдача без исполнения HTML |
| `POST /api/v1/actions/{id}/approve` · `/reject` | Write-действия |
| `GET /api/v1/jobs/{id}/events` | SSE потока задания |
| `GET /api/v1/usage` | Расход токенов и квота |

### SSE (`GET /jobs/{id}/events`)

`event: block` — `AgentUiBlock`; `event: status` — `{state, stage, startedAt}`;
`event: error` — problem+json; `event: done` — `{result}`.
Переподключение по `Last-Event-ID`; блоки идемпотентны по `id`.

### WebSocket сессии входа

Сервер → клиент: `{t:"frame", seq, mime:"image/jpeg", data}` (CDP screencast),
`{t:"state", url, title, status}`, `{t:"needs", what:"mfa"|"captcha"|"choice"}`.
Клиент → сервер: `{t:"mouse"|"key"|"scroll"|"resize", ...}`, `{t:"done"}`.
Кадры не записываются на диск и не попадают в логи. TTL сессии — минуты.

## 7. Коды ошибок

| Код | Смысл | Что делает UI |
|---|---|---|
| `source_auth_required` | Сессия протухла | Кнопка «войти снова», старые данные с меткой |
| `source_unavailable` | 5xx/таймаут источника | Повтор позже, данные устарели |
| `source_rate_limited` | 429 | Пауза, видимый срок |
| `login_guard_refused` | Наш guard остановил запрос входа (сайт при этом работает) | Честный текст, запасной экран или повтор шага; не «сайт не отвечает» |
| `recipe_drift` | Ответ не сошёлся со схемой | Пометка рецепта, задача автору |
| `recipe_rejected` | Lint нашёл личные данные | Рецепт не сохраняется |
| `egress_blocked` | Хост вне allowlist | Показать хост и спросить пользователя |
| `action_changed` | Подтверждённые поля изменились | Повторное подтверждение |
| `action_expired` | Истекло подтверждение | Новое предложение |
| `quota_exceeded` | Квота модели исчерпана | Понятный отказ и срок сброса |
| `injection_blocked` | Инструкция со страницы отклонена | Показать, что именно отклонено |
| `agent_busy` (409) | В треде ещё идёт ход агента | Повторить отправку позже |
| `secret_in_chat` (409) | Текст похож на секрет, пока задача ждёт защищённый ввод | Удалить сообщение; ввод — в защищённое поле |
| `secret_link_calendar` (409) | В сообщении личная ссылка календаря (iCal) | Удалить сообщение (не сохраняется, источник и задача не создаются); тред спрашивает кнопками, подключать ли календарь |
| `stale_target` (409) | Адресованная задача завершена или заменена | Перечитать runtime |
| `request_reused` (409) | Идентификатор запроса уже использован для другого тела | Новый идентификатор |

## 8. Журнал изменений контракта

Тред, маршрутизация сообщений и runtime — [contracts/conversation.md](../../contracts/conversation.md). SSE/WS задач без изменений; `purpose` и `connectContext` тредов удалены.
Сброс личных данных и клиентской очереди — [contracts/account-reset.md](../../contracts/account-reset.md).

| Дата | Что | Почему |
|---|---|---|
| 2026-10-26 | Первая редакция | Основание для параллельной работы |
| 2026-10-29 | Optional `X-Botai-Data-Reset` на `/me`, без изменения JSON | FR-023: после очистки очередь и offline snapshots не восстанавливают удалённые попытки; потребители API auth/PWA/chat/offline |
| 2026-10-26 | Ограничение рецепта v1 (только JSON) записано явно | Вывод исследования §4b: часть вузов отдаёт только HTML |
| 2026-10-30 | Новый код ошибки `login_guard_refused` (§7) | Блокировка нашего guard входа больше не выдаётся за `source_unavailable`; потребители: UI заданий, агент |
| 2026-10-30 | Новый код ошибки `secret_link_calendar` (409, §7), инструмент агента `connect_calendar`, задача `CONNECT` с `payload.calendar`, метка «Ссылка передана» | Календарь по личной ссылке iCal: ссылка-секрет не принимается в чате и вводится защищённым полем; клиент узнаёт её до очереди отправки (общие векторы `contracts/calendar-link-vectors.json`), `POST /chat/threads/{id}/calendar-link` просит тред предложить подключение, `PUT …/secrets` не принимает `ical_url`; потребители: чат (PWA), агент, API сообщений |
| 2026-10-28 | Conversational CONNECT: purpose/context, runtime, идемпотентные сообщения, защищённые receipts, legacy conversation resolver | Запрос владельца; потребители W1/W2/W3/W4/W5. Новые поля optional, старые клиенты сохраняют поведение |
| 2026-10-28 | recipe.request.render (default false, GET HTML only), weekStart | SPA, выбранная неделя; Explorer/Runner/BrowserSvc требуют новой реализации перед включением, старые рецепты без флага совместимы |
| 2026-10-30 | Единый тред (ADR-0013): удалены `purpose`/`connectContext` и `ThreadRuntime.purpose/connection`; `ThreadOut` получил `lastMessageAt`, `unread`, `attention`, `activeTask`, `preview` (убран `updatedAt`); `GET` списка возвращает все треды; новые `PATCH`/`DELETE` треда, `POST …/read`, `GET/DELETE /me/memory`; `runtime` = `{activeJob, tasks, pending, receipts}`; блоки `plan`, `task`, `attachment`, `text.quickReplies` | Агент хаба v3; ломает фронтенд чата (WS5: типы `ThreadOut`, `ThreadRuntime`, моки), сводку тредов в «Учёбе»; пользователей нет, совместимость не требуется |
| 2026-10-30 | Рецепт v2: `pagination.type=dateWindow {param, stepDays, windows, startOffset}`, `endpoint.uiUrl {path, query}`, `response.links` (≤4), `response.attributes` (≤8); словарь `scheduleEvents`: `kindText`, `address`, `group`, `lessonNumber`. v1 не меняется | FR-039–FR-042: источник отдаёт окно от даты, карточка занятия и ссылка на страницу, а не на API. Потребители: `learn/*`, `sources/*`, recipe executor; существующие рецепты читаются без изменений |
| 2026-10-30 | Домен: `provenance.openUrl` (все сущности), `ScheduleEvent.kindText/address/group/lessonNumber/links/attributes`; БД: `open_url` у всех доменных таблиц, новые колонки `schedule_events`, таблица `source_sitemaps`. В OpenAPI поля необязательны (значение по умолчанию), в ответе присутствуют всегда | FR-041, FR-042, FR-049; потребители: API, PWA (экраны данных, карточка занятия), агент (цитаты через провенанс). Миграция `b5d1c8e7a42f` от `e4a91b7c603d` |
| 2026-10-30 | Склейка источников расписания одного вуза (FR-054, аддитивно). Ответ чтения занятия (`/schedule`, `/today`): `sources[] {connectionId, title}` (все источники занятия, основной первым; при одном источнике — он один) и необязательное `disagreement {kind, detail}` (`kind` ∈ `time|title|location|teacher|group|address|several|not_in_primary`); `provenance` — основного источника. `ConnectionOut.scheduleRole` (`primary|secondary|null`). Новые маршруты: `PUT /spaces/{id}/primary-source {connectionId|null}` (204; чужой источник — 404, отключённый — 409), `POST /connections/{id}/disable` и `/enable` (обратимое отключение с остановкой задач источника и возвращением прежнего статуса, возвращают `ConnectionOut`). БД: `spaces.primary_schedule_connection_id` (миграция `c8e2f5a9d413` от `a7d41c9e5b20`, FK `ON DELETE SET NULL`); `connections.disabled_from`, таблица `agent_choices` (кнопки, которые ставит приложение перед сменой основного источника или отключением; нажатие — действия, названные в подписи, один раз) и индекс `ix_schedule_events_conn_start`, строится `CONCURRENTLY` (миграция `d9f3a1c7e582`). Схема: `domain.schema.json` — `EventSource`, `EventDisagreement`, `ScheduleEventView`. Поля необязательны в OpenAPI (значение по умолчанию), в ответе присутствуют всегда; без них ответ прежний. **Потребители:** PWA («Сегодня», расписание, карточка пары, «Источники»: типы `ScheduleEvent`, `Connection`, мок-режим и фикстуры), агент (`get_schedule`, `set_primary_source`, `ask_primary_source`, `disable_source`, `compare_sources`, контекст хода, `offer_choices` не заменяет кнопки приложения), чтение документов (`search_documents`, `read_document`, `list_documents`, счётчик раздела «Документы» не включают документы отключённого источника), читатели расписания и «Сегодня» по API | Решение владельца 30.10.2026 «склеивать, основной решает»: один вуз читается из нескольких источников, дубли не показываются, расхождения видны. Хранимые строки не меняются; отключение обратимо и скрывает всё |
| 2026-10-30 | Рецепт v2: `request.query[ключ]` — строка или список 2–8 строк (повторяющийся ключ адреса, только GET); `expand.weekly.weekdays` принимает слова (`monday`, `пн`…), `expand.weekly.intervalWeeks` — число или скаляр-поле источника; id экземпляра правила начинается с `occ1:`, будущие экземпляры пропавшего правила снимаются при sync. v1 не меняется | Инцидент production (SPA-кабинет, расписание отдаётся правилами, задания — повторяющимся `state`). Потребители: `learn/*` (compiler, lint, author), `sources/*` (executor, normalizer), `recipe.schema.json`; существующие рецепты читаются без изменений |
