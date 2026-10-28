# BotAI — план работ и распараллеливание

Статус: **исторический план этапов и действующие границы владения**, 2026-10-27;
уточнён 2026-10-29. Как исполнители работают одновременно и не мешают друг
другу. Модули и интерфейсы — [LLD](../architecture/lld.md); контракты — [contracts/](../../contracts/).

Ниже сохранена последовательность первоначальной реализации. Она не означает, что
перечисленных компонентов пока нет. Фактический путь подключения описан в
[архитектуре](../architecture/overview.md), проверки — в [testing.md](../development/testing.md),
порядок выпуска — в [deployment.md](deployment.md). Для текущей задачи полный набор
проверок выполняется в GitHub CI, локально — только ограниченные затронутые сценарии.

## 1. Принцип

Параллельность обеспечивают **границы владения файлами** и **протоколы в `botai.core`** с
тестовыми реализациями. Поток не ждёт соседа: он пишет против протокола и проверяет себя на
тестовой реализации и фикстурном вузе `fakeuni`.

```
contracts/                      W0  (меняется только PR с ревью)
backend/src/botai/core/         W0  (протоколы, ошибки, http, clock, ids, logging)
backend/src/botai/contracts/    W0  (сгенерировано)
backend/src/botai/db/           W0 → W1
backend/src/botai/main.py       W0  (create_app, подключение роутеров)
backend/src/botai/{auth,vault,spaces,domain,jobs,usage,api}/   W1
backend/src/botai/browser/      W2
backend/src/botai/{sources,learn}/                             W3
backend/src/botai/agent/        W5
backend/tests/<модуль>/         владелец модуля
backend/tests/fakeuni/{login,pages,api,moodle,ical}.py         W2: login · W3: остальное
apps/web/, packages/design/     W4
ops/, .github/                  W0
```

## 2. Этап 0 — фундамент (до параллельной работы, выполняет координатор)

- `contracts/*.schema.json` + генерация Pydantic и TS-типов, проверка расхождения в CI.
- `backend/`: `uv`, Python 3.13, FastAPI-каркас, все протоколы `core` с тестовыми реализациями,
  все таблицы и первая миграция, заглушки роутеров каждого модуля с финальными сигнатурами
  (возвращают 501), `fakeuni` с базовым входом и JSON-эндпоинтом.
- `apps/web`: Vite + React + TS + Tailwind v4, сгенерированные типы API, пустой экран.
- `ops/compose.yaml` (Postgres для разработки), `Makefile` (`make check`), CI в GitHub Actions,
  защита `main` с обязательными проверками и автомержем.

Выход: `make check` зелёный на чистой машине, всё в `main`.

## 3. Этап 1 — пять параллельных потоков

| Поток | Владеет | Результат | Против чего пишет |
|---|---|---|---|
| **W1. Платформа и API** | `auth`, `vault`, `spaces`, `domain`, `jobs`, `usage`, `api`, `db` | Приглашения и сессии; Vault (AES-GCM, AAD); пространства, подключения; чтение домена с тенант-предикатом; очередь заданий на Postgres + `PgJobContext` + SSE + ответы на блоки (секреты → Vault `transient`); воркер-раннер; квоты и учёт токенов | Протоколы `core`; реальный Postgres в тестах |
| **W2. Браузер и вход** | `browser`, `tests/fakeuni/login.py` | `PlaywrightGateway`; LoginFlow: шаги формы → блоки `input`(`secret`) / `wait` → ввод → признак успеха → `export_session`; выучивание шагов в пресет; автовход по паролю из Vault; `RemoteScreen` по WS с SLA; EgressProxy (CONNECT, allowlist, приватные сети, DNS-пин) | `JobContext`, `Vault` — тестовые реализации |
| **W3. Источники, обучение, sync, починка** | `sources`, `learn`, `tests/fakeuni/{pages,api,moodle,ical}.py` | Фабрика адаптеров; `recipe` (JSON + HTML), `moodle_ws`, `ical`, `document`; Runner, Normalizer (идемпотентный upsert), планировщик; Explorer (наблюдение → спецификация через LLM → lint → canary → вопросы); repair | `BrowserGateway` (`FakeBrowserGateway`), `LlmProvider` (`ScriptedLlm`), `JobContext` |
| **W4. Фронтенд и дизайн-система** | `apps/web`, `packages/design` | Токены по стайлгайду (проверенные контрасты), UI-kit, `Wordmark`, пять вкладок + боковая панель на ноутбуке, онбординг, подключение вуза (ProgressWall, блоки агента, `SecretField`, `WaitCard`, `RemoteScreen`), календарь, задания, чат, Варя (насест, сцена), PWA-манифест и офлайн, `SyncIndicator` без кнопок повтора | Сгенерированные TS-типы + MSW-моки по контрактам |
| **W5. Агент, чат, поиск** | `agent` | `DeepSeekProvider` (кэш префикса, учёт токенов, таймауты), классификация вопроса, SQL-инструменты только на чтение, FTS-поиск и факты, ответ с источниками и честным «не знаю», запуск repair из чата, ActionBroker (приоритет 3: каркас и `handoff`) | `LlmProvider`, `JobContext`, домен через репозитории W1 (на время — фикстурные данные в БД) |

### Точки стыковки

| Стык | Кто | Как снимается |
|---|---|---|
| Блоки агента в SSE | W1 ↔ W2, W3, W5 ↔ W4 | `agent-ui.schema.json`; производители вызывают `JobContext.emit/ask` |
| Секретные ответы | W1 ↔ W2 ↔ W4 | `input.secret` → `POST /jobs/{id}/answers` → Vault `transient` → `ask_secret()` |
| Сессия для sync | W2 → W3 | `SessionState` в Vault (`kind=session`), Runner читает через `Vault` |
| Спецификации | W3 ↔ W1 | Таблицы `adapter_specs`, `login_presets` из этапа 0 |
| Домен для чата | W3 → W1 → W5 | Таблицы домена из этапа 0, репозитории W1 |

## 4. Процесс

Брифы исполнителей — [briefs/](briefs/README.md): общие правила и по файлу на поток.


- Каждый поток работает в своём git worktree, ветка `feat/w<N>-<тема>`.
- Готовый вертикальный срез → PR в `main` с описанием на русском (что закрыто: FR/NFR, что не
  проверено) → CI → **автомерж** после зелёных проверок.
- После мержа worktree удаляется (`git worktree remove`), ветка — тоже.
- Конфликт в чужом каталоге невозможен по правилам; конфликт в `db/` или `contracts/` решает
  координатор.

## 5. Этап 2 — сведение

1. Сквозной сценарий на `fakeuni`: приглашение → пространство → подключение → вход зеркалом
   (пароль, код MFA) → обучение рецепта JSON и HTML → sync → календарь → вопрос в чате →
   «сломалось» → repair.
2. Ревью безопасности по [security.md](../architecture/security.md) §8.
3. Живая проверка ВШЭ и ЦУ — владелец.

## 6. Этап 3 — деплой

По [deployment.md](deployment.md), на `mnema`, не затрагивая Mnema.

## 7. Правила для исполнителей

Все правила — [AGENTS.md](../../AGENTS.md). Коротко: не трогай чужой каталог; тесты только на
фикстурах; наружу только через EgressProxy; секреты только в Vault; текст вуза — данные; ничего
под конкретный вуз; агент не пишет исполняемый код парсеров; каждый PR несёт тест; честный статус.
