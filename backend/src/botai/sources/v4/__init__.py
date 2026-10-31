"""Backend of v4 sources (phase 2.1/2.2): schema, service, identity, playbooks, artifacts.

No runner, no REST and no agent tools live here yet: this package is the storage layer that the
runner and the `source_*` tools will call. It is additive — v3 `connections` are untouched. See
docs/v4/14-sources-backend.md.
"""

from botai.sources.v4.artifacts import add_artifact, list_artifacts
from botai.sources.v4.identity import (
    calendar_key,
    digest_sha256,
    file_key,
    mail_key,
    normalize_url,
    site_key,
)
from botai.sources.v4.kinds import (
    ALL_KINDS,
    DATA_KINDS,
    NATURE_KINDS,
    SOURCE_STATUSES,
    normalize_kinds,
)
from botai.sources.v4.playbooks import (
    activate_playbook_version,
    active_playbook_version,
    add_playbook_version,
    create_playbook,
    get_playbook,
    list_playbook_versions,
    list_playbooks,
)
from botai.sources.v4.priorities import get_priority, primary_source, set_priority
from botai.sources.v4.schemas import SourcePatch
from botai.sources.v4.service import (
    create_source,
    disable_source,
    enable_source,
    get_source,
    list_sources,
    set_status,
    update_source,
)
from botai.sources.v4.state import get_source_state, upsert_source_state

__all__ = [
    "ALL_KINDS",
    "DATA_KINDS",
    "NATURE_KINDS",
    "SOURCE_STATUSES",
    "SourcePatch",
    "activate_playbook_version",
    "active_playbook_version",
    "add_artifact",
    "add_playbook_version",
    "calendar_key",
    "create_playbook",
    "create_source",
    "digest_sha256",
    "disable_source",
    "enable_source",
    "file_key",
    "get_playbook",
    "get_priority",
    "get_source",
    "get_source_state",
    "list_artifacts",
    "list_playbook_versions",
    "list_playbooks",
    "list_sources",
    "mail_key",
    "normalize_kinds",
    "normalize_url",
    "primary_source",
    "set_priority",
    "set_status",
    "site_key",
    "update_source",
    "upsert_source_state",
]
