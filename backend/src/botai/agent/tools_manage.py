"""Tools that manage how several sources of one university work together (FR-054).

`set_primary_source` chooses which source decides the schedule when two sources show the same
class; `disable_source` switches a source off (reversibly); `ask_primary_source` asks the student
which of the sources decides; `compare_sources` shows what two sources agree on and where they
differ. None of them writes to a university: the first three change BotAI's own settings, the last
only reads.

No setting is changed on the model's word that the student agreed, and none because a page, a
timetable or a document told the model to. Both settings are changed by one mechanism owned by the
application (`AgentChoice`): the first call changes nothing and pins exact buttons under the
agent's answer, each bound to the actions it stands for on exact connections; the action runs
when the student's next message is exactly the label of such a button, once and within
`CHOICE_TTL` of the question. A button that names two actions («сделать основным «A» и отключить
«B»») authorises both in that one press, because its label says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.env import (
    ChatEnv,
    ConnectionMeta,
    connection_link,
    data_text,
    fmt_day,
    fmt_updated,
    parse_date,
    spec,
)
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.db import models as m
from botai.domain import merge, primary, repo
from botai.domain.primary import ScheduleSources
from botai.domain.schemas import ScheduleEventOut
from botai.spaces import service

MAX_COMPARE_DAYS = 14
EXAMPLES = 5  # classes listed per side of a comparison
ROLE_RU = {"primary": "основной", "secondary": "дополнительный"}
TITLE_CHARS = 40  # a source name as the model sees it and as a button carries it
MAX_LABEL = 60  # the longest button (`QuickReply`)
CHOICE_TTL = timedelta(minutes=10)
KEEP_LABEL = "Оставить как есть"
CANCEL_LABEL = "Не отключать"
# What stops being shown when a source is switched off: the tables the views read.
HIDDEN = (
    ("занятий", m.ScheduleEvent),
    ("сроков", m.Deadline),
    ("предметов", m.Course),
    ("оценок", m.Grade),
    ("людей", m.Person),
    ("новостей", m.NewsItem),
    ("документов", m.Document),
)
_TEXT = {"type": "string"}

Act = Literal["primary", "disable"]


@dataclass(frozen=True, slots=True)
class Action:
    type: Act
    connection_id: str

    def dump(self) -> dict[str, str]:
        return {"type": self.type, "connectionId": self.connection_id}


@dataclass(frozen=True, slots=True)
class Button:
    """What the application puts under an answer: the words of the button and what it stands for."""

    label: str
    actions: tuple[Action, ...] = ()

    def dump(self) -> dict[str, Any]:
        return {"label": self.label, "actions": [a.dump() for a in self.actions]}


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold().rstrip(".!")


def _same(a: str, b: str) -> bool:
    return _fold(a) == _fold(b)


async def _schedule_sources(
    session: AsyncSession, user_id: str, space_id: str
) -> ScheduleSources | None:
    """The schedule sources of one university as the schedule reads them now."""
    return (await primary.live_schedule_sources(session, user_id, space_ids=[space_id])).get(
        space_id
    )


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def _title(conn: ConnectionMeta) -> str:
    """The name of a source as every tool result and every button shows it: one line, cleaned of
    look-alikes of our own text, at most `TITLE_CHARS` (a long name ends in «…»). A button made of
    it is the same words the model reads."""
    return _cut(data_text(conn.title, 200), TITLE_CHARS)


def _disable_label(conn: ConnectionMeta) -> str:
    return f"Отключить «{_title(conn)}»"


def _primary_label(conn: ConnectionMeta) -> str:
    return f"Сделать основным «{_title(conn)}»"


def _switch_label(new: ConnectionMeta, old: ConnectionMeta) -> str:
    """«Сделать основным «A» и отключить «B»»: both names, whole when they fit the 60 characters of
    a button, else the longer is cut first, so the button still names both sources."""
    fixed = len("Сделать основным «» и отключить «»")
    room = MAX_LABEL - fixed
    first, second = _title(new), _title(old)
    if len(first) + len(second) > room:
        short = min(len(first), len(second), room // 2)
        first = _cut(first, short if len(first) <= len(second) else room - short)
        second = _cut(second, short if len(second) < len(first) else room - short)
        if len(first) + len(second) > room:  # equal lengths: split the room evenly
            first, second = _cut(first, room // 2), _cut(second, room - room // 2)
    return f"Сделать основным «{first}» и отключить «{second}»"


# --- the buttons the application pins ----------------------------------------------------------


async def _pin(env: ChatEnv, buttons: list[Button]) -> bool:
    """Stores the question (replacing the thread's pending one) and writes the buttons under the
    agent's answer. Returns whether it replaced a question that was still open."""
    now = env.services.clock.now()
    async with env.services.db() as session:
        await session.execute(
            delete(m.AgentChoice).where(
                m.AgentChoice.user_id == env.user_id, m.AgentChoice.expires_at <= now
            )
        )
        replaced = (
            await session.scalar(
                select(m.AgentChoice.id).where(m.AgentChoice.thread_id == env.thread_id)
            )
            is not None
        )
        await session.execute(delete(m.AgentChoice).where(m.AgentChoice.thread_id == env.thread_id))
        session.add(
            m.AgentChoice(
                id=new_id(),
                user_id=env.user_id,
                thread_id=env.thread_id,
                buttons=[b.dump() for b in buttons],
                armed=None,
                expires_at=now + CHOICE_TTL,
                created_at=now,
            )
        )
        await session.commit()
    env.quick_replies = [b.label for b in buttons]
    env.pinned_replies = True
    return replaced


async def _question_time(session: AsyncSession, env: ChatEnv) -> datetime | None:
    """When the student wrote the message this turn answers (not the newest of the thread: a reply
    to a task may land while the turn runs)."""
    if env.question_id is None:
        return None
    return await session.scalar(
        select(m.ChatMessage.created_at).where(
            m.ChatMessage.id == env.question_id,
            m.ChatMessage.user_id == env.user_id,
            m.ChatMessage.thread_id == env.thread_id,
        )
    )


async def _authorize(
    env: ChatEnv, action: Act, connection_id: str
) -> Literal["ok", "declined", "none"]:
    """Does the student's message of this turn press a button of the pending question that stands
    for `action` on `connection_id`?

    `ok`: it does, and that action is now spent. `declined`: it presses a button that stands for
    nothing («Оставить как есть», «Не отключать»); the question is closed. `none`: it presses nothing
    that applies: not a button, not one of this question, a question that is stale or newer than the
    message, or another action. The first call that sees a press arms the question for that
    message; the actions of the button are then spent one by one, and only in that turn."""
    now = env.services.clock.now()
    hit = Action(action, connection_id).dump()
    async with env.services.db() as session:
        # Questions nobody answered in time are gone, not merely ignored.
        await session.execute(
            delete(m.AgentChoice).where(
                m.AgentChoice.user_id == env.user_id, m.AgentChoice.expires_at <= now
            )
        )
        choice = await session.scalar(
            select(m.AgentChoice)
            .where(m.AgentChoice.user_id == env.user_id, m.AgentChoice.thread_id == env.thread_id)
            .with_for_update()
        )
        asked_at = await _question_time(session, env)
        outcome: Literal["ok", "declined", "none"] = "none"
        if choice is not None and asked_at is not None and choice.created_at < asked_at:
            armed: dict[str, Any] | None = choice.armed
            if armed is not None and armed["messageId"] != env.question_id:
                await session.delete(choice)  # pressed in an earlier turn: whatever is left is void
            else:
                if armed is None:
                    pressed = next(
                        (b for b in choice.buttons if _same(b["label"], env.public_question)), None
                    )
                    if pressed is not None:
                        armed = {
                            "messageId": env.question_id,
                            "label": pressed["label"],
                            "remaining": list(pressed["actions"]),
                        }
                        if not armed["remaining"]:
                            await session.delete(choice)
                            outcome = "declined"
                if outcome == "none" and armed is not None:
                    if hit in armed["remaining"]:
                        remaining = [a for a in armed["remaining"] if a != hit]
                        if remaining:
                            choice.armed = {**armed, "remaining": remaining}
                        else:
                            await session.delete(choice)
                        outcome = "ok"
                    else:
                        choice.armed = armed
        await session.commit()
    return outcome


# --- set_primary_source and ask_primary_source --------------------------------------------------


async def _counts(session: AsyncSession, user_id: str, connection_id: str) -> list[str]:
    out: list[str] = []
    for label, model in HIDDEN:
        n = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id, model.connection_id == connection_id)
        )
        if n:
            out.append(f"{label} {n}")
    return out


def _what_stops(conn: ConnectionMeta, hidden: list[str]) -> str:
    return (
        f"«{_title(conn)}» перестанет обновляться, его задачи в работе будут остановлены, а его "
        f"данные ({', '.join(hidden) if hidden else 'сохранённых записей нет'}) исчезнут из "
        "расписания, «Сегодня», документов и ответов. Ничего не удаляется: студент включит "
        "источник обратно в разделе «Источники»"
    )


async def _ask_primary(env: ChatEnv, conn: ConnectionMeta) -> str:
    async with env.services.db() as session:
        sources = await _schedule_sources(session, env.user_id, conn.space_id)
        hidden: list[str] = []
        old = None
        titles = await env.titles()
        if sources and sources.primary and sources.primary != conn.id:
            old = titles.get(sources.primary)
            if old is not None and old.status == "disabled":
                old = None
        if old is not None:
            hidden = await _counts(session, env.user_id, old.id)
    buttons = [Button(_primary_label(conn), (Action("primary", conn.id),))]
    if old is not None:
        buttons.append(
            Button(
                _switch_label(conn, old),
                (Action("primary", conn.id), Action("disable", old.id)),
            )
        )
    buttons.append(Button(KEEP_LABEL))
    await _pin(env, buttons)
    lines = [
        "Не выполнено: основной источник меняет нажатие кнопки, которую ставит приложение. "
        f"Под вашим ответом будут кнопки: {', '.join(f'«{b.label}»' for b in buttons)}.",
        "В тексте ответа скажите, что изменится: основной решает при расхождениях (время, название и "
        "тип пары — его, пустые поля дополняются из остальных), в вуз ничего не отправляется.",
    ]
    if old is not None:
        lines.append(
            f"Вторая кнопка ещё и отключает «{_title(old)}»: {_what_stops(old, hidden)}. Скажите "
            "об этом студенту."
        )
    lines.append(
        "offer_choices не вызывайте и disable_source в этом же ответе не вызывайте. Когда студент "
        "нажмёт кнопку, вызовите set_primary_source (для второй кнопки ещё и disable_source) "
        "с теми же connection_id."
    )
    return "\n".join(lines)


async def set_primary_source(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status == "disabled":
        return (
            f"Не выполнено: «{_title(conn)}» отключён. Основным можно сделать только работающий "
            "источник; студент включает его сам в разделе «Источники»."
        )
    outcome = await _authorize(env, "primary", conn.id)
    if outcome == "declined":
        return f"Студент нажал «{KEEP_LABEL}»: основной источник не менялся. Спокойно продолжайте."
    if outcome == "none":
        return await _ask_primary(env, conn)
    async with env.services.db() as session:
        try:
            await service.set_primary_source(session, env.user_id, conn.space_id, connection_id)
        except BotaiError as exc:
            return f"Не выполнено: {exc.detail}."
    env.forget_meta()
    async with env.services.db() as session:
        sources = await _schedule_sources(session, env.user_id, conn.space_id)
        has_schedule = await session.scalar(
            select(func.count())
            .select_from(m.ScheduleEvent)
            .where(m.ScheduleEvent.user_id == env.user_id, m.ScheduleEvent.connection_id == conn.id)
        )
    titles = {c.id: _title(c) for c in (await env.titles()).values()}
    others = [titles[i] for i in (sources.order if sources else ()) if i != connection_id]
    lines = [
        f"Готово: основным источником расписания вуза «{data_text(conn.space_title)}» теперь "
        f"«{_title(conn)}». Это настройка Ботай, в вуз ничего не отправлено, данные не менялись."
    ]
    if not has_schedule:
        lines.append(
            "Данных расписания у этого источника пока нет: выбор записан и начнёт действовать, "
            "как только они появятся."
        )
    if others:
        lines.append(
            "Дополнительные: "
            + ", ".join(f"«{t}»" for t in others)
            + ". Совпадающие по времени и названию пары показываются одной: время, название и "
            "тип берутся у основного, пустые поля (аудитория, адрес, преподаватель, ссылки) "
            "дополняются из остальных, а расхождения помечаются в карточке пары."
        )
    return "\n".join(lines)


async def ask_primary_source(env: ChatEnv, args: dict[str, Any]) -> str:
    """The question «which of the sources decides the schedule?» as one click: a button per source."""
    titles = await env.titles()
    anchor = titles.get(str(args.get("connection_id", "")).strip())
    async with env.services.db() as session:
        by_space = await primary.live_schedule_sources(
            session, env.user_id, space_ids=[anchor.space_id] if anchor else None
        )
    scope = {c.space_id for c in (await env.meta())[0]}
    found = next(
        (
            s
            for s in by_space.values()
            if len(s.order) > 1 and (anchor is not None or s.space_id in scope)
        ),
        None,
    )
    if found is None:
        return (
            "Спрашивать не о чем: у вуза не больше одного источника с расписанием. "
            "Если студент назвал источник, вызовите set_primary_source."
        )
    chosen = [titles[i] for i in found.order[:3] if i in titles]
    buttons = [Button(_primary_label(c), (Action("primary", c.id),)) for c in chosen]
    buttons.append(Button(KEEP_LABEL, (Action("primary", found.order[0]),)))
    await _pin(env, buttons)
    return (
        "Кнопки поставило приложение: "
        + ", ".join(f"«{b.label}»" for b in buttons)
        + ". Сначала ответьте студенту по существу, в конце одной фразой объясните, что пары "
        "склеиваются и при расхождениях решает основной источник, и спросите, какой считать "
        "основным. offer_choices не вызывайте. Когда студент нажмёт кнопку, вызовите "
        "set_primary_source для названного в ней источника (для «Оставить как есть» — для "
        f"«{_title(titles[found.order[0]])}», он основной сейчас): так выбор запишется и вопрос "
        "не повторится."
    )


# --- disable_source -----------------------------------------------------------------------------


async def disable_source(env: ChatEnv, args: dict[str, Any]) -> str:
    connection_id = str(args.get("connection_id", "")).strip()
    conn = (await env.titles()).get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status == "disabled":
        return f"«{_title(conn)}» уже отключён студентом. Ничего не изменено."
    outcome = await _authorize(env, "disable", conn.id)
    if outcome == "declined":
        return "Студент отказался отключать: ничего не изменено. Спокойно продолжайте."
    async with env.services.db() as session:
        hidden = await _counts(session, env.user_id, connection_id)
        sources = await _schedule_sources(session, env.user_id, conn.space_id)
    after = [i for i in (sources.order if sources else ()) if i != connection_id]
    titles = {c.id: _title(c) for c in (await env.titles()).values()}
    consequence = f"Что остановится: {_what_stops(conn, hidden)}."
    if sources and sources.primary == connection_id and after:
        consequence += f" Основным расписанием станет «{titles[after[0]]}»."
    if outcome == "none":
        replaced = await _pin(
            env,
            [Button(_disable_label(conn), (Action("disable", conn.id),)), Button(CANCEL_LABEL)],
        )
        swapped = (
            " Прежний вопрос заменён этим: за один раз спрашивается об одном источнике, второй "
            "спросите отдельно."
            if replaced
            else ""
        )
        return (
            "Не выполнено: отключение ждёт подтверждения студента. "
            f"{consequence}{swapped} Приложение само поставит под вашим ответом кнопки "
            f"«{_disable_label(conn)}» и «{CANCEL_LABEL}»: offer_choices не вызывайте. В тексте "
            "ответа назовите источник, скажите, что остановится, и спросите, отключать ли. "
            "Источник отключится, когда студент нажмёт первую кнопку; тогда вызовите "
            "disable_source ещё раз."
        )
    async with env.services.db() as session:
        await service.set_source_enabled(
            session,
            env.services.vault,
            env.services.clock,
            env.user_id,
            connection_id,
            enabled=False,
        )
    env.forget_meta()
    return (
        f"Готово: «{_title(conn)}» отключён. {consequence} Включить обратно: Учёба → Источники → "
        f"«{_title(conn)}» → «Включить источник»."
    )


# --- compare_sources ----------------------------------------------------------------------------


def _slot(event: ScheduleEventOut, zone: ZoneInfo) -> str:
    s, f = event.starts_at.astimezone(zone), event.ends_at.astimezone(zone)
    return f"{fmt_day(event.starts_at, zone)} {s:%H:%M}–{f:%H:%M} «{data_text(event.title, 80)}»"


def _only(events: list[ScheduleEventOut], zone: ZoneInfo) -> str:
    shown = "; ".join(_slot(e, zone) for e in events[:EXAMPLES])
    more = f"; …и ещё {len(events) - EXAMPLES}" if len(events) > EXAMPLES else ""
    return shown + more


def _difference_line(pair: merge.Pair, names: tuple[str, str], zone: ZoneInfo) -> str:
    a, b = pair.a, pair.b
    values = {
        "time": (
            f"{a.starts_at.astimezone(zone):%H:%M}–{a.ends_at.astimezone(zone):%H:%M}",
            f"{b.starts_at.astimezone(zone):%H:%M}–{b.ends_at.astimezone(zone):%H:%M}",
        ),
        "title": (a.title, b.title),
        "location": (a.location, b.location),
        "teacher": (a.teacher, b.teacher),
        "group": (a.group, b.group),
        "address": (a.address, b.address),
    }
    label = {
        "time": "время",
        "title": "название",
        "location": "аудитория",
        "teacher": "преподаватель",
        "group": "группа",
        "address": "адрес",
    }
    parts = [
        f"{label[kind]}: «{names[0]}» {data_text(str(values[kind][0]), 60)} / "
        f"«{names[1]}» {data_text(str(values[kind][1]), 60)}"
        for kind, _ in pair.differences
    ]
    return f"- {_slot(a, zone)}: " + "; ".join(parts)


def _fills(comparison: merge.Comparison) -> dict[str, tuple[int, int]]:
    """Per field: in how many matched pairs only the first / only the second source has a value."""
    out: dict[str, tuple[int, int]] = {}
    for label, field in (
        ("аудитория", "location"),
        ("адрес", "address"),
        ("преподаватель", "teacher"),
        ("группа", "group"),
    ):
        only_first = sum(
            1 for p in comparison.pairs if getattr(p.a, field) and not getattr(p.b, field)
        )
        only_second = sum(
            1 for p in comparison.pairs if not getattr(p.a, field) and getattr(p.b, field)
        )
        if only_first or only_second:
            out[label] = (only_first, only_second)
    return out


async def compare_sources(env: ChatEnv, args: dict[str, Any]) -> str:
    metas = await env.titles()
    a, b = metas.get(str(args.get("a", "")).strip()), metas.get(str(args.get("b", "")).strip())
    if a is None or b is None or a.id == b.id:
        return "Нужны два разных подключения студента: возьмите connection_id из list_universities."
    if a.space_id != b.space_id:
        return "Сравнить можно источники одного вуза; эти относятся к разным вузам."
    for side in (a, b):
        if side.status == "disabled":
            return f"«{_title(side)}» отключён, его данные не показываются: сравнивать нечего."
    tz = await env.tz_for(a.space_id)
    today = env.services.clock.now().astimezone(tz).date()
    start_d = parse_date(args["from_date"]) if args.get("from_date") else today
    if start_d is None:
        return "Ошибка аргументов: даты нужны в формате YYYY-MM-DD."
    end_d = parse_date(args["to_date"]) if args.get("to_date") else start_d + timedelta(days=6)
    if end_d is None:
        return "Ошибка аргументов: даты нужны в формате YYYY-MM-DD."
    end_d = max(end_d, start_d)
    clipped = (end_d - start_d).days >= MAX_COMPARE_DAYS
    if clipped:
        end_d = start_d + timedelta(days=MAX_COMPARE_DAYS - 1)
    start = datetime.combine(start_d, time.min, tzinfo=tz)
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=tz)
    now = env.services.clock.now()
    async with env.services.db() as session:
        left = await repo.source_events(session, env.user_id, a.id, start=start, end=end, now=now)
        right = await repo.source_events(session, env.user_id, b.id, start=start, end=end, now=now)
        sources = await _schedule_sources(session, env.user_id, a.space_id)
    comparison = merge.compare(left, right, tz)
    keys = {
        x.id: env.sources.add(
            title=x.title,
            url=connection_link(x.id),
            kind="connection",
            fetched_at=x.last_sync_at,
        )
        for x in (a, b)
    }
    names = (data_text(a.title, 60), data_text(b.title, 60))

    def role(x: ConnectionMeta) -> str:
        found = sources.role_of(x.id) if sources else None
        return f", {ROLE_RU[found]}" if found else ", данных расписания нет"

    lines = [
        f"Сравнение расписаний за {start_d} — {end_d} ({(end_d - start_d).days + 1} дн.)"
        + (f", период сокращён до {MAX_COMPARE_DAYS} дней" if clipped else "")
        + f"; время в часовом поясе вуза. «{names[0]}» [{keys[a.id]}]{role(a)}; "
        f"«{names[1]}» [{keys[b.id]}]{role(b)}.",
        f"Занятий: в «{names[0]}» {len(left)}, в «{names[1]}» {len(right)}. "
        f"Совпало (время ±{int(merge.TIME_TOLERANCE.total_seconds() // 60)} мин и название): "
        f"{len(comparison.pairs)}.",
    ]
    if not left and not right:
        lines.append(
            f"В этот период в обоих источниках занятий нет. Обновлено: «{names[0]}» "
            f"{fmt_updated(a.last_sync_at, tz)}, «{names[1]}» {fmt_updated(b.last_sync_at, tz)}."
        )
        return "\n".join(lines)
    for name, ours in ((names[0], comparison.only_a), (names[1], comparison.only_b)):
        lines.append(
            f"Есть только в «{name}» ({len(ours)}): "
            + (_only(ours, tz) if ours else "таких занятий нет")
        )
    different = [p for p in comparison.pairs if p.differences]
    if different:
        lines.append(f"Совпавшие, но с расхождениями ({len(different)}):")
        lines.extend(_difference_line(p, names, tz) for p in different[:EXAMPLES])
        if len(different) > EXAMPLES:
            lines.append(f"…и ещё {len(different) - EXAMPLES}.")
    elif comparison.pairs:
        lines.append("У совпавших занятий расхождений нет.")
    fills = _fills(comparison)
    if fills:
        lines.append(
            "Поля, которые у совпавших занятий есть только у одного источника: "
            + "; ".join(
                f"{label} — "
                + ", ".join(
                    f"только у «{name}» {n}"
                    for name, n in ((names[0], first), (names[1], second))
                    if n
                )
                for label, (first, second) in fills.items()
            )
            + "."
        )
    return "\n".join(lines)


SPECS = {
    "set_primary_source": spec(
        "set_primary_source",
        "Сделать источник основным для расписания его вуза. Когда источники вуза (кабинет и "
        "календарь) показывают одну и ту же пару, основной решает: берутся его время, название и "
        "тип, пустые поля дополняются из остальных, расхождения помечаются. Это настройка Ботай: "
        "в вуз ничего не отправляется, данные не удаляются. Первый вызов ничего не меняет: "
        "приложение само поставит под вашим ответом кнопки («Сделать основным «название»», с "
        "отключением прежнего основного, «Оставить как есть»), а инструмент вернёт, что они "
        "значат. Источник станет основным, когда студент нажмёт кнопку и вы вызовете инструмент "
        "ещё раз. connection_id — из list_universities.",
        {"connection_id": _TEXT},
        ["connection_id"],
    ),
    "ask_primary_source": spec(
        "ask_primary_source",
        "Спросить студента, какой из источников расписания вуза основной: приложение ставит под "
        "вашим ответом по кнопке на источник и «Оставить как есть», нажатие — один щелчок. Когда у "
        "вуза несколько источников с расписанием и в контексте написано, что студент основной не "
        "выбирал. connection_id необязателен: любой источник нужного вуза.",
        {"connection_id": {**_TEXT, "description": "Любой источник вуза, если вуз не очевиден"}},
        [],
    ),
    "disable_source": spec(
        "disable_source",
        "Отключить источник обратимо: он перестаёт обновляться, его данные (занятия, сроки, "
        "предметы, оценки, документы) исчезают из видов и ответов, но не удаляются; студент включит "
        "его в «Источниках». Первый вызов ничего не отключает: приложение само поставит под вашим "
        "ответом кнопки «Отключить «название»» и «Не отключать», а инструмент вернёт, что "
        "остановится. Источник отключится, когда студент нажмёт первую кнопку и вы вызовете "
        "инструмент ещё раз. За один раз спрашивается об одном источнике.",
        {"connection_id": _TEXT},
        ["connection_id"],
    ),
    "compare_sources": spec(
        "compare_sources",
        "Сравнить расписание двух источников одного вуза за период (не больше 14 дней, по "
        "умолчанию неделя с сегодняшнего дня): сколько занятий совпало, что есть только в одном "
        "источнике, в чём совпавшие расходятся. Только чтение. a и b — connection_id.",
        {
            "a": {**_TEXT, "description": "connection_id первого источника"},
            "b": {**_TEXT, "description": "connection_id второго источника"},
            "from_date": {**_TEXT, "description": "Первый день YYYY-MM-DD, по умолчанию сегодня"},
            "to_date": {**_TEXT, "description": "Последний день включительно, не позже 14 дней"},
        },
        ["a", "b"],
    ),
}
STAGES = {
    "set_primary_source": "Выбираю основной источник",
    "ask_primary_source": "Готовлю вопрос об основном источнике",
    "disable_source": "Отключаю источник",
    "compare_sources": "Сравниваю источники",
}
IMPLS = {
    "set_primary_source": set_primary_source,
    "ask_primary_source": ask_primary_source,
    "disable_source": disable_source,
    "compare_sources": compare_sources,
}
