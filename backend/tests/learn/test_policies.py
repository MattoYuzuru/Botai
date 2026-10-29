"""Combining recipe kinds preserves the authority of every copied request."""

from typing import Any

import pytest

from botai.core.errors import BotaiError
from botai.learn.policies import bind_parts


def test_mixed_retained_and_new_requests_keep_separate_credentials() -> None:
    retained: list[dict[str, Any]] = [{"request": {"path": "/old"}}]
    bootstrap: list[dict[str, Any]] = [{"path": "/bootstrap"}]
    fresh: list[dict[str, Any]] = [{"request": {"path": "/new", "authBinding": "observed"}}]
    bindings = bind_parts({"type": "cookie"}, retained, bootstrap)
    bindings.update(
        bind_parts(
            {
                "type": "session",
                "bindings": {"observed": {"cookies": True, "bearer": "observedAuthorization"}},
            },
            fresh,
            [],
        )
    )
    assert retained[0]["request"]["authBinding"] == bootstrap[0]["authBinding"] == "bCookies"
    assert fresh[0]["request"]["authBinding"] == "bCookiesBearer"
    assert bindings == {
        "bCookies": {"cookies": True, "bearer": "none"},
        "bCookiesBearer": {"cookies": True, "bearer": "observedAuthorization"},
    }


@pytest.mark.parametrize("header", [None, "Authorization", "authorization"])
def test_legacy_default_bearer_promotes_without_inventing_a_header(header: str | None) -> None:
    endpoints: list[dict[str, Any]] = [{"request": {"path": "/api"}}]
    assert bind_parts({"type": "bearer", "headerName": header}, endpoints, []) == {
        "bBearer": {"cookies": False, "bearer": "observedAuthorization"},
    }


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "bearer", "headerName": "X-Api-Token"},
        {"type": "unknown"},
        {"type": "session", "bindings": {}},
        {"type": "session", "bindings": {"old": {"cookies": True, "bearer": "guess"}}},
    ],
)
def test_unrepresentable_authority_is_rejected(auth: dict[str, Any]) -> None:
    with pytest.raises(BotaiError):
        bind_parts(auth, [{"request": {"path": "/api", "authBinding": "old"}}], [])
