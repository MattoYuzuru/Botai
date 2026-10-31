"""`trace_read`: the agent reads its own and past runs (D-014, architecture §4.8).

The trace store is injected into `ToolContext` by the runtime, so the tool works with the in-memory
store of a test run and with the future DB adapter without changes. Payloads are data the runtime
recorded: model answers, tool arguments/results, counters, limits.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.protocol import ToolContext, ToolResult, ToolSpec, object_schema


class TraceReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, max_length=64)
    list_runs: bool = False
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    include_payloads: bool = True


class TraceReadTool:
    """Read the current run's events, another run, or the list of runs."""

    spec = ToolSpec(
        name="trace_read",
        description=(
            "Прочитать трассу: шаги текущего или прошлого запуска (модель, вызовы инструментов, "
            "результаты, лимиты, токены, стоимость) либо список запусков. Помогает понять, "
            "почему агент упёрся в стену и что делал в прошлый раз."
        ),
        parameters=object_schema(
            {
                "run_id": {
                    "type": ["string", "null"],
                    "maxLength": 64,
                    "description": "id запуска; по умолчанию — текущий.",
                },
                "list_runs": {
                    "type": "boolean",
                    "default": False,
                    "description": "Вернуть список запусков вместо событий.",
                },
                "after_seq": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
                "include_payloads": {"type": "boolean", "default": True},
            }
        ),
        timeout_s=10.0,
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = TraceReadArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure("invalid_arguments", _invalid(exc))
        store = context.trace_store
        if store is None:
            return ToolResult.failure(
                "no_trace_store", "В этом запуске трасса недоступна для чтения."
            )
        if args.list_runs:
            runs = await store.list_runs(limit=args.limit)
            return ToolResult.success(
                summary=f"Запусков в трассе: {len(runs)}",
                data={"runs": [run.model_dump(mode="json") for run in runs]},
            )
        run_id = args.run_id or context.run_id
        run = await store.get_run(run_id)
        if run is None:
            return ToolResult.failure("run_not_found", f"Трасса {run_id} не найдена.")
        selected = [event for event in run.events if event.seq >= args.after_seq]
        events = selected[: args.limit]
        payloads = [event.model_dump(mode="json") for event in events]
        if not args.include_payloads:
            for item in payloads:
                item.pop("payload", None)
        return ToolResult.success(
            summary=f"{run_id}: {len(events)} событий из {len(selected)}",
            data={
                "run": {
                    "run_id": run.run_id,
                    "task": run.task,
                    "status": run.status,
                    "steps": run.steps,
                    "total_tokens": run.total_tokens,
                    "cost_usd": run.cost_usd,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                },
                "events": payloads,
                "truncated": len(selected) > len(events),
            },
            truncated=len(selected) > len(events),
        )


def _invalid(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


__all__ = ["TraceReadArgs", "TraceReadTool"]
