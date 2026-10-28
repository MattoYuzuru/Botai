# Контракт подключения источника в чате

Статус: accepted, 2026-10-28. Основание — запрос владельца. Реализация выпускается отдельным PR после этого контракта. Потребители: W1 API/очередь/БД, W2 вход, W3 Explorer/Runner, W4 PWA, W5 ChatAgent. Старые REST/SSE/WS и `AgentUiBlock` сохраняют формы; новые поля необязательны.

## Создание беседы

`POST /api/v1/chat/threads`: существующие `spaceId`, `title`, плюс `purpose?: "chat" | "connect"` (по умолчанию chat), `clientRequestId?: ULID`, `connectContext?: {universityId?, entryUrls?, kind?, title?, requestedKinds?, absentKinds?, userNote?}`.

`connectContext` допустим только при purpose=connect. entryUrls — до 6 HTTPS URL длиной до 1000; без userinfo, нестандартного порта и секретных query-параметров. universityId использует существующий строковый ID каталога (до 50 символов), не ULID. userNote — до 2000 символов; виды данных уникальны и не пересекаются. Без явного выбора новый диалог начинает с расписания; отсутствующие дедлайны не являются ошибкой. Ответ 201 остаётся `ThreadOut`. Повтор clientRequestId того же владельца и тела возвращает ту же беседу; другое тело — 409.

purpose=connect создаёт один связанный CONNECT bootstrap даже без connectionId. Неизвестные вуз и адрес уточняются сообщениями. Thread.purpose/connect_context хранятся отдельно от LLM summary. Когда источник определён, job и беседа связываются с owned connection. Изменить цель до настройки можно через диалог; готовые источники не подменяются молча.

## Сообщения и текущая попытка

`POST /chat/threads/{id}/messages`: существующий text (1..8000), плюс `clientMessageId?: ULID`, `targetJobId?: ULID`. Ответ 202 остаётся `{message: MessageOut, jobId}`.

Адресованное активному CONNECT сообщение становится durable inbox turn этого задания; отдельный CHAT не запускается. Повтор clientMessageId с теми же владельцем/thread/текстом/target возвращает канонический ответ; другое содержание — 409. Чужой target — 404, завершённый или устаревший — 409. При обычном чате прежнее поведение сохраняется. Ввод пароля/MFA через этот путь отвергается до записи сообщения, если актуальный вопрос требует secret editor. Текст подключений клиент не кладёт в сохраняемый Outbox.

`GET /chat/threads/{id}/runtime`:

```ts
type ThreadRuntime = {
  purpose: "chat" | "connect";
  activeJob: JobOut | null;
  connection: ConnectionOut | null;
  pending: {jobId: string; blockId: string; channel: "native" | "dialogue" | "manual" | "approval"; secret: boolean}[];
  receipts: {jobId: string; messageId: string | null; blockId: string | null; status: "accepted" | "applied" | "superseded" | "rejected"; at: string}[];
};
```

Ровно одно из messageId/blockId в receipt заполнено. Для connect activeJob — текущая/последняя попытка, включая terminal до её замены; для chat — активное задание. Приоритет в connect: активный CONNECT/SYNC/REPAIR/LEARN, затем активный CHAT, иначе последняя операция источника (включая SYNC). После terminal подключение остаётся контекстом: сообщение без targetJobId начинает обычный CHAT в той же беседе; при активном CONNECT сообщение без target направляется в его inbox. В terminal pending пуст. dialogue — не секретное уточнение Explorer; native — типизированные поля/выбор; manual — актуальный экран; approval — конкретное write-подтверждение. В runtime нет значений ответов, ticket и кадров. Подробности активных блоков приходят существующим SSE; новый билет выдаёт существующий POST login-session. Runtime перечитывается после block/status/done и действий человека.

`POST /connections/{id}/conversation` с необязательным `{jobId}` возвращает `ThreadOut`, не запускает работу. Под блокировкой owned connection находит или создаёт каноническую connect-беседу, связывает старую owned попытку и проецирует доступную безопасную историю/итог. Чужие ID — 404. Legacy /connect/{jobId} использует GET job → connectionId → этот метод. Повторный вход и починка продолжают тот же чат; отсутствие спецификации ставит CONNECT вместо REPAIR.

## История и защищённый ввод

Каждый публичный agent block проецируется отдельным ChatMessage со стабильной идентичностью/upsert и временем первого появления; очередной thinking update не перемещает его. Ответ человека — следующий user turn. Не используется один растущий message на всю попытку, перестановка elevated/history или client-only keptBlocks splice.

Исторические controls read-only по runtime.pending/receipts. LoginBlock превращается в безопасную текстовую отметку; ticket/wsUrl не сохраняются в истории. Secret answer идёт через существующие jobs/answers → Vault; история получает только отметку без значения. После manual handoff старый auth input superseded, а не фиктивно answered. Write approval свободным текстом не подменяется.

## Диалог воркера

`DialogueTurn(message_id, text, created_at, reply_to_block_id=None)`; отдельный runtime-checkable `DialogueContext`:

- ask_dialogue(InputBlock) → str, только не секретный learning input;
- drain_dialogue() → list[DialogueTurn] в серверном порядке, at-least-once до checkpoint;
- checkpoint_learning(intent: dict) → None: атомарно сохраняет проверенное намерение, подтверждает полученные turns и закрывает пройденный dialogue question.

Job.payload.learning_intent: `{entry_urls, requested_kinds, absent_kinds, user_note}`. Private dialogue cursor не попадает в API. Crash до checkpoint повторяет turn; повторное применение идемпотентно. Legacy JobContext без DialogueContext использует прежний ask, пустой drain и no-op checkpoint.

## Несколько сайтов

Один recipe по-прежнему имеет один origin. Явная ссылка человека на другой origin создаёт отдельный owned источник того же Space и CONNECT в той же беседе. Продолжение `{connectionId, jobId}` показывается в текущем разговоре, а старый рецепт и allowlist не расширяются wildcard. Bearer/пароль между origin не копируются; обычный вход проверяет сессию нового источника. Любое дополнительное разрешение сети ограничено конкретным host.

## Совместимость и приёмка

Новые колонки additive с default chat; старый образ может работать поверх расширенной БД. Отдельные client request/message IDs обеспечивают transaction idempotency. Кнопка добавления открывает обычный ChatScreen; известный вуз передаётся контекстом. Вопрос остаётся в хронологии и рядом с актуальным editor внизу, «Думаю» и Варя находятся в том же чате. Reload и новая вкладка восстанавливают worker/thread/connection/pending с сервера.

Проверки: ownership/foreign IDs, одинаковые и конфликтующие повторы, crash/replay, секреты вне history/Outbox, без дублирования CHAT/CONNECT, disabled/cancelled targets, старый API, stable DOM keys/order, обычный чат, keyboard/mobile200%, настоящий API→SSE→PWA на fakeuni. Живой вуз проверяет владелец.
