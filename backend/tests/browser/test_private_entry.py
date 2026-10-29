"""Private, owner-confirmed acquisition context survives expiry without becoming a shared recipe."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

from botai.browser.session import decode_session, encode_session, session_storage_expiry
from botai.core.browser import SessionState
from botai.core.vault import SecretKind
from botai.sources.fetch import SourceClient
from botai.sources.session import dump_session, known_expired
from tests.browser.test_login import Rig, Student
from tests.browser.test_login import make_rig as make_rig
from tests.fakeuni import data, factory
from tests.fakeuni import login as fake_login
from tests.sources.helpers import NOW, make_ctx

ENTRY = data.ORIGIN + factory.ENTRY_PATH + "?view=private-acquisition-fixture"


@pytest.mark.parametrize("encode", [encode_session, dump_session])
def test_private_entry_is_masked_and_only_revealed_by_the_vault_serializer(
    encode: Callable[[SessionState], str],
) -> None:
    state = SessionState(origin=data.ORIGIN, captured_at=NOW, entry_url=SecretStr(ENTRY))
    assert ENTRY not in repr(state)
    assert ENTRY not in state.model_dump_json()
    assert "private-acquisition-fixture" not in state.model_dump_json()
    assert decode_session(encode(state)) == state
    assert ENTRY in encode(state)
    assert (
        decode_session(
            json.dumps({"origin": data.ORIGIN, "captured_at": NOW.isoformat()})
        ).entry_url
        is None
    )


@pytest.mark.parametrize(
    "entry",
    [
        "http://fakeuni.test/app",
        "https://other.test/app",
        "https://fakeuni.test:444/app",
        "https://user:pass@fakeuni.test/app",
        "https://@fakeuni.test/app",
        data.ORIGIN + "/bad\\path",
        data.ORIGIN + "/bad\npath",
        data.ORIGIN + "/a b",
        data.ORIGIN + "/" + "a" * 2000,
    ],
)
def test_private_entry_is_a_bounded_exact_origin_https_address(entry: str) -> None:
    with pytest.raises(ValidationError):
        SessionState(origin=data.ORIGIN, captured_at=NOW, entry_url=SecretStr(entry))


def test_private_context_retention_never_makes_expired_authentication_valid() -> None:
    expired = NOW - timedelta(seconds=1)
    state = SessionState(
        origin=data.ORIGIN,
        captured_at=expired,
        expires_hint=expired,
        entry_url=SecretStr(ENTRY),
    )
    assert session_storage_expiry(state) is None
    assert known_expired(state, NOW)
    state.entry_url = None
    assert session_storage_expiry(state) == expired


@pytest.mark.browser
async def test_plain_forced_reauth_returns_to_the_owners_confirmed_deep_entry_with_fresh_bearer(
    make_rig: Callable[..., Rig],
    fakeuni_app: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fake_login, "OTP_REQUIRED", False)
    rig = make_rig()
    first = await rig.flow.ensure_for_entry(
        rig.ctx(Student()),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=ENTRY,
    )
    assert first.entry_url == SecretStr(ENTRY)
    before_cookie = next(
        cookie.value for cookie in first.cookies if cookie.name == fake_login.COOKIE
    )
    before_bearer = next(
        scope.token for scope in first.bearer_scopes if scope.origin == data.ORIGIN
    )
    assert ENTRY not in json.dumps(rig.store.presets)
    assert factory.ENTRY_PATH not in json.dumps(rig.store.presets)

    # A fresh read-only browser used by discovery must keep the acquisition context.
    handle = await rig.gateway.open(
        user_id=rig.user,
        connection_id=rig.conn,
        allow_hosts=("fakeuni.test",),
        session=first,
        read_only=True,
        background=True,
    )
    try:
        await handle.goto(ENTRY)
        refreshed = await handle.export_session()
        assert refreshed.entry_url == SecretStr(ENTRY)
    finally:
        await handle.close()
    await rig.vault.put(rig.user, rig.conn, SecretKind.SESSION, encode_session(refreshed))

    fake_login.expire_all_sessions()
    student = Student()
    renewed = await rig.flow.ensure(
        rig.ctx(student),
        user_id=rig.user,
        connection_id=rig.conn,
        force=True,
    )
    assert renewed.entry_url == SecretStr(ENTRY)
    assert student.asked == []  # the owner already supplied the password; no model participates
    assert (
        next(cookie.value for cookie in renewed.cookies if cookie.name == fake_login.COOKIE)
        != before_cookie
    )
    assert (
        next(scope.token for scope in renewed.bearer_scopes if scope.origin == data.ORIGIN)
        != before_bearer
    )
    ctx = make_ctx(fakeuni_app, session=renewed)
    async with SourceClient(ctx, auth_type="session") as client:
        response = await client.request(
            "GET",
            data.ORIGIN + factory.DATA_PATH,
            auth_binding={"cookies": True, "bearer": "observedAuthorization"},
        )
    assert response.status_code == 200
    assert "days" in response.json()
    stored = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    assert stored is not None
    assert decode_session(stored.get_secret_value()).entry_url == SecretStr(ENTRY)
    assert ENTRY not in json.dumps(rig.store.presets)


@pytest.mark.browser
async def test_an_explicit_current_entry_overrides_the_saved_private_context(
    make_rig: Callable[..., Rig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig()
    state = SessionState(origin=data.ORIGIN, captured_at=NOW, entry_url=SecretStr(ENTRY))
    await rig.vault.put(rig.user, rig.conn, SecretKind.SESSION, encode_session(state))
    login = AsyncMock(return_value=SessionState(origin=data.ORIGIN, captured_at=NOW))
    monkeypatch.setattr(rig.flow, "_login", login)
    replacement = data.ORIGIN + "/another-confirmable-view"
    await rig.flow.ensure_for_entry(
        rig.ctx(None),
        user_id=rig.user,
        connection_id=rig.conn,
        entry_url=replacement,
        force=True,
    )
    assert login.await_args is not None
    assert login.await_args.kwargs["entry_url"] == replacement
