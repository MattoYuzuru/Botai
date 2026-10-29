"""Source authentication keeps the requested view and human hand-off across retries."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import pytest

from botai.core.browser import FakeBrowserGateway, FakePage, PageSnapshot, SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext, JobCancelled
from botai.core.llm import ScriptedLlm
from botai.db import models as m
from botai.learn.discovery import DiscoveryBudget, ObservationSession
from botai.learn.explorer import Explorer
from botai.learn.intent import LearningIntent
from botai.learn.observe import observations_of
from botai.learn.redact import Redactor
from tests.learn.conftest import EnvFactory
from tests.learn.test_discovery import ORIGIN, TrackingHandle, plan
from tests.sources.helpers import NOW, session_state
from tests.sources.support import FakeSessions


class EntrySessions(FakeSessions):
    def __init__(self) -> None:
        super().__init__()
        self.entries: list[tuple[str, bool, bool]] = []

    async def ensure_for_entry(
        self,
        ctx: object,
        *,
        user_id: str,
        connection_id: str,
        entry_url: str,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        self.entries.append((entry_url, force, manual))
        return await self.ensure(ctx, user_id=user_id, connection_id=connection_id, force=force)


async def test_source_session_receives_original_path_query_and_fragment(
    make_env: EnvFactory,
) -> None:
    sessions = EntrySessions()
    env = await make_env([], sessions=sessions)
    ctx = InMemoryJobContext(user_id=env.seeded.user_id)
    explorer = Explorer(ctx, env.services)
    entry = ORIGIN + "/courses/view?week=2026-10-26#classes"
    explorer.intent = LearningIntent(entry_urls=(entry,))
    info = await explorer._info(env.seeded.connection_id)

    await explorer._ensure_session(info)

    assert sessions.entries == [(entry, False, False)]


async def test_foreign_login_target_cannot_expand_source_access(make_env: EnvFactory) -> None:
    sessions = EntrySessions()
    env = await make_env([], sessions=sessions)
    explorer = Explorer(InMemoryJobContext(user_id=env.seeded.user_id), env.services)
    info = await explorer._info(env.seeded.connection_id)

    with pytest.raises(BotaiError) as error:
        await explorer._ensure_session(info, entry_url="https://foreign.fakeuni.test/login")

    assert error.value.code == ErrorCode.EGRESS_BLOCKED
    assert sessions.entries == []
    assert sessions.calls == 0


async def test_manual_login_precedes_existing_recipe_and_replays_without_model(
    make_env: EnvFactory,
) -> None:
    sessions = EntrySessions()
    env = await make_env([], sessions=sessions, with_spec=True, status="needs_reauth")

    result = await env.run(
        payload={
            "manual_login": True,
            "learning_intent": LearningIntent(
                entry_urls=(ORIGIN + "/",),
                requested_kinds=("scheduleEvents",),
                user_note="Дедлайнов здесь нет, нужны только пары",
            ).checkpoint(),
        }
    )

    assert result == {"status": "ok", "login_resumed": True}
    assert sessions.entries == [(ORIGIN + "/", True, True)]
    assert env.llm.requests == []
    assert await env.specs() == [(1, "active")]
    conn = await env.conn()
    assert conn.status == "active"
    assert conn.data_scope is None


async def test_cancelled_manual_login_preserves_configured_status_scope_and_rows(
    make_env: EnvFactory,
) -> None:
    class Cancelled(EntrySessions):
        async def ensure_for_entry(
            self,
            ctx: object,
            *,
            user_id: str,
            connection_id: str,
            entry_url: str,
            force: bool = False,
            manual: bool = False,
        ) -> SessionState:
            raise JobCancelled("synthetic cancellation")

    env = await make_env([], sessions=Cancelled(), with_spec=True)
    await env.sync()
    assert await env.count(m.ScheduleEvent) == 4

    with pytest.raises(JobCancelled):
        await env.run(payload={"manual_login": True, "kinds": ["scheduleEvents"]})

    conn = await env.conn()
    assert conn.status == "active"
    assert conn.data_scope is None
    assert await env.count(m.ScheduleEvent) == 4
    assert await env.specs() == [(1, "active")]


async def test_manual_request_fails_closed_for_legacy_provider(make_env: EnvFactory) -> None:
    sessions = FakeSessions()
    env = await make_env([], sessions=sessions)

    result = await env.run(payload={"manual_login": True, "kinds": ["scheduleEvents"]})

    assert result["status"] == "error"
    assert (await env.conn()).error_code == "source_unavailable"
    assert sessions.calls == 0
    assert env.llm.requests == []


async def test_deeper_auth_refusal_reauthenticates_that_page_not_original_home(
    make_env: EnvFactory,
) -> None:
    env = await make_env([])
    home, private = ORIGIN + "/", ORIGIN + "/classes?view=weekly"
    pages = {
        home: FakePage('<main>Курсы</main><a href="/classes?view=weekly">Пары</a>'),
        private: FakePage("<main>Please log in</main>"),
    }
    env.services.browser = FakeBrowserGateway(pages, NOW, ORIGIN)
    env.services.llm = ScriptedLlm(
        discovery_queue=[plan(1, "navigate", "l0"), plan(1, "navigate", "l0"), plan(2, "finish")]
    )

    class Restored(EntrySessions):
        async def ensure_for_entry(
            self,
            ctx: object,
            *,
            user_id: str,
            connection_id: str,
            entry_url: str,
            force: bool = False,
            manual: bool = False,
        ) -> SessionState:
            state = await super().ensure_for_entry(
                ctx,
                user_id=user_id,
                connection_id=connection_id,
                entry_url=entry_url,
                force=force,
                manual=manual,
            )
            env.services.browser = FakeBrowserGateway(
                {**pages, private: FakePage("<main>Мои пары</main>")}, NOW, ORIGIN
            )
            return state

    sessions = Restored()
    sessions.vault = env.vault
    env.services.sessions = sessions
    explorer = Explorer(InMemoryJobContext(user_id=env.seeded.user_id), env.services)
    explorer.intent = LearningIntent(entry_urls=(home,))
    info = await explorer._info(env.seeded.connection_id)

    obs = await explorer._observe(info, session_state(), Redactor(), [])

    assert sessions.entries == [(private, True, False)]
    assert not obs.auth_required
    assert explorer._reauth_attempted


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("captcha", [False, True])
async def test_refusal_target_keeps_requested_url_private_and_not_redirect_url(
    adaptive: bool,
    captcha: bool,
) -> None:
    private = ORIGIN + "/students/24680135/schedule?view=mine"
    refused = PageSnapshot(
        url=ORIGIN + "/signin",
        html="<main>Browser check</main>" if captcha else "<main>Please log in</main>",
        captcha_suspected=captcha,
    )
    if adaptive:

        class Redirect(TrackingHandle):
            async def goto(self, url: str) -> PageSnapshot:
                await super().goto(url)
                return refused

        state = ObservationSession(
            Redirect({private: FakePage(refused.html)}), ORIGIN, Redactor(), DiscoveryBudget()
        )
        await state.visit(private)
        obs = state.bundle()
    else:
        obs = observations_of(ORIGIN, Redactor(), [(private, refused)], [])

    assert obs.auth_required is not captcha
    assert obs.bot_protection is captcha
    assert obs.auth_target_url == private
    assert private not in obs.render()
    assert private not in repr(obs)
    assert "auth_target_url" not in obs.render()
