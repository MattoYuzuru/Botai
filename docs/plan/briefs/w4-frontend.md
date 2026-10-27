# W4 — Фронтенд, дизайн-система, Варя

Сначала прочитай [общие правила](README.md), затем **полностью**
[стайлгайд](../../design/styleguide.md) и [mascot.md](../../design/mascot.md) — это контракт,
пересчитанные значения цветов и контрастов там проверены. Образец дизайн-метода (не копировать
продукт): `/Users/yuzuru/Projects/BPMN.Space/frontend/src/shared/` (только чтение).

## Владеешь

`apps/web/`, `packages/design/`, корневой `pnpm-lock.yaml` и `biome.json`.

## Цель

PWA, которая выглядит и ощущается как Linear: тихая, плотная там, где надо, безупречная на
телефоне (iOS Safari, Android Chrome) и на ноутбуке. Все пять вкладок, подключение вуза с
потоком агента, общий календарь, задания, чат со шкалой контекста, Варя.

## Срезы

1. **Токены и UI-kit** (`packages/design`): `tokens.css` — OKLCH-шкалы и семантика из стайлгайда
   §2 (светлая и тёмная, `data-theme`, акцент пространства через `data-space` и тон), Tailwind v4
   `@theme`; `tokens.test.ts` — гамут каждой ступени и контрасты пар из §2 по формулам
   OKLab → sRGB и WCAG (не по таблице); компоненты каталога §8 с состояниями; `Icon` поверх Tabler;
   `Wordmark` (Bot + AI акцентом); `design-lint` (`scripts/design-lint.mjs`) с правилами §10,
   включая `retry-button`, `fill-as-text`, `input-font-size`, `extra-tab`. Маршрут `/styleguide`
   — витрина всех компонентов в обеих темах.
2. **Каркас приложения**: пять вкладок §7.1 (`TabBar` снизу, боковая панель от 1024 px),
   `ScreenHeader`, `UniversitySpaceSwitcher`, `SyncIndicator`, тема, safe area, `100dvh`,
   поведение при открытой клавиатуре. Вход в BotAI и регистрация по инвайту. Онбординг: что
   такое BotAI, что пароль вуза хранится зашифрованным и зачем, первый шаг «Подключить вуз».
3. **Подключение вуза**: `ProgressWall` с `ThinkingStream` (SSE задания), рендер всех блоков
   `AgentUiBlock` (`contracts/agent-ui.schema.json`; типы — `apps/web/src/api/agent-ui.gen.ts`):
   `ChoiceBlock` → `RadioGroup`/`Checkbox`, `InputBlock` → `Field` или **`SecretField`**
   (значение не держится в состоянии после отправки, `autocomplete` из блока, кегль 16 px),
   `WaitCard`, `HandoffCard`, `ApprovalCard`, `SummaryBlock`, `RemoteScreen` (WS, касания,
   масштаб без прокрутки). Неизвестный тип блока — текстом с пометкой.
4. **Данные**: «Сегодня», «Расписание» (неделя, конфликты), «Задания» (группы, срочность из
   `packages/shared`-логики — у тебя `src/lib/urgency.ts` с тестами), «Учёба» с динамическими
   разделами (`/spaces/{id}/sections`). Семь состояний данных §9 — **без кнопок повтора**:
   ретраи React Query в фоне, `SyncIndicator`, подпись свежести.
5. **Чат**: треды, сообщения блоками, стрим ответа по SSE, `ContextMeter` (плоская полоса по
   сегментам из `GET /chat/threads/{id}/context` + список источников), честное «не уверен».
6. **Варя**: скопируй `varya.js`, `sprites/` и `tools/export.mjs` из
   `/Users/yuzuru/Projects/doodle_vibe/mascot/` в `packages/design/src/mascot/vendor/` + запись
   происхождения в `docs/design/vendor.md` (W4 может создать этот файл). `resolveMascot` —
   чистая функция §2, `MascotStage`, насест на `ChatComposer` и сцена пустого чата §4,
   пузыри-реплики §6, сезонные образы §7, всё гаснет при reduced motion / скрытой вкладке.
7. **PWA**: манифест (имя BotAI, иконка из Вари), service worker с офлайн-оболочкой и кэшем
   последнего снимка, подсказка «Добавить на экран» для iOS (нет `beforeinstallprompt`).

## Как работать без бэкенда

Бэкенд пишется параллельно. Используй MSW (`apps/web/src/mocks/`) с ответами по типам
`schema.gen.ts` и потоками блоков из `contracts/examples/valid/agent-ui.blocks.json`; режим
`VITE_MOCKS=1`. Тесты — Vitest + Testing Library; ключевые сценарии — Playwright на моках
(маркер в CI тот же job `web`). Скриншоты на 390 и 1280 px в обеих темах — в описание PR.

## Готово, когда

Все вкладки и сценарии работают на моках и на настоящем API, где он уже смёржен; `design-lint`,
`tokens.test.ts`, typecheck и тесты зелёные; нет ни одной кнопки «Повторить/Обновить/Загрузить»;
iOS-зум на полях ввода отсутствует (16 px); Lighthouse PWA installable.

## Не делаешь

Бэкенд. Если нужен эндпоинт, которого нет в OpenAPI, — не выдумывай его: опиши в PR.
