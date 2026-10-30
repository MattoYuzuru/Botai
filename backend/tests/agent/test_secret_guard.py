"""What counts as a password or a code typed into the chat while a protected field is open (L1)."""

from __future__ import annotations

import pytest

from botai.agent.conversations import looks_like_secret


@pytest.mark.parametrize(
    "text",
    [
        "Qwerty123",
        "пароль Qwerty123",
        "Пароль: abc",
        "это Qwerty123",
        "ivanov / Qwerty123",
        "мой пасс Qwerty123",
        "мой пароль qwerty",
        "пароль от ЛК qwerty12",
        "P@ssw0rd",
        "мойПароль",
        "correct-horse-battery-staple",
        "код 123 456",
        "код из смс 424242",
        "код подтверждения 1234",
        "123456",
        "424 242",
        "token=abcdef",
        "Cookie: a=b",
    ],
)
def test_a_password_or_a_code_is_recognised_in_any_phrase(text: str) -> None:
    assert looks_like_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "да",
        "ок",
        "спасибо",
        "Привет!",
        "?",
        "2",
        "ИВТ-21",
        "БПМ-231",
        "2101",
        "Москва2024",
        "Код группы 2101",
        "Где мой пароль?",
        "Забыл пароль",
        "Какой пароль нужно вводить в это поле?",
        "что-то",
        "по-русски",
        "Расписание на 28.10",
        "Привет, как дела",
        "Какое расписание на завтра?",
        "3 курс",
    ],
)
def test_the_words_of_a_conversation_and_the_names_of_groups_are_not(text: str) -> None:
    assert not looks_like_secret(text)
