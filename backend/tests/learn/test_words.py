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
        [words.reason("response_status"), words.reason("evidence_query_mismatch")]
    )
    assert words.why([]) == "разобрать не удалось"


def test_no_records_is_told_only_when_nothing_more_specific_is_known() -> None:
    assert words.why(["empty_result", "required_fields_missing"]) == words.reason(
        "required_fields_missing"
    )
    assert words.why(["empty_result"]) == words.reason("empty_result")


def test_a_page_is_named_by_its_title_never_by_its_address() -> None:
    assert words.page_name("Задания") == "«Задания»"
    assert words.page_name("  Мои   пары  ") == "«Мои пары»"
    assert len(words.page_name("я" * 200)) == 42
    # no title: the page is not named, and the student is not shown an address
    assert words.page_name("") == ""
    assert words.page_name("   ") == ""
    # a page that only said "not found" was no place to look
    assert words.page_name("Not Found") == ""
    assert words.page_name("Страница не найдена") == ""


def test_seen_names_pages_and_kinds_only() -> None:
    assert words.seen([], []) == ""
    assert words.seen(["«Мои пары»"], ["scheduleEvents", "deadlines"]) == (
        "Что нашла: открывала «Мои пары»; данные видны: расписание, дедлайны."
    )
