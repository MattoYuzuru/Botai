"""The model as the author of a spec: one kind of data per request, checked before it is used.

The model returns candidate endpoints (and, if needed, bootstrap steps) for one kind. This module
turns each candidate into a complete recipe, runs the lint on it, and gives the model one more
attempt with the findings (codes and places, never values) when the first answer is rejected.
Nothing the model writes is ever executed: it is data that must pass the schema and the lint.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botai.contracts.specs import Kind
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmProvider, LlmRequest, LlmResponse
from botai.learn import prompting
from botai.learn.lint import LintContext, lint_spec
from botai.learn.revisions import author_digest as author_digest

log = logging.getLogger(__name__)

DEFAULT_FRESHNESS = {"syncEveryMinutes": 180, "staleAfterMinutes": 720}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_spec(
    *,
    origin: str,
    revision: int,
    now: datetime,
    digest: str,
    endpoints: list[dict[str, Any]],
    bootstrap: list[dict[str, Any]],
    auth: dict[str, Any],
    require_kinds: Sequence[str],
    min_items: int = 1,
    notes: str | None = None,
) -> dict[str, Any]:
    recipe: dict[str, Any] = {
        "version": 1,
        "origin": origin,
        "revision": revision,
        "learnedAt": iso_z(now),
        "learnedByDigest": digest,
        "auth": auth,
        "endpoints": endpoints,
        "validation": {"requireKinds": list(require_kinds), "minItems": min_items},
        "freshness": dict(DEFAULT_FRESHNESS),
    }
    if bootstrap:
        recipe["bootstrap"] = bootstrap
    if notes:
        recipe["notes"] = notes[:2000]
    return {"adapter": "recipe", "recipe": recipe}


def parse_json(content: str) -> Any:
    """The model's answer as JSON, tolerating a code fence or a sentence around the object."""
    text = _FENCE.sub("", content.strip())
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


@dataclass(slots=True)
class Candidate:
    kind: Kind
    endpoint: dict[str, Any]
    bootstrap: list[dict[str, Any]]
    auth: dict[str, Any]


@dataclass(slots=True)
class Authored:
    kind: Kind
    candidates: list[Candidate] = field(default_factory=list)
    found: bool = False
    reasoning: str = ""
    doubts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # lint findings of dropped candidates
    tokens: int = 0
    calls: int = 0


def _tokens(response: LlmResponse) -> int:
    u = response.usage
    return u.input_cache_miss + u.input_cache_hit + u.output


def _auth(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and raw.get("type") in {"cookie", "bearer", "none"}:
        out: dict[str, Any] = {"type": raw["type"]}
        header = raw.get("headerName")
        if raw["type"] == "bearer" and isinstance(header, str) and header:
            out["headerName"] = header
        return out
    return {"type": "cookie"}


def candidates_from(kind: Kind, data: Any) -> tuple[list[Candidate], bool, str, list[str]]:
    """Shape the model's JSON into candidates; anything malformed is dropped, not repaired."""
    if not isinstance(data, dict):
        return [], False, "", []
    body: dict[str, Any] = data
    found = body.get("found") is True
    raw_reasoning = body.get("reasoning")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""
    doubts = [d for d in _as_list(body.get("doubts")) if isinstance(d, str)][:3]
    bootstrap = [b for b in _as_list(body.get("bootstrap")) if isinstance(b, dict)]
    auth = _auth(body.get("auth"))
    out: list[Candidate] = []
    for raw in _as_list(body.get("endpoints"))[: prompting.MAX_CANDIDATES]:
        if not isinstance(raw, dict) or raw.get("kind") not in (kind, None):
            continue
        endpoint: dict[str, Any] = copy.deepcopy(raw)
        endpoint["kind"] = kind
        out.append(Candidate(kind, endpoint, copy.deepcopy(bootstrap), auth))
    return out, found, reasoning[:600], doubts


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class AuthorError(Exception):
    """The model could not produce a usable answer (not JSON twice, provider failure)."""


async def _ask(llm: LlmProvider, request: LlmRequest) -> tuple[Any, int]:
    response = await llm.complete(request)
    try:
        return parse_json(response.content), _tokens(response)
    except ValueError:
        return None, _tokens(response)


def merge_bootstrap(
    existing: list[dict[str, Any]], new: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Union of bootstrap steps; None if two steps define the same parameter differently."""
    merged = list(existing)
    for step in new:
        clash = False
        for have in merged:
            same_id = have.get("id") == step.get("id")
            shares = set(have.get("extract", {})) & set(step.get("extract", {}))
            if have == step:
                clash = True  # already present, nothing to add
                break
            if same_id or any(have["extract"][n] != step["extract"][n] for n in shares):
                return None
        if not clash:
            merged.append(step)
    return merged


def candidate_spec(
    candidate: Candidate,
    *,
    origin: str,
    revision: int,
    now: datetime,
    digest: str,
    index: int,
) -> dict[str, Any]:
    endpoint = copy.deepcopy(candidate.endpoint)
    endpoint["id"] = f"{candidate.kind}{index + 1}"
    return build_spec(
        origin=origin,
        revision=revision,
        now=now,
        digest=digest,
        endpoints=[endpoint],
        bootstrap=copy.deepcopy(candidate.bootstrap),
        auth=candidate.auth,
        require_kinds=[candidate.kind],
    )


async def author_kind(
    llm: LlmProvider,
    kind: Kind,
    observation: str,
    *,
    lint_ctx: LintContext,
    origin: str,
    revision: int,
    now: datetime,
    digest: str,
    previous: dict[str, Any] | None = None,
    problem: str | None = None,
) -> Authored:
    """Ask for candidates of one kind; lint each; retry once on a rejection or a broken answer.
    With `previous` (the endpoint that stopped working) it is a repair."""
    out = Authored(kind)
    feedback: list[str] | None = None
    for attempt in range(2):
        request = prompting.author_request(
            kind, observation, feedback, previous=previous, problem=problem
        )
        data, spent = await _ask(llm, request)
        out.calls += 1
        out.tokens += spent
        if data is None:
            feedback = ["ответ не является JSON-объектом по схеме"]
            continue
        candidates, found, reasoning, doubts = candidates_from(kind, data)
        out.found, out.reasoning, out.doubts = found, reasoning, doubts
        if not found or not candidates:
            return out  # an honest "nothing here" is a result, not a failure
        good: list[Candidate] = []
        findings: list[str] = []
        for i, cand in enumerate(candidates):
            spec = candidate_spec(
                cand, origin=origin, revision=revision, now=now, digest=digest, index=i
            )
            result = lint_spec(spec, lint_ctx)
            if result.ok:
                good.append(cand)
            else:
                findings.extend(f"вариант {i + 1}: {reason}" for reason in result.reasons())
        out.candidates = good
        out.rejected = findings
        if good or attempt == 1:
            return out
        feedback = findings
    if not out.candidates and not out.rejected:
        raise AuthorError("the model did not return a usable answer")
    return out


def as_botai_error(exc: AuthorError) -> BotaiError:
    return BotaiError(ErrorCode.LLM_UNAVAILABLE, "Не удалось получить разбор сайта от модели")
