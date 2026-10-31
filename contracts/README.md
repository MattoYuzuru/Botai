# Контракты

**Статус: legacy v3.** Контракты ниже описывают прежнюю архитектуру (два контура,
рецепт, read-only, allowlist) и живут в коде, пока идёт миграция. Целевые контракты v4
(инструменты, playbook, run, артефакты, agent-ui) перечислены в
[docs/v4/02-architecture.md](../docs/v4/02-architecture.md) §12.4; решения, отменяющие
прежние контракты, — [docs/v4/06-decisions.md](../docs/v4/06-decisions.md). Смысл и
обоснование прежней версии — [docs/v4/02-architecture.md](../docs/v4/02-architecture.md).

| Файл | Потребители |
|---|---|
| `conversation.md` | API, очередь, Explorer, ChatAgent, PWA |
| `domain.schema.json` | Normalizer, API, PWA |
| `recipe.schema.json` | Explorer, RecipeStore, Runner |
| `login-preset.schema.json` | BrowserSvc, Explorer |
| `agent-ui.schema.json` | ChatAgent, Explorer, PWA |
| `adapter-spec.schema.json` | Explorer, RecipeStore, Runner |
| `recipe-v2.md` | Explorer, Runner, lint (описание рецепта v2) |
| `account-reset.md` | API, PWA (сброс данных аккаунта) |
| `calendar-link-vectors.json` | Сервер и веб-клиент (общие случаи для личной ссылки календаря) |
| `events.md` | API, PWA, BrowserSvc |

REST описывает не файл в этом каталоге, а `backend/openapi.json`: его создаёт FastAPI, `./scripts/gen.sh` регенерирует.

## Правила

1. Изменение контракта — отдельный PR с перечислением, кого оно ломает, и с
   записью в журнале изменений `docs/v4/02-architecture.md` §12.4.
2. Типы генерируются из схем, а не пишутся руками:
   `openapi-typescript` для REST, `json-schema-to-typescript` для схем.
   Сгенерированные файлы коммитятся и проверяются в CI на расхождение.
3. Каждая схема сопровождается валидными и невалидными фикстурами в
   `contracts/examples/valid/` и `contracts/examples/invalid/`; CI проверяет обе стороны.
4. Никакое поле ответа API не содержит секрет, cookie, пароль или токен.
   Это проверяется тестом, а не соглашением.
