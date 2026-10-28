"""`recipe` adapter: HTTP by a learned recipe, answers in JSON (JSON Pointer) or HTML (CSS).

No model is involved: the recipe is data written once by the Explorer. An endpoint that no longer
matches reports a structural `Problem`; the Runner turns it into a repair job.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from botai.contracts.specs import (
    Endpoint,
    HtmlResponse,
    JsonResponse,
    Pagination,
    Recipe,
    RecipeSpec,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.sources.adapter import (
    AdapterKind,
    AdapterResult,
    EndpointOutcome,
    Problem,
    Record,
    SessionExpired,
    SyncContext,
)
from botai.sources.extract import (
    MISSING,
    apply_transform,
    html_value,
    parse_html,
    resolve,
    select_items,
)
from botai.sources.fetch import SourceClient, origin_host
from botai.sources.readonly import is_read_only_post
from botai.sources.templates import MAX_PARAM_VALUE, TemplateError, render
from botai.sources.vocab import VOCAB

ABSOLUTE_MAX_PAGES = 20
MAX_ITEMS_PER_ENDPOINT = 5000
DEFAULT_LIMIT_PARAM_VALUE = 100


class RecipeAdapter:
    kind: ClassVar[AdapterKind] = "recipe"

    def __init__(self, spec: RecipeSpec) -> None:
        self.recipe: Recipe = spec.recipe
        if any(endpoint.request.render for endpoint in self.recipe.endpoints):
            raise BotaiError(
                ErrorCode.RECIPE_REJECTED,
                "Чтение отрисованного HTML ещё не включено в этом исполнителе",
            )

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        recipe = self.recipe
        host = origin_host(recipe.origin)
        if host not in {h.lower() for h in ctx.allow_hosts}:
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                "Адрес рецепта не входит в разрешённые для этого подключения",
                extra={"host": host},
            )
        records: dict[tuple[str, str], Record] = {}
        outcomes: list[EndpointOutcome] = []
        async with SourceClient(
            ctx, auth_type=recipe.auth.type, header_name=recipe.auth.header_name
        ) as client:
            params, bootstrap_problems = await self._bootstrap(client, ctx)
            for endpoint in recipe.endpoints:
                outcome, found = await self._endpoint(
                    client, ctx, endpoint, params, bootstrap_problems
                )
                outcomes.append(outcome)
                for rec in found:
                    records[(rec.kind, rec.external_id)] = rec
        return AdapterResult(
            records=list(records.values()),
            fetched_at=ctx.now,
            outcomes=outcomes,
            spec_revision=recipe.revision,
        )

    # --- bootstrap ---------------------------------------------------------------------------

    async def _bootstrap(
        self, client: SourceClient, ctx: SyncContext
    ) -> tuple[dict[str, str], set[str]]:
        params: dict[str, str] = {}
        failed: set[str] = set()
        for step in self.recipe.bootstrap:
            try:
                url = self._url(step.path, ctx, params)
                response = await client.request("GET", url)
                doc = response.json() if response.status_code == 200 else MISSING
            except (TemplateError, ValueError, RecursionError):
                failed.update(step.extract)
                continue
            for name, pointer in step.extract.items():
                value = resolve(doc, pointer) if doc is not MISSING else MISSING
                text = _param_text(value)
                if text is None:
                    failed.add(name)
                else:
                    params[name] = text
        return params, failed

    # --- endpoint ----------------------------------------------------------------------------

    def _url(self, path: str, ctx: SyncContext, params: Mapping[str, str]) -> str:
        filled = render(path, now=ctx.now, tz=ctx.tz, params=params, in_path=True)
        if not filled.startswith("/") or filled.startswith("//"):
            raise TemplateError("path")
        url = self.recipe.origin + filled
        if urlsplit(url).hostname != origin_host(self.recipe.origin):
            raise TemplateError("path")
        return url

    async def _endpoint(
        self,
        client: SourceClient,
        ctx: SyncContext,
        endpoint: Endpoint,
        params: Mapping[str, str],
        bootstrap_failed: set[str],
    ) -> tuple[EndpointOutcome, list[Record]]:
        outcome = EndpointOutcome(endpoint_id=endpoint.id, kind=endpoint.kind, ok=True)

        def fail(problem: Problem, url: str = "") -> tuple[EndpointOutcome, list[Record]]:
            outcome.ok = False
            outcome.problem = problem
            outcome.source_url = url or outcome.source_url
            return outcome, []

        req = endpoint.request
        try:
            url = self._url(req.path, ctx, params)
            query = {
                k: render(v, now=ctx.now, tz=ctx.tz, params=params)
                for k, v in (req.query or {}).items()
            }
            body = (
                {
                    k: render(v, now=ctx.now, tz=ctx.tz, params=params)
                    for k, v in (req.body or {}).items()
                }
                if req.body
                else None
            )
        except TemplateError as exc:
            return fail("bootstrap" if exc.name in bootstrap_failed else "format")
        if req.method == "POST" and not (req.read_only_post and is_read_only_post(req.path, query)):
            return fail("status", url)

        pagination = endpoint.pagination
        pages = self._page_count(pagination)
        collected: list[Record] = []
        seen = 0
        offset = 0
        cursor: str | None = None
        previous_ids: tuple[str, ...] | None = None
        outcome.source_url = url
        for page_index in range(pages):
            page_query = {**query, **self._page_params(pagination, page_index, offset, cursor)}
            response = await client.request(
                req.method, url, params=page_query, json=body if req.method == "POST" else None
            )
            if response.status_code in (404, 410):
                return fail("not_found", str(response.url))
            if response.status_code >= 400:
                return fail("status", str(response.url))
            outcome.source_url = str(response.url)
            parsed = self._parse(endpoint, response, ctx)
            if isinstance(parsed, str):
                return fail(parsed, str(response.url))  # type: ignore[arg-type]
            raw_count, page_records, doc = parsed
            seen += raw_count
            collected.extend(page_records)
            ids = tuple(r.external_id for r in page_records[:5])
            if pagination is None or pagination.type == "none" or raw_count == 0:
                break
            if previous_ids is not None and ids and ids == previous_ids:
                break  # the server ignores the page parameter: do not loop
            previous_ids = ids
            limit = pagination.limit
            if limit and raw_count < limit:
                break
            if pagination.type == "offset":
                offset += raw_count
            if pagination.type == "cursor":
                cursor = _next_cursor(doc, pagination)
                if cursor is None:
                    break
            if pagination.total_path and _reached_total(doc, pagination, len(collected)):
                break
            if len(collected) >= MAX_ITEMS_PER_ENDPOINT:
                break
        collected = collected[:MAX_ITEMS_PER_ENDPOINT]
        outcome.items_seen = seen
        outcome.items_valid = len(collected)
        if seen == 0:
            outcome.problem = "empty"
        elif not collected:
            return fail("fields", outcome.source_url)
        return outcome, collected

    @staticmethod
    def _page_count(pagination: Pagination | None) -> int:
        if pagination is None or pagination.type == "none":
            return 1
        return max(1, min(pagination.max_pages, ABSOLUTE_MAX_PAGES))

    @staticmethod
    def _page_params(
        pagination: Pagination | None, page_index: int, offset: int, cursor: str | None
    ) -> dict[str, str]:
        if pagination is None or pagination.type == "none":
            return {}
        out: dict[str, str] = {}
        if pagination.limit_param and pagination.limit:
            out[pagination.limit_param] = str(pagination.limit)
        if pagination.type == "offset":
            out[pagination.param or "offset"] = str(offset)
        elif pagination.type == "page":
            out[pagination.param or "page"] = str(page_index + 1)
        elif pagination.type == "cursor" and cursor is not None:
            out[pagination.param or "cursor"] = cursor
        return out

    # --- parsing -----------------------------------------------------------------------------

    def _parse(
        self, endpoint: Endpoint, response: httpx.Response, ctx: SyncContext
    ) -> tuple[int, list[Record], Any] | str:
        spec = endpoint.response
        source_url = str(response.url)
        if isinstance(spec, JsonResponse):
            try:
                doc: Any = response.json()
            except (ValueError, RecursionError):
                _raise_if_login_page(response)
                return "format"
            items = resolve(doc, spec.items_path)
            if items is MISSING or not isinstance(items, list):
                return "format"
            records = [
                rec
                for raw in items
                if (rec := _json_record(endpoint, spec, raw, source_url, ctx)) is not None
            ]
            return len(items), records, doc
        assert isinstance(spec, HtmlResponse)
        content_type = response.headers.get("content-type", "")
        if "json" in content_type and "html" not in content_type:
            return "format"
        tree = parse_html(response.text)
        nodes = select_items(tree, spec.items_selector)
        if not nodes:
            _raise_if_login_page(response, tree_has_password=_has_password(tree))
            return 0, [], None
        records = []
        for node in nodes:
            fields: dict[str, Any] = {}
            ok = True
            for name, hf in spec.fields.items():
                if name not in VOCAB[endpoint.kind]:
                    continue
                value = html_value(
                    node,
                    selector=hf.selector,
                    attr=hf.attr,
                    pattern=hf.pattern,
                    transform=hf.transform,
                    tz=ctx.tz,
                )
                if value is None and hf.required:
                    ok = False
                    break
                fields[name] = value
            if ok and (rec := _make_record(endpoint, spec.identity, fields, source_url)):
                records.append(rec)
        return len(nodes), records, None


def _has_password(tree: Any) -> bool:
    try:
        return bool(tree.css_first("input[type=password]"))
    except Exception:
        return False


def _raise_if_login_page(response: httpx.Response, tree_has_password: bool | None = None) -> None:
    """A login form where data was expected means the session is gone."""
    if tree_has_password is None:
        text = response.text[:200_000].lower()
        tree_has_password = 'type="password"' in text or "type='password'" in text
    if tree_has_password:
        raise SessionExpired("the page asks to log in")


def _param_text(value: Any) -> str | None:
    if value is MISSING or value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_PARAM_VALUE:
        return None
    return text


def _next_cursor(doc: Any, pagination: Pagination) -> str | None:
    if not pagination.cursor_path:
        return None
    value = resolve(doc, pagination.cursor_path)
    if value is MISSING or value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _reached_total(doc: Any, pagination: Pagination, collected: int) -> bool:
    if not pagination.total_path or doc is None:
        return False
    total = resolve(doc, pagination.total_path)
    return isinstance(total, int) and not isinstance(total, bool) and collected >= total


def _json_record(
    endpoint: Endpoint, spec: JsonResponse, raw: Any, source_url: str, ctx: SyncContext
) -> Record | None:
    fields: dict[str, Any] = {}
    allowed = VOCAB[endpoint.kind]
    for name, jf in spec.fields.items():
        if name not in allowed:
            continue
        value = resolve(raw, jf.path)
        if (value is MISSING or value is None) and jf.fallback_path:
            value = resolve(raw, jf.fallback_path)
        converted = apply_transform(jf.transform, value, ctx.tz)
        if converted is None and jf.required:
            return None
        fields[name] = converted
    return _make_record(endpoint, spec.identity, fields, source_url)


def _make_record(
    endpoint: Endpoint, identity: list[str] | None, fields: dict[str, Any], source_url: str
) -> Record | None:
    external = fields.pop("externalId", None)
    if external is not None and not isinstance(external, (dict, list)):
        external_id = str(external).strip()[:200]
    else:
        external_id = ""
    if not external_id:
        if not identity:
            return None
        parts = [fields.get(name) for name in identity]
        if any(p is None for p in parts):
            return None
        digest = hashlib.sha256(
            json.dumps([endpoint.kind, parts], ensure_ascii=False, default=str).encode()
        ).hexdigest()
        external_id = digest
    return Record(
        kind=endpoint.kind,
        external_id=external_id,
        fields={k: v for k, v in fields.items() if v is not None},
        source_url=source_url,
    )
