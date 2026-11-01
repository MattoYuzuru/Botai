"""Project the persisted tool trace into the job's live, secret-free activity stream."""

from __future__ import annotations

from urllib.parse import urlsplit

from botai.agent.v4.traces import RunInfo, RunTrace, TraceEvent, TraceStore
from botai.contracts.agent_ui import ThinkingBlock
from botai.core.ids import new_id
from botai.core.jobs import JobContext

TOOL_LABELS = {
    "http_request": "Читаю сайт",
    "browser_open": "Открываю страницу",
    "browser_navigate": "Открываю страницу",
    "browser_read": "Читаю страницу",
    "browser_eval": "Проверяю данные страницы",
    "browser_click": "Перехожу по странице",
    "browser_type": "Заполняю поле",
    "browser_network": "Изучаю запросы сайта",
    "browser_cookies": "Проверяю сессию",
    "browser_session": "Сохраняю сессию",
    "secret_request": "Жду защищённый ввод",
    "secret_use": "Выполняю вход",
    "ask_user": "Уточняю у вас",
    "space_create": "Создаю вуз",
    "source_list": "Проверяю подключённые источники",
    "source_create": "Создаю источник",
    "source_schema": "Проверяю формат импорта",
    "source_session": "Настраиваю сессию источника",
    "source_probe": "Проверяю чтение источника",
    "source_read_live": "Читаю источник",
    "source_update_playbook": "Проверяю и сохраняю обработчик",
    "source_sync": "Сохраняю учебные данные",
    "source_repair": "Исправляю обработчик источника",
    "rag_search": "Ищу в материалах",
    "rag_ingest": "Индексирую материалы",
    "code_exec": "Выполняю код",
    "fs_read": "Читаю файл",
    "fs_write": "Сохраняю файл",
    "fs_list": "Проверяю файлы",
    "trace_read": "Изучаю предыдущие действия",
}


class JobTraceStore:
    """Keep the canonical trace and show each actual tool operation, including failures.

    Only static labels and a URL's hostname reach the summary. Tool arguments, response bodies,
    raw URLs, and model reasoning stay in the trace, never in notification/activity text.
    """

    def __init__(self, inner: TraceStore, ctx: JobContext) -> None:
        self._inner = inner
        self._ctx = ctx
        self._blocks: dict[str, ThinkingBlock] = {}

    async def append(self, event: TraceEvent) -> None:
        await self._inner.append(event)
        call_id = event.payload.get("call_id")
        if not isinstance(call_id, str):
            return
        if event.kind == "tool_call":
            tool = str(event.payload.get("tool", ""))
            label = TOOL_LABELS.get(tool, "Выполняю действие")
            arguments = event.payload.get("arguments")
            url = arguments.get("url") if isinstance(arguments, dict) else None
            if isinstance(url, str):
                try:
                    host = urlsplit(url).hostname
                except ValueError:
                    host = None
                if host:
                    label = f"{label}: {host}"
            block = ThinkingBlock(id=new_id(), at=event.at, stage=label[:120])
            self._blocks[call_id] = block
            await self._ctx.emit(block)
        elif event.kind == "tool_result" and call_id in self._blocks:
            block = self._blocks.pop(call_id)
            ok = event.payload.get("ok") is True
            detail = "Выполнено" if ok else "Не получилось; проверяю причину"
            result = event.payload.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            report = data.get("report") if isinstance(data, dict) else None
            counts = report.get("counts") if isinstance(report, dict) else None
            if ok and isinstance(counts, dict):
                count = sum(n for n in counts.values() if isinstance(n, int) and n >= 0)
                detail = f"Получено записей: {count}"
            await self._ctx.emit(
                block.model_copy(update={"state": "ok" if ok else "failed", "detail": detail})
            )

    async def read(self, run_id: str, *, after_seq: int = 0, limit: int = 100) -> list[TraceEvent]:
        return await self._inner.read(run_id, after_seq=after_seq, limit=limit)

    async def get_run(self, run_id: str) -> RunTrace | None:
        return await self._inner.get_run(run_id)

    async def list_runs(self, *, limit: int = 20) -> list[RunInfo]:
        return await self._inner.list_runs(limit=limit)
