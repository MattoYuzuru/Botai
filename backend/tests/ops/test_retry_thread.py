"""Operator retry CLI on fakes: no provider, no database, no Postgres (L0)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.ops import retry_thread

NOW = datetime(2026, 11, 3, 9, 40, tzinfo=UTC)
THREAD_ID = "0001KJEH7G8QDYGYYXT2K3ZFWQ"
OWNER_ID = "0001KJEH7HCBZGXZT856BQ1EFW"
MESSAGE_ID = "0001KJEH7J1SE75FN0NVM6JBJS"
JOB_ID = "0001KJEH7KZS1J8VN1H7FV0D3E"


class FakeSession:
    """Returns scripted rows from `scalar()`, in query order: thread, owner, message, job."""

    def __init__(self, *rows: object) -> None:
        self.rows = list(rows)
        self.queries: list[object] = []

    async def scalar(self, query: object) -> object:
        self.queries.append(query)
        return self.rows.pop(0) if self.rows else None


class FakeDb:
    """`services.db()` stand-in: one async context manager over the fake session."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeDb:
        return self

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc: object) -> None:
        return None


def thread_row() -> Any:
    return SimpleNamespace(id=THREAD_ID, user_id=OWNER_ID)


def owner_row() -> Any:
    return SimpleNamespace(id=OWNER_ID, display_name="Студент")


def message_row() -> Any:
    return SimpleNamespace(role="agent", created_at=NOW)


def job_row() -> Any:
    return SimpleNamespace(id=JOB_ID, kind="chat", state="failed")


RUN_ID = "0001KJEH7M3XQ4P9R2T5V8W0YZ"


def checkpoint_row() -> Any:
    return SimpleNamespace(
        run_id=RUN_ID,
        status="cost_limit",
        segment=2,
        message_count=41,
        bytes=123_456,
        updated_at=NOW,
        expires_at=NOW,
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    session: FakeSession,
    post: Callable[..., Awaitable[Any]] | None = None,
    checkpoint: Any = None,
) -> None:
    """The production entry points the CLI uses, replaced with fakes (no checkpoint by default)."""
    services = SimpleNamespace(db=FakeDb(session))
    monkeypatch.setattr(retry_thread, "build_services", lambda: services)
    if post is not None:
        monkeypatch.setattr(retry_thread, "post_message", post)

    async def resumable(_services: object, _thread: object) -> Any:
        return checkpoint

    monkeypatch.setattr(retry_thread, "resumable_checkpoint", resumable)


def ids_post(calls: list[dict[str, Any]]) -> Callable[..., Awaitable[Any]]:
    async def post(
        session: object, services: object, user_id: str, thread_id: str, body: Any
    ) -> Any:
        calls.append({"user_id": user_id, "thread_id": thread_id, "body": body})
        return SimpleNamespace(message=SimpleNamespace(id=MESSAGE_ID), job_id=JOB_ID)

    return post


async def test_normal_post_prints_ids_and_uses_the_thread_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    install(monkeypatch, FakeSession(thread_row(), owner_row()), ids_post(calls))
    out: list[str] = []
    err: list[str] = []
    rc = await retry_thread.run(["--thread", THREAD_ID], out=out.append, err=err.append)
    assert rc == 0
    assert err == []
    assert [(c["user_id"], c["thread_id"]) for c in calls] == [(OWNER_ID, THREAD_ID)]
    assert calls[0]["body"].text == retry_thread.DEFAULT_TEXT
    text = "\n".join(out)
    assert THREAD_ID in text
    assert MESSAGE_ID in text
    assert JOB_ID in text
    assert "sk-" not in text  # identifiers and states only, no provider key material


async def test_explicit_text_is_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    install(monkeypatch, FakeSession(thread_row(), owner_row()), ids_post(calls))
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--text", "Продолжай с расписанием"],
        out=lambda _line: None,
        err=lambda _line: None,
    )
    assert rc == 0
    assert calls[0]["body"].text == "Продолжай с расписанием"


async def test_agent_busy_exits_with_two_and_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def busy_post(*_args: object, **_kwargs: object) -> Any:
        raise BotaiError(ErrorCode.AGENT_BUSY, "Ассистент ещё отвечает на прошлое сообщение")

    install(monkeypatch, FakeSession(thread_row(), owner_row()), busy_post)
    out: list[str] = []
    err: list[str] = []
    rc = await retry_thread.run(["--thread", THREAD_ID], out=out.append, err=err.append)
    assert rc == 2
    assert out == []
    message = "\n".join(err)
    assert "Ассистент ещё отвечает" in message
    assert "Дождитесь ответа" in message


async def test_a_missing_thread_exits_with_one_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_post(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("a missing thread must not be posted to")

    install(monkeypatch, FakeSession(None), must_not_post)
    err: list[str] = []
    rc = await retry_thread.run(["--thread", THREAD_ID], out=lambda _line: None, err=err.append)
    assert rc == 1
    assert "Чат не найден" in "\n".join(err)


async def test_dry_run_reports_the_state_and_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_post(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("--dry-run must not post")

    session = FakeSession(thread_row(), owner_row(), message_row(), job_row())
    install(monkeypatch, session, must_not_post)
    out: list[str] = []
    err: list[str] = []
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--dry-run"], out=out.append, err=err.append
    )
    assert rc == 0
    assert err == []
    text = "\n".join(out)
    assert THREAD_ID in text
    assert OWNER_ID in text
    assert "Студент" in text
    assert "agent" in text
    assert "2026-11-03T09:40:00+00:00" in text
    assert JOB_ID in text
    assert "failed" in text
    assert "ничего не отправлено" in text
    # thread + owner + last message + last job, and no write path at all
    assert len(session.queries) == 4


async def test_dry_run_without_messages_or_jobs_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeSession(thread_row(), owner_row(), None, None))
    out: list[str] = []
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--dry-run"], out=out.append, err=lambda _line: None
    )
    assert rc == 0
    text = "\n".join(out)
    assert "Последнее сообщение: нет" in text
    assert "Последнее задание: нет" in text
    assert "Чекпоинт для продолжения: нет" in text


@pytest.mark.parametrize("thread", ["not-a-ulid", "0001KJEH7G8QDYGYYXT2K3ZFW", ""])
async def test_invalid_thread_id_is_rejected_before_services(
    monkeypatch: pytest.MonkeyPatch, thread: str
) -> None:
    def no_services() -> Any:
        raise AssertionError("invalid arguments must not build services")

    monkeypatch.setattr(retry_thread, "build_services", no_services)
    with pytest.raises(SystemExit) as exc:
        await retry_thread.run(["--thread", thread])
    assert exc.value.code == 2


async def test_lower_case_thread_id_is_accepted_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    install(monkeypatch, FakeSession(thread_row(), owner_row()), ids_post(calls))
    rc = await retry_thread.run(
        ["--thread", THREAD_ID.lower()], out=lambda _line: None, err=lambda _line: None
    )
    assert rc == 0
    assert calls[0]["thread_id"] == THREAD_ID


async def test_too_long_text_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_post(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("an invalid message must not be posted")

    install(monkeypatch, FakeSession(thread_row(), owner_row()), must_not_post)
    err: list[str] = []
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--text", "x" * 8001], out=lambda _line: None, err=err.append
    )
    assert rc == 1
    assert "текст сообщения не прошёл проверку" in "\n".join(err)


def test_main_is_wired_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m botai.ops.retry_thread` calls `run` with the command line and exits with its code."""
    calls: list[Sequence[str]] = []

    async def fake_run(argv: Sequence[str], **_kwargs: object) -> int:
        calls.append(argv)
        return 3

    monkeypatch.setattr(retry_thread, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["retry_thread", "--thread", THREAD_ID])
    with pytest.raises(SystemExit) as exc:
        retry_thread.main()
    assert exc.value.code == 3
    assert calls == [["--thread", THREAD_ID]]


async def test_a_fresh_checkpoint_is_resumed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    install(
        monkeypatch,
        FakeSession(thread_row(), owner_row()),
        ids_post(calls),
        checkpoint=checkpoint_row(),
    )
    out: list[str] = []
    rc = await retry_thread.run(["--thread", THREAD_ID], out=out.append, err=lambda _line: None)
    assert rc == 0
    assert calls[0]["body"].resume_run_id == RUN_ID
    assert f"Продолжение от чекпоинта: {RUN_ID}" in "\n".join(out)


async def test_without_a_checkpoint_the_message_is_a_plain_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    install(monkeypatch, FakeSession(thread_row(), owner_row()), ids_post(calls))
    rc = await retry_thread.run(
        ["--thread", THREAD_ID], out=lambda _line: None, err=lambda _line: None
    )
    assert rc == 0
    assert calls[0]["body"].resume_run_id is None


async def test_resume_flag_without_a_checkpoint_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_post(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("nothing to resume: nothing may be posted")

    install(monkeypatch, FakeSession(thread_row(), owner_row()), must_not_post)
    err: list[str] = []
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--resume"], out=lambda _line: None, err=err.append
    )
    assert rc == 1
    assert "нечего продолжать" in "\n".join(err)


async def test_dry_run_shows_the_checkpoint_without_message_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(thread_row(), owner_row(), message_row(), job_row())
    install(monkeypatch, session, checkpoint=checkpoint_row())
    out: list[str] = []
    rc = await retry_thread.run(
        ["--thread", THREAD_ID, "--dry-run"], out=out.append, err=lambda _line: None
    )
    assert rc == 0
    text = "\n".join(out)
    assert RUN_ID in text
    assert "cost_limit" in text
    assert "часть 3" in text
    assert "сообщений 41" in text
