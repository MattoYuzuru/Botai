"""Execution of one validated action: HTTP through the v4 client, browser through the run's control.

The executor is deliberately thin. An HTTP action is sent by the same `HttpRequestTool` the
`http_request` tool uses (one client factory, one timeout/redirect policy, one response shape);
a browser action is a sequence of `BrowserControl` operations on the run's real Chromium. The
executor never writes the journal: it returns a structured outcome, and `engine.py` persists it.

No body and no header/cookie value is part of the outcome that goes to the journal; the journal
gets method/URL/body hash and a response summary (`journal_payload`). The model-facing `data`
echo carries the response facts it needs to continue (status, URL, small text), and is masked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from botai.actions.models import ActionRequest
from botai.actions.redact import mask_text
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.http import HttpRequestTool
from botai.core.errors import BotaiError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover - typing only; the run owns the real browser object
    from botai.browser.control import BrowserControl

ECHO_TEXT_MAX = 2000


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What happened to one action: transport-level success/failure plus a redacted summary."""

    ok: bool
    status: int | None
    summary: str
    detail: str = ""
    error: str | None = None
    retryable: bool = False
    echo: dict[str, Any] = field(default_factory=dict)

    def journal_payload(self) -> dict[str, Any]:
        """What `actions_log.result` keeps: no response body, no request values (D-026)."""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary[:1000],
        }
        for key in ("url", "content_type", "bytes", "body_sha256"):
            value = self.echo.get(key)
            if value is not None:
                payload[key] = value
        if self.error is not None:
            payload["error"] = self.error
        return payload


class ActionExecutor:
    """Runs validated `ActionRequest`s. Stateless except for the injected test transport."""

    def __init__(self, *, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = http_transport

    async def execute(
        self,
        request: ActionRequest,
        *,
        control: BrowserControl | None = None,
        run_id: str = "",
    ) -> ExecutionOutcome:
        if request.transport == "http":
            return await self._execute_http(request, run_id=run_id)
        return await self._execute_browser(request, control)

    # -- HTTP ------------------------------------------------------------------------------------

    async def _execute_http(self, request: ActionRequest, *, run_id: str) -> ExecutionOutcome:
        if request.url is None or request.method is None:
            return ExecutionOutcome(
                ok=False, status=None, summary="", error="invalid_request", retryable=False
            )
        tool = HttpRequestTool(transport=self._transport)
        result = await tool.run(
            {
                "method": request.method,
                "url": request.url,
                "headers": request.headers,
                "body": request.body,
                "body_kind": request.body_kind,
                "cookies": request.cookies,
                "follow_redirects": request.follow_redirects,
                "timeout_s": request.timeout_s,
            },
            ToolContext(run_id=run_id or "action", user_id=None),
        )
        if not result.ok:
            error = result.error
            return ExecutionOutcome(
                ok=False,
                status=None,
                summary=error.detail if error else "",
                error=error.code if error else "action_failed",
                retryable=bool(error and error.retryable),
            )
        data = result.data or {}
        status = data.get("status")
        http_ok = isinstance(status, int) and status < 400
        echo: dict[str, Any] = {
            "status": status,
            "url": data.get("url"),
            "content_type": data.get("content_type"),
            "bytes": data.get("bytes"),
            "body_sha256": data.get("sha256"),
        }
        text = data.get("text")
        if isinstance(text, str):
            echo["text"] = mask_text(text)[:ECHO_TEXT_MAX]
        json_body = data.get("json")
        if json_body is not None:
            echo["json"] = json_body if _json_size(json_body) <= ECHO_TEXT_MAX else None
        summary = str(result.summary or (f"{status}" if status is not None else "ответ получен"))
        failure = "" if http_ok else f"Сайт ответил {status}: действие не принято."
        return ExecutionOutcome(
            ok=http_ok,
            status=status if isinstance(status, int) else None,
            summary=summary,
            detail=failure,
            error=None if http_ok else f"http_{status}",
            retryable=False,
            echo=echo,
        )

    # -- browser ---------------------------------------------------------------------------------

    async def _execute_browser(
        self, request: ActionRequest, control: BrowserControl | None
    ) -> ExecutionOutcome:
        if control is None:
            return ExecutionOutcome(
                ok=False,
                status=None,
                summary="",
                error="browser_unavailable",
                retryable=False,
            )
        info: Any = None
        executed = 0
        try:
            for step in request.steps:
                if step.action == "navigate" and step.url:
                    info = await control.navigate(step.url, timeout_s=request.timeout_s)
                elif step.action == "click" and step.target:
                    info = await control.click(step.target, timeout_s=request.timeout_s)
                elif step.action == "type" and step.target and step.text is not None:
                    info = await control.type(
                        step.target, step.text, clear=step.clear, timeout_s=request.timeout_s
                    )
                elif step.action == "press" and step.key:
                    info = await control.press(step.target, step.key, timeout_s=request.timeout_s)
                elif step.action == "select" and step.target:
                    info = await control.select(
                        step.target,
                        value=step.value,
                        label=step.label,
                        index=step.index,
                        timeout_s=request.timeout_s,
                    )
                else:  # pragma: no cover - ActionRequest validates the step shapes
                    raise BotaiError(ErrorCode.VALIDATION, "Шаг браузера неполный")
                executed += 1
        except BotaiError as exc:
            return ExecutionOutcome(
                ok=False,
                status=None,
                summary=exc.detail or "Браузер не выполнил действие.",
                error=exc.code.value,
                retryable=False,
                echo={"steps": executed, **_page_facts(info)},
            )
        except Exception:
            return ExecutionOutcome(
                ok=False,
                status=None,
                summary="Браузер не выполнил действие.",
                error="browser_failed",
                retryable=False,
                echo={"steps": executed, **_page_facts(info)},
            )
        facts = _page_facts(info)
        return ExecutionOutcome(
            ok=True,
            status=None,
            summary=f"Браузер выполнил шагов: {executed}",
            echo={"steps": executed, **facts},
        )


def _page_facts(info: Any) -> dict[str, Any]:
    if info is None:
        return {}
    facts: dict[str, Any] = {}
    for key in ("url", "title"):
        value = getattr(info, key, None)
        if isinstance(value, str):
            facts[key] = value[:1000]
    return facts


def _json_size(value: Any) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, default=str))


__all__ = ["ECHO_TEXT_MAX", "ActionExecutor", "ExecutionOutcome"]
