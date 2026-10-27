"""Benchmark scenarios B1-B3: learning a university and repairing its recipe (benchmark.md).

The product code under test is the real thing: the Explorer (`CONNECT`) and the repair job
(`REPAIR`) of `botai.learn`, the recipe lint, the canary, the first sync and the ingestion into
Postgres. Only the university is synthetic (`fakeuni`) and only the browser, the login and the
student are doubles, exactly as in W3's story test. The cell's profile drives the `learn` and
`repair` purposes (`bench.provider_for`).

* **B1** - connect to a university whose data come as JSON: schedule and deadlines.
* **B2** - connect to a university whose data are an HTML page: news.
* **B3** - an active recipe, then the university moves its timetable endpoint; the sync notices
  (`repairing`), the repair job re-learns the endpoint.

A run succeeds when the job ended as it should, the recipe is active and the rows in the database
are the reference rows of `bench/scenarios/learn.json` (the "canary matches the reference" of
benchmark.md); B1 and B2 also fail when the student was asked more than the one confirmation.

The synthetic university and W3's helpers live in `backend/tests/`, which the production image
does not contain. They are imported when a run starts; without them the scenarios are unavailable
with a clear message. Run from the `backend/` directory (`make bench` does).

Every run gets its own database cloned from one migrated template: the learned recipe of an
origin is shared between students, so a second run in the same database would skip the learning.
fakeuni keeps its switches (a moved endpoint) per process, so B3 runs one at a time; B1 and B2
run `--concurrency` runs at once.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from botai.agent import bench
from botai.agent.bench import (
    BENCH_DIR,
    Cell,
    FusedLlm,
    Launch,
    RunRecord,
    ScenarioUnavailable,
    database_url,
    migrated_database,
)
from botai.agent.llm import BudgetExceeded
from botai.contracts.specs import Kind
from botai.core.jobs import JobCancelled, JobKind
from botai.core.llm import LlmProvider
from botai.db import models as m
from botai.settings import Settings
from botai.sources.vocab import KIND_LABELS

LEARN_JSON = BENCH_DIR / "scenarios" / "learn.json"
# --dry-run only: a run is two or three model rounds over small pages
ROUGH_USD_PER_RUN = {"flash": 0.01, "pro": 0.04}
# kind -> (table, the column with the moment that is compared with the reference)
TABLES: dict[Kind, tuple[Any, str | None]] = {
    "scheduleEvents": (m.ScheduleEvent, "starts_at"),
    "deadlines": (m.Deadline, "due_at"),
    "news": (m.NewsItem, None),
}
BREAK_AFTER = timedelta(hours=3)  # the scheduled sync that finds the moved endpoint


class SetupFailed(ScenarioUnavailable):
    """The scenario's own preparation failed (not the model): nothing was learned from this run."""


# --- scenario plans -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expected:
    title: str
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    title: str
    job: str  # connect | repair
    kinds: tuple[Kind, ...]
    max_questions: int | None  # None: recorded, not judged
    reference: dict[Kind, tuple[Expected, ...]]


def load_plans(path: Path = LEARN_JSON) -> dict[str, Plan]:
    data = json.loads(path.read_text("utf-8"))
    reference: dict[Kind, tuple[Expected, ...]] = {
        cast(Kind, kind): tuple(
            Expected(
                row["title"],
                datetime.fromisoformat(row["at"]) if row.get("at") else None,
            )
            for row in rows
        )
        for kind, rows in data["reference"].items()
    }
    return {
        sid: Plan(
            id=sid,
            title=spec["title"],
            job=spec["job"],
            kinds=tuple(cast(Kind, k) for k in spec["kinds"]),
            max_questions=spec["maxQuestions"],
            reference={cast(Kind, k): reference[cast(Kind, k)] for k in spec["kinds"]},
        )
        for sid, spec in data["scenarios"].items()
    }


# --- W3's machinery, imported late --------------------------------------------------------------


class Kit:
    """The synthetic university and W3's test helpers (`backend/tests`), loaded when a run starts."""

    def __init__(self) -> None:
        try:
            self.create_app = import_module("tests.fakeuni.app").create_fakeuni
            self.api = import_module("tests.fakeuni.api")
            self.login = import_module("tests.fakeuni.login")
            env = import_module("tests.learn.env")
            self.env_class, self.preset = env.Env, env.PRESET
            self.site = import_module("tests.learn.support")
            self.seed_connection = import_module("tests.sources.support").seed_connection
        except ImportError as exc:
            raise ScenarioUnavailable(
                "нет синтетического вуза и помощников из backend/tests (fakeuni, learn, sources): "
                "B1-B3 запускают из каталога backend/ репозитория, в продуктовом образе тестов "
                f"нет ({type(exc).__name__}: {exc})"
            ) from exc

    def reset(self) -> None:
        """fakeuni as it was born: nothing moved, nobody logged in."""
        self.api.reset()
        self.login.SESSIONS.clear()

    def move_timetable(self) -> None:
        self.api.STATE.timetable_version = "v3"

    async def environment(
        self, app: object, sm: async_sessionmaker[AsyncSession], db: AsyncSession, *, learned: bool
    ) -> Any:
        """W3's `Env` over this database. `learned`: the connection already has an active recipe
        (the start of B3); otherwise it is a fresh connection of a university nobody learned."""
        seeded = await self.seed_connection(
            db,
            with_spec=learned,
            status="active" if learned else "draft",
            preset=self.preset,
            university=True,
        )
        await db.commit()  # the handlers read through sessions of their own
        browser = await self.site.fake_site(app)
        env = self.env_class(db, sm, app, seeded, [], browser)
        await env.prepare()
        return env


# --- a database per run -------------------------------------------------------------------------


class Clones:
    """New databases copied from one migrated template (milliseconds instead of a migration)."""

    def __init__(self, settings: Settings, template: str) -> None:
        self.settings, self.template = settings, template
        self._admin = create_async_engine(settings.database_url, isolation_level="AUTOCOMMIT")
        self._lock = asyncio.Lock()  # Postgres copies a template one clone at a time

    async def aclose(self) -> None:
        await self._admin.dispose()

    @asynccontextmanager
    async def new(self) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
        name = f"botai_bench_{os.urandom(5).hex()}"
        async with self._lock, self._admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{self.template}"'))
        engine = create_async_engine(database_url(self.settings, name))
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
            async with self._admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@asynccontextmanager
async def clones(settings: Settings) -> AsyncGenerator[Clones]:
    async with migrated_database(settings) as template:
        pool = Clones(settings, template)
        try:
            yield pool
        finally:
            await pool.aclose()


# --- judging ------------------------------------------------------------------------------------


async def read_rows(
    sm: async_sessionmaker[AsyncSession], connection_id: str, kind: Kind
) -> list[tuple[str, datetime | None]]:
    """(title, moment) of the connection's rows of this kind, the moment in UTC."""
    model, when = TABLES[kind]
    columns = [model.title] if when is None else [model.title, getattr(model, when)]
    async with sm() as session:
        rows = (
            await session.execute(select(*columns).where(model.connection_id == connection_id))
        ).all()
    return [
        (row[0], row[1].astimezone(UTC) if when is not None and row[1] is not None else None)
        for row in rows
    ]


def compare_rows(
    kind: Kind, expected: Sequence[Expected], actual: Sequence[tuple[str, datetime | None]]
) -> list[str]:
    """Why the rows differ from the reference (empty list: they match)."""
    timed = any(e.at is not None for e in expected)
    want = Counter((e.title, e.at if timed else None) for e in expected)
    have = Counter((title, at if timed else None) for title, at in actual)
    missing, extra = want - have, have - want
    if not missing and not extra:
        return []
    label = KIND_LABELS[kind].lower()
    parts = [f"{label}: записей {len(actual)} вместо {len(expected)}"]
    if missing:
        parts.append("нет: " + ", ".join(f"«{t}»" for t, _ in list(missing)[:3]))
    if extra:
        parts.append("не те: " + ", ".join(f"«{t}»" for t, _ in list(extra)[:3]))
    return ["; ".join(parts)]


@dataclass(slots=True)
class Outcome:
    """What the job returned, how it failed (if it did) and how long it took."""

    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0


async def timed(job: Awaitable[dict[str, Any]]) -> Outcome:
    """Run the job; the student's answers are scripted, so the time is the agent's own."""
    out = Outcome()
    started = time.monotonic()
    try:
        out.result = await job
    except BudgetExceeded:
        raise
    except JobCancelled as exc:
        out.error = f"задание ждало ответа студента, которого нет у сценария: {exc}"[:300]
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"[:300]
    out.seconds = time.monotonic() - started
    return out


@dataclass(slots=True)
class Verdict:
    success: bool
    reasons: list[str]
    facts: str


async def judge_run(
    plan: Plan,
    env: Any,
    sm: async_sessionmaker[AsyncSession],
    outcome: Outcome,
    questions: int,
) -> Verdict:
    """Success rules of B1-B3 (see the module docstring); `facts` is what the report shows."""
    status = str(outcome.result.get("status", "—"))
    specs = await env.specs()
    rows = {k: await read_rows(sm, env.seeded.connection_id, k) for k in plan.kinds}
    counts = ", ".join(f"{k}={len(rows[k])}" for k in plan.kinds)
    facts = f"status={status}; recipe={specs}; {counts}; вопросов={questions}"
    if outcome.error:
        return Verdict(False, [outcome.error], facts)

    reasons: list[str] = []
    repair = plan.job == "repair"
    wanted = "repaired" if repair else "ok"
    if status != wanted:
        detail = outcome.result.get("detail")
        reasons.append(
            f"задание закончилось со статусом «{status}»" + (f" ({detail})" if detail else "")
        )
    if repair:
        conn = await env.conn()
        if specs != [(1, "deprecated"), (2, "active")]:
            reasons.append(f"версии описания: {specs}, ожидалось [(1, deprecated), (2, active)]")
        if (conn.status, conn.spec_revision, conn.error_code) != ("active", 2, None):
            reasons.append(
                f"подключение: {conn.status}, версия {conn.spec_revision}, ошибка {conn.error_code}"
            )
        revisions = await env.rows_revision()
        if revisions != {2}:
            reasons.append(f"расписание прочитано версиями {sorted(revisions)}, ожидалась только 2")
    elif specs != [(1, "active")]:
        reasons.append(f"версии описания: {specs}, ожидалось [(1, active)]")
    for kind in plan.kinds:
        reasons += compare_rows(kind, plan.reference[kind], rows[kind])
    if plan.max_questions is not None and questions > plan.max_questions:
        reasons.append(
            f"студента спросили {questions} раз, ожидалось не больше {plan.max_questions}"
        )
    return Verdict(not reasons, reasons, facts)


# --- the scenario -------------------------------------------------------------------------------


class LearningScenario:
    """B1, B2 or B3: see `Plan` and the module docstring."""

    def __init__(self, plan: Plan) -> None:
        self.plan, self.id = plan, plan.id
        self.rough_usd = ROUGH_USD_PER_RUN

    def case_count(self, only: set[str]) -> int:
        return 1  # one story per cell and repeat

    async def run(
        self, launch: Launch, cells: Sequence[Cell]
    ) -> tuple[list[RunRecord], str | None]:
        kit = Kit()  # unavailable without backend/tests: say so before anything is spent
        app = kit.create_app()
        kit.reset()
        records: list[RunRecord] = []
        stop: str | None = None
        fatal: SetupFailed | None = None
        serial = self.plan.job == "repair"  # the moved endpoint is fakeuni's, shared by the process
        sem = asyncio.Semaphore(1 if serial else launch.concurrency)

        async with clones(launch.settings) as databases:

            async def one(cell: Cell, provider: LlmProvider, repeat: int) -> None:
                nonlocal stop, fatal
                async with sem:
                    if stop:
                        return
                    try:
                        records.append(
                            await self._once(kit, app, launch, databases, cell, provider, repeat)
                        )
                    except BudgetExceeded as exc:
                        stop = f"предохранитель: {exc}"
                    except SetupFailed as exc:
                        fatal, stop = exc, str(exc)

            tasks: list[Awaitable[None]] = []
            for cell in cells:
                provider = bench.provider_for(launch, cell)  # one per cell, shared by its repeats
                tasks += [one(cell, provider, repeat) for repeat in range(launch.repeats)]
            try:
                await asyncio.gather(*tasks)
            finally:
                kit.reset()
        if fatal is not None:
            raise fatal
        records.sort(key=lambda r: (r.cell, r.repeat))
        return records, stop

    async def _once(
        self,
        kit: Kit,
        app: object,
        launch: Launch,
        databases: Clones,
        cell: Cell,
        provider: LlmProvider,
        repeat: int,
    ) -> RunRecord:
        plan = self.plan
        llm = FusedLlm(provider, launch.fuse)
        async with databases.new() as sm, sm() as db:
            env = await kit.environment(app, sm, db, learned=plan.job == "repair")
            if plan.job == "repair":
                outcome = await self._repair(kit, env, llm)
            else:
                env.services.llm = llm
                outcome = await timed(
                    env.run(JobKind.CONNECT, {"kinds": list(plan.kinds)}),
                )
            questions = len(env.asked)
            verdict = await judge_run(plan, env, sm, outcome, questions)
        usages = [c.usage for c in llm.calls]
        miss = sum(u.input_cache_miss for u in usages)
        hit = sum(u.input_cache_hit for u in usages)
        return RunRecord(
            cell=cell.label,
            case=plan.id,
            kind=plan.job,
            repeat=repeat,
            success=verdict.success,
            reasons=verdict.reasons,
            honest=None,
            injection_executed=False,
            seconds=round(outcome.seconds, 2),
            steps=len(usages),
            tokens_miss=miss,
            tokens_hit=hit,
            tokens_out=sum(u.output for u in usages),
            tokens_reasoning=sum(u.reasoning for u in usages),
            cost_usd=round(sum(c.cost_usd for c in llm.calls), 6),
            cache_share=round(hit / (hit + miss), 3) if hit + miss else 0.0,
            tools=[],
            sources=0,
            answer=verdict.facts[:700],
            error=outcome.error,
            questions=questions,
        )

    async def _repair(self, kit: Kit, env: Any, llm: FusedLlm) -> Outcome:
        """The story up to the break runs without a model; only the repair job is measured."""
        try:
            await env.login()  # the student is logged in, the first sync has data to confirm
            first = await env.sync()
            if first["status"] != "ok":
                raise SetupFailed(f"{self.id}: первая синхронизация не удалась: {first}")
            kit.move_timetable()
            env.services.clock.advance(BREAK_AFTER)
            noticed = await env.sync()
            jobs = env.repair_jobs()
            if noticed["status"] != "repairing" or len(jobs) != 1:
                raise SetupFailed(f"{self.id}: поломка не замечена: {noticed}, заданий {len(jobs)}")
            await env.rebrowse()  # the observing browser sees the university as it is now
            env.services.llm = llm
            return await timed(env.run(JobKind.REPAIR, jobs[0]["payload"]))
        finally:
            kit.reset()


def _plans() -> dict[str, LearningScenario]:
    return {sid: LearningScenario(plan) for sid, plan in load_plans().items()}


LEARNING_SCENARIOS: dict[str, LearningScenario] = _plans()
