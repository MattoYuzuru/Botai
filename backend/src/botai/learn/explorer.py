"""Explorer: the model as the *author of a spec*, not a free browsing agent (ADR-0005).

For a new university the job does, in this order:

1. get a session (`services.sessions.ensure`, W2: mirrored login form, saved password, fallback);
2. observe adaptively: the model selects among observed same-origin links and evidence refs;
   the executor owns navigation, network capture, budgets and redaction;
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
import contextlib
import copy
import json
import logging
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace
from functools import partial
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from botai.browser.session import EntrySessionProvider, encode_session, session_storage_expiry
from botai.contracts.agent_ui import SummaryGroup
from botai.contracts.browser_plan import BrowserPlan
from botai.contracts.specs import Kind
from botai.core.browser import BrowserHandle, ObservedRequest, SessionState
from botai.core.diagnostics import diagnostic_event, url_facts
from botai.core.dialogue import DialogueContext
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import guard_job, save_secret
from botai.core.http import RetryPolicy
from botai.core.jobs import JobCancelled, JobClaimLost, JobContext, JobDeferred
from botai.core.llm import LlmProvider
from botai.core.research import ResearchContext
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn import horizon, revisions
from botai.learn.addresses import navigation_url, url_origin
from botai.learn.author import (
    AuthorError,
    Candidate,
    author_digest,
    author_kind,
    build_spec,
    candidate_spec,
    merge_bootstrap,
    parse_json,
)
from botai.learn.canary import CanaryReport, Comparison, compare, run_canary
from botai.learn.discovery import DiscoveryBudget, ProbeKit, adaptive_observe
from botai.learn.entry import entry_queries, rank_entries
from botai.learn.intent import LearningIntent, apply_intent, intent_request, prepare_turn
from botai.learn.lint import LintContext, lint_spec
from botai.learn.memory import MemoryLimits
from botai.learn.observe import Observations
from botai.learn.policies import bind_parts
from botai.learn.probe import http_prober
from botai.learn.progress import ActivationReceipt, ResearchLedger, SavedCandidate, intent_key
from botai.learn.progress import digest as research_digest
from botai.learn.redact import Redactor
from botai.learn.registry import find_warning, update_university
from botai.learn.sitemap import save_site_map
from botai.learn.ui import Ui, describe, item, kinds_text, sample_of
from botai.search.service import SearchResult
from botai.sources.adapter import SessionExpired, SyncContext
from botai.sources.runner import (
    ConnInfo,
    SpecPin,
    SyncOutcome,
    context_for,
    guard_source_write,
    load_conn_info,
    load_snapshot,
    set_connection,
    sync_connection,
)
from botai.sources.scope import scoped_spec
from botai.sources.session import load_session, recipe_requires_session
from botai.sources.vocab import KIND_LABELS, VOCAB

log = logging.getLogger(__name__)

DEFAULT_KINDS: tuple[Kind, ...] = ("scheduleEvents",)
TOKEN_BUDGET = 1_500_000
MAX_LINK_ROUNDS = 2
MAX_CANARY_AUTHOR_ROUNDS = 2
MAX_CANARY_FEEDBACK = 12


@dataclass(slots=True)
class Chosen:
    kind: Kind
    candidate: Candidate
    report: CanaryReport
    comparison: Comparison | None = None


def origin_from(url: str) -> str | None:
    """`https://host` of a link, or None: only plain https origins can be connected."""
    validated = navigation_url(url)
    return url_origin(validated) if validated else None


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
        self.intent = LearningIntent()
        self._learning = False
        self._confirmation_revisions = 0
        self._initial_note_pending = False
        self._previous_status: str | None = None
        self._reauth_attempted = False
        self._canary_session_checked: set[str] = set()
        self._observed_session: SessionState | None = None
        self._observation_limited = False
        self._verification_failed = False
        self._scope_connection_id: str | None = None
        self._scope_base: tuple[Kind, ...] = ()
        self._scope_auto_base = False
        self._scope_replaced = False
        self._manual_login = False
        self._manual_connect = False
        self._prepared_session: SessionState | None = None
        self._research: ResearchLedger | None = None
        self._request_facts: dict[str, ObservedRequest] = {}
        self._browser_plans: dict[str, BrowserPlan] = {}
        self._browser_plan_urls: dict[str, str] = {}
        self._horizon_asked: set[Kind] = set()

    # --- small helpers -----------------------------------------------------------------------

    async def _info(self, connection_id: str) -> ConnInfo:
        async with self.services.db() as db:
            return await load_conn_info(db, self.ctx.user_id, connection_id)

    async def _set(self, connection_id: str, **values: Any) -> None:
        await set_connection(self.services, connection_id, self.ctx.user_id, ctx=self.ctx, **values)

    def _sync_context(self, info: ConnInfo, session: SessionState | None) -> SyncContext:
        result = context_for(
            self.services, info, session, transport=self.transport, retry=self.retry
        )
        if self._research is not None:
            result.before_request = self._research.before_request
        return result

    def _llm(self) -> LlmProvider:
        return self._research.llm(self.services.llm) if self._research else self.services.llm

    def _token_limit(self) -> int:
        return self.services.settings.botai_research_max_tokens if self._research else TOKEN_BUDGET

    async def _research_for(self, info: ConnInfo) -> None:
        if not isinstance(self.ctx, ResearchContext):
            return
        if self._research is not None and self._research.state.intent_key == intent_key(
            self.intent
        ):
            return
        self._research = await ResearchLedger.load(
            self.ctx,
            self.services.settings,
            user_id=info.user_id,
            job_id=self.ctx.job_id,
            connection_id=info.connection_id,
            origin=info.origin,
            allow_hosts=info.allow_hosts,
            intent=self.intent,
            initial_tokens=self.tokens,
        )
        self.tokens = max(self.tokens, self._research.state.tokens)
        await self._research.save()

    async def _canary(
        self, spec: dict[str, Any], sctx: SyncContext, *, require: Collection[Kind]
    ) -> CanaryReport:
        capture = spec.get("recipe", {}).get("version") == 2

        async def check() -> CanaryReport:
            if capture:
                return await run_canary(spec, sctx, require=require, capture_failures=True)
            return await run_canary(spec, sctx, require=require)

        if self._research is None:
            return await check()
        async with self._research.operation("verify"):
            report = await check()
        if (
            capture
            and any(finding.retryable for finding in report.findings)
            and self._research.remaining_seconds() > 0
        ):
            raise JobDeferred("Источник временно недоступен, продолжу проверку", delay_seconds=60)
        return report

    async def _ensure_session(
        self,
        info: ConnInfo,
        *,
        entry_url: str | None = None,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        if entry_url is None:
            entry_url = next(
                (url for url in self.intent.entry_urls if url_origin(url) == info.origin),
                info.origin + "/",
            )
        target = navigation_url(entry_url)
        if target is None or url_origin(target) != info.origin:
            raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Страница входа не относится к источнику")
        sessions = self.services.sessions
        diagnostic_event(
            "auth.session.request",
            connection_id=info.connection_id,
            force=force,
            manual_login=manual,
            **url_facts(target),
        )
        if isinstance(sessions, EntrySessionProvider):
            state = await sessions.ensure_for_entry(
                self.ctx,
                user_id=info.user_id,
                connection_id=info.connection_id,
                entry_url=target,
                force=force,
                manual=manual,
            )
            diagnostic_event(
                "auth.entry_ready",
                connection_id=info.connection_id,
                force=force,
                manual_login=manual,
                entry_verified=True,
                **url_facts(target),
            )
            return state
        if manual:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Защищённый экран входа сейчас недоступен"
            )
        return await sessions.ensure(
            self.ctx, user_id=info.user_id, connection_id=info.connection_id, force=force
        )

    async def _redactor(self, info: ConnInfo) -> Redactor:
        r = Redactor()
        r.know(*info.profile.values())
        vault = self.services.vault
        login = await vault.get(info.user_id, info.connection_id, SecretKind.LOGIN)
        if login is not None:
            r.know(login.get_secret_value())
        password = await vault.get(info.user_id, info.connection_id, SecretKind.PASSWORD)
        if password is not None:  # never shown to a model; replaced if a page ever echoes it
            r.protect_values([password.get_secret_value()])
        session = await load_session(vault, info.user_id, info.connection_id)
        if session is not None:
            r.protect_session(session)
        return r

    # --- entry points ------------------------------------------------------------------------

    def _start_intent(self, payload: dict[str, Any]) -> None:
        self._learning = True
        self._manual_login = payload.get("manual_login") is True
        if "learning_intent" in payload:
            self.intent = LearningIntent.restore(payload["learning_intent"])
        else:
            entry = navigation_url(payload.get("url"))
            self.intent = LearningIntent(
                entry_urls=(entry,) if entry else (),
                requested_kinds=tuple(wanted_kinds(payload)),
                discovery_mode="requested" if "kinds" in payload else "auto",
            )
        self._initial_note_pending = bool(self.intent.user_note)
        if payload.get("_research_engine") == 1:
            self._initial_note_pending = False
            self._manual_login = False  # The previous slice already completed that human step.
        self._trace_intent()

    def _trace_intent(self, *, changed: bool = False, rejected_count: int = 0) -> None:
        diagnostic_event(
            "learn.intent",
            entry_count=len(self.intent.entry_urls),
            requested_count=len(self.intent.requested_kinds),
            absent_count=len(self.intent.absent_kinds),
            has_user_note=bool(self.intent.user_note),
            intent_changed=changed,
            rejected_count=rejected_count,
        )
        for kind in self.intent.requested_kinds:
            diagnostic_event("learn.intent_kind", kind=kind, outcome="requested")
        for kind in self.intent.absent_kinds:
            diagnostic_event("learn.intent_kind", kind=kind, outcome="absent")

    async def _initial_note(self, redactor: Redactor) -> None:
        if self._initial_note_pending:
            self._initial_note_pending = False
            await self._turn(self.intent.user_note, redactor)

    async def _checkpoint(self) -> None:
        if self._scope_connection_id is not None:
            scope = self._data_scope()
            await self._set(
                self._scope_connection_id, data_scope=list(scope) if scope is not None else None
            )
        if isinstance(self.ctx, DialogueContext):
            await self.ctx.checkpoint_learning(self.intent.checkpoint())

    def _data_scope(self) -> tuple[Kind, ...] | None:
        if self.intent.discovery_mode == "auto" or (
            self._scope_auto_base and not self._scope_replaced
        ):
            if not self.intent.absent_kinds:
                return None
            return tuple(kind for kind in KIND_LABELS if kind not in self.intent.absent_kinds)
        return tuple(
            kind
            for kind in dict.fromkeys(
                (
                    *(() if self._scope_replaced else self._scope_base),
                    *self.intent.requested_kinds,
                )
            )
            if kind not in self.intent.absent_kinds
        )

    async def _turn(self, text: str, redactor: Redactor, *, checkpoint: bool = True) -> None:
        # A plain link needs no classifier and costs no model call.
        before = self.intent
        plain = navigation_url(text)
        turn = prepare_turn(plain or text, redactor)
        if turn.rejected_links:
            await self.ui.say(
                "Ссылка с данными входа скрыта. Пришлите обычный адрес страницы без токена или кода входа."
            )
        if plain:
            self.intent = apply_intent(
                self.intent, turn, {"linkIndices": list(range(len(turn.links)))}
            )
        elif turn.text:
            response = await self._llm().complete(intent_request(turn, self.intent))
            usage = response.usage
            self.tokens += usage.input_cache_miss + usage.input_cache_hit + usage.output
            try:
                classified = parse_json(response.content)
                self.intent = apply_intent(self.intent, turn, classified)
                if isinstance(classified, dict) and classified.get("replaceKinds") is True:
                    self._scope_replaced = True
            except ValueError:
                # Retain the actual user's clarification and validated links, not an invented
                # classifier answer. A failed classifier must not erase a delivered chat turn.
                self.intent = apply_intent(
                    self.intent, turn, {"linkIndices": list(range(len(turn.links)))}
                )
        self._trace_intent(changed=self.intent != before, rejected_count=turn.rejected_links)
        if checkpoint:
            await self._checkpoint()

    async def _drain(self, redactor: Redactor) -> bool:
        before = self.intent
        if self._learning and isinstance(self.ctx, DialogueContext):
            turns = await self.ctx.drain_dialogue()
            for turn in turns:
                await self._turn(turn.text, redactor, checkpoint=False)
            if turns:
                # drain delivers a batch. Ack it only after every turn has been applied.
                await self._checkpoint()
                diagnostic_event(
                    "learn.dialogue", message_count=len(turns), intent_changed=self.intent != before
                )
        return self.intent != before

    async def _continue_foreign(self, info: ConnInfo) -> dict[str, Any] | None:
        foreign = [url for url in self.intent.entry_urls if url_origin(url) != info.origin]
        if not foreign:
            return None
        diagnostic_event("learn.outcome", outcome="continued", entry_count=len(foreign))
        # A recipe still has exactly one origin. A student's link creates a sibling source;
        # credentials and bearer values are not copied between the two connections.
        from botai.spaces.conversations import continue_source

        url = foreign[-1]
        child_intent = LearningIntent(
            (url,),
            self.intent.requested_kinds,
            self.intent.absent_kinds,
            self.intent.user_note,
            self.intent.discovery_mode,
        )
        result = await continue_source(
            self.ctx, self.services, info.connection_id, url, child_intent.checkpoint()
        )
        status = self._previous_status
        if status in (None, "learning", "syncing"):
            status = "active" if info.adapter else "draft"
        await self._set(info.connection_id, status=status)
        await self.ui.say(
            "Эта страница находится на другом сайте. Подключаю её отдельным источником "
            "в том же пространстве; при необходимости попрошу войти."
        )
        return {"status": "continued", **result}

    async def connect(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._manual_connect = payload.get("manual_login") is True
        result = await self._connect(connection_id, payload)
        if not self._manual_connect or result.get("status") != "repairing":
            return result
        # The manual CONNECT still owns the source interlock. Queueing its repair
        # would deduplicate back to this very job and leave the source repairing forever.
        # Run the existing repair state machine here, outside _connect's cancellation
        # restoration: Repairer itself leaves stopped/failed repairs stale, with old data.
        from botai.learn.repair import Repairer

        repairer = Repairer(self.ctx, self.services, transport=self.transport, retry=self.retry)
        repairer.tokens = self.tokens
        try:
            repaired = await repairer.run(connection_id, {"reason": "drift"})
        finally:
            self.tokens = repairer.tokens
        metadata = {key: value for key, value in result.items() if key != "sync"}
        if "sync" in result:
            metadata["initial_sync"] = result["sync"]
        return {**metadata, **repaired}

    async def _connect(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._scope_connection_id = connection_id
        self._start_intent(payload)
        info = await self._info(connection_id)
        if info.status == "disabled":
            return {"status": "skipped", "detail": "disabled"}
        manual_existing = self._manual_login and info.adapter is not None
        if manual_existing:
            # An authentication request cannot narrow an already configured source's scope.
            self._scope_connection_id = None
        self._previous_status = info.status
        await self._set(connection_id, status="learning", clear_error=not manual_existing)
        try:
            origin = await self._resolve_origin(info, payload)
            if origin is None:
                return await self._fail(
                    info, "Не получилось определить адрес сайта", ErrorCode.VALIDATION
                )
            info = await self._info(connection_id)
            if self._manual_login:
                # The human hand-off precedes classification of old notes or new turns.
                # A configured source resumes its recipe even while the model is unavailable.
                await self._checkpoint()
                stage = await self.ui.stage("Открываю защищённый вход")
                try:
                    self._prepared_session = await self._ensure_session(
                        info, force=True, manual=True
                    )
                except BotaiError:
                    await stage.finish("failed")
                    raise
                await stage.finish()
                self._manual_login = False
                self._reauth_attempted = True
                info = await self._info(connection_id)
                if info.adapter is not None:
                    await self._set(connection_id, status="active", clear_error=True)
                    outcome = await sync_connection(
                        self.ctx,
                        self.services,
                        connection_id,
                        transport=self.transport,
                        retry=self.retry,
                        queue_repair=not self._manual_connect,
                    )
                    await self._report_sync(outcome, prefix="Вход проверен.")
                    return {"status": outcome.status, "login_resumed": True}
            if payload.get("_research_engine") == 1:
                await self._research_for(info)
            await self._initial_note(await self._redactor(info))
            await self._checkpoint()
            await self._drain(await self._redactor(info))
            continued = await self._continue_foreign(info)
            if continued is not None:
                return continued
            resumed = await self._resume_activation(info)
            if resumed is not None:
                return resumed
            reused = await self._reuse(info)
            if reused is not None:
                return reused
            return await self._explore(info, wanted_kinds(payload), base_spec=None)
        except (JobCancelled, asyncio.CancelledError):
            # nobody answered in time (JobCancelled), or the student stopped the job and the
            # runner cancelled its task (CancelledError): either way the setup is not running
            with contextlib.suppress(JobClaimLost):
                await self._set(
                    connection_id,
                    status=(self._previous_status or "draft") if manual_existing else "draft",
                )
            raise
        except BotaiError as err:
            return await self._fail(info, err.detail or "Не удалось подключить сайт", err.code)

    async def learn(self, connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Teach an existing connection more kinds of data; the other endpoints are kept."""
        self._start_intent(payload)
        async with self.services.db() as db:
            snap = await load_snapshot(db, self.ctx.user_id, connection_id)
        if snap.adapter != "recipe":
            return {"status": "skipped", "detail": "adapter"}
        self._scope_connection_id = connection_id
        self._scope_auto_base = snap.data_scope is None
        self._scope_base = (
            snap.data_scope
            if snap.data_scope is not None
            else tuple(
                dict.fromkeys(endpoint["kind"] for endpoint in snap.spec["recipe"]["endpoints"])
            )
        )
        try:
            if payload.get("_research_engine") == 1:
                await self._research_for(snap)
            await self._checkpoint()
            await self._drain(await self._redactor(snap))
            resumed = await self._resume_activation(snap)
            if resumed is not None:
                return resumed
            return await self._explore(snap, wanted_kinds(payload), base_spec=snap.spec)
        except BotaiError as err:
            await self.ui.say(err.detail or "Не удалось выучить новые данные.")
            return {"status": "error", "detail": err.code.value}

    # --- origin ------------------------------------------------------------------------------

    async def _resolve_origin(self, info: ConnInfo, payload: dict[str, Any]) -> str | None:
        origin: str | None = info.origin or None
        public_title = info.title
        link = self.intent.entry_urls[0] if self.intent.entry_urls else payload.get("url")
        if origin is None and isinstance(link, str) and link.strip():
            origin = origin_from(link)
        if origin is None and info.university_id:
            async with self.services.db() as db:
                uni = await db.get(m.University, info.university_id)
            if uni is not None:
                public_title = uni.title
            known = [o for o in (uni.origins if uni else []) if origin_from(o)]
            if len(known) == 1:
                origin = known[0]
        if origin is None and self.services.search is not None:
            await self._suggest_entries(info, public_title)
            if self.intent.entry_urls:
                origin = url_origin(self.intent.entry_urls[0])
        for _ in range(2):
            if origin is not None:
                break
            answer = await self.ui.ask_learning_turn(
                "Какой сайт подключить? Нужна ссылка на личный кабинет, расписание или LMS."
            )
            if answer:
                await self._turn(answer, await self._redactor(info))
            origin = url_origin(self.intent.entry_urls[0]) if self.intent.entry_urls else None
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

    async def _suggest_entries(self, info: ConnInfo, title: str) -> None:
        """Offer public candidates for the entry page and let the student choose one (FR-038).

        A hit is an untrusted citation, shown by address only. It becomes the entry of the
        connection when the student picks it, which is what grants its host, nothing else does.
        """
        search = self.services.search
        if search is None:
            return
        queries = entry_queries(title)
        if not queries:
            return
        budget = search.budget()
        results: list[SearchResult] = []
        unavailable = False
        for query in queries:
            response = await search.search(
                query, budget=budget, user_id=self.ctx.user_id, job_id=self.ctx.job_id
            )
            results.extend(response.results)
            unavailable = unavailable or (
                not response.results and response.status in {"unavailable", "budget"}
            )
        candidates = rank_entries(results)
        if not candidates:
            if unavailable:
                await self.ui.say(
                    "Публичный поиск сейчас недоступен. Подключение можно продолжить "
                    "по вашей ссылке на сайт."
                )
            return
        picked = await self.ui.choose(
            "Поиск нашёл возможные адреса. Это лишь предложение: ваши данные он не открывает. "
            "Какой адрес подключить?",
            [
                (str(index), candidate.label[:120], f"поиск: {candidate.provider}")
                for index, candidate in enumerate(candidates)
            ]
            + [("other", "Другой адрес", "Пришлю ссылку сама")],
        )
        if picked is not None and picked.isdigit() and int(picked) < len(candidates):
            await self._turn(candidates[int(picked)].url, await self._redactor(info))

    # --- reuse (FR-009) ----------------------------------------------------------------------

    async def _reuse(self, info: ConnInfo) -> dict[str, Any] | None:
        if (
            self.intent.user_note
            or self.intent.absent_kinds
            or any(
                urlsplit(url).path not in ("", "/") or urlsplit(url).query or urlsplit(url).fragment
                for url in self.intent.entry_urls
            )
        ):
            return None  # An explicit view/page must be observed before choosing an old recipe.
        async with self.services.db() as db:
            row = await revisions.active_row(db, info.origin, "recipe")
        if row is None:
            return None
        available = {e["kind"] for e in row.spec["recipe"]["endpoints"]}
        if (
            self.intent.discovery_mode == "requested"
            and not set(self.intent.requested_kinds) <= available
        ):
            return None
        scope = self._data_scope()
        if scope is not None and not available.intersection(scope):
            return None
        stage = await self.ui.stage(
            "Для этого сайта описание уже есть",
            "Подключаю по готовому описанию, без повторного разбора",
        )
        if self._prepared_session is None and recipe_requires_session(
            scoped_spec(row.spec, scope)["recipe"]
        ):
            # Reuse still needs this student's verified entry. A login landing/probe alone
            # may not load the SPA page that sends the recipe's bearer Authorization.
            await self._ensure_session(info)
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
            self.ctx,
            self.services,
            info.connection_id,
            transport=self.transport,
            retry=self.retry,
            queue_repair=not self._manual_connect,
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
        try:
            session = self._prepared_session or await self._ensure_session(info)
            self._prepared_session = None
            info = await self._info(info.connection_id)
        except BotaiError:
            await stage.finish("failed")
            raise
        await stage.finish()
        redactor = await self._redactor(info)
        await self._initial_note(redactor)
        await self._drain(redactor)
        continued = await self._continue_foreign(info)
        if continued is not None:
            return continued
        sctx = self._sync_context(info, session)
        await self._research_for(info)
        if self._research is not None:
            sctx.before_request = self._research.before_request
        digest = author_digest(info.user_id)
        async with svc.db() as db:
            revision = await revisions.latest_revision(db, info.origin, "recipe") + 1

        chosen: dict[Kind, Chosen] = {}
        pending: list[Kind] = list(kinds)
        extra: list[str] = []
        warning: str | None = None
        lint_ctx = LintContext(allowed_hosts=info.allow_hosts, expected_origin=info.origin)
        for round_no in range(MAX_LINK_ROUNDS + 1):
            await self._drain(redactor)
            continued = await self._continue_foreign(info)
            if continued is not None:
                return continued
            chosen = self._scoped_selection(chosen)
            pending = [k for k in self.intent.requested_kinds if k not in chosen]
            diagnostic_event(
                "learn.round",
                round_no=round_no,
                pending_count=len(pending),
                chosen_count=len(chosen),
                token_count=self.tokens,
            )
            if not pending and self.intent.discovery_mode != "auto":
                break
            round_intent = self.intent
            await self._research_for(info)
            if self._research is not None:
                sctx.before_request = self._research.before_request
            obs = await self._observe(info, sctx.session or session, redactor, extra)
            if obs.limited and not self._observation_limited:
                await ui.say(
                    "Пока не удалось полностью проверить источник: исследование достигло "
                    "ограничения, план просмотра не сработал или запрос данных завершился ошибкой. "
                    "Это не означает, что данных нет."
                )
            self._observation_limited = self._observation_limited or obs.limited
            if self._observed_session is not None:
                session = self._observed_session
                sctx.session = session
            if self.intent.discovery_mode == "auto" and obs.discovered_kinds:
                self.intent = replace(
                    self.intent,
                    requested_kinds=tuple(
                        kind
                        for kind in obs.discovered_kinds
                        if kind not in self.intent.absent_kinds
                    ),
                )
                await self._checkpoint()
                round_intent = self.intent  # discovery is not a concurrent user correction
                pending = [kind for kind in self.intent.requested_kinds if kind not in chosen]
            if round_no == 0 and not obs.empty:
                warning, spent = await find_warning(self._llm(), obs.plain_text)
                self.tokens += spent
            if self._research is not None:
                lint_ctx = LintContext(
                    allowed_hosts=info.allow_hosts,
                    expected_origin=info.origin,
                    observed=obs.observed,
                )
                for kind in list(pending):
                    restored = await self._restore_candidate(
                        kind, lint_ctx, sctx, info, revision, digest
                    )
                    if restored is not None:
                        chosen[kind] = restored
                        pending.remove(kind)
            if not obs.empty:
                lint_ctx = LintContext(
                    allowed_hosts=info.allow_hosts,
                    expected_origin=info.origin,
                    observed=obs.observed,
                )
                text = obs.render()
                if self.intent.user_note:
                    text += (
                        "\n<user-clarification>"
                        + json.dumps(self.intent.user_note, ensure_ascii=False)
                        + "</user-clarification>"
                    )
                for kind in list(pending):
                    if await self._drain(redactor):
                        break  # Re-observe a changed entry/view before authoring another kind.
                    if self.tokens > self._token_limit():
                        diagnostic_event("learn.selection", outcome="failed", reason="token_budget")
                        break
                    found = await self._author_and_verify(
                        kind, text, lint_ctx, sctx, info, revision, digest
                    )
                    if found is not None:
                        found = await self._thin_horizon(
                            kind, found, redactor, sctx, info, revision, digest, lint_ctx
                        )
                    if await self._drain(redactor):
                        break  # A clarification during a canary must affect the final selection.
                    if found is not None:
                        chosen[kind] = found
                        pending.remove(kind)
                        if self._research is not None:
                            self._research.state.candidates[kind] = SavedCandidate.of(
                                found.candidate
                            )
                            self._research.account_total(self.tokens)
                            await self._research.save()
            if self.intent != round_intent:
                continue
            if not pending or round_no == MAX_LINK_ROUNDS:
                break
            if self._research is not None:
                memory = self._research.state.memory
                if memory.exhausted or memory.overflowed:
                    break
                if memory.frontier:
                    # An authored read failed, but there are still executor-observed pages.
                    # Continue that frontier before asking the student for the same link.
                    memory.finished = False
                    memory.pending_url = memory.frontier[0]
                    memory.resume_needed = True
                    self._research.state.phase = "observe"
                    await self._research.save()
                    raise JobDeferred("Продолжаю поиск доступных разделов")
            continued = await self._continue_foreign(info)
            if continued is not None:
                return continued
            if not await self._ask_page(info, pending, have_some=bool(chosen)):
                break

        await self._drain(redactor)
        continued = await self._continue_foreign(info)
        if continued is not None:
            return continued
        chosen = self._scoped_selection(chosen)
        pending = [k for k in self.intent.requested_kinds if k not in chosen]

        if not chosen:
            await ui.say(
                (
                    "Пока не удалось проверить "
                    if self._observation_limited or self._verification_failed
                    else "Не нашла в кабинете "
                )
                + (", ".join(KIND_LABELS[k].lower() for k in pending) or "запрошенные данные")
                + ". Ничего не сохраняю. Если пришлёте ссылку на страницу, где это видно, "
                "попробую ещё раз."
            )
            return await self._fail(
                info,
                "Не удалось проверить запрошенные данные"
                if self._observation_limited or self._verification_failed
                else "В кабинете не найдены нужные данные",
                ErrorCode.RECIPE_REJECTED
                if self._verification_failed
                else ErrorCode.SOURCE_UNAVAILABLE
                if self._observation_limited
                else ErrorCode.NOT_FOUND,
                say=False,
            )

        assembly_base = base_spec
        if assembly_base is None:
            async with svc.db() as db:
                shared = await revisions.active_row(db, info.origin, "recipe")
            assembly_base = shared.spec if shared is not None else None
        # Preserve previously validated kinds for other students. Only this connection's
        # effective scope is canaried/read; narrowing one user cannot shrink the shared recipe.
        built = await self._assemble(info, chosen, assembly_base, revision, digest, sctx, lint_ctx)
        if built is None:
            return await self._fail(
                info, "Собранное описание не прошло проверку", ErrorCode.RECIPE_REJECTED
            )
        spec, report = built
        if pending:
            await ui.say(
                (
                    "Не удалось проверить: "
                    if self._observation_limited or self._verification_failed
                    else "Не нашла: "
                )
                + ", ".join(KIND_LABELS[k].lower() for k in pending)
                + ". Остальное подключаю, к этому можно вернуться позже."
            )
        if warning:
            await ui.say(f"**Важно.** {warning} Учитывайте это перед подтверждением подключения.")
        if self._research is not None:
            self._research.state.phase = "confirm"
            await self._research.save()
        await self._show_summary(info, chosen, report)
        confirmed = await ui.confirm(
            "Всё верно? После подтверждения данные будут обновляться автоматически.",
            "Да, всё верно",
            "Нет, не то",
        )
        diagnostic_event("learn.confirmation", confirmed=confirmed, chosen_count=len(chosen))
        if not confirmed:
            await ui.say("Хорошо, ничего не сохраняю.")
            return await self._fail(
                info, "Собранное не подтверждено", ErrorCode.RECIPE_REJECTED, say=False
            )
        before = self.intent
        await self._drain(redactor)
        if self.intent != before:
            # Never activate a description the user changed while its confirmation was pending.
            self._confirmation_revisions += 1
            if self._confirmation_revisions > MAX_LINK_ROUNDS:
                return await self._fail(
                    info,
                    "Задача изменилась. Продолжите подключение новым сообщением.",
                    ErrorCode.VALIDATION,
                )
            return await self._explore(info, list(self.intent.requested_kinds), base_spec=base_spec)
        if self._research is not None:
            self._research.state.phase = "activate"
            await self._research.save()
        return await self._activate(info, spec, chosen, warning, digest, base_spec is not None)

    def _scoped_selection(self, chosen: dict[Kind, Chosen]) -> dict[Kind, Chosen]:
        return {
            kind: value
            for kind, value in chosen.items()
            if kind not in self.intent.absent_kinds
            and (self.intent.discovery_mode == "auto" or kind in self.intent.requested_kinds)
        }

    async def _observe(
        self, info: ConnInfo, session: SessionState, redactor: Redactor, extra: list[str]
    ) -> Observations:
        self._request_facts, self._browser_plans, self._browser_plan_urls = {}, {}, {}
        research = self._research
        if research is not None:
            state = research.state
            if research.remaining_seconds() <= 0:
                raise BotaiError(
                    ErrorCode.SOURCE_UNAVAILABLE, "Исследование достигло общего бюджета времени"
                )
            if (
                state.memory.requests + state.other_requests
                >= self.services.settings.botai_research_max_requests
            ):
                raise BotaiError(
                    ErrorCode.SOURCE_RATE_LIMITED, "Исследование достигло общего бюджета чтений"
                )
            if state.tokens >= self.services.settings.botai_research_max_tokens:
                state.memory.exhausted = True
                state.memory.stop_reason = "token_budget"
                await research.save()
                return Observations(origin=info.origin, limited=True, stop_reason="token_budget")
        stage = await self.ui.stage("Смотрю страницы кабинета")
        entries = [url for url in self.intent.entry_urls if url_origin(url) == info.origin]
        start_urls = list(dict.fromkeys([*entries, *extra, info.origin + "/"]))
        obs = Observations(origin=info.origin)
        for _ in range(2):
            redactor.protect_session(session)
            handle = await self.services.browser.open(
                user_id=info.user_id,
                connection_id=info.connection_id,
                allow_hosts=info.allow_hosts,
                session=session,
                read_only=True,
                background=True,
            )
            try:
                research = self._research
                settings = self.services.settings
                if research is not None:
                    research.state.pending_active_ms = int(
                        min(settings.botai_research_slice_seconds, research.remaining_seconds())
                        * 1000
                    )
                    if research.state.pending_active_ms <= 0:
                        raise BotaiError(
                            ErrorCode.SOURCE_UNAVAILABLE,
                            "Исследование достигло общего бюджета времени",
                        )
                    await research.save()
                obs = await adaptive_observe(
                    handle,
                    info.origin,
                    redactor,
                    start_urls=start_urls,
                    intent=self.intent,
                    llm=self.services.llm,
                    probe_kit=ProbeKit(
                        http_prober(partial(self._probe_context, handle, info, redactor)),
                        self.services.clock.now(),
                        info.tz,
                    ),
                    budget=DiscoveryBudget(
                        steps=settings.botai_research_slice_steps if research else 10,
                        seconds=min(
                            settings.botai_research_slice_seconds, research.remaining_seconds()
                        )
                        if research
                        else 90.0,
                        tokens=min(150_000, max(0, self._token_limit() - self.tokens)),
                    ),
                    memory=research.state.memory if research else None,
                    checkpoint=research.observed if research else None,
                    run_limits=MemoryLimits(
                        max_steps=settings.botai_research_max_steps,
                        max_requests=max(
                            1, settings.botai_research_max_requests - research.state.other_requests
                        ),
                        max_tokens=max(
                            1, settings.botai_research_max_tokens - research.state.other_tokens
                        ),
                        max_active_ms=max(
                            1,
                            int(settings.botai_research_max_seconds * 1000)
                            - research.state.other_active_ms,
                        ),
                    )
                    if research
                    else None,
                )
                if research is not None:
                    research.state.pending_active_ms = 0
                    await research.save()
                if not obs.auth_required and not obs.bot_protection:
                    # The SPA can rotate credentials while discovering data. Canary and the
                    # first sync must use this state, not the snapshot restored before browsing.
                    try:
                        async with asyncio.timeout(5.0):
                            refreshed = await handle.export_session()
                        redactor.protect_session(refreshed)
                    except Exception:
                        raise BotaiError(
                            ErrorCode.SOURCE_UNAVAILABLE,
                            "Не удалось сохранить обновлённое состояние браузера",
                        ) from None
                    await save_secret(
                        self.ctx,
                        self.services.vault,
                        info.user_id,
                        info.connection_id,
                        SecretKind.SESSION,
                        encode_session(refreshed),
                        expires_at=session_storage_expiry(refreshed),
                    )
                    session = refreshed
            finally:
                await handle.close()
            self.tokens += obs.planner_tokens
            if self._research is not None:
                self._research.account_total(self.tokens)
                await self._research.save()
            if not obs.auth_required and not obs.bot_protection:
                self._observed_session = session
                if self._research is not None and self._research.state.memory.resume_needed:
                    await stage.finish("ok", "Исследование сохранено, продолжаю просмотр")
                    raise JobDeferred("Продолжаю исследование источника")
                break
            if self._reauth_attempted:
                await stage.finish("failed")
                raise BotaiError(
                    ErrorCode.SOURCE_BOT_PROTECTION
                    if obs.bot_protection
                    else ErrorCode.SOURCE_AUTH_REQUIRED,
                    "После повторного входа источник всё ещё просит авторизоваться. Данные не проверены.",
                )
            self._reauth_attempted = True
            await self.ui.say(
                "Источник снова просит войти. Восстанавливаю вход перед поиском данных."
            )
            session = await self._ensure_session(info, entry_url=obs.auth_target_url, force=True)
            info = await self._info(info.connection_id)
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
                await guard_job(self.ctx, db, check_input=True)
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
        self._request_facts = obs.request_facts
        self._browser_plans = obs.browser_plans
        self._browser_plan_urls = obs.browser_plan_urls
        if obs.sitemap and not obs.auth_required and not obs.bot_protection:
            async with self.services.db() as db, db.begin():
                await guard_job(self.ctx, db, check_input=True)
                await save_site_map(db, info.origin, obs.sitemap, self.services.clock.now())
        return obs

    async def _probe_context(
        self, handle: BrowserHandle, info: ConnInfo, redactor: Redactor
    ) -> SyncContext:
        """The student's session as the browser holds it now, for a read-only probe."""
        async with asyncio.timeout(5.0):
            state = await handle.export_session()
        redactor.protect_session(state)
        context = self._sync_context(info, state)
        context.before_request = None  # the discovery memory already counts its probes
        return context

    async def _restore_candidate(
        self,
        kind: Kind,
        lint_ctx: LintContext,
        sctx: SyncContext,
        info: ConnInfo,
        revision: int,
        digest: str,
    ) -> Chosen | None:
        research = self._research
        if research is None:
            return None
        saved = research.state.candidates.get(kind)
        proposals = [saved] if saved else research.state.proposals.get(kind, [])
        if not proposals:
            return None
        stage = await self.ui.stage(f"Повторно проверяю: {KIND_LABELS[kind].lower()}")
        verified: list[Chosen] = []
        for index, proposal in enumerate(proposals):
            if proposal.kind != kind:
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED, "Вид данных сохранённого описания не совпал"
                )
            checked, _ = await self._verify_candidate(
                kind, proposal.candidate(), index, sctx, info, revision, digest, lint_ctx
            )
            if checked is not None:
                bound, report = checked
                verified.append(Chosen(kind, bound, report, None))
        if verified:
            await stage.finish("ok")
            return await self._pick(kind, verified, sctx.tz)
        research.state.candidates.pop(kind, None)
        research.state.proposals.pop(kind, None)
        await research.save()
        await stage.finish("failed", "Свежая проверка требует уточнить описание")
        return None

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
        verified: list[Chosen] = []
        feedback: list[str] | None = None
        candidate_count = 0
        for attempt in range(MAX_CANARY_AUTHOR_ROUNDS):
            try:
                authored = await author_kind(
                    self._llm(),
                    kind,
                    observation,
                    lint_ctx=lint_ctx,
                    origin=info.origin,
                    revision=revision,
                    now=self.services.clock.now(),
                    digest=digest,
                    previous=previous,
                    problem=problem,
                    verification_feedback=feedback,
                    request_facts=self._request_facts,
                    browser_plans=self._browser_plans,
                    browser_plan_urls=self._browser_plan_urls,
                    time_zone=info.tz,
                )
            except AuthorError:
                self._verification_failed = True
                await stage.finish("failed", "Модель не вернула пригодный ответ")
                return None
            self.tokens += authored.tokens
            candidate_count = len(authored.candidates)
            if not authored.candidates:
                rejected = bool(authored.rejected) or feedback is not None
                self._verification_failed = self._verification_failed or rejected
                reason = (
                    "Проверка отклонила все варианты" if rejected else "Подходящих данных не видно"
                )
                await stage.finish("failed" if rejected else "skipped", reason)
                diagnostic_event(
                    "learn.selection",
                    kind=kind,
                    outcome="rejected" if rejected else "not_found",
                    reason="canary_rejected"
                    if feedback is not None
                    else "lint_rejected"
                    if authored.rejected
                    else "no_candidates",
                    candidate_count=0,
                )
                return None
            feedback = []
            if self._research is not None:
                self._research.state.proposals[kind] = [
                    SavedCandidate.of(c) for c in authored.candidates
                ]
                self._research.account_total(self.tokens)
                await self._research.save()
            for i, cand in enumerate(authored.candidates):
                checked, findings = await self._verify_candidate(
                    kind, cand, i, sctx, info, revision, digest, lint_ctx
                )
                feedback.extend(findings[: max(0, MAX_CANARY_FEEDBACK - len(feedback))])
                if checked is not None:
                    bound, report = checked
                    comparison = (
                        compare(kind, baseline.get(kind), report.valid.get(kind, []), sctx.tz)
                        if baseline is not None
                        else None
                    )
                    verified.append(Chosen(kind, bound, report, comparison))
            if verified or self.tokens >= self._token_limit():
                break
            if attempt + 1 < MAX_CANARY_AUTHOR_ROUNDS:
                diagnostic_event(
                    "learn.selection",
                    kind=kind,
                    outcome="retry",
                    reason="canary_rejected",
                    attempt=attempt + 1,
                    candidate_count=candidate_count,
                )
        if not verified:
            self._verification_failed = True
            await stage.finish("failed", "Проверка на вашей сессии не прошла")
            diagnostic_event(
                "learn.selection",
                kind=kind,
                outcome="rejected",
                reason="canary_rejected",
                candidate_count=candidate_count,
                verified_count=0,
            )
            return None
        picked = await self._pick(kind, verified, sctx.tz)
        note = f"Найдено записей: {picked.report.count(kind)}"
        if picked.comparison is not None and picked.comparison.baseline:
            c = picked.comparison
            note += f"; совпало с прежними данными: {c.matched} из {c.baseline}"
        await stage.finish("ok", note)
        diagnostic_event(
            "learn.selection",
            kind=kind,
            outcome="accepted",
            candidate_count=candidate_count,
            verified_count=len(verified),
            record_count=picked.report.count(kind),
        )
        return picked

    async def _verify_candidate(
        self,
        kind: Kind,
        candidate: Candidate,
        index: int,
        sctx: SyncContext,
        info: ConnInfo,
        revision: int,
        digest: str,
        lint_ctx: LintContext,
    ) -> tuple[tuple[Candidate, CanaryReport] | None, list[str]]:
        """Run one compiled candidate on the student's session.

        A refused HTTP replay is evidence about this candidate, not proof that the browser
        login expired. Credentials remain executor-owned and are never part of feedback.
        """
        rendered = candidate.endpoint.get("request", {}).get("render") is True
        state = sctx.session
        findings: list[str] = []
        spec = candidate_spec(
            candidate,
            origin=info.origin,
            revision=revision,
            now=self.services.clock.now(),
            digest=digest,
            index=index,
        )
        lint = lint_spec(spec, lint_ctx)
        if not lint.ok:
            return None, lint.reasons()
        facts = {
            "kind": kind,
            "candidate_index": index,
            "auth_type": candidate.auth.get("type"),
            "transport": "rendered" if rendered else "http",
            "rendered": rendered,
            "bearer_present": state is not None and state.bearer is not None,
        }
        diagnostic_event("canary.candidate", phase="request", **facts)
        try:
            report = await self._canary(spec, sctx, require={kind})
            if info.connection_id not in self._canary_session_checked and any(
                finding.code
                in {"auth_missing", "auth_rejected", "auth_scope_mismatch", "login_redirect"}
                for finding in report.findings
            ):
                self._canary_session_checked.add(info.connection_id)
                async with self.services.db() as db:
                    await guard_job(self.ctx, db, check_input=True)
                # Endpoint refusal is not a request to log in. The session provider checks
                # the independent entry/probe, preserving a healthy human login (force=False).
                sctx.session = await self._ensure_session(info, force=False)
                self._observed_session = sctx.session
                report = await self._canary(spec, sctx, require={kind})
        except SessionExpired:
            diagnostic_event(
                "canary.candidate",
                phase="response",
                outcome="rejected",
                reason="session_expired",
                **facts,
            )
            return None, [
                f"вариант {index + 1}: доступ к данным не воспроизведён "
                f"с авторизацией {candidate.auth.get('type')}. "
                "Это не доказывает потерю входа в браузере; проверь наблюдаемый запрос "
                "или предложи render=true для подтверждённого HTML DOM."
            ]
        diagnostic_event(
            "canary.candidate",
            phase="response",
            outcome="accepted" if report.ok else "rejected",
            reason="ok" if report.ok else "canary_rejected",
            problem_count=len(report.problems),
            **facts,
        )
        if report.ok:
            return (candidate, report), findings
        findings.extend(f"вариант {index + 1}: {problem}" for problem in report.feedback())
        wide = (
            self._walk_candidate(candidate, info)
            if any(finding.code == "empty_result" for finding in report.findings)
            else None
        )
        if wide is not None:
            # The read answered but its window was empty (a holiday, a control week): the
            # neighbouring windows may hold the schedule.
            checked, more = await self._verify_candidate(
                kind, wide, index, sctx, info, revision, digest, lint_ctx
            )
            findings.extend(more)
            if checked is not None:
                return checked, findings
        return None, findings

    def _walk_candidate(self, candidate: Candidate, info: ConnInfo) -> Candidate | None:
        wide = horizon.walked(candidate.endpoint, self.services.clock.now(), info.tz)
        return replace(candidate, endpoint=wide) if wide is not None else None

    async def _thin_horizon(
        self,
        kind: Kind,
        found: Chosen,
        redactor: Redactor,
        sctx: SyncContext,
        info: ConnInfo,
        revision: int,
        digest: str,
        lint_ctx: LintContext,
    ) -> Chosen | None:
        """A date window with almost nothing in it is read over its neighbours; if it stays thin,
        the student is asked in plain words (FR-039). None: the answer sent the search elsewhere."""
        if kind in self._horizon_asked or not horizon.moves_date(found.candidate.endpoint):
            return found
        count = found.report.count(kind)
        if count > horizon.SPARSE:
            return found
        if not horizon.is_walk(found.candidate.endpoint):
            wide = self._walk_candidate(found.candidate, info)
            if wide is None:
                return found  # neighbouring windows cannot be read, so there is nothing to ask
            stage = await self.ui.stage("Проверяю соседние недели")
            checked, _ = await self._verify_candidate(
                kind, wide, 0, sctx, info, revision, digest, lint_ctx
            )
            if checked is not None and checked[1].count(kind) >= count:
                # A walk that the source answers window by window is never worse than one window,
                # and it keeps reading as the schedule fills up.
                found = Chosen(kind, checked[0], checked[1], found.comparison)
                count = found.report.count(kind)
                await stage.finish("ok", f"Найдено записей: {count}")
            else:
                await stage.finish("skipped", "Соседние недели не подтвердились")
        if count > horizon.SPARSE or kind != "scheduleEvents":
            return found  # a few deadlines or news are ordinary; a thin timetable is a question
        self._horizon_asked.add(kind)
        now = self.services.clock.now()
        endpoint = found.candidate.endpoint
        per_window: list[int] = []
        if found.report.result is not None:
            per_window = next(
                (
                    o.window_items
                    for o in found.report.result.outcomes
                    if o.kind == kind and o.window_items
                ),
                [],
            )
        span = horizon.covered(endpoint, now, info.tz)
        window = endpoint.get("pagination") or {}
        question = horizon.thin_question(
            count,
            kind_forms=("занятие", "занятия", "занятий")
            if kind == "scheduleEvents"
            else ("запись", "записи", "записей"),
            span=span,
            per_window=per_window,
            unit="неделям" if window.get("stepDays") == 7 else "периодам",
        )
        diagnostic_event(
            "learn.thin_horizon", kind=kind, record_count=count, window_count=len(per_window)
        )
        before = self.intent
        answer = await self.ui.ask_learning_turn(question)
        if not answer:
            return found
        await self._turn(answer, redactor)
        redirected = (
            self.intent.entry_urls != before.entry_urls
            or self.intent.requested_kinds != before.requested_kinds
            or self.intent.absent_kinds != before.absent_kinds
        )
        return None if redirected else found

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

    async def _ask_page(self, info: ConnInfo, pending: list[Kind], *, have_some: bool) -> bool:
        labels = ", ".join(KIND_LABELS[k].lower() for k in pending)
        if have_some:
            choice = await self.ui.choose(
                (
                    "Не удалось проверить: "
                    if self._observation_limited or self._verification_failed
                    else "Не нашла: "
                )
                + f"{labels}. Можно дать ссылку на страницу, где это видно.",
                [("link", "Дать ссылку", None), ("skip", "Пропустить", "Это можно добавить позже")],
            )
            if choice != "link":
                return False
        answer = await self.ui.ask_learning_turn(
            (
                f"Пока не удалось проверить: {labels}. "
                if self._observation_limited or self._verification_failed
                else f"Не вижу: {labels}. "
            )
            + "Пришлите ссылку или поясните, что здесь есть и что подключить."
        )
        if not answer:
            return False
        before = self.intent
        await self._turn(answer, await self._redactor(info))
        return self.intent != before

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
        auth_options: list[dict[str, Any]] = []
        require: list[Kind] = []
        retained_kinds: set[Kind] = set()
        scope = self._data_scope()
        bound_mode = any(
            value.candidate.auth.get("type") == "session" for value in chosen.values()
        ) or (
            base_spec is not None
            and base_spec["recipe"]["auth"].get("type") == "session"
            and any(endpoint["kind"] not in chosen for endpoint in base_spec["recipe"]["endpoints"])
        )
        bindings: dict[str, dict[str, Any]] = {}
        if base_spec is not None:
            base = base_spec["recipe"]
            endpoints = [e for e in copy.deepcopy(base["endpoints"]) if e["kind"] not in chosen]
            bootstrap = copy.deepcopy(base.get("bootstrap", [])) if endpoints else []
            if bound_mode:
                bindings.update(bind_parts(base["auth"], endpoints, bootstrap))
            retained_kinds = {e["kind"] for e in endpoints}
            if endpoints:
                auth_options.append(copy.deepcopy(base["auth"]))
            require = [k for k in base["validation"]["requireKinds"] if k not in chosen]
        for kind, ch in chosen.items():
            candidate_bootstrap = copy.deepcopy(ch.candidate.bootstrap)
            candidate_endpoint = copy.deepcopy(ch.candidate.endpoint)
            if bound_mode:
                bindings.update(
                    bind_parts(ch.candidate.auth, [candidate_endpoint], candidate_bootstrap)
                )
            merged = merge_bootstrap(bootstrap, candidate_bootstrap)
            if merged is None:
                await self.ui.note(
                    f"Пропускаю: {KIND_LABELS[kind].lower()}",
                    "Шаги подготовки этого раздела противоречат уже выбранным.",
                    "skipped",
                )
                continue
            bootstrap = merged
            endpoint = candidate_endpoint
            endpoint["id"] = f"{kind}1"
            endpoints.append(endpoint)
            require.append(kind)
            if ch.candidate.auth not in auth_options and (
                not retained_kinds or scope is None or retained_kinds <= set(scope)
            ):
                # A changed recipe-wide binding must also work for retained endpoints. Do
                # not read excluded kinds just to prove it or silently change their auth.
                auth_options.append(copy.deepcopy(ch.candidate.auth))
        if not endpoints:
            await stage.finish("failed")
            return None
        if bound_mode:
            referenced_bindings = {step["authBinding"] for step in bootstrap}
            referenced_bindings.update(
                endpoint["request"]["authBinding"]
                for endpoint in endpoints
                if not endpoint["request"].get("render")
            )
            bindings = {key: value for key, value in bindings.items() if key in referenced_bindings}
            auth_options = (
                [{"type": "session", "version": 1, "bindings": bindings}]
                if bindings
                # Only browser requests survive: their own session supplies authentication.
                else [{"type": "cookie"}]
            )
        problems: list[str] = []
        for auth in auth_options:
            spec = build_spec(
                origin=info.origin,
                revision=revision,
                now=self.services.clock.now(),
                digest=digest,
                endpoints=endpoints,
                bootstrap=bootstrap,
                auth=auth,
                require_kinds=require,
            )
            verdict = lint_spec(spec, lint_ctx)
            if not verdict.ok:
                await stage.finish("failed", "; ".join(verdict.reasons()[:3]))
                return None
            facts = {"auth_type": auth.get("type"), "endpoint_count": len(endpoints)}
            diagnostic_event("canary.assembly", phase="request", **facts)
            try:
                report = await self._canary(
                    scoped_spec(spec, scope),
                    sctx,
                    require=[k for k in require if k in chosen],
                )
            except SessionExpired:
                diagnostic_event(
                    "canary.assembly",
                    phase="response",
                    outcome="rejected",
                    reason="session_expired",
                    **facts,
                )
                continue
            diagnostic_event(
                "canary.assembly",
                phase="response",
                outcome="accepted" if report.ok else "rejected",
                reason="ok" if report.ok else "canary_rejected",
                **facts,
            )
            if report.ok:
                await stage.finish("ok")
                return spec, report
            problems = report.problems
        await stage.finish(
            "failed",
            "; ".join(problems[:3])
            or "Не удалось проверить единый способ чтения для собранного описания",
        )
        return None

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
                items.append(item(label, value or "—", _safe_url(rec.open_url or "")))
            groups.append(SummaryGroup(title=KIND_LABELS[kind], items=items))
        groups.append(
            SummaryGroup(title="Источник", items=[item("Сайт", info.origin, info.origin)])
        )
        await self.ui.summary(title, groups)

    async def _resume_activation(self, info: ConnInfo) -> dict[str, Any] | None:
        async with self.services.db() as db, db.begin():
            conn = await guard_source_write(db, self.ctx, info.user_id, info.connection_id)
            if conn is None or conn.status == "disabled":
                return None
            job = await db.get(m.Job, self.ctx.job_id)
            raw = job.payload.get("_research_activation") if job is not None else None
            if raw is None:
                return None
            try:
                receipt = ActivationReceipt.model_validate(raw)
            except ValidationError:
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED, "Не удалось проверить сохранённую активацию"
                ) from None
            if receipt.intent_key != intent_key(self.intent):
                return None  # A new instruction requires new proof and confirmation.
            row = await db.get(m.AdapterSpecRow, receipt.spec_id)
            if (
                row is None
                or row.origin != info.origin
                or row.adapter != "recipe"
                or row.state != "active"
                or row.revision != receipt.revision
                or conn.spec_id != row.id
                or research_digest(row.spec) != receipt.spec_digest
            ):
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED,
                    "Активированное описание изменилось; нужна новая проверка",
                )
        await self._set(info.connection_id, status="active", clear_error=True)
        return await self._finish_activation(
            info,
            receipt.kinds,
            receipt.revision,
            receipt.relearn,
            resumed=True,
            expected_spec=SpecPin(receipt.spec_id, receipt.revision, receipt.spec_digest),
        )

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
            conn = await guard_source_write(
                db, self.ctx, info.user_id, info.connection_id, origin=info.origin
            )
            if conn is None or conn.status == "disabled":
                return {"status": "skipped", "detail": "disabled"}
            row = await revisions.add_revision(
                db, origin=info.origin, adapter="recipe", spec=spec, state="canary", digest=digest
            )
            await revisions.activate(db, row, now=self.services.clock.now())
            conn.adapter = "recipe"
            conn.spec_id = row.id
            conn.spec_revision = row.revision
            known_hosts: list[str] = list(conn.allow_hosts or [])
            conn.allow_hosts = sorted({*known_hosts, host})
            conn.status = "active"
            conn.error_code = conn.error_detail = conn.error_at = None
            await update_university(db, info.university_id, info.origin, warning)
            revision = row.revision
            if self._research is not None:
                # Hash the persisted JSON representation, including JSONB numeric normalization.
                await db.refresh(row, attribute_names=["spec"])
                job = await db.get(m.Job, self.ctx.job_id)  # already fenced/locked above
                if job is None:
                    raise JobClaimLost
                receipt = ActivationReceipt(
                    intent_key=intent_key(self.intent),
                    spec_id=row.id,
                    revision=revision,
                    spec_digest=research_digest(row.spec),
                    kinds=sorted(chosen),
                    relearn=relearn,
                )
                job.payload = {**job.payload, "_research_activation": receipt.model_dump()}
        await stage.finish("ok", f"Версия описания: {revision}")
        return await self._finish_activation(
            info,
            sorted(chosen),
            revision,
            relearn,
            expected_spec=SpecPin(row.id, revision, research_digest(row.spec))
            if self._research is not None
            else None,
        )

    async def _finish_activation(
        self,
        info: ConnInfo,
        kinds: list[Kind],
        revision: int,
        relearn: bool,
        *,
        resumed: bool = False,
        expected_spec: SpecPin | None = None,
    ) -> dict[str, Any]:
        outcome = await sync_connection(
            self.ctx,
            self.services,
            info.connection_id,
            transport=self.transport,
            retry=self.retry,
            queue_repair=not self._manual_connect,
            expected_spec=expected_spec,
        )
        await self._report_sync(outcome, prefix="Готово.")
        diagnostic_event(
            "learn.outcome",
            connection_id=info.connection_id,
            outcome="ok" if outcome.status == "ok" else "failed",
            chosen_count=len(kinds),
            revision=revision,
        )
        return {
            "status": outcome.status,
            "kinds": sorted(kinds),
            "revision": revision,
            "relearn": relearn,
            **({"resumed_activation": True} if resumed else {}),
            "sync": outcome.as_dict(),
        }

    async def _registry(self, info: ConnInfo, origin: str, warning: str | None) -> None:
        async with self.services.db() as db, db.begin():
            connection = await guard_source_write(
                db, self.ctx, info.user_id, info.connection_id, origin=origin
            )
            if connection is None or connection.status == "disabled":
                return
            await update_university(db, info.university_id, origin, warning)

    async def _fail(
        self, info: ConnInfo, detail: str, code: ErrorCode, *, say: bool = True
    ) -> dict[str, Any]:
        diagnostic_event(
            "learn.outcome",
            connection_id=info.connection_id,
            outcome="failed",
            error_code=code.value,
        )
        if say:
            await self.ui.say(f"Не получилось подключить: {detail}.")
        await self._set(info.connection_id, status="error", error=(code.value, detail))
        return {"status": "error", "detail": code.value}


def _safe_url(url: str) -> str | None:
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.hostname else None
