# BotAI — High-Level Design

Статус: **описание реализации**, обновлено 2026-10-31. Диаграммы — Mermaid: открываются в превью Markdown в IDE и на
GitHub. Детали модулей — [LLD](lld.md). Почему так — [ADR](../README.md#решения).

Принятое направление рефакторинга — [возобновляемое исследование](source-research.md):
существующая очередь Job, зашифрованный checkpoint в том же Postgres и свежий браузер
при продолжении. Это реализовано; статус этапов — в [плане](../plan/source-research-refactor.md).

## 1. Система целиком

```mermaid
flowchart LR
    subgraph Device["Устройство студента"]
        PWA["PWA BotAI<br/>React · TS"]
    end

    subgraph VPS["VPS в РФ (docker compose)"]
        direction TB
        API["API<br/>FastAPI"]
        Worker["Worker<br/>фоновые задания"]
        Browser["BrowserSvc<br/>Chromium · Playwright"]
        Proxy["EgressProxy<br/>allowlist хостов"]
        PG[("PostgreSQL<br/>домен · очередь · FTS · Vault")]
        Search["SearchService<br/>бюджеты · fallback"]
    end

    subgraph Outside["Внешний мир"]
        Uni["Сайты вуза<br/>SSO · LMS · ЛК · страницы"]
        Docs["Файлы и Google-документы<br/>студента"]
        LLM["DeepSeek API"]
        SearchAPI["Exa / DeepSeek Search API"]
        STT["Groq Speech-to-Text<br/>диктовка"]
    end

    PWA -- "REST + SSE" --> API
    PWA -. "WS: только запасной экран входа" .-> Browser
    API --> PG
    Worker --> PG
    API -- "задания" --> Worker
    Worker --> Browser
    Browser --> Proxy
    Worker -- "httpx по рецепту" --> Proxy
    Proxy --> Uni
    Proxy --> Docs
    Worker -- "обучение, починка, чат" --> LLM
    Worker -- "публичный поиск" --> Search
    Search --> Proxy
    Proxy --> SearchAPI
    API -- "диктовка: звук" --> Proxy
    Proxy -- "через реле" --> STT
```

Два правила, на которых держится всё остальное:

- **Источники и публичный поиск — через EgressProxy.** Allowlist источника привязан к
  подключению; поиск получает отдельное разрешение только для фиксированного API провайдера.
  Существующий LLM-провайдер использует отдельный фиксированный путь DeepSeek, который
  не получает URL из модели или источника. Поисковая выдача не расширяет доступ к вузу.
- **Секреты — только в Vault.** В LLM уходят данные и вопросы, но не пароль, cookie или токен.

## 2. Два контура

```mermaid
flowchart TB
    subgraph Learn["Контур обучения — редко, дорого, с человеком"]
        L1["Вход: зеркало формы<br/>или запасной экран"] --> L2["Explorer наблюдает<br/>запросы и разметку"]
        L2 --> L3["LLM предлагает<br/>спецификацию адаптера"]
        L3 --> L4["Lint: нет личных значений,<br/>нет чужих хостов"]
        L4 --> L5["Canary на сессии автора"]
        L5 --> L6["Вопросы студенту<br/>choice · input · confirm"]
        L6 --> L7["Итог: что собрано<br/>подтверждение"]
    end

    Spec[("Спецификация адаптера<br/>recipe · moodle_ws · ical · document<br/>версии, откат")]

    subgraph Exec["Контур исполнения — часто, дёшево, без LLM"]
        E1["Планировщик"] --> E2["Фабрика адаптеров"]
        E2 --> E3["Исполнитель + сессия<br/>пользователя из Vault"]
        E3 --> E4["Нормализатор"]
        E4 --> E5[("Снимки домена<br/>пары · дедлайны · оценки …")]
    end

    L7 --> Spec
    Spec --> E2
    E3 -- "ответ не сошёлся" --> R["Задание repair"]
    Chat["Студент в чате:<br/>«расписание сломалось»"] --> R
    Canary["Ежедневная канарейка"] --> R
    R --> L2
```

## 3. Фабрика источников (ADR-0005)

```mermaid
classDiagram
    class SourceAdapter {
        <<protocol>>
        +kind: AdapterKind
        +fetch(ctx: SyncContext) AdapterResult
    }
    class AdapterFactory {
        +create(connection, spec) SourceAdapter
    }
    class RecipeAdapter {
        JSON Pointer или CSS-селекторы
        пагинация · шаблоны дат
    }
    class MoodleWsAdapter {
        токен пользователя
        core_calendar_* · mod_assign_* · gradereport_*
    }
    class IcalAdapter {
        ссылка экспорта
        VEVENT → занятие / дедлайн
    }
    class DocumentAdapter {
        PDF · DOCX · XLSX · HTML · Google export
        текст → чанки → FTS
        таблица → строки по спецификации
    }
    SourceAdapter <|.. RecipeAdapter
    SourceAdapter <|.. MoodleWsAdapter
    SourceAdapter <|.. IcalAdapter
    SourceAdapter <|.. DocumentAdapter
    AdapterFactory ..> SourceAdapter : создаёт по spec.adapter
```

Вуз и документ — это данные (спецификация), а не код. Агент пишет спецификацию, но **никогда не
пишет исполняемый код**: текст со страницы вуза не может стать кодом на сервере.

## 4. Вход в вуз (ADR-0001)

```mermaid
sequenceDiagram
    autonumber
    actor S as Студент
    participant P as PWA
    participant A as API
    participant W as Worker (LoginFlow)
    participant B as Chromium
    participant U as Сайт вуза
    participant V as Vault

    S->>P: «Подключить вуз»
    P->>A: сообщение в чат (агент вызывает connect_source)
    A->>W: задание connect
    W->>B: открыть страницу входа
    B->>U: GET (через EgressProxy)
    W-->>P: SSE block: input «Почта», input secret «Пароль»
    S->>P: вводит в нативные поля BotAI
    P->>A: POST /jobs/{id}/answers (secret)
    A->>W: значения в память задания, мимо LLM
    W->>B: заполнить форму, отправить
    B->>U: POST формы входа
    alt нужен код MFA
        W-->>P: SSE block: input secret «Код из SMS»
        S->>P: код
        P->>A: answers
        A->>W: код
        W->>B: ввести код
    else push на телефон
        W-->>P: SSE block: wait «Подтвердите вход на телефоне»
    else капча или незнакомая страница
        W-->>P: SSE block: login (запасной экран)
        S->>B: работает с экраном по WS
    end
    B->>U: проверка признака входа
    W->>V: зашифровать пароль и сессию (AES-GCM, AAD)
    W-->>P: SSE status: вход выполнен → обучение
```

## 5. Чат и самопочинка

Агент один на все треды; остальные его инструменты (подключение, живое чтение источника, публичный поиск, память) —
в [харнесе агента](agent-harness.md).

```mermaid
sequenceDiagram
    autonumber
    actor S as Студент
    participant P as PWA
    participant C as ChatAgent
    participant D as Домен (SQL)
    participant I as Индекс (FTS)
    participant L as LLM
    participant R as Repair

    S->>P: «Какая у меня следующая пара?»
    P->>C: сообщение (задание CHAT, единый агент)
    loop модель выбирает инструменты, пока не ответит
        C->>L: контекст хода и вопрос (без секретов)
        L-->>C: вызов инструмента или ответ
        alt данные студента
            C->>D: get_schedule и другие: SQL с тенант-предикатом
            D-->>C: пары (склеенные из источников) + время обновления + источники
        else документы и факты
            C->>I: search_documents, search_facts
            I-->>C: фрагменты + источники + свежесть
        end
    end
    C-->>P: ответ + источники + «обновлено …»

    S->>P: «Расписание не показывается»
    P->>C: сообщение
    C->>R: задание repair для подключения
    R-->>P: SSE thinking: «Сравниваю с прошлой версией …»
    R-->>P: итог: «Вуз поменял адрес расписания, поправила»
```

## 6. Развёртывание

```mermaid
flowchart LR
    Dev["Агенты и владелец"] -- "PR" --> GH["GitHub"]
    GH -- "CI: lint · типы · тесты · сборка" --> CI["Actions"]
    CI -- "зелёный → автомерж" --> Main["main"]
    Main -- "deploy job<br/>SSH-ключ CI в Secrets" --> VPS["VPS: пользователь botai-deploy<br/>docker compose pull && up"]
    Owner["SSH владельца"] -. "только логи и отладка" .-> VPS
```

Деплой выполнен (сервер подготовлен 2026-10-27, дальше выкладывается каждый зелёный `main`): порядок, откат и проверки после выпуска —
[deployment.md](../plan/deployment.md). До выкладки всё проверяется локально и в CI на фикстурах.
