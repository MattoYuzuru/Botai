# Контракты

Замороженная часть проекта. Пять потоков работ пишут код параллельно именно
потому, что эти файлы согласованы до кода. Смысл и обоснование —
[docs/architecture/contracts.md](../docs/architecture/contracts.md).

| Файл | Потребители |
|---|---|
| `domain.schema.json` | Normalizer, API, PWA |
| `recipe.schema.json` | Explorer, RecipeStore, Runner |
| `login-preset.schema.json` | BrowserSvc, Explorer |
| `agent-ui.schema.json` | ChatAgent, Explorer, PWA |
| `openapi.yaml` | PWA, API |
| `events.md` | API, PWA, BrowserSvc |

## Правила

1. Изменение контракта — отдельный PR с перечислением, кого оно ломает, и с
   записью в журнале изменений `docs/architecture/contracts.md` §8.
2. Типы генерируются из схем, а не пишутся руками:
   `openapi-typescript` для REST, `json-schema-to-typescript` для схем.
   Сгенерированные файлы коммитятся и проверяются в CI на расхождение.
3. Каждая схема сопровождается валидными и невалидными фикстурами в
   `contracts/fixtures/`; CI проверяет обе стороны.
4. Никакое поле ответа API не содержит секрет, cookie, пароль или токен.
   Это проверяется тестом, а не соглашением.
