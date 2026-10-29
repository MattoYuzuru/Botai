"""Lint of adapter specs written by the model (security.md T6, T4; checks C4, C5).

A spec is shared by every student of a university, so it must hold no personal value and no way
out of the connection's origin. The model's output is validated here before it is stored:

1. JSON Schema (`botai.contracts.schemas.errors`);
2. no personal values: e-mail, phone, JWT, UUID, SNILS, runs of 6+ digits, token-like strings,
   and any literal equal to a value observed in this user's responses;
3. no absolute URLs, `..`, protocol-relative paths, hosts outside the connection, credentials;
4. only parameter names the contract allows, none that look like secrets;
5. only fields of the domain vocabulary, only read-only requests, only safe regular expressions.

A finding carries a code and the place in the spec, never the offending value, so the report can
go back to the model or to a log without leaking what it found.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import ValidationError

from botai.contracts.schemas import errors as schema_errors
from botai.contracts.specs import Recipe
from botai.core.errors import BotaiError, ErrorCode
from botai.sources import rxsafe, templates
from botai.sources.adapters.moodle_ws import ALLOWED_FUNCTIONS
from botai.sources.extract import selector_valid
from botai.sources.readonly import is_read_only_post
from botai.sources.vocab import REQUIRED, TABLE_EXTRA_FIELDS, TABLE_KINDS, VOCAB

SCHEMA = "adapter-spec.schema.json"
MAX_ISSUES = 50

# --- patterns -------------------------------------------------------------------------------

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(
    r"(?<!\d)(?:\+7|8|7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)|\+\d{10,15}"
)
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
SNILS = re.compile(r"\b\d{3}-\d{3}-\d{3}[ -]\d{2}\b")
LONG_DIGITS = re.compile(r"\d{6,}")
TOKEN_LIKE = re.compile(r"[A-Za-z0-9_+/=-]{24,}")
ABSOLUTE_URL = re.compile(r"(?i)[a-z][a-z0-9+.-]*://|(?<![\w:])//[\w.-]+|\bwww\.")
HOST_LIKE = re.compile(
    r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*\."
    r"(?:com|ru|org|net|edu|io|test|example|invalid|local|localhost|app|dev|info|xyz|su|рф)\b"
)
# Names that must not be parameters, query keys, pointers or attributes of a shared spec.
_STRONG = (
    r"(?:token|secret|password|sesskey|csrf|xsrf|sessionid|apikey|api[-_]key|bearer|cookie|jwt)"
)
SECRET_NAME = re.compile(
    r"(?i)(?:^|[-_.])(?:token|secret|passw(?:or)?d|pwd|sesskey|csrf|xsrf|session(?:id)?|"
    r"api[-_]?key|apikey|signature|jwt|bearer|cookie|auth(?:orization)?|email|login|otp)"
    r"(?:$|[-_.])|" + _STRONG
)
# For pointers and attributes only the unambiguous words count: "session" may be a class meeting.
STRONG_SECRET = re.compile(r"(?i)" + _STRONG)
FORBIDDEN_HEADERS = frozenset(
    {"cookie", "set-cookie", "host", "content-length", "transfer-encoding", "connection"}
)
# A GET that changes things on the source (log out, delete, edit...) must never be a read.
DESTRUCTIVE = re.compile(
    r"(?i)(?:^|[/_.-])(?:log-?out|sign-?out|log-?off|logoff|exit|delete|remove|destroy|drop|"
    r"unsubscribe|revoke|cancel|submit|edit|выход|выйти|удал\w*)(?:$|[/_.-])"
)
_CLEAN_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# Keys whose string value is structural and may look like a long hex number or a date.
_STRUCTURAL_KEYS = frozenset({"learnedAt", "learnedByDigest", "adapter", "version"})


@dataclass(frozen=True, slots=True)
class LintIssue:
    code: str
    path: str  # place in the spec; free-form keys are replaced by "<key>"
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(slots=True)
class LintContext:
    """What lint knows about the connection the spec is written for."""

    allowed_hosts: Collection[str] = ()
    expected_origin: str | None = None
    # Strings seen in this user's responses (cell values, JSON leaves, visible text). No literal
    # of the spec may equal one of the sensitive ones.
    observed: Collection[str] = field(default=(), repr=False)
    sensitive_values: frozenset[str] = field(init=False, default=frozenset(), repr=False)

    def __post_init__(self) -> None:
        self.sensitive_values = frozenset(
            v for v in (s.strip() for s in self.observed) if sensitive(v)
        )


@dataclass(slots=True)
class LintResult:
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def reasons(self) -> list[str]:
        """Short lines for the model's second attempt and for logs. Values are never included."""
        return [str(i) for i in self.issues]

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def to_error(self) -> BotaiError:
        return BotaiError(
            ErrorCode.RECIPE_REJECTED,
            f"Спецификация отклонена проверкой: замечаний {len(self.issues)}",
            extra={"issues": [{"code": i.code, "path": i.path} for i in self.issues]},
        )

    def raise_if_failed(self) -> None:
        if self.issues:
            raise self.to_error()


def sensitive(value: str) -> bool:
    """Would this observed value be personal or identifying if it ended up in a shared spec?"""
    if len(value) < 4:
        return False
    return any(c.isdigit() for c in value) or any(c.isupper() for c in value)


# --- entry point ----------------------------------------------------------------------------


def lint_spec(spec: Any, ctx: LintContext | None = None) -> LintResult:
    ctx = ctx or LintContext()
    result = LintResult()
    schema_problems = schema_errors(SCHEMA, spec)
    if schema_problems:
        for problem in schema_problems[:MAX_ISSUES]:
            where, _, _ = problem.partition(": ")
            result.issues.append(
                LintIssue("schema", "/" + _clean_path(where.split("/")), "нарушает схему контракта")
            )
        return result
    issues: list[LintIssue] = []
    adapter = spec["adapter"]
    if adapter == "recipe":
        _recipe(spec["recipe"], ctx, issues)
    elif adapter == "moodle_ws":
        _moodle(spec, ctx, issues)
    elif adapter == "ical":
        _ical(spec, ctx, issues)
    else:
        _document(spec, ctx, issues)
    _scan_personal(spec, issues)
    _scan_literals(spec, ctx, issues)
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.code, issue.path)
        if key not in seen:
            seen.add(key)
            result.issues.append(issue)
    del result.issues[MAX_ISSUES:]
    return result


def _clean_path(parts: Collection[str]) -> str:
    return "/".join(p if _CLEAN_KEY.match(p) else "<key>" for p in parts if p != "<root>")


def _p(*parts: Any) -> str:
    return "/" + _clean_path([str(p) for p in parts])


# --- hosts, origins -------------------------------------------------------------------------


def _host_of(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


def _check_origin(origin: str, ctx: LintContext, where: str, issues: list[LintIssue]) -> None:
    host = _host_of(origin)
    allowed = {h.lower() for h in ctx.allowed_hosts}
    if allowed and host not in allowed:
        issues.append(LintIssue("host.not_allowed", where, "хост вне разрешённых для подключения"))
    if ctx.expected_origin and origin.rstrip("/") != ctx.expected_origin.rstrip("/"):
        issues.append(LintIssue("origin.mismatch", where, "origin не совпадает с подключением"))
    if _is_ip_or_local(host):
        issues.append(LintIssue("host.ip_literal", where, "IP-адрес и localhost запрещены"))


def _is_ip_or_local(host: str) -> bool:
    if host in {"localhost", ""} or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


# --- paths and templates --------------------------------------------------------------------


def _check_path(path: str, where: str, issues: list[LintIssue]) -> None:
    decoded = unquote(unquote(path))
    segments = decoded.split("/")
    if path.startswith("//") or decoded.startswith("//"):
        issues.append(LintIssue("url.protocol_relative", where, "путь не может начинаться с //"))
    if ".." in decoded or any(seg == "." for seg in segments) or "\\" in decoded:
        issues.append(LintIssue("url.dot_segment", where, "в пути недопустимы .. и обратная косая"))
    if "#" in decoded or "?" in path:
        issues.append(
            LintIssue("url.fragment_or_query", where, "фрагмент и запрос в пути запрещены")
        )
    if DESTRUCTIVE.search(decoded):
        issues.append(
            LintIssue("url.destructive", where, "адрес похож на действие, а не на чтение")
        )


def _check_template(template: str, known: set[str], where: str, issues: list[LintIssue]) -> None:
    for name in templates.placeholders(template):
        if not templates.is_valid_placeholder(name, known):
            issues.append(LintIssue("template.unknown", where, "неизвестный параметр шаблона"))
        elif not templates.is_builtin(name) and SECRET_NAME.search(name):
            issues.append(
                LintIssue("template.secret_name", where, "имя параметра похоже на секрет")
            )
    stripped = templates.PLACEHOLDER.sub("", template)
    if "{" in stripped or "}" in stripped:
        issues.append(LintIssue("template.malformed", where, "незакрытая скобка шаблона"))


def _query_literals(request: Mapping[str, Any]) -> dict[str, str]:
    return {
        k: templates.PLACEHOLDER.sub("", v)
        for k, v in (request.get("query") or {}).items()
        if isinstance(v, str)
    }


# --- recipe ---------------------------------------------------------------------------------


def _recipe(recipe: Mapping[str, Any], ctx: LintContext, issues: list[LintIssue]) -> None:
    base = ("recipe",)
    if recipe["version"] == 2:
        try:
            # Cross-field data invariants (aliases, scalar sources, IANA zones and weekly
            # operands) belong to the typed executor too. Never forward Pydantic's raw input.
            Recipe.model_validate(recipe)
        except ValidationError:
            issues.append(
                LintIssue(
                    "data.invalid",
                    _p(*base),
                    "источники полей, зоны времени или сочетания выражений недопустимы",
                )
            )
    _check_origin(recipe["origin"], ctx, _p(*base, "origin"), issues)
    auth = recipe["auth"]
    header = str(auth.get("headerName", "")).lower()
    if header in FORBIDDEN_HEADERS:
        issues.append(
            LintIssue("auth.header", _p(*base, "auth", "headerName"), "запрещённый заголовок")
        )

    known: set[str] = set()
    step_ids: set[str] = set()
    for i, step in enumerate(recipe.get("bootstrap", [])):
        where = (*base, "bootstrap", i)
        if step["id"] in step_ids:
            issues.append(LintIssue("bootstrap.duplicate", _p(*where, "id"), "повтор id шага"))
        step_ids.add(step["id"])
        _check_path(step["path"], _p(*where, "path"), issues)
        _check_template(step["path"], known, _p(*where, "path"), issues)
        for name, pointer in step["extract"].items():
            if SECRET_NAME.search(name):
                issues.append(
                    LintIssue(
                        "template.secret_name", _p(*where, "extract", name), "имя похоже на секрет"
                    )
                )
            _check_pointer(pointer, _p(*where, "extract", name), issues)
        known |= set(step["extract"])

    endpoint_ids: set[str] = set()
    kinds: set[str] = set()
    for i, endpoint in enumerate(recipe["endpoints"]):
        _endpoint(endpoint, known, (*base, "endpoints", i), endpoint_ids, issues)
        kinds.add(endpoint["kind"])
    missing = set(recipe["validation"]["requireKinds"]) - kinds
    if missing:
        issues.append(
            LintIssue(
                "validation.kind_not_covered", _p(*base, "validation"), "requireKinds без эндпоинта"
            )
        )


def _check_pointer(pointer: str, where: str, issues: list[LintIssue]) -> None:
    segments = [s.replace("~1", "/").replace("~0", "~") for s in pointer.split("/")[1:]]
    if any(STRONG_SECRET.search(s) for s in segments if not s.isdigit()):
        issues.append(LintIssue("pointer.secret", where, "указатель ведёт к секрету"))


def _endpoint(
    endpoint: Mapping[str, Any],
    known: set[str],
    base: tuple[Any, ...],
    ids: set[str],
    issues: list[LintIssue],
) -> None:
    if endpoint["id"] in ids:
        issues.append(LintIssue("endpoint.duplicate", _p(*base, "id"), "повтор id эндпоинта"))
    ids.add(endpoint["id"])
    kind = endpoint["kind"]
    request = endpoint["request"]
    req_base = (*base, "request")
    _check_path(request["path"], _p(*req_base, "path"), issues)
    _check_template(request["path"], known, _p(*req_base, "path"), issues)
    for section in ("query", "body"):
        for key, value in (request.get(section) or {}).items():
            where = _p(*req_base, section, key)
            if SECRET_NAME.search(key):
                issues.append(
                    LintIssue("param.secret_name", where, "имя параметра похоже на секрет")
                )
            _check_template(value, known, where, issues)
    if request["method"] == "POST" and not (
        request.get("readOnlyPost") and is_read_only_post(request["path"], _query_literals(request))
    ):
        issues.append(
            LintIssue(
                "method.post_not_allowed", _p(*req_base, "method"), "POST вне allowlist чтения"
            )
        )

    if request.get("browserPlan") is not None:
        _browser_plan(request["browserPlan"], (*req_base, "browserPlan"), issues)

    pagination = endpoint.get("pagination")
    response = endpoint["response"]
    if pagination:
        for name in ("cursorPath", "totalPath"):
            if pagination.get(name):
                _check_pointer(pagination[name], _p(*base, "pagination", name), issues)
        for name in ("param", "limitParam"):
            if name in pagination and SECRET_NAME.search(pagination[name]):
                issues.append(
                    LintIssue(
                        "param.secret_name", _p(*base, "pagination", name), "имя похоже на секрет"
                    )
                )
        if pagination["type"] == "cursor" and (
            not pagination.get("cursorPath") or response["format"] != "json"
        ):
            issues.append(
                LintIssue(
                    "pagination.cursor", _p(*base, "pagination"), "курсор нужен путь и ответ JSON"
                )
            )
    _response(kind, response, (*base, "response"), issues)


def _browser_plan(node: Any, base: tuple[Any, ...], issues: list[LintIssue]) -> None:
    """Apply the same CSS policy to every nested, schema-validated replay descriptor."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            where = (*base, key)
            if key == "selector":
                _selector(value, _p(*where), issues)
            elif isinstance(value, (Mapping, list)):
                _browser_plan(value, where, issues)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _browser_plan(item, (*base, index), issues)


def _response(
    kind: Any, response: Mapping[str, Any], base: tuple[Any, ...], issues: list[LintIssue]
) -> None:
    fields: Mapping[str, Any] = response["fields"]
    vocab = VOCAB[kind]
    for name in fields:
        if name not in vocab:
            issues.append(
                LintIssue("field.unknown", _p(*base, "fields", name), "поля нет в словаре вида")
            )
    names = set(fields)
    if "externalId" not in names and not response.get("identity"):
        issues.append(LintIssue("identity.missing", _p(*base), "нужен externalId или identity"))
    for ident in response.get("identity") or []:
        if ident not in names:
            issues.append(
                LintIssue(
                    "identity.unknown_field",
                    _p(*base, "identity"),
                    "identity ссылается на несуществующее поле",
                )
            )
    generated = {"startsAt", "endsAt"} if response.get("expand") else set()
    available = names | generated
    needed = set(REQUIRED[kind])
    if kind == "scheduleEvents" and not ({"endsAt", "durationMinutes"} & available):
        needed.add("endsAt")
    for name in sorted(needed - available):
        issues.append(
            LintIssue(
                "field.missing_required", _p(*base, "fields", name), "обязательное поле не описано"
            )
        )

    for name, field_spec in fields.items():
        field_base = (*base, "fields", name)
        if "compose" in field_spec:
            for operand in ("date", "time"):
                _data_scalar(
                    field_spec["compose"][operand],
                    response["format"],
                    _p(*field_base, "compose", operand),
                    issues,
                )
        else:
            _data_scalar(field_spec, response["format"], _p(*field_base), issues)
    for index, collection in enumerate(response.get("collections") or []):
        where = _p(*base, "collections", index)
        if response["format"] == "json":
            _check_pointer(collection["path"], where, issues)
        else:
            _selector(collection["selector"], where, issues)
    expansion = response.get("expand")
    if expansion:
        for name in (
            "startDate",
            "untilDate",
            "weekdays",
            "startTime",
            "endTime",
            "durationMinutes",
            "anchorDate",
            "excludeDates",
            "includeDates",
        ):
            operand = expansion.get(name)
            if isinstance(operand, Mapping):
                _data_scalar(operand, response["format"], _p(*base, "expand", name), issues)
    if response.get("itemsPath") is not None:
        _check_pointer(response["itemsPath"], _p(*base, "itemsPath"), issues)
    if response.get("itemsSelector") is not None:
        _selector(response["itemsSelector"], _p(*base, "itemsSelector"), issues)


def _data_scalar(
    field_spec: Mapping[str, Any], format: str, where: str, issues: list[LintIssue]
) -> None:
    if format == "json":
        _check_pointer(field_spec["path"], where, issues)
        if field_spec.get("fallbackPath"):
            _check_pointer(field_spec["fallbackPath"], where, issues)
        return
    _selector(field_spec["selector"], where, issues, allow_self=True)
    attr = field_spec.get("attr")
    if attr and (
        STRONG_SECRET.search(attr) or attr.lower().startswith("on") or attr.lower() == "style"
    ):
        issues.append(LintIssue("html.attr", where, "атрибут запрещён"))
    pattern = field_spec.get("pattern")
    if pattern:
        for reason in rxsafe.problems(pattern):
            issues.append(LintIssue("pattern.unsafe", where, reason))


def _selector(
    selector: str, where: str, issues: list[LintIssue], *, allow_self: bool = False
) -> None:
    if allow_self and selector == ".":
        return  # the list element itself
    if ":has(" in selector or "::" in selector or not selector_valid(selector):
        issues.append(LintIssue("html.selector", where, "селектор недопустим"))


# --- other adapters -------------------------------------------------------------------------


def _moodle(spec: Mapping[str, Any], ctx: LintContext, issues: list[LintIssue]) -> None:
    _check_origin(spec["origin"], ctx, _p("origin"), issues)
    for i, function in enumerate(spec["functions"]):
        if function not in ALLOWED_FUNCTIONS:
            issues.append(
                LintIssue("moodle.function", _p("functions", i), "функции нет в allowlist")
            )


def _ical(spec: Mapping[str, Any], ctx: LintContext, issues: list[LintIssue]) -> None:
    _check_origin(spec["origin"], ctx, _p("origin"), issues)
    cond = (spec.get("classify") or {}).get("deadlineIf") or {}
    pattern = cond.get("summaryPattern")
    if pattern:
        for reason in rxsafe.problems(pattern):
            issues.append(
                LintIssue("pattern.unsafe", _p("classify", "deadlineIf", "summaryPattern"), reason)
            )


def _document(spec: Mapping[str, Any], ctx: LintContext, issues: list[LintIssue]) -> None:
    source = spec["source"]
    url = source.get("url")
    if source["kind"] != "upload" and not url:
        issues.append(LintIssue("document.url", _p("source"), "для источника нужна ссылка"))
    if url:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username or parts.password or _is_ip_or_local(host):
            issues.append(
                LintIssue(
                    "document.url",
                    _p("source", "url"),
                    "ссылка должна быть https без учётных данных и IP",
                )
            )
        allowed = {h.lower() for h in ctx.allowed_hosts}
        if allowed and host not in allowed:
            issues.append(
                LintIssue(
                    "host.not_allowed", _p("source", "url"), "хост вне разрешённых для подключения"
                )
            )
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if SECRET_NAME.search(key):
                issues.append(
                    LintIssue("document.url_secret", _p("source", "url"), "в ссылке есть секрет")
                )
        if source["kind"] in {"google_doc", "google_sheet"} and host != "docs.google.com":
            issues.append(
                LintIssue("document.url", _p("source", "url"), "ссылка не на документ Google")
            )
    table = spec.get("table")
    if not table:
        return
    if table["kind"] not in TABLE_KINDS:
        issues.append(
            LintIssue("table.kind", _p("table", "kind"), "вид не поддерживается таблицами")
        )
        return
    allowed_columns = VOCAB[table["kind"]] | TABLE_EXTRA_FIELDS
    for target in table["columns"]:
        if target not in allowed_columns or target == "externalId":
            issues.append(
                LintIssue(
                    "field.unknown", _p("table", "columns", target), "столбец не из словаря вида"
                )
            )
    if not (REQUIRED[table["kind"]] <= set(table["columns"]) | _date_aliases(table["columns"])):
        issues.append(
            LintIssue(
                "field.missing_required", _p("table", "columns"), "не хватает обязательных столбцов"
            )
        )


def _date_aliases(columns: Mapping[str, Any]) -> set[str]:
    """A `date` column can stand in for startsAt/dueAt (the adapter composes them)."""
    return {"startsAt", "dueAt"} if "date" in columns else set()


# --- generic scans --------------------------------------------------------------------------


def _strings(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _strings(value, (*path, str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            yield from _strings(value, (*path, str(i)))


def _scan_personal(spec: Mapping[str, Any], issues: list[LintIssue]) -> None:
    for path, value in _strings(spec):
        key = path[-1] if path else ""
        if key in _STRUCTURAL_KEYS or key == "origin":
            continue
        where = _p(*path)
        is_doc_url = path[:2] == ("source", "url")
        if EMAIL.search(value):
            issues.append(LintIssue("personal.email", where, "похоже на e-mail"))
        if JWT.search(value):
            issues.append(LintIssue("personal.jwt", where, "похоже на JWT"))
        if is_doc_url:
            continue  # a document link is a link by design; its secrets are checked separately
        if ABSOLUTE_URL.search(value):
            issues.append(LintIssue("url.absolute", where, "абсолютные адреса запрещены"))
        if PHONE.search(value):
            issues.append(LintIssue("personal.phone", where, "похоже на телефон"))
        if UUID.search(value):
            issues.append(LintIssue("personal.uuid", where, "похоже на UUID"))
        if SNILS.search(value):
            issues.append(LintIssue("personal.snils", where, "похоже на СНИЛС"))
        if LONG_DIGITS.search(value):
            issues.append(LintIssue("personal.digits", where, "число из 6 и более цифр"))
        if _token_like(value):
            issues.append(LintIssue("personal.token", where, "похоже на токен"))
        in_value_field = "query" in path or "body" in path or "notes" in path
        if in_value_field and HOST_LIKE.search(templates.PLACEHOLDER.sub("", value)):
            issues.append(LintIssue("url.host_in_value", where, "в значении есть имя хоста"))


def _token_like(value: str) -> bool:
    """A long run of letters and digits that is not prose or a slug: key, session or hash."""
    for token in TOKEN_LIKE.findall(templates.PLACEHOLDER.sub("", value)):
        digits = sum(c.isdigit() for c in token)
        letters = sum(c.isalpha() for c in token)
        if digits >= 2 and letters >= 2:
            return True
    return False


def _literals(spec: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """(where, literal) for every place a model could have copied an observed value into."""
    adapter = spec["adapter"]
    if adapter == "recipe":
        recipe = spec["recipe"]
        yield _p("recipe", "notes"), recipe.get("notes", "")
        for i, step in enumerate(recipe.get("bootstrap", [])):
            yield from _path_literals(step["path"], ("recipe", "bootstrap", i, "path"))
        for i, endpoint in enumerate(recipe["endpoints"]):
            base = ("recipe", "endpoints", i)
            request = endpoint["request"]
            yield from _path_literals(request["path"], (*base, "request", "path"))
            for section in ("query", "body"):
                for key, value in (request.get(section) or {}).items():
                    yield _p(*base, "request", section, key), templates.PLACEHOLDER.sub("", value)
            response = endpoint["response"]
            if recipe["version"] == 2:
                yield from _data_literals(response, (*base, "response"))
                continue
            for name, field_spec in response["fields"].items():
                where = (*base, "response", "fields", name)
                yield _p(*where, "selector"), field_spec.get("selector", "")
                yield _p(*where, "pattern"), field_spec.get("pattern", "")
            yield _p(*base, "response", "itemsSelector"), response.get("itemsSelector", "")
    elif adapter == "ical":
        cond = (spec.get("classify") or {}).get("deadlineIf") or {}
        yield _p("classify", "deadlineIf", "summaryPattern"), cond.get("summaryPattern", "")
        for i, cat in enumerate(cond.get("categoryIn", [])):
            yield _p("classify", "deadlineIf", "categoryIn", i), cat
    elif adapter == "document":
        table = spec.get("table") or {}
        yield _p("table", "sheet"), table.get("sheet", "")
        for target, mapping in (table.get("columns") or {}).items():
            yield _p("table", "columns", target), mapping.get("column", "")
        yield _p("table", "rowFilter", "column"), (table.get("rowFilter") or {}).get("column", "")


def _data_literals(node: Any, base: tuple[Any, ...]) -> Iterator[tuple[str, str]]:
    """V2's nested extractors are shared structural data, never copied student values."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            where = (*base, key)
            if isinstance(value, str) and key in {
                "selector",
                "itemsSelector",
                "pattern",
                "attr",
                "as",
            }:
                yield _p(*where), value
            elif isinstance(value, str) and key in {"path", "fallbackPath", "itemsPath"}:
                yield from _path_literals(value, where)
            elif isinstance(value, (Mapping, list)):
                yield from _data_literals(value, where)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _data_literals(item, (*base, index))


def _path_literals(path: str, base: tuple[Any, ...]) -> Iterator[tuple[str, str]]:
    for segment in unquote(templates.PLACEHOLDER.sub("", path)).split("/"):
        if segment:
            yield _p(*base), segment


def _scan_literals(spec: Mapping[str, Any], ctx: LintContext, issues: list[LintIssue]) -> None:
    sensitive_values = ctx.sensitive_values
    if not sensitive_values:
        return
    for where, literal in _literals(spec):
        if not literal:
            continue
        stripped = literal.strip()
        if stripped in sensitive_values or any(
            len(v) >= 8 and v in literal for v in sensitive_values
        ):
            issues.append(
                LintIssue("observed.value", where, "равно значению из ответов пользователя")
            )
