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
import copy
import json
import logging
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from botai.contracts.agent_ui import SummaryGroup
from botai.contracts.specs import Kind
from botai.core.browser import SessionState
from botai.core.diagnostics import diagnostic_event
from botai.core.dialogue import DialogueContext
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import RetryPolicy
from botai.core.jobs import JobCancelled, JobContext
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn import revisions
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
from botai.learn.discovery import DiscoveryBudget, adaptive_observe
from botai.learn.intent import LearningIntent, apply_intent, intent_request, prepare_turn
from botai.learn.lint import LintContext, lint_spec
from botai.learn.observe import Observations
from botai.learn.redact import Redactor
from botai.learn.registry import find_warning, update_university
from botai.learn.ui import Ui, describe, item, kinds_text, sample_of
from botai.search import institutional_search_query
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
from botai.sources.scope import scoped_spec
from botai.sources.session import load_session
from botai.sources.vocab import KIND_LABELS, VOCAB

log = logging.getLogger(__name__)

DEFAULT_KINDS: tuple[Kind, ...] = ("scheduleEvents",)
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
        self._observed_session: SessionState | None = None
        self._observation_limited = False
        self._verification_failed = False
        self._scope_connection_id: str | None = None
        self._scope_base: tuple[Kind, ...] = ()
        self._scope_auto_base = False
        self._scope_replaced = False

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
        session = await load_session(vault, info.user_id, info.connection_id)
        if session is not None:
            r.protect_session(session)
        return r

    # --- entry points ------------------------------------------------------------------------

    def _start_intent(self, payload: dict[str, Any]) -> None:
        self._learning = True
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
            response = await self.services.llm.complete(intent_request(turn, self.intent))
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
        self._scope_connection_id = connection_id
        self._start_intent(payload)
        info = await self._info(connection_id)
        if info.status == "disabled":
            return {"status": "skipped", "detail": "disabled"}
        self._previous_status = info.status
        await self._set(connection_id, status="learning", clear_error=True)
        try:
            origin = await self._resolve_origin(info, payload)
            if origin is None:
                return await self._fail(
                    info, "Не получилось определить адрес сайта", ErrorCode.VALIDATION
                )
            info = await self._info(connection_id)
            await self._initial_note(await self._redactor(info))
            await self._checkpoint()
            await self._drain(await self._redactor(info))
            continued = await self._continue_foreign(info)
            if continued is not None:
                return continued
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
            await self._checkpoint()
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
            query = institutional_search_query(public_title)
            if query is not None:
                result = await self.services.search.search(
                    query,
                    budget=self.services.search.budget(),
                    user_id=self.ctx.user_id,
                    job_id=self.ctx.job_id,
                )
                if result.results:
                    # Search output is untrusted. Show only validated citation URLs with
                    # provenance, never titles/snippets as instructions or an egress grant.
                    candidates = "\n".join(
                        f"- <{hit.url}> (поиск: {hit.provider})" for hit in result.results
                    )
                    await self.ui.say(
                        "Публичный поиск нашёл возможные сайты:\n\n"
                        + candidates
                        + "\n\nЭто ещё не подтверждает доступ к вашим данным. "
                        "Пришлите точную ссылку на сайт, который нужно подключить."
                    )
                elif result.status in {"unavailable", "budget"}:
                    await self.ui.say(
                        "Публичный поиск сейчас недоступен. Подключение можно продолжить "
                        "по вашей ссылке на сайт."
                    )
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
        try:
            session = await svc.sessions.ensure(
                self.ctx, user_id=info.user_id, connection_id=info.connection_id
            )
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
            obs = await self._observe(info, session, redactor, extra)
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
                warning, spent = await find_warning(svc.llm, obs.plain_text)
                self.tokens += spent
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
                    if self.tokens > TOKEN_BUDGET:
                        diagnostic_event("learn.selection", outcome="failed", reason="token_budget")
                        break
                    found = await self._author_and_verify(
                        kind, text, lint_ctx, sctx, info, revision, digest
                    )
                    if await self._drain(redactor):
                        break  # A clarification during a canary must affect the final selection.
                    if found is not None:
                        chosen[kind] = found
                        pending.remove(kind)
            if self.intent != round_intent:
                continue
            if not pending or round_no == MAX_LINK_ROUNDS:
                break
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
                obs = await adaptive_observe(
                    handle,
                    info.origin,
                    redactor,
                    start_urls=start_urls,
                    intent=self.intent,
                    llm=self.services.llm,
                    budget=DiscoveryBudget(tokens=min(150_000, max(0, TOKEN_BUDGET - self.tokens))),
                )
            finally:
                await handle.close()
            self.tokens += obs.planner_tokens
            if not obs.auth_required and not obs.bot_protection:
                self._observed_session = session
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
            session = await self.services.sessions.ensure(
                self.ctx, user_id=info.user_id, connection_id=info.connection_id, force=True
            )
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
            self._verification_failed = True
            await stage.finish("failed", "Модель не вернула пригодный ответ")
            return None
        self.tokens += authored.tokens
        if not authored.candidates:
            self._verification_failed = self._verification_failed or bool(authored.rejected)
            reason = (
                "Проверка отклонила все варианты"
                if authored.rejected
                else "Подходящих данных не видно"
            )
            await stage.finish("skipped" if not authored.rejected else "failed", reason)
            diagnostic_event(
                "learn.selection",
                kind=kind,
                outcome="rejected" if authored.rejected else "not_found",
                reason="lint_rejected" if authored.rejected else "no_candidates",
                candidate_count=0,
            )
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
            self._verification_failed = True
            await stage.finish("failed", "Проверка на вашей сессии не прошла")
            diagnostic_event(
                "learn.selection",
                kind=kind,
                outcome="rejected",
                reason="canary_rejected",
                candidate_count=len(authored.candidates),
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
            candidate_count=len(authored.candidates),
            verified_count=len(verified),
            record_count=picked.report.count(kind),
        )
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
            report = await run_canary(
                scoped_spec(spec, self._data_scope()),
                sctx,
                require=[k for k in require if k in chosen],
            )
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
        outcome = await sync_connection(
            self.ctx, self.services, info.connection_id, transport=self.transport, retry=self.retry
        )
        await self._report_sync(outcome, prefix="Готово.")
        diagnostic_event(
            "learn.outcome",
            connection_id=info.connection_id,
            outcome="ok" if outcome.status == "ok" else "failed",
            chosen_count=len(chosen),
            revision=revision,
        )
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
