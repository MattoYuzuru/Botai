"""`ui_propose_panel`, `panel_list`, `panel_update`: the agent offers panels of «Учёба» (D-039).

While it researches, connects or repairs a source the agent often sees data it was not asked
for — grades, a rating, the student's card. After the student agrees to connect that kind the
agent extends the playbook (`source_update_playbook`) and syncs; the system creates the default
panels of the new kinds by itself. What is left for these tools are the *extra* panels the agent
thinks are worth having («только оценки по алгоритмам», «рейтинг потока крупно»).

A panel is a thin presentation config over data the app already holds (`docs/v4/23-panels-backend.md`),
never the data itself and never code. So the tool

* validates the whole spec on the server with the same `PanelSpec` the API renders (a bad spec is
  a structured error the model can fix, nothing is stored);
* refuses to propose a panel over a dataset that has no rows for the space — an empty panel is a
  guess, not a finding — and tells the model why;
* stores the proposal through `panels.service.propose_panel` (`proposed`, invisible, `origin=agent`,
  the job that made it) and then asks the student with the existing `ask_user` machinery: a choice
  with a one-line preview. «Добавить панель» → `active`, «Не сейчас» → `dismissed` (and the key is
  not offered again unless the student asks for it). Without an ask channel (a run that cannot
  wait for the student) the panel stays `proposed`: the screen shows it as a suggestion card.

Personal data stays with its owner: every call is scoped to the run's user, the tool results and
the question carry counts and the student's own place, never the rows. Text the model supplies is
checked with the run's secret masker; a value the broker knows is refused rather than stored.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.protocol import (
    AskQuestion,
    FunctionTool,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.core.clock import Clock, SystemClock
from botai.core.errors import BotaiError
from botai.db.models import Panel
from botai.panels import service as panel_service
from botai.panels.data import DataResolver
from botai.panels.schemas import (
    PANEL_ICONS,
    ChartSpec,
    GradesData,
    PanelSpecModel,
    ProfileData,
    RankingData,
    dump_spec,
    parse_spec,
)

ADD_LABEL = "Добавить панель"
LATER_LABEL = "Не сейчас"
MAX_QUESTION = 500  # `ChoiceBlock.question` limit: the longer text is cut by the ask channel
MAX_RATIONALE = 300

DEFAULT_ICONS: dict[str, str] = {
    "kpi": "grade",
    "chart": "chart",
    "table": "table",
    "list": "grade",
    "ranking": "trophy",
    "profile": "idCard",
}
PANEL_TYPES = ("kpi", "chart", "table", "ranking", "profile", "list")
DATASET_KINDS = ("grades", "profile", "ranking")

PanelStatusFilter = Literal["proposed", "active", "dismissed", "any"]


class _DatasetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    board: str | None = None
    courseId: str | None = None


class _ProposeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=40)
    source_id: str | None = Field(default=None, max_length=40)
    key: str = Field(min_length=1, max_length=60)
    type: str
    title: str = Field(min_length=1, max_length=200)
    icon: str | None = Field(default=None, max_length=40)
    dataset: _DatasetArgs
    view: dict[str, Any] | None = None
    rationale: str = Field(min_length=1, max_length=2000)
    student_requested: bool = False


class _ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str | None = Field(default=None, max_length=40)
    status: PanelStatusFilter = "any"


class _UpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    panel_id: str = Field(min_length=1, max_length=40)
    visible: bool | None = None
    hidden_in_all: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)


def _invalid_arguments(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors(include_input=False, include_url=False)[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "arguments"
        parts.append(f"{location}: {error['msg']}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


def _plural(n: int, one: str, few: str, many: str) -> str:
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def _strings(value: Any) -> list[str]:
    """Every string inside a JSON-like value (keys included): what the masker must look at."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in cast("dict[Any, Any]", value).items():
            found.extend(_strings(key))
            found.extend(_strings(item))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for item in cast("list[Any]", value):
            found.extend(_strings(item))
        return found
    return []


def _failure(exc: BotaiError) -> ToolResult:
    return ToolResult.failure(exc.code.value, exc.detail)


class PanelToolkit:
    """Builds the panel tools over one session factory; `mask` is the run's secret masker."""

    def __init__(
        self,
        *,
        sm: async_sessionmaker[AsyncSession],
        clock: Clock | None = None,
        mask: Callable[[str], str] | None = None,
    ) -> None:
        self._sm = sm
        self._clock: Clock = clock or SystemClock()
        self._mask = mask
        self._handlers: list[ToolHandler] = [
            FunctionTool(self.propose_spec, self._propose),
            FunctionTool(self.list_spec, self._list),
            FunctionTool(self.update_spec, self._update),
        ]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    # --- ui_propose_panel ------------------------------------------------------------------

    async def _propose(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        try:
            args = _ProposeArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid_arguments(exc)
        leaked = self._secret_in(arguments)
        if leaked:
            return ToolResult.failure(
                "secret_in_text",
                "В тексте панели есть значение секрета. Секреты нельзя класть в название, "
                "ключ или пояснение: опиши панель словами без значения.",
            )
        if args.type not in PANEL_TYPES:
            return ToolResult.failure(
                "invalid_spec", f"type: допустимы {', '.join(PANEL_TYPES)}", hint=_SPEC_HINT
            )
        raw = self._raw_spec(args)
        try:
            spec = parse_spec(raw)
            panel_service.valid_key(args.key)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail, hint=_SPEC_HINT)
        stored = dump_spec(spec)
        job_id = context.metadata.get("job_id")
        job = job_id if isinstance(job_id, str) and job_id else None

        try:
            async with self._sm() as session:
                await panel_service.get_space(session, user_id, args.space_id)
                counted = await self._count(session, user_id, args.space_id, spec, stored)
                if isinstance(counted, ToolResult):
                    return counted
                rows, preview = counted
                proposal = await panel_service.propose_panel(
                    session,
                    user_id=user_id,
                    space_id=args.space_id,
                    key=args.key,
                    spec=stored,
                    source_id=args.source_id,
                    job_id=job,
                    force=args.student_requested,
                )
                panel_id, status, outcome = (
                    proposal.panel.id,
                    proposal.panel.status,
                    proposal.outcome,
                )
                await session.commit()
        except BotaiError as exc:
            return _failure(exc)

        base: dict[str, Any] = {
            "panelId": panel_id,
            "key": args.key,
            "outcome": outcome,
            "status": status,
            "rows": rows,
        }
        if outcome == "dismissed":
            return ToolResult.success(
                summary=(
                    f"Студент уже отклонял панель «{spec.title}»: не предлагаю её снова. "
                    "Если он сам попросил такую панель — вызови с student_requested=true."
                ),
                data=base,
            )
        if status == "active":
            return ToolResult.success(
                summary=f"Панель «{spec.title}» уже добавлена: ничего не менял.", data=base
            )
        if context.ask is None:
            return ToolResult.success(
                summary=(
                    f"Предложение панели «{spec.title}» сохранено. Спросить студента сейчас "
                    "нельзя: он увидит карточку-предложение в «Учёбе» и решит сам."
                ),
                data={**base, "answer": "not_asked"},
            )
        question = self._question(spec.title, preview, args.rationale)
        answer = await context.ask(
            AskQuestion(
                question=question,
                options=(ADD_LABEL, LATER_LABEL),
                input_kind="choice",
            )
        )
        if answer.cancelled or answer.choice not in (0, 1):
            return ToolResult.success(
                summary=(
                    f"Студент не ответил про панель «{spec.title}»: она осталась предложением "
                    "в «Учёбе». Не повторяй вопрос."
                ),
                data={**base, "answer": "no_answer"},
            )
        accepted = answer.choice == 0
        try:
            async with self._sm() as session:
                changed = await panel_service.set_panel_status(
                    session,
                    user_id=user_id,
                    panel_id=panel_id,
                    status="active" if accepted else "dismissed",
                )
                new_status = changed.status
                await session.commit()
        except BotaiError as exc:
            return _failure(exc)
        if accepted:
            return ToolResult.success(
                summary=f"Панель «{spec.title}» добавлена в «Учёбу».",
                data={**base, "status": new_status, "answer": "accepted"},
            )
        return ToolResult.success(
            summary=(
                f"Студент отказался от панели «{spec.title}»: она убрана и больше не "
                "предлагается. Не спорь и не предлагай её снова."
            ),
            data={**base, "status": new_status, "answer": "declined"},
        )

    def _secret_in(self, arguments: dict[str, Any]) -> bool:
        mask = self._mask
        if mask is None:
            return False
        return any(mask(text) != text for text in _strings(arguments))

    @staticmethod
    def _raw_spec(args: _ProposeArgs) -> dict[str, Any]:
        """The spec the model described, with the icon filled in when it did not choose one."""
        dataset: dict[str, Any] = {"kind": args.dataset.kind}
        if args.dataset.board is not None:
            dataset["board"] = args.dataset.board
        if args.dataset.courseId is not None:
            dataset["courseId"] = args.dataset.courseId
        return {
            "v": 1,
            "type": args.type,
            "title": args.title,
            "icon": args.icon or DEFAULT_ICONS.get(args.type, "table"),
            "dataset": dataset,
            "view": args.view or {},
        }

    async def _count(
        self,
        session: AsyncSession,
        user_id: str,
        space_id: str,
        spec: PanelSpecModel,
        stored: dict[str, Any],
    ) -> tuple[int, str] | ToolResult:
        """What the panel would show: row count and a one-line preview, or a refusal when the
        dataset is empty. Reads through the same resolver the API uses (live sources only), on a
        panel that is never stored."""
        resolver = DataResolver(session, user_id, self._clock.now())
        data = await resolver.resolve(Panel(space_id=space_id, spec=stored))
        title = spec.title
        if isinstance(data, GradesData):
            shown = len(data.points) if isinstance(spec, ChartSpec) else data.total
            if shown == 0:
                reason = (
                    "у оценок вуза нет чисел со шкалой — график строить не из чего; предложи "
                    "таблицу или плитки"
                    if isinstance(spec, ChartSpec) and data.total
                    else "в приложении нет таких оценок"
                )
                return self._empty(title, reason)
            noun = _plural(data.total, "оценка", "оценки", "оценок")
            preview = f"{data.total} {noun}"
            if data.numeric:
                preview += f", с числами: {data.numeric}"
            return data.total, preview
        if isinstance(data, ProfileData):
            if data.total == 0:
                return self._empty(title, "в приложении нет полей профиля")
            return data.total, f"{data.total} {_plural(data.total, 'поле', 'поля', 'полей')}"
        if isinstance(data, RankingData):
            if data.total == 0:
                return self._empty(title, "в приложении нет такого списка рейтинга")
            preview = f"{data.total} {_plural(data.total, 'строка', 'строки', 'строк')}"
            mine = next((row for row in data.rows if row.is_me), None)
            if mine is not None:
                of = mine.total or data.participants
                preview += f", вы — {mine.position}" + (f" из {of}" if of else "")
            return data.total, preview
        return self._empty(title, "данные панели не прочитались")

    @staticmethod
    def _empty(title: str, reason: str) -> ToolResult:
        return ToolResult.failure(
            "empty_dataset",
            f"Панель «{title}» не предложена: {reason}. Сначала подключи и синхронизируй эти "
            "данные (source_update_playbook с нужным видом, source_sync), потом предлагай панель.",
        )

    @staticmethod
    def _question(title: str, preview: str, rationale: str) -> str:
        head = f"Добавить в «Учёбу» панель «{title}»? {preview}."
        room = MAX_QUESTION - len(head) - 1
        note = " ".join(rationale.split())
        if room < 20 or not note:
            return head[:MAX_QUESTION]
        if len(note) > room:
            note = note[: room - 1].rstrip() + "…"
        return f"{head} {note}"

    propose_spec = ToolSpec(
        name="ui_propose_panel",
        description=(
            "Предложить студенту панель в «Учёбе» (карточку с его данными: оценки, рейтинг, "
            "профиль) и спросить согласия. Панель — только настройка показа поверх данных, "
            "которые уже есть в приложении; HTML и код не принимаются. Сервер проверяет spec; "
            "если для панели нет строк данных, вернёт empty_dataset — тогда сначала подключи "
            "и синхронизируй вид. Типы: kpi (плитки: count/latest/best), chart (variant "
            "bar|line, x date|course, limit; нужны оценки с числами), table (columns из "
            "title,course,value,scale,date; limit), list (limit), ranking (dataset.board "
            "обязателен; top, highlightMe), profile (fields — ключи полей, пусто = все). "
            "dataset.kind: grades для kpi/chart/table/list, ranking, profile. Иконку можно не "
            "указывать — подберётся по типу; допустимы: "
            + ", ".join(PANEL_ICONS)
            + ". Перед вызовом посмотри panel_list: системные панели оценок, профиля и "
            "рейтинга появляются сами после синхронизации — не дублируй их, предлагай только "
            "дополнительные. Ключ key стабилен: повтор того же ключа не создаёт дубль, "
            "отклонённый студентом ключ повторно не предлагается (student_requested=true — "
            "только если студент сам попросил такую панель). В title и rationale нельзя "
            "секреты, ссылки и разметка. rationale — одна фраза «зачем студенту эта панель»."
        ),
        parameters=object_schema(
            {
                "space_id": {"type": "string", "description": "id вуза студента"},
                "source_id": {
                    "type": ["string", "null"],
                    "description": "источник, из данных которого панель (если известен)",
                },
                "key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 60,
                    "description": "стабильный слаг: латиница, цифры, «-», «_», «:»",
                },
                "type": {"type": "string", "enum": list(PANEL_TYPES)},
                "title": {"type": "string", "minLength": 1, "maxLength": 60},
                "icon": {"type": ["string", "null"], "description": "имя иконки или null"},
                "dataset": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": list(DATASET_KINDS)},
                        "board": {
                            "type": ["string", "null"],
                            "description": "название списка рейтинга как у источника (ranking)",
                        },
                        "courseId": {
                            "type": ["string", "null"],
                            "description": "id курса приложения (только grades)",
                        },
                    },
                    "required": ["kind"],
                },
                "view": {
                    "type": ["object", "null"],
                    "description": "настройки показа по типу панели; пусто — по умолчанию",
                },
                "rationale": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "одна фраза: зачем студенту эта панель (показывается ему)",
                },
                "student_requested": {"type": "boolean", "default": False},
            },
            required=["space_id", "key", "type", "title", "dataset", "rationale"],
        ),
        timeout_s=600.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )

    # --- panel_list ------------------------------------------------------------------------

    async def _list(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        try:
            args = _ListArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid_arguments(exc)
        statuses: tuple[Literal["proposed", "active", "dismissed"], ...] = (
            ("proposed", "active", "dismissed") if args.status == "any" else (args.status,)
        )
        items: list[dict[str, Any]] = []
        try:
            async with self._sm() as session:
                for status in statuses:
                    rows = await panel_service.list_panels(
                        session, user_id=user_id, space_id=args.space_id, status=status
                    )
                    items.extend(_panel_item(panel, space.id) for panel, space, _ in rows)
        except BotaiError as exc:
            return _failure(exc)
        return ToolResult.success(
            summary=f"Панелей: {len(items)}",
            data={"panels": items},
        )

    list_spec = ToolSpec(
        name="panel_list",
        description=(
            "Список панелей «Учёбы» студента (id, ключ, тип, название, статус, набор данных). "
            "Вызывай перед ui_propose_panel, чтобы не дублировать системные панели. space_id — "
            "только один вуз; без него — все вузы студента. status: proposed|active|dismissed|any."
        ),
        parameters=object_schema(
            {
                "space_id": {"type": ["string", "null"]},
                "status": {
                    "type": "string",
                    "enum": ["proposed", "active", "dismissed", "any"],
                    "default": "any",
                },
            }
        ),
        timeout_s=30.0,
        policy="read",
        idempotent=True,
    )

    # --- panel_update ----------------------------------------------------------------------

    async def _update(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        try:
            args = _UpdateArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid_arguments(exc)
        if args.visible is None and args.hidden_in_all is None and args.position is None:
            return ToolResult.failure(
                "invalid_arguments", "Нужно хотя бы одно из: visible, hidden_in_all, position"
            )
        try:
            async with self._sm() as session:
                panel = await panel_service.update_panel(
                    session,
                    user_id=user_id,
                    panel_id=args.panel_id,
                    visible=args.visible,
                    hidden_in_all=args.hidden_in_all,
                    position=args.position,
                )
                result = {
                    "panelId": panel.id,
                    "visible": panel.visible,
                    "hiddenInAll": panel.hidden_in_all,
                    "position": panel.position,
                }
                await session.commit()
        except BotaiError as exc:
            return _failure(exc)
        return ToolResult.success(summary="Панель обновлена", data=result)

    update_spec = ToolSpec(
        name="panel_update",
        description=(
            "Показать или скрыть панель студента и поменять её место (visible, hidden_in_all — "
            "скрыть только в режиме «Все вузы», position). Только по прямой просьбе студента "
            "(«скрой рейтинг», «подними оценки выше»); сам панели не прячь. Отклонённую панель "
            "так не включить."
        ),
        parameters=object_schema(
            {
                "panel_id": {"type": "string"},
                "visible": {"type": ["boolean", "null"]},
                "hidden_in_all": {"type": ["boolean", "null"]},
                "position": {"type": ["integer", "null"], "minimum": 0, "maximum": 10000},
            },
            required=["panel_id"],
        ),
        timeout_s=30.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )


_SPEC_HINT = (
    "Проверь type/title/icon/dataset/view: kpi, chart, table, list — dataset.kind=grades; "
    "ranking — dataset.kind=ranking и dataset.board; profile — dataset.kind=profile. "
    "title — простой текст до 60 знаков. Иконки: " + ", ".join(PANEL_ICONS) + "."
)


def _panel_item(panel: Panel, space_id: str) -> dict[str, Any]:
    spec: dict[str, Any] = panel.spec
    raw = spec.get("dataset")
    dataset: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    return {
        "id": panel.id,
        "key": panel.key,
        "type": panel.type,
        "title": spec.get("title"),
        "icon": spec.get("icon"),
        "status": panel.status,
        "visible": panel.visible,
        "origin": panel.origin,
        "spaceId": space_id,
        "sourceId": panel.source_id,
        "dataset": {"kind": dataset.get("kind"), "board": dataset.get("board")},
    }


def build_panel_tools(toolkit: PanelToolkit) -> list[ToolHandler]:
    """The panel set; the wiring adds it next to `source_*` when the v4 engine is on."""
    return toolkit.tools()


__all__ = ["PanelToolkit", "build_panel_tools"]
