"""`source_*` tools of the v4 agent: create, probe, sync, repair, playbook, list, live read.

The tools are thin wrappers over `sources.v4` service and runner: each opens its own session,
commits its own changes and returns structured data. The runner is injected (transport, artifact
store, executor) so tests never touch the network and production wiring decides where artifact
bytes live. No tool returns a cookie, token or password: the session stays in the runner, and the
report contains only counts, sanitized URLs and artifact ids.

The machinery follows `agent/v4/protocol.py`: a `ToolSpec` with a strict JSON schema plus
`run(arguments, context) -> ToolResult`. The runtime validates arguments before calling a handler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.executor import Executor
from botai.agent.v4.protocol import (
    FunctionTool,
    ToolArtifact,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.core.clock import Clock
from botai.core.errors import BotaiError
from botai.db.models import Source
from botai.sources.v4 import artifacts, playbooks, service
from botai.sources.v4.runner import RunReport, SecretReader, SourceRunner
from botai.sources.v4.schemas import SourcePatch

ExecutorFactory = Callable[[], Executor]
MAX_REPORT_CHARS = 60_000


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListArgs(_Args):
    space_id: str | None = None
    status: str | None = None
    kind: str | None = None


class CreateArgs(_Args):
    space_id: str
    title: str
    url: str | None = None
    link: str | None = None
    file_sha256: str | None = None
    mailbox: str | None = None
    kinds: list[str] = Field(default_factory=list)
    playbook_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    auth_ref: str | None = None


class ProbeArgs(_Args):
    source_id: str
    spec: dict[str, Any] | None = None
    code: str | None = None
    language: Literal["python", "js"] = "python"
    version: int | None = Field(default=None, ge=1)
    persist: bool = False


class SyncArgs(_Args):
    source_id: str
    version: int | None = Field(default=None, ge=1)


class RepairArgs(_Args):
    source_id: str
    spec: dict[str, Any] | None = None
    code: str | None = None
    language: Literal["python", "js"] = "python"
    version: int | None = Field(default=None, ge=1)
    activate: bool = False


class UpdatePlaybookArgs(_Args):
    source_id: str
    spec: dict[str, Any] | None = None
    code: str | None = None
    tests: list[dict[str, Any]] = Field(default_factory=list)
    canary: dict[str, Any] | None = None
    activate: bool = True


class DisableArgs(_Args):
    source_id: str


class ReadLiveArgs(_Args):
    source_id: str
    url: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "POST"] = "GET"
    max_chars: int = Field(default=20_000, ge=200, le=200_000)
    save_artifact: bool = True


def _source_data(source: Source) -> dict[str, Any]:
    return {
        "id": source.id,
        "spaceId": source.space_id,
        "universityId": source.university_id,
        "title": source.title,
        "entryUrl": source.entry_url,
        "kinds": list(source.kinds),
        "status": source.status,
        "playbookId": source.playbook_id,
        "trust": source.trust,
        "priority": source.priority,
        "lastSyncAt": source.last_sync_at.isoformat() if source.last_sync_at else None,
        "lastError": source.last_error,
    }


def _report_data(report: RunReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def _report_summary(report: RunReport) -> str:
    counts = ", ".join(f"{name}: {count}" for name, count in sorted(report.counts.items()))
    errors = "; ".join(error.code for error in report.errors)
    head = f"{report.run}: {'ok' if report.ok else 'failed'}"
    parts = [head, counts or "записей нет"]
    if errors:
        parts.append(f"ошибки: {errors}")
    return "; ".join(parts)


class SourceToolkit:
    """Builds the `source_*` tools over one session factory and one runner configuration."""

    def __init__(
        self,
        *,
        sm: async_sessionmaker[AsyncSession],
        transport: httpx.AsyncBaseTransport | None = None,
        artifact_store: artifacts.ArtifactStore | None = None,
        executor_factory: ExecutorFactory | None = None,
        clock: Clock | None = None,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        secret_reader: SecretReader | None = None,
    ) -> None:
        self._sm = sm
        self._transport = transport
        self._artifact_store = artifact_store
        self._executor_factory = executor_factory
        self._clock = clock
        self._cookies = dict(cookies or {})
        self._headers = dict(headers or {})
        self._secret_reader = secret_reader
        self._handlers: list[ToolHandler] = [
            FunctionTool(self.list_spec, self._list),
            FunctionTool(self.create_spec, self._create),
            FunctionTool(self.probe_spec, self._probe),
            FunctionTool(self.sync_spec, self._sync),
            FunctionTool(self.repair_spec, self._repair),
            FunctionTool(self.update_playbook_spec, self._update_playbook),
            FunctionTool(self.disable_spec, self._disable),
            FunctionTool(self.read_live_spec, self._read_live),
        ]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    def runner(self, session: AsyncSession, user_id: str) -> SourceRunner:
        return SourceRunner(
            session,
            user_id=user_id,
            transport=self._transport,
            artifact_store=self._artifact_store,
            executor_factory=self._executor_factory,
            clock=self._clock,
            cookies=self._cookies,
            headers=self._headers,
            secret_reader=self._secret_reader,
        )

    # --- handlers --------------------------------------------------------------------------

    async def _list(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(ListArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        async with self._sm() as session:
            rows = await service.list_sources(
                session,
                user_id=user_id,
                space_id=args.space_id,
                status=args.status,
                kind=args.kind,
            )
            data = {"sources": [_source_data(row) for row in rows]}
        return ToolResult.success(summary=f"Источников: {len(data['sources'])}", data=data)

    async def _create(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(CreateArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                source = await service.create_source(
                    session,
                    user_id=user_id,
                    space_id=args.space_id,
                    title=args.title,
                    entry_url=args.url,
                    link=args.link,
                    file_sha256=args.file_sha256,
                    mailbox=args.mailbox,
                    kinds=args.kinds,
                    playbook_id=args.playbook_id,
                    config=args.config,
                    auth_ref=args.auth_ref,
                )
                await session.commit()
                data = _source_data(source)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        return ToolResult.success(summary=f"Источник «{args.title}» создан", data={"source": data})

    async def _probe(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(ProbeArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                report = await self.runner(session, user_id).probe(
                    args.source_id,
                    spec=args.spec,
                    code=args.code,
                    language=args.language,
                    version=args.version,
                    persist=args.persist,
                )
                await session.commit()
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        return _report_result(report, summary=_report_summary(report))

    async def _sync(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(SyncArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                report = await self.runner(session, user_id).sync(
                    args.source_id, version=args.version
                )
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        return _report_result(report, summary=_report_summary(report))

    async def _repair(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(RepairArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                source = await service.get_source(
                    session, user_id=user_id, source_id=args.source_id
                )
                runner = self.runner(session, user_id)
                if args.spec is not None or args.code is not None:
                    if source.playbook_id is None:
                        return ToolResult.failure(
                            "no_playbook", "У источника нет playbook: сначала создайте его"
                        )
                    version_row = await playbooks.add_playbook_version(
                        session,
                        user_id=user_id,
                        playbook_id=source.playbook_id,
                        spec=args.spec,
                        code=args.code,
                    )
                    report = await runner.canary(args.source_id, version=version_row.version)
                    if args.activate and report.ok:
                        await playbooks.activate_playbook_version(
                            session,
                            user_id=user_id,
                            playbook_id=source.playbook_id,
                            version=version_row.version,
                        )
                    await session.commit()
                elif args.version is not None:
                    report = await runner.canary(args.source_id, version=args.version)
                    if args.activate and report.ok and source.playbook_id is not None:
                        await playbooks.activate_playbook_version(
                            session,
                            user_id=user_id,
                            playbook_id=source.playbook_id,
                            version=args.version,
                        )
                        await session.commit()
                else:
                    report = await runner.repair(args.source_id)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        if not report.ok and args.activate:
            return ToolResult.failure(
                "canary_failed",
                "Кандидат не прошёл canary: ничего не активировано",
                data={"report": _report_data(report)},
            )
        return _report_result(
            report,
            summary=_report_summary(report)
            + ("; активировано" if args.activate and report.ok else ""),
        )

    async def _update_playbook(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(UpdatePlaybookArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                source = await service.get_source(
                    session, user_id=user_id, source_id=args.source_id
                )
                if source.playbook_id is None:
                    playbook = await playbooks.create_playbook(
                        session,
                        user_id=user_id,
                        title=source.title,
                        university_id=source.university_id,
                    )
                    source = await service.update_source(
                        session,
                        user_id=user_id,
                        source_id=source.id,
                        patch=SourcePatch(playbook_id=playbook.id),
                    )
                playbook_id = source.playbook_id
                assert playbook_id is not None
                row = await playbooks.add_playbook_version(
                    session,
                    user_id=user_id,
                    playbook_id=playbook_id,
                    spec=args.spec,
                    code=args.code,
                    tests=args.tests,
                    canary=args.canary,
                )
                report: RunReport | None = None
                activated = False
                if args.activate:
                    report = await self.runner(session, user_id).canary(
                        args.source_id, version=row.version
                    )
                    if report.ok:
                        await playbooks.activate_playbook_version(
                            session,
                            user_id=user_id,
                            playbook_id=playbook_id,
                            version=row.version,
                        )
                        activated = True
                await session.commit()
                data: dict[str, Any] = {
                    "version": row.version,
                    "activated": activated,
                    "checksum": row.checksum,
                }
                if report is not None:
                    data["canary"] = _report_data(report)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        if args.activate and not data["activated"]:
            return ToolResult.failure(
                "canary_failed",
                "Новая версия не прошла canary и не активирована",
                data=data,
            )
        return ToolResult.success(
            summary=f"Версия {data['version']} добавлена"
            + (" и активирована" if data["activated"] else ""),
            data=data,
        )

    async def _disable(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(DisableArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                source = await service.disable_source(
                    session, user_id=user_id, source_id=args.source_id
                )
                await session.commit()
                data = _source_data(source)
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        return ToolResult.success(summary="Источник выключен", data={"source": data})

    async def _read_live(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = _user(context)
        if user_id is None:
            return ToolResult.failure("no_user", "Инструмент требует пользователя")
        args = _validate(ReadLiveArgs, arguments)
        if isinstance(args, ToolResult):
            return args
        try:
            async with self._sm() as session:
                result = await self.runner(session, user_id).read_live(
                    args.source_id,
                    url=args.url,
                    method=args.method,
                    max_chars=args.max_chars,
                    save_artifact=args.save_artifact,
                )
                await session.commit()
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        data = result.model_dump(mode="json")
        if not result.ok:
            return ToolResult.failure(
                result.error.code if result.error else "read_failed",
                result.error.detail if result.error else "Чтение не удалось",
                data=data,
            )
        tool_artifacts = (
            [_tool_artifact(result.artifact_id, kind="source_artifact")]
            if result.artifact_id
            else []
        )
        return ToolResult.success(
            summary=f"Прочитано {len(result.text)} знаков, HTTP {result.status}",
            data=data,
            artifacts=tool_artifacts,
        )

    # --- specs -----------------------------------------------------------------------------

    list_spec = ToolSpec(
        name="source_list",
        description=(
            "Список источников пользователя: id, вуз, виды, статус, последняя синхронизация, "
            "ошибка. Фильтры: пространство, статус, вид. Вызывайте перед изменением источника."
        ),
        parameters=object_schema(
            {
                "space_id": {"type": ["string", "null"], "description": "Пространство вуза"},
                "status": {"type": ["string", "null"], "description": "Статус источника"},
                "kind": {"type": ["string", "null"], "description": "Вид источника"},
            }
        ),
        timeout_s=30.0,
        policy="read",
        idempotent=True,
    )
    create_spec = ToolSpec(
        name="source_create",
        description=(
            "Создать источник вуза: кабинет/сайт (url), календарь по личной ссылке (link), файл "
            "(file_sha256) или почту (mailbox). Источник один раз: тот же адрес отвечает конфликтом. "
            "Созданный источник нужно проверить через source_probe, затем синхронизировать."
        ),
        parameters=object_schema(
            {
                "space_id": {"type": "string", "description": "Пространство вуза"},
                "title": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Человеческое название",
                },
                "url": {"type": ["string", "null"], "description": "Адрес сайта/кабинета"},
                "link": {
                    "type": ["string", "null"],
                    "description": "Личная ссылка календаря (секрет)",
                },
                "file_sha256": {"type": ["string", "null"], "description": "sha256 файла"},
                "mailbox": {"type": ["string", "null"], "description": "Почтовый ящик"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Виды: природа и данные",
                },
                "playbook_id": {"type": ["string", "null"], "description": "Готовый playbook"},
                "config": {"type": "object", "description": "Параметры источника"},
                "auth_ref": {"type": ["string", "null"], "description": "Имя секрета в Vault"},
            },
            required=["space_id", "title"],
        ),
        timeout_s=60.0,
        policy="write_soft",
        serial_only=True,
    )
    probe_spec = ToolSpec(
        name="source_probe",
        description=(
            "Проверить источник черновиком playbook (spec/code) или готовой версией, ничего не "
            "сохраняя: сколько записей, какие ошибки, что видно. Возвращает отчёт и превью. "
            "persist=true сохраняет артефакты пробного чтения."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string"},
                "spec": {"type": ["object", "null"], "description": "Декларативный spec playbook"},
                "code": {"type": ["string", "null"], "description": "Код парсера (python/js)"},
                "language": {"type": "string", "enum": ["python", "js"], "default": "python"},
                "version": {"type": ["integer", "null"], "minimum": 1},
                "persist": {"type": "boolean", "default": False},
            },
            required=["source_id"],
        ),
        timeout_s=300.0,
        policy="read",
        max_result_chars=MAX_REPORT_CHARS,
    )
    sync_spec = ToolSpec(
        name="source_sync",
        description=(
            "Синхронизировать источник: выполнить активную (или указанную) версию playbook, "
            "сохранить артефакты/состояние и обновить статус. Возвращает честный отчёт: записи по "
            "видам, ошибки, что прочитано."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string"},
                "version": {"type": ["integer", "null"], "minimum": 1},
            },
            required=["source_id"],
        ),
        timeout_s=300.0,
        policy="write_soft",
        serial_only=True,
        max_result_chars=MAX_REPORT_CHARS,
    )
    repair_spec = ToolSpec(
        name="source_repair",
        description=(
            "Починить источник: прогнать кандидат (spec/code или version) как canary, сохранить "
            "результат версии. activate=true активирует только успешный кандидат; при провале "
            "ничего не меняется."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string"},
                "spec": {"type": ["object", "null"]},
                "code": {"type": ["string", "null"]},
                "language": {"type": "string", "enum": ["python", "js"], "default": "python"},
                "version": {"type": ["integer", "null"], "minimum": 1},
                "activate": {"type": "boolean", "default": False},
            },
            required=["source_id"],
        ),
        timeout_s=300.0,
        policy="write_soft",
        serial_only=True,
        max_result_chars=MAX_REPORT_CHARS,
    )
    update_playbook_spec = ToolSpec(
        name="source_update_playbook",
        description=(
            "Добавить новую версию playbook источника (spec и/или код, тесты). activate=true "
            "сначала прогоняет canary и активирует версию только при успехе; при провале версия "
            "остаётся для разбора."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string"},
                "spec": {"type": ["object", "null"]},
                "code": {"type": ["string", "null"]},
                "tests": {"type": "array", "items": {"type": "object"}},
                "canary": {"type": ["object", "null"]},
                "activate": {"type": "boolean", "default": True},
            },
            required=["source_id"],
        ),
        timeout_s=300.0,
        policy="write_soft",
        serial_only=True,
    )
    disable_spec = ToolSpec(
        name="source_disable",
        description=(
            "Выключить источник обратимо: он перестаёт синхронизироваться, данные скрываются, "
            "но не удаляются. Включение — отдельное действие приложения."
        ),
        parameters=object_schema({"source_id": {"type": "string"}}, required=["source_id"]),
        timeout_s=30.0,
        policy="write_soft",
        serial_only=True,
    )
    read_live_spec = ToolSpec(
        name="source_read_live",
        description=(
            "Прочитать одну страницу или API источника его сессией (cookie/токен остаются внутри "
            "инструмента): текст/JSON и артефакт. Для разбора данных, которых нет в сохранённом "
            "снимке."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": "string"},
                "url": {"type": "string", "maxLength": 4096},
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 200000},
                "save_artifact": {"type": "boolean", "default": True},
            },
            required=["source_id", "url"],
        ),
        timeout_s=120.0,
        policy="read",
    )


def build_source_tools(toolkit: SourceToolkit) -> list[ToolHandler]:
    """The phase-2 tool set; the wiring adds it to the runtime when the v4 engine is on."""
    return toolkit.tools()


def _user(context: ToolContext) -> str | None:
    return context.user_id


def _validate(model: type[_Args], arguments: dict[str, Any]) -> Any:
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        return ToolResult.failure("invalid_arguments", _validation_detail(exc))


def _validation_detail(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "arguments"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _tool_artifact(artifact_id: str | None, *, kind: str) -> ToolArtifact:
    return ToolArtifact(id=artifact_id or "", kind=kind)


def _report_result(report: RunReport, *, summary: str) -> ToolResult:
    tool_artifacts = [
        _tool_artifact(artifact_id, kind="source_artifact")
        for artifact_id in report.artifact_ids[:20]
    ]
    if not report.ok:
        return ToolResult.failure(
            report.errors[0].code if report.errors else "run_failed",
            summary,
            data={"report": _report_data(report)},
            retryable=any(error.retryable for error in report.errors),
        )
    return ToolResult.success(
        summary=summary, data={"report": _report_data(report)}, artifacts=tool_artifacts
    )
