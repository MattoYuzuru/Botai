"""`DELETE /sources/{id}`: the source's data and artifacts go, its history and learned knowledge stay.

The owner's rule (03 §1.5): switching off hides data reversibly; deleting removes the source and its
data. Jobs, agent traces and playbooks outlive the source (no foreign key / D-018); a task in flight
refuses the delete with 409 instead of losing its record.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from botai.core.ids import new_id
from botai.db.models import (
    AgentRun,
    Assignment,
    Course,
    Deadline,
    Grade,
    Job,
    Location,
    NewsItem,
    Person,
    Playbook,
    ScheduleEvent,
    Source,
    SourceArtifact,
    SourcePriority,
    SourceState,
)
from botai.sources.v4 import service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import FrozenClock, Sm, signup
from tests.sources.v4.support import create_account

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


async def _space(client: httpx.AsyncClient, title: str = "Fakeuni") -> str:
    response = await client.post("/api/v1/spaces", json={"title": title})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _delete(client: httpx.AsyncClient, source_id: str) -> httpx.Response:
    return await client.delete(f"/api/v1/sources/{source_id}")


def _prov(user_id: str, source_id: str) -> dict:
    return {
        "user_id": user_id,
        "connection_id": None,
        "source_id": source_id,
        "external_id": new_id(),
        "source_url": f"{BASE}/api",
        "fetched_at": NOW,
        "seen_at": NOW,
    }


async def _seed(session, *, user_id: str, space_id: str, source: Source) -> dict[str, str]:
    """The rows a source owns, a playbook and a finished job that must outlive it."""
    playbook = Playbook(id=new_id(), title="Fakeuni", language="declarative")
    session.add(playbook)
    await session.flush()
    source.playbook_id = playbook.id
    other_source = Source(
        id=new_id(),
        user_id=user_id,
        space_id=space_id,
        title="Второй",
        identity_key=f"{BASE}/other",
        status="active",
        kinds=["site"],
    )
    session.add(other_source)
    await session.flush()
    session.add_all(
        [
            SourceArtifact(
                id=new_id(),
                user_id=user_id,
                source_id=source.id,
                kind="page",
                sha256="0" * 64,
            ),
            SourceState(source_id=source.id),
            SourcePriority(
                space_id=space_id, kind="schedule", source_ids=[source.id, other_source.id]
            ),
            ScheduleEvent(
                title="Пара",
                kind="other",
                starts_at=NOW,
                ends_at=NOW,
                format="unknown",
                changed=False,
                **_prov(user_id, source.id),
            ),
            Course(title="Матан", archived=False, **_prov(user_id, source.id)),
            Assignment(title="Задача", **_prov(user_id, source.id)),
            Deadline(title="Дедлайн", state="open", **_prov(user_id, source.id)),
            Grade(value="8", **_prov(user_id, source.id)),
            Person(name="Иванов", role="other", **_prov(user_id, source.id)),
            Location(title="305", **_prov(user_id, source.id)),
            NewsItem(title="Новость", published_at=NOW, **_prov(user_id, source.id)),
        ]
    )
    job_id = new_id()
    session.add(
        Job(
            id=job_id,
            user_id=user_id,
            kind="sync",
            state="done",
            payload={"source_id": source.id},
            run_after=NOW,
        )
    )
    await session.flush()  # the trace points at the job row by foreign key
    session.add(
        AgentRun(run_id=new_id(), user_id=user_id, job_id=job_id, task="sync", status="done")
    )
    await session.commit()
    return {"playbook": playbook.id, "other_source": other_source.id, "job": job_id}


async def _count(sm: Sm, model: type, **where) -> int:
    async with sm() as session:
        query = select(func.count()).select_from(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return int(await session.scalar(query) or 0)


async def test_deleting_a_source_removes_its_data_and_artifacts_but_keeps_history(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    created = await v4_client.post(
        "/api/v1/sources",
        json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
    )
    source_id = created.json()["id"]
    async with tx_sessionmaker() as session:
        source = await service.get_source(session, user_id=account.id, source_id=source_id)
        ids = await _seed(session, user_id=account.id, space_id=space_id, source=source)

    response = await _delete(v4_client, source_id)
    assert response.status_code == 204, response.text
    assert response.content == b""

    async with tx_sessionmaker() as session:
        assert await session.get(Source, source_id) is None
        # the learned knowledge is shared and stays (D-018)
        assert await session.get(Playbook, ids["playbook"]) is not None
        # the run history and its trace stay, they carry the payload, not a foreign key
        job = await session.get(Job, ids["job"])
        assert job is not None
        assert job.payload == {"source_id": source_id}
        agent_run = await session.scalar(select(AgentRun).where(AgentRun.job_id == ids["job"]))
        assert agent_run is not None
    for model in (
        SourceArtifact,
        SourceState,
        ScheduleEvent,
        Course,
        Assignment,
        Deadline,
        Grade,
        Person,
        Location,
        NewsItem,
    ):
        assert await _count(tx_sessionmaker, model, source_id=source_id) == 0, model.__name__
    async with tx_sessionmaker() as session:
        priority = (
            await session.scalars(select(SourcePriority).where(SourcePriority.space_id == space_id))
        ).one()
        assert priority.source_ids == [ids["other_source"]]
    # the address is free again: the same source can be connected anew
    again = await v4_client.post(
        "/api/v1/sources",
        json={"spaceId": space_id, "title": "Снова", "url": f"{BASE}/app"},
    )
    assert again.status_code == 201, again.text


async def test_a_source_with_a_task_in_flight_is_not_deleted(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    created = await v4_client.post(
        "/api/v1/sources",
        json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
    )
    source_id = created.json()["id"]
    running_id = new_id()
    async with tx_sessionmaker() as session:
        session.add(
            Job(
                id=running_id,
                user_id=account.id,
                kind="sync",
                state="running",
                payload={"source_id": source_id},
                run_after=clock.now(),
            )
        )
        await session.commit()

    response = await _delete(v4_client, source_id)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"
    assert "дождитесь" in response.json()["detail"].lower()
    async with tx_sessionmaker() as session:
        assert await session.get(Source, source_id) is not None  # nothing was deleted

    # when the task is over the delete goes through — and the finished job can stay without a source
    async with tx_sessionmaker() as session:
        job = await session.get(Job, running_id)
        assert job is not None
        job.state = "done"
        await session.commit()
    assert (await _delete(v4_client, source_id)).status_code == 204
    async with tx_sessionmaker() as session:
        assert await session.get(Job, running_id) is not None


async def test_a_foreign_source_is_404(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    other = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        foreign = await service.create_source(
            session, user_id=other.user_id, space_id=other.space_id, title="Чужой"
        )
        await session.commit()
    assert (await _delete(v4_client, foreign.id)).status_code == 404
    async with tx_sessionmaker() as session:
        assert await session.get(Source, foreign.id) is not None  # another student's row is intact


async def test_the_delete_endpoint_is_off_on_the_v3_engine(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    other = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session, user_id=other.user_id, space_id=other.space_id, title="Чужой"
        )
        await session.commit()
    assert (await client.delete(f"/api/v1/sources/{source.id}")).status_code == 404


async def test_the_playbook_of_a_deleted_source_can_be_attached_to_a_new_one(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    """Playbooks are the learned knowledge of a university, not a part of one source record (D-018)."""
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    created = (
        await v4_client.post(
            "/api/v1/sources",
            json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
        )
    ).json()
    async with tx_sessionmaker() as session:
        source = await service.get_source(session, user_id=account.id, source_id=created["id"])
        ids = await _seed(session, user_id=account.id, space_id=space_id, source=source)
    assert (await _delete(v4_client, created["id"])).status_code == 204

    replacement = (
        await v4_client.post(
            "/api/v1/sources",
            json={"spaceId": space_id, "title": "Новый кабинет", "url": f"{BASE}/app2"},
        )
    ).json()
    async with tx_sessionmaker() as session:
        updated = await service.update_source(
            session,
            user_id=account.id,
            source_id=replacement["id"],
            patch=SourcePatch(playbook_id=ids["playbook"]),
        )
        await session.commit()
        assert updated.playbook_id == ids["playbook"]
