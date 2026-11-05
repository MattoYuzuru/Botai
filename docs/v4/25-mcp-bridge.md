# MCP-мост: BotAI для внешних агентов

Статус: **предложение, 2026-11-05. Не реализовано.** Нужны решения владельца из §6.
Факты о протоколе и клиентах собраны 2026-11-05 по первичным источникам (ссылки ниже). Что не
удалось проверить, помечено «не проверено».

## 1. Зачем

Один раз подключить BotAI к Claude Code, Claude.ai/Desktop, Codex, Cursor или ChatGPT и спрашивать
из них: «какое у меня расписание», «что горит по дедлайнам», «найди в материалах курса…», «спроси
агента BotAI и дождись ответа». Внешний агент работает с теми же данными и тем же агентом, что PWA, а
не повторяет вход в вузы сам.

## 2. Вывод

Да, это имеет смысл, и в архитектуру ложится без нового узла:

- **Remote MCP-сервер внутри существующего FastAPI** на `https://botai.mnema.app/mcp` (транспорт
  Streamable HTTP). Отдельный сервис, Redis или очередь не нужны: инструменты MCP вызывают те же
  сервисы, что REST (`agent/conversations`, `rag`, `sources`, домен).
- **WebMCP — не замена.** Это черновик W3C CG (`document.modelContext`), в Chrome — origin trial с
  версии 149. Он работает только во вкладке, где открыт и выполнен вход в BotAI; Claude.ai, ChatGPT и
  Codex до него не дотягиваются (Claude Code — только через DevTools-мост Chrome). Можно добавить
  позже как удобство в браузере, но основной путь — серверный MCP.
- Отдельный «контракт» вне MCP не нужен: MCP и есть контракт. Его схема инструментов фиксируется в
  `contracts/mcp-tools.schema.json` (отдельный PR в `contracts/`, по правилу №9).

## 3. Протокол на 2026-11-05

- Текущая ревизия спеки — **2026-07-28**: без `initialize`/`Mcp-Session-Id`, обязательный
  `server/discover`, ответ на POST — JSON или SSE этого запроса; старый HTTP+SSE устарел с 2025-03-26.
  [versioning](https://modelcontextprotocol.io/specification/versioning),
  [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
  [transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).
- Авторизация удалённого сервера: OAuth 2.1, Protected Resource Metadata (RFC 9728), параметр
  `resource` (RFC 8707); регистрация клиента — предпочтительно Client ID Metadata Documents, DCR
  устарел (MAY). [authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization).
- Долгие вызовы: `notifications/progress` в ядре; асинхронные задачи вынесены в расширение
  `io.modelcontextprotocol/tasks` (по матрице клиентов — Claude web и Claude Code).
  [tasks](https://modelcontextprotocol.io/extensions/tasks/overview).
- Python SDK `mcp` 2.x монтируется в Starlette/FastAPI (`streamable_http_app()`), умеет быть
  resource server (`TokenVerifier`, метаданные RFC 9728).
  [ASGI](https://py.sdk.modelcontextprotocol.io/run/asgi/),
  [authorization](https://py.sdk.modelcontextprotocol.io/run/authorization/).

Клиенты:

| Клиент | Как подключить | Bearer-токен без OAuth |
|---|---|---|
| Claude Code | `claude mcp add --transport http botai https://botai.mnema.app/mcp` | да, `--header "Authorization: Bearer …"` |
| Codex CLI | `codex mcp add botai --url …` | да, `bearer_token_env_var` (таймаут по умолчанию 60 с — поднять) |
| Cursor | `mcp.json`: `url` + `headers` | да |
| Claude.ai / Desktop | Connectors → Add custom connector | нет (статические заголовки — бета для части организаций) — нужен OAuth |
| ChatGPT | Plugins → Add custom MCP server | нет — нужен OAuth |

Не проверено: какие из Codex/ChatGPT/Cursor уже говорят на ревизии 2026-07-28; таймауты ChatGPT.

## 4. Предлагаемая архитектура

```
внешний агент ──HTTPS──▶ Caddy ──▶ api: /mcp (mcp SDK, Streamable HTTP)
                                     │  TokenVerifier → mcp_tokens (hash, scopes, user_id)
                                     ▼
                       те же сервисы, что у REST: agent/conversations, rag, sources, домен
                                     │
                       worker: ход агента v4 (job AGENT) — как из PWA
```

**Аутентификация, два этапа.**

1. *Персональный токен* (сразу): в «Настройках» — «Подключить внешнего агента» → токен показывается
   один раз, в БД только sha256, с областями и сроком, отзыв одной кнопкой. Покрывает Claude Code,
   Codex, Cursor. Новая таблица `mcp_tokens` (аддитивная миграция).
2. *OAuth 2.1* (для Claude.ai и ChatGPT): минимальный authorization server в том же FastAPI —
   страница согласия поверх существующего входа, PKCE S256, CIMD (`token_endpoint_auth_methods: none`)
   и DCR для Cursor, токены привязаны к ресурсу `/mcp` (audience), refresh-токены с ротацией.

**Инструменты (первый набор).** Чтение помечено `readOnlyHint`.

| Инструмент | Что делает | Основа |
|---|---|---|
| `whoami` | пользователь, вузы, часовой пояс | `/api/v1/me`, пространства |
| `schedule` | занятия за период (по умолчанию сегодня/неделя) | домен `schedule_events` |
| `deadlines` | задания и сроки | домен `deadlines` |
| `grades` | оценки и рейтинг | домен/панели |
| `sources` | источники, статус, последний sync | `sources` v4 |
| `search` | гибридный поиск по RAG-корпусу с цитатами | `rag_search` |
| `ask_agent` | вопрос агенту BotAI в новом или указанном треде | `post_message` + job AGENT |
| `get_answer` | статус и ответ хода по `turn_id` (опрос) | runtime треда |
| `threads` / `read_thread` | список чатов и переписка | REST чатов |
| `sync_source` | запустить обновление источника | `sync-jobs` (202) |

`ask_agent` возвращает ответ, если он готов за ~50 с (с `notifications/progress` по шагам агента),
иначе `turn_id` для `get_answer`; где клиент умеет Tasks — отдаёт задачу. Действия на сайтах вузов идут
через тот же агент и ту же политику `auto/ask/never` (D-006): вопрос
`ask` появится в PWA, MCP вернёт «ждёт подтверждения».

**Наблюдаемость.** Каждый MCP-вызов — строка журнала (`audit_log`: клиент, инструмент, длительность,
без аргументов-секретов); ходы агента из MCP видны в обычных трассах с пометкой `via=mcp`.

## 5. Что не делаем сразу

- Не отдаём через MCP первичные секреты (пароли, MFA, ключ-ссылки) — принцип 5, это не меняется.
- WebMCP, ресурсы/подписки MCP, промпты MCP — после первого набора инструментов.
- Публичная регистрация клиентов «для всех» — не цель (pet-проект на двоих).

## 6. Решения владельца

1. **Сессионный материал наружу.** Отдавать ли внешнему агенту cookie/токены вузов (инструмент
   `session_export`)? Внутри BotAI это рабочий материал агента (D-008), но спека MCP запрещает
   «пробрасывать» токены клиента дальше и требует, чтобы сервер не был confused deputy. Предложение:
   по умолчанию **нет**; внешний агент просит BotAI выполнить запрос (`ask_agent`/`http` через агента),
   а не забирает cookie. Если нужно — отдельная область `sessions:export`, выключенная по умолчанию.
2. **OAuth сразу или только токены.** Предложение: этап 1 (токены) сейчас, OAuth — когда понадобится
   Claude.ai/ChatGPT.
3. **Доступ друга.** MCP-токен может выпускать каждый пользователь для себя (данные строго свои).
