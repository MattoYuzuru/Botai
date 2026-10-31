"""`fs_read` / `fs_write` / `fs_list`: the run's workspace (plan 1.2, architecture §4.4).

Every path is resolved inside the workspace and a symlink cannot walk out of it: `resolve()`
follows links and the result must still be under the workspace root. This is containment of the
agent's own files, not an egress restriction — the free harness may read and write inside its
workspace as it likes.

Files are UTF-8 text; binary content is reported as a structured error instead of a broken string.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.protocol import ToolContext, ToolResult, ToolSpec, object_schema

MAX_TEXT_BYTES = 20_000_000


class FsPathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(default=".", max_length=4096)


class FsReadArgs(FsPathArgs):
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=20_000, ge=1, le=200_000)


class FsWriteArgs(FsPathArgs):
    content: str = Field(max_length=2_000_000)
    mode: Literal["overwrite", "append"] = "overwrite"
    create_parents: bool = True


class FsListArgs(FsPathArgs):
    recursive: bool = False
    max_entries: int = Field(default=500, ge=1, le=5000)


class WorkspaceEscape(ValueError):
    """The requested path resolves outside the run's workspace."""


def resolve_in_workspace(workspace: Path, relative: str) -> Path:
    """Resolve a tool path against the workspace; raise `WorkspaceEscape` when it leaves it."""
    root = workspace.resolve()
    raw = Path(relative)
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise WorkspaceEscape(f"Путь не разрешён: {relative!r}") from exc
    if resolved != root:
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise WorkspaceEscape(
                f"Путь {relative!r} выходит за пределы workspace; используйте относительный путь."
            ) from exc
    return resolved


def _relative(workspace: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace.resolve()).as_posix() or "."
    except ValueError:  # pragma: no cover - resolve_in_workspace already guarantees containment
        return path.name


def _invalid(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


class FsReadTool:
    """Read a UTF-8 text file from the workspace."""

    spec = ToolSpec(
        name="fs_read",
        description=(
            "Прочитать текстовый файл из workspace задачи. offset/max_chars — в символах; "
            "усечение видно в truncated и total_chars. Бинарные файлы не читаются."
        ),
        parameters=object_schema(
            {
                "path": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": "Путь относительно workspace.",
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200000,
                    "default": 20000,
                },
            },
            required=["path"],
        ),
        timeout_s=10.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = FsReadArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            path = resolve_in_workspace(context.workspace, args.path)
        except WorkspaceEscape as exc:
            return ToolResult.failure("workspace_escape", str(exc))
        if not path.exists():
            return ToolResult.failure("not_found", f"Файл не найден: {args.path}")
        if path.is_dir():
            return ToolResult.failure("is_directory", "Это каталог; используйте fs_list.")
        try:
            size = path.stat().st_size
            if size > MAX_TEXT_BYTES:
                return ToolResult.failure(
                    "file_too_large",
                    f"Файл {size} байт; используйте offset или разбейте чтение.",
                )
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult.failure("read_failed", f"Ошибка чтения: {type(exc).__name__}")
        if b"\x00" in raw[:4096]:
            return ToolResult.failure(
                "binary_file", "Файл похож на бинарный; текстовое чтение недоступно."
            )
        text = raw.decode("utf-8", errors="replace")
        total = len(text)
        chunk = text[args.offset : args.offset + args.max_chars]
        truncated = args.offset + len(chunk) < total
        return ToolResult.success(
            summary=f"{_relative(context.workspace, path)}: {len(chunk)} из {total} символов",
            data={
                "path": _relative(context.workspace, path),
                "offset": args.offset,
                "total_chars": total,
                "truncated": truncated,
                "content": chunk,
            },
            truncated=truncated,
        )


class FsWriteTool:
    """Write a UTF-8 text file in the workspace."""

    spec = ToolSpec(
        name="fs_write",
        description=(
            "Записать текстовый файл в workspace задачи (overwrite или append). Родительские "
            "каталоги создаются. Возвращает путь, число байт и sha256."
        ),
        parameters=object_schema(
            {
                "path": {
                    "type": "string",
                    "maxLength": 4096,
                    "description": "Путь относительно workspace.",
                },
                "content": {"type": "string", "maxLength": 2000000},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                "create_parents": {"type": "boolean", "default": True},
            },
            required=["path", "content"],
        ),
        timeout_s=10.0,
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = FsWriteArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            path = resolve_in_workspace(context.workspace, args.path)
        except WorkspaceEscape as exc:
            return ToolResult.failure("workspace_escape", str(exc))
        if path.is_dir():
            return ToolResult.failure("invalid_path", "Путь указывает на каталог.")
        parent = path.parent
        if not parent.exists():
            if not args.create_parents:
                return ToolResult.failure(
                    "parent_missing", "Родительский каталог не существует.", retryable=False
                )
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return ToolResult.failure("write_failed", f"Ошибка каталога: {type(exc).__name__}")
        payload = args.content.encode("utf-8")
        try:
            with path.open("ab" if args.mode == "append" else "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            return ToolResult.failure("write_failed", f"Ошибка записи: {type(exc).__name__}")
        total = path.stat().st_size
        return ToolResult.success(
            summary=f"{_relative(context.workspace, path)}: {total} байт ({args.mode})",
            data={
                "path": _relative(context.workspace, path),
                "mode": args.mode,
                "bytes": len(payload),
                "total_bytes": total,
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )


class FsListTool:
    """List the workspace (optionally recursively)."""

    spec = ToolSpec(
        name="fs_list",
        description=(
            "Показать файлы и каталоги workspace задачи. recursive — обойти вложенные; "
            "количество записей ограничено max_entries."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "maxLength": 4096, "default": "."},
                "recursive": {"type": "boolean", "default": False},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
            }
        ),
        timeout_s=10.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = FsListArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            path = resolve_in_workspace(context.workspace, args.path)
        except WorkspaceEscape as exc:
            return ToolResult.failure("workspace_escape", str(exc))
        if not path.exists():
            return ToolResult.failure("not_found", f"Путь не найден: {args.path}")
        if path.is_file():
            entries = [_entry(context.workspace, path)]
            truncated = False
        else:
            iterator = path.rglob("*") if args.recursive else path.iterdir()
            roots = sorted(iterator, key=lambda entry: entry.as_posix())
            truncated = len(roots) > args.max_entries
            entries = [_entry(context.workspace, entry) for entry in roots[: args.max_entries]]
        return ToolResult.success(
            summary=f"{_relative(context.workspace, path)}: {len(entries)} записей"
            + (" (список обрезан)" if truncated else ""),
            data={
                "path": _relative(context.workspace, path),
                "entries": entries,
                "truncated": truncated,
            },
            truncated=truncated,
        )


def _entry(workspace: Path, path: Path) -> dict[str, Any]:
    kind = "dir" if path.is_dir() else "file"
    size = path.stat().st_size if kind == "file" else 0
    return {"path": _relative(workspace, path), "kind": kind, "size": size}


__all__ = [
    "FsListArgs",
    "FsListTool",
    "FsPathArgs",
    "FsReadArgs",
    "FsReadTool",
    "FsWriteArgs",
    "FsWriteTool",
    "WorkspaceEscape",
    "resolve_in_workspace",
]
