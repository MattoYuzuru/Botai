"""Shared plumbing of the v4 agent acceptance bench (lane E: independent verification).

This module is deliberately boring and explicit: it builds the real v4 wiring the way the
`AGENT` job handler does, supplies a test-only owner channel for the Keychain credentials,
turns the run into measurable facts and writes sanitized evidence. Nothing here changes product
code, and nothing here prints a secret value.

Security rules implemented here (AGENTS.md §секреты, docs/v4/04-benchmark.md §2, D-008):

* values of primary secrets are read from the macOS Keychain in memory only, registered in the
  benchmark `Redactor`, and returned to the runtime's `ask` channel; the model receives only a
  `sec_…` handle issued by the product `SecretBroker`;
* before any file leaves for the repository, the summary is swept for registered values and
  e-mail shapes; raw files are swept in place (exact-value redaction) and only then counted;
* raw artifacts stay under ``~/.local/state/botai-v4-bench/`` (0700/0600, outside the repo).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
BACKEND_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"
STATE_ROOT = Path(os.environ.get("BOTAI_BENCH_STATE", "~/.local/state/botai-v4-bench")).expanduser()
EVIDENCE_ROOT = REPO_ROOT / "docs" / "v4" / "_evidence"
EVIDENCE_DATE_DIR = "2026-10-31-v4-agent"

#: broker secret name -> Keychain service is owned by `bench.live.secret_broker`.
SECRET_NAMES = {"mail": "hse_mail", "password": "hse_password"}

_URL = re.compile(r"https?://[^\s\"'<>\\)\]]+")
_TIME_LIKE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
_DAY_WORDS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_JSON_MAX_BYTES = 8_000_000


# --- bootstrap -----------------------------------------------------------------------------------


def bootstrap_imports() -> None:
    """Pin the repository `.env` and make `botai`/`tests` importable for the bench process."""
    os.environ.setdefault("BOTAI_ENV_FILE", str(REPO_ROOT / ".env"))
    for entry in (str(BACKEND_DIR), str(REPO_ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def ensure_backend_python() -> None:
    """Re-exec with the backend virtualenv when running under a Python without the project deps."""
    try:
        import botai  # noqa: F401
        import playwright  # noqa: F401
    except ImportError:
        if BACKEND_PYTHON.exists() and Path(sys.executable).resolve() != BACKEND_PYTHON.resolve():
            os.execv(
                str(BACKEND_PYTHON),
                [str(BACKEND_PYTHON), "-m", "bench.v4.run_agent", *sys.argv[1:]],
            )
        raise SystemExit(
            "нужен backend/.venv (Playwright + botai): выполните `cd backend && uv sync`"
        ) from None


def load_settings(*, provider: str = "deepseek") -> tuple[Any, list[str]]:
    """Real product settings from the repository `.env`; problems are reported without values.

    The provider is chosen explicitly by the bench (`deepseek` for the live L3): the `.env`
    default is `mock`, which is correct for tests but would make an L3 run prove nothing about
    the model. Everything else (key, base URL, models, limits) comes from the `.env`/settings.
    """
    bootstrap_imports()
    from botai.settings import Settings

    settings = Settings(env="dev", agent_engine="v4", llm_provider=provider)
    problems: list[str] = []
    if settings.llm_provider != "deepseek":
        problems.append(f"LLM_PROVIDER={settings.llm_provider!r}: живой прогон требует deepseek")
    if not settings.deepseek_api_key.get_secret_value():
        problems.append("DEEPSEEK_API_KEY пуст в .env — живой прогон модели невозможен")
    return settings, problems


def provider_model_name(settings: Any) -> str:
    """Which model the `agent` purpose profile resolves to (no key involved)."""
    from botai.agent.llm import load_profiles

    profile = load_profiles(settings).get("agent")
    return profile.model if profile is not None else ""


def _offline_db_sessions() -> Any:
    """A session factory that refuses to open a session.

    The current `build_agent_wiring` refuses to assemble `source_*`/`rag_*` tools without a DB
    session factory. The acceptance scenarios here exercise browser/secrets/files (no DB tools),
    and this bench must never write the shared dev database — so the factory is present but
    refuses connections. A model call to `source_*`/`rag_*` becomes an honest tool error instead
    of a silent write, and the evidence records that DB tools were not exercised.
    """
    raise RuntimeError(
        "bench lane E: DB-инструменты (source_*/rag_*) в этом сценарии не используются; "
        "фабрика сессий намеренно отключена, чтобы не писать в общую dev-БД"
    )


def build_services(settings: Any, *, browser: Any = None) -> Any:
    """A minimal `Services` stand-in: no live DB, in-memory vault/queue, system clock."""
    from types import SimpleNamespace

    from botai.core.clock import SystemClock
    from botai.core.queue import InMemoryJobQueue
    from botai.core.vault import InMemoryVault

    return SimpleNamespace(
        settings=settings,
        vault=InMemoryVault(),
        clock=SystemClock(),
        browser=browser,
        db=_offline_db_sessions,
        queue=InMemoryJobQueue(),
        embedder=None,
        search=None,
        llm=None,
        sessions=None,
    )


# --- test-only owner channel (mirrors the runtime ask channel) ----------------------------------


class KeychainAskChannel:
    """The owner, simulated: answers `secret_request` from the Keychain, everything else canned.

    The runtime calls this callback from `ask_user` and `secret_request`. A secret answer is
    returned as the `AskAnswer.text` the product expects — the `SecretBroker` turns it into a
    Vault record + handle, so the model never sees the value. Values are registered in the
    benchmark `Redactor` and dropped immediately; only the *kind* of the answer is recorded.
    """

    def __init__(self, redactor: Any, *, max_secret_deliveries: int = 6) -> None:
        self._redactor = redactor
        self._max = max_secret_deliveries
        self._counts: dict[str, int] = {}
        self._secret_answers = 0
        self.questions: list[dict[str, Any]] = []
        self.cancelled = 0

    async def __call__(self, question: Any) -> Any:
        from botai.agent.v4.protocol import AskAnswer

        self.questions.append(
            {
                "inputKind": question.input_kind,
                "question": self._redactor.redact(question.question)[:240],
                "placeholder": self._redactor.redact(question.placeholder or "")[:120] or None,
                "options": [self._redactor.redact(item)[:120] for item in question.options][:8],
            }
        )
        if question.input_kind == "secret":
            if self._secret_answers >= self._max:
                self.cancelled += 1
                return AskAnswer(cancelled=True)
            kind = self._classify(question)
            value = await asyncio.to_thread(self._fetch, SECRET_NAMES[kind])
            self._redactor.register(value)
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._secret_answers += 1
            return AskAnswer(text=value)
        if question.input_kind == "choice" and question.options:
            return AskAnswer(choice=0)
        text = f"{question.question} {question.placeholder or ''}".lower()
        if any(
            hint in text
            for hint in (
                "парол",
                "password",
                "логин",
                "login",
                "почт",
                "mail",
                "e-mail",
                "email",
                "учёт",
                "учет",
                "username",
                "кред",
            )
        ):
            # A real owner does not type credentials into the chat: policy is the protected input
            # (D-008, plan 0.5.7). The nudge keeps the flow on the product secret path instead of
            # letting the model fabricate a username from an unanswerable plain-text question.
            return AskAnswer(
                text=(
                    "Креды текстом не присылаю: запроси логин и пароль через secret_request "
                    "(защищённый ввод), затем подставь их через secret_use. Значения модели не "
                    "показываются; текст сообщений не должен их содержать."
                )
            )
        return AskAnswer(text="Действуй по плану задачи.")

    def _classify(self, question: Any) -> str:
        text = f"{question.question} {question.placeholder or ''}".lower()
        if any(hint in text for hint in ("парол", "password", "passwd", "pwd")):
            return "password"
        if any(
            hint in text
            for hint in (
                "почт",
                "mail",
                "логин",
                "login",
                "username",
                "e-mail",
                "email",
                "учёт",
                "учет",
            )
        ):
            return "mail"
        return "mail" if not self._counts.get("mail") else "password"

    def _fetch(self, name: str) -> str:
        from bench.live.secret_broker import get_secret

        return get_secret(name)

    def summary(self) -> dict[str, Any]:
        """Facts about secret handling: kinds and counts only, never values."""
        return {
            "secretAnswers": self._secret_answers,
            "cancelled": self.cancelled,
            "deliveries": [
                {"kind": kind, "count": count} for kind, count in sorted(self._counts.items())
            ],
            "questions": self.questions,
        }


# --- limits and product facts --------------------------------------------------------------------


def clamped_limits(settings: Any, *, max_steps: int, max_seconds: float, max_usd: float) -> Any:
    """Owner limits from settings, further clamped by the bench budget (never widened)."""
    from botai.agent.v4.factory import run_limits_from_settings
    from botai.agent.v4.runtime import RunLimits

    base = run_limits_from_settings(settings)

    def pick(base_value: Any, cap: Any) -> Any:
        candidates = [value for value in (base_value, cap) if value is not None]
        return min(candidates) if candidates else None

    return RunLimits(
        max_steps=pick(base.max_steps, max_steps),
        max_seconds=pick(base.max_seconds, max_seconds),
        max_tokens=base.max_tokens,
        max_usd=pick(base.max_usd, max_usd),
        hard_stop_usd=pick(base.hard_stop_usd, max_usd),
        auto_continue=False,
        max_calls_per_step=base.max_calls_per_step,
        max_parallel_tools=base.max_parallel_tools,
        tool_result_chars=base.tool_result_chars,
    )


def git_head_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def host_facts() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


# --- run -> measurements -------------------------------------------------------------------------


def tool_sequence(result: Any, redactor: Any) -> list[dict[str, Any]]:
    """Tool calls in execution order with ok/error/duration — the agent's real step list."""
    pending: dict[str, dict[str, Any]] = {}
    sequence: list[dict[str, Any]] = []
    for event in result.trace.events:
        payload = event.payload or {}
        if event.kind == "tool_call":
            call_id = str(payload.get("call_id") or "")
            item: dict[str, Any] = {
                "tool": payload.get("tool"),
                "ok": None,
                "error": None,
                "durationMs": None,
                "arguments": sanitize_arguments(payload.get("arguments"), redactor),
            }
            pending[call_id] = item
            sequence.append(item)
        elif event.kind == "tool_result":
            call_id = str(payload.get("call_id") or "")
            item = pending.get(call_id)
            if item is None:
                continue
            item["ok"] = payload.get("ok")
            item["error"] = payload.get("error")
            item["durationMs"] = payload.get("duration_ms")
            summary = scrub_url_queries(redactor.redact(str(payload.get("summary") or "")))[:200]
            item["summary"] = summary or None
    return sequence


def sanitize_arguments(arguments: Any, redactor: Any) -> dict[str, Any]:
    """Short, secret-free view of tool arguments kept for evidence."""
    if not isinstance(arguments, dict):
        return {}
    from bench.live.redact import endpoint_template

    view: dict[str, Any] = {}
    for key, value in arguments.items():
        if key in {"url", "entry_url"} and isinstance(value, str):
            view[key] = endpoint_template(value)
        elif key in {"code", "expression", "content", "text"}:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            view[f"{key}Chars"] = len(text)
        elif isinstance(value, dict | list):
            encoded = json.dumps(value, ensure_ascii=False, default=str)
            view[key] = redactor.redact(encoded)[:200]
        else:
            view[key] = redactor.redact(str(value))[:200] if value is not None else None
    return view


def observed_endpoints(trace: Any, redactor: Any, *, limit: int = 60) -> list[str]:
    """Host+path of every URL seen in the trace (query values and ids removed)."""
    from bench.live.redact import endpoint_template

    encoded = json.dumps([event.payload for event in trace.events], ensure_ascii=False, default=str)
    endpoints: set[str] = set()
    for match in _URL.finditer(encoded):
        cleaned = endpoint_template(redactor.redact(match.group(0)))
        if cleaned and len(cleaned) <= 300:
            endpoints.add(cleaned)
        if len(endpoints) >= limit * 4:
            break
    return sorted(endpoints)[:limit]


def workspace_inventory(root: Path, redactor: Any) -> list[dict[str, Any]]:
    """Facts about the files the agent produced — shapes and counts, never content."""
    if not root.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith("."):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        item: dict[str, Any] = {
            "path": redactor.redact(relative)[:200],
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        text = data.decode("utf-8", errors="replace")
        item["containsEmail"] = bool(re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}", text))
        item.update(_describe_content(text))
        files.append(item)
        if len(files) >= 60:
            break
    return files


def _describe_content(text: str) -> dict[str, Any]:
    """JSON/text shape without any value: type, record count, key names."""
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            shape: dict[str, Any] = {"format": "json", "jsonType": type(parsed).__name__}
            if isinstance(parsed, list):
                shape["records"] = len(parsed)
                if parsed and isinstance(parsed[0], dict):
                    shape["keys"] = sorted(str(key) for key in parsed[0])[:15]
            elif isinstance(parsed, dict):
                shape["keys"] = sorted(str(key) for key in parsed)[:15]
                lists = [value for value in parsed.values() if isinstance(value, list)]
                if lists:
                    shape["records"] = max(len(item) for item in lists)
            schedule_keys = {
                "begin_time",
                "end_time",
                "lesson_type",
                "week_begin",
                "lessons",
                "lessons_count",
                "schedule_events",
                "timetable",
            }
            keys_here = set(shape.get("keys") or [])
            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                keys_here |= {str(key) for key in parsed[0]}
            shape["scheduleLike"] = bool(keys_here & schedule_keys) or _looks_like_schedule(
                json.dumps(parsed, ensure_ascii=False)
            )
            return shape
    lines = [line for line in stripped.splitlines() if line.strip()]
    return {
        "format": "text",
        "lines": len(lines),
        "scheduleLike": _looks_like_schedule(stripped),
    }


def _looks_like_schedule(text: str) -> bool:
    lowered = text.lower()
    return _TIME_LIKE.search(text) is not None and any(word in lowered for word in _DAY_WORDS)


def scrub_url_queries(text: str) -> str:
    """Replace every URL in `text` with host+path: query values are session/one-time material."""
    from bench.live.redact import endpoint_template

    return _URL.sub(lambda match: endpoint_template(match.group(0)), text)


def secret_leaks(text: str, redactor: Any) -> bool:
    return redactor.contains_secret(text)


def secret_sweep(root: Path, redactor: Any) -> dict[str, Any]:
    """Exact-value redaction over every text artifact, mirroring `bench.live.run`."""
    scanned = 0
    redacted: list[str] = []
    if not root.is_dir():
        return {"filesScanned": 0, "filesRedacted": 0, "redactedFiles": []}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > _JSON_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        if redactor.contains_secret(text):
            path.write_text(redactor.redact(text), encoding="utf-8")
            path.chmod(0o600)
            redacted.append(path.relative_to(root).as_posix())
    return {
        "filesScanned": scanned,
        "filesRedacted": len(redacted),
        "redactedFiles": redacted,
    }


def sanitize_for_repository(value: Any, redactor: Any) -> Any:
    """A last gate before anything is written under the public `docs/` tree."""
    if isinstance(value, str):
        text = scrub_url_queries(redactor.redact(value))
        return text.replace(str(Path.home()), "~").replace(str(REPO_ROOT), "<repo>")
    if isinstance(value, list):
        return [sanitize_for_repository(item, redactor) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_for_repository(item, redactor) for key, item in value.items()}
    return value


# --- evidence ------------------------------------------------------------------------------------


def evidence_dir() -> Path:
    path = EVIDENCE_ROOT / EVIDENCE_DATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_evidence(summary: dict[str, Any], redactor: Any) -> Path:
    """Write one sanitized run summary and regenerate the RU evidence README."""
    directory = evidence_dir()
    safe = sanitize_for_repository(summary, redactor)
    encoded = json.dumps(safe, ensure_ascii=False, indent=2, default=str)
    if redactor.contains_secret(encoded):  # pragma: no cover - defence in depth
        raise SystemExit("refusing to write evidence: a registered secret survived sanitization")
    path = directory / f"{safe['scenario']}-{safe['runId']}.json"
    path.write_text(encoded + "\n", encoding="utf-8")
    _write_readme(directory)
    return path


def _write_readme(directory: Path) -> None:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    rows.sort(key=lambda row: str(row.get("startedAt") or ""))
    lines = [
        "# Приёмка v4-агента: L2/L3 из независимой проверки (lane E)",
        "",
        "Санитизированные сводки прогонов **настоящего** v4-harness "
        "(`botai.agent.v4.factory.build_agent_wiring` — тот же wiring, что у job `AGENT`), "
        "а не детерминированного `bench/live`. Скрипт: "
        "[`bench/v4/run_agent.py`](../../../../bench/v4/run_agent.py). "
        "Аудит фазы 0.5 (0.5.4–0.5.7 + prod-readiness для `BOTAI_AGENT_ENGINE=v4`) — "
        "[phase-0.5-audit.md](phase-0.5-audit.md).",
        "",
        "| Сценарий | Статус | Время | Шаги | Инструменты | Токены | $ | Барьер |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        metrics = row.get("metrics") or {}
        barrier = row.get("barrier") or {}
        if row.get("outcome") == "success":
            verdict = "успех"
        elif row.get("status") == "answer":
            verdict = "answer, не принято"
        else:
            verdict = str(row.get("outcome") or "?")
        lines.append(
            "| {scenario} | {status} | {secs} с | {steps} | {tools} | {tokens} | {cost} | {barrier} |".format(
                scenario=row.get("scenario"),
                status=verdict,
                secs=row.get("durationSec"),
                steps=metrics.get("steps"),
                tools=metrics.get("toolCalls"),
                tokens=metrics.get("totalTokens"),
                cost=metrics.get("costUsd"),
                barrier=barrier.get("status") or barrier.get("errorCode") or "",
            )
        )
    live_rows = [row for row in rows if str(row.get("scenario", "")).startswith("live")]
    l2_rows = [row for row in rows if str(row.get("scenario", "")).startswith("l2")]
    successes = [row for row in rows if row.get("outcome") == "success"]
    partials = [row for row in rows if row.get("outcome") == "partial"]
    barriers = [
        row for row in rows if row.get("outcome") == "barrier" and row.get("status") != "answer"
    ]
    functional = [
        row for row in rows if row.get("outcome") != "success" and row.get("status") == "answer"
    ]
    live_spend = round(
        sum(float((row.get("metrics") or {}).get("costUsd") or 0.0) for row in live_rows),
        6,
    )
    lines += [
        "",
        "**Итог приёмки:** успешных прогонов — {ok}; partial — {part}; функционально `answer`, "
        "но НЕ принято (security gate) — {func}; барьеров — {bad}. "
        "L2 (mock, $0): {l2}; L3 живой вуз: {live}. Суммарная стоимость живых L3-прогонов: "
        "${spend} (реальные деньги, по данным runtime).".format(
            ok=len(successes),
            part=len(partials),
            func=len(functional),
            bad=len(barriers),
            spend=live_spend,
            l2=(
                f"{sum(1 for row in l2_rows if row.get('outcome') == 'success')}/{len(l2_rows)} успешно"
                if l2_rows
                else "нет"
            ),
            live=(
                f"{sum(1 for row in live_rows if row.get('outcome') == 'success')} success, "
                f"{sum(1 for row in live_rows if row.get('outcome') == 'partial')} partial, "
                f"{sum(1 for row in live_rows if row.get('outcome') != 'success' and row.get('status') == 'answer')} "
                "answer-не-принят, "
                f"{sum(1 for row in live_rows if row.get('outcome') == 'barrier' and row.get('status') != 'answer')} "
                f"barrier из {len(live_rows)}"
                if live_rows
                else "нет"
            ),
        ),
        "",
        "Полные детали прогона — в JSON-файлах рядом (статус, последовательность инструментов, "
        "факты о workspace, проверки секретов). Текст ответа агента, трасса, сообщения и "
        "содержимое скачанных данных в репозиторий **не попадают**: они лежат вне его, в "
        "`~/.local/state/botai-v4-bench/<дата>/<запуск>/raw/v4-agent/` (0700).",
        "",
        "## Что доказано / что нет",
        "",
        "Смотрите `proven`/`notProven` в каждом JSON и раздел ниже. Базовое правило: "
        "успех = `status=answer` **и** артефакт в workspace **и** проверка секретов чистая; "
        "живой прогон одного SHA не доказывает качество агента в целом (D-010).",
        "",
        "Барьер `step_limit` — это исход бюджета шагов, а не отказ инструментов: "
        "цепочка не завершена за отведённые шаги, данные могли быть уже получены. "
        "Для приёмки он значит «не доказано», даже если часть шагов прошла успешно. "
        "Запуски с `workaround` (обход известного бага) помечены отдельно и "
        "**не являются** product-path приёмкой.",
        "",
        "`partial` — данные получены и сохранены в workspace, но финальный ответ не дан "
        "(обычно шаговый бюджет). Для приёмки это **не** успех: успех требует `status=answer`.",
        "",
        "",
    ]
    for row in rows:
        lines.append(f"### {row.get('scenario')} · `{row.get('runId')}`")
        lines.append("")
        lines.append(f"- Когда: {row.get('startedAt')} (HEAD `{row.get('headSha')}`)")
        if row.get("outcome") == "success":
            verdict_line = "успех"
        elif row.get("status") == "answer":
            verdict_line = (
                "функционально `answer` (ответ и данные получены), но приёмка НЕ засчитана: "
                "security gate (см. ниже)"
            )
        else:
            verdict_line = f"барьер `{row.get('status')}`"
        lines.append("- Итог: " + verdict_line)
        code_state = row.get("codeState") or {}
        if code_state.get("note"):
            lines.append(f"- Состояние кода: {code_state['note']}")
        gate = row.get("securityGate") or {}
        if gate and not gate.get("passed"):
            lines.append(
                f"- Security gate: **НЕ пройден** — {gate.get('cause')} "
                f"(raw sweep переписал {gate.get('rawFilesRedacted')} файл(ов))"
            )
        workaround = row.get("workaround")
        if workaround:
            lines.append(
                f"- **Workaround, не product-path**: {workaround.get('name')} — {workaround.get('why')}"
            )

        for item in row.get("proven") or []:
            lines.append(f"- Доказано: {item}")
        for item in row.get("notProven") or []:
            lines.append(f"- Не доказано: {item}")
        lines.append("")
    lines += [
        "## Как воспроизвести",
        "",
        "```bash",
        "cd /Users/yuzuru/Projects/Botai",
        "backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse   # реальные креды+модель",
        "backend/.venv/bin/python -m bench.v4.run_agent --scenario l2-fakeuni # mock-модель, $0",
        "```",
        "",
        "Требования: `.env` с `LLM_PROVIDER=deepseek` и ключом; Keychain "
        "`botai.live.hse.mail`/`botai.live.hse.password` (account `botai-v4-bench`); "
        "Playwright Chromium в `backend/.venv`. Значения кредов не печатаются.",
        "",
        "## Границы доказательства",
        "",
        "- Один владелец, один день, один прогон на сценарий: это приёмка конкретного SHA, "
        "а не статистика качества модели.",
        "- L3-успех зависит от живого вуза (вёрстка, SSO, антибот); барьер — тоже честный "
        "результат и он записан точно (статус/ошибка/инструмент), без догадок.",
        "- Сырые артефакты (трасса, сообщения, workspace агента) не коммитятся и не являются "
        "доказательством для CI.",
        "",
    ]
    (directory / "README.md").write_text("\n".join(lines), encoding="utf-8")
