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
| `ScheduleEvent` | `id, connectionId, externalId, courseId?, title, kind (lecture/seminar/exam/other), startsAt, endsAt, location?, teacherIds[], format (offline/online/hybrid), recurrence?, provenance` |
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
  | { type: "text";     id: string; markdown: string }
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
  | { type: "login";    id: string; connectionId: string; ticket: string; wsUrl: string };

type AgentUiAnswer = { blockId: string; values: string[] | string | boolean };
```

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
| `POST /api/v1/spaces` · `GET /api/v1/spaces` | Пространства вузов |
| `POST /api/v1/spaces/{id}/connections` | Начать подключение (создаёт job) |
| `GET /api/v1/connections/{id}` · `DELETE` | Состояние, отключение |
| `POST /api/v1/connections/{id}/login-session` | Открыть сессию входа (вернёт `wsUrl`) |
| `POST /api/v1/connections/{id}/sync` | Ручная синхронизация |
| `GET /api/v1/schedule?from&to&spaceId?` | Общий календарь |
| `GET /api/v1/deadlines` · `/courses` · `/grades` · `/people` | Домен |
| `POST /api/v1/chat/threads` · `POST /api/v1/chat/threads/{id}/messages` | Чат |
| `POST /api/v1/chat/threads/{id}/answers` | Ответ на `AgentUiBlock` |
| `POST /api/v1/documents` | Подключить таблицу/документ/ссылку |
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
| `recipe_drift` | Ответ не сошёлся со схемой | Пометка рецепта, задача автору |
| `recipe_rejected` | Lint нашёл личные данные | Рецепт не сохраняется |
| `egress_blocked` | Хост вне allowlist | Показать хост и спросить пользователя |
| `action_changed` | Подтверждённые поля изменились | Повторное подтверждение |
| `action_expired` | Истекло подтверждение | Новое предложение |
| `quota_exceeded` | Квота модели исчерпана | Понятный отказ и срок сброса |
| `injection_blocked` | Инструкция со страницы отклонена | Показать, что именно отклонено |

## 8. Журнал изменений контракта

| Дата | Что | Почему |
|---|---|---|
| 2026-10-26 | Первая редакция | Основание для параллельной работы |
| 2026-10-26 | Ограничение рецепта v1 (только JSON) записано явно | Вывод исследования §4b: часть вузов отдаёт только HTML |
