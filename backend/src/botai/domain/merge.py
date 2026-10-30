"""Owner: W1. The read-time merge of the schedule events of several sources of one university
(FR-054, owner's decision of 30.10.2026: «склеивать, основной решает»).

A university can have a timetable from its cabinet and the same classes in a calendar. Stored rows
stay per source (sync never knows about the merge); every read of the schedule runs the pure
functions below over the rows of one space:

* the primary source's events are the base; each other source (oldest first) is matched to what is
  there already, so the primary decides, then the older of the others, then the newer;
* two events match when their times agree (start and end each within `TIME_TOLERANCE`, or the
  intervals overlap by at least `STRONG_OVERLAP` of their union) and their titles are similar
  (`TITLE_MATCH`); matching is one to one per source, best title first, then the closest time;
* two events of different kinds never match: a lecture and a seminar of one course, or a class and
  its exam, are different events even at the same hour (known kinds that differ, an assessment word
  — экзамен, контрольная, зачёт — in only one title);
* a matched pair is one class: the base keeps its time, title and kind, fills its empty fields from
  the other and lists both sources. A value both have and differ on stays the base's, and the
  class carries an `EventDisagreement` naming the other source's value (also words in the title
  that the base does not have);
* an event with no match stays in the list as its own class. If the primary source has a class of
  a similar title within `NEARBY` of it but none at this slot, and its regular data (`Coverage`)
  reaches this slot, the class is marked `not_in_primary` (likely moved or cancelled): shown with a
  note, never hidden.

No LLM, no database and no mutation of the stored rows. Only events of one space meet; spaces
(universities) never merge, and cross-space conflicts (FR-013) are computed afterwards by the caller
on the merged list. Candidates are found by bisecting a start-sorted list, so the cost stays
near-linear for a three-month window.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from botai.domain.schemas import (
    EventAttribute,
    EventDisagreement,
    EventLink,
    EventSource,
    ScheduleEventOut,
)

# Times. A class break at a university is ten minutes or more, far from the 80-90 minutes between
# two classes' starts, so ten minutes of slack cannot join neighbours; the overlap rule covers a
# calendar that rounds a class up to a whole hour or adds a quarter of an hour for the walk.
TIME_TOLERANCE = timedelta(minutes=10)
STRONG_OVERLAP = 0.7  # overlap / union of the two intervals
# Longer events (a whole-day reminder, a vacation) are not classes: they never match, they show.
MAX_MATCH_DURATION = timedelta(hours=12)

# Titles. In the same slot a student cannot be in two places, so half of the words in common is
# enough; to say that a class "exists elsewhere in the primary" the name must be more convincing.
TITLE_MATCH = 0.5
SIMILAR_ELSEWHERE = 0.6
CONTAINMENT_SIMILARITY = 0.75  # one title says everything the other says (two words or more)
NEARBY = timedelta(days=7)
# A calendar rounds the end of a class (80 minutes become an hour and a half): that is noise. The
# start moving, or the end by more than this, is a different time.
END_NOISE = timedelta(minutes=15)
# An event with no length (a reminder) is a minute long for matching, so that the same reminder in
# two sources still meets itself.
ZERO_LENGTH = timedelta(minutes=1)
# The primary's data "reaches" a slot while its regular classes follow each other with no gap longer
# than this (a week of holidays inside a term stays covered, a summer does not).
COVERAGE_GAP = timedelta(days=14)

MAX_DETAIL = 400
MAX_LINKS = 4  # the limits of the domain contract
MAX_ATTRIBUTES = 8

Kind = Literal["time", "title", "location", "teacher", "group", "address"]

# Words that tell nothing about which class it is: function words and the kind of a class, which
# the source gives in another field («Лекция: Матанализ» is «Матанализ»).
_STOP = frozenset(
    {"и", "в", "во", "на", "по", "для", "с", "со", "к", "о", "об", "от", "из", "за", "а", "не"}
    | {"или", "the", "of", "a", "an", "in", "on", "to", "for", "and"}
)
_KIND_WORDS = {"лекция", "семинар", "практика", "практическое", "занятие", "пара", "урок"} | {
    "lecture",
    "seminar",
    "practice",
    "class",
    "lesson",
}
_ENDINGS = "аеёиоуыэюяйь"
_WORD = re.compile(r"[^\W_]+")
_ROOM_NOISE = frozenset({"ауд", "аудитория", "каб", "кабинет", "комн", "комната", "room"})
_HONORIFICS = frozenset(
    {"проф", "профессор", "доц", "доцент", "преп", "преподаватель", "ст", "старший"}
    | {"асс", "ассистент", "dr", "prof"}
)


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _stem(word: str) -> str:
    """A cheap root: Russian endings off, six letters at most. «Анализа» and «анализ» agree."""
    if word.isdigit():
        return word
    if word.isascii():
        return word[:-1] if len(word) > 3 and word.endswith("s") else word
    for _ in range(2):
        if len(word) > 4 and word[-1] in _ENDINGS:
            word = word[:-1]
    return word[:6]


_KIND_STEMS = frozenset(_stem(_norm(w)) for w in _KIND_WORDS)

# What a title says about the type of the class, for sources that do not give a kind of their own.
_TITLE_KIND = {
    _stem(_norm(word)): kind
    for kind, words in (
        ("lecture", ("лекция", "lecture")),
        ("seminar", ("семинар", "практика", "практическое", "seminar", "practice")),
    )
    for word in words
}
# Words that make a class an assessment, never "the same class" as one without the word.
_ASSESSMENT = frozenset(
    _stem(_norm(word))
    for word in (
        "экзамен",
        "контрольная",
        "контроль",
        "зачёт",
        "консультация",
        "коллоквиум",
        "пересдача",
        "тест",
        "тестирование",
        "exam",
        "test",
        "quiz",
        "midterm",
    )
)
IRREGULAR_KINDS = frozenset({"exam", "consultation"})


def _title_words(title: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(_norm(title)) if w not in _STOP]


def assessment_words(title: str) -> frozenset[str]:
    """The assessment words of a title (экзамен, контрольная, зачёт, консультация…), as roots."""
    return frozenset(w for w in _title_words(title) if w in _ASSESSMENT)


def class_kind(kind: str, title: str) -> str | None:
    """The type of a class: the source's own kind, else what the words of the title say, else
    `None` (unknown)."""
    if kind != "other":
        return kind
    found = {_TITLE_KIND[w] for w in _title_words(title) if w in _TITLE_KIND}
    return next(iter(found)) if len(found) == 1 else None


def title_tokens(title: str) -> frozenset[str]:
    """The words that identify a class: casefolded, ё as е, punctuation and function words off,
    stemmed, the kind of the class (lecture, seminar) off. A title of nothing else keeps its words."""
    words = _title_words(title)
    content = [w for w in words if w not in _KIND_STEMS]
    return frozenset(content or words)


def title_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """0..1: the share of words in common; a title contained in the other (two words or more) counts
    as `CONTAINMENT_SIMILARITY`, a single contained word as the bare minimum of a match."""
    common = len(a & b)
    if not common:
        return 0.0
    jaccard = common / len(a | b)
    if common == min(len(a), len(b)):
        floor = CONTAINMENT_SIMILARITY if common >= 2 else TITLE_MATCH
        return max(jaccard, floor)
    return jaccard


def time_distance(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> timedelta | None:
    """How far apart two intervals are when they are the same class, `None` when they are not."""
    start_gap, end_gap = abs(a_start - b_start), abs(a_end - b_end)
    if start_gap <= TIME_TOLERANCE and end_gap <= TIME_TOLERANCE:
        return start_gap + end_gap
    overlap = min(a_end, b_end) - max(a_start, b_start)
    if overlap <= timedelta(0):
        return None
    union = max(a_end, b_end) - min(a_start, b_start)
    return start_gap + end_gap if overlap / union >= STRONG_OVERLAP else None


# --- comparing values ---------------------------------------------------------------------------


def _blank(value: str | None) -> bool:
    return not value or not value.strip()


def _words(value: str, drop: frozenset[str] = frozenset()) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(_norm(value)) if w not in drop)


def _same_room(a: str, b: str) -> bool | None:
    """`305`, `ауд. 305` and `Корпус Б, 305` are one room. `None`: nothing to compare."""
    left, right = _words(a, _ROOM_NOISE), _words(b, _ROOM_NOISE)
    if not left or not right:
        return None
    return left <= right or right <= left


def _same_people(a: str, b: str) -> bool | None:
    """Teachers agree when they share a name: «Иванов И. И.» and «Иван Иванов» do; initials alone
    and degrees say nothing. A list of teachers agrees with any of them."""
    left = {_stem(w) for w in _words(a, _HONORIFICS) if len(w) >= 3}
    right = {_stem(w) for w in _words(b, _HONORIFICS) if len(w) >= 3}
    if not left or not right:
        return None
    return bool(left & right)


def _same_text(a: str, b: str) -> bool | None:
    """Addresses and groups are written in many ways (б-р, бульвар): half of the shorter's words
    in the other is the same place or group. Numbers (a house, a group's year and number) must not
    contradict: «Б-21» and «Б-22» are two groups."""
    left, right = _words(a), _words(b)
    if not left or not right:
        return None
    numbers = (
        {w for w in left if any(c.isdigit() for c in w)},
        {w for w in right if any(c.isdigit() for c in w)},
    )
    if numbers[0] and numbers[1] and not (numbers[0] <= numbers[1] or numbers[1] <= numbers[0]):
        return False
    return len(left & right) / min(len(left), len(right)) >= 0.5


def _label_room(value: str) -> str:
    text = " ".join(value.split())
    return (
        text
        if not re.search(r"\d", text) or re.match(r"(?i)(ауд|каб|room)", text)
        else f"ауд. {text}"
    )


def _minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    return f"{moment.astimezone(zone):%H:%M}"


def _short_title(title: str) -> str:
    text = " ".join(title.split())
    return text if len(text) <= 80 else text[:79].rstrip() + "…"


def differences(
    base: ScheduleEventOut, other: ScheduleEventOut, zone: ZoneInfo
) -> list[tuple[Kind, str]]:
    """What the two say differently about the same class, in the words of the other one. Only a
    value both have is compared: an empty one is filled, not a disagreement. A time differs when the
    start does, or the end by more than `END_NOISE`; a title, when the other has words of its own."""
    out: list[tuple[Kind, str]] = []
    if _minute(base.starts_at) != _minute(other.starts_at) or (
        abs(base.ends_at - other.ends_at) > END_NOISE
    ):
        out.append(("time", f"время {_clock(other.starts_at, zone)}–{_clock(other.ends_at, zone)}"))
    if title_tokens(other.title) - title_tokens(base.title):
        out.append(("title", f"название «{_short_title(other.title)}»"))
    checks: tuple[
        tuple[Kind, str | None, str | None, Callable[[str, str], bool | None], str], ...
    ] = (
        ("location", base.location, other.location, _same_room, "{}"),
        ("teacher", base.teacher, other.teacher, _same_people, "преподаватель {}"),
        ("group", base.group, other.group, _same_text, "группа {}"),
        ("address", base.address, other.address, _same_text, "адрес {}"),
    )
    for kind, mine, theirs, same, template in checks:
        left, right = (mine or "").strip(), (theirs or "").strip()
        if not left or not right:
            continue
        if same(left, right) is False:
            value = _label_room(right) if kind == "location" else " ".join(right.split())
            out.append((kind, template.format(value)))
    return out


# --- the merge ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    """The stretches of time the primary source has regular classes for: a class outside them is not
    "missing" there. A far exam or a reminder is not regular and does not stretch it."""

    spans: tuple[tuple[datetime, datetime], ...]

    @classmethod
    def span(cls, first: datetime, last: datetime) -> Coverage:
        return cls(((first, last),))

    @classmethod
    def of_classes(cls, classes: Iterable[tuple[datetime, datetime]]) -> Coverage | None:
        """Regular classes (start, end) joined into stretches: a gap over `COVERAGE_GAP` splits."""
        spans: list[tuple[datetime, datetime]] = []
        for start, end in sorted(classes):
            if spans and start - spans[-1][1] <= COVERAGE_GAP:
                spans[-1] = (spans[-1][0], max(spans[-1][1], end))
            else:
                spans.append((start, end))
        return cls(tuple(spans)) if spans else None

    def reaches(self, start: datetime, end: datetime) -> bool:
        return any(first <= start and end <= last for first, last in self.spans)


def is_regular(kind: str, title: str, start: datetime, end: datetime) -> bool:
    """A class that says something about how far a source's timetable goes: not an exam, a
    consultation or anything with an assessment word, and not longer than a class can be."""
    return (
        kind not in IRREGULAR_KINDS
        and not assessment_words(title)
        and end - start <= MAX_MATCH_DURATION
    )


@dataclass(frozen=True, slots=True)
class SpacePlan:
    """How one university's events are merged: its schedule sources (the primary first), their
    titles, its time zone and how far the primary's data reaches."""

    order: tuple[str, ...]
    titles: Mapping[str, str]
    zone: ZoneInfo
    coverage: Coverage | None


@dataclass(slots=True)
class _Entry:
    event: ScheduleEventOut
    tokens: frozenset[str]
    start: datetime
    end: datetime  # never before the start: an event with no length is a minute long
    rank: int  # the position of the source the class came from; 0 is the primary
    matchable: bool
    kind: str | None  # what the class is, if the source or its title says
    assessment: frozenset[str]
    notes: list[tuple[str, Kind, str]] = field(default_factory=list)  # (source title, kind, text)
    missing: bool = False

    @classmethod
    def of(cls, event: ScheduleEventOut, rank: int) -> _Entry:
        end = max(event.ends_at, event.starts_at + ZERO_LENGTH)
        return cls(
            event=event,
            tokens=title_tokens(event.title),
            start=event.starts_at,
            end=end,
            rank=rank,
            matchable=end - event.starts_at <= MAX_MATCH_DURATION,
            kind=class_kind(event.kind, event.title),
            assessment=assessment_words(event.title),
        )


def _same_class(a: _Entry, b: _Entry) -> bool:
    """False for two events that cannot be one class whatever their time and words: both kinds are
    known and differ, or an assessment word is in one title only."""
    if a.kind is not None and b.kind is not None and a.kind != b.kind:
        return False
    return a.assessment == b.assessment


def _sorted(entries: list[_Entry]) -> list[_Entry]:
    return sorted(entries, key=lambda e: (e.start, e.end, e.event.id))


def _assign(incoming: Sequence[_Entry], pool: Sequence[_Entry]) -> dict[int, int]:
    """One to one: index in `incoming` -> index in `pool` (sorted by start). The best title wins,
    then the closest time, then the ids, so the same rows always merge the same way."""
    starts = [e.start for e in pool]
    candidates: list[tuple[float, timedelta, str, str, int, int]] = []
    for i, item in enumerate(incoming):
        if not item.matchable:
            continue
        for j in range(bisect_left(starts, item.start - MAX_MATCH_DURATION), len(pool)):
            other = pool[j]
            if other.start >= item.end:
                break
            if not other.matchable or other.end <= item.start or not _same_class(item, other):
                continue
            gap = time_distance(item.start, item.end, other.start, other.end)
            if gap is None:
                continue
            similarity = title_similarity(item.tokens, other.tokens)
            if similarity >= TITLE_MATCH:
                candidates.append((-similarity, gap, other.event.id, item.event.id, i, j))
    candidates.sort()
    taken: set[int] = set()
    chosen: dict[int, int] = {}
    for *_, i, j in candidates:
        if i not in chosen and j not in taken:
            chosen[i] = j
            taken.add(j)
    return chosen


def _absorb(entry: _Entry, other: ScheduleEventOut, source: EventSource, zone: ZoneInfo) -> None:
    """Fills the empty fields of `entry` from `other` and records what they disagree on. The
    address of the class (`url`), its links and its format come only from a source that says the
    same kind of class; a link of the other source is shown under the name of that source."""
    base = entry.event
    update: dict[str, Any] = {}
    kinds_agree = base.kind == other.kind or "other" in (base.kind, other.kind)
    fillable = ["location", "teacher", "address", "group", "kind_text", "course_title"]
    if kinds_agree:
        fillable.append("url")
    for name in fillable:
        if _blank(getattr(base, name)) and not _blank(getattr(other, name)):
            update[name] = getattr(other, name)
    if base.lesson_number is None and other.lesson_number is not None:
        update["lesson_number"] = other.lesson_number
    if kinds_agree and base.format == "unknown" and other.format != "unknown":
        update["format"] = other.format
    if base.kind == "other" and _blank(base.kind_text) and other.kind != "other":
        update["kind"] = other.kind  # «other» is the default of a source that does not say
    links = list(base.links)
    known = {link.url for link in links}
    extra: list[EventLink] = []
    if kinds_agree:
        extra = [
            EventLink(url=link.url, label=f"{source.title}: {link.label}"[:80])
            for link in other.links
        ]
        if (
            not _blank(other.url)
            and other.url not in known
            and other.url != update.get("url", base.url)
        ):
            # a different address of the same class is one more way in, not a loss
            extra.append(EventLink(url=str(other.url), label=source.title[:80]))
    for link in extra:
        if link.url not in known and len(links) < MAX_LINKS:
            links.append(link)
            known.add(link.url)
    if links != base.links:
        update["links"] = links
    attributes = list(base.attributes)
    labels = {a.label.casefold() for a in attributes}
    values = {a.value.casefold() for a in attributes}
    for attribute in other.attributes:
        if (
            attribute.label.casefold() not in labels
            and attribute.value.casefold() not in values
            and len(attributes) < MAX_ATTRIBUTES
        ):
            attributes.append(EventAttribute(label=attribute.label, value=attribute.value))
            labels.add(attribute.label.casefold())
            values.add(attribute.value.casefold())
    if attributes != base.attributes:
        update["attributes"] = attributes
    if other.changed and not base.changed:
        update["changed"] = True
    update["sources"] = [*base.sources, source]
    for kind, text in differences(base, other, zone):
        entry.notes.append((source.title, kind, text))
    entry.event = base.model_copy(update=update)


def _short(text: str) -> str:
    """The detail of a disagreement is a short line (the contract allows `MAX_DETAIL` characters)."""
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1].rstrip() + "…"


def _disagreement(entry: _Entry, plan: SpacePlan) -> EventDisagreement | None:
    if entry.missing:
        title = plan.titles.get(plan.order[0], "")
        return EventDisagreement(
            kind="not_in_primary",
            detail=_short(
                f"Нет в основном источнике «{title}»: возможно, пару перенесли или отменили"
            ),
        )
    if not entry.notes:
        return None
    kinds: set[Kind] = {kind for _, kind, _ in entry.notes}
    clauses: dict[str, list[str]] = {}
    for title, _, text in entry.notes:
        clauses.setdefault(title, []).append(text)
    detail = "; ".join(f"«{title}» — {', '.join(texts)}" for title, texts in clauses.items())
    only: Kind | None = next(iter(kinds)) if len(kinds) == 1 else None
    return EventDisagreement(kind=only or "several", detail=_short(detail))


def _mark_missing(entries: Sequence[_Entry], primary: Sequence[_Entry], plan: SpacePlan) -> None:
    """A class of another source that the primary does not have at this slot, though it has the
    same class a few days earlier or later, is probably moved or cancelled there. Only where the
    primary's regular classes reach, and only a class of the same kind (a consultation is not a
    missing lecture)."""
    coverage = plan.coverage
    if coverage is None or not primary:
        return
    starts = [e.start for e in primary]
    for entry in entries:
        if entry.rank == 0 or not entry.matchable or not coverage.reaches(entry.start, entry.end):
            continue
        elsewhere = False
        here = False
        for j in range(bisect_left(starts, entry.start - NEARBY), len(primary)):
            other = primary[j]
            if other.start > entry.start + NEARBY:
                break
            if not _same_class(entry, other):
                continue
            if title_similarity(entry.tokens, other.tokens) < SIMILAR_ELSEWHERE:
                continue
            if time_distance(entry.start, entry.end, other.start, other.end) is not None:
                here = True
                break
            elsewhere = True
        entry.missing = elsewhere and not here


def merge_space(events: Sequence[ScheduleEventOut], plan: SpacePlan) -> list[ScheduleEventOut]:
    """The events of one university from all its sources as one list (sorted by start). A source
    the plan does not list comes after the listed ones, in the order of its id."""
    rank = {connection_id: i for i, connection_id in enumerate(plan.order)}
    unknown = sorted({e.provenance.connection_id for e in events} - set(rank))
    rank.update({connection_id: len(plan.order) + i for i, connection_id in enumerate(unknown)})
    by_source: dict[int, list[_Entry]] = {}
    for event in events:
        position = rank[event.provenance.connection_id]
        by_source.setdefault(position, []).append(_Entry.of(event, position))
    pool = _sorted(by_source.pop(0, []))
    primary = list(pool)
    for position in sorted(by_source):
        incoming = _sorted(by_source[position])
        connection_id = incoming[0].event.provenance.connection_id
        source = EventSource(
            connection_id=connection_id, title=plan.titles.get(connection_id, "Источник")
        )
        matched = _assign(incoming, pool)
        for i, item in enumerate(incoming):
            if i in matched:
                _absorb(pool[matched[i]], item.event, source, plan.zone)
            else:
                pool.append(item)
        pool = _sorted(pool)
    _mark_missing(pool, primary, plan)
    return [
        entry.event.model_copy(update={"disagreement": disagreement})
        if (disagreement := _disagreement(entry, plan))
        else entry.event
        for entry in pool
    ]


def merge_events(
    events: Sequence[ScheduleEventOut], plans: Mapping[str, SpacePlan]
) -> list[ScheduleEventOut]:
    """Merges each university that has a plan (more than one schedule source); the rest pass
    through untouched. `plans` is keyed by space id; the result is sorted by start."""
    if not plans:
        return list(events)
    kept: list[ScheduleEventOut] = []
    by_space: dict[str, list[ScheduleEventOut]] = {}
    for event in events:
        space_id = event.provenance.space_id
        if space_id in plans:
            by_space.setdefault(space_id, []).append(event)
        else:
            kept.append(event)
    for space_id, rows in by_space.items():
        kept.extend(merge_space(rows, plans[space_id]))
    return sorted(kept, key=lambda e: (e.starts_at, e.ends_at, e.id))


# --- comparing two sources (the agent's compare_sources) ----------------------------------------


@dataclass(frozen=True, slots=True)
class Pair:
    a: ScheduleEventOut
    b: ScheduleEventOut
    differences: tuple[tuple[Kind, str], ...]  # in the words of `b`


@dataclass(frozen=True, slots=True)
class Comparison:
    pairs: list[Pair]
    only_a: list[ScheduleEventOut]
    only_b: list[ScheduleEventOut]


def compare(
    a_events: Sequence[ScheduleEventOut], b_events: Sequence[ScheduleEventOut], zone: ZoneInfo
) -> Comparison:
    """The same matching as the merge, laid out: pairs, and what only one of the sources has."""
    left = _sorted([_Entry.of(e, 0) for e in a_events])
    right = _sorted([_Entry.of(e, 1) for e in b_events])
    matched = _assign(right, left)
    pairs = [
        Pair(left[j].event, right[i].event, tuple(differences(left[j].event, right[i].event, zone)))
        for i, j in sorted(
            matched.items(), key=lambda ij: (left[ij[1]].start, left[ij[1]].event.id)
        )
    ]
    used = set(matched.values())
    return Comparison(
        pairs=pairs,
        only_a=[e.event for j, e in enumerate(left) if j not in used],
        only_b=[e.event for i, e in enumerate(right) if i not in matched],
    )
