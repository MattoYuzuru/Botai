"""Operator command: canary or sync a v4 source on behalf of its owner, without the model.

    python -m botai.ops.source_sync --source ULID [--canary] [--dry-run] [--wait SECONDS]
                                    [--kinds-add KIND[,KIND]]

A source created by the agent stays `draft` until a successful `SourceRunner.sync` marks it
`active` and plans the next tick (`next_sync_at`); the scheduler never picks a `draft` source up,
and the agent can run out of its token budget before it calls `source_sync`. The playbook may
already be active with a green canary: what is missing is exactly one deterministic replay, and
this command makes it. Nothing here calls a model ($0).

Default mode enqueues the normal `SYNC` job through `sources.v4.sync_jobs.enqueue_sync`, the same
code as `POST /sources/{id}/sync-jobs`; the worker runs `_sync_source_by_id`, which writes the
domain, flips the status and sets `next_sync_at`. `--wait N` polls the job and prints the outcome.
`--canary` replays the active playbook through `SourceRunner.probe(persist=False)` in this process:
no domain rows, no artifacts, no status change, no canary/health record. `--dry-run` reads the
source and prints what would happen: no writes, no network. `--kinds-add` adds data kinds to the
source's declared `kinds` (the vocabulary of `sources.v4.kinds`) and prints before/after.

The command prints only identifiers, states, counts, field names with presence counts, and error
codes: never record values, titles, URLs, error details, cookies, Vault or session contents.
Exit codes: 0 success, 2 the source is not found or the request is invalid, 1 failure.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.factory import build_source_toolkit
from botai.core.errors import BotaiError
from botai.core.services import Services
from botai.db.models import Job, Source, User
from botai.jobs.events import TERMINAL
from botai.sources.normalize import MODELS
from botai.sources.v4 import kinds as kinds_vocab
from botai.sources.v4 import playbooks, service, store, sync_jobs
from botai.sources.v4.runner import RunReport
from botai.sources.v4.schemas import SourcePatch
from botai.wiring import build_services

POLL_SECONDS = 2.0
MAX_WAIT_SECONDS = 3600.0
# Fields of a record are named by the playbook; only plain identifier-like names are printed.
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_EMPTY_TEXT = frozenset({"", "none", "null"})
_REFUSED_STATUSES = frozenset({"disabled", "archived"})
# ULID: 26 chars of Crockford base32 (no I, L, O, U).
_SOURCE_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class SourceSyncError(Exception):
    """A failed run (exit 1), printed without a traceback."""


class InvalidRequest(SourceSyncError):
    """A mistake of the operator or a source that cannot be run (exit 2)."""


@dataclass(frozen=True, slots=True)
class SourceInfo:
    source_id: str
    owner_id: str
    owner_name: str
    status: str
    kinds: tuple[str, ...]
    playbook_id: str | None
    active_version: int | None
    canary_ok: bool | None
    last_sync_at: datetime | None
    next_sync_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a finished sync job left behind; only states, counts and codes."""

    job_state: str
    result_status: str | None
    counts: dict[str, int]
    error_codes: list[str]


async def inspect_source(session: AsyncSession, source_id: str) -> SourceInfo:
    """The source and its owner by id alone: the operator reaches any account's source."""
    source = await session.scalar(select(Source).where(Source.id == source_id))
    if source is None:
        raise InvalidRequest("Источник не найден")
    owner = await session.scalar(select(User).where(User.id == source.user_id))
    if owner is None:  # a foreign-key violation would be a broken database, not a bad command
        raise InvalidRequest("Владелец источника не найден")
    version_number: int | None = None
    canary_ok: bool | None = None
    if source.playbook_id is not None:
        version = await playbooks.active_playbook_version(
            session, user_id=owner.id, playbook_id=source.playbook_id
        )
        if version is not None:
            version_number = version.version
            raw = (version.canary or {}).get("ok")
            canary_ok = raw if isinstance(raw, bool) else None
    code = (source.last_error or {}).get("code")
    return SourceInfo(
        source_id=source.id,
        owner_id=owner.id,
        owner_name=owner.display_name,
        status=source.status,
        kinds=tuple(source.kinds),
        playbook_id=source.playbook_id,
        active_version=version_number,
        canary_ok=canary_ok,
        last_sync_at=source.last_sync_at,
        next_sync_at=source.next_sync_at,
        last_error_code=code if isinstance(code, str) else None,
    )


def check_runnable(info: SourceInfo) -> None:
    """The sources this command refuses, with the reason in operator language."""
    if info.status in _REFUSED_STATUSES:
        raise InvalidRequest(f"Источник в состоянии {info.status}: запуск запрещён")
    if info.playbook_id is None:
        raise InvalidRequest("У источника нет playbook: сначала подключите источник")
    if info.active_version is None:
        raise InvalidRequest(
            "У playbook нет активной версии: прогоните canary и активируйте версию"
        )


def merged_kinds(current: Sequence[str], additions: Sequence[str]) -> list[str]:
    """The declared kinds with the additions appended; vocabulary validation is the service's."""
    return kinds_vocab.normalize_kinds([*current, *additions])


def field_presence(records: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Per kind: how many records there are and how many have each field non-empty (no values)."""
    totals: Counter[str] = Counter()
    present: dict[str, Counter[str]] = {}
    for record in records:
        kind = str(record.kind)
        totals[kind] += 1
        bucket = present.setdefault(kind, Counter())
        for name, value in dict(record.fields).items():
            if _FIELD_NAME.fullmatch(str(name)) and _non_empty(value):
                bucket[str(name)] += 1
    return {
        kind: {"records": totals[kind], **dict(sorted(present[kind].items()))}
        for kind in sorted(totals)
    }


def _non_empty(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_TEXT
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _stamp(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else "нет"


def _print_source(info: SourceInfo, out: Callable[[str], None]) -> None:
    out(f"Источник: {info.source_id}")
    out(f"Владелец: {info.owner_name} ({info.owner_id})")
    out(f"Состояние: {info.status}")
    out(f"Виды: {', '.join(info.kinds) if info.kinds else 'нет'}")
    if info.active_version is None:
        out("Активная версия playbook: нет")
    else:
        canary = {True: "canary ok", False: "canary не пройден", None: "canary не записан"}
        out(f"Активная версия playbook: v{info.active_version} ({canary[info.canary_ok]})")
    out(f"Последний sync: {_stamp(info.last_sync_at)}")
    out(f"Следующий sync: {_stamp(info.next_sync_at)}")
    if info.last_error_code is not None:
        out(f"Последняя ошибка: {info.last_error_code}")


def _kind_list(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("укажите вид источника, например grades")
    for item in items:
        if item not in kinds_vocab.DATA_KINDS:
            raise argparse.ArgumentTypeError(
                f"неизвестный вид данных {item!r}; допустимы: {', '.join(kinds_vocab.DATA_KINDS)}"
            )
    return items


def _source_id(value: str) -> str:
    normalized = value.strip().upper()
    if not _SOURCE_ID.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            f"идентификатор источника — ULID из 26 символов, получено {value!r}"
        )
    return normalized


def _wait_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"нужно число секунд, получено {value!r}") from None
    if not 0 < seconds <= MAX_WAIT_SECONDS:
        raise argparse.ArgumentTypeError(f"ожидание — от 1 до {int(MAX_WAIT_SECONDS)} секунд")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="botai.ops.source_sync",
        description="BotAI: canary или sync источника v4 от имени владельца, без модели",
    )
    p.add_argument("--source", required=True, type=_source_id, help="ULID источника")
    p.add_argument(
        "--canary",
        action="store_true",
        help="прогнать активный playbook без записи; печатаются только счётчики",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="только прочитать источник и показать план; ничего не писать, в сеть не ходить",
    )
    p.add_argument(
        "--wait",
        type=_wait_seconds,
        metavar="SECONDS",
        help="дождаться задания sync до SECONDS секунд и напечатать итог",
    )
    p.add_argument(
        "--kinds-add",
        action="append",
        type=_kind_list,
        metavar="KIND",
        help="добавить вид данных (можно через запятую, флаг можно повторять)",
    )
    return p


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


# --- canary --------------------------------------------------------------------------------------


def _print_canary(report: RunReport, out: Callable[[str], None]) -> None:
    out(f"Canary: playbook v{report.version}, прогон без записи")
    out(f"Результат: {'ok' if report.ok else 'не пройден'}")
    out(f"Чтений: {len(report.reads)}")
    counts = ", ".join(f"{kind}={count}" for kind, count in sorted(report.counts.items()))
    out(f"Записей по видам: {counts or 'нет'}")
    for kind, stats in field_presence(report.records).items():
        fields = ", ".join(f"{name}={count}" for name, count in stats.items() if name != "records")
        sample = "" if not report.records_truncated else " (выборка)"
        out(f"Поля {kind} (записей {stats['records']}{sample}): {fields or 'нет'}")
    if report.errors:
        codes = Counter(error.code for error in report.errors)
        out("Ошибки: " + ", ".join(f"{code}×{n}" for code, n in sorted(codes.items())))


async def run_canary(
    services: Services,
    info: SourceInfo,
    *,
    http_transport: httpx.AsyncBaseTransport | None,
    artifact_root: str | Path | None,
    source_cookies: Mapping[str, str] | None,
) -> RunReport:
    """The active version through `SourceRunner.probe(persist=False)`: no write of any kind."""
    toolkit = build_source_toolkit(
        services,
        user_id=info.owner_id,
        artifact_root=artifact_root,
        transport=http_transport,
        cookies=source_cookies,
    )
    async with services.db() as session:
        report = await toolkit.runner(session, info.owner_id).probe(
            info.source_id, version=info.active_version, persist=False
        )
        await session.rollback()
    return report


# --- sync ----------------------------------------------------------------------------------------


async def _job(services: Services, job_id: str) -> Job:
    async with services.db() as session:
        job = await session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise SourceSyncError(f"Задание {job_id} пропало из очереди")
    return job


def _outcome(job: Job) -> Outcome:
    result = job.result or {}
    raw_counts = result.get("counts")
    counts = (
        {str(k): int(v) for k, v in raw_counts.items() if isinstance(v, int)}
        if isinstance(raw_counts, dict)
        else {}
    )
    raw_errors = result.get("errors")
    codes = (
        [str(e["code"]) for e in raw_errors if isinstance(e, dict) and "code" in e]
        if isinstance(raw_errors, list)
        else []
    )
    if job.error and isinstance(job.error.get("code"), str):
        codes.append(str(job.error["code"]))
    status = result.get("status")
    return Outcome(
        job_state=job.state,
        result_status=status if isinstance(status, str) else None,
        counts=counts,
        error_codes=codes,
    )


async def wait_for_job(
    services: Services, job_id: str, *, seconds: float, poll_s: float
) -> Job | None:
    """The job once it is terminal, or None when `seconds` ran out first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        job = await _job(services, job_id)
        if job.state in TERMINAL:
            return job
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(min(poll_s, max(deadline - loop.time(), 0.01)))


async def domain_rows(services: Services, source_id: str, kinds: Sequence[str]) -> dict[str, int]:
    """Rows the source owns now, per data kind that has a domain table."""
    rows: dict[str, int] = {}
    async with services.db() as session:
        for kind in sorted(set(kinds)):
            reading = kinds_vocab.reading_kind(kind)
            if reading is None:
                continue
            model: Any = MODELS[reading]
            rows[kind] = int(
                await session.scalar(
                    select(func.count()).select_from(model).where(model.source_id == source_id)
                )
                or 0
            )
    return rows


async def _report_sync(
    services: Services, before: SourceInfo, job: Job, out: Callable[[str], None]
) -> int:
    outcome = _outcome(job)
    async with services.db() as session:
        after = await inspect_source(session, before.source_id)
    out(f"Задание: {job.id}, состояние {outcome.job_state}")
    if outcome.result_status is not None:
        out(f"Итог sync: {outcome.result_status}")
    counts = ", ".join(f"{kind}={n}" for kind, n in sorted(outcome.counts.items()))
    out(f"Прочитано записей: {counts or 'нет'}")
    rows = await domain_rows(services, before.source_id, list(outcome.counts))
    if rows:
        out("Строк в базе от источника: " + ", ".join(f"{k}={n}" for k, n in rows.items()))
    out(f"Состояние источника: {before.status} -> {after.status}")
    out(f"Последний sync: {_stamp(after.last_sync_at)}")
    out(f"Следующий sync: {_stamp(after.next_sync_at)}")
    if outcome.error_codes:
        codes = Counter(outcome.error_codes)
        out("Ошибки: " + ", ".join(f"{code}×{n}" for code, n in sorted(codes.items())))
    if outcome.job_state == "done" and outcome.result_status == "ok":
        return 0
    if outcome.result_status == "needs_reauth":
        out("Сессия источника не принята: нужен новый вход владельца в кабинет.")
    return 1


# --- command -------------------------------------------------------------------------------------


async def run(
    argv: Sequence[str],
    *,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = _stderr,
    services: Services | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    artifact_root: str | Path | None = None,
    source_cookies: Mapping[str, str] | None = None,
    poll_s: float = POLL_SECONDS,
) -> int:
    args = build_parser().parse_args(argv)
    additions: list[str] = [kind for group in args.kinds_add or [] for kind in group]
    svc = services if services is not None else build_services()
    try:
        async with svc.db() as session:
            info = await inspect_source(session, args.source)
        _print_source(info, out)
        check_runnable(info)
        if not args.canary and svc.settings.agent_engine != "v4":
            raise InvalidRequest("Задачи источников на v4 выключены: нужен BOTAI_AGENT_ENGINE=v4")

        after_kinds = merged_kinds(info.kinds, additions)
        kinds_change = additions and after_kinds != list(info.kinds)
        if additions:
            out(f"Виды до: {', '.join(info.kinds) or 'нет'}")
            out(f"Виды после: {', '.join(after_kinds) or 'нет'}")
        mode = "canary без записи" if args.canary else "sync через очередь"
        if args.dry_run:
            if kinds_change:
                out("--dry-run: виды были бы обновлены")
            out(f"--dry-run: был бы выполнен {mode}; ничего не записано, сеть не использована")
            return 0
        if kinds_change:
            async with svc.db() as session:
                await service.update_source(
                    session,
                    user_id=info.owner_id,
                    source_id=info.source_id,
                    patch=SourcePatch(kinds=after_kinds),
                )
                await session.commit()
            out("Виды обновлены")

        if args.canary:
            report = await run_canary(
                svc,
                info,
                http_transport=http_transport,
                artifact_root=artifact_root,
                source_cookies=source_cookies,
            )
            _print_canary(report, out)
            return 0 if report.ok else 1

        async with svc.db() as session:
            source = await store.owned_source(session, info.owner_id, info.source_id)
            job_id, state = await sync_jobs.enqueue_sync(svc, user_id=info.owner_id, source=source)
        out(f"Задание: {job_id} (состояние {state})")
        if args.wait is None:
            out(
                "Sync поставлен в очередь: его выполнит worker; итог — повторным запуском с --wait."
            )
            return 0
        job = await wait_for_job(svc, job_id, seconds=args.wait, poll_s=poll_s)
        if job is None:
            err(f"Ошибка: задание не завершилось за {args.wait:g} с; оно остаётся в очереди")
            return 1
        return await _report_sync(svc, info, job, out)
    except InvalidRequest as exc:
        err(f"Ошибка: {exc}")
        return 2
    except SourceSyncError as exc:
        err(f"Ошибка: {exc}")
        return 1
    except BotaiError as exc:
        err(f"Ошибка: {exc.detail}")
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
