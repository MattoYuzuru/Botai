"""What a connection found, in the student's words: counts, the nearest classes, the closing line.

The text is assembled by code from the records of the canary (the student's own data, never shown to
a model) and read by the student. Source text is stripped of markdown before it is placed in a line.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from botai.contracts.specs import Kind
from botai.sources.adapter import Record
from botai.sources.vocab import KIND_LABELS

Forms = tuple[str, str, str]  # 1 занятие, 2 занятия, 5 занятий

UNITS: dict[Kind, Forms] = {
    "scheduleEvents": ("занятие", "занятия", "занятий"),
    "deadlines": ("задание со сроком", "задания со сроками", "заданий со сроками"),
    "courses": ("курс", "курса", "курсов"),
    "assignments": ("задание", "задания", "заданий"),
    "grades": ("оценка", "оценки", "оценок"),
    "people": ("преподаватель", "преподавателя", "преподавателей"),
    "locations": ("аудитория", "аудитории", "аудиторий"),
    "news": ("новость", "новости", "новостей"),
}
WEEKS: Forms = ("неделя", "недели", "недель")
_HOURS: Forms = ("час", "часа", "часов")
_MINUTES: Forms = ("минуту", "минуты", "минут")

# The order the student cares about: classes first, then deadlines.
ORDER: tuple[Kind, ...] = (
    "scheduleEvents",
    "deadlines",
    "courses",
    "assignments",
    "grades",
    "news",
    "people",
    "locations",
)

# The closing line names the section and what to ask about it.
_SECTION: dict[Kind, tuple[str, bool]] = {  # (nominative, plural)
    "scheduleEvents": ("Расписание", False),
    "deadlines": ("Дедлайны", True),
    "courses": ("Курсы", True),
    "assignments": ("Задания", True),
    "grades": ("Оценки", True),
    "people": ("Преподаватели", True),
    "locations": ("Аудитории", True),
    "news": ("Новости", True),
}
_ASK: dict[Kind, str] = {
    "scheduleEvents": "что у меня завтра?",
    "deadlines": "какие у меня сроки на этой неделе?",
    "courses": "какие у меня курсы?",
    "assignments": "какие у меня задания?",
    "grades": "какие у меня оценки?",
    "news": "что нового?",
    "people": "кто у меня преподаёт?",
    "locations": "где у меня занятия?",
}

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WEEKDAYS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
PREVIEW_DAYS = 2
PER_DAY = 8
_TITLE = 100
DETAILS_MAX = 2000  # ConfirmBlock.details

_MARKUP = re.compile(r"[*`\[\]<>\\|]|botai://")


def plural(n: int, forms: Forms) -> str:
    tens, hundreds = n % 10, n % 100
    if tens == 1 and hundreds != 11:
        return forms[0]
    if 2 <= tens <= 4 and not 12 <= hundreds <= 14:
        return forms[1]
    return forms[2]


def counted(n: int, forms: Forms) -> str:
    return f"{n} {plural(n, forms)}"


def found_text(kind: Kind, n: int) -> str:
    """ "Найдено занятий: 35": the stage line of a kind, in the words of that kind."""
    return f"Найдено {UNITS[kind][2]}: {n}"


def plain(text: object, limit: int = _TITLE) -> str:
    """Text of a source as one plain line: nothing in it can become a link, a bold run or a list."""
    line = " ".join(_MARKUP.sub(" ", str(text)).split())
    line = line.lstrip("#>-+ ")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _when(record: Record, name: str, tz: ZoneInfo) -> datetime | None:
    value = record.fields.get(name)
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)


def _date(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _days(records: Sequence[Record], tz: ZoneInfo) -> list[date]:
    return sorted({m.date() for r in records if (m := _when(r, "startsAt", tz))})


def weeks_to(last: datetime | None, now: datetime, tz: ZoneInfo) -> int | None:
    """How many calendar weeks from this one the classes reach (the current week counts), given the
    start of the last class; None when there is none or it is already over."""
    if last is None:
        return None
    today = now.astimezone(tz).date()
    end = last.astimezone(tz).date()
    return None if end < today else (_monday(end) - _monday(today)).days // 7 + 1


def kind_line(kind: Kind, records: Sequence[Record], tz: ZoneInfo) -> str:
    """ "35 занятий с 26 октября по 6 декабря — 6 недель", "12 заданий со сроками", "8 курсов"."""
    text = counted(len(records), UNITS[kind])
    days = _days(records, tz) if kind == "scheduleEvents" else []
    if not days:
        return text
    first, last = days[0], days[-1]
    if first == last:
        return f"{text} на {_date(first)}"
    weeks = (_monday(last) - _monday(first)).days // 7 + 1
    return f"{text} с {_date(first)} по {_date(last)} — {counted(weeks, WEEKS)}"


def upcoming_days(
    records: Sequence[Record], now: datetime, tz: ZoneInfo, limit: int = PREVIEW_DAYS
) -> list[tuple[date, list[Record]]]:
    """The nearest `limit` days that have classes still ahead, from today on; empty days are
    skipped, so a free week still yields the next two days with classes."""
    cut = now.astimezone(tz)
    by_day: dict[date, list[tuple[datetime, Record]]] = {}
    for record in records:
        start = _when(record, "startsAt", tz)
        if start is None or (_when(record, "endsAt", tz) or start) < cut:
            continue
        by_day.setdefault(start.date(), []).append((start, record))
    return [
        (day, [record for _, record in sorted(by_day[day], key=lambda pair: pair[0])])
        for day in sorted(by_day)[:limit]
    ]


def day_title(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {_date(day)}"


def event_line(record: Record, tz: ZoneInfo) -> str:
    """ "13:00–14:20 · Базы данных · В-310": time, title, room."""
    start = _when(record, "startsAt", tz)
    end = _when(record, "endsAt", tz)
    span = f"{start:%H:%M}" if start else ""
    if start and end and end != start:
        span += f"–{end:%H:%M}"
    parts = [
        span,
        plain(record.fields.get("title") or "Без названия"),
        plain(record.fields.get("location") or "", 40),
    ]
    return " · ".join(part for part in parts if part)


def preview_lines(records: Sequence[Record], now: datetime, tz: ZoneInfo) -> list[str]:
    days = upcoming_days(records, now, tz)
    if not days:
        return ["Ближайших пар в этих данных нет: все найденные занятия уже прошли."]
    lines = ["Ближайшие пары:"]
    for day, events in days:
        lines += ["", f"**{day_title(day)}**"]
        lines += [f"- {event_line(record, tz)}" for record in events[:PER_DAY]]
        if len(events) > PER_DAY:
            lines.append(f"- …и ещё {len(events) - PER_DAY}")
    return lines


def confirm_details(
    records: Mapping[Kind, Sequence[Record]],
    now: datetime,
    tz: ZoneInfo,
    *,
    heading: str = "Нашла в кабинете:",
) -> str:
    """The body of the confirmation: what is connected in words and the nearest classes."""
    lines = [plain(heading, 120), ""]
    lines += [
        f"- **{KIND_LABELS[kind]}:** {kind_line(kind, records[kind], tz)}"
        for kind in ORDER
        if kind in records
    ]
    if "scheduleEvents" in records:
        lines += ["", *preview_lines(records["scheduleEvents"], now, tz)]
    return "\n".join(lines)[:DETAILS_MAX]


def every_text(minutes: int) -> str:
    """ "каждые 3 часа", "каждый час", "раз в сутки"."""
    if minutes == 1440:
        return "раз в сутки"
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return "каждый час" if hours == 1 else f"каждые {counted(hours, _HOURS)}"
    return f"каждые {counted(minutes, _MINUTES)}"


def _ahead(weeks: int) -> str:
    """ "на 6 недель вперёд", "на 1 неделю вперёд"."""
    unit = plural(weeks, WEEKS)
    return f"на {weeks} {'неделю' if unit == 'неделя' else unit} вперёд"


def finished_text(
    counts: Mapping[Kind, int],
    *,
    university: str | None,
    every_minutes: int,
    weeks: int | None = None,
) -> str:
    """The closing message of a connection: what is connected, how it is kept fresh, what to ask.

    `counts` are what the first read stored; `weeks` is how far the classes reach (when known).
    """
    kinds: list[Kind] = [kind for kind in ORDER if counts.get(kind)]
    if not kinds:
        return (
            "Готово, источник подключён, но данных в нём пока не нашлось. "
            "Как только они появятся, обновлю сама."
        )

    def phrase(kind: Kind) -> str:
        text = counted(counts[kind], UNITS[kind])
        return f"{text} {_ahead(weeks)}" if kind == "scheduleEvents" and weeks else text

    where = f" {plain(university, 60)}" if university else ""
    if len(kinds) == 1:
        name, many = _SECTION[kinds[0]]
        head = (
            f"Готово! {name}{where} {'подключены' if many else 'подключено'}: {phrase(kinds[0])}."
        )
    else:
        parts = "; ".join(f"{KIND_LABELS[kind].lower()} — {phrase(kind)}" for kind in kinds)
        head = f"Готово! Данные{where} подключены: {parts}."
    ask = f"спросите, например, «{_ASK[kinds[0]]}»"
    if every_minutes <= 0:
        return f"{head} {ask[0].upper()}{ask[1:]}"
    return f"{head} Обновляю само {every_text(every_minutes)} — {ask}"
