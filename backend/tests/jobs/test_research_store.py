"""Real Postgres checkpoint persistence; a narrow claim adapter isolates the store contract."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.ids import new_id
from botai.core.research import ResearchCheckpoint, ResearchUsage
from botai.core.vault import InMemoryVault, SealedSecret, SecretKind, VaultError
from botai.db.models import Connection, Job, JobResearchCheckpoint, SecretRow, User
from botai.jobs.research import (
    MAX_PAYLOAD_BYTES,
    ResearchConflict,
    ResearchFormatError,
    ResearchStore,
    purge_expired,
)
from tests.auth.support import make_user
from tests.jobs.env import Env

pytestmark = pytest.mark.db
MARKER = "private-frontier-checkpoint-FIXTURE"
INTENT = hashlib.sha256(b"schedule scope fixture").hexdigest()
OTHER_INTENT = hashlib.sha256(b"grades scope fixture").hexdigest()


class ClaimLost(RuntimeError):
    """The adapter's explicit failure, propagated unchanged by ResearchStore."""


@dataclass
class Guard:
    job_id: str
    user_id: str
    connection_id: str | None
    worker: str = "research-fixture"
    attempt: int = 1
    input_revision: int = 0
    guard_calls: list[int | None] = field(default_factory=list)

    async def assert_current(
        self, session: AsyncSession, *, input_revision: int | None = None
    ) -> None:
        self.guard_calls.append(input_revision)
        job = await session.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
        if (
            job is None
            or job.state not in {"running", "waiting_user"}
            or job.locked_by != self.worker
            or job.attempts != self.attempt
            or (
                input_revision is not None
                and job.payload.get("input_revision", 0) != input_revision
            )
        ):
            raise ClaimLost("test claim is no longer current")


async def context(env: Env, *, user_id: str | None = None) -> Guard:
    user = user_id or await env.owner()
    _, connection = await env.connection(user)
    job_id = new_id()
    async with env.sm() as session, session.begin():
        session.add(
            Job(
                id=job_id,
                user_id=user,
                connection_id=connection,
                kind="connect",
                state="running",
                locked_by="research-fixture",
                locked_at=env.clock.now(),
                attempts=1,
                run_after=env.clock.now(),
                payload={},
            )
        )
    return Guard(job_id, user, connection)


def store(env: Env) -> ResearchStore:
    # The real Vault factory returns PgVault. Its optional cipher capability never exposes keys.
    from botai.core.vault import CipherVault

    assert isinstance(env.services.vault, CipherVault)
    return ResearchStore(env.sm, env.services.vault, env.clock)


async def test_roundtrip_is_private_encrypted_and_persists_revisions(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    assert await research.load(ctx) is None
    checkpoint = await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    assert checkpoint.revision == 1
    assert MARKER not in repr(checkpoint)
    restored = await store(env).load(ctx)
    assert restored is not None
    assert restored.revision == 1
    assert restored.intent_key == INTENT
    assert restored.payload.get_secret_value() == MARKER
    assert ctx.guard_calls == [None, 0, None]

    async with env.sm() as session:
        row = await session.get(JobResearchCheckpoint, ctx.job_id)
        assert row is not None
        assert row.format_version == 1
        assert row.expires_at == env.clock.now() + timedelta(hours=24)
        assert MARKER.encode() not in row.ciphertext + row.wrapped_dek + row.nonce
        dump = await session.scalar(
            text(
                "select job_research_checkpoints::text from job_research_checkpoints where job_id=:j"
            ),
            {"j": ctx.job_id},
        )
        assert isinstance(dump, str)
        assert MARKER not in dump
        assert MARKER.encode().hex() not in dump
        assert not list(
            await session.scalars(select(SecretRow).where(SecretRow.user_id == ctx.user_id))
        )
        job = await session.get(Job, ctx.job_id)
        assert job is not None
        assert job.payload == {"_research_engine": 1}
        assert job.result is None
        assert job.error is None

    env.advance(hours=2)
    updated = await research.save(
        ctx, SecretStr("updated"), intent_key=OTHER_INTENT, expected_revision=1
    )
    assert updated.revision == 2
    async with env.sm() as session:
        row = await session.get(JobResearchCheckpoint, ctx.job_id)
        assert row is not None
        assert row.updated_at == env.clock.now()
        assert row.expires_at == env.clock.now() + timedelta(hours=24)
    assert (await research.load(ctx)) == updated


async def test_stale_cas_and_concurrent_writers_do_not_overwrite(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr("initial"), intent_key=INTENT, expected_revision=0)
    with pytest.raises(ResearchConflict, match="revision changed"):
        await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    outcomes = await asyncio.gather(
        research.save(ctx, SecretStr("writer-a"), intent_key=INTENT, expected_revision=1),
        research.save(ctx, SecretStr("writer-b"), intent_key=INTENT, expected_revision=1),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, ResearchCheckpoint) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ResearchConflict) for outcome in outcomes) == 1
    restored = await research.load(ctx)
    assert restored is not None
    assert restored.revision == 2
    assert restored.payload.get_secret_value() in {"writer-a", "writer-b"}


async def test_failed_initial_cas_does_not_publish_an_engine_marker(env: Env) -> None:
    ctx = await context(env)
    with pytest.raises(ResearchConflict, match="revision changed"):
        await store(env).save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=1)
    async with env.sm() as session:
        job = await session.get(Job, ctx.job_id)
        assert job is not None
        assert job.payload == {}
        assert await session.get(JobResearchCheckpoint, ctx.job_id) is None


@pytest.mark.parametrize("engine_marker", [0, 2, None, "1", True])
async def test_unknown_engine_marker_blocks_load_and_save_without_overwrite(
    env: Env, engine_marker: object
) -> None:
    ctx = await context(env)
    async with env.sm() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.id == ctx.job_id)
            .values(payload={"_research_engine": engine_marker})
        )
    research = store(env)
    with pytest.raises(ResearchFormatError, match="engine version"):
        await research.load(ctx)
    with pytest.raises(ResearchFormatError, match="engine version"):
        await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    async with env.sm() as session:
        job = await session.get(Job, ctx.job_id)
        assert job is not None
        assert job.payload == {"_research_engine": engine_marker}
        assert await session.get(JobResearchCheckpoint, ctx.job_id) is None


async def test_known_checkpoint_without_legacy_engine_marker_still_loads(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    saved = await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    async with env.sm() as session, session.begin():
        await session.execute(update(Job).where(Job.id == ctx.job_id).values(payload={}))
    assert await research.load(ctx) == saved


@pytest.mark.parametrize("change", [{"state": "queued"}, {"attempts": 2}, {"locked_by": "another"}])
async def test_stale_claim_cannot_load_save_or_delete_expired_state(
    env: Env, change: dict[str, object]
) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr("kept"), intent_key=INTENT, expected_revision=0)
    env.advance(hours=25)
    async with env.sm() as session, session.begin():
        await session.execute(update(Job).where(Job.id == ctx.job_id).values(**change))
    with pytest.raises(ClaimLost):
        await research.load(ctx)
    with pytest.raises(ClaimLost):
        await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=1)
    async with env.sm() as session:
        assert await session.get(JobResearchCheckpoint, ctx.job_id) is not None


async def test_changed_input_prevents_save_in_the_guard_transaction(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr("kept"), intent_key=INTENT, expected_revision=0)
    async with env.sm() as session, session.begin():
        await session.execute(
            update(Job).where(Job.id == ctx.job_id).values(payload={"input_revision": 1})
        )
    with pytest.raises(ClaimLost):
        await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=1)
    restored = await research.load(ctx)
    assert restored is not None
    assert restored.payload.get_secret_value() == "kept"


async def test_scope_must_match_job_even_when_the_guard_only_checks_claim(env: Env) -> None:
    ctx = await context(env)
    other = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr("kept"), intent_key=INTENT, expected_revision=0)
    for mismatch in (
        replace(ctx, user_id="wrong-user"),
        replace(ctx, connection_id=other.connection_id),
        replace(ctx, connection_id=None),
    ):
        with pytest.raises(ResearchConflict):
            await research.load(mismatch)
        with pytest.raises(ResearchConflict):
            await research.save(mismatch, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    assert await research.load(other) is None


@pytest.mark.parametrize("field", ["job", "user", "connection", "revision", "intent", "ciphertext"])
async def test_ciphertext_cannot_move_between_contexts_or_metadata(env: Env, field: str) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    target = ctx
    async with env.sm() as session, session.begin():
        row = await session.get(JobResearchCheckpoint, ctx.job_id)
        assert row is not None
        if field == "job":
            target = await context(env, user_id=ctx.user_id)
            row.job_id = target.job_id
            row.connection_id = target.connection_id or ""
        elif field == "user":
            user = await make_user(env.sm)
            env.users.append(user)
            target = replace(ctx, user_id=user)
            await session.execute(update(Job).where(Job.id == ctx.job_id).values(user_id=user))
            row.user_id = user
        elif field == "connection":
            other = await context(env)
            target = replace(ctx, connection_id=other.connection_id)
            await session.execute(
                update(Job).where(Job.id == ctx.job_id).values(connection_id=other.connection_id)
            )
            row.connection_id = other.connection_id or ""
        elif field == "revision":
            row.revision += 1
        elif field == "intent":
            row.intent_key = OTHER_INTENT
        else:
            row.ciphertext = row.ciphertext[:-1]
    with pytest.raises(VaultError) as error:
        await research.load(target)
    assert MARKER not in str(error.value)


async def test_unknown_format_fails_closed_without_mutation(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    async with env.sm() as session, session.begin():
        await session.execute(
            update(JobResearchCheckpoint)
            .where(JobResearchCheckpoint.job_id == ctx.job_id)
            .values(format_version=2)
        )
    with pytest.raises(ResearchFormatError):
        await research.load(ctx)
    with pytest.raises(ResearchFormatError):
        await research.save(ctx, SecretStr("new"), intent_key=INTENT, expected_revision=1)


async def test_utf8_bound_is_explicit_and_never_truncates(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    boundary = "я" * (MAX_PAYLOAD_BYTES // 2)
    await research.save(ctx, SecretStr(boundary), intent_key=INTENT, expected_revision=0)
    with pytest.raises(ValueError, match="512 KiB"):
        await research.save(ctx, SecretStr(boundary + "я"), intent_key=INTENT, expected_revision=1)
    restored = await research.load(ctx)
    assert restored is not None
    assert restored.revision == 1
    assert restored.payload.get_secret_value() == boundary
    with pytest.raises(ValueError, match="digest"):
        await research.save(ctx, SecretStr("x"), intent_key="not-a-digest", expected_revision=1)


async def test_expiry_discards_own_state_and_revision_restarts_at_one(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    env.advance(hours=24)
    assert await research.load(ctx) is None
    async with env.sm() as session:
        assert await session.get(JobResearchCheckpoint, ctx.job_id) is None
    checkpoint = await research.save(
        ctx, SecretStr("fresh"), intent_key=INTENT, expected_revision=0
    )
    assert checkpoint.revision == 1


async def test_cipher_failure_rolls_back_revision_and_expiry_refresh(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    await research.save(ctx, SecretStr("kept"), intent_key=INTENT, expected_revision=0)

    class BrokenCipher(InMemoryVault):
        def seal_value(
            self, user_id: str, scope_id: str, kind: SecretKind, value: str
        ) -> SealedSecret:
            raise VaultError("synthetic cipher failure")

    env.advance(hours=1)
    with pytest.raises(VaultError, match="synthetic cipher failure"):
        await ResearchStore(env.sm, BrokenCipher(), env.clock).save(
            ctx, SecretStr(MARKER), intent_key=OTHER_INTENT, expected_revision=1
        )
    restored = await research.load(ctx)
    assert restored is not None
    assert restored.revision == 1
    assert restored.intent_key == INTENT
    assert restored.payload.get_secret_value() == "kept"


async def test_maintenance_removes_only_expired_checkpoints(env: Env) -> None:
    old = await context(env)
    fresh = await context(env)
    research = store(env)
    await research.save(old, SecretStr("old"), intent_key=INTENT, expected_revision=0)
    env.advance(hours=23)
    await research.save(fresh, SecretStr("fresh"), intent_key=INTENT, expected_revision=0)
    env.advance(hours=1)
    assert await purge_expired(env.sm, env.clock.now()) == 1
    assert await research.load(old) is None
    assert await research.load(fresh) is not None
    assert await purge_expired(env.sm, env.clock.now()) == 0


@pytest.mark.parametrize("owner", ["job", "connection", "user"])
async def test_checkpoint_cascades_with_its_owner(env: Env, owner: str) -> None:
    ctx = await context(env)
    await store(env).save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0)
    async with env.sm() as session, session.begin():
        if owner == "job":
            await session.execute(delete(Job).where(Job.id == ctx.job_id))
        elif owner == "connection":
            await session.execute(delete(Connection).where(Connection.id == ctx.connection_id))
        else:
            await session.execute(delete(User).where(User.id == ctx.user_id))
    async with env.sm() as session:
        assert await session.get(JobResearchCheckpoint, ctx.job_id) is None


async def test_nonsecret_usage_survives_knowledge_ttl_and_failed_cas(env: Env) -> None:
    ctx = await context(env)
    research = store(env)
    usage = ResearchUsage(tokens=500, active_ms=1200, requests=20, steps=4)
    assert await research.load_usage(ctx) is None
    await research.save(ctx, SecretStr(MARKER), intent_key=INTENT, expected_revision=0, usage=usage)
    with pytest.raises(ResearchConflict):
        await research.save(
            ctx,
            SecretStr("late"),
            intent_key=INTENT,
            expected_revision=0,
            usage=ResearchUsage(tokens=1),
        )
    assert await research.load_usage(ctx) == usage
    env.advance(hours=25)
    assert await research.load(ctx) is None
    assert await research.load_usage(ctx) == usage
    async with env.sm() as session:
        job = await session.get(Job, ctx.job_id)
        assert job is not None
        assert MARKER not in str(job.payload)
        assert job.payload["_research_usage"] == {
            "tokens": 500,
            "active_ms": 1200,
            "requests": 20,
            "steps": 4,
        }
    await research.save(
        ctx, SecretStr("fresh knowledge"), intent_key=INTENT, expected_revision=0, usage=usage
    )
    assert await research.load_usage(ctx) == usage
