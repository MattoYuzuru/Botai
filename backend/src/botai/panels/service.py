"""Panels: default ones for the data a university has, proposals of the agent, the student's switches.

Every function takes `user_id` and applies it as the tenant predicate; a foreign id is
`BotaiError(NOT_FOUND)`, never `forbidden`. Functions flush but never commit: the caller (the
router, the sync writer, an agent tool) owns the transaction.

Life of a panel (`status`): a system panel is born `active`; an agent's panel is born `proposed`
and invisible, becomes `active` when the student accepts it and `dismissed` when he refuses; a
dismissed key is not proposed again unless the caller forces it. The student hides a panel with
`visible` (everywhere) or `hidden_in_all` (only in the all-universities mode): hiding is not
dismissing.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import (
    Grade,
    Job,
    Panel,
    ProfileField,
    RankingEntry,
    Source,
    Space,
    University,
)
from botai.domain import sources
from botai.panels.schemas import (
    MAX_TITLE,
    ChartSpec,
    ChartView,
    Dataset,
    KpiSpec,
    ListSpec,
    PanelSpecModel,
    PanelStatus,
    ProfileSpec,
    RankingSpec,
    dump_spec,
    parse_spec,
)

GRADES_OVERVIEW = "grades-overview"
GRADES_CHART = "grades-chart"
GRADES_RECENT = "grades-recent"
STUDENT_PROFILE = "student-profile"
RANKING_PREFIX = "ranking:"
MAX_PANELS_PER_SPACE = 24
MAX_SYSTEM_BOARDS = 8  # rating boards that get a panel of their own; the rest stay data

_KEY = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,59}$")
_STATUS_MOVES: dict[str, frozenset[str]] = {
    "proposed": frozenset({"active", "dismissed"}),
    "active": frozenset({"dismissed"}),
    "dismissed": frozenset({"active"}),
}
ProposalOutcome = Literal["created", "updated", "exists", "dismissed"]


@dataclass(frozen=True, slots=True)
class Proposal:
    """What `propose_panel` did: `created`, `updated` (a still-proposed panel got a new spec),
    `exists` (already live, nothing changed) or `dismissed` (the student refused this key)."""

    panel: Panel
    outcome: ProposalOutcome


@dataclass(frozen=True, slots=True)
class SpaceData:
    """What a university has to show: the count of grades, the numeric ones, profile fields and
    the boards of the rating lists, among the rows of its live sources."""

    grades: int
    numeric: int
    dated: int
    profile: int
    boards: tuple[str, ...]


def valid_key(key: str) -> str:
    if not _KEY.match(key):
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Ключ панели: латиница, цифры, «-», «_», «:», начинается с буквы или цифры, до 60 знаков",
        )
    return key


def ranking_key(board: str) -> str:
    """A stable key of the system panel of a board: readable ASCII part plus a hash of the name,
    so two boards never share a key and the same board always gets the same one."""
    folded = " ".join(board.casefold().split())
    digest = hashlib.sha1(folded.encode(), usedforsecurity=False).hexdigest()
    ascii_part = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_part).strip("-")[:30].strip("-")
    return f"{RANKING_PREFIX}{slug}-{digest[:8]}" if slug else f"{RANKING_PREFIX}{digest[:12]}"


def _plain_title(text: str, fallback: str) -> str:
    """A board name as a panel title: the characters a title may not have become spaces."""
    cleaned = re.sub(r"[<>`]|://|\bwww\.|\]\(|\*\*|__|[\x00-\x1f\x7f]", " ", text)
    cleaned = " ".join(cleaned.split()).lstrip("#-*+ ")[:MAX_TITLE].strip()
    return cleaned or fallback


# --- reading --------------------------------------------------------------------------------


async def get_space(session: AsyncSession, user_id: str, space_id: str) -> Space:
    space = await session.scalar(
        select(Space).where(Space.id == space_id, Space.user_id == user_id)
    )
    if space is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Пространство не найдено")
    return space


async def space_ids(session: AsyncSession, user_id: str) -> list[str]:
    """The universities of the user (a legacy space without one is not shown), oldest first."""
    return list(
        await session.scalars(
            select(Space.id)
            .where(Space.user_id == user_id, Space.university_id.is_not(None))
            .order_by(Space.created_at, Space.id)
        )
    )


async def get_panel(session: AsyncSession, *, user_id: str, panel_id: str) -> Panel:
    panel = await session.scalar(
        select(Panel).where(Panel.id == panel_id, Panel.user_id == user_id)
    )
    if panel is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Панель не найдена")
    return panel


async def list_panels(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str | None = None,
    status: PanelStatus = "active",
) -> list[tuple[Panel, Space, University]]:
    """The panels of one space or of every space of the user, in display order: spaces by age,
    then the panel position. The university comes along for the short title."""
    if space_id is not None:
        await get_space(session, user_id, space_id)
    stmt = (
        select(Panel, Space, University)
        .join(Space, Space.id == Panel.space_id)
        .join(University, University.id == Space.university_id)
        .where(Panel.user_id == user_id, Space.user_id == user_id, Panel.status == status)
        .order_by(Space.created_at, Space.id, Panel.position, Panel.created_at, Panel.id)
    )
    if space_id is not None:
        stmt = stmt.where(Panel.space_id == space_id)
    return [(panel, space, uni) for panel, space, uni in (await session.execute(stmt)).all()]


async def space_data(session: AsyncSession, *, user_id: str, space_id: str) -> SpaceData:
    """What the live sources of a university hold for the three datasets (a few counts)."""
    live = await sources.load(session, user_id, space_id=space_id)
    if live.is_empty:
        return SpaceData(grades=0, numeric=0, dated=0, profile=0, boards=())
    grade_filter = (Grade.user_id == user_id, live.filter_of(Grade))
    numeric = (Grade.value_num.is_not(None), Grade.scale_max.is_not(None))
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(*numeric),
                func.count().filter(*numeric, Grade.graded_at.is_not(None)),
            ).where(*grade_filter)
        )
    ).one()
    profile = await session.scalar(
        select(func.count())
        .select_from(ProfileField)
        .where(ProfileField.user_id == user_id, live.filter_of(ProfileField))
    )
    boards = tuple(
        await session.scalars(
            select(RankingEntry.board)
            .where(RankingEntry.user_id == user_id, live.filter_of(RankingEntry))
            .group_by(RankingEntry.board)
            .order_by(RankingEntry.board)
        )
    )
    return SpaceData(
        grades=int(row[0]),
        numeric=int(row[1]),
        dated=int(row[2]),
        profile=int(profile or 0),
        boards=boards,
    )


# --- default panels -----------------------------------------------------------------------------


async def _next_position(session: AsyncSession, space_id: str) -> int:
    top = await session.scalar(select(func.max(Panel.position)).where(Panel.space_id == space_id))
    return 0 if top is None else int(top) + 1


async def _insert_absent(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str,
    key: str,
    spec: PanelSpecModel,
    position: int,
) -> bool:
    """Insert an active system panel unless the key is taken; True when this call made it."""
    made = await session.scalar(
        pg_insert(Panel)
        .values(
            id=new_id(),
            user_id=user_id,
            space_id=space_id,
            key=key,
            type=spec.type,
            spec=dump_spec(spec),
            status="active",
            visible=True,
            hidden_in_all=False,
            position=position,
            origin="system",
        )
        .on_conflict_do_nothing(index_elements=["space_id", "key"])
        .returning(Panel.id)
    )
    return made is not None


async def ensure_default_panels(session: AsyncSession, *, user_id: str, space_id: str) -> list[str]:
    """Create, once, the system panels a university's data deserves; returns the keys made now.

    Idempotent by the unique `(space_id, key)`: a panel that exists in any status or visibility
    is left exactly as it is, so the student's switches and refusals are never undone. Grades give
    an overview, (when some of them have numbers) a chart and a list of the latest ones, the card
    fields give the profile, every rating board gives its own ranking panel."""
    await get_space(session, user_id, space_id)
    data = await space_data(session, user_id=user_id, space_id=space_id)
    wanted: list[tuple[str, PanelSpecModel]] = []

    def want(key: str, make: Callable[[], PanelSpecModel]) -> None:
        """A default that cannot be built from odd data is skipped: a panel must never fail the
        sync that is writing the data."""
        try:
            wanted.append((key, make()))
        except ValidationError:
            return

    if data.grades:
        want(
            GRADES_OVERVIEW,
            lambda: KpiSpec(
                type="kpi", title="Успеваемость", icon="grade", dataset=Dataset(kind="grades")
            ),
        )
    if data.numeric:
        by_date = data.dated > 0
        want(
            GRADES_CHART,
            lambda: ChartSpec(
                type="chart",
                title="Оценки",
                icon="chart",
                dataset=Dataset(kind="grades"),
                view=ChartView(
                    variant="line" if by_date else "bar", x="date" if by_date else "course"
                ),
            ),
        )
    if data.profile:
        want(
            STUDENT_PROFILE,
            lambda: ProfileSpec(
                type="profile", title="Профиль", icon="idCard", dataset=Dataset(kind="profile")
            ),
        )
    for board in data.boards[:MAX_SYSTEM_BOARDS]:
        want(
            ranking_key(board),
            lambda board=board: RankingSpec(
                type="ranking",
                title=_plain_title(board, "Рейтинг"),
                icon="trophy",
                dataset=Dataset(kind="ranking", board=board),
            ),
        )
    if data.grades:
        # Words («отлично», «зачтено») have no chart; the latest grades are readable at any scale.
        want(
            GRADES_RECENT,
            lambda: ListSpec(
                type="list", title="Последние оценки", icon="grade", dataset=Dataset(kind="grades")
            ),
        )
    made: list[str] = []
    if not wanted:
        return made
    position = await _next_position(session, space_id)
    for key, spec in wanted:
        if await _insert_absent(
            session, user_id=user_id, space_id=space_id, key=key, spec=spec, position=position
        ):
            made.append(key)
            position += 1
    return made


async def prune_after_source_removal(
    session: AsyncSession, *, user_id: str, space_id: str, source_id: str
) -> None:
    """After a source's rows are deleted: drop the panels built for that source, and the system
    panels whose data no live source of the university has any more. Call it before the source
    row goes: the panel's foreign key to it is `SET NULL`. Panels the student hid or agent panels
    about other sources stay."""
    await session.execute(
        delete(Panel).where(Panel.user_id == user_id, Panel.source_id == source_id)
    )
    data = await space_data(session, user_id=user_id, space_id=space_id)
    system = await session.scalars(
        select(Panel).where(
            Panel.user_id == user_id, Panel.space_id == space_id, Panel.origin == "system"
        )
    )
    for panel in system:
        try:
            spec = parse_spec(panel.spec)
        except BotaiError:
            await session.delete(panel)
            continue
        kind = spec.dataset.kind
        gone = (
            (kind == "grades" and not data.grades)
            or (kind == "profile" and not data.profile)
            or (kind == "ranking" and spec.dataset.board not in data.boards)
        )
        if gone:
            await session.delete(panel)
    await session.flush()


# --- proposals of the agent -----------------------------------------------------------------


async def propose_panel(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str,
    key: str,
    spec: object,
    source_id: str | None = None,
    job_id: str | None = None,
    force: bool = False,
) -> Proposal:
    """The agent's panel: validated, stored `proposed` and invisible until the student accepts it.

    Idempotent by `key`: the same key again returns what is there (a still-proposed panel takes
    the new spec; a live one is left alone; a refused one is not proposed again unless `force`).
    Raises `VALIDATION` for a bad key or spec and when the university already holds
    `MAX_PANELS_PER_SPACE` panels, `NOT_FOUND` for a foreign space, source or job."""
    valid_key(key)
    model = parse_spec(spec)
    await get_space(session, user_id, space_id)
    if source_id is not None:
        found = await session.scalar(
            select(Source.id).where(
                Source.id == source_id, Source.user_id == user_id, Source.space_id == space_id
            )
        )
        if found is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Источник не найден")
    if job_id is not None:
        owned = await session.scalar(select(Job.id).where(Job.id == job_id, Job.user_id == user_id))
        if owned is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    stored = dump_spec(model)
    existing = await session.scalar(
        select(Panel).where(Panel.space_id == space_id, Panel.key == key, Panel.user_id == user_id)
    )
    if existing is not None:
        return await _reconsider(
            session,
            existing,
            model_type=model.type,
            stored=stored,
            source_id=source_id,
            job_id=job_id,
            force=force,
        )
    taken = await session.scalar(
        select(func.count())
        .select_from(Panel)
        .where(Panel.space_id == space_id, Panel.status != "dismissed")
    )
    if int(taken or 0) >= MAX_PANELS_PER_SPACE:
        raise BotaiError(
            ErrorCode.VALIDATION,
            f"В вузе уже {MAX_PANELS_PER_SPACE} панелей: сначала уберите ненужные",
        )
    made = await session.scalar(
        pg_insert(Panel)
        .values(
            id=new_id(),
            user_id=user_id,
            space_id=space_id,
            source_id=source_id,
            key=key,
            type=model.type,
            spec=stored,
            status="proposed",
            visible=False,
            hidden_in_all=False,
            position=await _next_position(session, space_id),
            origin="agent",
            proposed_by_job_id=job_id,
        )
        .on_conflict_do_nothing(index_elements=["space_id", "key"])
        .returning(Panel.id)
    )
    panel = await session.scalar(
        select(Panel).where(Panel.space_id == space_id, Panel.key == key, Panel.user_id == user_id)
    )
    if panel is None:  # a concurrent writer took the key for another user's space: impossible
        raise BotaiError(ErrorCode.INTERNAL, "Панель не создана")
    if made is None:  # lost a race for the key: treat the winner as the existing panel
        return await _reconsider(
            session,
            panel,
            model_type=model.type,
            stored=stored,
            source_id=source_id,
            job_id=job_id,
            force=force,
        )
    return Proposal(panel, "created")


async def _reconsider(
    session: AsyncSession,
    panel: Panel,
    *,
    model_type: str,
    stored: dict[str, object],
    source_id: str | None,
    job_id: str | None,
    force: bool,
) -> Proposal:
    if panel.status == "dismissed" and not force:
        return Proposal(panel, "dismissed")
    if panel.status == "active":
        return Proposal(panel, "exists")
    revived = panel.status == "dismissed"
    if not revived and panel.spec == stored and panel.type == model_type:
        return Proposal(panel, "exists")
    panel.type, panel.spec = model_type, stored
    panel.status, panel.visible = "proposed", False
    if source_id is not None:
        panel.source_id = source_id
    if job_id is not None:
        panel.proposed_by_job_id = job_id
    await session.flush()
    return Proposal(panel, "updated")


# --- the student's decisions ----------------------------------------------------------------


async def set_panel_status(
    session: AsyncSession, *, user_id: str, panel_id: str, status: PanelStatus
) -> Panel:
    """Accept (`active`, shown), dismiss (`dismissed`, hidden and not proposed again) or bring a
    dismissed panel back. A move the life of a panel does not have is `CONFLICT`."""
    panel = await get_panel(session, user_id=user_id, panel_id=panel_id)
    if panel.status == status:
        return panel
    if status not in _STATUS_MOVES.get(panel.status, frozenset()):
        raise BotaiError(
            ErrorCode.CONFLICT, f"Панель нельзя перевести из «{panel.status}» в «{status}»"
        )
    panel.status = status
    panel.visible = status == "active"
    await session.flush()
    return panel


async def update_panel(
    session: AsyncSession,
    *,
    user_id: str,
    panel_id: str,
    visible: bool | None = None,
    hidden_in_all: bool | None = None,
    position: int | None = None,
) -> Panel:
    """The student's switches; omitted ones stay. Dismissed panels cannot be switched on."""
    panel = await get_panel(session, user_id=user_id, panel_id=panel_id)
    if panel.status == "dismissed":
        raise BotaiError(ErrorCode.CONFLICT, "Отклонённую панель сначала нужно вернуть")
    if visible is not None:
        panel.visible = visible
    if hidden_in_all is not None:
        panel.hidden_in_all = hidden_in_all
    if position is not None:
        panel.position = position
    await session.flush()
    return panel
