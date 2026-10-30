"""Turn author references into executor-owned request bindings and browser plans."""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from botai.contracts.specs import MAX_REPEATED_VALUES
from botai.core.browser import ObservedRequest, auth_origin
from botai.learn.addresses import (
    looks_like_auth_page,
    request_literals,
    ui_segment_ok,
    ui_value_ok,
)
from botai.learn.auth_bindings import BindingEvidenceError, compile_auth_binding
from botai.learn.lint import SECRET_NAME
from botai.sources import templates

# What each static reason means for the author, in words. Structure only, never a value.
HINTS: dict[str, str] = {
    "evidence_unknown": "requestEvidenceId не из наблюдений: укажи ссылку r… из списка запросов",
    "evidence_auth_unproven": "у наблюдённого запроса не подтверждены заголовки: выбери другой запрос",
    "evidence_response_unverified": "наблюдённый запрос не завершился успехом: выбери успешный",
    "evidence_origin_mismatch": "наблюдённый запрос относится к другому сайту",
    "evidence_method_mismatch": "метод в описании не совпал с наблюдённым запросом",
    "evidence_path_mismatch": (
        "путь не совпал с путём наблюдённого запроса; `{имя}` из bootstrap можно подставить "
        "только вместо идентификатора, который в наблюдении показан меткой <v…>"
    ),
    "evidence_path_literal_replaced": (
        "слово вроде `me` в пути — литерал, по которому сам сайт узнаёт текущего студента; "
        "оставь путь ровно как в наблюдении, без `{имя}` и без bootstrap"
    ),
    "evidence_query_mismatch": (
        "query должен воспроизводить наблюдённый запрос: те же ключи и значения; ключ, который "
        'повторяется в адресе, записывай списком всех значений ("tag": ["a", "b"])'
    ),
    "evidence_auth_scheme_unsupported": "схема авторизации наблюдённого запроса не поддерживается",
    "evidence_auth_inconsistent": "заголовки наблюдённого запроса противоречивы: выбери другой",
    "window_param_missing": "в query нет ключа, который двигает окно дат",
    "window_template_unsupported": "окно дат двигает только шаблон даты ({weekStart}, {today}…)",
    "window_template_mismatch": "ни одно окно обхода не воспроизводит наблюдённый запрос",
    "factory_bootstrap_read_only": "шаг bootstrap должен быть GET",
    "factory_evidence_required": "у эндпоинта и шага bootstrap нужен requestEvidenceId",
    "factory_execution_fields_forbidden": "authBinding, browserPlan и uiUrl задаёт сервер",
    "browser_page_unobserved": "render=true нужен requestEvidenceId документа той же страницы",
}


class FactoryCompileError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def feedback(self) -> str:
        """The finding as the author reads it: code, what it means, where (no values)."""
        parts = [self.code]
        if hint := HINTS.get(self.code):
            parts.append(hint)
        if self.detail:
            parts.append(self.detail)
        return " — ".join(parts)


@dataclass(slots=True)
class CompiledCandidate:
    endpoint: dict[str, Any]
    bootstrap: list[dict[str, Any]]
    auth: dict[str, Any]


def needs_recipe_v2(
    endpoints: Sequence[Mapping[str, Any]],
    bootstrap: Sequence[Mapping[str, Any]],
    auth: Mapping[str, Any],
) -> bool:
    if auth.get("type") == "session" or any("authBinding" in step for step in bootstrap):
        return True
    for endpoint in endpoints:
        request, response = endpoint.get("request", {}), endpoint.get("response", {})
        pagination = endpoint.get("pagination")
        if "uiUrl" in endpoint or (
            isinstance(pagination, dict) and pagination.get("type") == "dateWindow"
        ):
            return True
        if isinstance(request, dict) and any(
            key in request for key in ("authBinding", "browserPlan")
        ):
            return True
        query = request.get("query") if isinstance(request, dict) else None
        if isinstance(query, dict) and any(isinstance(value, list) for value in query.values()):
            return True
        if isinstance(response, dict):
            if any(key in response for key in ("collections", "expand", "links", "attributes")):
                return True
            fields = response.get("fields", {})
            if isinstance(fields, dict) and any(
                isinstance(field, dict) and any(key in field for key in ("from", "compose"))
                for field in fields.values()
            ):
                return True
    return False


def _binding_name(binding: Mapping[str, Any]) -> str:
    return {
        (False, False): "bNone",
        (True, False): "bCookies",
        (False, True): "bBearer",
        (True, True): "bCookiesBearer",
    }[binding["cookies"], binding["bearer"] == "observedAuthorization"]


def _rendered(request: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    if request.get("method") != "GET" or "body" in request or response.get("format") != "html":
        raise FactoryCompileError("browser_render_read_only")


def _certified_template(
    template: str, raw: str, *, now: datetime, tz: ZoneInfo, in_path: bool = False
) -> str:
    names = templates.placeholders(template)
    if not names or any(not templates.is_builtin(name) for name in names):
        raise FactoryCompileError("browser_entry_template_unsupported")
    try:
        resolved = templates.render(template, now=now, tz=tz, params={}, in_path=in_path)
    except templates.TemplateError:
        raise FactoryCompileError("browser_entry_template_unsupported") from None
    if resolved != raw:
        raise FactoryCompileError("browser_entry_template_mismatch")
    return template


def _entry_request(
    url: str,
    origin: str,
    request: Mapping[str, Any],
    *,
    now: datetime | None,
    time_zone: ZoneInfo | None,
) -> tuple[str, dict[str, str | list[str]]]:
    if auth_origin(url) is None or auth_origin(url) != auth_origin(origin):
        raise FactoryCompileError("browser_entry_origin_mismatch")
    parts = urlsplit(url)
    if parts.fragment:
        raise FactoryCompileError("browser_entry_unrepresentable")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=12)
    except ValueError:
        raise FactoryCompileError("browser_entry_unrepresentable") from None
    by_key: dict[str, list[str]] = {}
    for key, value in pairs:
        by_key.setdefault(key, []).append(value)
    if any(len(values) > MAX_REPEATED_VALUES for values in by_key.values()):
        raise FactoryCompileError("browser_entry_unrepresentable")
    # A key that the page's own address repeats comes back as the list of its values.
    path = parts.path or "/"
    query: dict[str, str | list[str]] = {
        key: values[0] if len(values) == 1 else values for key, values in by_key.items()
    }
    if now is not None and time_zone is not None:
        authored_path = request.get("path")
        if isinstance(authored_path, str) and "{" in authored_path:
            path = _certified_template(authored_path, path, now=now, tz=time_zone, in_path=True)
        authored_query = request.get("query")
        if isinstance(authored_query, dict) and any(
            isinstance(value, str) and "{" in value for value in authored_query.values()
        ):
            if set(authored_query) != set(query):
                raise FactoryCompileError("browser_entry_template_mismatch")
            for key, value in authored_query.items():
                if isinstance(value, str) and "{" in value:
                    observed = query[key]
                    if not isinstance(observed, str):  # a template stands for one value
                        raise FactoryCompileError("browser_entry_template_mismatch")
                    query[key] = _certified_template(value, observed, now=now, tz=time_zone)
    return path, query


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _window_range(pagination: Mapping[str, Any]) -> range:
    first = pagination.get("startOffset", 0)
    return range(first, first + int(pagination["windows"]))


def _certify_window(
    request: Mapping[str, Any],
    pagination: Mapping[str, Any],
    observed: ObservedRequest,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> None:
    """The walk must reproduce what the browser really asked: one of its windows (normally the
    current one) renders the observed value of the moved parameter."""
    query = request.get("query")
    template = query.get(pagination["param"]) if isinstance(query, dict) else None
    if not isinstance(template, str):
        raise FactoryCompileError("window_param_missing")
    if not templates.is_date_template(template):
        raise FactoryCompileError("window_template_unsupported")
    seen = dict(parse_qsl(urlsplit(observed.url).query, keep_blank_values=True))
    value = seen.get(pagination["param"])
    step = int(pagination["stepDays"])
    for window in _window_range(pagination):
        moved = templates.shifted_now(now, tz, window * step)
        if templates.render(template, now=moved, tz=tz, params={}) == value:
            return
    raise FactoryCompileError("window_template_mismatch")


def _interface_page(
    observed: ObservedRequest, documents: Sequence[ObservedRequest], origin: str
) -> str | None:
    """The page whose script made the request. A sign-in step is not where the data is shown:
    then the last ordinary document of the same origin stands in, else there is no address."""
    candidates = [observed.initiator_url, *(d.url for d in reversed(documents))]
    for page in candidates:
        if page is None or auth_origin(page) is None or auth_origin(page) != auth_origin(origin):
            continue
        if not looks_like_auth_page(page):
            return page
    return None


def ui_url_for(
    observed: ObservedRequest,
    request: Mapping[str, Any],
    origin: str,
    *,
    now: datetime,
    tz: ZoneInfo,
    documents: Sequence[ObservedRequest] = (),
) -> dict[str, Any] | None:
    """Where the student sees this data: the page that issued the observed request, with the
    request's own date templates. Anything that cannot be generalised is left out, so a link
    that would point at one student's login, group or past week is never produced.

    A literal survives only as a structural word (`schedule`, `week`...) or when the recipe's own
    API request states the same word. A path segment that is neither makes the address unusable."""
    page = _interface_page(observed, documents, origin)
    if page is None:
        return None
    parts = urlsplit(page)
    if parts.fragment:
        return None
    path = parts.path or "/"
    request_words, request_values = request_literals(request)
    segments = [segment for segment in unquote(path).split("/") if segment]
    if any(not ui_segment_ok(segment, request_words) for segment in segments):
        return None
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=12)
    except ValueError:
        return None
    declared = request.get("query")
    api_query: dict[str, Any] = declared if isinstance(declared, dict) else {}
    query: dict[str, str] = {}
    repeated = {key for key, count in Counter(key for key, _ in pairs).items() if count > 1}
    for key, value in pairs:
        if (
            key in repeated  # a page address does not carry a list of values
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key)
            or SECRET_NAME.search(key)
        ):
            continue
        template = api_query.get(key)
        candidates = [template] if isinstance(template, str) and "{" in template else []
        if _ISO_DATE.match(value):
            candidates += ["{today}", "{weekStart}"]
            candidates += [f"{{plusDays:{n}}}" for n in range(1, 32)]
            candidates += [f"{{minusDays:{n}}}" for n in range(1, 32)]
        match = next(
            (
                c
                for c in candidates
                if templates.is_date_template(c)
                and templates.render(c, now=now, tz=tz, params={}) == value
            ),
            None,
        )
        if match is not None:
            query[key] = match
        elif ui_value_ok(key, value, request_values):
            query[key] = value
    if not segments and not query:
        return None  # the site root says nothing about where this data is shown
    ui: dict[str, Any] = {"path": path}
    if query:
        ui["query"] = query
    return ui


def _portable_browser_plan(plan: BaseModel) -> dict[str, Any]:
    """Share structural replay facts, without unnecessary source or student display labels."""
    portable = plan.model_dump(mode="json", by_alias=True, exclude_none=True)

    def frames(node: dict[str, Any]) -> None:
        for frame in node.get("framePath", []):
            frame.pop("title", None)
            frame.pop("name", None)

    def step(node: dict[str, Any]) -> None:
        for key in ("target", "region"):
            if key in node:
                node[key].pop("label", None)
        frames(node)

    frames(portable)
    for item in portable.get("steps", []):
        step(item)
    if portable.get("pagination") is not None:
        step(portable["pagination"]["next"])
    # emptyTarget.label is required evidence of a current explicit empty result.
    return portable


def compile_candidate(
    endpoint: Mapping[str, Any],
    bootstrap: Sequence[Mapping[str, Any]],
    *,
    origin: str,
    request_facts: Mapping[str, ObservedRequest],
    browser_plans: Mapping[str, object],
    browser_plan_urls: Mapping[str, str],
    now: datetime | None = None,
    time_zone: ZoneInfo | None = None,
) -> CompiledCandidate:
    """Compile references before a proposal is linted or checkpointed; never trust authored auth."""
    compiled = copy.deepcopy(dict(endpoint))
    request, response = compiled.get("request"), compiled.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise FactoryCompileError("factory_request_invalid")
    if "authBinding" in request or "browserPlan" in request or "uiUrl" in compiled:
        raise FactoryCompileError("factory_execution_fields_forbidden")
    evidence_id = compiled.pop("requestEvidenceId", None)
    plan_ref = compiled.pop("browserPlanRef", None)
    if evidence_id is not None and plan_ref is not None:
        raise FactoryCompileError("factory_reference_conflict")
    bindings: dict[str, Any] = {}

    def bind(ref: object, spec: dict[str, Any]) -> None:
        if not isinstance(ref, str):
            raise FactoryCompileError("factory_evidence_required")
        method, path = spec.get("method"), spec.get("path")
        if not isinstance(method, str) or not isinstance(path, str):
            raise FactoryCompileError("factory_request_invalid")
        try:
            binding = compile_auth_binding(
                request_facts,
                ref,
                origin=origin,
                method=method,
                path=path,
                query=spec.get("query"),
            )
        except BindingEvidenceError as exc:
            raise FactoryCompileError(exc.code, exc.detail) from None
        name = _binding_name(binding)
        bindings[name] = binding
        spec["authBinding"] = name

    if plan_ref is not None:
        if not isinstance(plan_ref, str) or plan_ref not in browser_plans:
            raise FactoryCompileError("browser_plan_unknown")
        _rendered(request, response)
        entry = browser_plan_urls.get(plan_ref)
        if not isinstance(entry, str):
            raise FactoryCompileError("browser_entry_missing")
        path, query = _entry_request(entry, origin, request, now=now, time_zone=time_zone)
        plan = browser_plans[plan_ref]
        if not isinstance(plan, BaseModel):
            raise FactoryCompileError("browser_plan_invalid")
        request["path"] = path
        request.pop("query", None)
        if query:
            request["query"] = query
        request["browserPlan"] = _portable_browser_plan(plan)
        request["render"] = True
    elif request.get("render") is True:
        _rendered(request, response)
        if not isinstance(evidence_id, str) or evidence_id not in request_facts:
            raise FactoryCompileError("browser_page_unobserved")
        if request_facts[evidence_id].resource_type != "document":
            raise FactoryCompileError("browser_page_unobserved")
        bind(evidence_id, request)
        request.pop("authBinding")  # browser replay owns authentication, not the HTTP client
        bindings.clear()
    else:
        bind(evidence_id, request)
        observed = request_facts[cast(str, evidence_id)]
        pagination = compiled.get("pagination")
        if isinstance(pagination, dict) and pagination.get("type") == "dateWindow":
            if now is None or time_zone is None:
                raise FactoryCompileError("window_template_unsupported")
            _certify_window(request, pagination, observed, now=now, tz=time_zone)
        if now is not None and time_zone is not None:
            documents = [
                fact
                for fact in request_facts.values()
                if fact.resource_type == "document"
                and fact.method == "GET"
                and fact.status is not None
                and 200 <= fact.status < 300
            ]
            ui = ui_url_for(observed, request, origin, now=now, tz=time_zone, documents=documents)
            if ui is not None:
                compiled["uiUrl"] = ui

    preparation: list[dict[str, Any]] = []
    for raw in bootstrap:
        step = copy.deepcopy(dict(raw))
        if "authBinding" in step or "browserPlan" in step:
            raise FactoryCompileError("factory_execution_fields_forbidden")
        ref = step.pop("requestEvidenceId", None)
        if step.get("method") != "GET":
            raise FactoryCompileError("factory_bootstrap_read_only")
        bind(ref, step)
        preparation.append(step)
    auth: dict[str, Any] = (
        {"type": "session", "version": 1, "bindings": bindings} if bindings else {"type": "cookie"}
    )
    return CompiledCandidate(compiled, preparation, auth)
