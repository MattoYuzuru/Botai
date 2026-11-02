"""The source protocol the agent can inspect before writing a playbook (D-003)."""

from __future__ import annotations

from typing import Any

from botai.sources.v4.kinds import ALL_KINDS, DATA_KINDS, READING_KINDS
from botai.sources.v4.runner import CodeOutput, PlaybookSpecV1
from botai.sources.vocab import REQUIRED, VOCAB


def source_schema() -> dict[str, Any]:
    """Expose the actual validators, shared field vocabulary and an executable parser example."""
    return {
        "source_kinds": list(ALL_KINDS),
        "record_kinds": list(DATA_KINDS),
        "fields": {
            kind: {"required": sorted(REQUIRED[reading]), "allowed": sorted(VOCAB[reading])}
            for kind, reading in READING_KINDS.items()
        },
        "playbook_spec_schema": PlaybookSpecV1.model_json_schema(),
        "parser_output_schema": CodeOutput.model_json_schema(),
        "rules": [
            "spec не содержит auth/headers: сессия хранится через source_session save в Vault.",
            "Календарь: secret_request → source_create(link=handle). readUrl={secret} подставляет ссылку из Vault только при HTTP-чтении; используй его в source_read_live и spec.reads[].url. Значение ссылки не нужно модели, коду и playbook.",
            "iCalendar: сохраняй UID и экземпляр повторения в external_id. DTSTART;VALUE=DATE означает весь день: attributes=[{label:'Весь день',value:'Да'}], начало/конец в часовом поясе пространства, DTEND исключительный. Не объединяй разные календари по названию или времени.",
            "После входа: source_session save; в новом ходе source_session load для браузера.",
            "HTTP reads используют cookies и наблюдённый браузером Bearer только для его origin; сессия доступна коду в session.json.",
            "Код читает reads/<name>.json (либо .html/.txt) и печатает один JSON-объект.",
            "records: [{kind, external_id, fields}]. fields использует camelCase, не колонки БД.",
            "schedule: startsAt + endsAt (или durationMinutes); даты — ISO datetime с timezone.",
            "Пустой кодовый результат требует empty_reason с проверенной причиной отсутствия данных.",
            "Если сессия истекла или API вернул 401/403, код возвращает {auth_required: true}; source_sync отметит needs_reauth и не запишет records.",
            "Для declarative reads min_items задаёт ожидаемый минимум, 0 явно допускает пустой список.",
            "source_probe ничего не импортирует; после активации вызови source_sync и проверь counts.",
        ],
        "example": {
            "spec": {
                "version": 1,
                "reads": [{"name": "lessons", "url": "https://university.example/api/lessons"}],
            },
            "code": (
                "import json\n"
                "from pathlib import Path\n"
                "items = json.loads(Path('reads/lessons.json').read_text())['items']\n"
                "records = [{'kind': 'schedule', 'external_id': str(item['id']), 'fields': {"
                "'title': item['title'], 'startsAt': item['start'], 'endsAt': item['end']"
                "}} for item in items]\n"
                "result = {'records': records}\n"
                "if not items:\n"
                "    result['empty_reason'] = 'API вернул пустой items за запрошенный период'\n"
                "print(json.dumps(result))\n"
            ),
            "output": {
                "records": [
                    {
                        "kind": "schedule",
                        "external_id": "lesson-1",
                        "fields": {
                            "title": "Математика",
                            "startsAt": "2026-11-02T10:00:00+03:00",
                            "endsAt": "2026-11-02T11:30:00+03:00",
                        },
                    },
                    {
                        "kind": "deadlines",
                        "external_id": "task-1",
                        "fields": {"title": "Задание", "dueAt": "2026-11-03T23:59:00+03:00"},
                    },
                ]
            },
        },
    }
