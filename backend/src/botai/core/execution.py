"""Optional execution fences for persistent handlers; test contexts remain independent."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.jobs import JobContext
from botai.core.vault import SecretKind, Vault


@runtime_checkable
class GuardedJobContext(Protocol):
    job_id: str
    user_id: str
    connection_id: str | None

    @property
    def input_revision(self) -> int: ...

    async def assert_current(
        self, session: AsyncSession, *, input_revision: int | None = None
    ) -> None: ...


async def guard_job(ctx: object, session: AsyncSession, *, check_input: bool = False) -> None:
    if isinstance(ctx, GuardedJobContext):
        await ctx.assert_current(
            session, input_revision=ctx.input_revision if check_input else None
        )


@runtime_checkable
class AuthenticationContextProvider(Protocol):
    def authentication_context(self) -> JobContext: ...


def authentication_job(ctx: JobContext) -> JobContext:
    """Keep human login independent of new data-scope notes, within its original claim."""
    if isinstance(ctx, AuthenticationContextProvider):
        return ctx.authentication_context()
    return ctx


@runtime_checkable
class AuthenticationFence(Protocol):
    async def assert_authentication_current(self, session: AsyncSession) -> None: ...


async def guard_authentication_job(ctx: object, session: AsyncSession) -> None:
    if isinstance(ctx, AuthenticationFence):
        await ctx.assert_authentication_current(session)
    else:
        await guard_job(ctx, session, check_input=True)


@runtime_checkable
class SecretPublisher(Protocol):
    async def put_secret(
        self,
        user_id: str,
        connection_id: str,
        kind: SecretKind,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None: ...


async def save_secret(
    ctx: object,
    vault: Vault,
    user_id: str,
    connection_id: str,
    kind: SecretKind,
    value: str,
    *,
    expires_at: datetime | None = None,
) -> None:
    if isinstance(ctx, SecretPublisher):
        await ctx.put_secret(user_id, connection_id, kind, value, expires_at=expires_at)
    else:
        await vault.put(user_id, connection_id, kind, value, expires_at=expires_at)
