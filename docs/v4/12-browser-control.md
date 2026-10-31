# Полный браузерный контроль и persistent-профили (v4, фаза 1.3–1.4)

Статус: **реализовано 2026-10-31**, пилот v4, task-13. Модули
`backend/src/botai/browser/control.py` (`BrowserControl`) и
`backend/src/botai/browser/profiles.py` (`BrowserProfileStore`); тесты
`backend/tests/browser/test_control.py`, `test_profiles.py` на настоящем
Chromium и локальном HTTP-сайте (`tests/browser/local_site.py`).

Основание: [02-architecture.md](02-architecture.md) §5 «Браузер» и §4.4
«Полный каталог инструментов», [05-implementation-plan.md](05-implementation-plan.md)
§1.3–1.4, решения [D-005](06-decisions.md) (сеть без allowlist),
[D-007](06-decisions.md) (антибот — техническая задача, человек-в-цикле),
[D-008](06-decisions.md) (сессия — рабочий материал агента),
[D-014](06-decisions.md) (полная трассировка).

`BrowserControl` намеренно **независим** от `browser/login.py` (вход v3) и
`browser/screen.py` (удалённый экран): он владеет своими контекстами и
страницами, а его страница никогда не является контекстом входа подключения.
Это библиотека для будущего `agent/tools/browser.py`; JSON-схемы инструментов
`browser_*` живут в runtime-слое и вызывают методы отсюда.

## 1. Что умеет

| Возможность | Метод |
|---|---|
| Навигация по любому URL/редиректу/IP | `navigate(url, wait_until=..., timeout_s=...)` |
| Клик (CSS/текст/роль/test id/label/координаты) | `click(target, button=..., click_count=..., modifiers=...)` |
| Ввод текста, клавиши, выбор в `<select>` | `type(target, text, clear=...)`, `press(target|None, key, modifiers=...)`, `select(target, value|label|index)` |
| Ожидания | `wait(kind, value, timeout_s=...)`: `selector`, `text`, `network_idle`, `timeout`, `navigation` |
| Чтение | `read_text`, `read_dom` (живой `outerHTML`), `read_links`, `read_tables`, `read_aria`, универсальный `read(mode, target?, max_chars?)` |
| JavaScript | `eval_js(expression, arg=..., await_promise=True, timeout_s=...)` |
| Скриншоты | `screenshot(full_page=..., target=..., image_format="png"|"jpeg", quality=...)` → `bytes` |
| Cookies | `cookies(urls?)`, `set_cookies(...)`, `clear_cookies()` |
| localStorage/sessionStorage | `get_storage(kind, origin?)`, `set_storage(data, kind, origin?, clear=...)` |
| Storage state | `export_storage_state(indexed_db=False)`, `import_storage_state(state)` |
| Сеть | `network_log()`, `clear_network_log()`, `har_start()`, `har_stop()` → HAR 1.2 |
| Скачивание/загрузка | `download(target, save_dir?, filename?)`, `upload(target, files)` |
| Вкладки | `tabs()`, `new_tab(url?)`, `switch_tab(tab_id)`, `close_tab(tab_id)` |
| Контексты | `contexts()`, `new_context(storage_state?)`, `switch_context(id)`, `close_context(id)` |
| Диалоги | `dialogs()` — `alert/confirm/prompt` автоматически закрываются и запоминаются |
| Закрытие | `close()` (идемпотентно), `async with BrowserControl.launch(...) as control` |

Прямой сети и allowlist нет: `BrowserControl` запускает обычный Chromium (у
gateway — отдельный браузер без прокси и без `--host-resolver-rules`), поэтому
доступны любой домен, редирект, IP-литерал, WebSocket и загрузки. Прокси можно
задать явно полем `ControlConfig.proxy` (например, логирующий прокси будущей
настройки `egress_mode="log"`), но по умолчанию — прямой выход.

```python
from botai.browser.control import BrowserControl, ControlConfig

control = await BrowserControl.launch(config=ControlConfig(headless=True))
try:
    info = await control.navigate("https://example.org/login")
    await control.type({"selector": "#email"}, "student@example.org")
    await control.click({"role": "button", "name": "Продолжить"})
    await control.wait("selector", "#password", timeout_s=10)
    await control.type({"label": "Пароль"}, "correct horse battery staple")
    await control.press({"selector": "#password"}, "Enter")
    text = await control.read_text()
    shot = await control.screenshot(full_page=True)  # bytes → артефакт
    cookies = await control.cookies()
finally:
    await control.close()
```

## 2. Цели (`Target`)

Один элемент задаётся ровно одной стратегией:

```python
{"selector": "#login"}                  # CSS/Playwright-селектор (включая text=, xpath=)
{"text": "Войти"}                       # видимый текст (exact=False по умолчанию)
{"role": "button", "name": "Войти"}     # ARIA role + доступное имя
{"test_id": "submit"}                   # data-testid
{"label": "Пароль"}                     # <label>
{"x": 120, "y": 640}                    # координаты на canvas/странице (только click)
```

`Target.from_mapping(...)` валидирует и даёт `describe()` — человеческое
описание для ошибки, без содержимого страницы. `as_target(...)` принимает
`Target` или dict — удобно принять JSON из инструмента.

## 3. Конфигурация

`ControlConfig` (frozen dataclass; всё — возможности, не политика):

| Поле | По умолчанию | Смысл |
|---|---|---|
| `headless` | `True` | headful требует Xvfb в контейнере (`browser`) |
| `sandbox` | `None` → как у gateway | `chromium_sandbox` |
| `ignore_https_errors` | `False` | только для локальной отладки/фикстур |
| `viewport`, `locale`, `timezone`, `user_agent` | 1280×800, `ru-RU` | согласованность с устройством владельца |
| `geolocation`, `permissions` | нет, `()` | разрешения/геолокация по запросу |
| `proxy` | `None` | `http(s)://`/`socks5://` (прямой выход по умолчанию) |
| `accept_downloads` | `True` | загрузки |
| `capture_network_bodies` | `False` | хранить тела запросов/ответов в логе/HAR (память; тела — материал агента) |
| `downloads_dir` | `None` | куда складывать скачанное; иначе 0700-временный каталог |
| `upload_root` | `None` | каталог workspace/артефактов run; загрузки с диска разрешены только внутри него |
| `redact_body` | `None` | hook `Callable[[str], str]`, которым runtime маскирует первичные секреты в телах |
| `default_timeout_ms`, `navigation_timeout_ms` | 15 с, 30 с | таймауты действий и навигации |

Ошибки — `BotaiError` с кодами: `validation` (плохой URL/цель/arg),
`not_found` (элемент/вкладка/контекст не найдены или не дождались),
`source_unavailable` (страница не ответила/навигация упала),
`conflict` (браузер закрыт, контекст/вкладка закрыты, HAR уже пишется),
`not_implemented` (несколько контекстов у постоянного профиля). Playwright-текст
ошибки никогда не пересказывается: он может содержать значения полей или
содержимое страницы.

## 4. Данные результатов

* `PageInfo(url, title, tab_id, context_id)`, `TabInfo(...)`, `ContextInfo(...)` — `.to_dict()`;
* `NetworkEntry(...)`: method, url, resource_type, status, request/response
  headers, `request_body`/`response_body` (если `capture_network_bodies`),
  failed, started_at, duration_ms; `to_dict()` и `to_har_entry()`;
* `DownloadInfo(suggested_filename, url, path, size_bytes, sha256)` — файл уже
  на диске (каталог 0700, файл 0600), хеш позволяет трассе ссылаться на него;
* `StorageImportResult(cookies_added, origins_applied, origins_failed)`.

HAR: `har_start()` начинает запись, `har_stop()` возвращает HAR 1.2 и
выключает её (`conflict`, если HAR не писался). Записи общие для контекстов
этого контроля. Тела в HAR попадают только при `capture_network_bodies=True`
и только после редакции (см. §9): пароли/MFA/ключи в телах не сохраняются.

## 5. Persistent-профили (`BrowserProfileStore`)

Профиль — серверная «как текущее устройство» память браузера: cookies,
localStorage, IndexedDB, service workers и cache. Это не дубль Vault: сессию
приложения по-прежнему хранит Vault, профиль хранит браузерное состояние.
Копирование реального Chrome-профиля с мака — задача `bench/live`, здесь только
серверный профиль, который агент растит сам или в который импортируются cookies.

Раскладка (каталоги `0700`, файлы `0600`):

```
<root>/<user>/<profile>/chromium/            # Chromium user-data dir
<root>/<user>/<profile>/storage_state.json   # Playwright storage state (cookies + localStorage)
<root>/<user>/<profile>/.lock                # fcntl.flock (один браузер на профиль)
```

`root` — `BOTAI_BROWSER_PROFILES_DIR`, иначе `$XDG_STATE_HOME/botai-v4-browser/profiles`
или `~/.local/state/botai-v4-browser/profiles`. Для контейнера настройка задаёт
том (профили — данные владельца, не tmpfs).

```python
from botai.browser.profiles import BrowserProfileStore, locked_profile

store = BrowserProfileStore()
async with locked_profile(store, user_id, "default") as directory:
    control = await BrowserControl.launch_persistent(
        store.user_data_dir(user_id, "default"),
        gateway=gateway,  # общий Playwright-драйвер; без него стартует свой
    )
    try:
        ...  # работа агента; cookies/IndexedDB/SW переживут закрытие
    finally:
        await control.close()
```

Методы store: `profile_dir`, `user_data_dir`, `storage_state_path`, `lock`
(`async with`), `load_storage_state`, `save_storage_state`, `import_cookies`,
`clear_storage_state`, `list`, `delete`; `locked_profile(store, user, name)` —
готовый асинхронный контекст с эксклюзивной блокировкой.

* `import_cookies(user, profile, cookies)` — слияние по `name+domain+path`
  (так CDP/Chrome-хелпер сидит серверный профиль, без копирования профиля).
* `save_storage_state`/`load_storage_state` — атомарная запись (tmp+`os.replace`),
  `0600`; перед запуском можно передать состояние в
  `BrowserControl.launch(storage_state=...)`.
* `validate_storage_state`/`validate_cookie` — границы (500 cookies, 30 origins,
  2 МБ, только `http(s)` origins без кредов в URL); имена `user`/`profile` —
  `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, чтобы путь не вышел за каталог профилей.
* `lock` — `fcntl.flock`: второй браузер на том же `user-data-dir` получает
  `conflict`, иначе Chromium повреждает профиль. Один профиль — один браузер.

`launch_persistent` принимает `user_data_dir` **и обязательный `lock`** —
`store.lock(user, profile)` (контроль сам входит в блокировку и отпускает её в
`close()`): Chromium повреждает user-data dir, открытый дважды, поэтому запуск
без лока запрещён (`conflict` с понятным текстом), а второй запуск того же
профиля получает `conflict` от `flock`. Каталог создаётся `0700`, у
постоянного профиля один контекст (так устроен Chromium), поэтому
`new_context()` отвечает `not_implemented`. Изоляцию даёт отдельный профиль.

## 6. Изоляция и жизненный цикл

* `BrowserControl.launch(gateway=...)` — общий с gateway браузер (общий
  Playwright-драйвер и его tmp-каталог), но **свои контексты**. Логин-браузер
  gateway (прокси, allowlist, guard'ы) живёт отдельно; `gateway.control_browser`
  запускает второй Chromium без прокси и резолвер-правил и закрывает его в
  `gateway.aclose()`.
* `BrowserControl.launch()` без gateway — свой Playwright-драйвер и браузер,
  закрываются вместе с контролем.
* Каждый `new_context()` — отдельный cookie-jar и storage; страницы одного
  контекста — вкладки. `close_context`/`close_tab` закрывают ресурсы, `close()`
  идемпотентен; после него действия отвечают `conflict`.
* Вкладки/попапы регистрируются через событие `context.on("page")`; закрытие
  вкладки снимает её из списка, активной становится соседняя.

## 7. Сеть, HAR и наблюдаемость

* Все запросы/ответы контекста пишутся в ограниченный лог (2000 записей на
  контекст): метод, URL, тип ресурса, статус, заголовки, длительность. Это
  **материал агента** (D-008): он может читать заголовки/тела и HAR.
* `capture_network_bodies=True` дополнительно хранит тела запросов/ответов
  (до 20 000 символов на тело; только текстовые/JSON/XML).
* В лог приложения (`logging`) не попадают ни URL, ни заголовки, ни тела, ни
  значения полей, ни текст страниц: только счётчики событий и размеры
  (тест `test_page_content_cookies_and_typed_values_never_reach_logs`).
* Трассировка — обязанность runtime-слоя: `screenshot()` возвращает `bytes`,
  `har_stop()` — dict, `download()` — путь+хеш; runtime сохраняет их артефактами
  с provenance (D-014). Control сам артефакты не пишет (кроме скачанных файлов
  на диск в указанный каталог).

## 8. Как это стыкуется с runtime-инструментами `browser_*`

Runtime-engineer создаёт `agent/v4/**` и называет интерфейс инструментов;
`BrowserControl` от него не зависит. Ожидаемое соответствие (02 §4.4):

| Инструмент | Вызов control |
|---|---|
| `browser_open(url?, profile?, headless?, viewport?, locale?, timezone?)` | `BrowserControl.launch(...)` / `launch_persistent(...)`; профиль — `BrowserProfileStore` |
| `browser_navigate(url, wait_until?, timeout_s?)` | `navigate(...)` |
| `browser_click(target, button?, click_count?, modifiers?)` | `click(...)` |
| `browser_type/browser_press/browser_select` | `type/press/select` |
| `browser_read(mode, target?, max_chars?)` | `read(...)` |
| `browser_eval(expression, await_promise?, args?)` | `eval_js(...)` |
| `browser_screenshot(full_page?, target?, format?)` | `screenshot(...)` → runtime пишет артефакт |
| `browser_wait(kind, value?, timeout_s?)` | `wait(...)` |
| `browser_tabs(action, tab_id?, url?)` | `tabs/new_tab/switch_tab/close_tab` |
| `browser_cookies(action, cookies?, handle?)` | `cookies/set_cookies/clear_cookies` + `export/import_storage_state` |
| `browser_storage(action, kind, data?, origin?)` | `get_storage/set_storage` |
| `browser_network(action, url_filter?)` | `network_log/har_start/har_stop` |
| `browser_download/browser_upload` | `download/upload` |
| `browser_close(scope, save_session?)` | `close`, `close_context`, `close_tab`; при `save_session` runtime делает `export_storage_state` → `BrowserProfileStore.save_storage_state` |

Сознательно **не реализовано в этом модуле** (следующие задачи):

* `browser_intercept` (правила подмены/блокировки запросов) — слой перехвата
  будет поверх Playwright `route` в runtime/egress; control сейчас только
  наблюдает.
* `browser_frames` — `PageSnapshot` v3 тоже смотрит главный документ; API
  фреймов/iframe появится отдельно.
* `browser_device` на ходу — устройство задаётся `ControlConfig` при запуске
  (UA/viewport/locale/timezone/geolocation/permissions), без переключения в
  середине сессии.
* IndexedDB чтение/запись через отдельный метод — пока через `eval_js`,
  persistent-профиль или `export_storage_state(indexed_db=True)`.
* `browser_remote` — это `browser/screen.py` (человек-в-цикле), не control.
* attach к внешнему Chrome владельца по CDP — `bench/live`, не серверный слой.
* нативный HAR-рекордер Playwright (`record_har_path`) не используется: HAR
  строится из наблюдённых событий, чтобы быть доступным в любой момент; при
  необходимости runtime может включить встроенный рекордер отдельно.

## 9. Безопасность и ограничения (hardening)

Этот слой возвращает агенту материал сессии (D-008), но не превращает его в
утечку:

1. **Загрузка с диска — только внутри workspace.** `upload()` принимает
   `(name, bytes)` всегда, а путь (`Path`/`str`) — только если задан
   `ControlConfig.upload_root` (каталог run/workspace). Путь резолвится
   (`resolve()`), и файл вне корня получает `forbidden` «Загрузка: файл вне
   разрешённого каталога run» — произвольный путь ФС, в том числе `/etc/...`,
   передать нельзя. Без `upload_root` дисковый путь отклоняется `validation`
   с подсказкой передать bytes.
2. **Редакция тел перед сохранением и выдачей.** Тела запросов/ответов
   сохраняются в network log/HAR только после `BrowserControl._redact`:
   первый шаг — hook `ControlConfig.redact_body` (runtime подставляет маскировку
   известных секретов из secret broker), затем встроенная маска полей
   `password/passwd/pwd/secret/otp/mfa/passcode/cvv/security_answer/
   private_key/api_key/llm_key/credential` для JSON и form-urlencoded.
   Тела, попавшие в лог до появления hook, невозможны (hook задан при запуске),
   а `har_stop()`/`network_log()` всегда отдают уже отредактированные тела.
   Сессионный материал (cookies, bearer) по D-008 не маскируется — runtime
   решает, что писать в артефакты и трассу. Тест с паролем в POST-теле и
   кастомным hook: значения не появляются ни в network log, ни в HAR.
3. **Постоянный профиль — только под эксклюзивной блокировкой.** См. §5:
   `launch_persistent(..., lock=store.lock(user, profile))`; иначе `conflict`.
4. **Размер storage state ограничен до записи.** `validate_storage_state`
   считает сериализованный JSON (не только число записей): больше
   `MAX_STATE_BYTES` (2 МБ) — `validation` «Storage state: слишком большой»,
   файл не создаётся; то же при импорте в `BrowserControl.import_storage_state`.
   Обрезка не применяется: молча потерять cookie хуже, чем явная ошибка.
5. **Логи.** `logging` получает только счётчики/размеры; URL, заголовки, тела,
   значения полей и текст страниц не логируются (тест на отсутствие маркеров в
   `caplog`). Каталог профиля — `0700`, файлы состояния — `0600`; первичные
   секреты в профиль/state не пишутся вовсе — их место в Vault/secret broker.
6. **Сеть.** Control-браузер по умолчанию выходит напрямую без прокси и без
   allowlist (D-005): это осознанная возможность, а не дыра; логирующий прокси
   `egress_mode="log"` подключается полем `proxy` (следующий шаг egress).

## 10. Как проверить

```bash
cd backend
uv run --frozen pytest tests/browser/test_control.py -q     # 26 тестов
uv run --frozen pytest tests/browser/test_profiles.py -q    # 23 теста
uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen pyright
```

Тесты поднимают локальный HTTP-сайт (`tests/browser/local_site.py`, loopback ASGI,
без TLS и без EgressProxy) и настоящий Chromium через общий gateway. Покрыто:
навигация по IP-литералу и через редирект, клики/ввод/клавиши/выбор, чтение
text/DOM/таблиц/ARIA, `eval_js` с аргументом и с неожидаемым промисом, PNG/JPEG и
элементный скриншот, диалоги, cookies/localStorage/sessionStorage, экспорт и
импорт storage state в новый контекст, network log и HAR с телом JSON-ответа,
скачивание (0600) и загрузка (bytes и файл с диска), вкладки/попап/контексты и
их изоляцию, понятные ошибки (validation/not_found/source_unavailable/conflict),
идемпотентное закрытие, отсутствие секретов/страниц/значений в логах.

Hardening покрыт отдельно: отказ загрузки пути вне `upload_root`, маска пароля
в POST-теле по умолчанию и по hook `redact_body` (network log и HAR), отказ
запуска постоянного профиля без лока и конфликт второго захвата, отказ
storage state больше 2 МБ без создания файла.

`test_profiles.py` проверяет 0700/0600, дефолтный root из env, round-trip
storage state, слияние cookies, границы и валидацию, эксклюзивный lock,
`locked_profile`, список/удаление профилей и — на реальном Chromium — что
persistent user-data dir сохраняет cookie и localStorage между двумя запусками,
а сохранённый storage state восстанавливается в свежем контексте.

Риски и границы: постоянный профиль — один контекст и один браузер (lock);
тела сети в памяти при `capture_network_bodies` (размер ограничен); прямой
выход control-браузера не проходит через логирующий прокси (это следующий шаг
egress `direct|log`); содержимое страниц/сессий — материал агента (D-008).
Встроенная маска ловит типовые поля тел, а значения, известные только
runtime/secret broker, маскирует hook `redact_body`; трассы и артефакты
по-прежнему обязан фильтровать runtime-слой, а не этот модуль.
