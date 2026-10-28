# Навигатор по документации

Состояние проекта: код MVP существует, текущая интеграция проходит приёмку, 2026-10-29.
Первая точка входа — [README](../README.md), первое действие владельца —
[вопросы](../QUESTIONS.md).

## По задаче

| Задача | Куда идти |
|---|---|
| Понять, что продукт делает | [Требования](product/requirements.md) |
| Понять, где живёт мой компонент | [HLD](architecture/hld.md), [LLD](architecture/lld.md), [обзор](architecture/overview.md) |
| Узнать формат данных, который я читаю или пишу | [Контракты](architecture/contracts.md) |
| Трогаю сессии, сеть, промпты или данные пользователя | [Безопасность](architecture/security.md) |
| Оценить риск, стоимость или конкурентов | [Реализуемость](research/feasibility.md) |
| Делаю экран или компонент | [Стайлгайд](design/styleguide.md) |
| Делаю маскота, прогресс или пустое состояние | [Маскот](design/mascot.md), [вендоренная Варя](design/vendor.md) |
| Понять, кто что пишет и в каком порядке | [План работ](plan/workstreams.md), [брифы потоков](plan/briefs/README.md) |
| Харнес агента, профили рассуждения | [agent-harness.md](architecture/agent-harness.md), [бенчмарк](plan/benchmark.md) |
| Деплой на сервер | [deployment.md](plan/deployment.md) |
| Написать тест, использовать ключ модели | [Тесты и ключ модели](development/testing.md) |
| Публичный Web Search, провайдеры и ключи | [Публичный поиск](development/public-search.md) |
| Исследование кабинета, DOM, сеть и виды данных | [Адаптивное исследование](research/adaptive-observation.md) |
| Включить диагностику или прочитать проход по ID | [Диагностика](development/diagnostics.md) |
| Узнать, почему решено именно так | [Решения (ADR)](#решения) |

## Решения

| ADR | Тема | Статус |
|---|---|---|
| [0001](adr/0001-login-form-mirroring.md) | Вход — зеркало формы, пароль в Vault, удалённый экран как запасной путь | accepted |
| [0002](adr/0002-recipe-as-bridge.md) | Декларативный рецепт — единственный мост обучение → исполнение | accepted |
| [0003](adr/0003-stack.md) | Стек: Python-бэкенд, TS-фронтенд, Postgres | accepted |
| [0004](adr/0004-agent-ui-blocks.md) | Своя схема блоков интерфейса агента вместо полного A2UI | accepted |
| [0005](adr/0005-source-adapter-factory.md) | Фабрика источников и самопочинка | accepted |
| [0006](adr/0006-retrieval-mvp.md) | Поиск в MVP: SQL + полнотекстовый Postgres | accepted |
| [0007](adr/0007-agent-harness.md) | Свой тонкий харнес агента над DeepSeek | accepted |
| [0008](adr/0008-conversational-connections.md) | Подключение источника в обычном чате | accepted |
| [0009](adr/0009-rendered-html-recipes.md) | Отрисованные HTML-рецепты для SPA | accepted |
| [0010](adr/0010-adaptive-source-discovery.md) | Ограниченный цикл исследования и состояние браузера в Vault | accepted |

Решения владельца — [QUESTIONS.md](../QUESTIONS.md).

## Правила документации

- Документ честно отмечает статус: `draft`, `proposed`, `accepted`,
  `implemented`, `validated`. Написанный документ не делает поведение сделанным.
- Фактическое утверждение о вузе, цене или инструменте сопровождается
  источником и отметкой уверенности: **Ф** — проверено, **П** — предположение.
- Устаревший документ правится, а не дублируется новым.
