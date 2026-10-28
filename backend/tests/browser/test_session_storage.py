"""Browser storage is bounded, origin-scoped and sealed as part of a connection session."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from botai.browser.probe import requires_authentication
from botai.browser.session import (
    MAX_STORAGE_BYTES,
    MAX_STORAGE_ENTRIES,
    MAX_STORAGE_VALUE_CHARS,
    bounded_browser_storage,
    decode_session,
    encode_session,
    storage_origin,
)
from botai.core.browser import PageSnapshot, SessionOriginStorage, SessionState

MARKER = "private-browser-storage-fixture"


def test_legacy_sessions_decode_with_empty_storage() -> None:
    state = decode_session(
        json.dumps({"origin": "https://fakeuni.test", "captured_at": datetime.now(UTC).isoformat()})
    )
    assert state.browser_storage == []


def test_browser_storage_values_are_masked_until_serialized_for_vault() -> None:
    state = SessionState(
        origin="https://fakeuni.test",
        captured_at=datetime.now(UTC),
        browser_storage=[
            SessionOriginStorage(
                origin="https://fakeuni.test",
                local_storage={"access_token": SecretStr(MARKER), "ready": SecretStr("true")},
                session_storage={"transient": SecretStr(MARKER)},
            )
        ],
    )
    assert MARKER not in repr(state)
    assert MARKER not in repr(state.browser_storage)
    assert MARKER not in state.model_dump_json()
    sealed_input = encode_session(state)
    assert MARKER in sealed_input
    restored = decode_session(sealed_input)
    assert restored.browser_storage[0].local_storage["access_token"].get_secret_value() == MARKER
    assert restored.browser_storage[0].session_storage["transient"].get_secret_value() == MARKER


def test_storage_bounds_and_exact_origin_allowlist_apply_on_restoration() -> None:
    states = [
        SessionOriginStorage(
            origin="https://fakeuni.test",
            local_storage={
                "huge": SecretStr("x" * (MAX_STORAGE_VALUE_CHARS + 1)),
                **{f"key-{i}": SecretStr(MARKER) for i in range(MAX_STORAGE_ENTRIES + 4)},
            },
        ),
        SessionOriginStorage(
            origin="https://fakeuni.test.evil.test", local_storage={"token": SecretStr(MARKER)}
        ),
        SessionOriginStorage(
            origin="http://fakeuni.test", session_storage={"token": SecretStr(MARKER)}
        ),
    ]
    bounded = bounded_browser_storage(states, ("fakeuni.test",))
    assert [entry.origin for entry in bounded] == ["https://fakeuni.test"]
    assert len(bounded[0].local_storage) == MAX_STORAGE_ENTRIES
    assert "huge" not in bounded[0].local_storage
    assert storage_origin("https://fakeuni.test:444/app", ("fakeuni.test",)) is None
    assert storage_origin("https://secret@fakeuni.test/app", ("fakeuni.test",)) is None


def test_storage_byte_budget_is_shared_between_origins_and_stores() -> None:
    state = SessionOriginStorage(
        origin="https://fakeuni.test",
        local_storage={f"local-{i}": SecretStr("x" * MAX_STORAGE_VALUE_CHARS) for i in range(20)},
        session_storage={
            f"session-{i}": SecretStr("x" * MAX_STORAGE_VALUE_CHARS) for i in range(20)
        },
    )
    bounded = bounded_browser_storage([state], ("fakeuni.test",))
    size = sum(
        len(key.encode()) + len(value.get_secret_value().encode())
        for entry in bounded
        for store in (entry.local_storage, entry.session_storage)
        for key, value in store.items()
    )
    assert size <= MAX_STORAGE_BYTES
    assert len(bounded[0].local_storage) < 20


@pytest.mark.parametrize(
    ("url", "text", "expected"),
    [
        ("https://fakeuni.test/app", "Вы не вошли в аккаунт. Для продолжения войдите", True),
        ("https://fakeuni.test/app", "You are not signed in", True),
        ("https://fakeuni.test/callback?error=login_required", "", True),
        ("https://fakeuni.test/app", "Мои курсы. Тестирование ПО", False),
    ],
)
def test_restored_page_authentication_is_checked_separately_from_api_status(
    url: str, text: str, expected: bool
) -> None:
    assert requires_authentication(PageSnapshot(url=url, text=text)) is expected


def test_html_auth_refusal_is_detected_without_page_text() -> None:
    snapshot = PageSnapshot(
        url="https://fakeuni.test/app",
        html="<main><h2>Вы не вошли в аккаунт</h2><p>Для продолжения войдите</p></main>",
    )
    assert requires_authentication(snapshot)


def test_explanatory_login_text_does_not_reject_an_authenticated_page() -> None:
    snapshot = PageSnapshot(
        url="https://fakeuni.test/app",
        html=(
            "<main><h1>Мои курсы</h1><p>"
            + "Учебные материалы. " * 150
            + "</p><h3>Помощь</h3><p>Please sign in again if you change your password.</p></main>"
        ),
    )
    assert not requires_authentication(snapshot)
