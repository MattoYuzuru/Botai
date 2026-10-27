"""Actors and probes of the full-stack test: the student at the PWA and the places secrets leak to."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.contracts.agent_ui import ASKING_TYPES
from botai.core.llm import ScriptedLlm
from botai.db.base import Base
from tests.fakeuni import data
from tests.jobs.test_sse import Stream, listen

Sm = async_sessionmaker[AsyncSession]
Block = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Asked:
    """One question the student was shown, as the PWA gets it."""

    type: str
    kind: str | None  # input blocks: the `inputKind`
    secret: bool
    question: str

    @property
    def label(self) -> str:
        what = f"{self.type}:{self.kind}" if self.kind else self.type
        return f"{what}{' (secret)' if self.secret else ''}"


@dataclass(slots=True)
class Attendance:
    """What a job showed the student while they were looking at it."""

    job_id: str
    events: list[dict[str, Any]]
    asked: list[Asked] = field(default_factory=lambda: list[Asked]())

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def blocks(self, kind: str | None = None) -> list[Block]:
        found = [e["data"] for e in self.events if e["event"] == "block"]
        return [b for b in found if kind is None or b["type"] == kind]

    def states(self) -> list[str]:
        return [e["data"]["state"] for e in self.events if e["event"] == "status"]

    @property
    def result(self) -> dict[str, Any]:
        done = [e for e in self.events if e["event"] == "done"]
        assert done, f"job {self.job_id} did not finish: {self.names()}"
        return done[-1]["data"]["result"]


class Student:
    """The person at the PWA: follows the SSE stream of a job and answers what it asks.

    Fields are answered by what they are, like a person would: the login is the one with
    `autocomplete=username`, the password the `password` input, the SMS code the `code` input, and
    the final question "is this right?" gets a yes. Anything else is a test failure, not a guess.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        headers: dict[str, str],
        *,
        username: str = data.USERNAME,
        password: str = data.PASSWORD,
        otp: str = data.OTP,
        confirm: bool = True,
    ) -> None:
        self._http = http
        self._headers = headers
        self._username, self._password, self._otp, self._confirm = username, password, otp, confirm
        self.history: list[Asked] = []

    def _reply(self, block: Block) -> str | bool:
        kind = block.get("inputKind")
        if block["type"] == "input" and block["secret"]:
            if kind == "password":
                return self._password
            if kind == "code":
                return self._otp
        elif block["type"] == "input" and block.get("autocomplete") == "username":
            return self._username
        elif block["type"] == "confirm":
            return self._confirm
        raise AssertionError(f"the student does not know what to answer to: {block}")

    async def attend(
        self,
        job_id: str,
        *,
        within: float = 180.0,
        on_question: Callable[[Block], Awaitable[None]] | None = None,
    ) -> Attendance:
        """Watch a job until it ends, answering every question as it appears. `on_question` is
        called with each question before it is answered: the moment the PWA would show it."""
        attendance = Attendance(job_id, [])
        answered: set[str] = set()
        async with listen(self._http, f"/api/v1/jobs/{job_id}/events", self._headers) as stream:
            attendance.events = stream.events
            async with asyncio.timeout(within):
                while not self._ended(stream):
                    for event in list(stream.events):
                        block = event["data"]
                        if (
                            event["event"] == "block"
                            and block["type"] in ASKING_TYPES
                            and block["id"] not in answered
                        ):
                            answered.add(block["id"])
                            if on_question is not None:
                                await on_question(block)
                            await self._answer(job_id, block, attendance)
                    await asyncio.sleep(0.02)
            await stream.finished()
        return attendance

    @staticmethod
    def _ended(stream: Stream) -> bool:
        return any(e["event"] in ("done", "error") for e in stream.events)

    async def _answer(self, job_id: str, block: Block, attendance: Attendance) -> None:
        asked = Asked(
            block["type"], block.get("inputKind"), bool(block.get("secret")), block["question"]
        )
        attendance.asked.append(asked)
        self.history.append(asked)
        response = await self._http.post(
            f"/api/v1/jobs/{job_id}/answers",
            headers=self._headers,
            json={"blockId": block["id"], "values": self._reply(block)},
        )
        assert response.status_code == 204, response.text


class WireTap(logging.Handler):
    """Every log record of the process, formatted the way a log file would hold it (tracebacks
    included). It sees the records before the redacting filter of a production handler does."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def dump_database(sm: Sm) -> str:
    """Every row of every table as text: what a database dump would hold."""
    parts: list[str] = []
    async with sm() as session:
        for table in Base.metadata.sorted_tables:
            rows = (await session.execute(text(f'select * from "{table.name}"'))).all()  # noqa: S608
            parts.extend(f"{table.name}: {tuple(row)!r}" for row in rows)
    return "\n".join(parts)


def sent_to_models(*models: ScriptedLlm) -> str:
    """Everything that was sent to a language model, as one string."""
    return json.dumps(
        [r.model_dump(mode="json") for llm in models for r in llm.requests], ensure_ascii=False
    )


def leaks(haystack: str, needles: dict[str, str]) -> list[str]:
    """Names of the needles found in the haystack."""
    return [name for name, needle in needles.items() if needle in haystack]
