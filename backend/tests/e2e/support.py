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

from botai.browser.gateway import PlaywrightGateway
from botai.contracts.agent_ui import ASKING_TYPES
from botai.core.llm import ScriptedLlm
from botai.db.base import Base
from tests.auth.support import PASSWORD, make_user
from tests.fakeuni import data
from tests.jobs.env import Env, session_header
from tests.jobs.test_sse import Stream, listen

Sm = async_sessionmaker[AsyncSession]
Block = dict[str, Any]
WRONG_PASSWORD = "Wr0ng-P@ss-FIXTURE"  # a typo of the student: must not leak either
PROXY_MASTER = "e2e-proxy-master-secret-0123456789abcdef"  # >= 32 characters, as the proxy demands


@dataclass(frozen=True, slots=True)
class ProxyProcess:
    """The EgressProxy running as its own process: where to find it, and what it logged."""

    url: str
    lines: list[str]


@dataclass(frozen=True, slots=True)
class Stack:
    """The environment of W1 plus the real browser and the proxy of this test."""

    env: Env
    gateway: PlaywrightGateway
    proxy: ProxyProcess
    proxy_mark: int  # how many lines the proxy had logged before this test

    def proxy_log(self) -> list[str]:
        """What the proxy decided since this test began, one line per tunnel."""
        return [line for line in self.proxy.lines[self.proxy_mark :] if "egress host=" in line]


@dataclass(frozen=True, slots=True)
class Learner:
    """A registered student who has started connecting a university."""

    client: httpx.AsyncClient  # the app on the student's phone (cookie jar included)
    user_id: str
    space_id: str
    connection_id: str
    connect_job: str

    def headers(self) -> dict[str, str]:
        return session_header(self.client)


class Campus:
    """The people around the app: the owner who invites, and the students who register."""

    def __init__(self, env: Env) -> None:
        self._env = env
        self._owner: httpx.AsyncClient | None = None

    async def _owner_client(self) -> httpx.AsyncClient:
        if self._owner is None:
            owner_id = await make_user(
                self._env.sm, "owner@example.com", is_owner=True, password=PASSWORD
            )
            self._env.users.append(owner_id)
            self._owner = self._env.new_client()
            login = await self._owner.post(
                "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
        return self._owner

    async def enrol(self, email: str, *, url: str = "https://fakeuni.test/") -> Learner:
        """Invitation, registration, a space for the university, a connection (a `connect` job)."""
        env = self._env
        owner = await self._owner_client()
        invite = (await owner.post("/api/v1/auth/invites")).json()["code"]
        client = env.new_client()
        registered = await client.post(
            "/api/v1/auth/register",
            json={
                "inviteCode": invite,
                "email": email,
                "displayName": email.split("@")[0].title(),
                "password": PASSWORD,
            },
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]
        env.users.append(user_id)

        space = await client.post("/api/v1/spaces", json={"title": "Фейкуни"})
        assert space.status_code == 201, space.text
        space_id = space.json()["id"]
        started = await client.post(
            f"/api/v1/spaces/{space_id}/connections", json={"kind": "portal", "url": url}
        )
        assert started.status_code == 201, started.text
        connection_id, job_id = started.json()["connection"]["id"], started.json()["jobId"]
        assert started.json()["connection"]["status"] == "draft"
        queued = await env.job(job_id)
        assert (queued.kind, queued.connection_id, queued.user_id) == (
            "connect",
            connection_id,
            user_id,
        )
        return Learner(client, user_id, space_id, connection_id, job_id)


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
    waiting_on: Block | None = None  # the question the student walked away from, if they did

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
        typos: int = 0,
    ) -> None:
        self._http = http
        self._headers = headers
        self._username, self._password, self._otp, self._confirm = username, password, otp, confirm
        self._typos = typos  # the password is mistyped this many times before it is right
        self.history: list[Asked] = []

    def _reply(self, block: Block) -> str | bool:
        kind = block.get("inputKind")
        if block["type"] == "input" and block["secret"]:
            if kind == "password":
                if self._typos > 0:
                    self._typos -= 1
                    return WRONG_PASSWORD
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
        walk_away_at: Callable[[Block], bool] | None = None,
    ) -> Attendance:
        """Watch a job until it ends, answering every question as it appears. `on_question` is
        called with each question before it is answered: the moment the PWA would show it.
        `walk_away_at` picks the question the student leaves unanswered (and stops watching)."""
        attendance = Attendance(job_id, [])
        answered: set[str] = set()
        async with listen(self._http, f"/api/v1/jobs/{job_id}/events", self._headers) as stream:
            attendance.events = stream.events
            async with asyncio.timeout(within):
                while not self._ended(stream):
                    for event in list(stream.events):
                        block = event["data"]
                        if (
                            event["event"] != "block"
                            or block["type"] not in ASKING_TYPES
                            or block["id"] in answered
                        ):
                            continue
                        answered.add(block["id"])
                        if on_question is not None:
                            await on_question(block)
                        if walk_away_at is not None and walk_away_at(block):
                            attendance.waiting_on = block
                            stream._task.cancel()
                            return attendance
                        await self._answer(job_id, block, attendance)
                    await asyncio.sleep(0.02)
            await stream.finished()
        return attendance

    @staticmethod
    def _ended(stream: Stream) -> bool:
        return any(
            e["event"] in ("done", "error")
            or (e["event"] == "status" and e["data"]["state"] == "cancelled")
            for e in stream.events
        )

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
