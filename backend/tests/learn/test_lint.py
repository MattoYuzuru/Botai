"""Spec lint: valid specs pass; the poisoned and leaking ones do not (security checks C4, C5)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from botai.contracts.schemas import errors
from botai.core.errors import BotaiError, ErrorCode
from botai.learn.lint import LintContext, lint_spec
from tests.fakeuni import data, moodle
from tests.sources import recipes

CTX = LintContext(allowed_hosts=["fakeuni.test"], expected_origin="https://fakeuni.test")
MARKER = "ZZ-LEAK-MARKER"


def observed_from_fakeuni() -> list[str]:
    leaves: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif node is not None:
            leaves.append(str(node))

    walk(data.timetable())
    walk(data.deadlines())
    walk(
        {
            "sub": "u-1001",
            "fullName": "Иванова Анна",
            "programme": "Прикладная математика",
            "year": 3,
        }
    )
    return [*leaves, "Перенос занятий 4 ноября", "Запись на пересдачу открыта"]


FULL_CTX = LintContext(
    allowed_hosts=["fakeuni.test"],
    expected_origin="https://fakeuni.test",
    observed=observed_from_fakeuni(),
)


def edit(spec: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    out = copy.deepcopy(spec)
    mutate(out)
    return out


def ep(spec: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return spec["recipe"]["endpoints"][index]


# --- valid ----------------------------------------------------------------------------------


def test_fixture_specs_pass_with_the_users_observations() -> None:
    for spec in (
        recipes.full_recipe(),
        recipes.recipe_spec([recipes.deadlines_html_endpoint()], require=("deadlines",)),
    ):
        result = lint_spec(spec, FULL_CTX)
        assert result.ok, result.reasons()


def test_contract_examples_pass_the_schema_part() -> None:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "examples"
        / "valid"
        / "adapter-spec.json"
    )
    for spec in json.loads(path.read_text()):
        assert errors("adapter-spec.schema.json", spec) == []


def test_other_adapters_pass() -> None:
    lms = LintContext(allowed_hosts=["lms.fakeuni.test"])
    moodle_spec = {
        "adapter": "moodle_ws",
        "origin": "https://lms.fakeuni.test",
        "functions": ["core_enrol_get_users_courses"],
    }
    ical_spec = {
        "adapter": "ical",
        "origin": "https://lms.fakeuni.test",
        "classify": {
            "deadlineIf": {
                "zeroDuration": True,
                "categoryIn": ["Deadline"],
                "summaryPattern": "защит",
            }
        },
    }
    assert lint_spec(moodle_spec, lms).ok
    assert lint_spec(ical_spec, lms).ok
    document = {
        "adapter": "document",
        "source": {"kind": "url", "url": "https://files.fakeuni.test/exams.xlsx"},
        "format": "xlsx",
        "table": {
            "kind": "scheduleEvents",
            "columns": {
                "title": {"column": "Предмет"},
                "date": {"column": "Дата", "transform": "date"},
            },
            "rowFilter": {"column": "ФИО", "equalsParam": "fullName"},
        },
    }
    assert lint_spec(document, LintContext(allowed_hosts=["files.fakeuni.test"])).ok


# --- schema ---------------------------------------------------------------------------------


def test_schema_violations_are_reported_without_running_other_checks() -> None:
    bad = {"adapter": "python", "code": "import os"}
    result = lint_spec(bad, CTX)
    assert result.codes() == {"schema"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: ep(s)["request"].update(path="https://evil.test/x"),
        lambda s: ep(s)["request"].update(path="/a/b#frag"),
        lambda s: ep(s)["request"].update(path="/api\\admin"),
        lambda s: s["recipe"].update(origin="https://user:pw@fakeuni.test"),
        lambda s: ep(s)["request"].update(method="POST"),
        lambda s: ep(s)["request"].update(method="DELETE"),
        lambda s: ep(s)["request"]["query"].update({"bad key": "x"}),
        lambda s: ep(s)["response"]["fields"].update({"bad-name": {"path": "/x"}}),
    ],
)
def test_contract_schema_blocks_structural_abuse(mutate: Callable[[dict[str, Any]], None]) -> None:
    result = lint_spec(edit(recipes.recipe_spec(), mutate), CTX)
    assert "schema" in result.codes()


# --- addresses and hosts --------------------------------------------------------------------

CASES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "protocol-relative path",
        lambda s: ep(s)["request"].update(path="//evil.test/x"),
        "url.protocol_relative",
    ),
    ("dot segments", lambda s: ep(s)["request"].update(path="/api/../admin"), "url.dot_segment"),
    (
        "encoded dot segments",
        lambda s: ep(s)["request"].update(path="/api/%2e%2e/admin"),
        "url.dot_segment",
    ),
    (
        "double encoded dots",
        lambda s: ep(s)["request"].update(path="/api/%252e%252e/admin"),
        "url.dot_segment",
    ),
    ("single dot segment", lambda s: ep(s)["request"].update(path="/api/./x"), "url.dot_segment"),
    (
        "foreign origin",
        lambda s: s["recipe"].update(origin="https://evil.test"),
        "host.not_allowed",
    ),
    (
        "origin differs from connection",
        lambda s: s["recipe"].update(origin="https://www.fakeuni.test"),
        "host.not_allowed",
    ),
    (
        "absolute url in query",
        lambda s: ep(s)["request"]["query"].update(next="https://evil.test/c"),
        "url.absolute",
    ),
    (
        "scheme-less host in query",
        lambda s: ep(s)["request"]["query"].update(next="evil.example.com"),
        "url.host_in_value",
    ),
    (
        "ip literal origin",
        lambda s: s["recipe"].update(origin="https://169.254.169.254"),
        "host.ip_literal",
    ),
    (
        "localhost origin",
        lambda s: s["recipe"].update(origin="https://localhost"),
        "host.ip_literal",
    ),
    # personal values
    (
        "e-mail in query",
        lambda s: ep(s)["request"]["query"].update(owner="anna@fakeuni.test"),
        "personal.email",
    ),
    (
        "phone in query",
        lambda s: ep(s)["request"]["query"].update(phone="+7 (999) 123-45-67"),
        "personal.phone",
    ),
    (
        "jwt in query",
        lambda s: ep(s)["request"]["query"].update(
            t="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        ),
        "personal.jwt",
    ),
    (
        "uuid in path",
        lambda s: ep(s)["request"].update(path="/api/2b1f0e0a-9c1d-4f6e-8a41-0a1b2c3d4e5f/tt"),
        "personal.uuid",
    ),
    (
        "student number in path",
        lambda s: ep(s)["request"].update(path="/api/students/12345678/timetable"),
        "personal.digits",
    ),
    (
        "unix timestamp literal",
        lambda s: ep(s)["request"]["query"].update({"from": "1793000000"}),
        "personal.digits",
    ),
    (
        "hex token in query",
        lambda s: ep(s)["request"]["query"].update(
            k="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        ),
        "personal.token",
    ),
    ("snils", lambda s: s["recipe"].update(notes="СНИЛС 112-233-445 95"), "personal.snils"),
    (
        "e-mail in notes",
        lambda s: s["recipe"].update(notes="пишет ivanova@fakeuni.test"),
        "personal.email",
    ),
    # observed values
    (
        "observed id in path",
        lambda s: ep(s)["request"].update(path="/api/students/u-1001/timetable"),
        "observed.value",
    ),
    (
        "observed id in query",
        lambda s: ep(s)["request"]["query"].update(account="u-1001"),
        "observed.value",
    ),
    (
        "observed name in selector",
        lambda s: ep(s, 2)["response"]["fields"]["title"].update(
            selector="a[title='Иванова Анна']"
        ),
        "observed.value",
    ),
    (
        "observed name in notes",
        lambda s: s["recipe"].update(notes="Расписание студентки Иванова Анна"),
        "observed.value",
    ),
    (
        "observed course in pattern",
        lambda s: ep(s, 2)["response"]["fields"]["title"].update(pattern="Математический анализ"),
        "observed.value",
    ),
    # a read that is really an action
    ("logout", lambda s: ep(s)["request"].update(path="/auth/logout"), "url.destructive"),
    ("sign out", lambda s: ep(s)["request"].update(path="/api/sign-out"), "url.destructive"),
    ("delete", lambda s: ep(s)["request"].update(path="/api/news/delete/17"), "url.destructive"),
    ("edit", lambda s: ep(s)["request"].update(path="/portal/edit-profile"), "url.destructive"),
    ("submit", lambda s: ep(s)["request"].update(path="/tasks/submit"), "url.destructive"),
    (
        "russian logout",
        lambda s: ep(s)["request"].update(path="/%D0%B2%D1%8B%D1%85%D0%BE%D0%B4"),
        "url.destructive",
    ),
    # names
    (
        "secret query key",
        lambda s: ep(s)["request"]["query"].update(sesskey="{today}"),
        "param.secret_name",
    ),
    (
        "token query key",
        lambda s: ep(s)["request"]["query"].update(access_token="{today}"),
        "param.secret_name",
    ),
    (
        "session query key",
        lambda s: ep(s)["request"]["query"].update(session="{today}"),
        "param.secret_name",
    ),
    (
        "email query key",
        lambda s: ep(s)["request"]["query"].update(email="{today}"),
        "param.secret_name",
    ),
    (
        "cookie header",
        lambda s: s["recipe"]["auth"].update(type="bearer", headerName="Cookie"),
        "auth.header",
    ),
    (
        "unknown placeholder",
        lambda s: ep(s)["request"]["query"].update(x="{studentId}"),
        "template.unknown",
    ),
    (
        "malformed placeholder",
        lambda s: ep(s)["request"]["query"].update(x="{today"),
        "template.malformed",
    ),
    (
        "pointer to a token",
        lambda s: ep(s)["response"]["fields"]["title"].update(path="/accessToken"),
        "pointer.secret",
    ),
    # code-like content
    (
        "unsafe regexp",
        lambda s: ep(s, 2)["response"]["fields"]["title"].update(pattern="(a+)+$"),
        "pattern.unsafe",
    ),
    (
        "backreference",
        lambda s: ep(s, 2)["response"]["fields"]["title"].update(pattern=r"(a)\1"),
        "pattern.unsafe",
    ),
    (
        "has selector",
        lambda s: ep(s, 2)["response"].update(itemsSelector="li:has(a)"),
        "html.selector",
    ),
    (
        "pseudo-element",
        lambda s: ep(s, 2)["response"].update(itemsSelector="li::before"),
        "html.selector",
    ),
    (
        "broken selector",
        lambda s: ep(s, 2)["response"].update(itemsSelector="li["),
        "html.selector",
    ),
    (
        "event handler attr",
        lambda s: ep(s, 2)["response"]["fields"]["url"].update(attr="onclick"),
        "html.attr",
    ),
    (
        "secret attr",
        lambda s: ep(s, 2)["response"]["fields"]["url"].update(attr="data-csrf"),
        "html.attr",
    ),
    # structure
    (
        "post outside allowlist",
        lambda s: ep(s)["request"].update(method="POST", readOnlyPost=True),
        "method.post_not_allowed",
    ),
    (
        "field outside vocabulary",
        lambda s: ep(s)["response"]["fields"].update(passportNumber={"path": "/passport"}),
        "field.unknown",
    ),
    ("no identity", lambda s: ep(s)["response"]["fields"].pop("externalId"), "identity.missing"),
    (
        "identity of a missing field",
        lambda s: ep(s)["response"].update(identity=["nope"]),
        "identity.unknown_field",
    ),
    (
        "required field missing",
        lambda s: ep(s)["response"]["fields"].pop("startsAt"),
        "field.missing_required",
    ),
    ("duplicate endpoint id", lambda s: ep(s, 1).update(id="timetable"), "endpoint.duplicate"),
    (
        "required kind without endpoint",
        lambda s: s["recipe"]["validation"].update(requireKinds=["scheduleEvents", "grades"]),
        "validation.kind_not_covered",
    ),
    (
        "cursor without path",
        lambda s: ep(s).update(pagination={"type": "cursor", "param": "c"}),
        "pagination.cursor",
    ),
    (
        "secret pagination param",
        lambda s: ep(s).update(pagination={"type": "page", "param": "token"}),
        "param.secret_name",
    ),
]


@pytest.mark.parametrize(("name", "mutate", "code"), CASES, ids=[c[0] for c in CASES])
def test_poisoned_recipes_are_rejected(
    name: str, mutate: Callable[[dict[str, Any]], None], code: str
) -> None:
    base = recipes.full_recipe()
    result = lint_spec(edit(base, mutate), FULL_CTX)
    assert code in result.codes(), (name, result.reasons())


def test_bootstrap_rules() -> None:
    spec = recipes.full_recipe()
    spec["recipe"]["bootstrap"] = [
        {"id": "account", "method": "GET", "path": "/api/me", "extract": {"accountId": "/sub"}}
    ]
    ep(spec)["request"]["path"] = "/api/users/{accountId}/tt"
    assert lint_spec(spec, CTX).ok
    # a parameter named like a secret, an extract pointing at one, a use before definition
    leaky = edit(
        spec, lambda s: s["recipe"]["bootstrap"][0].update(extract={"sessionToken": "/sub"})
    )
    ep(leaky)["request"]["path"] = "/api/users/{sessionToken}/tt"
    assert "template.secret_name" in lint_spec(leaky, CTX).codes()
    pointer = edit(
        spec, lambda s: s["recipe"]["bootstrap"][0].update(extract={"accountId": "/csrfToken"})
    )
    assert "pointer.secret" in lint_spec(pointer, CTX).codes()
    early = edit(spec, lambda s: s["recipe"]["bootstrap"][0].update(path="/api/{accountId}/me"))
    assert "template.unknown" in lint_spec(early, CTX).codes()


# --- other adapters -------------------------------------------------------------------------


def test_moodle_and_ical_rules() -> None:
    lms = LintContext(allowed_hosts=["lms.fakeuni.test"])
    assert lint_spec(
        {
            "adapter": "moodle_ws",
            "origin": "https://evil.test",
            "functions": ["core_enrol_get_users_courses"],
        },
        lms,
    ).codes() == {"host.not_allowed"}
    assert (
        "schema"
        in lint_spec(
            {
                "adapter": "moodle_ws",
                "origin": "https://lms.fakeuni.test",
                "functions": ["core_user_update_users"],
            },
            lms,
        ).codes()
    )
    unsafe = {
        "adapter": "ical",
        "origin": "https://lms.fakeuni.test",
        "classify": {"deadlineIf": {"summaryPattern": "(a+)+$"}},
    }
    assert "pattern.unsafe" in lint_spec(unsafe, lms).codes()


def test_document_rules() -> None:
    def doc(**source: Any) -> dict[str, Any]:
        return {"adapter": "document", "source": {"kind": "url", **source}}

    host = LintContext(allowed_hosts=["files.fakeuni.test"])
    assert lint_spec(doc(url="https://files.fakeuni.test/a.pdf"), host).ok
    assert "host.not_allowed" in lint_spec(doc(url="https://evil.test/a.pdf"), host).codes()
    assert "document.url" in lint_spec(doc(url="http://files.fakeuni.test/a.pdf"), host).codes()
    assert "document.url" in lint_spec(doc(url="https://10.0.0.5/a.pdf"), LintContext()).codes()
    assert (
        "document.url_secret"
        in lint_spec(doc(url="https://files.fakeuni.test/a.pdf?token=abc"), host).codes()
    )
    assert (
        "personal.email"
        in lint_spec(doc(url="https://files.fakeuni.test/a.pdf?u=a@b.ru"), host).codes()
    )
    assert (
        "document.url"
        in lint_spec({"adapter": "document", "source": {"kind": "google_sheet"}}, host).codes()
    )
    table = {
        "kind": "scheduleEvents",
        "columns": {
            "title": {"column": "Предмет"},
            "startsAt": {"column": "Дата"},
            "passport": {"column": "Паспорт"},
        },
    }
    spec = doc(url="https://files.fakeuni.test/a.xlsx") | {"table": table}
    assert "field.unknown" in lint_spec(spec, host).codes()
    no_title = doc(url="https://files.fakeuni.test/a.xlsx") | {
        "table": {"kind": "grades", "columns": {"title": {"column": "A"}}}
    }
    assert "field.missing_required" in lint_spec(no_title, host).codes()
    named = doc(url="https://files.fakeuni.test/a.xlsx") | {
        "table": {
            "kind": "deadlines",
            "columns": {"title": {"column": "Иванова Анна"}, "date": {"column": "Дата"}},
        }
    }
    assert (
        "observed.value"
        in lint_spec(
            named, LintContext(allowed_hosts=["files.fakeuni.test"], observed=["Иванова Анна"])
        ).codes()
    )


# --- reports never carry the offending values ----------------------------------------------


def test_reports_carry_codes_and_places_but_never_values() -> None:
    secret_email = f"{MARKER}@fakeuni.test"
    spec = recipes.full_recipe()
    ep(spec)["request"]["query"]["owner"] = secret_email
    ep(spec)["request"]["query"]["note"] = f"https://evil.test/{MARKER}"
    spec["recipe"]["notes"] = f"тут {MARKER} 12345678 и Иванова Анна"
    ctx = LintContext(allowed_hosts=["fakeuni.test"], observed=["Иванова Анна", MARKER + "-obs1"])
    result = lint_spec(spec, ctx)
    assert {"personal.email", "url.absolute", "personal.digits", "observed.value"} <= result.codes()
    blob = json.dumps(result.reasons(), ensure_ascii=False)
    err = result.to_error()
    blob += json.dumps(err.extra, ensure_ascii=False) + err.detail
    for leaked in (MARKER, "Иванова", "12345678", "evil.test"):
        assert leaked not in blob
    assert err.code == ErrorCode.RECIPE_REJECTED
    assert err.status == 400


def test_raise_if_failed() -> None:
    lint_spec(recipes.recipe_spec(), CTX).raise_if_failed()
    bad = edit(recipes.recipe_spec(), lambda s: ep(s)["request"].update(path="/a/../b"))
    with pytest.raises(BotaiError) as exc:
        lint_spec(bad, CTX).raise_if_failed()
    assert exc.value.code == ErrorCode.RECIPE_REJECTED


def test_a_copied_token_is_rejected_even_under_an_innocent_name() -> None:
    for token in (moodle.TOKEN, "9f86d081884c7d659a2feaa0c55ad015"):
        spec = recipes.recipe_spec()
        ep(spec)["request"]["query"]["k"] = token
        assert "personal.token" in lint_spec(spec, CTX).codes()


def test_ordinary_paths_that_only_resemble_actions_pass() -> None:
    for path in (
        "/api/v2/submissions",
        "/api/editorial/news",
        "/api/exits-map",
        "/api/v2/cancellations",
    ):
        spec = edit(recipes.recipe_spec(), lambda s, path=path: ep(s)["request"].update(path=path))
        assert "url.destructive" not in lint_spec(spec, CTX).codes(), path
