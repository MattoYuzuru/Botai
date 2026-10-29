# ADR-0012: проверяемая фабрика источников v2

- Статус: **accepted**, 2026-10-29; реализация собрана в интеграционной ветке,
  полный CI и выпуск ещё не подтверждены.
- Связь: FR-008–FR-011, FR-018, NFR-004–NFR-008, NFR-012–NFR-013;
  [ADR-0005](0005-source-adapter-factory.md), [ADR-0011](0011-resumable-source-discovery.md),
  [контракт v2](../../contracts/recipe-v2.md), [LLD v2](../architecture/factory-v2.md).

## Причина изменения

Длительное исследование не решает три отдельные проблемы: HTTP-запрос может требовать
Cookie и Bearer одновременно; иерархические данные нельзя выразить плоским itemsPath;
доступ к SPA-вкладке или фильтру нужно повторить в новом runtime. Увеличение времени
или возврат свободного parser code не даёт проверяемого и безопасного sync.

## Решение

Сохранить фабрику декларативных адаптеров. Ввести явную recipe version 2 с совместимым
reader v1/v2; producer включать отдельно, по умолчанию выключенным.

1. **Per-request auth.** Модель выбирает current evidence ID. Доверенный compiler
   подтверждает метод/path/query/origin и успешный наблюдаемый запрос, сам формирует
   Cookie/Bearer binding. В спецификации только policy; значения и origin-scoped bearer
   остаются в Vault. Идентификатор evidence не становится частью общего рецепта.
2. **Data DSL.** Ограниченные nested collections с parent context, date/time composition
   с явной timezone и DST policy, weekly expansion с source range и bounded horizon.
   Типизированные операнды и фиксированный executor заменяют исполняемый код модели.
3. **Browser replay.** Только наблюдаемые native controls, ARIA tabs, same-origin frames,
   rel=next и bounded ready wait. Executor компилирует structural descriptors, затем
   перепроверяет target/ownership/effect в новом handle. Произвольный JS/click не вводится.
4. **Проверка.** Schema → рекурсивный lint → scoped canary → bounded correction →
   подтверждение → активация. Ошибки транспорта, auth, shape и неполнота различаются;
   cap/loop не становятся успешным полным снимком.
5. **Пожизненный расход Job.** TTL относится к зашифрованным знаниям. Несекретная projection
   counters/reservations сохраняется в Job в одной транзакции с checkpoint и не удаляется
   его TTL cleanup. Это исправляет ограничение первого этапа ADR-0011.

Детальные лимиты и фактический поддерживаемый набор — в [LLD v2](../architecture/factory-v2.md).
Модель не определяет allowlist, credentials audience, безопасность POST или effect actions.
CAPTCHA/MFA и разрешение нового host остаются действием человека.

## Альтернативы

| Вариант | Решение |
|---|---|
| Поднять time/token limits | Недостаточно: не исправляет auth, потерю выразительности или replay |
| Хранить cookies/headers внутри recipe | Отклонён: общая спецификация нарушила бы tenant/secret boundary |
| Исполнять парсер или JS, написанный моделью | Отклонён: нарушает ADR-0005 и ограничение write-действий |
| Одна browser-сессия навсегда | Отклонён: не переносит runtime и смешивает credentials с reusable knowledge |
| Версионированная policy/DSL и проверенный replay | Выбран: новые паттерны остаются данными, синхронизация проверяема и без модели |

## Совместимость и выпуск

V1 не получает v2 syntax; существующие рецепты автоматически не переписываются.
BOTAI_FACTORY_V2_ENABLED=false запрещает новое производство через v2 author,
но не отключает reader активных v2-рецептов. BOTAI_RESEARCH_ENABLED — отдельный
переключатель durable engine; незавершённые новые runs не отправляются legacy handler.

Порядок: отдельный контрактный PR → совместимый reader при выключенном producer →
полный CI и health/migration evidence того же SHA → подготовленный rollback reader v2 →
отдельное включение producer → ограниченная операторская приёмка.
Откат после активации v2 на binary, не понимающий v2, требует парковки подключений
или адресного возврата совместимых ревизий. Откат с удалением checkpoint таблицы нештатный.

## Последствия и границы доказательства

Нужны отрицательные fixtures на auth scope, late claims/input, неверный target/effect,
таймзоны/DST, nested overflow и неполную пагинацию, а также новая сессия, другой пользователь
и sync без LLM. Сквозной synthetic сценарий — часть интеграционной приёмки, не доказательство
качества живой модели или поддержки настоящих вузов.

Полный RFC 5545, custom calendars и произвольные site actions остаются неподдерживаемыми.
Новый паттерн добавляется общим типизированным контрактом и executor-ом с tests,
а не кодом или endpoint конкретного вуза.
