"""Network, console and DOM evidence for the live benchmark.

The benchmark keeps a compact HAR (Playwright recorder, bodies omitted), a network log with
API endpoint discovery, console lines and DOM counters. Everything written here passes through
the :class:`bench.live.redact.Redactor`, and raw session material stays outside the repository.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import Redactor, endpoint_template

_JSON_MIME = ("application/json", "text/json", "application/graphql")
_TIME_LIKE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
_DATE_LIKE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")

#: Keys that usually mark one record of a schedule / task collection.
RECORD_KEYS = re.compile(
    r"(?i)(id|name|title|date|start|end|time|deadline|due|state|status|subject|discipline|"
    r"auditor|room|task|event|lesson|course|exercise|score|mark)"
)


@dataclass(slots=True)
class NetEntry:
    method: str
    url: str
    status: int
    resource_type: str
    mime: str
    ok: bool
    size: int | None = None

    def as_dict(self, redactor: Redactor) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": _redacted_url(self.url, redactor),
            "status": self.status,
            "resourceType": self.resource_type,
            "mime": self.mime,
            "ok": self.ok,
            "size": self.size,
        }


def _redacted_url(url: str, redactor: Redactor) -> str:
    from .redact import clean_url

    return clean_url(url, redactor=redactor)


class NetworkRecorder:
    """Collects responses from one page and can count entities in JSON bodies."""

    def __init__(self, redactor: Redactor, *, max_entries: int = 4000, max_bodies: int = 120):
        self.redactor = redactor
        self.max_entries = max_entries
        self.max_bodies = max_bodies
        self._entries: list[NetEntry] = []
        self._responses: list[Any] = []
        self._bodies: list[dict[str, Any]] = []

    # --- playwright handlers --------------------------------------------------------------

    def attach(self, page: Any) -> None:
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)

    def _on_response(self, response: Any) -> None:
        if len(self._entries) >= self.max_entries:
            return
        try:
            request = response.request
            entry = NetEntry(
                method=request.method,
                url=response.url,
                status=int(response.status),
                resource_type=request.resource_type,
                mime=str((response.headers or {}).get("content-type", "")),
                ok=bool(response.ok),
                size=_safe_size(response),
            )
        except Exception:
            return
        self._entries.append(entry)
        if (
            entry.resource_type in {"xhr", "fetch"}
            and any(marker in entry.mime for marker in _JSON_MIME)
            and len(self._responses) < self.max_bodies
        ):
            self._responses.append(response)

    def _on_failed(self, request: Any) -> None:
        if len(self._entries) >= self.max_entries:
            return
        try:
            failure = request.failure or "failed"
            self._entries.append(
                NetEntry(
                    method=request.method,
                    url=request.url,
                    status=0,
                    resource_type=request.resource_type,
                    mime="",
                    ok=False,
                    size=None,
                )
            )
            self._entries[-1].failure = str(failure)  # type: ignore[attr-defined]
        except Exception:
            return

    # --- analysis -------------------------------------------------------------------------

    @property
    def entries(self) -> list[NetEntry]:
        return list(self._entries)

    def api_endpoints(self) -> list[dict[str, Any]]:
        """Unique API endpoints seen in traffic (host+path only, no query values)."""
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self._entries:
            url = entry.url
            if not ("/api/" in url or "/graphql" in url or entry.resource_type in {"xhr", "fetch"}):
                continue
            if any(
                part in url
                for part in (
                    "yandex.ru/metrika",
                    "mc.yandex",
                    "google-analytics",
                    "top-fwz1.mail.ru",
                    "sentry",
                    "googletagmanager",
                )
            ):
                continue
            key = (entry.method, endpoint_template(url))
            bucket = buckets.setdefault(
                key,
                {
                    "method": entry.method,
                    "endpoint": endpoint_template(url),
                    "calls": 0,
                    "statuses": [],
                    "json": False,
                    "resourceType": entry.resource_type,
                },
            )
            bucket["calls"] += 1
            if entry.status and entry.status not in bucket["statuses"]:
                bucket["statuses"].append(entry.status)
            if any(marker in entry.mime for marker in _JSON_MIME):
                bucket["json"] = True
        result = sorted(buckets.values(), key=lambda item: (item["endpoint"], item["method"]))
        for item in result:
            item["endpoint"] = self.redactor.redact(str(item["endpoint"]))
        return result

    def json_payloads(self, *, url_contains: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Read stored JSON responses (called after the page settled)."""
        payloads: list[dict[str, Any]] = []
        for response in self._responses:
            try:
                url = response.url
                if url_contains and not any(marker in url for marker in url_contains):
                    continue
                data = response.json()
            except Exception:
                continue
            payloads.append(
                {
                    "endpoint": endpoint_template(url),
                    "payload": data,
                }
            )
        return payloads

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [entry.as_dict(self.redactor) for entry in self._entries],
            "apiEndpoints": self.api_endpoints(),
            "endpointCount": len(self.api_endpoints()),
            "responseCount": len(self._entries),
        }
        path.write_text(
            json.dumps(self.redactor.redact_payload(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        path.chmod(0o600)


class ConsoleRecorder:
    """Console and page errors, redacted before they touch a file."""

    def __init__(self, redactor: Redactor, *, max_lines: int = 500):
        self.redactor = redactor
        self.max_lines = max_lines
        self._lines: list[dict[str, str]] = []

    def attach(self, page: Any) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    def _on_console(self, message: Any) -> None:
        if len(self._lines) >= self.max_lines:
            return
        try:
            self._lines.append(
                {
                    "type": str(message.type),
                    "text": self.redactor.redact(str(message.text))[:1200],
                }
            )
        except Exception:
            pass

    def _on_page_error(self, error: Any) -> None:
        if len(self._lines) >= self.max_lines:
            return
        self._lines.append({"type": "pageerror", "text": self.redactor.redact(str(error))[:1200]})

    @property
    def lines(self) -> list[dict[str, str]]:
        return list(self._lines)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"lines": self._lines, "count": len(self._lines)}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)


# --- entity counting ------------------------------------------------------------------------------


def _safe_size(response: Any) -> int | None:
    try:
        sizes = response.request.sizes()
        return int(sizes.get("responseBodySize", 0)) or None
    except Exception:
        return None


def iter_list_of_dicts(
    payload: Any, *, max_records: int = 5000, max_depth: int = 8
) -> list[dict[str, Any]]:
    """Collect dict records that appear as elements of lists anywhere in a JSON payload."""
    records: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth or len(records) >= max_records:
            return
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    records.append(item)
                    walk(item, depth + 1)
                elif isinstance(item, list):
                    walk(item, depth + 1)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)

    walk(payload, 0)
    return records


def count_records(payload: Any, *, key_pattern: re.Pattern[str] = RECORD_KEYS) -> dict[str, Any]:
    """Count record-like objects in an unknown JSON payload (schedule, tasks, ...)."""
    records = iter_list_of_dicts(payload)
    with_hint = sum(1 for record in records if any(key_pattern.search(str(key)) for key in record))
    keys: dict[str, int] = {}
    for record in records[:200]:
        for key in record:
            keys[str(key)] = keys.get(str(key), 0) + 1
    return {
        "listRecords": len(records),
        "recordLikeRecords": with_hint,
        "topKeys": sorted(keys.items(), key=lambda item: item[1], reverse=True)[:15],
    }


def payload_shape(payload: Any, *, max_keys: int = 20) -> dict[str, Any]:
    """Structural summary of a JSON body: types, key names, list lengths. No values."""
    shape: dict[str, Any] = {"type": type(payload).__name__}
    if isinstance(payload, dict):
        shape["keys"] = sorted(str(key) for key in payload)[:max_keys]
        lists: dict[str, int] = {
            str(key): len(value)
            for key, value in payload.items()
            if isinstance(value, list) and len(value) <= 10_000
        }
        if lists:
            shape["lists"] = dict(
                sorted(lists.items(), key=lambda item: item[1], reverse=True)[:max_keys]
            )
    elif isinstance(payload, list):
        shape["length"] = len(payload)
        if payload and isinstance(payload[0], dict):
            shape["itemKeys"] = sorted(str(key) for key in payload[0])[:max_keys]
    return shape


# --- page helpers ---------------------------------------------------------------------------------


def settle(page: Any, milliseconds: int = 1200) -> None:
    """Wait for late XHRs of an SPA; nothing here depends on a specific site."""
    try:
        page.wait_for_load_state("networkidle", timeout=min(15_000, milliseconds + 10_000))
    except Exception:
        pass
    page.wait_for_timeout(milliseconds)


def dom_counts(page: Any) -> dict[str, Any]:
    """Generic DOM counters: rows, links, buttons, time-like and date-like text."""
    try:
        return page.evaluate(
            """() => {
                const text = document.body ? document.body.innerText : '';
                const count = (sel) => document.querySelectorAll(sel).length;
                return {
                    bodyTextLength: text.length,
                    anchors: count('a'),
                    buttons: count('button'),
                    formFields: count('input, select, textarea'),
                    tableRows: count('tr'),
                    listItems: count('li'),
                    headings: count('h1, h2, h3'),
                    timeLike: (text.match(/\\b\\d{1,2}[:.]\\d{2}\\b/g) || []).length,
                    dateLike: (text.match(/\\b\\d{2}\\.\\d{2}\\.\\d{4}\\b/g) || []).length,
                };
            }"""
        )
    except Exception:
        return {}


def time_like_count(text: str) -> int:
    return len(_TIME_LIKE.findall(text))


def date_like_count(text: str) -> int:
    return len(_DATE_LIKE.findall(text))


def save_screenshot(page: Any, path: Path, *, full_page: bool = False) -> bool:
    """Save a screenshot into the private run directory."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=full_page, animations="disabled")
        path.chmod(0o600)
        return True
    except Exception:
        return False


def page_text_excerpt(page: Any, limit: int = 300) -> str:
    """Small redactable excerpt for the raw run log (kept in the private directory)."""
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""
    return str(text)[:limit]


def form_fields(page: Any) -> list[dict[str, str]]:
    """Field names/types of the forms on a page (values are never read)."""
    try:
        return list(
            page.evaluate(
                """() => Array.from(document.querySelectorAll('form input, form select, form textarea'))
                    .slice(0, 40)
                    .map((el) => ({
                        tag: el.tagName.toLowerCase(),
                        type: (el.getAttribute('type') || '').toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        autocomplete: el.getAttribute('autocomplete') || '',
                    }))"""
            )
        )
    except Exception:
        return []


@dataclass(slots=True)
class ScenarioEvidence:
    """Per-scenario artifact writer: all paths are relative to the run's raw directory."""

    name: str
    raw_dir: Path
    redactor: Redactor
    network: NetworkRecorder = field(default=None)  # type: ignore[assignment]
    console: ConsoleRecorder = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.network is None:
            self.network = NetworkRecorder(self.redactor)
        if self.console is None:
            self.console = ConsoleRecorder(self.redactor)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.chmod(0o700)
