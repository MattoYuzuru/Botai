"""Identity of a v4 source: an address with a path, a hashed link, a file digest or a mailbox."""

from __future__ import annotations

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.sources.v4 import identity


def test_two_paths_of_one_host_are_two_sources() -> None:
    assert identity.site_key("https://LK.Example.org/schedule") == "https://lk.example.org/schedule"
    assert identity.site_key("https://lk.example.org/tasks") != identity.site_key(
        "https://lk.example.org/schedule"
    )


def test_case_default_port_trailing_slash_and_fragment_are_one_address() -> None:
    base = identity.site_key("https://lk.example.org/schedule")
    assert identity.site_key("https://LK.EXAMPLE.ORG:443/schedule/") == base
    assert identity.site_key("https://lk.example.org//schedule//") == base
    assert identity.site_key("https://lk.example.org/schedule#week-2") == base


def test_secret_query_is_dropped_and_the_rest_is_sorted() -> None:
    first = identity.site_key(
        "https://lk.example.org/schedule?view=week&group=2101&token=FIXTURE-SECRET"
    )
    second = identity.site_key(
        "https://lk.example.org/schedule?group=2101&access_token=other&view=week"
    )
    assert first == second == "https://lk.example.org/schedule?group=2101&view=week"
    assert "FIXTURE-SECRET" not in first


def test_idn_becomes_punycode() -> None:
    assert (
        identity.site_key("https://ПРИМЕР.рф/расписание")
        == "https://xn--e1afmkfd.xn--p1ai/расписание"
    )


def test_webcal_is_normalized_to_https_for_identity() -> None:
    assert (
        identity.site_key("webcal://calendar.example.org/export.ics")
        == "https://calendar.example.org/export.ics"
    )


def test_a_long_non_secret_id_stays_in_the_identity() -> None:
    course_id = "8f3a" * 12  # 48 hex characters: data, not a secret
    key = identity.site_key(f"https://lk.example.org/course?course={course_id}&view=week")
    assert course_id in key
    assert key == f"https://lk.example.org/course?course={course_id}&view=week"


def test_calendar_key_is_a_digest_and_never_the_link() -> None:
    link = "https://calendar.example.org/private/fixture-key/export.ics"
    key = identity.calendar_key(link)
    assert key.startswith("link:")
    assert len(key) == len("link:") + 64
    assert "fixture-key" not in key
    assert (
        identity.calendar_key("webcal://calendar.example.org/private/fixture-key/export.ics") == key
    )
    other = identity.calendar_key("https://calendar.example.org/private/another/export.ics")
    assert other != key


def test_file_key_is_the_digest() -> None:
    digest = "a" * 64
    assert identity.file_key(digest.upper()) == f"file:{digest}"
    with pytest.raises(BotaiError) as exc:
        identity.file_key("not-a-digest")
    assert exc.value.code is ErrorCode.VALIDATION


def test_mail_key_is_the_mailbox_in_lower_case() -> None:
    assert identity.mail_key(" Student@Example.ORG ") == "mail:student@example.org"
    with pytest.raises(BotaiError) as exc:
        identity.mail_key("not-a-mailbox")
    assert exc.value.code is ErrorCode.VALIDATION


def test_provisional_and_digest_helpers() -> None:
    assert identity.provisional_key("s1") == "draft:s1"
    assert identity.is_provisional("draft:s1")
    assert not identity.is_provisional("https://lk.example.org")
    assert identity.digest_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("", "Адрес пуст"),
        ("ftp://lk.example.org/x", "Ссылка должна начинаться"),
        ("https://user:password@lk.example.org/x", "логин или пароль"),
        ("https:///x", "нет имени сайта"),
        ("not an address", "Ссылка должна начинаться"),
    ],
)
def test_bad_addresses_are_validation_errors(url: str, detail: str) -> None:
    with pytest.raises(BotaiError) as exc:
        identity.site_key(url)
    assert exc.value.code is ErrorCode.VALIDATION
    assert detail in exc.value.detail


def test_overlong_address_is_rejected_not_truncated() -> None:
    with pytest.raises(BotaiError) as exc:
        identity.site_key("https://lk.example.org/" + "a" * identity.MAX_IDENTITY)
    assert exc.value.code is ErrorCode.VALIDATION
    assert "длинная" in exc.value.detail


def test_validate_identity_accepts_known_prefixes_only() -> None:
    assert identity.validate_identity("link:" + "a" * 64).startswith("link:")
    assert identity.validate_identity("mail:student@example.org").startswith("mail:")
    with pytest.raises(BotaiError) as exc:
        identity.validate_identity("example.org/schedule")
    assert exc.value.code is ErrorCode.VALIDATION
