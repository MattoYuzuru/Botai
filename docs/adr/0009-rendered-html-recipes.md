# ADR-0009: ограниченное чтение отрисованного HTML

- Статус: accepted, 2026-10-28.
- Требования: FR-008/011/016/032/033, NFR-001/004/005/006/008/012.

## Контекст

Explorer видит DOM после JavaScript, а HTTP-исполнитель получает пустую оболочку SPA. Поэтому верный CSS-рецепт не подтверждается canary. Глубокое содержимое также терялось при подготовке короткого DOM-префикса для модели.

## Решение

Сначала предпочитать наблюдаемый воспроизводимый HTTP/JSON endpoint. Для подтверждённого DOM-only источника HTML endpoint может явно задать request.render=true (по умолчанию false). Только GET без тела, response.format=html, после отдельной canary; adapter остаётся recipe, исполняется только декларативное извлечение. Новый произвольный parser code запрещён. Исполнитель использует `RenderedPageReader`; HTTP-рецепты сохраняют прежний путь без Chromium.

Browser reader использует тот же tenant-bound session/Vault и EgressProxy; разрешает GET/HEAD/OPTIONS, блокирует WebSocket и остальные методы до создания страниц, оставляет service workers выключенными. Никаких действий, учётных данных или LLM при sync. Бюджеты: не более 3 отрисованных страниц и 30 секунд суммарно на fetch; ожидание itemsSelector ограничено. Фоновый reader не закрывает активный вход человека: при занятом контексте возвращается ограниченная retriable ошибка, прежний снимок сохраняется. Кадры, DOM и cookies на диск не пишутся.

Один recipe — один origin. Чужие хосты не добавляются из текста страницы автоматически. Исходный URL/query исследуется первым; {weekStart} означает понедельник текущей недели в timezone источника для дальнейшего детерминированного sync.

## Последствия и проверка

Chromium дороже HTTP, поэтому применяется по явному проверенному флагу. Проверяются HTTP-shell + rendered DOM с данными, запрет POST/WS, egress/tenant isolation, canary и повторный sync без LLM, busy foreground login, таймаут и сохранение последнего снимка. Наблюдение отдаёт модели ограниченный приоритетный skeleton содержимого, а не первый кусок навигации.

Техническая опора: [BrowserContext routing](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route) и [WebSocket routing](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket). Установленная версия проверяется по lock-файлу и тестам.
