"""v4 text answers retain usable identity/session data without copying primary secrets."""

from __future__ import annotations

import pytest

from botai.core.errors import BotaiError
from botai.spaces.conversations import safe_text


@pytest.mark.parametrize(
    "text",
    [
        "student.42@example.test",
        "Логин student42, Иванова Анна Сергеевна, группа БПМ231",
        "Cookie: session=fixture-session-42; token=fixture-token-42",
    ],
)
def test_v4_text_answer_keeps_personal_and_session_data(text: str) -> None:
    assert safe_text(text, redact_personal=False) == text


@pytest.mark.parametrize(
    "text",
    ["пароль: fixture-password-42", "password=fixture-password-42", "код MFA: 654321"],
)
def test_v4_text_answer_hides_labelled_primary_secrets(text: str) -> None:
    assert safe_text(text, redact_personal=False).endswith("<secret>")


def test_v4_text_answer_keeps_secret_link_protection() -> None:
    with pytest.raises(BotaiError):
        safe_text("https://example.test/login?token=fixture", redact_personal=False)


def test_legacy_text_answer_still_pseudonymises_email() -> None:
    assert safe_text("student.42@example.test") == "<email>"
