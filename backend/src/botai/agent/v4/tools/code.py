"""`code_exec`: run agent-written code through the phase-1 executor (plan 1.2, architecture §4.4).

The tool is a thin typed adapter over `botai.agent.v4.executor`: the executor owns the process,
limits, workspace and created files; this handler validates the model's arguments, maps the
tool-facing language name (`javascript`) to the executor's (`js`) and caps what enters the model
context. The run's workspace is shared with `fs_*`: a script may read/write the same files those
tools see, and the files it creates are listed in the result relative to the workspace.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.executor import (
    ExecRequest,
    ExecResult,
    Executor,
    ExecutorError,
    ExecutorUnavailable,
)
from botai.agent.v4.protocol import (
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)

MAX_STDOUT_CHARS = 20_000
MAX_STDERR_CHARS = 8_000

_TOOL_LANGUAGES: dict[str, Literal["python", "js", "bash"]] = {
    "python": "python",
    "javascript": "js",
    "js": "js",
    "bash": "bash",
}


class CodeExecArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["python", "javascript", "bash"]
    code: str = Field(min_length=1, max_length=200_000)
    files: dict[str, str] = Field(default_factory=dict, max_length=100)
    timeout_s: float | None = Field(default=None, gt=0, le=300)
    memory_mb: int | None = Field(default=None, ge=32, le=4096)


def _invalid(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


def _result_payload(result: ExecResult) -> dict[str, Any]:
    stdout = result.stdout
    stderr = result.stderr
    return {
        "stdout": stdout[:MAX_STDOUT_CHARS],
        "stderr": stderr[:MAX_STDERR_CHARS],
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "files": result.files,
        "truncated": result.truncated or len(stdout) > MAX_STDOUT_CHARS,
        "timed_out": result.timed_out,
    }


class CodeExecTool:
    """`code_exec` handler: workspace-scoped subprocess with the executor's limits."""

    spec = ToolSpec(
        name="code_exec",
        description=(
            "Выполнить код в песочнице задачи: python, javascript (Node.js) или bash. Сеть "
            "доступна (фаза 1: свободный harness). Рабочая директория — workspace задачи: "
            "относительные пути читают/пишут те же файлы, что fs_*; созданные файлы перечислены "
            "в files. Возвращаются stdout/stderr/exit_code; лимиты времени и вывода ограничены. "
            "Обязательный аргумент — code (строка с исходным кодом), а не command."
        ),
        parameters=object_schema(
            {
                "language": {"type": "string", "enum": ["python", "javascript", "bash"]},
                "code": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200000,
                    "description": "Исходный код для исполнения (строка), не command.",
                },
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Файлы, записанные в workspace до запуска: путь -> содержимое.",
                },
                "timeout_s": {"type": ["number", "null"], "minimum": 0.5, "maximum": 300},
                "memory_mb": {"type": ["integer", "null"], "minimum": 32, "maximum": 4096},
            },
            required=["language", "code"],
        ),
        timeout_s=320.0,
        policy="write_soft",
    )

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = CodeExecArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        if context.workspace.resolve() != self._executor.workspace.root.resolve():
            return ToolResult.failure(
                "workspace_mismatch",
                "Workspace инструмента и исполнителя не совпадают; код запущен не будет.",
            )
        request = ExecRequest(
            language=_TOOL_LANGUAGES[args.language],
            code=args.code,
            files=args.files,
            timeout_s=args.timeout_s,
            memory_mb=args.memory_mb,
        )
        try:
            result = await self._executor.run(request)
        except ExecutorUnavailable as exc:
            return ToolResult.failure("executor_unavailable", str(exc))
        except ExecutorError as exc:
            return ToolResult.failure("executor_error", str(exc))
        data = _result_payload(result)
        if result.timed_out:
            return ToolResult.failure(
                "timeout",
                f"Код не уложился в лимит времени ({result.duration_ms} мс).",
                data=data,
            )
        summary = (
            f"Выполнено ({args.language}): exit {result.exit_code}, "
            f"{result.duration_ms} мс, файлов: {len(result.files)}"
        )
        return ToolResult.success(summary=summary, data=data, truncated=data["truncated"])


def build_code_tools(executor: Executor) -> list[ToolHandler]:
    return [CodeExecTool(executor)]


__all__ = ["CodeExecTool", "build_code_tools"]
