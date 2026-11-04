"""Operator source CLI on Postgres: canary without writes, sync through the real queue (L1).

No model and no network: the university is an `httpx.MockTransport` that serves a synthetic grades
list, the worker is the real v4 dispatch table started in the test. The playbook is active with a
green canary and the source is `draft`, exactly the state the command exists for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.agent.v4.dispatch import make_v4_handlers
from botai.core.jobs import JobKind
from botai.db.models import Grade, Job, PlaybookVersion, Source, SourceArtifact
from botai.ops import source_sync
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import make_space
from tests.jobs.env import Env

pytestmark = pytest.mark.db

COOKIE = "SESSION-COOKIE-VALUE-0123456789"
URL = "https://lk.fakeuni.test/api/marks"
TITLES = ("Секретный предмет Альфа", "Секретный предмет Бета", "Секретный предмет Гамма")
ROWS = [
    {"id": "g1", "t": TITLES[0], "v": "91.5", "s": "баллы", "at": "2026-06-10", "c": "c-1"},
    {"id": "g2", "t": TITLES[1], "v": "отлично", "s": "5-балльная", "at": "2026-06-12"},
    {"id": "g3", "t": TITLES[2], "v": "зачёт", "s": "", "at": "2026-06-15"},
]
SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {
            "name": "marks",
            "kind": "grades",
            "url": URL,
            "format": "json",
            "items_path": "",
            "fields": {
                "externalId": "/id",
                "title": "/t",
                "value": "/v",
                "scale": "/s",
                "gradedAt": "/at",
                "courseId": "/c",
            },
        }
    ],
    "timeout_s": 10,
}
# Everything the command must never print: cookie, titles, grade values.
FORBIDDEN = (COOKIE, *TITLES, "отлично", "зачёт", "91.5", URL)


def _transport(status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, text="denied")
        if COOKIE not in request.headers.get("cookie", ""):
            return httpx.Response(401, text="login")
        return httpx.Response(200, json=ROWS)

    return httpx.MockTransport(handler)


class Lines:
    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join([*self.out, *self.err])


async def make_source(
    env: Env,
    user_id: str,
    *,
    with_playbook: bool = True,
    status: str | None = None,
    kinds: tuple[str, ...] = ("portal", "schedule", "deadlines"),
) -> str:
    space_id = await make_space(env.sm, user_id)
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url="https://lk.fakeuni.test/go",
            kinds=list(kinds),
            config={},
        )
        if with_playbook:
            playbook = await playbooks.create_playbook(
                session, user_id=user_id, title="Кабинет", university_id=None
            )
            version = await playbooks.add_playbook_version(
                session, user_id=user_id, playbook_id=playbook.id, spec=SPEC
            )
            await playbooks.record_canary(
                session,
                user_id=user_id,
                playbook_id=playbook.id,
                version=version.version,
                canary={"ok": True, "counts": {"grades": 3}},
            )
            await playbooks.activate_playbook_version(
                session, user_id=user_id, playbook_id=playbook.id, version=version.version
            )
            await service.update_source(
                session,
                user_id=user_id,
                source_id=source.id,
                patch=SourcePatch(playbook_id=playbook.id),
            )
        if status is not None:
            await service.update_source(
                session, user_id=user_id, source_id=source.id, patch=SourcePatch(status=status)
            )
        await session.commit()
        return source.id


async def invoke(
    env: Env, argv: list[str], tmp_path: Path, *, transport: httpx.MockTransport | None = None
) -> tuple[int, Lines]:
    lines = Lines()
    code = await source_sync.run(
        argv,
        out=lines.out.append,
        err=lines.err.append,
        services=env.services,
        http_transport=transport or _transport(),
        artifact_root=tmp_path / "artifacts",
        source_cookies={"sid": COOKIE},
        poll_s=0.05,
    )
    return code, lines


def start_worker(env: Env, tmp_path: Path, transport: httpx.MockTransport | None = None) -> None:
    env.start(
        env.worker(
            make_v4_handlers(
                http_transport=transport or _transport(),
                artifact_root=tmp_path / "artifacts",
                source_cookies={"sid": COOKIE},
            )
        )
    )


async def counts_of(env: Env, source_id: str) -> dict[str, int]:
    async with env.sm() as session:
        grades = await session.scalar(
            select(func.count()).select_from(Grade).where(Grade.source_id == source_id)
        )
        artifacts = await session.scalar(
            select(func.count())
            .select_from(SourceArtifact)
            .where(SourceArtifact.source_id == source_id)
        )
        jobs = await session.scalar(select(func.count()).select_from(Job))
    return {"grades": grades or 0, "artifacts": artifacts or 0, "jobs": jobs or 0}


async def source_row(env: Env, source_id: str) -> Source:
    async with env.sm() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        return source


async def test_canary_prints_counts_and_field_presence_and_writes_nothing(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)
    async with env.sm() as session:
        before_versions = (await session.scalars(select(PlaybookVersion))).all()
        canary_before = [v.canary for v in before_versions if v.canary]

    code, lines = await invoke(env, ["--source", source_id, "--canary"], tmp_path)

    assert code == 0, lines.text
    assert "Результат: ok" in lines.text
    assert "grades=3" in lines.text
    # presence only: 3 records, course id set on one of them, scale empty on one
    assert "Поля grades (записей 3)" in lines.text
    assert "course_external_id=1" in lines.text
    assert "scale=2" in lines.text
    assert "value=3" in lines.text
    assert "graded_at=3" in lines.text
    for secret in FORBIDDEN:
        assert secret not in lines.text
    assert await counts_of(env, source_id) == {"grades": 0, "artifacts": 0, "jobs": 0}
    source = await source_row(env, source_id)
    assert (source.status, source.last_sync_at, source.next_sync_at) == ("draft", None, None)
    async with env.sm() as session:
        after = (await session.scalars(select(PlaybookVersion))).all()
    assert [v.canary for v in after if v.canary] == canary_before


async def test_canary_failure_exits_one_and_prints_only_codes(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)

    code, lines = await invoke(
        env, ["--source", source_id, "--canary"], tmp_path, transport=_transport(403)
    )

    assert code == 1
    assert "Результат: не пройден" in lines.text
    assert "source_auth_required" in lines.text
    assert "denied" not in lines.text
    assert (await source_row(env, source_id)).status == "draft"


async def test_sync_moves_a_draft_source_to_active_through_the_worker(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)
    start_worker(env, tmp_path)

    code, lines = await invoke(env, ["--source", source_id, "--wait", "30"], tmp_path)

    assert code == 0, lines.text
    assert "Состояние источника: draft -> active" in lines.text
    assert "Итог sync: ok" in lines.text
    assert "grades=3" in lines.text
    assert "Строк в базе от источника: grades=3" in lines.text
    for secret in FORBIDDEN:
        assert secret not in lines.text
    source = await source_row(env, source_id)
    assert source.status == "active"
    assert source.last_sync_at is not None
    assert source.next_sync_at is not None
    assert source.next_sync_at > source.last_sync_at
    assert (await counts_of(env, source_id))["grades"] == 3
    async with env.sm() as session:
        job = await session.scalar(select(Job).where(Job.kind == JobKind.SYNC.value))
    assert job is not None
    assert job.state == "done"
    assert job.payload["source_id"] == source_id

    # a repeat is idempotent: the same rows, no duplicates
    again, _ = await invoke(env, ["--source", source_id, "--wait", "30"], tmp_path)
    assert again == 0
    assert (await counts_of(env, source_id))["grades"] == 3


async def test_sync_without_wait_only_enqueues(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)

    code, lines = await invoke(env, ["--source", source_id], tmp_path)

    assert code == 0, lines.text
    assert "Задание:" in lines.text
    async with env.sm() as session:
        job = await session.scalar(select(Job).where(Job.kind == JobKind.SYNC.value))
    assert job is not None
    assert (job.state, job.user_id, job.payload) == ("queued", user.id, {"source_id": source_id})
    assert (await source_row(env, source_id)).status == "draft"

    # a second run reuses the queued job instead of queueing another
    again, _ = await invoke(env, ["--source", source_id], tmp_path)
    assert again == 0
    assert (await counts_of(env, source_id))["jobs"] == 1


async def test_wait_times_out_with_exit_one_when_no_worker_runs(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)

    code, lines = await invoke(env, ["--source", source_id, "--wait", "1"], tmp_path)

    assert code == 1
    assert "не завершилось" in lines.text


async def test_a_rejected_session_ends_in_needs_reauth_with_exit_one(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)
    start_worker(env, tmp_path, transport=_transport(403))

    code, lines = await invoke(env, ["--source", source_id, "--wait", "30"], tmp_path)

    assert code == 1
    assert "draft -> needs_reauth" in lines.text
    assert "source_auth_required" in lines.text
    assert "нужен новый вход" in lines.text
    assert (await counts_of(env, source_id))["grades"] == 0


async def test_dry_run_reads_only(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)
    seen: list[httpx.Request] = []

    for extra in ([], ["--canary"]):
        code, lines = await invoke(
            env,
            ["--source", source_id, "--dry-run", "--kinds-add", "grades", *extra],
            tmp_path,
            transport=_transport(seen=seen),
        )
        assert code == 0, lines.text
        assert "--dry-run" in lines.text
        assert "Виды после: portal, schedule, deadlines, grades" in lines.text
        assert f"Источник: {source_id}" in lines.text
        assert "v1 (canary ok)" in lines.text

    assert seen == []  # no network
    assert await counts_of(env, source_id) == {"grades": 0, "artifacts": 0, "jobs": 0}
    source = await source_row(env, source_id)
    assert source.kinds == ["portal", "schedule", "deadlines"]
    assert source.status == "draft"


async def test_kinds_add_updates_the_declared_kinds_and_prints_before_and_after(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id)

    code, lines = await invoke(
        env, ["--source", source_id, "--canary", "--kinds-add", "grades,schedule"], tmp_path
    )

    assert code == 0, lines.text
    assert "Виды до: portal, schedule, deadlines" in lines.text
    assert "Виды после: portal, schedule, deadlines, grades" in lines.text
    assert "Виды обновлены" in lines.text
    assert (await source_row(env, source_id)).kinds == ["portal", "schedule", "deadlines", "grades"]


@pytest.mark.parametrize("bad", ["homework", "portal", "grades,,nope"])
def test_kinds_add_rejects_names_outside_the_data_vocabulary(bad: str) -> None:
    with pytest.raises(SystemExit) as stop:
        source_sync.build_parser().parse_args(["--source", "0" * 26, "--kinds-add", bad])
    assert stop.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--source", "short"],
        ["--source", "0" * 26, "--wait", "0"],
        ["--source", "0" * 26, "--wait", "x"],
    ],
)
def test_bad_arguments_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        source_sync.build_parser().parse_args(argv)
    assert stop.value.code == 2


async def test_unknown_source_exits_two(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    code, lines = await invoke(env, ["--source", "0" * 26], tmp_path)
    assert code == 2
    assert "Источник не найден" in lines.err[0]


@pytest.mark.parametrize("status", ["disabled", "archived"])
async def test_a_switched_off_source_is_refused(env: Env, tmp_path: Path, status: str) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id, status=status)

    code, lines = await invoke(env, ["--source", source_id, "--canary"], tmp_path)

    assert code == 2
    assert status in lines.text
    assert (await counts_of(env, source_id))["jobs"] == 0


async def test_a_source_without_playbook_is_refused(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    source_id = await make_source(env, user.id, with_playbook=False)

    code, lines = await invoke(env, ["--source", source_id], tmp_path)

    assert code == 2
    assert "нет playbook" in lines.err[0]


async def test_sync_needs_the_v4_engine(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v3"
    user = await env.account()
    source_id = await make_source(env, user.id)

    code, lines = await invoke(env, ["--source", source_id], tmp_path)

    assert code == 2
    assert "BOTAI_AGENT_ENGINE=v4" in lines.err[0]
