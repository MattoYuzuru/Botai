"""Repair: when a university changes something, the agent re-learns the spec (FR-031, ADR-0005).

Three things start it: a sync that no longer matches the spec (`reason=drift`), the daily check on
a session (`mode=check`), and a student writing "the schedule is not showing" (`reason=chat`). The
job

1. runs the current spec on the student's session and decides, without a model, whether anything
   is really broken (an empty answer that is *really* empty is not);
2. if so, observes the site again, asks the model to rewrite only the broken endpoints (with the
   old endpoint and the problem in the prompt), lints and canaries the candidates, and compares
   what they read with the last confirmed snapshot by key fields;
3. applies the new revision when the data match, otherwise shows the student what is read now and
   asks; after applying, a sync verifies it and a bad revision is rolled back;
4. tells the student in a few lines what the university changed and what was fixed.

While it runs the old data stay on screen with the connection marked `repairing`.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import httpx

from botai.contracts.specs import Kind
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import Handler
from botai.core.http import RetryPolicy
from botai.core.jobs import JobCancelled, JobClaimLost, JobContext
from botai.core.services import Services
from botai.db import models as m
from botai.learn import horizon, revisions
from botai.learn.author import Candidate, author_digest, iso_z, merge_bootstrap
from botai.learn.canary import CanaryReport, compare, load_baseline, run_canary
from botai.learn.compiler import needs_recipe_v2
from botai.learn.explorer import TOKEN_BUDGET, Chosen, Explorer, kinds_text
from botai.learn.intent import LearningIntent
from botai.learn.lint import LintContext, lint_spec
from botai.learn.policies import bind_parts
from botai.sources import schedule
from botai.sources.adapter import AdapterResult, SessionExpired
from botai.sources.factory import AdapterFactory
from botai.sources.runner import (
    Drift,
    Snapshot,
    SyncOutcome,
    assess,
    baseline_counts,
    build_context,
    fetch_with_reauth,
    guard_source_write,
    load_snapshot,
    sync_connection,
)
from botai.sources.scope import scoped_spec
from botai.sources.session import load_session
from botai.sources.vocab import KIND_LABELS

log = logging.getLogger(__name__)


def endpoint_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What differs between two endpoints, in words, from spec data only (no personal values)."""
    lines: list[str] = []
    old_req, new_req = old["request"], new["request"]
    if old_req["path"] != new_req["path"]:
        lines.append(f"адрес запроса: `{old_req['path']}` → `{new_req['path']}`")
    if (old_req.get("query") or {}) != (new_req.get("query") or {}) or (
        old_req.get("body") or {}
    ) != (new_req.get("body") or {}):
        lines.append("параметры запроса")
    old_resp, new_resp = old["response"], new["response"]
    if old_resp["format"] != new_resp["format"]:
        names = {"json": "JSON", "html": "HTML"}
        lines.append(f"формат ответа: {names[old_resp['format']]} → {names[new_resp['format']]}")
    else:
        key = "itemsPath" if old_resp["format"] == "json" else "itemsSelector"
        if old_resp.get(key) != new_resp.get(key):
            lines.append(f"где лежит список: `{old_resp.get(key)}` → `{new_resp.get(key)}`")
    old_fields, new_fields = old_resp["fields"], new_resp["fields"]
    moved = [
        name for name in old_fields if name in new_fields and old_fields[name] != new_fields[name]
    ]
    if moved:
        lines.append("поля ответа: " + ", ".join(f"«{n}»" for n in moved[:6]))
    added = [n for n in new_fields if n not in old_fields]
    if added:
        lines.append("добавлены поля: " + ", ".join(f"«{n}»" for n in added[:6]))
    old_page, new_page = old.get("pagination") or {}, new.get("pagination") or {}
    if old_page != new_page:
        if new_page.get("type") == "dateWindow":
            lines.append(
                f"горизонт чтения: {new_page['windows']} окон по {new_page['stepDays']} дн., "
                "а не одно окно"
            )
        else:
            lines.append("постраничная выдача")
    return lines


class Repairer(Explorer):
    """Shares the observation, authoring and verification of the Explorer."""

    async def run(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        mode = "check" if payload.get("mode") == "check" else "repair"
        # The chat puts the student's own words in `reason`; only the known triggers are kept, so
        # free text never reaches the shared revision notes.
        raw_reason = payload.get("reason")
        reason = raw_reason if raw_reason in {"drift", "canary"} else "chat"
        async with self.services.db() as db:
            snap = await load_snapshot(db, self.ctx.user_id, connection_id)
        if snap.adapter != "recipe" or snap.status in {"disabled", "learning", "draft"}:
            return {"status": "skipped", "detail": snap.status}
        self.intent = LearningIntent(
            requested_kinds=snap.data_scope
            if snap.data_scope is not None
            else tuple(dict.fromkeys(e["kind"] for e in snap.spec["recipe"]["endpoints"]))
        )
        try:
            return await self._repair(snap, mode, reason)
        except (JobCancelled, asyncio.CancelledError):  # unanswered, or stopped by the student
            with contextlib.suppress(JobClaimLost):
                await self._leave_stale(snap, "Починка не завершена: задание остановлено")
            raise
        except BotaiError as err:
            if err.code in {ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.SOURCE_RATE_LIMITED}:
                await self._leave_stale(snap, err.detail, code=err.code.value, minutes=5)
                raise
            await self._leave_stale(snap, err.detail or "Починка не удалась", code=err.code.value)
            await self.ui.say(f"Исправить не получилось: {err.detail or 'ошибка'}.")
            return {"status": "error", "detail": err.code.value}

    # --- the job -----------------------------------------------------------------------------

    async def _repair(self, snap: Snapshot, mode: str, reason: str) -> dict[str, Any]:
        svc = self.services
        now = svc.clock.now()
        await self.ctx.status("Проверяю сайт")
        sctx = build_context(
            svc,
            snap,
            await load_session(svc.vault, snap.user_id, snap.connection_id),
            transport=self.transport,
            retry=self.retry,
        )
        fetched = await fetch_with_reauth(self.ctx, svc, snap, sctx)
        if isinstance(fetched, SyncOutcome):
            await self.ui.say("Не получается войти в кабинет — нужно войти заново.")
            return {"status": fetched.status, "detail": fetched.detail}
        async with svc.db() as db:
            counts = await baseline_counts(db, snap.connection_id, snap.last_sync_at)
            keys = await load_baseline(db, snap.connection_id, snap.last_sync_at, now)
        verdict = assess(scoped_spec(snap.spec, snap.data_scope), fetched, counts)
        drift = [*verdict.drift, *await self._horizon_gaps(snap, sctx, fetched, verdict.drift)]
        if not drift:
            return await self._healthy(snap, mode, reason)
        return await self._fix(snap, sctx, drift, keys, reason)

    async def _horizon_gaps(
        self, snap: Snapshot, sctx: Any, fetched: AdapterResult, known: list[Drift]
    ) -> list[Drift]:
        """A recipe that reads one window of dates is healthy in shape and still wrong: the
        neighbouring windows hold what it never sees (FR-039, F-03). The same endpoint is read
        over its neighbours; if that gives clearly more, the horizon is the defect."""
        now = self.services.clock.now()
        spec = scoped_spec(snap.spec, snap.data_scope)
        skip = {d.kind for d in known}
        gaps: list[Drift] = []
        for endpoint in spec["recipe"]["endpoints"]:
            kind: Kind = endpoint["kind"]
            wide = horizon.walked(endpoint, now, snap.tz)
            if kind in skip or wide is None:
                continue
            probe = copy.deepcopy(spec)
            probe["recipe"]["endpoints"] = [wide]
            probe["recipe"]["version"] = 2
            try:
                result = await AdapterFactory().create(scoped_spec(probe, (kind,))).fetch(sctx)
            except (SessionExpired, BotaiError):
                continue  # nothing to compare with: leave the verdict to the ordinary check
            if any(not outcome.ok for outcome in result.outcomes):
                continue  # the source ignores the moved date, or fails: no evidence of a gap
            current, wider = fetched.count(kind), result.count(kind)
            if wider > current and (current <= horizon.SPARSE or wider >= 2 * current):
                gaps.append(Drift(endpoint["id"], kind, "horizon"))
        return gaps

    async def _widened(
        self,
        kind: Kind,
        snap: Snapshot,
        endpoint: dict[str, Any],
        sctx: Any,
        revision: int,
        digest: str,
        lint_ctx: LintContext,
        keys: dict[Kind, Counter[tuple[str, ...]]],
    ) -> Chosen | None:
        """The stored endpoint read over neighbouring windows: no model, same auth and fields."""
        wide = horizon.walked(endpoint, self.services.clock.now(), snap.tz)
        if wide is None:
            return None
        candidate = Candidate(
            kind,
            wide,
            copy.deepcopy(snap.spec["recipe"].get("bootstrap", [])),
            snap.spec["recipe"]["auth"],
        )
        checked, _ = await self._verify_candidate(
            kind, candidate, 0, sctx, snap, revision, digest, lint_ctx
        )
        if checked is None:
            return None
        bound, report = checked
        return Chosen(
            kind, bound, report, compare(kind, keys.get(kind), report.valid.get(kind, []), snap.tz)
        )

    async def _record_canary(self, snap: Snapshot, *, ok: bool) -> None:
        async with self.services.db() as db, db.begin():
            connection = await guard_source_write(
                db, self.ctx, snap.user_id, snap.connection_id, origin=snap.origin
            )
            if connection is None or connection.status == "disabled":
                return
            row = await db.get(m.AdapterSpecRow, snap.spec_id)
            if row is not None:
                await revisions.record(
                    db,
                    revisions.AUDIT_CANARY,
                    row,
                    self.services.clock.now(),
                    user_id=snap.user_id,
                    ok=ok,
                    via="check",
                )

    async def _healthy(self, snap: Snapshot, mode: str, reason: str) -> dict[str, Any]:
        await self._record_canary(snap, ok=True)
        if mode == "check" and snap.status in {"active", "syncing"}:
            return {"status": "healthy", "mode": "check"}
        await self._set(snap.connection_id, status="active", clear_error=True)
        outcome = await sync_connection(
            self.ctx,
            self.services,
            snap.connection_id,
            transport=self.transport,
            retry=self.retry,
            accept_empty=True,
            queue_repair=False,
        )
        counts = kinds_text(outcome.kinds)
        if outcome.status == "ok":
            tail = f" Сейчас в данных — {counts}." if counts else " Записей сейчас нет."
            await self.ui.say(
                "Проверено: сайт отвечает так же, как раньше, ничего чинить не нужно." + tail
            )
        return {"status": "healthy" if outcome.status == "ok" else outcome.status}

    async def _fix(
        self,
        snap: Snapshot,
        sctx: Any,
        drift: list[Drift],
        keys: Any,
        reason: str,
    ) -> dict[str, Any]:
        ui, svc = self.ui, self.services
        await self._set(snap.connection_id, status="repairing")
        await self._record_canary(snap, ok=False)
        broken = _kinds_of(drift, snap.spec)
        stage = await ui.stage(
            "Сайт читается иначе, чем раньше",
            "Не читается: " + ", ".join(KIND_LABELS[k].lower() for k in broken),
        )
        await stage.finish("ok")

        redactor = await self._redactor(snap)
        obs = await self._observe(snap, sctx.session, redactor, [])
        if self._observed_session is not None:
            sctx.session = self._observed_session
        lint_ctx = LintContext(
            allowed_hosts=snap.allow_hosts, expected_origin=snap.origin, observed=obs.observed
        )
        async with svc.db() as db:
            revision = await revisions.latest_revision(db, snap.origin, "recipe") + 1
        digest = author_digest(snap.user_id)
        endpoints = {e["kind"]: e for e in snap.spec["recipe"]["endpoints"]}
        fixed: dict[Kind, Chosen] = {}
        unresolved: list[Kind] = []
        empty: list[Kind] = []
        text = obs.render()
        for kind in broken:
            problem = _problem_for(kind, drift)
            if obs.empty or self.tokens > TOKEN_BUDGET:
                unresolved.append(kind)
                continue
            chosen = (
                await self._widened(
                    kind, snap, endpoints[kind], sctx, revision, digest, lint_ctx, keys
                )
                if problem == "horizon"
                else None
            ) or await self._author_and_verify(
                kind,
                text,
                lint_ctx,
                sctx,
                snap,
                revision,
                digest,
                previous=endpoints[kind],
                problem=problem,
                baseline=keys,
            )
            if chosen is not None:
                fixed[kind] = chosen
            elif problem == "empty":
                empty.append(kind)  # the model sees nothing better: the source may be really empty
            else:
                unresolved.append(kind)

        if not fixed:
            return await self._without_new_revision(snap, empty, unresolved)

        merged = _merge(snap.spec, fixed, revision, svc.clock.now(), digest)
        new_spec, dropped = merged
        unresolved.extend(k for k in dropped if k not in unresolved)
        if new_spec is None or not fixed:
            return await self._without_new_revision(snap, empty, unresolved)
        verdict = lint_spec(new_spec, lint_ctx)
        if not verdict.ok:
            await ui.note(
                "Проверка отклонила исправленное описание",
                "; ".join(verdict.reasons()[:3]),
                "failed",
            )
            return await self._without_new_revision(snap, empty, [*unresolved, *fixed])
        try:
            report = await run_canary(
                scoped_spec(new_spec, snap.data_scope), sctx, require=list(fixed)
            )
        except SessionExpired:
            raise BotaiError(ErrorCode.SOURCE_AUTH_REQUIRED, "Сайт не принял сессию") from None
        if not _fixed_ok(report, fixed, new_spec):
            await ui.note(
                "Исправленное описание не прошло проверку", "; ".join(report.problems[:3]), "failed"
            )
            return await self._without_new_revision(snap, empty, [*unresolved, *fixed])

        if any(c.comparison is None or c.comparison.verdict != "match" for c in fixed.values()):
            await self._show_summary(snap, fixed, report, title="Вот что читается теперь")
            if not await ui.confirm(
                "Данные изменились по сравнению с прежними. Это ваши данные? "
                "После подтверждения описание будет обновлено.",
                "Да, это они",
                "Нет, не то",
            ):
                await ui.say("Хорошо, прежнее описание остаётся, ничего не меняю.")
                return await self._without_new_revision(
                    snap, empty, [*unresolved, *fixed], say=False
                )
        return await self._apply(snap, new_spec, fixed, unresolved, empty, digest, reason)

    # --- outcomes ----------------------------------------------------------------------------

    async def _without_new_revision(
        self,
        snap: Snapshot,
        empty: Sequence[Kind],
        unresolved: Sequence[Kind],
        *,
        say: bool = True,
    ) -> dict[str, Any]:
        """Nothing better was found. If the only trouble was an empty answer, the source is
        simply empty now; otherwise the student is told what could not be fixed."""
        if not unresolved:
            await self._set(snap.connection_id, status="active", clear_error=True)
            outcome = await sync_connection(
                self.ctx,
                self.services,
                snap.connection_id,
                transport=self.transport,
                retry=self.retry,
                accept_empty=True,
                queue_repair=False,
            )
            if say:
                labels = ", ".join(KIND_LABELS[k].lower() for k in empty)
                await self.ui.say(
                    f"Сайт отвечает как раньше, но записей сейчас нет: {labels}. "
                    "Возможно, каникулы или семестр ещё не начался. Данные появятся сами."
                )
            return {"status": outcome.status, "empty": list(empty)}
        now = self.services.clock.now()
        await self._set(
            snap.connection_id,
            status="stale",
            error=(ErrorCode.RECIPE_DRIFT.value, "Не удалось подобрать новое описание"),
            next_sync_at=now + schedule.RETRY_AFTER_REPAIR,
        )
        if say:
            labels = ", ".join(KIND_LABELS[k].lower() for k in unresolved)
            await self.ui.say(
                f"Сайт изменился, и подобрать новое описание не удалось: {labels}. "
                "Прежние данные остаются на экране с пометкой, что они устарели. "
                "Исправление будет повторено позже."
            )
        return {"status": "unresolved", "kinds": list(unresolved)}

    async def _apply(
        self,
        snap: Snapshot,
        spec: dict[str, Any],
        fixed: dict[Kind, Chosen],
        unresolved: list[Kind],
        empty: list[Kind],
        digest: str,
        reason: str,
    ) -> dict[str, Any]:
        ui, svc = self.ui, self.services
        stage = await ui.stage("Сохраняю исправленное описание")
        now = svc.clock.now()
        async with svc.db() as db, db.begin():
            conn = await guard_source_write(
                db, self.ctx, snap.user_id, snap.connection_id, origin=snap.origin
            )
            if conn is None or conn.status == "disabled":
                return {"status": "skipped", "detail": "disabled"}
            row = await revisions.add_revision(
                db,
                origin=snap.origin,
                adapter="recipe",
                spec=spec,
                state="canary",
                digest=digest,
                notes=f"repair: {reason}",
            )
            old = await revisions.activate(db, row, now=now)
            conn.spec_id, conn.spec_revision, conn.status = row.id, row.revision, "active"
            new_id, new_revision = row.id, row.revision
        await stage.finish("ok", f"Версия описания: {new_revision}")

        outcome = await sync_connection(
            self.ctx,
            svc,
            snap.connection_id,
            transport=self.transport,
            retry=self.retry,
            accept_empty=True,
            queue_repair=False,
        )
        bad = [d for d in outcome.drift if d.kind in fixed]
        if outcome.status not in {"ok", "repairing", "stale"} or bad:
            return await self._roll_back(snap, new_id, old, outcome)

        old_endpoints = {e["kind"]: e for e in snap.spec["recipe"]["endpoints"]}
        lines: list[str] = []
        for kind, ch in fixed.items():
            changes = endpoint_changes(old_endpoints[kind], ch.candidate.endpoint)
            lines.append(
                f"- {KIND_LABELS[kind].lower()}: " + ("; ".join(changes) if changes else "разметка")
            )
        counts = kinds_text(outcome.kinds, only=set(fixed))
        message = "**Исправлено.** Сайт поменялся, описание обновлено:\n" + "\n".join(lines)
        if counts:
            message += f"\n\nДанные на месте — {counts}."
        if unresolved:
            await self._set(
                snap.connection_id,
                status="stale",
                error=(ErrorCode.RECIPE_DRIFT.value, "Часть данных не удалось починить"),
                next_sync_at=now + schedule.RETRY_AFTER_REPAIR,
            )
            message += (
                "\n\nНе получилось починить: "
                + ", ".join(KIND_LABELS[k].lower() for k in unresolved)
                + ". Эти данные остаются прежними, исправление будет повторено позже."
            )
        await ui.say(message)
        return {
            "status": "repaired",
            "revision": new_revision,
            "fixed": sorted(fixed),
            "unresolved": list(unresolved),
            "empty": list(empty),
        }

    async def _roll_back(
        self, snap: Snapshot, new_id: str, old: m.AdapterSpecRow | None, outcome: SyncOutcome
    ) -> dict[str, Any]:
        svc = self.services
        now = svc.clock.now()
        async with svc.db() as db, db.begin():
            conn = await guard_source_write(
                db, self.ctx, snap.user_id, snap.connection_id, origin=snap.origin
            )
            if conn is None or conn.status == "disabled":
                return {"status": "skipped", "detail": "disabled"}
            row = await db.get(m.AdapterSpecRow, new_id)
            previous = await revisions.rollback(db, row, now=now) if row is not None else None
            if previous is not None:
                conn.spec_id, conn.spec_revision = previous.id, previous.revision
        await self._set(
            snap.connection_id,
            status="stale",
            error=(
                ErrorCode.RECIPE_DRIFT.value,
                "Новое описание не подтвердилось на синхронизации",
            ),
            next_sync_at=now + schedule.RETRY_AFTER_REPAIR,
        )
        await self.ui.say(
            "Новое описание не подтвердилось при проверке данных, поэтому возвращено прежнее. "
            "Прежние данные остаются на экране, исправление будет повторено позже."
        )
        return {"status": "rolled_back", "sync": outcome.as_dict()}

    async def _leave_stale(
        self, snap: Snapshot, detail: str, *, code: str = "recipe_drift", minutes: int = 60
    ) -> None:
        now = self.services.clock.now()
        await self._set(
            snap.connection_id,
            status="stale",
            error=(code, detail),
            next_sync_at=now + timedelta(minutes=minutes),
        )


# --- helpers --------------------------------------------------------------------------------


def _kinds_of(drift: list[Drift], spec: dict[str, Any]) -> list[Kind]:
    present = {e["kind"] for e in spec["recipe"]["endpoints"]}
    kinds: list[Kind] = []
    for d in drift:
        if d.kind and d.kind in present and d.kind not in kinds:
            kinds.append(d.kind)
    return kinds


def _problem_for(kind: Kind, drift: list[Drift]) -> str:
    problems = [d.problem for d in drift if d.kind == kind]
    structural = [p for p in problems if p != "empty"]
    return (structural or problems or ["format"])[0]


def _fixed_ok(report: CanaryReport, fixed: dict[Kind, Chosen], spec: dict[str, Any]) -> bool:
    kinds = set(fixed)
    ids = {e["id"] for e in spec["recipe"]["endpoints"] if e["kind"] in kinds}
    if report.result is None:
        return False
    if any(o.structural and o.endpoint_id in ids for o in report.result.outcomes):
        return False
    return all(report.count(k) >= 1 for k in kinds)


def _merge(
    spec: dict[str, Any], fixed: dict[Kind, Chosen], revision: int, now: Any, digest: str
) -> tuple[dict[str, Any] | None, list[Kind]]:
    """The old spec with the fixed endpoints swapped in. A fix whose bootstrap contradicts the
    spec's own is dropped (and reported), the others go ahead.

    Fixed endpoints carry executor-compiled auth bindings. The endpoints that stay keep the
    authority they always had, now written as bindings too, and the recipe-wide policy is
    rebuilt from the bindings that are still referenced."""
    new = copy.deepcopy(spec)
    recipe = new["recipe"]
    bootstrap: list[dict[str, Any]] = list(recipe.get("bootstrap", []))
    dropped: list[Kind] = []
    bound = recipe["auth"].get("type") == "session" or any(
        c.candidate.auth.get("type") == "session" for c in fixed.values()
    )
    bindings: dict[str, dict[str, Any]] = {}
    if bound:
        retained = [e for e in recipe["endpoints"] if e["kind"] not in fixed]
        bindings.update(bind_parts(recipe["auth"], retained, bootstrap))
    for kind in list(fixed):
        candidate_bootstrap = copy.deepcopy(fixed[kind].candidate.bootstrap)
        replacement = copy.deepcopy(fixed[kind].candidate.endpoint)
        if bound:
            bindings.update(
                bind_parts(fixed[kind].candidate.auth, [replacement], candidate_bootstrap)
            )
        merged = merge_bootstrap(bootstrap, candidate_bootstrap)
        if merged is None:
            dropped.append(kind)
            del fixed[kind]
            continue
        bootstrap = merged
        for i, endpoint in enumerate(recipe["endpoints"]):
            if endpoint["kind"] == kind:
                replacement["id"] = endpoint["id"]
                recipe["endpoints"][i] = replacement
        if not bound:
            auths = {tuple(sorted(c.candidate.auth.items())) for c in fixed.values()}
            if len(auths) == 1 and fixed[kind].candidate.auth != recipe["auth"]:
                recipe["auth"] = copy.deepcopy(fixed[kind].candidate.auth)
    if not fixed:
        return None, dropped
    if bootstrap:
        recipe["bootstrap"] = bootstrap
    if bound:
        referenced = {step.get("authBinding") for step in bootstrap}
        referenced.update(
            e["request"].get("authBinding")
            for e in recipe["endpoints"]
            if not e["request"].get("render")
        )
        live = {name: value for name, value in bindings.items() if name in referenced}
        recipe["auth"] = (
            {"type": "session", "version": 1, "bindings": live} if live else {"type": "cookie"}
        )
    if needs_recipe_v2(recipe["endpoints"], bootstrap, recipe["auth"]):
        recipe["version"] = 2
    recipe["revision"] = revision
    recipe["learnedAt"] = iso_z(now)
    recipe["learnedByDigest"] = digest
    return new, dropped


# --- job handler ----------------------------------------------------------------------------


def make_repair_handler(
    *, transport: httpx.AsyncBaseTransport | None = None, retry: RetryPolicy | None = None
) -> Handler:
    async def handle(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        connection_id = ctx.connection_id or str(payload.get("connection_id", ""))
        if not connection_id:
            raise BotaiError(ErrorCode.VALIDATION, "У задания починки нет подключения")
        repairer = Repairer(ctx, services, transport=transport, retry=retry)
        return await repairer.run(connection_id, payload)

    return handle
