"""Typed views of adapter specs and login presets (contracts/adapter-spec, recipe, login-preset).

Structure only; the authoritative validation is `botai.contracts.schemas.errors(...)`, which the
spec lint runs first. Agents write JSON that must pass both.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from botai.contracts.browser_plan import BrowserPlan

Kind = Literal[
    "courses", "scheduleEvents", "deadlines", "assignments", "grades", "people", "locations", "news"
]
Transform = Literal[
    "iso", "unixSeconds", "unixMillis", "trim", "number", "bool", "html2text", "joinNames"
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)


class JsonScalar(_M):
    path: str
    from_: str = Field(default="item", alias="from")
    fallback_path: str | None = Field(default=None)


class JsonField(JsonScalar):
    transform: Transform | None = None
    required: bool = False


class HtmlScalar(_M):
    selector: str
    from_: str = Field(default="item", alias="from")
    attr: str | None = None
    pattern: str | None = None


class HtmlField(HtmlScalar):
    transform: Transform | None = None
    required: bool = False


class _Zoned(_M):
    time_zone: str
    fold: Literal["reject", "earlier", "later"] = "reject"

    @model_validator(mode="after")
    def validate_zone(self) -> Self:
        try:
            ZoneInfo(self.time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("timeZone must be an installed IANA time zone") from None
        return self


class JsonDateTime(_Zoned):
    type: Literal["dateTime"]
    date: JsonScalar
    time: JsonScalar
    day_offset: Literal[0, 1] = 0


class HtmlDateTime(_Zoned):
    type: Literal["dateTime"]
    date: HtmlScalar
    time: HtmlScalar
    day_offset: Literal[0, 1] = 0


class JsonComposedField(_M):
    compose: JsonDateTime
    required: bool = False


class HtmlComposedField(_M):
    compose: HtmlDateTime
    required: bool = False


class JsonCollection(_M):
    as_: str = Field(alias="as", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    path: str


class HtmlCollection(_M):
    as_: str = Field(alias="as", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    selector: str


class ExpansionHorizon(_M):
    from_: str = Field(
        alias="from",
        pattern=r"^\{(?:today|weekStart|(?:plusDays|minusDays):[0-9]{1,3})\}$",
    )
    days: int = Field(ge=1, le=93, strict=True)


class _Weekly(_Zoned):
    type: Literal["weekly"]
    interval_weeks: int = Field(default=1, ge=1, le=4, strict=True)
    end_day_offset: Literal[0, 1] = 0
    horizon: ExpansionHorizon
    max_occurrences: int = Field(default=512, ge=1, le=512, strict=True)


class JsonWeekly(_Weekly):
    start_date: JsonScalar
    until_date: JsonScalar
    weekdays: JsonScalar
    start_time: JsonScalar
    end_time: JsonScalar | None = None
    duration_minutes: JsonScalar | Annotated[int, Field(ge=1, le=1440, strict=True)] | None = None
    anchor_date: JsonScalar | None = None
    exclude_dates: JsonScalar | None = None
    include_dates: JsonScalar | None = None

    @model_validator(mode="after")
    def validate_weekly(self) -> Self:
        _validate_weekly(self)
        return self


class HtmlWeekly(_Weekly):
    start_date: HtmlScalar
    until_date: HtmlScalar
    weekdays: HtmlScalar
    start_time: HtmlScalar
    end_time: HtmlScalar | None = None
    duration_minutes: HtmlScalar | Annotated[int, Field(ge=1, le=1440, strict=True)] | None = None
    anchor_date: HtmlScalar | None = None
    exclude_dates: HtmlScalar | None = None
    include_dates: HtmlScalar | None = None

    @model_validator(mode="after")
    def validate_weekly(self) -> Self:
        _validate_weekly(self)
        return self


def _validate_weekly(value: JsonWeekly | HtmlWeekly) -> None:
    if (value.end_time is None) == (value.duration_minutes is None):
        raise ValueError("weekly expansion requires exactly one of endTime and durationMinutes")
    if value.interval_weeks > 1 and value.anchor_date is None:
        raise ValueError("multiweek expansion requires an explicit anchorDate")
    if value.duration_minutes is not None and value.end_day_offset:
        raise ValueError("endDayOffset requires endTime")


class JsonResponse(_M):
    format: Literal["json"]
    items_path: str | None = None
    collections: list[JsonCollection] | None = Field(default=None, min_length=1, max_length=4)
    fields: dict[str, JsonField | JsonComposedField]
    identity: list[str] | None = None
    expand: JsonWeekly | None = None

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        _validate_sources(self.items_path, self.collections, self)
        return self


class HtmlResponse(_M):
    format: Literal["html"]
    items_selector: str | None = None
    collections: list[HtmlCollection] | None = Field(default=None, min_length=1, max_length=4)
    fields: dict[str, HtmlField | HtmlComposedField]
    identity: list[str] | None = None
    expand: HtmlWeekly | None = None

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        _validate_sources(self.items_selector, self.collections, self)
        return self


def _validate_sources(
    legacy: str | None,
    collections: list[JsonCollection] | list[HtmlCollection] | None,
    response: JsonResponse | HtmlResponse,
) -> None:
    if (legacy is None) == (collections is None):
        raise ValueError("response requires exactly one of legacy item source and collections")
    aliases = [c.as_ for c in collections or []]
    if len(set(aliases)) != len(aliases) or {"item", "root"}.intersection(aliases):
        raise ValueError("collection aliases must be unique and not reserved")
    allowed = {"item", "root", *aliases}
    for obj in _scalar_sources(response):
        if obj.from_ not in allowed:
            raise ValueError("scalar source must name item, root or a collection alias")


def _scalar_sources(value: _M) -> list[JsonScalar | HtmlScalar]:
    if isinstance(value, (JsonScalar, HtmlScalar)):
        return [value]
    found: list[JsonScalar | HtmlScalar] = []
    for name in type(value).model_fields:
        child = getattr(value, name)
        if isinstance(child, _M):
            found.extend(_scalar_sources(child))
        elif isinstance(child, dict):
            for item in child.values():
                if isinstance(item, _M):
                    found.extend(_scalar_sources(item))
    return found


class RecipeRequest(_M):
    render: bool = False
    auth_binding: str | None = None
    browser_plan: BrowserPlan | None = None
    method: Literal["GET", "POST"]
    read_only_post: bool = Field(default=False)
    path: str
    query: dict[str, str] | None = None
    body: dict[str, str] | None = None


class Pagination(_M):
    type: Literal["none", "offset", "page", "cursor"]
    param: str | None = None
    limit_param: str | None = Field(default=None)
    limit: int | None = None
    max_pages: int = Field(default=5)
    cursor_path: str | None = Field(default=None)
    total_path: str | None = Field(default=None)


class Endpoint(_M):
    id: str
    kind: Kind
    request: RecipeRequest
    pagination: Pagination | None = None
    response: Annotated[JsonResponse | HtmlResponse, Field(discriminator="format")]

    @model_validator(mode="after")
    def validate_rendering(self) -> Self:
        if self.request.render and (
            self.request.method != "GET"
            or self.request.body is not None
            or not isinstance(self.response, HtmlResponse)
        ):
            raise ValueError("rendered endpoints require GET HTML without a body")
        if self.request.browser_plan is not None and (
            not self.request.render
            or (self.pagination is not None and self.pagination.type != "none")
        ):
            raise ValueError("browser replay requires rendered GET and owns its pagination")
        if self.response.expand is not None and self.kind != "scheduleEvents":
            raise ValueError("weekly expansion only supports scheduleEvents")
        if self.response.expand is not None and {
            "startsAt",
            "endsAt",
            "durationMinutes",
        }.intersection(self.response.fields):
            raise ValueError("weekly expansion owns startsAt and endsAt")
        return self


class BootstrapStep(_M):
    id: str
    auth_binding: str | None = None
    method: Literal["GET"]
    path: str
    extract: dict[str, str]


class SessionBinding(_M):
    cookies: bool
    bearer: Literal["none", "observedAuthorization"]


class RecipeAuth(_M):
    type: Literal["cookie", "bearer", "none", "session"]
    header_name: str | None = Field(default=None)
    version: Literal[1] | None = None
    bindings: dict[str, SessionBinding] | None = Field(default=None, min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.type == "session":
            if self.version != 1 or not self.bindings or self.header_name is not None:
                raise ValueError("session auth requires versioned executor bindings")
        elif self.version is not None or self.bindings is not None:
            raise ValueError("legacy auth does not accept executor bindings")
        return self


class RecipeValidation(_M):
    require_kinds: list[Kind] = Field()
    min_items: int = Field(default=1)


class Freshness(_M):
    sync_every_minutes: int | None = Field(default=None)
    stale_after_minutes: int | None = Field(default=None)


class Recipe(_M):
    version: Literal[1, 2]
    origin: str
    revision: int
    learned_at: str = Field()
    learned_by_digest: str | None = Field(default=None)
    auth: RecipeAuth
    bootstrap: list[BootstrapStep] = Field(default_factory=list)
    endpoints: list[Endpoint]
    validation: RecipeValidation
    freshness: Freshness | None = None
    notes: str | None = None

    @model_serializer(mode="wrap")
    def serialize_version(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.version == 1:
            auth = data.get("auth")
            if isinstance(auth, dict):
                auth.pop("version", None)
                auth.pop("bindings", None)
            for step in data.get("bootstrap", []):
                step.pop("authBinding", None)
                step.pop("auth_binding", None)
            for endpoint in data.get("endpoints", []):
                request = endpoint.get("request")
                if isinstance(request, dict):
                    request.pop("authBinding", None)
                    request.pop("auth_binding", None)
                    request.pop("browserPlan", None)
                    request.pop("browser_plan", None)
            # Added defaults must not become explicit v2 syntax in a typed v1 round trip.
            for endpoint in data.get("endpoints", []):
                response = endpoint.get("response")
                if not isinstance(response, dict):
                    continue
                response.pop("collections", None)
                response.pop("expand", None)
                for field in response.get("fields", {}).values():
                    field.pop("from", None)
                    field.pop("from_", None)
        return data

    @model_validator(mode="after")
    def validate_data_version(self) -> Self:
        if self.auth.type == "session":
            if self.version != 2:
                raise ValueError("executor bindings require recipe v2")
            bindings = self.auth.bindings or {}
            if any(step.auth_binding not in bindings for step in self.bootstrap) or any(
                endpoint.request.auth_binding not in bindings
                for endpoint in self.endpoints
                if not endpoint.request.render
            ):
                raise ValueError("request refers to an unknown auth binding")
        if self.version == 1:
            if any("auth_binding" in step.model_fields_set for step in self.bootstrap) or any(
                "auth_binding" in endpoint.request.model_fields_set
                or "browser_plan" in endpoint.request.model_fields_set
                for endpoint in self.endpoints
            ):
                raise ValueError("request auth bindings require recipe v2")
            for endpoint in self.endpoints:
                response = endpoint.response
                if (
                    response.collections is not None
                    or response.expand is not None
                    or any(
                        isinstance(f, (JsonComposedField, HtmlComposedField))
                        for f in response.fields.values()
                    )
                    or any(
                        "from_" in scalar.model_fields_set for scalar in _scalar_sources(response)
                    )
                ):
                    raise ValueError(
                        "collections, source aliases, composition and expansion require recipe v2"
                    )
        return self


class RecipeSpec(_M):
    adapter: Literal["recipe"]
    recipe: Recipe


class MoodleWsSpec(_M):
    adapter: Literal["moodle_ws"]
    origin: str
    service: str = "moodle_mobile_app"
    functions: list[str]
    horizon_days: int = Field(default=60)


class IcalSpec(_M):
    adapter: Literal["ical"]
    origin: str
    classify: dict[str, Any] | None = None


class DocumentSpec(_M):
    adapter: Literal["document"]
    source: dict[str, Any]
    format: Literal["pdf", "docx", "xlsx", "csv", "html", "text"] | None = None
    table: dict[str, Any] | None = None


AdapterSpec = Annotated[
    RecipeSpec | MoodleWsSpec | IcalSpec | DocumentSpec, Field(discriminator="adapter")
]


class LoginField(_M):
    role: Literal["username", "password", "otp", "text", "checkbox"]
    selector: str
    label: str


class LoginStep(_M):
    id: str
    match: dict[str, str]
    fields: list[LoginField]
    submit: str | None = None
    kind: Literal["form", "push_wait", "captcha", "choice"] = "form"


class LoginPreset(_M):
    """Learned login preset for one origin. Captcha/MFA are never automated."""

    id: str
    name: str
    university_id: str | None = Field(default=None)
    time_zone: str = Field(default="Europe/Moscow")
    revision: int | None = None
    start: dict[str, str]
    allow_hosts: list[str] = Field()
    idp: dict[str, Any] | None = None
    success: dict[str, Any]
    expired: dict[str, Any]
    mfa: dict[str, Any]
    captcha: dict[str, Any]
    bot_protection: dict[str, Any] | None = Field(default=None)
    capture: dict[str, Any]
    mode: Literal["assisted", "screen"] = "assisted"
    steps: list[LoginStep] = Field(default_factory=list)
    errors: list[dict[str, str]] | None = None
