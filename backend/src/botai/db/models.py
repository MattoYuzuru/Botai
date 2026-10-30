"""All tables (docs/architecture/lld.md §3). Owner after stage 0: W1. New column = PR + migration.

Every table holding user data has `user_id`; reads go through repositories that take `user_id`
first (tenant predicate, NFR-001). Domain tables share provenance columns and
UNIQUE(connection_id, external_id) for idempotent upserts.
"""

from __future__ import annotations

from datetime import datetime
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

from botai.db.base import Base, pk_ulid, ts_now

FK_USER = "users.id"
FK_CONN = "connections.id"


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
    connection_id: Mapped[str] = mapped_column(
        String(26), ForeignKey(FK_CONN, ondelete="CASCADE"), index=True, nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)  # where it was read (may be API)
    open_url: Mapped[str | None] = mapped_column(Text)  # the page that shows it to the student
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spec_revision: Mapped[int | None] = mapped_column(Integer)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # last sync that saw it


def _uq(table: str) -> tuple[UniqueConstraint]:
    return (UniqueConstraint("connection_id", "external_id", name=f"uq_{table}_conn_ext"),)


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
