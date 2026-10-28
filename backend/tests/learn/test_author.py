"""A malformed recipe author response cannot prove that a source has no data."""

from __future__ import annotations

import json

import pytest

from botai.core.llm import LlmResponse, ScriptedLlm
from botai.learn.author import AuthorError, author_kind
from botai.learn.lint import LintContext
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        [],
        {"found": "false", "endpoints": []},
        {"found": True, "endpoints": []},
        {"found": False, "endpoints": [{"kind": "scheduleEvents"}]},
        {"found": True, "endpoints": ["not an endpoint"]},
    ],
)
async def test_malformed_author_response_is_retried_then_rejected(invalid: object) -> None:
    llm = ScriptedLlm(queue=[LlmResponse(content=json.dumps(invalid)) for _ in range(2)])
    with pytest.raises(AuthorError):
        await author_kind(
            llm,
            "scheduleEvents",
            "<observation>synthetic fixture</observation>",
            lint_ctx=LintContext(allowed_hosts=["fakeuni.test"], expected_origin=ORIGIN),
            origin=ORIGIN,
            revision=1,
            now=NOW,
            digest="fixture-author",
        )
    assert len(llm.requests) == 2
