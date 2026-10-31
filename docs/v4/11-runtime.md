# Runtime v4: ядро свободного harness

Статус: **реализовано (task-12), 2026-10-31.** Фаза 1.1, инструментальный протокол и базовые
инструменты 1.2, провайдеры 1.5. Живой L2/L3 через новый runtime, браузерные инструменты,
`code_exec`, секреты и подключение к задачам/job'ам — отдельные задачи (16, 20); этот пакет их
не подменяет и не ломает v3 (`agent/harness.py`, `agent/tools*.py`, `agent/factory.py` не
изменялись).

## 1. Что сделано

- Новый изолированный пакет `backend/src/botai/agent/v4/`: цикл «контекст → провайдер → tool
  calls → результаты → ответ», мягкие лимиты, пауза/остановка, ошибки инструментов как данные,
  параллельные/последовательные вызовы, учёт токенов и стоимости, полная трасса.
- `protocol.py`: `ToolSpec` (имя, описание, JSON Schema, таймаут, класс политики,
  идемпотентность, лимит результата), `ToolResult`, `ToolContext`, `ToolHandler`, реестр,
  каналы `ask_user` и политики. Строгие pydantic-модели: схемы проверяются на `type: object` и
  `additionalProperties: false`, аргументы модели валидируются по JSON Schema **до** исполнения.
- `traces.py`: `TraceEvent` / `RunTrace` / `RunInfo`, протокол `TraceStore` и `MemoryTraceStore`
  для тестов; DB-адаптер добавляется в wiring без изменения runtime.
- `runtime.py`: `AgentRuntime.run(task, provider, tools, trace_store=..., limits=..., ask=...)`,
  `RunTask`, `RunLimits`, `RunResult`, `ManualControl`.
- `providers/`: `Provider` (структурно совместим с `core.llm`), `MockProvider` на
  `ScriptedLlm`, адаптер DeepSeek (реюз `agent.llm.DeepSeekProvider`), Anthropic Messages API,
  OpenAI-совместимый `/chat/completions`, фабрика `build_provider(settings)`.
- `tools/`: `http_request`, `fs_read`/`fs_write`/`fs_list`, `ask_user`, `trace_read`.
- `settings.py`: `llm_provider += anthropic|openai_compat`, ключи/URL/модели Anthropic и
  OpenAI-совместимых провайдеров; значения по умолчанию v3 не изменены (`mock`, DeepSeek).
- Тесты `backend/tests/agent/v4/`: цикл, лимиты/пауза/резюме, трассы, инструменты,
  провайдеры на `MockTransport`, настройки. Реальной сети и ключей в тестах нет.

## 2. Структура пакета

```
backend/src/botai/agent/v4/
  __init__.py            # публичные экспорты runtime/protocol/traces
  protocol.py            # ToolSpec, ToolResult, ToolContext, ToolHandler, registry, ask, policy
  runtime.py             # AgentRuntime, RunTask, RunLimits, RunResult, ManualControl
  traces.py              # TraceEvent, RunTrace, RunInfo, TraceStore, MemoryTraceStore
  http.py                # ЕДИНСТВЕННАЯ фабрика httpx-клиента в v4 (guard архитектуры)
  providers/
    base.py              # Provider protocol, require_secret
    mock.py              # MockProvider (очередь ScriptedLlm)
    deepseek.py          # адаптер к существующему agent.llm.DeepSeekProvider
    anthropic.py         # Messages API + tool_use/tool_result mapping
    openai_compat.py     # любой /chat/completions
    factory.py           # build_provider(settings)
  tools/
    http.py              # http_request
    fs.py                # fs_read/fs_write/fs_list + workspace guard
    ask.py               # ask_user
    traces.py            # trace_read
    __init__.py          # build_basic_tools()
  executor/              # песочница code_exec/fs — параллельная задача 14, не часть task-12
```

## 3. Публичные интерфейсы (контракт для browser/executor/sources/wiring)

### Инструмент

```python
from botai.agent.v4.protocol import ToolContext, ToolResult, ToolSpec, ToolHandler, object_schema

class SourceSyncTool:                     # любой новый инструмент — так
    spec = ToolSpec(
        name="source_sync",
        description="Синхронизировать источник",
        parameters=object_schema({"source_id": {"type": "string"}}, required=["source_id"]),
        timeout_s=120.0,
        policy="write_soft",              # read | write_soft | write_destructive | money | auth
        idempotent=True,
        max_result_chars=20_000,
    )

    async def run(self, arguments: dict, context: ToolContext) -> ToolResult: ...
```

- `ToolResult.success(summary=..., data=..., artifacts=..., truncated=..., next=..., cost_ms=...)`
  и `ToolResult.failure(code, detail, retryable=..., hint=..., data=...)` — результат это данные,
  рантайм сам сериализует его в JSON и оборачивает в `<source_data tool="…">`.
- `ToolResult.data` не должен содержать первичных секретов (пароль, MFA-код, ключ-ссылка).
  Cookie/токены/сессии — рабочий материал, их возвращать можно (D-008).
- Ошибки инструмента — `ToolResult.failure`, а не исключения: рантайм превращает исключение в
  `internal_error` без текста исключения. `retryable` — подсказка модели, повторяет пусть она.
- `FunctionTool(spec, fn)` — адаптер для простой async-функции.
- `build_basic_tools()` отдаёт текущий набор; wiring складывает его с `browser_*`
  (`browser/control.py`), `code_exec`/`fs_*` (`agent/v4/executor`), `source_*`, `rag_*`,
  `memory_*`, `secret_*`, `job_*` в один список `ToolHandler` — runtime не меняется.
- `ToolContext`: `run_id`, `task`, `workspace: Path`, `step`, `user_id`, `metadata`,
  `trace_store`, `ask`. Workspace задачи создаёт wiring; fs-инструменты в него заперты
  (symlink-выход за пределы — `workspace_escape`).

### Провайдер

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    async def complete(self, request: LlmRequest) -> LlmResponse: ...
    async def aclose(self) -> None: ...
```

`build_provider(settings)` (`botai.agent.v4.providers.factory`) выбирает по `llm_provider`:
`mock` (тесты), `deepseek` (адаптер к проверенному v3-провайдеру), `anthropic`,
`openai_compat`. Стриминга в v4-провайдерах пока нет: runtime потребляет целые ответы.

### Трасса

```python
class TraceStore(Protocol):
    async def append(self, event: TraceEvent) -> None: ...
    async def read(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[TraceEvent]: ...
    async def get_run(self, run_id: str) -> RunTrace | None: ...
    async def list_runs(self, *, limit: int = 20) -> list[RunInfo]: ...
```

`MemoryTraceStore` — эталон поведения; DB-адаптер (`runs`/`run_steps`/`tool_calls` или
аддитивно к `job_events`) реализует тот же протокол в task-16. Виды событий: `run_started`,
`model_call`, `tool_call`, `tool_result`, `limit`, `pause`, `cancel`, `error`, `note`,
`run_finished`. Агент читает трассу инструментом `trace_read`; владелец увидит то же в UI.
Payload ограничен по размеру (`content` до 4k, результат целиком — до 4k, большие результаты
помечаются `result_truncated_in_trace`), секретов в трассе быть не должно.

### Управление, вопрос человеку, политика

- `RunControlView` (`cancelled`, `paused`) — реализация у worker'а/UI; `ManualControl` — для
  тестов. Пауза/отмена проверяются перед каждым шагом; run возвращает `paused`/`cancelled` с
  чекпоинтом, работа не теряется.
- `AskUser = Callable[[AskQuestion], Awaitable[AskAnswer]]` передаётся в `run(ask=...)`; вопрос
  `input_kind="secret"` возвращает модели только `{"answered": true, "hidden": true}` — значение
  увидит broker/исполнитель, но не промпт.
- `PolicyDecider` (`approve=...` в конструкторе `AgentRuntime`) спрашивается только для
  инструментов с `policy != "read"`; по умолчанию (движок политик — фаза 3) все действия
  разрешены. Отказ — `ToolResult.failure("policy_denied", reason)`.
- `Pricing = Callable[[LlmUsage, datetime], float]` — цена вызова; по умолчанию тариф DeepSeek
  из `botai.usage.service`. Для Claude/OpenAI wiring должен передать свой `pricing` (см. §9).

### Резюме run'а

```python
result = await runtime.run(
    RunTask(task="…", system="…", messages=history, workspace="/tasks/<id>", user_id=uid),
    provider,
    tools,
    trace_store=DbTraceStore(...),
    limits=RunLimits(max_steps=settings.agent_limits_steps, ...),
    ask=ui_ask,
)
result.status        # answer | step_limit | time_limit | token_limit | cost_limit |
                     # hard_stop | paused | cancelled | error
result.content       # финальный ответ модели
result.messages      # чекпоинт без system: передать в новый RunTask(messages=...) после
                     # паузы/рестарта/поднятия лимита
result.trace         # RunTrace с событиями, токенами и стоимостью
```

## 4. Как подключить к задачам и job'ам (task-16)

1. `agent_engine == "v4"` (флаг уже есть, по умолчанию `v3`); v3-путь не удаляется.
2. Фабрика wiring собирает: `provider = build_provider(settings)`, `AgentRuntime(control=...,
   approve=..., pricing=...)`, набор инструментов (`build_basic_tools()` + browser/executor/
   sources), `DbTraceStore` на сессию, workspace на задачу.
3. `RunLimits` из настроек: `max_steps=agent_limits_steps`, `max_seconds=agent_limits_seconds`,
   `max_tokens=agent_limits_tokens`, `max_usd=agent_limits_usd`,
   `hard_stop_usd=agent_hard_stop_usd`, `auto_continue=agent_auto_continue`. `None` = без
   предела (D-009), умолчаний-стен нет.
4. `RunTask` из job-контекста: kind → системный промпт и профиль (`purpose`, `tier`, `model`),
   durable state → `messages`, задача → `task`, том → `workspace`.
5. Чекпоинт после каждого шага уже в трассе; для resume сохранять `result.messages` (сжатый
   `run_state` в БД — в wiring). `ask_user` паркует run в `waiting_user`, повторный ответ
   продолжает с чекпоинта.

Пока wiring не сделан, runtime вызывается только тестами; `make check` и production v3 не
затронуты.

## 5. Лимиты, пауза, остановка

| Настройка | `RunLimits` | Поведение |
|---|---|---|
| `agent_limits_steps` | `max_steps` | мягкий счётчик шагов модели |
| `agent_limits_seconds` | `max_seconds` | мягкий счётчик времени run'а |
| `agent_limits_tokens` | `max_tokens` | мягкий счётчик токенов (hit+miss+output) |
| `agent_limits_usd` | `max_usd` | мягкий счётчик денег |
| `agent_hard_stop_usd` | `hard_stop_usd` | аварийный стоп: срабатывает всегда |
| `agent_auto_continue` | `auto_continue` | True = только предупреждение в трассе, False = стоп |

- Лимит — наблюдаемая величина: в трассе событие `limit` с `steps/elapsed_s/total_tokens/
  cost_usd`. При `auto_continue=False` run завершается статусом `*_limit` и **резюмируемым**
  чекпоинтом; при `True` — пишет предупреждение и продолжает (страховка — `hard_stop_usd`).
- Жёстких «шагов на задачу» по умолчанию нет; `max_calls_per_step` (16) и
  `max_parallel_tools` (8) — санитарные пределы одного ответа модели, оба настраиваются,
  `max_calls_per_step=None` снимает предел.
- Пауза/отмена владельцем: `RunControlView` → статус `paused`/`cancelled`, сообщения сохранены.
  Вопрос `ask_user` блокирует run (worker должен парковать его, не держа слот).

## 6. Провайдеры и настройки

| `llm_provider` | Класс | Ключ/URL | Модели по умолчанию |
|---|---|---|---|
| `mock` | `MockProvider` | — | scripted очередь |
| `deepseek` | `DeepSeekProviderAdapter` | `DEEPSEEK_API_KEY`, `LLM_BASE_URL`, `LLM_PROFILE_*` | как в v3 |
| `anthropic` | `AnthropicProvider` | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` | `anthropic_model_fast/pro` |
| `openai_compat` | `OpenAICompatProvider` | `LLM_OPENAI_API_KEY`, `LLM_OPENAI_BASE_URL` | `llm_openai_model_fast/pro` |

- Anthropic: `POST {base_url}/v1/messages` (+ `/v1` не дублируется), заголовки `x-api-key`,
  `anthropic-version: 2023-06-01`; system → top-level `system`; tool calls → `tool_use`,
  результаты → `tool_result`, параллельные результаты группируются в один user-turn; usage:
  `cache_read_input_tokens` → hit, `input_tokens + cache_creation_input_tokens` → miss.
  Расширенное thinking пока не отправляется (у разных моделей разные бюджеты), `thinking/effort`
  запроса для Claude игнорируются.
- OpenAI-совместимые: стандартные `tools`/`tool_calls`, `response_format: json_object` при
  `json_schema`, `max_tokens`; reasoning_content читается, если эндпоинт его вернул.
- Общие ретраи: 2 повтора на 5xx/429/таймаут/битый JSON, экспоненциальный backoff с
  `Retry-After`; ошибки без тела ответа (оно может эхоить промпт); 401/402 — понятное сообщение
  владельцу. Ключи — `SecretStr`, в логи/исключения не попадают.
- `DeepSeekProviderAdapter` не дублирует HTTP-логику: профили, стриминг, ретраи и квоты
  остаются в `agent/llm.py`; адаптер добавляет только `Provider.name` и `aclose()`.

## 7. Безопасность

- Прямой `httpx` в v4 разрешён только в `agent/v4/http.py` — это проверяет
  `tests/core/test_architecture.py`; остальные модули получают клиент оттуда. Транспорт для
  тестов (`MockTransport`) передаётся параметром, сеть в тестах не используется.
- `http_request` не принимает логин/пароль в URL (`credentials_in_url`): это попало бы в трассу.
  Cookie передаются отдельно; первичные секреты — только через будущий `secret_use`.
- `ask_user(input_kind="secret")` не возвращает значение модели.
- Трасса хранит аргументы и результаты инструментов (для самонаблюдаемости): инструменты,
  работающие с секретами, обязаны передавать хендлы, а не значения (фаза 1.4).
- `fs_*` запирают пути в workspace, включая symlink-выход; `http_request` ограничивает размер
  тела (`max_bytes`, по умолчанию 2 МБ) и умеет сохранять тело в workspace вместо контекста.
- Секреты не печатаются в отчётах; в тестах используются фиктивные ключи вида
  `sk-…-FIXTURE`.

## 8. Тесты

```bash
cd backend
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest tests/agent/v4 -q
```

Покрыто: цикл «tool call → результат → ответ», порядок tool-схем, параллельные чтения и
последовательные записи, unknown tool / bad JSON / schema violation / exception / timeout /
policy denial как данные, `<source_data>`-обёртка (нельзя закрыть), чекпоинт и resume,
мягкие лимиты steps/seconds/tokens/usd, `auto_continue`, `hard_stop_usd`, пауза/отмена,
`sse`/wire DeepSeek-адаптера, Anthropic (system/tools/tool_result/usage/401/500/429),
OpenAI-compat (tools/usage/429/overrides), ретраи, `http_request` (методы, JSON/form/raw,
редиректы, таймаут, сеть, cap, save_to, escape), `fs_*` (roundtrip, append, offset, binary,
traversal, symlink, list), `ask_user` (text/choice/secret/no-channel/cancel/timeout),
`trace_read` (текущий/прошлый/список/пагинация), настройки и обратная совместимость v3.

## 9. Ограничения и что осталось

- **Нет стриминга**: runtime и провайдеры потребляют целый ответ. SSE-события шага уже есть в
  трассе; стриминг текста в UI — отдельно.
- **Нет сжатия контекста**: при переполнении окна provider вернёт ошибку; сжатие (§4.2) —
  следующий шаг. Пока чекпоинт и трасса позволяют пересобрать контекст снаружи.
- **Трасса только в памяти** (`MemoryTraceStore`); DB-адаптер и API/UI чтения — task-16.
- **Политики действий** (auto/ask/never, журнал, идемпотентность) — фаза 3; runtime лишь
  спрашивает `PolicyDecider` и пишет событие.
- **Секреты** (`secret_use`, broker, подстановка в поля/HTTP) — фаза 1.4; `http_request` и
  `ask_user` уже готовы не пропускать значения в контекст.
- **Цена Claude/OpenAI** по умолчанию считается по тарифу DeepSeek (fallback
  `usage.service.PRICES`); до wiring нужен `pricing` для конкретного провайдера, иначе лимит по
  деньгам занижен/завышен.
- **Мультипарт/потоковая загрузка** в `http_request` не сделаны (JSON/form/raw + сохранение
  ответа в workspace есть); артефактов с id (`artifacts`) пока нет — инструменты возвращают
  `saved_to` в workspace.
- **Вложенные run'ы, children, `update_plan`, steering** — по мере фаз 1.2+; протокол
  инструментов к ним готов.
- **Живой L2/L3** через новый runtime не прогонялся (нужны ключи и wiring); реальная сеть в
  тестах не использовалась — критерий фазы 1.1 «агент решает fakeuni инструментами» закрывается
  в task-16/20 вместе с браузером и песочницей.
