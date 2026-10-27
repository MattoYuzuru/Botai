"""Explorer: the model as the *author of a spec*, not a free browsing agent (ADR-0005).

For a new university the job does, in this order:

1. get a session (`services.sessions.ensure`, W2: mirrored login form, saved password, fallback);
2. observe deterministically: the home page, a bounded number of same-origin menu links, the
   network log; personal values are replaced by placeholders before anything is shown to a model;
3. ask the model for one kind of data at a time, with the recipe schema as the response format;
4. lint every candidate, run it on this very session (canary), and ask the student only when
   several candidates give different data;
5. show what was collected and, once the student confirms, store the spec as an active revision,
   attach it to the connection and run the first sync.

Nothing the model writes is executed. If nothing is found the student is asked, honestly, for a link
to the page that shows the data. A second student of the same university finds the active spec and
skips the exploration (FR-009).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from botai.contracts.agent_ui import SummaryGroup
from botai.contracts.specs import Kind
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import RetryPolicy
from botai.core.jobs import JobCancelled, JobContext
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn import revisions
from botai.learn.author import (
    AuthorError,
    Candidate,
    author_digest,
    author_kind,
    build_spec,
    candidate_spec,
    merge_bootstrap,
)
from botai.learn.canary import CanaryReport, Comparison, compare, run_canary
from botai.learn.lint import LintContext, lint_spec
from botai.learn.observe import Observations, observe
from botai.learn.redact import Redactor
from botai.learn.registry import find_warning, update_university
from botai.learn.ui import Ui, describe, item, kinds_text, sample_of
from botai.sources.adapter import SessionExpired, SyncContext
from botai.sources.runner import (
    ConnInfo,
    SyncOutcome,
    context_for,
    load_conn_info,
    load_snapshot,
    set_connection,
    sync_connection,
)
from botai.sources.vocab import KIND_LABELS, VOCAB

log = logging.getLogger(__name__)

DEFAULT_KINDS: tuple[Kind, ...] = ("scheduleEvents", "deadlines")
TOKEN_BUDGET = 1_500_000
MAX_LINK_ROUNDS = 2


@dataclass(slots=True)
class Chosen:
    kind: Kind
    candidate: Candidate
    report: CanaryReport
    comparison: Comparison | None = None


def origin_from(url: str) -> str | None:
    """`https://host` of a link, or None: only plain https origins can be connected."""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.port not in (None, 443):
        return None
    return f"https://{host}"


def wanted_kinds(payload: dict[str, Any]) -> list[Kind]:
    raw = payload.get("kinds")
    kinds: list[Kind] = []
    if isinstance(raw, list):
        for k in raw:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(k, str) and k in VOCAB and k not in kinds:
                kinds.append(k)  # type: ignore[arg-type]
    return kinds or list(DEFAULT_KINDS)


class Explorer:
    def __init__(
        self,
        ctx: JobContext,
        services: Services,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.ctx = ctx
        self.services = services
        self.ui = Ui(ctx, services.clock)
        self.transport = transport
        self.retry = retry
        self.tokens = 0

    # --- small helpers -----------------------------------------------------------------------

    async def _info(self, connection_id: str) -> ConnInfo:
        async with self.services.db() as db:
            return await load_conn_info(db, self.ctx.user_id, connection_id)

    async def _set(self, connection_id: str, **values: Any) -> None:
        await set_connection(self.services, connection_id, self.ctx.user_id, **values)

    def _sync_context(self, info: ConnInfo, session: SessionState | None) -> SyncContext:
        return context_for(self.services, info, session, transport=self.transport, retry=self.retry)

    async def _redactor(self, info: ConnInfo) -> Redactor:
        r = Redactor()
        r.know(*info.profile.values())
        vault = self.services.vault
        login = await vault.get(info.user_id, info.connection_id, SecretKind.LOGIN)
        if login is not None:
            r.know(login.get_secret_value())
        password = await vault.get(info.user_id, info.connection_id, SecretKind.PASSWORD)
        if password is not None:  # never shown to a model; replaced if a page ever echoes it
            r.known.add(password.get_secret_value())
            r.observed.add(password.get_secret_value())
        return r

    # --- entry points ------------------------------------------------------------------------

    async def connect(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        info = await self._info(connection_id)
        if info.status == "disabled":
            return {"status": "skipped", "detail": "disabled"}
        await self._set(connection_id, status="learning", clear_error=True)
        try:
            origin = await self._resolve_origin(info, payload)
            if origin is None:
                return await self._fail(
                    info, "Не получилось определить адрес сайта", ErrorCode.VALIDATION
                )
            info = await self._info(connection_id)
            reused = await self._reuse(info)
            if reused is not None:
                return reused
            return await self._explore(info, wanted_kinds(payload), base_spec=None)
        except (JobCancelled, asyncio.CancelledError):
            # nobody answered in time (JobCancelled), or the student stopped the job and the
            # runner cancelled its task (CancelledError): either way the setup is not running
            await self._set(connection_id, status="draft")
            raise
        except BotaiError as err:
            return await self._fail(info, err.detail or "Не удалось подключить сайт", err.code)

    async def learn(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Teach an existing connection more kinds of data; the other endpoints are kept."""
        async with self.services.db() as db:
            snap = await load_snapshot(db, self.ctx.user_id, connection_id)
        if snap.adapter != "recipe":
            return {"status": "skipped", "detail": "adapter"}
        try:
            return await self._explore(snap, wanted_kinds(payload), base_spec=snap.spec)
        except BotaiError as err:
            await self.ui.say(err.detail or "Не удалось выучить новые данные.")
            return {"status": "error", "detail": err.code.value}

    # --- origin ------------------------------------------------------------------------------

    async def _resolve_origin(self, info: ConnInfo, payload: dict[str, Any]) -> str | None:
        origin: str | None = None
        link = payload.get("url")
        if isinstance(link, str) and link.strip():
            origin = origin_from(link)
        origin = origin or (info.origin or None)
        if origin is None and info.university_id:
            async with self.services.db() as db:
                uni = await db.get(m.University, info.university_id)
            known = [o for o in (uni.origins if uni else []) if origin_from(o)]
            if len(known) == 1:
                origin = known[0]
        for _ in range(2):
            if origin is not None:
                break
            answer = await self.ui.ask_url(
                "Какой сайт подключить? Нужна ссылка на личный кабинет, расписание или LMS."
            )
            origin = origin_from(answer) if answer else None
            if origin is None:
                await self.ui.say("Подойдёт только адрес вида https://example.org без порта.")
        if origin is None:
            return None
        host = urlsplit(origin).hostname or ""
        hosts = sorted({*info.allow_hosts, host})
        await self._set(
            info.connection_id, origin=origin, allow_hosts=hosts, title=info.title or host
        )
        return origin

    # --- reuse (FR-009) ----------------------------------------------------------------------

    async def _reuse(self, info: ConnInfo) -> dict[str, Any] | None:
        async with self.services.db() as db:
            row = await revisions.active_row(db, info.origin, "recipe")
        if row is None:
            return None
        stage = await self.ui.stage(
            "Для этого сайта описание уже есть",
            "Подключаю по готовому описанию, без повторного разбора",
        )
        host = urlsplit(info.origin).hostname or ""
        await self._set(
            info.connection_id,
            adapter="recipe",
            spec_id=row.id,
            spec_revision=row.revision,
            allow_hosts=sorted({*info.allow_hosts, host}),
            status="active",
        )
        outcome = await sync_connection(
            self.ctx, self.services, info.connection_id, transport=self.transport, retry=self.retry
        )
        await stage.finish("ok" if outcome.status == "ok" else "failed")
        await self._report_sync(outcome, prefix="Готово.")
        await self._registry(info, row.spec["recipe"]["origin"], None)
        return {"status": outcome.status, "reused": True, "revision": row.revision}

    async def _report_sync(self, outcome: SyncOutcome, *, prefix: str) -> None:
        if outcome.status == "ok":
            what = kinds_text(outcome.kinds) or "данные пока не найдены"
            await self.ui.say(f"{prefix} Подключено: {what}. Дальше всё обновляется само.")
        elif outcome.status == "repairing":
            await self.ui.say(
                f"{prefix} Сайт ответил иначе, чем описано. Описание уточняется, "
                "а прежние данные остаются на месте."
            )
        elif outcome.status == "needs_reauth":
            await self.ui.say("Нужно войти заново — вход не подтвердился.")
        else:
            await self.ui.say("Данные пока загрузить не удалось, попробую снова позже.")

    # --- the exploration ---------------------------------------------------------------------

    async def _explore(
        self, info: ConnInfo, kinds: list[Kind], *, base_spec: dict[str, Any] | None
    ) -> dict[str, Any]:
        ui, svc = self.ui, self.services
        stage = await ui.stage("Вхожу в кабинет")
        session = await svc.sessions.ensure(
            self.ctx, user_id=info.user_id, connection_id=info.connection_id
        )
        await stage.finish()
        redactor = await self._redactor(info)
        sctx = self._sync_context(info, session)
        digest = author_digest(info.user_id)
        async with svc.db() as db:
            revision = await revisions.latest_revision(db, info.origin, "recipe") + 1

        chosen: dict[Kind, Chosen] = {}
        pending = list(kinds)
        extra: list[str] = []
        warning: str | None = None
        lint_ctx = LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin)
        for round_no in range(MAX_LINK_ROUNDS + 1):
            obs = await self._observe(info, session, redactor, extra)
            if round_no == 0 and not obs.empty:
                warning, spent = await find_warning(svc.llm, obs.plain_text)
                self.tokens += spent
            if not obs.empty:
                lint_ctx = LintContext(
                    allowed_hosts=info.allow_hosts,
                    expected_origin=info.origin,
                    observed=obs.observed,
                )
                text = obs.render()
                for kind in list(pending):
                    if self.tokens > TOKEN_BUDGET:
                        break
                    found = await self._author_and_verify(
                        kind, text, lint_ctx, sctx, info, revision, digest
                    )
                    if found is not None:
                        chosen[kind] = found
                        pending.remove(kind)
            if not pending or round_no == MAX_LINK_ROUNDS:
                break
            link = await self._ask_page(info, pending, have_some=bool(chosen))
            if link is None:
                break
            extra.append(link)

        if not chosen:
            await ui.say(
                "Не нашла в кабинете "
                + ", ".join(KIND_LABELS[k].lower() for k in pending)
                + ". Ничего не сохраняю. Если пришлёте ссылку на страницу, где это видно, "
                "попробую ещё раз."
            )
            return await self._fail(
                info, "В кабинете не найдены нужные данные", ErrorCode.NOT_FOUND, say=False
            )

        built = await self._assemble(info, chosen, base_spec, revision, digest, sctx, lint_ctx)
        if built is None:
            return await self._fail(
                info, "Собранное описание не прошло проверку", ErrorCode.RECIPE_REJECTED
            )
        spec, report = built
        if pending:
            await ui.say(
                "Не нашла: "
                + ", ".join(KIND_LABELS[k].lower() for k in pending)
                + ". Остальное подключаю, к этому можно вернуться позже."
            )
        await self._show_summary(info, chosen, report)
        confirmed = await ui.confirm(
            "Всё верно? После подтверждения данные будут обновляться автоматически.",
            "Да, всё верно",
            "Нет, не то",
        )
        if not confirmed:
            await ui.say("Хорошо, ничего не сохраняю.")
            return await self._fail(
                info, "Собранное не подтверждено", ErrorCode.RECIPE_REJECTED, say=False
            )
        return await self._activate(info, spec, chosen, warning, digest, base_spec is not None)

    async def _observe(
        self, info: ConnInfo, session: SessionState, redactor: Redactor, extra: list[str]
    ) -> Observations:
        stage = await self.ui.stage("Смотрю страницы кабинета")
        handle = await self.services.browser.open(
            user_id=info.user_id,
            connection_id=info.connection_id,
            allow_hosts=info.allow_hosts,
            session=session,
        )
        try:
            obs = await observe(
                handle, info.origin, redactor, start_urls=[info.origin + "/", *extra]
            )
        finally:
            await handle.close()
        await stage.finish(
            "ok" if not obs.empty else "failed",
            f"Страниц: {len(obs.pages)}, запросов: {len(obs.calls)}",
        )
        if obs.injections:
            await self.ui.note(
                "Найден текст, похожий на указание",
                "Он проигнорирован: содержимое страниц — это данные, а не команды.",
                "skipped",
            )
            async with self.services.db() as db, db.begin():
                db.add(
                    m.AuditLog(
                        user_id=info.user_id,
                        kind="injection_blocked",
                        detail={
                            "source": "learn",
                            "connection_id": info.connection_id,
                            "count": obs.injections,
                        },
                    )
                )
        return obs

    async def _author_and_verify(
        self,
        kind: Kind,
        observation: str,
        lint_ctx: LintContext,
        sctx: SyncContext,
        info: ConnInfo,
        revision: int,
        digest: str,
        *,
        previous: dict[str, Any] | None = None,
        problem: str | None = None,
        baseline: dict[Kind, Counter[tuple[str, ...]]] | None = None,
    ) -> Chosen | None:
        """Author, lint and canary the candidates of one kind and pick one. In a repair
        (`previous` given) each candidate is also compared with the confirmed snapshot."""
        ui = self.ui
        label = KIND_LABELS[kind].lower()
        stage = await ui.stage(f"Разбираю: {label}" if previous is None else f"Чиню: {label}")
        try:
            authored = await author_kind(
                self.services.llm,
                kind,
                observation,
                lint_ctx=lint_ctx,
                origin=info.origin,
                revision=revision,
                now=self.services.clock.now(),
                digest=digest,
                previous=previous,
                problem=problem,
            )
        except AuthorError:
            await stage.finish("failed", "Модель не вернула пригодный ответ")
            return None
        self.tokens += authored.tokens
        if not authored.candidates:
            reason = (
                "Проверка отклонила все варианты"
                if authored.rejected
                else "Подходящих данных не видно"
            )
            await stage.finish("skipped" if not authored.rejected else "failed", reason)
            return None
        verified: list[Chosen] = []
        for i, cand in enumerate(authored.candidates):
            spec = candidate_spec(
                cand,
                origin=info.origin,
                revision=revision,
                now=self.services.clock.now(),
                digest=digest,
                index=i,
            )
            try:
                report = await run_canary(spec, sctx, require={kind})
            except SessionExpired:
                raise BotaiError(
                    ErrorCode.SOURCE_AUTH_REQUIRED, "Сайт не принял сессию сразу после входа"
                ) from None
            if report.ok:
                comparison = (
                    compare(kind, baseline.get(kind), report.valid.get(kind, []), sctx.tz)
                    if baseline is not None
                    else None
                )
                verified.append(Chosen(kind, cand, report, comparison))
        if not verified:
            await stage.finish("failed", "Проверка на вашей сессии не прошла")
            return None
        picked = await self._pick(kind, verified, sctx.tz)
        note = f"Найдено записей: {picked.report.count(kind)}"
        if picked.comparison is not None and picked.comparison.baseline:
            c = picked.comparison
            note += f"; совпало с прежними данными: {c.matched} из {c.baseline}"
        await stage.finish("ok", note)
        return picked

    async def _pick(self, kind: Kind, verified: list[Chosen], tz: ZoneInfo) -> Chosen:
        """One candidate. A repair prefers the one that reads the confirmed data again; several
        candidates that read *different* data are a question for the student."""
        if len(verified) == 1:
            return verified[0]
        matching = [c for c in verified if c.comparison and c.comparison.verdict == "match"]
        if matching:
            return max(matching, key=lambda c: c.comparison.ratio if c.comparison else 0.0)
        id_sets = [frozenset(r.external_id for r in c.report.valid[kind]) for c in verified]
        if all(s == id_sets[0] for s in id_sets):
            return verified[0]  # the same data twice
        options: list[tuple[str, str, str | None]] = []
        now = self.services.clock.now()
        for i, cand in enumerate(verified):
            samples = [
                "{}: {}".format(*describe(r, tz))
                for r in sample_of(cand.report.valid[kind], kind, now)[:2]
            ]
            options.append(
                (str(i), f"Вариант {i + 1}: {cand.report.count(kind)} записей", "; ".join(samples))
            )
        answer = await self.ui.choose(
            f"Нашлось несколько источников для раздела «{KIND_LABELS[kind].lower()}». "
            "Какой из них ваш?",
            options,
        )
        try:
            idx = int(answer or "0")
        except ValueError:
            idx = 0
        return verified[idx if 0 <= idx < len(verified) else 0]

    async def _ask_page(
        self, info: ConnInfo, pending: list[Kind], *, have_some: bool
    ) -> str | None:
        labels = ", ".join(KIND_LABELS[k].lower() for k in pending)
        if have_some:
            choice = await self.ui.choose(
                f"Не нашла: {labels}. Можно дать ссылку на страницу, где это видно.",
                [("link", "Дать ссылку", None), ("skip", "Пропустить", "Это можно добавить позже")],
            )
            if choice != "link":
                return None
        answer = await self.ui.ask_url(f"Дайте ссылку на страницу, где видно: {labels}.")
        if not answer:
            return None
        parts = urlsplit(answer if "://" in answer else "https://" + answer)
        if f"https://{(parts.hostname or '').lower()}" != info.origin:
            await self.ui.say(
                "Пока можно подключать только страницы того же сайта, что и кабинет. "
                f"Адрес {parts.hostname or '—'} не подходит."
            )
            return None
        return f"{info.origin}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")

    # --- the final spec ----------------------------------------------------------------------

    async def _assemble(
        self,
        info: ConnInfo,
        chosen: dict[Kind, Chosen],
        base_spec: dict[str, Any] | None,
        revision: int,
        digest: str,
        sctx: SyncContext,
        lint_ctx: LintContext,
    ) -> tuple[dict[str, Any], CanaryReport] | None:
        stage = await self.ui.stage("Проверяю описание целиком")
        endpoints: list[dict[str, Any]] = []
        bootstrap: list[dict[str, Any]] = []
        auth: dict[str, Any] | None = None
        require: list[Kind] = []
        if base_spec is not None:
            base = base_spec["recipe"]
            endpoints = [e for e in copy.deepcopy(base["endpoints"]) if e["kind"] not in chosen]
            bootstrap = copy.deepcopy(base.get("bootstrap", []))
            auth = copy.deepcopy(base["auth"])
            require = [k for k in base["validation"]["requireKinds"] if k not in chosen]
        for kind, ch in chosen.items():
            merged = merge_bootstrap(bootstrap, copy.deepcopy(ch.candidate.bootstrap))
            if merged is None:
                await self.ui.note(
                    f"Пропускаю: {KIND_LABELS[kind].lower()}",
                    "Шаги подготовки этого раздела противоречат уже выбранным.",
                    "skipped",
                )
                continue
            bootstrap = merged
            endpoint = copy.deepcopy(ch.candidate.endpoint)
            endpoint["id"] = f"{kind}1"
            endpoints.append(endpoint)
            require.append(kind)
            auth = auth or ch.candidate.auth
        if not endpoints:
            await stage.finish("failed")
            return None
        spec = build_spec(
            origin=info.origin,
            revision=revision,
            now=self.services.clock.now(),
            digest=digest,
            endpoints=endpoints,
            bootstrap=bootstrap,
            auth=auth or {"type": "cookie"},
            require_kinds=require,
        )
        verdict = lint_spec(spec, lint_ctx)  # with the same observations the candidates met
        if not verdict.ok:
            await stage.finish("failed", "; ".join(verdict.reasons()[:3]))
            return None
        try:
            report = await run_canary(spec, sctx, require=[k for k in require if k in chosen])
        except SessionExpired:
            await stage.finish("failed", "Сайт не принял сессию")
            return None
        if not report.ok:
            await stage.finish("failed", "; ".join(report.problems[:3]))
            return None
        await stage.finish("ok")
        return spec, report

    async def _show_summary(
        self,
        info: ConnInfo,
        chosen: dict[Kind, Chosen],
        report: CanaryReport,
        *,
        title: str = "Вот что удалось собрать",
    ) -> None:
        now = self.services.clock.now()
        groups: list[SummaryGroup] = []
        for kind in chosen:
            records = report.valid.get(kind, [])
            items = [item("Найдено записей", str(len(records)))]
            for rec in sample_of(records, kind, now):
                label, value = describe(rec, info.tz)
                items.append(item(label, value or "—", _safe_url(rec.source_url)))
            groups.append(SummaryGroup(title=KIND_LABELS[kind], items=items))
        groups.append(
            SummaryGroup(title="Источник", items=[item("Сайт", info.origin, info.origin)])
        )
        await self.ui.summary(title, groups)

    async def _activate(
        self,
        info: ConnInfo,
        spec: dict[str, Any],
        chosen: dict[Kind, Chosen],
        warning: str | None,
        digest: str,
        relearn: bool,
    ) -> dict[str, Any]:
        stage = await self.ui.stage("Сохраняю описание")
        host = urlsplit(info.origin).hostname or ""
        async with self.services.db() as db, db.begin():
            row = await revisions.add_revision(
                db, origin=info.origin, adapter="recipe", spec=spec, state="canary", digest=digest
            )
            await revisions.activate(db, row, now=self.services.clock.now())
            conn = await db.get(m.Connection, info.connection_id)
            if conn is not None and conn.status != "disabled":
                conn.adapter = "recipe"
                conn.spec_id = row.id
                conn.spec_revision = row.revision
                known_hosts: list[str] = list(conn.allow_hosts or [])
                conn.allow_hosts = sorted({*known_hosts, host})
                conn.status = "active"
                conn.error_code = conn.error_detail = conn.error_at = None
            await update_university(db, info.university_id, info.origin, warning)
            revision = row.revision
        await stage.finish("ok", f"Версия описания: {revision}")
        if warning:
            await self.ui.say(f"**Важно.** {warning} Подключение работает, но имейте это в виду.")
        outcome = await sync_connection(
            self.ctx, self.services, info.connection_id, transport=self.transport, retry=self.retry
        )
        await self._report_sync(outcome, prefix="Готово.")
        return {
            "status": outcome.status,
            "kinds": sorted(chosen),
            "revision": revision,
            "relearn": relearn,
            "sync": outcome.as_dict(),
        }

    async def _registry(self, info: ConnInfo, origin: str, warning: str | None) -> None:
        async with self.services.db() as db, db.begin():
            await update_university(db, info.university_id, origin, warning)

    async def _fail(
        self, info: ConnInfo, detail: str, code: ErrorCode, *, say: bool = True
    ) -> dict[str, Any]:
        if say:
            await self.ui.say(f"Не получилось подключить: {detail}.")
        await self._set(info.connection_id, status="error", error=(code.value, detail))
        return {"status": "error", "detail": code.value}


def _safe_url(url: str) -> str | None:
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.hostname else None
