"""The words a student reads when research is left unfinished: every code has a phrase, none leaks."""

from __future__ import annotations

from typing import get_args

import pytest

from botai.learn import words
from botai.learn.canary import CanaryCode
from botai.learn.compiler import HINTS


@pytest.mark.parametrize("code", [*get_args(CanaryCode), *HINTS])
def test_every_code_of_the_checks_has_a_phrase_of_its_own(code: str) -> None:
    text = words.reason(code)
    assert text, code
    # `auth_*` and `evidence_*` share a phrase by family; nothing falls to the generic one
    assert text != words.GENERIC, code


def test_a_code_without_a_phrase_falls_back_to_one_sentence_not_to_the_code() -> None:
    assert words.reason("something.new") == words.GENERIC
    assert "something" not in words.why(["something.new"])


@pytest.mark.parametrize("text", [*words.REASONS.values(), *words.STOPS.values()])
def test_a_phrase_is_prose(text: str) -> None:
    assert not any(ch in text for ch in "_{}<>")
    assert text[0].islower()


def test_the_latest_two_different_reasons_are_told() -> None:
    codes = ["empty_result", "evidence_query_mismatch", "response_status", "empty_result"]
    assert words.why(codes) == "; ".join(
        [words.reason("empty_result"), words.reason("response_status")]
    )
    assert words.why([]) == "разобрать не удалось"


def test_a_page_is_named_by_its_title_or_its_path_and_an_error_page_is_not_a_place() -> None:
    assert words.page_name("/learn/tasks", "Задания") == "«Задания»"
    assert words.page_name("/learn/tasks?x=1", "") == "«/learn/tasks»"
    assert words.page_name("/api/docs", "Not Found") == ""
    assert words.page_name("/x", "  Мои   пары  ") == "«Мои пары»"
    assert len(words.page_name("/x", "я" * 200)) == 42


def test_seen_names_pages_and_kinds_only() -> None:
    assert words.seen([], []) == ""
    assert words.seen(["«Мои пары»"], ["scheduleEvents", "deadlines"]) == (
        "Что нашла: открывала «Мои пары»; данные видны: расписание, дедлайны."
    )
