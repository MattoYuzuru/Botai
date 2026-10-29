"""`recipe` adapter: HTTP by a learned recipe, answers in JSON (JSON Pointer) or HTML (CSS).

No model is involved: the recipe is data written once by the Explorer. An endpoint that no longer
matches reports a structural `Problem`; the Runner turns it into a repair job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from botai.contracts.specs import (
    Endpoint,
    HtmlField,
    HtmlResponse,
    JsonField,
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
from botai.sources.expressions import (
    MAX_COLLECTION_NODES,
    MAX_INSTANCES,
    MAX_LEAVES,
    ExpressionError,
    collect,
    expand_weekly,
    field_value,
)
from botai.sources.extract import (
    MISSING,
    apply_transform,
    html_value,
    parse_datetime,
    parse_html,
    resolve,
    select_items,
)
from botai.sources.fetch import SourceClient, origin_host
from botai.sources.readonly import is_read_only_post
from botai.sources.rendered import MAX_RENDERED_SECONDS, RenderedPageError, RenderedPageReader
from botai.sources.templates import MAX_PARAM_VALUE, TemplateError, render
from botai.sources.vocab import VOCAB

ABSOLUTE_MAX_PAGES = 20
MAX_ITEMS_PER_ENDPOINT = 5000
DEFAULT_LIMIT_PARAM_VALUE = 100


class RecipeAdapter:
    kind: ClassVar[AdapterKind] = "recipe"

    def __init__(self, spec: RecipeSpec) -> None:
        self.recipe: Recipe = spec.recipe

    async def fetch(self, ctx: SyncContext) -> AdapterResult:
        if any(endpoint.request.render for endpoint in self.recipe.endpoints):
            try:
                # Reserve one second for the owned background handle's bounded cleanup.
                async with asyncio.timeout(MAX_RENDERED_SECONDS - 1.0):
                    return await self._fetch(ctx)
            except TimeoutError:
                raise BotaiError(
                    ErrorCode.SOURCE_UNAVAILABLE, "Источник не успел загрузиться"
                ) from None
        return await self._fetch(ctx)

    async def _fetch(self, ctx: SyncContext) -> AdapterResult:
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
        reader = RenderedPageReader(ctx)
        async with SourceClient(
            ctx, auth_type=recipe.auth.type, header_name=recipe.auth.header_name
        ) as client:
            params, bootstrap_problems = await self._bootstrap(client, ctx)
            for endpoint in recipe.endpoints:
                outcome, found = await self._endpoint(
                    client, ctx, endpoint, params, bootstrap_problems, reader
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
                response = await client.request(
                    "GET", url, auth_binding=self._binding(step.auth_binding)
                )
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

    def _binding(self, identifier: str | None) -> dict[str, Any] | None:
        if self.recipe.auth.type != "session":
            return None
        binding = (self.recipe.auth.bindings or {}).get(identifier or "")
        if binding is None:
            raise BotaiError(
                ErrorCode.RECIPE_REJECTED, "Не найдена проверенная привязка авторизации"
            )
        return binding.model_dump()

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
        reader: RenderedPageReader,
    ) -> tuple[EndpointOutcome, list[Record]]:
        outcome = EndpointOutcome(endpoint_id=endpoint.id, kind=endpoint.kind, ok=True)

        def fail(problem: Problem, url: str = "") -> tuple[EndpointOutcome, list[Record]]:
            outcome.ok = False
            outcome.problem = problem
            outcome.source_url = url or outcome.source_url
            return outcome, []

        req = endpoint.request
        if req.render and (
            req.method != "GET" or not isinstance(endpoint.response, HtmlResponse) or req.body
        ):
            return fail("format")  # Defense in depth in addition to schema/Endpoint validation.
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

        if req.browser_plan is not None:
            return await self._browser_endpoint(endpoint, ctx, reader, url, query, params)
        pagination = endpoint.pagination
        pages = self._page_count(pagination)
        collected: list[Record] = []
        seen = 0
        offset = 0
        cursor: str | None = None
        previous_pages: set[str] = set()
        unique_ids: set[str] = set()
        nodes_seen = 0
        leaves_seen = 0
        complete = False
        outcome.source_url = url
        for page_index in range(pages):
            page_query = {**query, **self._page_params(pagination, page_index, offset, cursor)}
            if req.render:
                assert isinstance(endpoint.response, HtmlResponse)
                request = httpx.Request("GET", url, params=page_query)
                try:
                    selector = endpoint.response.items_selector
                    if selector is None:
                        assert endpoint.response.collections is not None
                        selector = endpoint.response.collections[0].selector
                    snapshot = await reader.read(str(request.url), selector)
                except RenderedPageError as exc:
                    return fail(exc.problem, str(request.url))
                response = httpx.Response(
                    200,
                    text=snapshot.html,
                    headers={"content-type": "text/html; charset=utf-8"},
                    request=httpx.Request("GET", snapshot.url),
                )
            else:
                response = await client.request(
                    req.method,
                    url,
                    params=page_query,
                    json=body if req.method == "POST" else None,
                    auth_binding=self._binding(req.auth_binding),
                )
            outcome.status = response.status_code
            outcome.transport = "browser" if req.render else "http"
            outcome.binding_id = req.auth_binding
            if response.status_code in (404, 410):
                return fail("not_found", str(response.url))
            if response.status_code >= 400:
                return fail("status", str(response.url))
            outcome.source_url = str(response.url)
            try:
                parsed = self._parse(
                    endpoint,
                    response,
                    ctx,
                    remaining_nodes=MAX_COLLECTION_NODES - nodes_seen,
                    remaining_leaves=MAX_LEAVES - leaves_seen,
                    remaining_instances=MAX_INSTANCES - len(collected),
                )
            except ExpressionError as exc:
                return fail(exc.problem, str(response.url))
            if isinstance(parsed, str):
                return fail(parsed, str(response.url))
            raw_count, page_records, doc, page_nodes, page_leaves = parsed
            nodes_seen += page_nodes
            leaves_seen += page_leaves
            seen += raw_count
            outcome.items_seen = seen
            outcome.items_valid = len(collected)
            if len(collected) + len(page_records) > MAX_ITEMS_PER_ENDPOINT:
                if self.recipe.version == 1:
                    # Preserve the legacy bounded prefix, while refusing to declare the
                    # truncated endpoint complete (missing-row markers must remain untouched).
                    outcome.ok, outcome.problem = False, "limit"
                    collected.extend(page_records[: MAX_ITEMS_PER_ENDPOINT - len(collected)])
                    outcome.items_valid = len(collected)
                    return outcome, collected
                return fail("limit", str(response.url))
            page_digest = hashlib.sha256(response.content).hexdigest()
            ids = {r.external_id for r in page_records}
            if (
                page_index
                and raw_count
                and (page_digest in previous_pages or (ids and not ids.difference(unique_ids)))
            ):
                # Keep usable records, but never mark an unfinished walk as a complete snapshot.
                outcome.ok, outcome.problem = False, "no_progress"
                return outcome, collected
            previous_pages.add(page_digest)
            unique_ids.update(ids)
            collected.extend(page_records)
            if pagination is None or pagination.type == "none" or raw_count == 0:
                complete = True
                break
            limit = pagination.limit
            if limit and raw_count < limit:
                complete = True
                break
            if pagination.type == "offset":
                offset += raw_count
            if pagination.type == "cursor":
                cursor = _next_cursor(doc, pagination)
                if cursor is None:
                    complete = True
                    break
            if pagination.total_path and _reached_total(doc, pagination, seen):
                complete = True
                break
        outcome.items_seen = seen
        outcome.items_valid = len(collected)
        if not complete:
            outcome.ok, outcome.problem = False, "limit"
        elif seen == 0 or (self.recipe.version == 2 and not leaves_seen):
            outcome.problem = "empty"
        elif not collected and not (
            self.recipe.version == 2 and endpoint.response.expand is not None
        ):
            return fail("fields", outcome.source_url)
        return outcome, collected

    async def _browser_endpoint(
        self,
        endpoint: Endpoint,
        ctx: SyncContext,
        reader: RenderedPageReader,
        url: str,
        query: Mapping[str, str],
        params: Mapping[str, str],
    ) -> tuple[EndpointOutcome, list[Record]]:
        response_spec = endpoint.response
        if not isinstance(response_spec, HtmlResponse) or endpoint.request.browser_plan is None:
            return EndpointOutcome(endpoint.id, endpoint.kind, False, problem="format"), []
        selector = response_spec.items_selector
        if selector is None:
            selector = (
                " ".join(
                    collection.selector
                    for collection in response_spec.collections or []
                    if collection.selector != "."
                )
                or "body"
            )
        outcome = EndpointOutcome(
            endpoint.id, endpoint.kind, True, source_url=url, transport="browser"
        )
        request = httpx.Request("GET", url, params=query)
        try:
            pages = await reader.read_many(
                str(request.url), selector, plan=endpoint.request.browser_plan, values=params
            )
        except RenderedPageError as exc:
            outcome.ok, outcome.problem = False, exc.problem
            return outcome, []
        collected: list[Record] = []
        nodes_seen = leaves_seen = 0
        identities: set[str] = set()
        for snapshot in pages:
            response = httpx.Response(
                200,
                text=snapshot.html,
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("GET", snapshot.url),
            )
            try:
                parsed = self._parse(
                    endpoint,
                    response,
                    ctx,
                    remaining_nodes=MAX_COLLECTION_NODES - nodes_seen,
                    remaining_leaves=MAX_LEAVES - leaves_seen,
                    remaining_instances=MAX_INSTANCES - len(collected),
                )
            except ExpressionError as exc:
                outcome.ok, outcome.problem = False, exc.problem
                return outcome, []
            if isinstance(parsed, str):
                outcome.ok, outcome.problem = False, parsed
                return outcome, []
            root_count, records, _document, nodes, leaves = parsed
            if records and not {row.external_id for row in records}.difference(identities):
                outcome.ok, outcome.problem = False, "no_progress"
                return outcome, []
            identities.update(row.external_id for row in records)
            collected.extend(records)
            nodes_seen, leaves_seen = nodes_seen + nodes, leaves_seen + leaves
            outcome.items_seen += root_count
            outcome.source_url, outcome.status = snapshot.url, 200
        outcome.items_valid = len(collected)
        if not leaves_seen:
            outcome.problem = "empty"
        elif not collected and response_spec.expand is None:
            outcome.ok, outcome.problem = False, "fields"
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
        self,
        endpoint: Endpoint,
        response: httpx.Response,
        ctx: SyncContext,
        *,
        remaining_nodes: int = MAX_COLLECTION_NODES,
        remaining_leaves: int = MAX_LEAVES,
        remaining_instances: int = MAX_INSTANCES,
    ) -> tuple[int, list[Record], Any, int, int] | Problem:
        spec = endpoint.response
        source_url = str(response.url)
        if self.recipe.version == 2:
            if isinstance(spec, JsonResponse):
                try:
                    document = response.json()
                except (ValueError, RecursionError):
                    _raise_if_login_page(response)
                    return "format"
            else:
                if "json" in response.headers.get("content-type", ""):
                    return "format"
                document = parse_html(response.text)
            page = collect(
                document,
                spec,
                remaining_nodes=remaining_nodes,
                remaining_leaves=remaining_leaves,
            )
            if not page.root_count and isinstance(spec, HtmlResponse):
                _raise_if_login_page(response, tree_has_password=_has_password(document))
            records: list[Record] = []
            for bindings in page.items:
                fields = {
                    name: field_value(expression, bindings, ctx.tz)
                    for name, expression in spec.fields.items()
                    if name in VOCAB[endpoint.kind]
                }
                # In v2 an explicitly declared identity is authoritative, including parent fields.
                if endpoint.kind == "scheduleEvents" and spec.expand is None:
                    start = parse_datetime(fields.get("startsAt"), ctx.tz)
                    end = parse_datetime(fields.get("endsAt"), ctx.tz)
                    if start is not None and end is not None and end <= start:
                        raise ExpressionError("fields")
                record = _make_record(
                    endpoint, spec.identity, dict(fields), source_url, prefer_identity=True
                )
                if record is None:
                    raise ExpressionError("fields")
                if spec.expand is None:
                    records.append(record)
                else:
                    for occurrence in expand_weekly(
                        spec.expand,
                        bindings,
                        now=ctx.now,
                        remaining_instances=remaining_instances - len(records),
                    ):
                        instance_id = hashlib.sha256(
                            json.dumps(
                                [
                                    "weekly-v1",
                                    endpoint.kind,
                                    record.external_id,
                                    occurrence.local_date.isoformat(),
                                    occurrence.local_time.isoformat(),
                                    spec.expand.time_zone,
                                ],
                                ensure_ascii=False,
                            ).encode()
                        ).hexdigest()
                        records.append(
                            Record(
                                kind=endpoint.kind,
                                external_id=instance_id,
                                fields={
                                    **record.fields,
                                    "startsAt": occurrence.starts_at,
                                    "endsAt": occurrence.ends_at,
                                },
                                source_url=source_url,
                            )
                        )
                if len(records) > remaining_instances:
                    raise ExpressionError("limit")
            if len({r.external_id for r in records}) != len(records):
                raise ExpressionError("fields")
            return page.root_count, records, document, page.nodes_seen, len(page.items)
        if isinstance(spec, JsonResponse):
            try:
                doc: Any = response.json()
            except (ValueError, RecursionError):
                _raise_if_login_page(response)
                return "format"
            assert spec.items_path is not None
            items = resolve(doc, spec.items_path)
            if items is MISSING or not isinstance(items, list):
                return "format"
            records = [
                rec
                for raw in items
                if (rec := _json_record(endpoint, spec, raw, source_url, ctx)) is not None
            ]
            return len(items), records, doc, len(items), len(items)
        assert isinstance(spec, HtmlResponse)
        content_type = response.headers.get("content-type", "")
        if "json" in content_type and "html" not in content_type:
            return "format"
        tree = parse_html(response.text)
        assert spec.items_selector is not None
        nodes = select_items(tree, spec.items_selector)
        if not nodes:
            _raise_if_login_page(response, tree_has_password=_has_password(tree))
            return 0, [], None, 0, 0
        records = []
        for node in nodes:
            fields: dict[str, Any] = {}
            ok = True
            for name, hf in spec.fields.items():
                assert isinstance(hf, HtmlField)  # v1 is checked by Recipe's version gate.
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
        return len(nodes), records, None, len(nodes), len(nodes)


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
        assert isinstance(jf, JsonField)  # v1 is checked by Recipe's version gate.
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
    endpoint: Endpoint,
    identity: list[str] | None,
    fields: dict[str, Any],
    source_url: str,
    *,
    prefer_identity: bool = False,
) -> Record | None:
    external = fields.pop("externalId", None)
    if (
        external is not None
        and not isinstance(external, (dict, list))
        and not (prefer_identity and identity)
    ):
        external_id = str(external).strip()[:200]
    else:
        external_id = ""
    if not external_id:
        if not identity:
            return None
        parts = [external if name == "externalId" else fields.get(name) for name in identity]
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
