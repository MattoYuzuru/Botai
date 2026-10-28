"""Opt-in, bounded metadata traces. No page, message, model or credential contents.

Separate from SSE and its ten-minute retention. A closed field vocabulary is the boundary:
unknown fields and string values are dropped, rather than redacting arbitrary content afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
from collections import Counter, OrderedDict
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from botai.core.errors import ErrorCode
from botai.settings import Settings, get_settings

_IDS = {"job_id", "connection_id", "thread_id", "message_id", "block_id", "request_id"}
_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")
_HEX = re.compile(r"[0-9a-f]{16}\Z")
_EVENT = re.compile(
    r"(?:diagnostics|http|job|dialogue|chat|llm|browser|auth|learn|observe|canary|egress|search)\.[a-z_.]{1,64}\Z"
)
_NUMBERS = set(
    [
        "attempt",
        "up_bytes",
        "down_bytes",
        "attempts",
        "status",
        "expected_status",
        "anonymous_status",
        "elapsed_ms",
        "latency_ms",
        "delay_ms",
        "page_index",
        "round_no",
        "candidate_index",
        "limit",
        "body_bytes",
        "sample_chars",
        "html_chars",
        "text_chars",
        "input_chars",
        "output_chars",
        "observation_chars",
        "rendered_chars",
        "budget_chars",
        "raw_chars",
        "cookie_count",
        "form_count",
        "allow_host_count",
        "host_round",
        "request_count",
        "response_count",
        "page_count",
        "call_count",
        "candidate_count",
        "rejected_count",
        "chosen_count",
        "pending_count",
        "turn_count",
        "entry_url_count",
        "requested_kind_count",
        "absent_kind_count",
        "link_count",
        "rejected_link_count",
        "input_count",
        "output_count",
        "reasoning_count",
        "tool_count",
        "message_count",
        "endpoint_count",
        "lint_count",
        "valid_count",
        "invalid_count",
        "item_count",
        "injection_count",
        "query_count",
        "path_depth",
        "share_chars",
        "unexamined_count",
        "queued_count",
        "sample_missing_count",
        "verified_count",
        "revision",
        "absent_count",
        "body_chars",
        "considered_count",
        "doubt_count",
        "duplicate_count",
        "entry_count",
        "failed_count",
        "filtered_count",
        "missing_count",
        "problem_count",
        "record_count",
        "requested_count",
        "required_count",
        "structural_count",
        "token_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "input_miss",
        "input_hit",
        "output",
        "reasoning",
        "step",
        "tokens",
        "budget_ms",
        "event_count",
    ]
)
_BOOLEANS = set(
    [
        "read_only",
        "limited",
        "json_response",
        "html_response",
        "budget_exhausted",
        "omitted_sensitive",
        "truncated",
        "proxy_failed",
        "blocked_by_client",
        "navigation",
        "timed_out",
        "redirected",
        "captcha_suspected",
        "auth_required",
        "credential_fields",
        "bearer_present",
        "expires_hint_present",
        "storage_restored",
        "force",
        "stored_present",
        "preset_present",
        "form_detected",
        "push_detected",
        "captcha_detected",
        "unknown_detected",
        "hard_block_detected",
        "error_detected",
        "preset_replayed",
        "passed",
        "expired_hint",
        "probe_present",
        "redirect_check_present",
        "egress_blocked",
        "found",
        "has_user_note",
        "intent_changed",
        "rejected_links",
        "login_required",
        "has_sample",
        "body_truncated",
        "repair",
        "confirmed",
        "ok",
        "secret",
        "idempotent",
        "source",
        "has_target",
        "stored",
        "has_error",
        "body_reduced",
    ]
)
_CODES: dict[str, set[str]] = {
    "stop_reason": {
        "navigation_failed",
        "network_log_failed",
        "finished",
        "authentication",
        "empty",
        "step_budget",
        "token_budget",
        "page_budget",
        "request_budget",
        "no_progress",
        "planner_unavailable",
        "invalid_plan",
        "provider_failed",
        "timeout",
    },
    "decision": {"allow", "deny"},
    "method": {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE", "OTHER"},
    "resource_type": {"xhr", "fetch", "document", "other"},
    "capture_from": {"none", "authorizationHeader", "localStorage", "sessionStorage"},
    "kind": {
        "scheduleEvents",
        "deadlines",
        "courses",
        "assignments",
        "grades",
        "people",
        "locations",
        "news",
        "documents",
    },
    "job_kind": {"connect", "learn", "repair", "sync", "chat", "action", "ingest"},
    "phase": {"requested", "final", "request", "response", "parse", "selection"},
    "outcome": {
        "requested",
        "absent",
        "started",
        "accepted",
        "rejected",
        "not_found",
        "failed",
        "ok",
        "empty",
        "budget_exceeded",
        "cancelled",
        "continued",
        "done",
        "error",
        "skipped",
        "queued",
        "retry",
        "answer",
    },
    "reason": {
        "missing_key",
        "invalid_key",
        "invalid_config",
        "invalid_response",
        "credits_exhausted",
        "rate_limited",
        "unavailable",
        "cooldown",
        "deadline",
        "call_budget",
        "cost_budget",
        "unsafe_query",
        "disabled",
        "navigation_failed",
        "network_log_failed",
        "invalid_json",
        "no_candidates",
        "lint_rejected",
        "canary_rejected",
        "session_expired",
        "transport_failed",
        "unconfirmed",
        "intent_changed",
        "token_budget",
        "not_found",
        "recipe_rejected",
        "validation",
        "provider_failed",
        "timeout",
        "shutdown",
        "user",
        "event_budget",
        "fetch_failed",
        "planner_unavailable",
        "internal",
        "bad_request",
        "auth_failed",
        "method_not_allowed",
        "ip_literal",
        "port_not_allowed",
        "not_allowlisted",
        "too_many_hosts",
        "budget_exceeded",
        "dns_failure",
        "invalid_address",
        "private_address",
        "connect_timeout",
        "connect_failed",
        "connection_lost",
        "ok",
    },
    "state": {"queued", "running", "waiting_user", "done", "failed", "cancelled"},
    "channel": {"manual", "native", "dialogue", "approval"},
    "receipt_status": {"accepted", "applied", "superseded"},
    "block_type": {
        "thinking",
        "text",
        "input",
        "login",
        "wait",
        "choice",
        "confirm",
        "approval",
        "summary",
        "progress",
        "warning",
    },
    "purpose": {"classify", "chat", "learn", "repair", "extract"},
    "provider": {"exa", "deepseek"},
    "error_code": {code.value for code in ErrorCode},
    "lint_code": set(
        [
            "auth.header",
            "bootstrap.duplicate",
            "document.url",
            "document.url_secret",
            "endpoint.duplicate",
            "field.missing_required",
            "field.unknown",
            "host.ip_literal",
            "host.not_allowed",
            "html.attr",
            "html.selector",
            "identity.missing",
            "identity.unknown_field",
            "method.post_not_allowed",
            "moodle.function",
            "observed.value",
            "origin.mismatch",
            "pagination.cursor",
            "param.secret_name",
            "pattern.unsafe",
            "personal.digits",
            "personal.email",
            "personal.jwt",
            "personal.phone",
            "personal.snils",
            "personal.token",
            "personal.uuid",
            "pointer.secret",
            "schema",
            "table.kind",
            "template.malformed",
            "template.secret_name",
            "template.unknown",
            "url.absolute",
            "url.destructive",
            "url.dot_segment",
            "url.fragment_or_query",
            "url.host_in_value",
            "url.protocol_relative",
            "validation.kind_not_covered",
        ]
    ),
}
_context: ContextVar[dict[str, object] | None] = ContextVar("diagnostic_context", default=None)
_settings: Settings | None = None
_handler: RotatingFileHandler | None = None
_bucket: str | None = None
_counts: OrderedDict[str, int] = OrderedDict()
MAX_EVENTS_PER_SCOPE = 10_000
_FILES = re.compile(r"(?:api|worker|proxy|app)-\d{10}\.jsonl(?:\.\d+)?\Z")


def url_facts(url: str) -> dict[str, object]:
    """Stable correlation without saving paths, query values, credentials or fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        # Even host labels can hold identifiers: save a digest, not an untrusted hostname.
        return {
            "host_hash": hashlib.sha256(host.lower().encode()).hexdigest()[:16],
            "url_hash": hashlib.sha256(url.encode()).hexdigest()[:16],
            "path_hash": hashlib.sha256(parts.path.encode()).hexdigest()[:16],
            "path_depth": len([p for p in parts.path.split("/") if p]),
            "query_count": len(parts.query.split("&")) if parts.query else 0,
        }
    except ValueError:
        return {}


def _clean(facts: dict[str, object]) -> dict[str, object]:
    clean: dict[str, object] = {}
    for key, value in facts.items():
        if (
            (key in _IDS and isinstance(value, str) and _ID.fullmatch(value))
            or (
                key in {"host_hash", "url_hash", "path_hash", "grant_hash"}
                and isinstance(value, str)
                and _HEX.fullmatch(value)
            )
            or (key in _BOOLEANS and isinstance(value, bool))
        ):
            clean[key] = value
        elif key in _NUMBERS and type(value) in (int, float) and isinstance(value, (int, float)):
            if 0 <= value <= 1e15 and math.isfinite(value):
                clean[key] = value
        elif key in _CODES and isinstance(value, str) and value in _CODES[key]:
            clean[key] = value
    return clean


@contextmanager
def diagnostic_scope(**facts: object) -> Generator[None]:
    token = _context.set(_clean({**(_context.get() or {}), **facts}))
    try:
        yield
    finally:
        _context.reset(token)


def bind_diagnostics(**facts: object) -> None:
    """Add a connection discovered after bootstrap to the current job's context."""
    _context.set(_clean({**(_context.get() or {}), **facts}))


def diagnostic_context() -> dict[str, object]:
    """Copy safe correlation before registering callbacks in another task's dispatcher."""
    return {
        key: value
        for key, value in (_context.get() or {}).items()
        if key in _IDS or key == "job_kind"
    }


class _PrivateHandler(RotatingFileHandler):
    def _open(self) -> TextIOWrapper:
        descriptor = os.open(
            self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        return os.fdopen(descriptor, "a", encoding="utf-8")


def configure_diagnostics(settings: Settings | None = None) -> None:
    global _settings, _handler, _bucket
    if _handler is not None:
        _handler.close()
    _handler, _bucket = None, None
    _counts.clear()
    _settings = settings or get_settings()
    purge_diagnostics()


def purge_diagnostics() -> None:
    """Delete only this writer's known trace files, by age and total file budget.

    Hour buckets may be removed a little early, so records never outlive the retention window.
    The worker calls this even when diagnostics is disabled. Never follows symlinks.
    """
    global _handler, _bucket
    cfg = _settings
    if cfg is None:
        return
    directory = Path(cfg.botai_diagnostics_dir)
    if not directory.is_dir():
        return
    cutoff = datetime.now(UTC) - timedelta(hours=cfg.botai_diagnostics_retention_hours)
    try:
        files = sorted(
            (
                p
                for p in directory.iterdir()
                if _FILES.fullmatch(p.name) and not p.is_symlink() and p.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        kept: Counter[str] = Counter()
        for path in files:
            service, stamp = path.name.split("-", 1)
            old = datetime.strptime(stamp[:10], "%Y%m%d%H").replace(tzinfo=UTC) <= cutoff
            if old and _handler is not None and path == Path(_handler.baseFilename):
                _handler.close()
                _handler, _bucket = None, None
            if not old:
                kept[service] += path.stat().st_size
            # Do not unlink an active handler: it would keep writing an invisible inode.
            if (
                old
                or kept[service]
                > cfg.botai_diagnostics_max_bytes * (cfg.botai_diagnostics_backup_count + 1)
            ) and (_handler is None or path != Path(_handler.baseFilename)):
                path.unlink(missing_ok=True)
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("diagnostic cleanup unavailable")


def diagnostic_event(event: str, **facts: object) -> None:
    global _handler, _bucket
    cfg = _settings or get_settings()
    if not cfg.botai_diagnostics or not _EVENT.fullmatch(event):
        return
    clean = _clean({**(_context.get() or {}), **facts})
    scope_id = str(
        clean.get("job_id")
        or clean.get("connection_id")
        or clean.get("request_id")
        or clean.get("grant_hash")
        or "process"
    )
    seen = _counts.get(scope_id, 0)
    if seen > MAX_EVENTS_PER_SCOPE:
        return
    _counts[scope_id] = seen + 1
    _counts.move_to_end(scope_id)
    if len(_counts) > 1024:
        _counts.popitem(last=False)
    if seen == MAX_EVENTS_PER_SCOPE:
        event, clean = (
            "diagnostics.truncated",
            {**_clean({k: v for k, v in clean.items() if k in _IDS}), "reason": "event_budget"},
        )
    now = datetime.now(UTC)
    bucket = now.strftime("%Y%m%d%H")
    try:
        if _handler is None or _bucket != bucket:
            if _handler is not None:
                _handler.close()
            directory = Path(cfg.botai_diagnostics_dir)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _handler = _PrivateHandler(
                directory / f"{cfg.botai_diagnostics_service}-{bucket}.jsonl",
                maxBytes=cfg.botai_diagnostics_max_bytes,
                backupCount=max(1, cfg.botai_diagnostics_backup_count),
                encoding="utf-8",
            )
            _handler.setFormatter(logging.Formatter("%(message)s"))
            _bucket = bucket
        record = {
            "version": 1,
            "at": now.isoformat(),
            "service": cfg.botai_diagnostics_service,
            "event": event,
            **clean,
        }
        _handler.handle(
            logging.LogRecord(
                "botai.diagnostics",
                logging.INFO,
                "",
                0,
                json.dumps(record, ensure_ascii=True),
                (),
                None,
            )
        )
        purge_diagnostics()
    except (OSError, ValueError):
        # Diagnostic failure cannot change login, job execution or API behavior.
        logging.getLogger(__name__).warning("diagnostic sink unavailable")


def _read_records(cfg: Settings) -> Iterator[dict[str, object]]:
    cutoff = datetime.now(UTC) - timedelta(hours=cfg.botai_diagnostics_retention_hours)
    for path in sorted(Path(cfg.botai_diagnostics_dir).glob("*.jsonl*")):
        if not _FILES.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, dict) or not isinstance(raw.get("at"), str):
                            continue
                        at = datetime.fromisoformat(raw["at"])
                        if (
                            at < cutoff
                            or not isinstance(raw.get("event"), str)
                            or not _EVENT.fullmatch(raw["event"])
                        ):
                            continue
                        yield {"at": at.isoformat(), "event": raw["event"], **_clean(raw)}
                    except (ValueError, TypeError):
                        continue
        except OSError:
            continue  # Rotation may remove a file between listing and opening it.


def main() -> None:
    parser = argparse.ArgumentParser(description="Read bounded BotAI metadata diagnostics locally")
    parser.add_argument("--thread")
    parser.add_argument("--job")
    parser.add_argument("--connection")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    filters = {
        key: value
        for key, value in {
            "thread_id": args.thread,
            "job_id": args.job,
            "connection_id": args.connection,
        }.items()
        if value
    }
    if any(not _ID.fullmatch(v) for v in filters.values()):
        parser.error("identifiers must be ULIDs")
    if not filters and not args.summary:
        parser.error("select --thread, --job, --connection or --summary")
    cfg = get_settings()
    grants = (
        {
            row["grant_hash"]
            for row in _read_records(cfg)
            if filters and all(row.get(k) == v for k, v in filters.items()) and "grant_hash" in row
        }
        if filters
        else set()
    )
    records: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    total = 0
    for row in _read_records(cfg):
        if (
            filters
            and not all(row.get(k) == v for k, v in filters.items())
            and row.get("grant_hash") not in grants
        ):
            continue
        total += 1
        counts[str(row["event"])] += 1
        if not args.summary and len(records) < 50_000:
            records.append(row)
    if args.summary:
        print(
            json.dumps(
                {
                    "enabled": cfg.botai_diagnostics,
                    "retention_hours": cfg.botai_diagnostics_retention_hours,
                    "record_count": total,
                    "events": dict(counts),
                }
            )
        )
    else:
        records.sort(key=lambda r: str(r["at"]))
        for record in records:
            print(json.dumps(record))
        if total > len(records):
            print(
                json.dumps(
                    {
                        "event": "diagnostics.export_truncated",
                        "record_count": total,
                        "limit": len(records),
                    }
                )
            )


if __name__ == "__main__":
    main()
