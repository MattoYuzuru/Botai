"""A scope note waits for human login; claim, binding and research fences still apply."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from botai.browser.presets import PgLoginStore
from botai.contracts.agent_ui import TextBlock
from botai.core.execution import authentication_job, guard_job, save_secret
from botai.core.ids import new_id
from botai.core.jobs import JobClaimLost, JobInputChanged, JobKind
from botai.core.vault import SecretKind
from botai.db.models import Job, LoginPresetRow
from botai.jobs import dialogue
from botai.jobs.context import PgJobContext
from tests.browser.test_login import preset_dict
from tests.jobs.env import Env
from tests.jobs.test_fencing import context

pytestmark = pytest.mark.db


async def awaiting_authentication(env: Env) -> tuple[PgJobContext, str]:
    user = (await env.account()).id
    origin = f"https://auth-{new_id().lower()}.fakeuni.test"
    _, connection = await env.connection(user, origin=origin)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=user,
        connection_id=connection,
        payload={"_research_engine": 1},
    )
    worker = env.worker({}, name="login-owner")
    claim = await worker.claim()
    assert claim is not None
    assert claim.id == job_id
    return context(env, worker, claim), origin


async def scope_note(env: Env, ctx: PgJobContext) -> None:
    async with env.sm() as session, session.begin():
        row = await session.scalar(select(Job).where(Job.id == ctx.job_id).with_for_update())
        assert row is not None
        dialogue.advance_input(row)


async def test_login_scope_note_does_not_revoke_authentication_or_adopt_research_input(
    env: Env,
) -> None:
    ctx, origin = await awaiting_authentication(env)
    auth = authentication_job(ctx)
    connection = ctx.connection_id
    assert connection is not None
    await scope_note(env, ctx)

    await auth.status("Завершаю вход")
    await auth.emit(TextBlock(id=new_id(), at=env.clock.now(), markdown="Вход подтверждён"))
    for kind in (SecretKind.SESSION, SecretKind.LOGIN, SecretKind.PASSWORD):
        await save_secret(auth, env.services.vault, ctx.user_id, connection, kind, "fixture")
        assert await env.services.vault.has(ctx.user_id, connection, kind)

    store = PgLoginStore(env.sm)
    host = origin.removeprefix("https://")
    preset = preset_dict(origin + "/login", (host,), probe=False)
    preset["success"] = {"urlOrigin": origin}
    try:
        await store.allow_host(ctx.user_id, connection, "id.fakeuni.test", ctx=auth)
        assert await store.save_preset(origin, preset, connection_id=connection, ctx=auth) == 1
        assert (await store.connection(ctx.user_id, connection)).preset_id is not None

        # The temporary auth capability never grants data/canary/checkpoint publication.
        for research_ctx in (ctx, auth):
            async with env.sm() as session:
                with pytest.raises(JobInputChanged):
                    await guard_job(research_ctx, session, check_input=True)
        with pytest.raises(JobInputChanged):
            await ctx.status("Старое исследование")
        with pytest.raises(JobInputChanged):
            await save_secret(
                ctx, env.services.vault, ctx.user_id, connection, SecretKind.SESSION, "late"
            )
        with pytest.raises(JobInputChanged):
            await store.allow_host(ctx.user_id, connection, "late.fakeuni.test", ctx=ctx)
        with pytest.raises(JobInputChanged):
            await store.save_preset(origin, preset, connection_id=connection, ctx=ctx)
        assert ctx.input_revision == 0
        assert await ctx.drain_dialogue() == []
        assert ctx.input_revision == 1
        async with env.sm() as session:
            await guard_job(ctx, session, check_input=True)
    finally:
        async with env.sm() as session, session.begin():
            await session.execute(delete(LoginPresetRow).where(LoginPresetRow.origin == origin))


@pytest.mark.parametrize("revocation", ["reclaimed", "cancelled", "rebound"])
async def test_authentication_view_cannot_publish_after_its_claim_or_binding_is_revoked(
    env: Env, revocation: str
) -> None:
    ctx, origin = await awaiting_authentication(env)
    auth = authentication_job(ctx)
    connection = ctx.connection_id
    assert connection is not None
    if revocation == "reclaimed":
        env.advance(minutes=3)
        survivor = env.worker({}, name="new-login-owner")
        assert await survivor.recover_orphans() == 1
        assert await survivor.claim() is not None
    elif revocation == "cancelled":
        from botai.jobs.answers import cancel_job

        async with env.sm() as session:
            await cancel_job(session, env.services.vault, env.clock, ctx.user_id, ctx.job_id)
    else:
        _, replacement = await env.connection(ctx.user_id)
        async with env.sm() as session, session.begin():
            row = await session.get(Job, ctx.job_id)
            assert row is not None
            row.connection_id = replacement

    with pytest.raises(JobClaimLost):
        await auth.status("Старый вход")
    with pytest.raises(JobClaimLost):
        await save_secret(
            auth, env.services.vault, ctx.user_id, connection, SecretKind.SESSION, "stale"
        )
    store = PgLoginStore(env.sm)
    with pytest.raises(JobClaimLost):
        await store.allow_host(ctx.user_id, connection, "late.fakeuni.test", ctx=auth)
    with pytest.raises(JobClaimLost):
        await store.save_preset(
            origin, preset_dict(origin + "/login"), connection_id=connection, ctx=auth
        )
    assert not await env.services.vault.has(ctx.user_id, connection, SecretKind.SESSION)
    assert await store.preset_for(origin) is None
