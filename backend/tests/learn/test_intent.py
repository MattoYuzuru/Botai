"""User intent and navigation addresses stay bounded and cannot be authored by a model."""

from __future__ import annotations

import json

import pytest

from botai.learn.addresses import navigation_url
from botai.learn.intent import (
    LearningIntent,
    apply_intent,
    intent_request,
    merge_intent,
    prepare_turn,
)

ENTRY = "https://fakeuni.test/schedule?begin_date=2026-10-26&view=week"


def test_entry_preserves_selected_week_and_spa_route() -> None:
    assert navigation_url(ENTRY) == ENTRY
    assert navigation_url("fakeuni.test/app#/schedule?week=2026-10-26") == (
        "https://fakeuni.test/app#/schedule?week=2026-10-26"
    )
    assert navigation_url("https://FAKEUNI.test:443/schedule?week=2026-10-26") == (
        "https://fakeuni.test/schedule?week=2026-10-26"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:fixture@fakeuni.test/schedule",
        "http://fakeuni.test/schedule",
        "https://fakeuni.test:8443/",
        "https://127.0.0.1/schedule",
        "https://127.1/schedule",
        "https://0x7f.1/schedule",
        "https://[::1]/",
        "https://fakeuni.test/schedule?access_token=fixture-secret",
        "https://fakeuni.test/callback?code=fixture-secret",
        "https://fakeuni.test/?SAMLResponse=fixture-secret",
        "https://fakeuni.test/app#/callback?token=fixture-secret",
        "https://fakeuni.test/app#access_token=fixture-secret",
        "https://fakeuni.test/schedule?x=abc0123456789def0123456789",
    ],
)
def test_secret_or_unsafe_addresses_never_enter_intent(url: str) -> None:
    assert navigation_url(url) is None
    turn = prepare_turn(f"Вот ссылка: {url}")
    assert not turn.links
    assert "fixture-secret" not in repr(turn)


def test_control_characters_are_not_a_navigation_address() -> None:
    assert navigation_url("https://fakeuni.test/\n/schedule") is None


def test_conversational_turn_uses_actual_link_and_can_exclude_optional_kind() -> None:
    turn = prepare_turn(f"Эта ссылка и есть расписание: {ENTRY}. Дедлайнов тут нет.")
    request = intent_request(turn, LearningIntent())
    assert request.purpose == "classify"
    assert request.messages[1].content.count("begin_date") == 1
    assert "2026-10-26" in request.messages[1].content
    result = apply_intent(
        LearningIntent(requested_kinds=("scheduleEvents", "deadlines")),
        turn,
        {
            "linkIndices": [0],
            "kinds": ["scheduleEvents"],
            "absentKinds": ["deadlines"],
            "note": "Здесь только расписание",
        },
    )
    assert result.entry_urls == (ENTRY,)
    assert result.requested_kinds == ("scheduleEvents",)
    assert result.absent_kinds == ("deadlines",)
    assert LearningIntent.restore(result.checkpoint()) == result
    assert ENTRY not in repr(result)


def test_later_message_can_reenable_a_kind_and_add_another_real_link() -> None:
    initial = LearningIntent((ENTRY,), ("scheduleEvents",), ("deadlines",))
    turn = prepare_turn("Дедлайны всё-таки есть: https://fakeuni.test/tasks")
    result = apply_intent(
        initial, turn, {"linkIndices": [0], "kinds": ["deadlines"], "absentKinds": []}
    )
    assert result.entry_urls == (ENTRY, "https://fakeuni.test/tasks")
    assert result.requested_kinds == ("scheduleEvents", "deadlines")
    assert result.absent_kinds == ()


@pytest.mark.parametrize(
    "answer",
    [
        {"url": "https://invented.test/"},
        {"linkIndices": [1]},
        {"linkIndices": [-1]},
        {"linkIndices": [True]},
        {"kinds": ["unknownKind"]},
        {"kinds": ["deadlines"], "absentKinds": ["deadlines"]},
        {"kinds": ["scheduleEvents"], "replaceKinds": "yes"},
    ],
)
def test_untrusted_classifier_cannot_invent_a_link_or_kind(answer: object) -> None:
    with pytest.raises(ValueError, match="learning"):
        apply_intent(LearningIntent(), prepare_turn(ENTRY), answer)


def test_rejected_url_and_labelled_secret_are_absent_from_prompt_and_checkpoint() -> None:
    turn = prepare_turn(
        "Расписание https://fakeuni.test/schedule?token=fixture-secret "
        "пароль: fixture-password. Вот безопасная ссылка https://fakeuni.test/schedule"
    )
    request = intent_request(turn, LearningIntent())
    checkpoint = apply_intent(LearningIntent(), turn, {"linkIndices": [0]}).checkpoint()
    sent = json.dumps(request.model_dump(), ensure_ascii=False) + json.dumps(checkpoint)
    assert "fixture-secret" not in sent
    assert "fixture-password" not in sent
    assert turn.links == ("https://fakeuni.test/schedule",)
    assert turn.rejected_links == 1


def test_checkpoint_restoration_drops_a_secret_address() -> None:
    restored = LearningIntent.restore(
        {
            "entry_urls": ["https://fakeuni.test/?token=fixture-secret", ENTRY],
            "requested_kinds": ["scheduleEvents"],
        }
    )
    assert restored.entry_urls == (ENTRY,)


@pytest.mark.parametrize(
    "text",
    [
        "пароль fixture-password",
        "password fixture-passphrase with spaces",
        "токен fixture-secret",
        "код подтверждения 424242",
    ],
)
def test_labelled_secrets_with_whitespace_are_removed_before_prompt_or_checkpoint(
    text: str,
) -> None:
    turn = prepare_turn(text)
    request = intent_request(turn, LearningIntent())
    checkpoint = apply_intent(LearningIntent(), turn, {}).checkpoint()
    sent = json.dumps(request.model_dump(), ensure_ascii=False) + json.dumps(checkpoint)
    for secret in (
        "fixture-password",
        "fixture-passphrase",
        "with spaces",
        "fixture-secret",
        "424242",
    ):
        assert secret not in sent


def test_api_length_turn_scans_and_redacts_the_tail_without_silent_truncation() -> None:
    text = "Обычный текст. " * 400 + "В конце сообщения."
    assert len(text) > 4000
    assert prepare_turn(text, max_length=8000).text.endswith("В конце сообщения.")
    secret_tail = "Обычный текст. " * 400 + "https://fakeuni.test/?token=fixture-secret"
    turn = prepare_turn(secret_tail, max_length=8000)
    assert turn.rejected_links == 1
    assert "fixture-secret" not in turn.text


def test_bare_dns_date_link_is_validated_and_canonicalized_before_prompt() -> None:
    turn = prepare_turn("Это расписание: fakeuni.test/schedule?begin_date=2026-10-26")
    assert turn.links == ("https://fakeuni.test/schedule?begin_date=2026-10-26",)
    assert turn.rejected_links == 0
    result = apply_intent(LearningIntent(), turn, {"linkIndices": [0]})
    assert result.entry_urls == turn.links
    assert "begin_date=2026-10-26" in intent_request(turn, LearningIntent()).messages[1].content


@pytest.mark.parametrize(
    "url",
    [
        "fakeuni.test/?access_token=fixture-secret",
        "fakeuni.test#access_token=fixture-secret",
        "fakeuni.test/app#/callback?code=fixture-secret",
        "fakeuni.test/schedule?token=fixture-secret",
    ],
)
def test_bare_dns_secret_query_and_hash_never_reach_prompt_or_checkpoint(url: str) -> None:
    turn = prepare_turn(f"Вот ссылка {url}")
    assert turn.rejected_links == 1
    assert turn.links == ()
    request = intent_request(turn, LearningIntent())
    checkpoint = apply_intent(LearningIntent(), turn, {}).checkpoint()
    sent = json.dumps(request.model_dump(), ensure_ascii=False) + json.dumps(checkpoint)
    assert "fixture-secret" not in sent
    assert url not in sent


def test_email_is_not_misidentified_as_a_bare_dns_navigation_link() -> None:
    turn = prepare_turn("Почта student@fakeuni.test")
    assert turn.links == ()
    assert turn.rejected_links == 0
    assert "<email>" in turn.text


def test_only_requested_kinds_replace_an_earlier_explicit_choice() -> None:
    result = apply_intent(
        LearningIntent(requested_kinds=("courses", "scheduleEvents", "deadlines")),
        prepare_turn("Оставь только расписание"),
        {"kinds": ["scheduleEvents"], "replaceKinds": True},
    )
    assert result.requested_kinds == ("scheduleEvents",)
    assert result.discovery_mode == "requested"
    assert LearningIntent.restore(result.checkpoint()) == result


# --- a source that is asked for again: what is asked now wins, what was said survives ------------

OLD_PAGE = "https://fakeuni.test/learn/timetable"
NEW_PAGE = "https://fakeuni.test/learn/tasks"


def test_a_new_ask_adds_its_kinds_note_and_page_to_the_old_one() -> None:
    stale = LearningIntent(
        (OLD_PAGE,), ("scheduleEvents",), (), "Подключение по ссылке на расписание", "requested"
    )
    fresh = LearningIntent(
        (NEW_PAGE,),
        ("scheduleEvents", "deadlines"),
        (),
        "Прочитай расписание и задания заново",
        "requested",
    )
    merged = merge_intent(stale, fresh)
    assert merged.requested_kinds == ("scheduleEvents", "deadlines")
    assert merged.user_note == "Прочитай расписание и задания заново"
    assert merged.entry_urls == (NEW_PAGE, OLD_PAGE)  # the page asked now first, the old one kept
    assert merged.discovery_mode == "requested"


def test_an_ask_without_kinds_keeps_what_the_student_named_before() -> None:
    stale = LearningIntent(
        (OLD_PAGE,), ("deadlines",), ("locations",), "только задания", "requested"
    )
    fresh = LearningIntent((OLD_PAGE,), ("scheduleEvents",), (), "", "auto")  # the bare default
    merged = merge_intent(stale, fresh)
    assert merged.requested_kinds == ("deadlines",)
    assert merged.absent_kinds == ("locations",)  # an exclusion survives
    assert merged.user_note == "только задания"  # the old note stands in for an empty one
    assert merged.discovery_mode == "requested"
    assert merged.entry_urls == (OLD_PAGE,)


def test_kinds_an_auto_research_found_are_findings_not_wishes() -> None:
    stale = LearningIntent(
        (OLD_PAGE,), ("scheduleEvents", "people", "courses", "locations"), (), "", "auto"
    )
    fresh = LearningIntent((OLD_PAGE,), ("scheduleEvents", "deadlines"), (), "", "requested")
    merged = merge_intent(stale, fresh)
    assert merged.requested_kinds == ("scheduleEvents", "deadlines")
    nothing_named = merge_intent(
        stale, LearningIntent((OLD_PAGE,), ("scheduleEvents",), (), "", "auto")
    )
    assert nothing_named.discovery_mode == "auto"
    assert nothing_named.requested_kinds == ("scheduleEvents",)


def test_naming_an_excluded_kind_again_lifts_the_exclusion() -> None:
    stale = LearningIntent((), ("scheduleEvents",), ("deadlines",), "", "requested")
    fresh = LearningIntent((), ("deadlines",), (), "", "requested")
    merged = merge_intent(stale, fresh)
    assert merged.absent_kinds == ()
    assert merged.requested_kinds == ("scheduleEvents", "deadlines")
    swapped = merge_intent(
        LearningIntent((), ("scheduleEvents",), (), "", "requested"),
        LearningIntent(
            (), ("deadlines",), ("scheduleEvents",), "вместо расписания задания", "requested"
        ),
    )
    assert (swapped.requested_kinds, swapped.absent_kinds) == (("deadlines",), ("scheduleEvents",))
    gone = merge_intent(
        LearningIntent((), ("scheduleEvents",), (), "", "requested"),
        LearningIntent((), ("scheduleEvents",), ("scheduleEvents",), "", "auto"),
    )
    assert gone.discovery_mode == "auto"  # nothing left to name: back to looking around


def test_an_old_note_does_not_outlive_an_ask_that_names_kinds() -> None:
    stale = LearningIntent((OLD_PAGE,), ("scheduleEvents",), (), "только расписание", "requested")
    fresh = LearningIntent((OLD_PAGE,), ("scheduleEvents", "deadlines"), (), "", "requested")
    merged = merge_intent(stale, fresh)
    assert merged.requested_kinds == ("scheduleEvents", "deadlines")
    assert merged.user_note == ""  # «only the schedule» was said before this ask, not with it


def test_naming_only_these_kinds_replaces_the_earlier_ones() -> None:
    stale = LearningIntent((), ("scheduleEvents", "courses"), ("locations",), "", "requested")
    fresh = LearningIntent((), ("deadlines",), (), "только задания", "requested")
    added = merge_intent(stale, fresh)
    assert added.requested_kinds == ("scheduleEvents", "courses", "deadlines")
    replaced = merge_intent(stale, fresh, replace=True)
    assert replaced.requested_kinds == ("deadlines",)
    assert replaced.absent_kinds == ("locations",)  # an exclusion is not a kind that was asked
    assert replaced.user_note == "только задания"
