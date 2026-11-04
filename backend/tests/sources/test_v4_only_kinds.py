"""The v4-only kinds `profile` and `ranking` (D-039): vocabulary, coercion, canary keys.

They live next to the v3 kinds without touching them: the v3 recipe language (`contracts.specs.Kind`,
`recipe.schema.json`) and the tables the v3 learner iterates stay exactly as they were.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest

from botai.contracts.specs import Kind
from botai.sources import normalize, vocab
from botai.sources.v4 import kinds
from botai.sources.v4.agent_schema import source_schema

TZ = ZoneInfo("Europe/Moscow")
URL = "https://fakeuni.test/api/v2/profile"
ROOT = Path(__file__).resolve().parents[3] / "contracts"


def _coerce(kind: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    return normalize.coerce_fields(kind, fields, URL, TZ)  # type: ignore[arg-type]


def test_the_v3_vocabulary_does_not_know_the_new_kinds() -> None:
    assert set(get_args(Kind)) == {
        "courses",
        "scheduleEvents",
        "deadlines",
        "assignments",
        "grades",
        "people",
        "locations",
        "news",
    }
    for table in (vocab.VOCAB, vocab.REQUIRED, vocab.KEY_FIELDS, vocab.KIND_LABELS):
        assert "profile" not in table
        assert "ranking" not in table
    assert set(normalize.MODELS) == set(get_args(Kind))
    recipe = json.loads((ROOT / "recipe.schema.json").read_text("utf-8"))
    assert "profile" not in json.dumps(recipe)
    assert "ranking" not in json.dumps(recipe)


def test_the_v4_runner_vocabulary_has_every_kind_with_canary_keys() -> None:
    assert set(vocab.ALL_VOCAB) == set(vocab.ALL_REQUIRED) == set(vocab.ALL_KEY_FIELDS)
    assert set(vocab.ALL_KIND_LABELS) == set(vocab.ALL_VOCAB)
    assert set(normalize.ALL_MODELS) == set(vocab.ALL_VOCAB) == set(normalize.COERCERS)
    for kind, required in vocab.ALL_REQUIRED.items():
        assert required <= vocab.ALL_VOCAB[kind], kind
        assert set(vocab.ALL_KEY_FIELDS[kind]) <= vocab.ALL_VOCAB[kind] | {"title", "name"}, kind
    assert vocab.ALL_KEY_FIELDS["profile"] == ("field", "value")
    assert vocab.ALL_KEY_FIELDS["ranking"] == ("board", "position", "label")


def test_the_new_kinds_are_v4_data_kinds_with_a_domain_table() -> None:
    for name in ("profile", "ranking"):
        assert name in kinds.DATA_KINDS
        assert kinds.reading_kind(name) == name
        assert kinds.v4_kind(name) == name  # type: ignore[arg-type]
        assert kinds.normalize_kinds([name]) == [name]
    # the v3 recipe vocabulary still maps only the old names
    assert "profile" not in kinds.LEGACY_KIND_ALIASES
    assert kinds.reading_kind("documents") is None


def test_the_agent_protocol_teaches_the_new_kinds() -> None:
    fields = source_schema()["fields"]
    assert fields["profile"] == {
        "required": ["field", "value"],
        "allowed": ["externalId", "field", "group", "label", "value"],
    }
    assert "board" in fields["ranking"]["required"]
    assert "scoreNum" in fields["ranking"]["allowed"]
    assert "value_num" not in json.dumps(fields["grades"])  # camelCase vocabulary, not columns


def test_the_playbook_contract_lists_the_new_data_kinds() -> None:
    schema = json.loads((ROOT / "playbook.schema.json").read_text("utf-8"))
    enum = schema["properties"]["reads"]["items"]["properties"]["dataKind"]["enum"]
    assert set(enum) == set(kinds.DATA_KINDS)
    assert {"profile", "ranking"} <= set(enum)


PROFILE_CASES: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
    (
        {"field": "group", "label": "Группа", "value": "ПМ-31", "group": "Обучение"},
        {"field": "group", "label": "Группа", "value": "ПМ-31", "group_text": "Обучение"},
    ),
    # the value stays the text the source printed; numbers become text, nothing is parsed
    (
        {"field": "year", "label": "Курс", "value": 3},
        {"field": "year", "label": "Курс", "value": "3", "group_text": None},
    ),
    ({"field": "avg", "value": "8,50"}, {"label": "avg", "value": "8,50"}),
    ({"field": "budget", "label": "Бюджет", "value": True}, {"value": "Да"}),
    # nothing to show
    ({"field": "group", "label": "Группа", "value": "None"}, None),
    ({"field": "group", "label": "Группа", "value": ""}, None),
    ({"field": "group", "label": "Группа"}, None),
    ({"label": "Группа", "value": "ПМ-31"}, None),
    # a card field that holds a secret is dropped, never stored
    ({"field": "portal_password", "label": "Пароль", "value": "x"}, None),
    ({"field": "api_key", "label": "Ключ", "value": "x"}, None),
    ({"field": "x", "label": "Одноразовый код", "value": "123456"}, None),
    ({"field": "token", "label": "Token", "value": "abc"}, None),
]


@pytest.mark.parametrize(("fields", "expected"), PROFILE_CASES)
def test_profile_coercion(fields: dict[str, Any], expected: dict[str, Any] | None) -> None:
    row = _coerce("profile", fields)
    if expected is None:
        assert row is None
        return
    assert row is not None
    for column, value in expected.items():
        assert row[column] == value, column


RANKING_BASE = {"board": "Рейтинг потока", "position": 14, "label": "Иванова А."}
RANKING_CASES: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
    (
        {**RANKING_BASE, "score": "81,2", "isMe": True, "total": 120, "period": "Осень 2026"},
        {
            "board": "Рейтинг потока",
            "position": 14,
            "label": "Иванова А.",
            "score": "81,2",
            "score_num": 81.2,  # a plain number the source printed
            "is_me": True,
            "total": 120,
            "period": "Осень 2026",
        },
    ),
    ({**RANKING_BASE, "score": "81,2", "scoreNum": 81.25}, {"score": "81,2", "score_num": 81.25}),
    # words stay words: no number is invented
    ({**RANKING_BASE, "score": "высокий"}, {"score": "высокий", "score_num": None}),
    ({**RANKING_BASE, "score": "92 балла"}, {"score": "92 балла", "score_num": None}),
    ({**RANKING_BASE}, {"score": None, "score_num": None, "is_me": False, "total": None}),
    ({**RANKING_BASE, "position": "3", "isMe": "да"}, {"position": 3, "is_me": True}),
    ({**RANKING_BASE, "isMe": "нет"}, {"is_me": False}),
    ({**RANKING_BASE, "score": "None", "period": "None"}, {"score": None, "period": None}),
    # a place must be a whole number from 1
    ({**RANKING_BASE, "position": 0}, None),
    ({**RANKING_BASE, "position": -2}, None),
    ({**RANKING_BASE, "position": 2.5}, None),
    ({**RANKING_BASE, "position": "первое"}, None),
    ({**RANKING_BASE, "position": True}, None),
    ({"position": 1, "label": "Иванова А."}, None),
    ({"board": "Рейтинг", "position": 1}, None),
]


@pytest.mark.parametrize(("fields", "expected"), RANKING_CASES)
def test_ranking_coercion(fields: dict[str, Any], expected: dict[str, Any] | None) -> None:
    row = _coerce("ranking", fields)
    if expected is None:
        assert row is None
        return
    assert row is not None
    for column, value in expected.items():
        assert row[column] == value, column


def test_a_v3_coercion_of_the_old_kinds_is_unchanged() -> None:
    """`coerce` keeps its v3 signature; the old kinds coerce as before."""
    from botai.sources.adapter import Record

    record = Record(
        kind="people",
        external_id="p-1",
        fields={"name": "Иванов И.", "role": "преподаватель"},
        source_url=URL,
    )
    row = normalize.coerce("people", record, TZ)
    assert row is not None
    assert row["name"] == "Иванов И."
    assert row["role"] == "teacher"
