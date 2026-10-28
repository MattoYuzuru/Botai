"""The slice-5 definition of done, end to end (brief W1): invite -> registration -> space ->
connection -> `connect` job in the queue -> SSE with the events of a stand-in handler -> answers to
blocks, a secret one included."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import update

from botai.contracts.agent_ui import (
    ChoiceBlock,
    ChoiceOption,
    InputBlock,
    SummaryBlock,
    SummaryGroup,
    SummaryItem,
    ThinkingBlock,
)
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import Connection
from tests.auth.support import NOW, PASSWORD, make_user
from tests.jobs.env import Env, live_http, session_header
from tests.jobs.test_sse import Stream, listen

pytestmark = pytest.mark.db

UNI_PASSWORD = "P@ssw0rd-FIXTURE"


async def test_invite_to_connect_job_to_answers(env: Env) -> None:
    # --- the owner invites a friend -----------------------------------------------------------
    owner = env.new_client()
    owner_id = await make_user(env.sm, "owner@example.com", is_owner=True, password=PASSWORD)
    env.users.append(owner_id)
    assert (
        await owner.post(
            "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
        )
    ).status_code == 200
    invite = (await owner.post("/api/v1/auth/invites")).json()["code"]

    # --- the friend registers, adds a university and a source ---------------------------------
    friend = env.new_client()
    reg = await friend.post(
        "/api/v1/auth/register",
        json={
            "inviteCode": invite,
            "email": "friend@example.com",
            "displayName": "Друг",
            "password": PASSWORD,
        },
    )
    assert reg.status_code == 201
    friend_id = reg.json()["user"]["id"]
    env.users.append(friend_id)

    space = (await friend.post("/api/v1/spaces", json={"title": "Тестовый университет"})).json()
    started = await friend.post(
        f"/api/v1/spaces/{space['id']}/connections",
        json={"kind": "lms", "url": "https://lms.example.org/login"},
    )
    assert started.status_code == 201
    connection_id = started.json()["connection"]["id"]
    job_id = started.json()["jobId"]
    assert started.json()["connection"]["status"] == "draft"
    queued = await env.job(job_id)
    assert (queued.kind, queued.state, queued.connection_id) == ("connect", "queued", connection_id)

    # --- a stand-in for the W2/W3 handler: stages, a question, a secret question, a result -----
    pick = ChoiceBlock(
        id=new_id(),
        at=NOW,
        question="Что подключить?",
        mode="single",
        required=True,
        options=[
            ChoiceOption(value="schedule", label="Расписание"),
            ChoiceOption(value="lms", label="LMS"),
        ],
    )
    password = InputBlock(
        id=new_id(),
        at=NOW,
        question="Пароль от личного кабинета",
        input_kind="password",
        required=True,
        secret=True,
        autocomplete="current-password",
    )

    async def connect(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        await ctx.status("Открываю сайт")
        await ctx.emit(ThinkingBlock(id=new_id(), at=NOW, stage="Нашёл форму входа", state="ok"))
        choice = await ctx.ask(pick)
        pw = await ctx.ask_secret(password)
        assert ctx.connection_id is not None
        await services.vault.put(
            ctx.user_id, ctx.connection_id, SecretKind.PASSWORD, pw.get_secret_value()
        )
        async with services.db() as s:
            await s.execute(
                update(Connection).where(Connection.id == ctx.connection_id).values(status="active")
            )
            await s.commit()
        await ctx.emit(
            SummaryBlock(
                id=new_id(),
                at=NOW,
                title="Готово",
                groups=[
                    SummaryGroup(
                        title="Подключено",
                        items=[SummaryItem(label="Источник", value=str(choice.values))],
                    )
                ],
            )
        )
        return {"connected": choice.values, "payload": payload}

    env.start(env.worker({JobKind.CONNECT: connect}))

    # --- the browser follows the job over SSE and answers what it is asked --------------------
    async with (
        live_http(env.app) as http,
        listen(http, f"/api/v1/jobs/{job_id}/events", session_header(friend)) as stream,
    ):
        await stream.until(lambda s: _asked(s, pick.id))
        waiting = [e for e in stream.events if e["event"] == "status"][-1]
        assert waiting["data"]["state"] == "waiting_user"

        r = await http.post(
            f"/api/v1/jobs/{job_id}/answers",
            headers=session_header(friend),
            json={"blockId": pick.id, "values": "schedule"},
        )
        assert r.status_code == 204
        again = await http.post(
            f"/api/v1/jobs/{job_id}/answers",
            headers=session_header(friend),
            json={"blockId": pick.id, "values": "lms"},
        )
        assert again.status_code == 409
        assert again.json()["code"] == "block_answered"

        await stream.until(lambda s: _asked(s, password.id))
        r = await http.post(
            f"/api/v1/jobs/{job_id}/answers",
            headers=session_header(friend),
            json={"blockId": password.id, "values": UNI_PASSWORD},
        )
        assert r.status_code == 204
        await stream.finished()

    # --- what the stream said ---------------------------------------------------------------
    names = stream.names()
    assert names[0] == "status"
    assert names[-1] == "done"
    assert stream.events[-1]["data"] == {
        "result": {
            "connected": "schedule",
            "payload": {
                "url": "https://lms.example.org/login",
                "kind": "lms",
                "kinds": ["scheduleEvents", "deadlines"],
            },
        }
    }
    blocks = [e["data"] for e in stream.events if e["event"] == "block"]
    assert [b["type"] for b in blocks] == ["thinking", "choice", "input", "summary"]
    assert blocks[2]["secret"] is True
    states = [e["data"]["state"] for e in stream.events if e["event"] == "status"]
    assert states == [
        "queued",
        "running",
        "running",
        "waiting_user",
        "running",
        "waiting_user",
        "running",
        "done",
    ]
    assert UNI_PASSWORD not in json.dumps(stream.events)

    # --- and where the secret went ------------------------------------------------------------
    conn = (await friend.get(f"/api/v1/connections/{connection_id}")).json()
    assert conn["status"] == "active"
    assert conn["hasPassword"] is True
    assert UNI_PASSWORD not in json.dumps(conn)
    stored = await env.services.vault.get(friend_id, connection_id, SecretKind.PASSWORD)
    assert stored is not None
    assert stored.get_secret_value() == UNI_PASSWORD
    assert not await env.services.vault.has(friend_id, password.id, SecretKind.TRANSIENT)
    answers = {a.block_id: a for a in await env.answers(job_id)}
    assert (answers[pick.id].values, answers[pick.id].is_secret) == ("schedule", False)
    assert (answers[password.id].values, answers[password.id].is_secret) == (None, True)
    for e in await env.events(job_id):
        assert UNI_PASSWORD not in json.dumps(e.data)

    # --- the student can make the system forget the password ----------------------------------
    assert (await friend.delete(f"/api/v1/connections/{connection_id}/password")).status_code == 204
    assert (await friend.get(f"/api/v1/connections/{connection_id}")).json()["hasPassword"] is False

    # --- a late reconnect gets only what is new ------------------------------------------------
    last = stream.events[-1]["id"]
    tail = await friend.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": last})
    assert tail.status_code == 204  # nothing new, and the browser must stop reconnecting


def _asked(stream: Stream, block_id: str) -> bool:
    """The block was emitted and the job is waiting for the answer."""
    seen = any(e["event"] == "block" and e["data"]["id"] == block_id for e in stream.events)
    status = [e for e in stream.events if e["event"] == "status"][-1:]
    return seen and bool(status) and status[0]["data"]["state"] == "waiting_user"
