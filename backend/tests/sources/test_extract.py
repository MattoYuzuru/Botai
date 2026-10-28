from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from botai.sources import rxsafe
from botai.sources.extract import (
    MISSING,
    apply_transform,
    html_to_text,
    html_value,
    parse_html,
    resolve,
    select_items,
    selector_valid,
)
from botai.sources.templates import TemplateError, render

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 27, 21, 30, tzinfo=UTC)  # already Oct 28 in Moscow


def test_week_start_is_local_monday_and_advances_without_a_frozen_literal() -> None:
    sunday_utc = datetime(2026, 11, 1, 21, 30, tzinfo=UTC)
    assert render("{weekStart}", now=NOW, tz=MSK, params={}) == "2026-10-26"
    assert render("{weekStart}", now=sunday_utc, tz=MSK, params={}) == "2026-11-02"
    assert render("{weekStart}", now=sunday_utc, tz=ZoneInfo("UTC"), params={}) == "2026-10-26"


# --- JSON Pointer ---------------------------------------------------------------------------


def test_pointer_follows_objects_arrays_and_escapes() -> None:
    doc = {"a": [{"b/c": 1, "d~e": 2}], "": 3}
    assert resolve(doc, "") == doc
    assert resolve(doc, "/a/0/b~1c") == 1
    assert resolve(doc, "/a/0/d~0e") == 2
    assert resolve(doc, "/") == 3


@pytest.mark.parametrize("pointer", ["/a/1", "/a/x", "/a/01", "/nope", "a", "/a/0/b/c"])
def test_pointer_missing_cases(pointer: str) -> None:
    assert resolve({"a": [{"b": 1}]}, pointer) is MISSING


# --- templates ------------------------------------------------------------------------------


def test_templates_use_the_local_day_and_unix_now() -> None:
    kwargs = {"now": NOW, "tz": MSK, "params": {"accountId": "u 1"}}
    assert render("{today}", **kwargs) == "2026-10-28"
    assert render("{plusDays:7}", **kwargs) == "2026-11-04"
    assert render("{minusDays:2}", **kwargs) == "2026-10-26"
    assert render("t={nowUnix}", **kwargs) == f"t={int(NOW.timestamp())}"
    assert render("{accountId}", **kwargs) == "u 1"


def test_template_path_values_are_fully_percent_encoded() -> None:
    out = render("/u/{id}/x", now=NOW, tz=MSK, params={"id": "../..//evil?x=1"}, in_path=True)
    assert out == "/u/..%2F..%2F%2Fevil%3Fx%3D1/x"
    assert out.count("/") == 3  # the value stays a single path segment
    with pytest.raises(TemplateError):
        render("/u/{id}/x", now=NOW, tz=MSK, params={"id": ".."}, in_path=True)


def test_unknown_placeholder_is_an_error_not_a_literal() -> None:
    with pytest.raises(TemplateError) as exc:
        render("{missing}", now=NOW, tz=MSK, params={})
    assert exc.value.name == "missing"


# --- transforms -----------------------------------------------------------------------------


def test_iso_naive_means_space_time_zone_and_comes_out_as_utc() -> None:
    assert apply_transform("iso", "2026-10-26 09:30", MSK) == "2026-10-26T06:30:00+00:00"
    assert apply_transform("iso", "2026-10-26T09:30:00+03:00", MSK) == "2026-10-26T06:30:00+00:00"
    assert apply_transform("iso", "2026-10-26T06:30:00Z", MSK) == "2026-10-26T06:30:00+00:00"
    assert apply_transform("iso", "2026-10-26", MSK) == "2026-10-25T21:00:00+00:00"


def test_iso_understands_russian_dates() -> None:
    assert apply_transform("iso", "27.10.2026 18:05", MSK) == "2026-10-27T15:05:00+00:00"
    assert apply_transform("iso", "5 ноября 2026", MSK) == "2026-11-04T21:00:00+00:00"
    assert apply_transform("iso", "не дата", MSK) is None
    assert apply_transform("iso", "31.02.2026", MSK) is None


def test_unix_transforms_reject_nonsense() -> None:
    assert apply_transform("unixSeconds", 1_793_000_000, MSK) == "2026-10-26T07:33:20+00:00"
    assert apply_transform("unixMillis", 1_793_000_000_000, MSK) == "2026-10-26T07:33:20+00:00"
    assert apply_transform("unixSeconds", 5, MSK) is None
    assert apply_transform("unixSeconds", True, MSK) is None


def test_number_bool_trim_and_joinnames() -> None:
    assert apply_transform("number", "1 234,5 баллов", MSK) == 1234.5
    assert apply_transform("number", "85", MSK) == 85
    assert apply_transform("number", "нет", MSK) is None
    assert apply_transform("bool", "Да", MSK) is True
    assert apply_transform("bool", "0", MSK) is False
    assert apply_transform("trim", "  a \n  b ", MSK) == "a b"
    assert apply_transform(
        "joinNames", [{"name": "Иванов И."}, {"fullName": "Петров П."}], MSK
    ) == ("Иванов И., Петров П.")
    assert apply_transform("joinNames", ["A", "B"], MSK) == "A, B"


def test_only_scalars_flow_on_and_control_characters_are_removed() -> None:
    assert apply_transform(None, {"a": 1}, MSK) is None
    assert apply_transform(None, [1], MSK) is None
    assert apply_transform(None, "a\x00b\x07c", MSK) == "abc"


def test_html2text_keeps_line_breaks_and_drops_scripts() -> None:
    text = html_to_text(
        "<p>Решите <b>1</b>–3</p><script>alert(1)</script><p>Срок: завтра<br>вечером</p>"
    )
    assert text == "Решите 1–3\nСрок: завтра\nвечером"


# --- HTML -----------------------------------------------------------------------------------

PAGE = """<ul>
<li class="it"><a href="/n/1">  Первая
  новость </a><time datetime="2026-10-27">27 октября</time><span class="r">№ 17/2026</span></li>
<li class="it"><a href="/n/2">Вторая</a></li></ul>"""


def test_html_fields_text_attr_pattern_and_missing() -> None:
    tree = parse_html(PAGE)
    first, second = select_items(tree, "li.it")
    kw = {"tz": MSK, "transform": None}
    assert html_value(first, selector="a", attr=None, pattern=None, **kw) == "Первая новость"
    assert html_value(first, selector="a", attr="href", pattern=None, **kw) == "/n/1"
    assert html_value(first, selector="span.r", attr=None, pattern=r"№ (\d+)/", **kw) == "17"
    assert html_value(first, selector="span.r", attr=None, pattern=r"zzz", **kw) is None
    assert html_value(second, selector="time", attr="datetime", pattern=None, **kw) is None
    assert html_value(
        first, selector="time", attr="datetime", pattern=None, tz=MSK, transform="iso"
    ) == ("2026-10-26T21:00:00+00:00")


def test_selector_validation_and_empty_match() -> None:
    assert selector_valid("li.it > a")
    assert not selector_valid("li[")
    assert select_items(parse_html(PAGE), "li[") == []


# --- restricted regexes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [r"(\d+)\.(\d+)", r"№ (\d{1,3})/", r"^(.*?) —", r"(?:ab){2,3}"],
)
def test_safe_patterns_are_accepted(pattern: str) -> None:
    assert rxsafe.problems(pattern) == []


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+$",
        r"(a*)*b",
        r"(a|aa)+c",
        r"(.*a){20}x",
        r"(a)\1",
        r"(?=a)b",
        r".*.*.*.*x",
        "(",
        "a" * 300,
    ],
)
def test_dangerous_patterns_are_rejected(pattern: str) -> None:
    assert rxsafe.problems(pattern) != []


def test_matching_is_bounded() -> None:
    assert rxsafe.first_group(r"(\d+)", "x" * 10_000 + "5") is None
    assert rxsafe.first_group(r"(\d+)", "abc 42") == "42"


# --- the session as the login flow stores it ----------------------------------------------


async def test_a_session_sealed_by_the_login_flow_is_readable_by_sync() -> None:
    from botai.browser.session import encode_session
    from botai.core.vault import InMemoryVault, SecretKind
    from botai.sources.session import cookie_jar, load_session
    from tests.sources.helpers import CONN_ID, NOW, USER_ID, session_state

    vault = InMemoryVault()
    sealed = encode_session(session_state("tok-from-login", expires_hint=NOW + timedelta(days=3)))
    await vault.put(USER_ID, CONN_ID, SecretKind.SESSION, sealed)
    state = await load_session(vault, USER_ID, CONN_ID)
    assert state is not None
    assert state.expires_hint == NOW + timedelta(days=3)
    assert cookie_jar(state, NOW).get("FAKEUNI_SESSION", domain="fakeuni.test") == "tok-from-login"
