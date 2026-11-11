"""The source protocol the agent can inspect before writing a playbook (D-003, D-043).

`guides` are the general rules of grades, task statuses and links (docs/v4/27 §3.2–§3.4): one
vocabulary for any university, no presets. The run report's `warnings` name the typical mistakes
they prevent (`sources.v4.checks`).
"""

from __future__ import annotations

from typing import Any

from botai.sources.v4.kinds import ALL_KINDS, DATA_KINDS, READING_KINDS
from botai.sources.v4.runner import CodeOutput, PlaybookSpecV1
from botai.sources.vocab import ALL_REQUIRED, ALL_VOCAB, GRADE_LAYERS

GUIDES: dict[str, list[str]] = {
    "grades": [
        "Найди все места, где сайт показывает оценки: страница задания, журнал или ведомость "
        "курса, зачётка, сводный отчёт, рейтинг. Это разные слои — пиши их разными записями "
        "с layer: item (работа), current (накопленный итог, считает источник), final (итог за "
        "период: ведомость, зачётка), summary (GPA, средний балл, кредит-рейтинг).",
        "Оценку за работу бери там, где она стоит рядом с максимумом (score + maxScore): пиши "
        "value, max, assignmentId, courseId, gradedAt, url, layer: item.",
        "Списки заданий часто отдают только активные. Оценённые лежат в другом фильтре или архиве "
        "(evaluated, graded, finished, «проверено», «завершённые») — прочитай и его.",
        "Нет оценки — нет записи: null, пустая строка, «—», «не оценено» — не оценка. 0 пиши, "
        "только если источник говорит, что работа проверена (есть дата проверки или статус "
        "«оценено»).",
        "Итог курса не складывай сам: пиши его, только если источник его показывает (current до "
        "закрытия ведомости, final — после). Вес элемента (weight) пиши, если он дан.",
        "Шкала у каждой записи своя: 10-балльная, 5-балльная, 100-балльная, проценты, зачёт/"
        "незачёт, буквы. Не переводи одну в другую. «Зачтено», «отлично», «A» — value текстом.",
        "«7,5», «8 из 10», «8/10», «86%» пиши как есть: числа извлекаются сами. Максимум, данный "
        "отдельно, — в max числом; scale тогда можно не писать.",
        "Один предмет в нескольких модулях или попытках — разные записи с period; пересдача не "
        "затирает первую оценку (свой external_id).",
        "Сводные показатели (GPA, средний балл, кредит-рейтинг) — layer: summary, только как дал "
        "источник, с period.",
    ],
    "task_status": [
        "state — одно из open, submitted, graded, missed, unknown, только если знаешь наверняка. "
        "stateText — подпись статуса с сайта как есть («Решение прикреплено», «На проверке»): "
        "студент видит её.",
        "Если в state не слово из набора, оно уходит в stateText, а состояние выводит словарь. "
        "Не выдумывай перевод: лучше stateText и unknown, чем неверное состояние.",
        "Для студента submitted и graded значат «от меня ничего не нужно»; «на доработке», "
        "«возвращено», «черновик» — open; срок прошёл без сдачи — missed.",
        "Связывай задание, дедлайн и оценку одним идентификатором: deadlines.assignmentId и "
        "grades.assignmentId — external_id задания.",
        "Если студент говорит, что статус значит другое, — это правило статуса (study_status_rule), "
        "а не правка playbook.",
    ],
    "links": [
        "url записи ведёт на страницу этой сущности — задания, пары, оценки, курса, — а не на "
        "список и не на главную.",
        "Собирай адрес из идентификаторов ответа (курс, тема, задание); шаблон страницы найди, "
        "открыв одну сущность в браузере, и проверь одной ссылкой.",
        "Нет у сущности своей страницы — url не пиши: ссылку на источник покажет приложение.",
    ],
    "warnings": [
        "Отчёт probe/canary/sync содержит warnings — подсказки, не ошибки: generic_links (у всех "
        "записей одна ссылка), unknown_states (≥ 30 % дедлайнов unknown), grades_without_numbers "
        "(числовые оценки без max), no_item_grades (оценённые задания без оценок за работу). "
        "Исправь playbook и проверь снова.",
    ],
}


def source_schema() -> dict[str, Any]:
    """Expose the actual validators, shared field vocabulary and an executable parser example."""
    return {
        "source_kinds": list(ALL_KINDS),
        "record_kinds": list(DATA_KINDS),
        "fields": {
            kind: {"required": sorted(ALL_REQUIRED[reading]), "allowed": sorted(ALL_VOCAB[reading])}
            for kind, reading in READING_KINDS.items()
        },
        "grade_layers": list(GRADE_LAYERS),
        "playbook_spec_schema": PlaybookSpecV1.model_json_schema(),
        "parser_output_schema": CodeOutput.model_json_schema(),
        "guides": GUIDES,
        "rules": [
            "spec не содержит auth/headers: сессия хранится через source_session save в Vault.",
            "Календарь: secret_request → source_create(link=handle). readUrl={secret} подставляет ссылку из Vault только при HTTP-чтении; используй его в source_read_live и spec.reads[].url. Значение ссылки не нужно модели, коду и playbook.",
            "iCalendar: сохраняй UID и экземпляр повторения в external_id. DTSTART;VALUE=DATE означает весь день: attributes=[{label:'Весь день',value:'Да'}], начало/конец в часовом поясе пространства, DTEND исключительный. Не объединяй разные календари по названию или времени.",
            "После входа: source_session save; в новом ходе source_session load для браузера.",
            "HTTP reads используют cookies и наблюдённый браузером Bearer только для его origin; сессия доступна коду в session.json.",
            "Код читает reads/<name>.json (либо .html/.txt) и печатает один JSON-объект.",
            "records: [{kind, external_id, fields}]. fields использует camelCase, не колонки БД.",
            "grades: value — как напечатал источник; max — верх шкалы числом, если дан отдельно; layer, period, assignmentId, url, comment — см. guides.grades. Пустое значение не пиши строкой «None»: пропусти запись. Средние не считай.",
            "deadlines: state (open|submitted|graded|missed|unknown) + stateText (подпись с сайта) — см. guides.task_status.",
            "url каждой записи — страница этой сущности, не список: guides.links.",
            "profile: одно поле карточки студента на запись: fields {field (ключ), label (подпись), value (строка как в источнике), group?}. Пароли и токены не сохраняются.",
            "ranking: одно место рейтинга на запись: fields {board (название списка), position (место, целое ≥ 1), label (имя или номер), score (текст как в источнике), scoreNum? (число), isMe? (true для своей строки), total? (всего в списке), period?}. Чужих людей храни минимально: имя и балл.",
            "schedule: startsAt + endsAt (или durationMinutes); даты — ISO datetime с timezone.",
            "Пустой кодовый результат требует empty_reason с проверенной причиной отсутствия данных.",
            "Если сессия истекла или API вернул 401/403, код возвращает {auth_required: true}; source_sync отметит needs_reauth и не запишет records.",
            "Для declarative reads min_items задаёт ожидаемый минимум, 0 явно допускает пустой список.",
            "source_probe ничего не импортирует; после активации вызови source_sync и проверь counts и warnings.",
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
                        "fields": {
                            "title": "Домашнее задание 3",
                            "dueAt": "2026-11-03T23:59:00+03:00",
                            "state": "submitted",
                            "stateText": "Решение прикреплено",
                            "courseId": "course-7",
                            "url": "https://university.example/courses/7/tasks/task-1",
                        },
                    },
                    {
                        "kind": "grades",
                        "external_id": "grade-task-1",
                        "fields": {
                            "title": "Домашнее задание 3",
                            "value": "8,5",
                            "max": 10,
                            "layer": "item",
                            "assignmentId": "task-1",
                            "courseId": "course-7",
                            "gradedAt": "2026-11-05T12:00:00+03:00",
                            "url": "https://university.example/courses/7/tasks/task-1",
                        },
                    },
                ]
            },
        },
    }
