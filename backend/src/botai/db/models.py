"""All tables (docs/architecture/lld.md §3). Owner after stage 0: W1. New column = PR + migration.

Every table holding user data has `user_id`; reads go through repositories that take `user_id`
first (tenant predicate, NFR-001). Domain tables share provenance columns and
UNIQUE(connection_id, external_id) for idempotent upserts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.engine import Connection as SqlConnection
from sqlalchemy.orm import Mapped, Mapper, mapped_column
from sqlalchemy.types import UserDefinedType

from botai.db.base import Base, pk_ulid, ts_now

FK_USER = "users.id"
FK_CONN = "connections.id"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _user_fk() -> Mapped[str]:
    return mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="CASCADE"), index=True, nullable=False
    )


# --- identity -----------------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk_ulid()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # argon2, BotAI login
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = ts_now()


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = pk_ulid()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # sha256 hex
    created_by: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="CASCADE"), nullable=False
    )
    used_by: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = ts_now()


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = ts_now()


# --- universities, spaces, connections ----------------------------------------------------------


class University(Base):
    """Learned registry. Nothing is hardcoded: rows appear when someone connects a university."""

    __tablename__ = "universities"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # slug
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    short_title: Mapped[str] = mapped_column(String(40), nullable=False)
    origins: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    accent_hue: Mapped[float] = mapped_column(Float, nullable=False)  # OKLCH hue in 235..345
    accent_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    third_party_warning: Mapped[str | None] = mapped_column(
        Text
    )  # Q12: show a warning, connect anyway
    created_at: Mapped[datetime] = ts_now()


class Space(Base):
    """One university of one user (`university_id` set), or the single "My materials" space of a
    user for documents outside any university (`university_id` NULL)."""

    __tablename__ = "spaces"
    __table_args__ = (
        Index(
            "uq_spaces_user_id_university_id",
            "user_id",
            "university_id",
            unique=True,
            postgresql_where=text("university_id IS NOT NULL"),
        ),
        Index(
            "uq_spaces_user_id_materials",
            "user_id",
            unique=True,
            postgresql_where=text("university_id IS NULL"),
        ),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    university_id: Mapped[str | None] = mapped_column(String(50), ForeignKey("universities.id"))
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    accent_hue: Mapped[float] = mapped_column(Float, nullable=False, default=262.2)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Moscow")
    profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )  # name, programme, year
    # The source whose schedule decides when several sources of this university show the same
    # class (FR-054). NULL: nothing chosen, the oldest live source with schedule events decides
    # (`domain.primary`). A deleted source clears the choice; a disabled one is passed over at read
    # time and counts again once switched back on. `use_alter`: connections point back at spaces.
    primary_schedule_connection_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey(
            "connections.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_spaces_primary_schedule_connection_id_connections",
        ),
    )
    created_at: Mapped[datetime] = ts_now()


class SourceSitemap(Base):
    """Non-secret map of one origin (navigation, routes, FAQ/search endpoints), shared like a recipe."""

    __tablename__ = "source_sitemaps"
    origin: Mapped[str] = mapped_column(String(255), primary_key=True)
    map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = ts_now()


class AdapterSpecRow(Base):
    __tablename__ = "adapter_specs"
    __table_args__ = (UniqueConstraint("origin", "adapter", "revision"),)
    id: Mapped[str] = pk_ulid()
    origin: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    adapter: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # recipe | moodle_ws | ical | document
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # draft | canary | active | deprecated | rolled_back
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    learned_by_digest: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_now()


class LoginPresetRow(Base):
    __tablename__ = "login_presets"
    __table_args__ = (UniqueConstraint("origin", "revision"),)
    id: Mapped[str] = pk_ulid()
    origin: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    preset: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = ts_now()


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    space_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    origin: Mapped[str] = mapped_column(String(255), nullable=False)
    # portal | lms | schedule | site | sheet | doc | document | calendar | other
    # (`spaces.sources.CONNECTION_KINDS`): sheet = a table, doc = a document or page behind a
    # link, document = an uploaded file
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(120), nullable=False)  # human, never a bare host
    # The UI page of the source that holds the data (no secret query), for "open in the portal".
    entry_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    spec_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("adapter_specs.id", ondelete="SET NULL")
    )
    spec_revision: Mapped[int | None] = mapped_column(Integer)
    login_preset_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("login_presets.id", ondelete="SET NULL")
    )
    allow_hosts: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    # None keeps legacy/automatic discovery; an explicit list limits this connection's reads.
    data_scope: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_detail: Mapped[str | None] = mapped_column(Text)
    error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The status the source had when the student switched it off; switching it on restores it
    # (a source that needed a login still needs one; a calendar waiting for its link still waits).
    disabled_from: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = ts_now()


class SecretRow(Base):
    """Ciphertext only (security.md §3). Plaintext never exists in the database."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("user_id", "connection_id", "kind"),)
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    connection_id: Mapped[str] = mapped_column(
        String(26), nullable=False, index=True
    )  # job id for transient
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    kek_id: Mapped[str] = mapped_column(String(20), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    aad_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_now()
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- domain (shared provenance columns) ---------------------------------------------------------


class _Domain:
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="CASCADE"), index=True, nullable=False
    )
    # Provenance of the v3 reading path: a domain row belongs to a connection. A v4 source has no
    # connection, so its rows leave this NULL and fill `source_id` instead. Both columns are
    # nullable on the way from v3 to v4 (docs/v4/03-sources-and-ux §6.1): old rows are untouched,
    # and no row loses the provenance it was written with.
    connection_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE"), index=True
    )
    # Provenance of the v4 reading path: a row read by one `sources` record. Deleting the source
    # keeps the data (SET NULL). `connection_id IS NULL AND source_id IS NULL` must not happen;
    # a row with both set is a bridge row of the v3→v4 data migration.
    source_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)  # where it was read (may be API)
    open_url: Mapped[str | None] = mapped_column(Text)  # the page that shows it to the student
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spec_revision: Mapped[int | None] = mapped_column(Integer)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # last sync that saw it


def _uq(table: str) -> tuple[UniqueConstraint, Index]:
    """Natural keys of the two provenance paths: one row per (v3 link | v4 source) × external id.

    The v3 constraint stays as it was; v4 rows are unique per source by a partial index (a plain
    UNIQUE over a nullable column would not enforce NULL rows)."""
    return (
        UniqueConstraint("connection_id", "external_id", name=f"uq_{table}_conn_ext"),
        Index(
            f"uq_{table}_source_ext",
            "source_id",
            "external_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
    )


class Course(_Domain, Base):
    __tablename__ = "courses"
    __table_args__ = _uq("courses")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60))
    term: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ScheduleEvent(_Domain, Base):
    __tablename__ = "schedule_events"
    __table_args__ = (
        *_uq("schedule_events"),
        Index("ix_schedule_events_user_start", "user_id", "starts_at"),
        # One source's classes in a window (the read of a schedule, the reach of a source, FR-054).
        Index("ix_schedule_events_conn_start", "connection_id", "starts_at"),
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    course_external_id: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    location_text: Mapped[str | None] = mapped_column(String(200))
    teacher_text: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text)
    kind_text: Mapped[str | None] = mapped_column(String(120))  # the source's own class type
    address: Mapped[str | None] = mapped_column(String(300))
    group_text: Mapped[str | None] = mapped_column(String(200))
    lesson_number: Mapped[int | None] = mapped_column(Integer)
    links: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )  # [{url, label}], online rooms and materials
    attributes: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )  # [{label, value}], extra values under general labels
    changed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Assignment(_Domain, Base):
    __tablename__ = "assignments"
    __table_args__ = _uq("assignments")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    course_external_id: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    max_score: Mapped[float | None] = mapped_column(Float)


class Deadline(_Domain, Base):
    __tablename__ = "deadlines"
    __table_args__ = (*_uq("deadlines"), Index("ix_deadlines_user_due", "user_id", "due_at"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    course_external_id: Mapped[str | None] = mapped_column(String(200))
    assignment_external_id: Mapped[str | None] = mapped_column(String(200))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="unknown")
    url: Mapped[str | None] = mapped_column(Text)


class Grade(_Domain, Base):
    __tablename__ = "grades"
    __table_args__ = _uq("grades")
    course_external_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(300))
    value: Mapped[str] = mapped_column(String(60), nullable=False)
    scale: Mapped[str | None] = mapped_column(String(60))
    weight: Mapped[float | None] = mapped_column(Float)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Person(_Domain, Base):
    __tablename__ = "people"
    __table_args__ = _uq("people")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    email: Mapped[str | None] = mapped_column(String(320))
    url: Mapped[str | None] = mapped_column(Text)


class Location(_Domain, Base):
    __tablename__ = "locations"
    __table_args__ = _uq("locations")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    building: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))


class NewsItem(_Domain, Base):
    __tablename__ = "news_items"
    __table_args__ = _uq("news_items")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)


# --- documents and retrieval (ADR-0006) ---------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    space_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connection_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # file | sheet | page
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(120))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = ts_now()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),)
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    document_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('russian', text)", persisted=True)
    )


class Attachment(Base):
    """A file of a chat (or of the "add a document" form): the bytes live here, owned by one user.

    Created unbound by `POST /attachments`; sending a message binds it (`message_id`). A document
    attachment is indexed by an INGEST job that reads these bytes (`document_id` appears once the
    text is searchable, `ingest_state` says how far it got).
    """

    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_user_id_thread_id", "user_id", "thread_id"),)
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    thread_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("chat_threads.id", ondelete="CASCADE")
    )
    message_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("chat_messages.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mime: Mapped[str] = mapped_column(String(120), nullable=False)  # detected from the bytes
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # image|document|audio|other
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("documents.id", ondelete="SET NULL")
    )
    ingest_state: Mapped[str] = mapped_column(
        String(10), nullable=False, default="none", server_default="none"
    )  # none | queued | indexed | failed
    created_at: Mapped[datetime] = ts_now()


class Fact(Base):
    __tablename__ = "facts"
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    space_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stale_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[str] = mapped_column(String(12), nullable=False)


# --- jobs ---------------------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_pick", "state", "run_after"),
        Index("ix_jobs_thread", "thread_id"),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    state: Mapped[str] = mapped_column(String(14), nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(String(120))
    connection_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("chat_threads.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobResearchCheckpoint(Base):
    """Private encrypted research state. No plaintext payload in Job, events or diagnostics."""

    __tablename__ = "job_research_checkpoints"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("format_version > 0", name="format_version_positive"),
        CheckConstraint("intent_key ~ '^[0-9a-f]{64}$'", name="intent_key_hex"),
        Index("ix_job_research_checkpoints_expires_at", "expires_at"),
    )
    job_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = _user_fk()
    connection_id: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE"), index=True, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kek_id: Mapped[str] = mapped_column(String(20), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    aad_version: Mapped[int] = mapped_column(Integer, nullable=False)


class JobEvent(Base):
    """SSE stream of a job. Never contains secrets, page contents or prompts."""

    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "seq"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # status | block | block_update | error | done
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = ts_now()


class JobAnswer(Base):
    """Non-secret answers. Secret answers go to `secrets` (kind=transient, connection_id=job id)."""

    __tablename__ = "job_answers"
    __table_args__ = (UniqueConstraint("job_id", "block_id"),)
    id: Mapped[str] = pk_ulid()
    job_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(String(26), nullable=False)
    values: Mapped[Any] = mapped_column(JSONB)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = ts_now()


# --- chat, actions, usage -----------------------------------------------------------------------


class ChatThread(Base):
    """One conversation. It has no purpose: every thread talks to the same agent, and long tasks
    (connect, repair, sync, ingest) show their blocks in the thread they were asked in."""

    __tablename__ = "chat_threads"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_chat_threads_user_id_client_request_id"
        ),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    space_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="Новый чат")
    client_request_id: Mapped[str | None] = mapped_column(String(26))
    summary: Mapped[str | None] = mapped_column(
        Text
    )  # rolling summary of old turns (agent-harness §3)
    created_at: Mapped[datetime] = ts_now()
    # Bumped by an insert of any ChatMessage (see `_bump_thread` below); orders the thread list.
    last_message_at: Mapped[datetime] = ts_now()
    # When the student last opened the thread; an agent message newer than this is unread.
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Links the agent met while reading ({"public": [...], "source": [...]}, the last ones): the
    # only addresses besides a source's own that a live read may open.
    seen_links: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class AgentChoice(Base):
    """The buttons the application itself put under an answer of the agent before it changes a setting
    that the agent proposed (the primary source of a university, switching a source off): each button
    is bound to exact actions on exact connections. The action runs only when the student's next
    message is exactly the label of a button, within a few minutes, once: the first call that sees the
    press arms the choice for that message (`armed`), and every action it binds is spent at most once.
    One pending choice per thread: a new question replaces the old one, so a press never reaches a
    question the student did not see last."""

    __tablename__ = "agent_choices"
    __table_args__ = (UniqueConstraint("thread_id"),)
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    thread_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False
    )
    # [{"label": str, "actions": [{"type": "primary" | "disable", "connectionId": str}]}]
    buttons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # After a press: {"messageId", "label", "actions": [what the button stood for], "remaining":
    # [actions not yet spent]}; None before. A pressed choice stays, spent or not, until the turn of
    # that message is over (the next call of any later turn removes it), so a repeated call in the
    # press turn finds the press instead of asking anew.
    armed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = ts_now()


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_id", "client_message_id", name="uq_chat_messages_thread_id_client_message_id"
        ),
        Index("ix_chat_messages_user_job", "user_id", "job_id"),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    thread_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chat_threads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # user | agent
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False
    )  # AgentUiBlock[] (by_alias)
    job_id: Mapped[str | None] = mapped_column(String(26))
    client_message_id: Mapped[str | None] = mapped_column(String(26))
    created_at: Mapped[datetime] = ts_now()


@event.listens_for(ChatMessage, "after_insert")
def _bump_thread(
    _mapper: Mapper[ChatMessage], connection: SqlConnection, message: ChatMessage
) -> None:
    """A message in a thread moves `last_message_at` forward, whoever inserted it. A progress line
    of a task ("thinking") is not a message: it must not reorder the thread list."""
    if message.blocks and message.blocks[0].get("type") == "thinking":
        return
    at = message.__dict__.get("created_at")
    connection.execute(
        update(ChatThread)
        .where(ChatThread.id == message.thread_id)
        .values(last_message_at=func.greatest(ChatThread.last_message_at, at or func.now()))
    )


class UserMemory(Base):
    """What the agent remembers about a student (group, programme, preferences, useful steps)."""

    __tablename__ = "user_memory"
    __table_args__ = (
        Index("ix_user_memory_user_created", "user_id", "created_at"),
        # the same note is kept once, whatever its case
        Index("uq_user_memory_user_id_note", "user_id", func.lower(text("note")), unique=True),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = ts_now()


class ActionProposalRow(Base):
    __tablename__ = "action_proposals"
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    connection_id: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE"), nullable=False
    )
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fields_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = ts_now()


class LlmUsageRow(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_user_day", "user_id", "created_at"),)
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    job_id: Mapped[str | None] = mapped_column(String(26))
    purpose: Mapped[str] = mapped_column(String(12), nullable=False)
    model: Mapped[str] = mapped_column(String(60), nullable=False)
    input_cache_miss: Mapped[int] = mapped_column(Integer, nullable=False)
    input_cache_hit: Mapped[int] = mapped_column(Integer, nullable=False)
    output: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = ts_now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # action_executed | injection_blocked | ...
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = ts_now()


# --- sources v4: the live record an agent keeps about one source ---------------------------------
#
# Additive to v3: `connections` keep working; nothing is renamed or migrated here. A v4 source is
# identified inside its space by `identity_key` — a normalized address, `link:<sha256>` of a private
# calendar link, `file:<sha256>` of an uploaded file or `mail:<box>` — never by origin, so a second
# cabinet, table or calendar of one university is a second row. Playbooks are versioned knowledge:
# a declarative spec and/or parser code, activated by the service and never executed here.
# See docs/v4/03-sources-and-ux.md §1-2 and docs/v4/14-sources-backend.md.

SOURCE_STATUSES = (
    "draft",
    "probing",
    "learning",
    "waiting_user",
    "active",
    "syncing",
    "stale",
    "needs_reauth",
    "needs_link",
    "error",
    "disabled",
    "archived",
)
SOURCE_TRUST_LEVELS = ("high", "normal", "low")
SOURCE_NATURE_KINDS = (
    "site",
    "portal",
    "lms",
    "api",
    "calendar",
    "sheet",
    "doc",
    "file",
    "mail",
    "feed",
    "other",
)
SOURCE_DATA_KINDS = (
    "schedule",
    "deadlines",
    "assignments",
    "courses",
    "grades",
    "people",
    "locations",
    "news",
    "documents",
    "messages",
)
PLAYBOOK_LANGUAGES = ("declarative", "python", "js")
ARTIFACT_KINDS = ("page", "json", "file", "har", "screenshot", "log", "doc", "other")


def _in_list(values: tuple[str, ...]) -> str:
    """SQL for a CHECK constraint: the vocabulary is repeated in the migration, so one place wins."""
    return ", ".join(f"'{value}'" for value in values)


class Playbook(Base):
    """How to read a source: a declarative spec and/or code, with versions and health.

    `owner_id` NULL means the playbook is shared (D-018); otherwise only its owner sees it.
    Never executed in this process: the runner takes a version into the sandbox.
    """

    __tablename__ = "playbooks"
    __table_args__ = (
        Index("ix_playbooks_university_id", "university_id"),
        Index("ix_playbooks_owner_id", "owner_id"),
        CheckConstraint(f"language IN ({_in_list(PLAYBOOK_LANGUAGES)})", name="language_valid"),
    )
    id: Mapped[str] = pk_ulid()
    university_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("universities.id", ondelete="SET NULL")
    )
    owner_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey(FK_USER, ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str] = mapped_column(
        String(20), nullable=False, default="declarative", server_default="declarative"
    )
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    health: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = ts_now()


class PlaybookVersion(Base):
    """One revision of a playbook: spec JSON and/or code, tests, canary, checksum.

    Exactly one active version per playbook (`uq_playbook_versions_active`); the prior active one
    stays for rollback. `created_by_run` points at the job that wrote it, when a job did.
    """

    __tablename__ = "playbook_versions"
    __table_args__ = (
        UniqueConstraint("playbook_id", "version"),
        Index(
            "uq_playbook_versions_active",
            "playbook_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        CheckConstraint("spec IS NOT NULL OR code IS NOT NULL", name="payload_present"),
    )
    id: Mapped[str] = pk_ulid()
    playbook_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    code: Mapped[str | None] = mapped_column(Text)
    tests: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    canary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_by_run: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = ts_now()


class Source(Base):
    """A source of one university: where data lives, how it is read and how healthy it is."""

    __tablename__ = "sources"
    __table_args__ = (
        # The only uniqueness: one live record per normalized identity inside a space. A partial
        # index keeps the intent explicit (rows always have an identity; a future NULL would not
        # collide).
        Index(
            "uq_sources_space_identity_key",
            "space_id",
            "identity_key",
            unique=True,
            postgresql_where=text("identity_key IS NOT NULL"),
        ),
        Index("ix_sources_space_id_status", "space_id", "status"),
        Index("ix_sources_playbook_id", "playbook_id"),
        CheckConstraint(f"status IN ({_in_list(SOURCE_STATUSES)})", name="status_valid"),
        CheckConstraint(f"trust IN ({_in_list(SOURCE_TRUST_LEVELS)})", name="trust_valid"),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    space_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    # Kept as a convenience join key: a space names the university; v3 keeps "materials" spaces.
    university_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("universities.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    entry_url: Mapped[str | None] = mapped_column(Text)  # the UI page, secret query removed
    identity_key: Mapped[str] = mapped_column(String(400), nullable=False)
    kinds: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    playbook_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("playbooks.id", ondelete="SET NULL")
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    auth_ref: Mapped[str | None] = mapped_column(String(200))  # Vault handle name, never a secret
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    trust: Mapped[str] = mapped_column(
        String(10), nullable=False, default="normal", server_default="normal"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    disabled_from: Mapped[str | None] = mapped_column(String(20))  # status to restore on enable
    created_at: Mapped[datetime] = ts_now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
    )


class SourceArtifact(Base):
    """What a run read or saved: a page, JSON, file, HAR, screenshot, log or document.

    Bytes live outside the database under `storage_ref`; `sha256` is the content address used for
    deduplication and provenance.
    """

    __tablename__ = "source_artifacts"
    __table_args__ = (
        Index("ix_source_artifacts_source_id_captured_at", "source_id", "captured_at"),
        Index("ix_source_artifacts_sha256", "sha256"),
        CheckConstraint(f"kind IN ({_in_list(ARTIFACT_KINDS)})", name="kind_valid"),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    source_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int | None] = mapped_column(Integer)
    storage_ref: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    captured_at: Mapped[datetime] = ts_now()
    run_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    step: Mapped[int | None] = mapped_column(Integer)


class SourceState(Base):
    """The small mutable state of a source's reading: cursor, site map and found endpoints."""

    __tablename__ = "source_state"
    source_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    last_revision: Mapped[int | None] = mapped_column(Integer)
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    site_map: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    endpoints: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
    )


class SourcePriority(Base):
    """Order of sources for one data kind of one university: first alive source is the primary.

    A setting, not an architectural ceiling (03-sources-and-ux §4.1): any number of sources can be
    listed, the order only decides which one wins a conflict for that kind.
    """

    __tablename__ = "source_priorities"
    space_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    source_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
    )


# --- RAG (phase 2.5) ----------------------------------------------------------------------------

# Width of `embedding_vec` in a fresh schema. The migration takes it from `BOTAI_RAG_DIMENSION`
# (default 384, the local provider); this constant only lets the mapped column render DDL.
DEFAULT_VECTOR_DIMENSION = 384


class VectorType(UserDefinedType[list[float]]):
    """`pgvector`'s `vector(n)` as a SQLAlchemy type without the `pgvector` Python package.

    The repo forbids a new dependency for one column, so the type is declared by hand:

    * DDL renders `vector(<dimension>)` (the migration owns the real width);
    * a value is bound as pgvector's text form `[0.1,0.2,…]` — `asyncpg` sends an unknown-type
      parameter as text and PostgreSQL parses it through the `vector` input function;
    * a read comes back as that same text form and is parsed to `list[float]`.

    pgvector is optional: on a database without the extension the column simply does not exist, and
    the JSONB `embedding` stays the source of truth (search/pgvector.py detects the column once per
    process). Not generic on purpose — SQLAlchemy's `UserDefinedType` takes no type parameter.
    """

    cache_ok = True

    def __init__(self, dimension: int = DEFAULT_VECTOR_DIMENSION) -> None:
        self.dimension = dimension

    def get_col_spec(self, **_: object) -> str:
        return f"vector({self.dimension})"

    def bind_processor(self, dialect: object) -> Any:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(repr(float(item)) for item in value) + "]"

        return process

    def result_processor(self, dialect: object, coltype: object) -> Any:
        def process(value: str | bytes | list[float] | None) -> list[float] | None:
            if value is None or isinstance(value, list):
                return value
            text = value.decode() if isinstance(value, bytes) else value
            body = text.strip()
            if not (body.startswith("[") and body.endswith("]")):
                raise ValueError("unexpected pgvector value shape")
            inner = body[1:-1].strip()
            return [float(part) for part in inner.split(",")] if inner else []

        return process


class ChunkEmbedding(Base):
    """The vector of one chunk for one embedding model, plus the provenance of the indexed text.

    Portable storage is `embedding` JSONB: it is the source of truth and works on any Postgres. The
    pgvector column `embedding_vec` is a derived acceleration for the same value: a migration adds
    it with a fixed width and an HNSW cosine index, the indexer fills both representations, and
    search reads `embedding_vec` only when the extension, the column and the provider dimension all
    match (docs/v4/18-rag.md §3, search/pgvector.py). When they do not match, the JSONB path still
    answers. Vectors of different models coexist: the unique key is `(chunk_id, model)`, so
    re-embedding with another provider does not destroy the previous one.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model", name="uq_chunk_embeddings_chunk_model"),
        Index("ix_chunk_embeddings_user_id_model", "user_id", "model"),
        Index("ix_chunk_embeddings_source_id", "source_id"),
        Index("ix_chunk_embeddings_content_hash", "content_hash"),
        # Declared here so `alembic revision --autogenerate` does not propose dropping the index the
        # pgvector migration creates; the reflected `vector` type itself is not comparable.
        Index(
            "ix_chunk_embeddings_embedding_vec_hnsw",
            "embedding_vec",
            postgresql_using="hnsw",
            postgresql_ops={"embedding_vec": "vector_cosine_ops"},
        ),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    chunk_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("chunks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Set when the text came from a v4 source artifact; NULL for v3 documents and chat material.
    source_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("sources.id", ondelete="SET NULL")
    )
    artifact_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("source_artifacts.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    # sha256 of the embedded text: deduplication and the cache key, never a secret.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    # Derived, nullable, pgvector-only: NULL means "not written or different width"; the JSONB value
    # above is always present and remains the fallback.
    embedding_vec: Mapped[list[float] | None] = mapped_column(VectorType(), nullable=True)
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str | None] = mapped_column(String(20))  # artifact kind or document kind
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trust: Mapped[str | None] = mapped_column(String(10))  # source trust level at capture time
    created_at: Mapped[datetime] = ts_now()


# --- v4 agent runs and traces (task-16, docs/v4/15-runtime-wiring.md) ----------------------------


class AgentRun(Base):
    """One v4 agent runtime run: status and counters; steps are `agent_run_events` (D-014).

    Additive to the v3 tables: `job_id` is only a link for provenance and is nulled when the job
    row is purged, so the trace outlives the short lived queue row.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_user_started", "user_id", "started_at"),
        Index("ix_agent_runs_job", "job_id"),
    )
    run_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = _user_fk()
    job_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    task: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_now()


class AgentRunEvent(Base):
    """One step of a v4 agent trace: model call, tool call/result, limit, pause, finish (D-014).

    `payload` is bounded and already secret-free when written: tool handlers mask primary secrets
    (docs/v4/15-runtime-wiring.md §4). `seq` is the runtime's monotonic event number inside a run.
    """

    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_run_events_run_seq"),
        Index("ix_agent_run_events_run_seq", "run_id", "seq"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


# --- actions: the owner's autonomy policy and the append-only action journal (D-006, D-025, D-026) -
#
# Additive next to the v3 `action_proposals`/`audit_log` tables: the v3 path keeps working behind
# `agent_engine=v3`, while the v4 action tools and the API use these two tables. A policy row is
# the owner's explicit choice for one class of action in one scope; the journal row is one concrete
# proposal and everything that happened to it. `actions_log` is append-only: rows are updated
# through their lifecycle but never deleted (except with the user, by cascade).

ACTION_CLASSES = ("read", "write_soft", "write_destructive", "money", "auth")
"""What a write action does (docs/v4/02-architecture.md §7). Not whether it is allowed."""

ACTION_MODES = ("auto", "ask", "never")
"""`auto` — do it; `ask` — ask the owner once with details; `never` — refuse (D-006)."""

POLICY_SCOPE_KINDS = ("global", "site", "space", "source")
"""Policy scope from broadest to narrowest; the narrowest matching row wins (§7)."""

ACTION_DECISIONS = (
    "auto",  # policy auto: allowed without asking, not executed yet
    "asked",  # policy ask: waiting for the owner's decision
    "approved",  # the owner (or the API) approved; not executed yet
    "never",  # policy never: refused, no request was made
    "executing",  # claimed by one executor: a second attempt must not send again
    "executed",  # the request went out; there was a response
    "failed",  # the request was refused or the site rejected it
    "denied",  # the owner (or the API) denied
    "expired",  # the question expired without a decision
)
"""Lifecycle of one `actions_log` row. `auto`/`asked`/`never` mirror the resolved mode."""


class ActionPolicy(Base):
    """Owner autonomy policy: one row per (scope, action class) for one user (D-025).

    `scope_kind` is `global` (no `scope_id`), `site` (host, e.g. `lms.example.edu`), `space` or
    `source` (a ULID). The narrowest matching row wins; classes without a row use the documented
    defaults of `botai.actions.policy`. `limits` is small JSON (e.g. `{"max_per_day": 5}`): the
    engine escalates `auto` to `ask` when the counter is reached instead of silently refusing.
    """

    __tablename__ = "action_policies"
    __table_args__ = (
        CheckConstraint(f"scope_kind IN ({_in_list(POLICY_SCOPE_KINDS)})", name="scope_kind_valid"),
        CheckConstraint(f"action_class IN ({_in_list(ACTION_CLASSES)})", name="action_class_valid"),
        CheckConstraint(f"mode IN ({_in_list(ACTION_MODES)})", name="mode_valid"),
        # One row per class and scope. A NULL `scope_id` (global) is not compared by a plain
        # UNIQUE in Postgres, so global and scoped rows get separate partial indexes.
        Index(
            "uq_action_policies_global",
            "user_id",
            "action_class",
            unique=True,
            postgresql_where=text("scope_kind = 'global'"),
        ),
        Index(
            "uq_action_policies_scoped",
            "user_id",
            "scope_kind",
            "scope_id",
            "action_class",
            unique=True,
            postgresql_where=text("scope_kind <> 'global'"),
        ),
    )
    id: Mapped[str] = pk_ulid()
    user_id: Mapped[str] = _user_fk()
    scope_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(300))
    action_class: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(6), nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = ts_now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
    )


class ActionLog(Base):
    """One proposed action and its journal (D-026): what was shown, decided, sent and returned.

    `proposal` is the redacted request the owner sees ("the agent shows exactly what it sends"):
    secret-looking values are masked, but the method, URL and field names are exact. `request` is
    the executed request without its body — method, URL, kind, byte count and `body_sha256` only;
    primary secret values never reach this table. `idempotency_key` is unique per user: a retried
    proposal with the same key returns the stored row instead of sending twice.
    """

    __tablename__ = "actions_log"
    __table_args__ = (
        CheckConstraint(f"action_class IN ({_in_list(ACTION_CLASSES)})", name="action_class_valid"),
        CheckConstraint(f"mode IN ({_in_list(ACTION_MODES)})", name="mode_valid"),
        CheckConstraint(f"decision IN ({_in_list(ACTION_DECISIONS)})", name="decision_valid"),
        CheckConstraint(f"scope_kind IN ({_in_list(POLICY_SCOPE_KINDS)})", name="scope_kind_valid"),
        Index(
            "uq_actions_log_user_idempotency",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_actions_log_user_created", "user_id", "created_at"),
        Index("ix_actions_log_source", "source_id"),
        Index("ix_actions_log_run", "run_id"),
        Index("ix_actions_log_decision", "decision"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = _user_fk()
    run_id: Mapped[str | None] = mapped_column(String(26))
    job_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="SET NULL")
    )
    source_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("sources.id", ondelete="SET NULL")
    )
    scope_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, default="global", server_default="global"
    )
    scope_id: Mapped[str | None] = mapped_column(String(300))
    action_class: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(6), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="asked")
    intent: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    proposal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    rollback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = ts_now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=_utc_now,
        nullable=False,
    )
