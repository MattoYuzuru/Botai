"""Input shapes of the v4 source service.

`SourcePatch` distinguishes a field left out (unchanged) from a field sent as `null` (cleared):
`model_fields_set` is what the update function reads, so `{"authRef": null}` clears the handle while
`{}` changes nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from botai.core.api import ApiModel


class SourcePatch(ApiModel):
    """Fields a caller may change on a source; every field is optional."""

    title: str | None = Field(default=None, max_length=200)
    entry_url: str | None = Field(default=None, max_length=2000)
    identity: str | None = Field(default=None, max_length=400)
    kinds: list[str] | None = None
    status: str | None = None
    playbook_id: str | None = Field(default=None, max_length=26)
    config: dict[str, Any] | None = None
    auth_ref: str | None = Field(default=None, max_length=200)
    priority: int | None = None
    trust: str | None = None
    last_error: dict[str, Any] | None = None
    last_sync_at: datetime | None = None


# --- REST / tool inputs and outputs of the v4 sources API ----------------------------------------


class SourceCreateIn(ApiModel):
    """POST /sources: exactly one way to name the source's identity, plus its kinds."""

    space_id: str = Field(min_length=1, max_length=26)
    title: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    link: str | None = Field(default=None, max_length=2000, description="Личная ссылка календаря")
    file_sha256: str | None = Field(default=None, max_length=64)
    mailbox: str | None = Field(default=None, max_length=320)
    kinds: list[str] = Field(default_factory=list)
    playbook_id: str | None = Field(default=None, max_length=26)
    config: dict[str, Any] = Field(default_factory=dict)
    auth_ref: str | None = Field(default=None, max_length=200)


class SourceOut(ApiModel):
    """A source as the application shows it: no secrets, only the identity digest and state."""

    id: str
    space_id: str
    university_id: str | None
    title: str
    entry_url: str | None
    kinds: list[str]
    status: str
    playbook_id: str | None
    config: dict[str, Any]
    auth_ref: str | None
    priority: int
    trust: str
    last_sync_at: datetime | None
    last_error: dict[str, Any] | None
    disabled_from: str | None
    created_at: datetime
    updated_at: datetime


class ProbeIn(ApiModel):
    """A candidate spec/code to try without saving it (or a version to re-run as a probe)."""

    spec: dict[str, Any] | None = None
    code: str | None = Field(default=None, max_length=200_000)
    language: str = Field(default="python", pattern="^(python|js)$")
    version: int | None = Field(default=None, ge=1)
    persist: bool = False


class SyncIn(ApiModel):
    version: int | None = Field(default=None, ge=1)


class CanaryIn(ApiModel):
    version: int | None = Field(default=None, ge=1)


class RepairIn(ApiModel):
    spec: dict[str, Any] | None = None
    code: str | None = Field(default=None, max_length=200_000)
    language: str = Field(default="python", pattern="^(python|js)$")
    version: int | None = Field(default=None, ge=1)
    activate: bool = False


class PlaybookCreateIn(ApiModel):
    """POST /sources/{id}/playbook: create and attach a playbook to the source."""

    title: str = Field(min_length=1, max_length=200)
    language: str = Field(default="declarative", pattern="^(declarative|python|js)$")
    university_id: str | None = Field(default=None, max_length=50)
    shared: bool = False


class PlaybookVersionIn(ApiModel):
    spec: dict[str, Any] | None = None
    code: str | None = Field(default=None, max_length=200_000)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    canary: dict[str, Any] | None = None
    activate: bool = False


class PlaybookOut(ApiModel):
    id: str
    university_id: str | None
    owner_id: str | None
    title: str
    language: str
    current_version: int
    health: dict[str, Any]
    provenance: dict[str, Any]
    created_at: datetime


class PlaybookVersionOut(ApiModel):
    id: str
    playbook_id: str
    version: int
    spec: dict[str, Any] | None
    code: str | None
    tests: list[dict[str, Any]]
    canary: dict[str, Any] | None
    active: bool
    checksum: str | None
    created_by_run: str | None
    created_at: datetime


class ArtifactOut(ApiModel):
    id: str
    source_id: str
    kind: str
    url: str | None
    sha256: str
    content_type: str | None
    size: int | None
    metadata: dict[str, Any]
    captured_at: datetime
    run_id: str | None
    step: int | None


class SourceStateOut(ApiModel):
    source_id: str
    last_revision: int | None
    cursor: dict[str, Any]
    site_map: dict[str, Any]
    endpoints: list[dict[str, Any]]
    updated_at: datetime


class PriorityIn(ApiModel):
    source_ids: list[str] = Field(default_factory=list)


class PriorityOut(ApiModel):
    space_id: str
    kind: str
    source_ids: list[str]


# --- run reports as the REST layer serialises them (camelCase, like every other API model) --------


class RunErrorOut(ApiModel):
    code: str
    detail: str
    read: str | None
    retryable: bool


class ReadOutcomeOut(ApiModel):
    name: str
    url: str | None
    format: str
    ok: bool
    status: int | None
    items_seen: int
    items_valid: int
    items_invalid: int
    artifact_id: str | None
    error: RunErrorOut | None


class NormalizedRecordOut(ApiModel):
    kind: str
    external_id: str
    fields: dict[str, Any]
    source_url: str | None
    open_url: str | None


class RunReportOut(ApiModel):
    source_id: str
    run: str
    version: int | None
    ok: bool
    source_status: str | None
    started_at: datetime
    finished_at: datetime
    reads: list[ReadOutcomeOut]
    counts: dict[str, int]
    records: list[NormalizedRecordOut]
    records_truncated: bool
    errors: list[RunErrorOut]
    artifact_ids: list[str]
    cursor: dict[str, Any] | None
    notes: list[str]
    persisted: bool
