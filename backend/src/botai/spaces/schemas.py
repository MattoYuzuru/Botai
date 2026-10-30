from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from botai.core.api import ApiModel
from botai.spaces.views import ConnectionKind

ConnectionStatus = Literal[
    "draft",
    "learning",
    "active",
    "syncing",
    "stale",
    "needs_reauth",
    "waiting_user",
    "repairing",
    "error",
    "disabled",
]
# A calendar link is not one of them: it is a task's protected field (chat), where it is checked and read.
SecretKindIn = Literal["token", "api_key"]


class UniversityOut(ApiModel):
    id: str
    title: str
    short_title: str
    aliases: list[str]
    origins: list[str]
    accent_hue: float
    third_party_warning: str | None = None


class SpaceCreate(ApiModel):
    title: str = Field(
        min_length=1, max_length=120, description="Название вуза, как его пишет студент"
    )
    university_id: str | None = None  # from GET /universities, if already learned
    homepage_url: str | None = Field(default=None, max_length=500)


class SpaceUpdate(ApiModel):
    title: str | None = Field(default=None, max_length=120)
    time_zone: str | None = None


class SpaceOut(ApiModel):
    """A university of the student (or "My materials", `universityId` null)."""

    id: str
    title: str
    short_title: str
    aliases: list[str]
    university_id: str | None
    accent_hue: float
    time_zone: str
    created_at: datetime


class ConnectionCreate(ApiModel):
    kind: ConnectionKind | None = None  # None: let the agent discover what to connect
    url: str | None = Field(default=None, max_length=1000)
    title: str | None = Field(default=None, max_length=120)


class ActiveTaskOut(ApiModel):
    job_id: str
    kind: Literal["connect", "repair", "sync", "ingest"]
    state: Literal["queued", "running", "waiting_user"]


class ConnectionOut(ApiModel):
    """A source of a university."""

    id: str
    space_id: str
    origin: str
    kind: ConnectionKind
    adapter: str | None
    title: str  # human; never a bare host
    entry_url: str | None = None  # the UI page of the source, for "open in the portal"
    status: ConnectionStatus
    has_password: bool
    # kinds of user-supplied secrets present, never their values
    stored_secrets: list[Literal["token", "ical_url", "api_key"]] = Field(default_factory=list)
    last_sync_at: datetime | None
    next_sync_at: datetime | None
    error_code: str | None = None
    error_detail: str | None = None
    thread_id: str | None = None  # the thread of the latest task of this source
    active_task: ActiveTaskOut | None = None
    # Among the university's sources that have schedule events: the one that decides when they show
    # the same class (`primary`) or the others (`secondary`); null for a source without a schedule
    # and for a disabled one (FR-054).
    schedule_role: Literal["primary", "secondary"] | None = None


class PrimarySourceIn(ApiModel):
    """`connectionId`: a live source of this university; null returns to the default."""

    connection_id: str | None = Field(default=None, pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")


class ConnectionStarted(ApiModel):
    connection: ConnectionOut
    job_id: str


class SecretIn(ApiModel):
    kind: SecretKindIn
    value: str = Field(min_length=1, max_length=4000)


class JobRef(ApiModel):
    job_id: str


class ConversationIn(ApiModel):
    job_id: str | None = Field(default=None, pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")


class SectionOut(ApiModel):
    """Dynamic sections (FR-014): only kinds that have data appear."""

    kind: Literal[
        "courses",
        "scheduleEvents",
        "deadlines",
        "assignments",
        "grades",
        "people",
        "locations",
        "news",
        "documents",
    ]
    count: int
