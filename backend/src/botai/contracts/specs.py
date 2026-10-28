"""Typed views of adapter specs and login presets (contracts/adapter-spec, recipe, login-preset).

Structure only; the authoritative validation is `botai.contracts.schemas.errors(...)`, which the
spec lint runs first. Agents write JSON that must pass both.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

Kind = Literal[
    "courses", "scheduleEvents", "deadlines", "assignments", "grades", "people", "locations", "news"
]
Transform = Literal[
    "iso", "unixSeconds", "unixMillis", "trim", "number", "bool", "html2text", "joinNames"
]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)


class JsonField(_M):
    path: str
    transform: Transform | None = None
    fallback_path: str | None = Field(default=None)
    required: bool = False


class HtmlField(_M):
    selector: str
    attr: str | None = None
    transform: Transform | None = None
    pattern: str | None = None
    required: bool = False


class JsonResponse(_M):
    format: Literal["json"]
    items_path: str = Field()
    fields: dict[str, JsonField]
    identity: list[str] | None = None


class HtmlResponse(_M):
    format: Literal["html"]
    items_selector: str = Field()
    fields: dict[str, HtmlField]
    identity: list[str] | None = None


class RecipeRequest(_M):
    render: bool = False
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
        return self


class BootstrapStep(_M):
    id: str
    method: Literal["GET"]
    path: str
    extract: dict[str, str]


class RecipeAuth(_M):
    type: Literal["cookie", "bearer", "none"]
    header_name: str | None = Field(default=None)


class RecipeValidation(_M):
    require_kinds: list[Kind] = Field()
    min_items: int = Field(default=1)


class Freshness(_M):
    sync_every_minutes: int | None = Field(default=None)
    stale_after_minutes: int | None = Field(default=None)


class Recipe(_M):
    version: Literal[1]
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
