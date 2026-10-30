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

What a label may be is the application's business too: names are cleaned of quotes that could close
ours, no two labels of a question read alike once folded or cut (the web client shows one button
for two equal labels), each fits the 60 UTF-16 units of a button, and no question is asked under an
answer that already carries a task card (the web client shows buttons only under a last text block).
A press covers its whole turn: a call that comes after the press, to repeat or to ask anew, is told
what is left of the button and replaces nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
from botai.spaces import service, views

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


def _u16(text: str) -> int:
    """Length in UTF-16 units: what the web client counts against the 60 characters of a button
    (an emoji is two)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _cut(text: str, limit: int) -> str:
    """At most `limit` UTF-16 units, a cut ending in «…»."""
    if _u16(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > limit - 1:
            break
        out.append(ch)
        used += width
    return "".join(out).rstrip() + "…"


# The quotes of a source's name must not close ours: a document called `Календарь» и отключить
# «Кабинет` would otherwise make a button that says something else than it stands for.
_QUOTES = str.maketrans(dict.fromkeys("«„“‟〝＂", "‹") | dict.fromkeys('»”〞"', "›"))


def _title(conn: ConnectionMeta) -> str:
    """The name of a source as every tool result and every button shows it: one line, cleaned of
    look-alikes of our own text and of quotes, at most `TITLE_CHARS` UTF-16 units (a long name ends
    in «…»). A button made of it is the same words the model reads."""
    text = data_text(conn.title, 200).translate(_QUOTES)
    return _cut(text or views.KIND_TITLES.get(conn.kind, "Источник"), TITLE_CHARS)


def _tagged(text: str, tag: str) -> str:
    suffix = f" ({tag})"
    return _cut(text, TITLE_CHARS - _u16(suffix)) + suffix


def _names(conns: Sequence[ConnectionMeta]) -> dict[str, str]:
    """A name for each source such that no two buttons can look the same: sources whose names fold
    to one (a long name cut at the same place, names that differ in case, empty names that fall
    back to the title of the kind) get the kind of the source added, then a number."""
    names = {c.id: _title(c) for c in conns}

    def clashes() -> list[list[ConnectionMeta]]:
        groups: dict[str, list[ConnectionMeta]] = {}
        for c in conns:
            groups.setdefault(_fold(names[c.id]), []).append(c)
        return [g for g in groups.values() if len(g) > 1]

    for group in clashes():
        for c in group:
            names[c.id] = _tagged(names[c.id], views.KIND_TITLES.get(c.kind, "источник").lower())
    for group in clashes():
        for number, c in enumerate(group, 1):
            names[c.id] = _tagged(_title(c), str(number))
    return names


def _disable_label(name: str) -> str:
    return f"Отключить «{name}»"


def _primary_label(name: str) -> str:
    return f"Сделать основным «{name}»"


def _switch_label(new: ConnectionMeta, old: ConnectionMeta, names: dict[str, str]) -> str:
    """«Сделать основным «A» и отключить «B»»: both names, whole when they fit the 60 UTF-16 units
    of a button, else the longer is cut first, so the button still names both sources. Names that
    would look the same once cut give way to the kind of each source, then to «новый» and «старый»."""
    room = MAX_LABEL - _u16("Сделать основным «» и отключить «»")
    first, second = names[new.id], names[old.id]
    if _u16(first) + _u16(second) > room:
        half = room // 2
        keep_first = _u16(first) <= half
        keep_second = _u16(second) <= half
        if keep_first:
            second = _cut(second, room - _u16(first))
        elif keep_second:
            first = _cut(first, room - _u16(second))
        else:
            first, second = _cut(first, half), _cut(second, room - half)
        if _fold(first) == _fold(second):
            first = _cut(views.KIND_TITLES.get(new.kind, "новый"), half)
            second = _cut(views.KIND_TITLES.get(old.kind, "старый"), room - half)
        if _fold(first) == _fold(second):
            first, second = "новый", "старый"
    return f"Сделать основным «{first}» и отключить «{second}»"


# --- the buttons the application pins ----------------------------------------------------------

_BLOCKED = (
    "Не выполнено: в этом ответе уже есть карточка задачи, а под такой ответ приложение не может "
    "поставить кнопки. Расскажите студенту, что сделано, и задайте этот вопрос в следующем "
    "сообщении: вызовите инструмент ещё раз тогда."
)


def _distinct(buttons: list[Button]) -> list[Button]:
    """Buttons whose labels differ even after folding (the student sees one button for two equal
    labels, and the press could not tell them apart), each within the 60 units of a button."""
    out: list[Button] = []
    seen: set[str] = set()
    for button in buttons:
        label = _cut(button.label, MAX_LABEL)
        number = 1
        while _fold(label) in seen:
            number += 1
            tag = f" ({number})"
            label = _cut(button.label, MAX_LABEL - _u16(tag)) + tag
        seen.add(_fold(label))
        out.append(Button(label, button.actions))
    return out


async def _pin(env: ChatEnv, buttons: list[Button]) -> Literal["pinned", "replaced", "blocked"]:
    """Stores the question (replacing the thread's pending one) and writes the buttons under the
    agent's answer. `replaced`: another question of the thread was still open. `blocked`: the answer
    already carries a task card, and the web client shows buttons only under a last text block, so
    nothing is stored: the question is for the next message."""
    if env.blocks:
        return "blocked"
    buttons = _distinct(buttons)
    now = env.services.clock.now()
    mine = (m.AgentChoice.user_id == env.user_id, m.AgentChoice.thread_id == env.thread_id)
    async with env.services.db() as session:
        await session.execute(
            delete(m.AgentChoice).where(
                m.AgentChoice.user_id == env.user_id, m.AgentChoice.expires_at <= now
            )
        )
        replaced = await session.scalar(select(m.AgentChoice.id).where(*mine)) is not None
        await session.execute(delete(m.AgentChoice).where(*mine))
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
    return "replaced" if replaced else "pinned"


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


@dataclass(frozen=True, slots=True)
class Auth:
    """What the message of this turn authorises for one requested action.

    `ok`: it presses a button that stands for the action, and the action is now spent. `declined`: it
    presses a button that stands for nothing («Оставить как есть», «Не отключать»); the question is
    closed. `spent`: it pressed a button of the question in this very turn, and the action is not
    left on it (it was done already, or the button never stood for it): the question is kept for
    what is left. `none`: it presses nothing that applies: not a button, not one of this question,
    labels that cannot be told apart, a question that is stale or newer than the message."""

    outcome: Literal["ok", "declined", "spent", "none"]
    label: str = ""
    done: bool = False  # `spent`: the requested action was on the button and is already done
    remaining: tuple[dict[str, str], ...] = ()


async def _authorize(env: ChatEnv, action: Act | None, connection_id: str = "") -> Auth:
    """Does the student's message of this turn press a button of the pending question that stands
    for `action` on `connection_id`? (`None`: only whether it pressed one, spending nothing.)

    The first call that sees a press arms the question for that message; the actions of the button
    are then spent one by one, and only in that turn. The question stays in the table, with what is
    left of it, until the turn is over: a call that repeats a spent action, or asks anew, finds the
    press and is told so, instead of replacing the question and losing the rest of the button."""
    now = env.services.clock.now()
    hit = Action(action, connection_id).dump() if action is not None else None
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
        result = Auth("none")
        if choice is not None and asked_at is not None and choice.created_at < asked_at:
            armed: dict[str, Any] | None = choice.armed
            if armed is not None and armed["messageId"] != env.question_id:
                await session.delete(choice)  # pressed in an earlier turn: whatever is left is void
            else:
                fresh = False
                if armed is None:
                    matches = [b for b in choice.buttons if _same(b["label"], env.public_question)]
                    # Two buttons that read alike cannot be told apart by a press: nothing passes.
                    if len(matches) == 1:
                        actions = list(matches[0]["actions"])
                        armed = {
                            "messageId": env.question_id,
                            "label": matches[0]["label"],
                            "actions": actions,
                            "remaining": list(actions),
                        }
                        fresh = True
                if armed is not None:
                    remaining: list[dict[str, str]] = list(armed["remaining"])
                    if fresh and not remaining:
                        result = Auth("declined", armed["label"])
                    elif hit is not None and hit in remaining:
                        remaining.remove(hit)
                        result = Auth("ok", armed["label"])
                    else:
                        result = Auth(
                            "spent",
                            armed["label"],
                            done=hit is not None and hit in armed.get("actions", []),
                            remaining=tuple(remaining),
                        )
                    choice.armed = {**armed, "remaining": remaining}
        await session.commit()
    return result


def _spent_text(auth: Auth, titles: dict[str, ConnectionMeta], *, asking: bool = False) -> str:
    """Why nothing happened when the student's press of this turn has no such action left, and what
    is left on that button. The question stays; no new one is asked in the same turn."""

    def describe(action: dict[str, str]) -> str:
        meta = titles.get(action["connectionId"])
        name = _title(meta) if meta is not None else "источник"
        return f"{'сделать основным' if action['type'] == 'primary' else 'отключить'} «{name}»"

    left = (
        "По этой кнопке осталось: " + "; ".join(describe(a) for a in auth.remaining) + ". Вызовите "
        "для этого set_primary_source или disable_source."
        if auth.remaining
        else "По этой кнопке больше ничего не осталось."
    )
    if asking:
        why = "вопрос этим закрыт"
    elif auth.done:
        why = "это действие уже выполнено"
    else:
        why = "эта кнопка его не включает"
    return (
        f"Не выполнено: в этом ходе студент нажал «{auth.label}», и {why}. {left} Новый вопрос "
        "в этом же ответе не задавайте: если нужно другое, спросите в следующем сообщении."
    )


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
    titles = await env.titles()
    async with env.services.db() as session:
        sources = await _schedule_sources(session, env.user_id, conn.space_id)
        hidden: list[str] = []
        old = None
        if sources and sources.primary and sources.primary != conn.id:
            old = titles.get(sources.primary)
            if old is not None and old.status == "disabled":
                old = None
        if old is not None:
            hidden = await _counts(session, env.user_id, old.id)
    names = _names([conn, old] if old is not None else [conn])
    buttons = [Button(_primary_label(names[conn.id]), (Action("primary", conn.id),))]
    if old is not None:
        buttons.append(
            Button(
                _switch_label(conn, old, names),
                (Action("primary", conn.id), Action("disable", old.id)),
            )
        )
    buttons.append(Button(KEEP_LABEL))
    if await _pin(env, buttons) == "blocked":
        return _BLOCKED
    labels = env.quick_replies
    lines = [
        "Не выполнено: основной источник меняет нажатие кнопки, которую ставит приложение. "
        f"Под вашим ответом будут кнопки: {', '.join(f'«{label}»' for label in labels)}.",
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
    titles = await env.titles()
    conn = titles.get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status == "disabled":
        return (
            f"Не выполнено: «{_title(conn)}» отключён. Основным можно сделать только работающий "
            "источник; студент включает его сам в разделе «Источники»."
        )
    auth = await _authorize(env, "primary", conn.id)
    if auth.outcome == "declined":
        return f"Студент нажал «{auth.label}»: основной источник не менялся. Спокойно продолжайте."
    if auth.outcome == "spent":
        return _spent_text(auth, titles)
    if auth.outcome == "none":
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
    names = {c.id: _title(c) for c in (await env.titles()).values()}
    others = [names[i] for i in (sources.order if sources else ()) if i != connection_id]
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
    pressed = await _authorize(env, None)  # a button of this turn is not to be replaced
    if pressed.outcome != "none":
        return _spent_text(pressed, titles, asking=True)
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
    names = _names(chosen)
    buttons = [Button(_primary_label(names[c.id]), (Action("primary", c.id),)) for c in chosen]
    buttons.append(Button(KEEP_LABEL, (Action("primary", found.order[0]),)))
    if await _pin(env, buttons) == "blocked":
        return _BLOCKED
    return (
        "Кнопки поставило приложение: "
        + ", ".join(f"«{label}»" for label in env.quick_replies)
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
    metas = await env.titles()
    conn = metas.get(connection_id)
    if conn is None:
        return "Такого подключения у студента нет. Возьмите connection_id из list_universities."
    if conn.status == "disabled":
        return f"«{_title(conn)}» уже отключён студентом. Ничего не изменено."
    auth = await _authorize(env, "disable", conn.id)
    if auth.outcome == "declined":
        return "Студент отказался отключать: ничего не изменено. Спокойно продолжайте."
    if auth.outcome == "spent":
        return _spent_text(auth, metas)
    async with env.services.db() as session:
        hidden = await _counts(session, env.user_id, connection_id)
        sources = await _schedule_sources(session, env.user_id, conn.space_id)
    after = [i for i in (sources.order if sources else ()) if i != connection_id]
    titles = {c.id: _title(c) for c in metas.values()}
    consequence = f"Что остановится: {_what_stops(conn, hidden)}."
    if sources and sources.primary == connection_id and after:
        consequence += f" Основным расписанием станет «{titles[after[0]]}»."
    if auth.outcome == "none":
        siblings = _names([c for c in metas.values() if c.space_id == conn.space_id])
        pinned = await _pin(
            env,
            [
                Button(_disable_label(siblings[conn.id]), (Action("disable", conn.id),)),
                Button(CANCEL_LABEL),
            ],
        )
        if pinned == "blocked":
            return _BLOCKED
        swapped = (
            " Прежний вопрос заменён этим: за один раз спрашивается об одном источнике, второй "
            "спросите отдельно."
            if pinned == "replaced"
            else ""
        )
        return (
            "Не выполнено: отключение ждёт подтверждения студента. "
            f"{consequence}{swapped} Приложение само поставит под вашим ответом кнопки "
            f"«{env.quick_replies[0]}» и «{env.quick_replies[1]}»: offer_choices не вызывайте. В "
            "тексте ответа назовите источник, скажите, что остановится, и спросите, отключать ли. "
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
