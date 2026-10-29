"""A university is one space of a student; its sources are parts of it (FR-037, plan §3.5)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError
from botai.db import models as m
from botai.spaces import service, universities, views
from tests.auth.support import Sm, make_connection, make_user, signup

Make = Callable[[], httpx.AsyncClient]
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


# --- one space per university ---------------------------------------------------------------------


async def test_a_university_is_one_space_however_it_is_named(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    ids = set()
    for index, body in enumerate(
        (
            {"title": "ВШЭ"},
            {"title": "Вышка"},
            {"title": "высшая  школа экономики"},
            {"title": "что угодно", "universityId": "vysshaya-shkola-ekonomiki"},
        )
    ):
        r = await client.post("/api/v1/spaces", json=body)
        assert r.status_code == (201 if index == 0 else 200), (body, r.text)
        ids.add(r.json()["id"])
    assert len(ids) == 1
    listed = (await client.get("/api/v1/spaces")).json()
    assert [(s["title"], s["shortTitle"]) for s in listed] == [("НИУ ВШЭ", "НИУ ВШЭ")]


async def test_a_second_space_of_one_university_is_impossible_in_the_database(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:
        s.add(m.University(id="u1", title="Вуз", short_title="В", origins=[], accent_hue=250))
        await s.flush()
        s.add_all(
            [
                m.Space(user_id=user, university_id="u1", title="Вуз"),
                m.Space(user_id=user, university_id=None, title="Мои материалы"),
            ]
        )
        await s.commit()
    for university_id in ("u1", None):
        async with tx_sessionmaker() as s:
            s.add(m.Space(user_id=user, university_id=university_id, title="Дубль"))
            with pytest.raises(IntegrityError):
                await s.flush()
    other = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:  # the same university for another student is fine
        s.add(m.Space(user_id=other, university_id="u1", title="Вуз"))
        s.add(m.Space(user_id=other, university_id=None, title="Мои материалы"))
        await s.commit()


async def test_ensure_space_creates_once_and_names_the_space_after_the_university(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:
        first = await service.ensure_space(s, user, "tsentralnyi-universitet")  # a catalog id
        again = await service.ensure_space(s, user, "tsentralnyi-universitet")
        await s.commit()
    assert first.id == again.id
    assert (first.title, first.university_id) == ("Центральный университет", first.university_id)
    async with tx_sessionmaker() as s:
        uni = await s.get(m.University, "tsentralnyi-universitet")  # the registry row appears now
        assert uni is not None
        assert first.accent_hue == uni.accent_hue
        with pytest.raises(BotaiError) as unknown:
            await service.ensure_space(s, user, "no-such-university")
    assert unknown.value.status == 404


async def test_one_materials_space_per_user_for_documents_outside_a_university(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    other = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:
        mine = await service.ensure_space(s, acc.id, None)
        again = await service.ensure_space(s, acc.id, None)
        theirs = await service.ensure_space(s, other, None)
        await s.commit()
    assert mine.id == again.id != theirs.id
    assert (mine.university_id, mine.title) == (None, "Мои материалы")
    listed = (await client.get("/api/v1/spaces")).json()
    assert [(s["title"], s["shortTitle"], s["universityId"]) for s in listed] == [
        ("Мои материалы", "Материалы", None)
    ]


async def test_two_requests_at_once_still_make_one_space(engine: object, database_url: str) -> None:
    """Real, concurrent transactions (not the shared test one): the advisory lock and the unique
    index leave exactly one space."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    eng = create_async_engine(database_url)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with sm() as s:
            user = m.User(
                email="race@example.com", display_name="R", password_hash="x", is_owner=False
            )
            s.add(user)
            await s.commit()
            user_id = user.id

        async def one() -> str:
            async with sm() as s:
                space = await service.ensure_space(s, user_id, "tsentralnyi-universitet")
                await s.commit()
                return space.id

        ids = await asyncio.gather(*(one() for _ in range(6)))
        assert len(set(ids)) == 1
        async with sm() as s:
            await s.execute(m.User.__table__.delete().where(m.User.id == user_id))
            await s.execute(
                m.University.__table__.delete().where(m.University.id == "tsentralnyi-universitet")
            )
            await s.commit()
    finally:
        await eng.dispose()


async def test_a_site_address_is_not_a_name_for_a_university(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    for title in ("lk.example.org", "https://lms.example.org/login", "my.uni.example"):
        r = await client.post("/api/v1/spaces", json={"title": title})
        assert r.status_code == 422, title
        assert "словами" in r.json()["detail"]
    ok = await client.post(
        "/api/v1/spaces", json={"title": "Институт связи", "homepageUrl": "https://lk.example.org"}
    )
    assert ok.status_code == 201  # the address may accompany a real name
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Space)) == 1


# --- finding a university -------------------------------------------------------------------------


async def test_search_ranks_the_candidates_a_name_may_mean(tx_sessionmaker: Sm) -> None:
    async with tx_sessionmaker() as s:
        s.add(
            m.University(
                id="learned-poly",
                title="Политехнический институт",
                short_title="ПИ",
                origins=["https://lk.poly.example"],
                accent_hue=250,
            )
        )
        await s.commit()
        by_alias = await universities.search(s, "вышка")
        by_short = await universities.search(s, "нияу мифи")
        typo = await universities.search(s, "Высшая школа экономкии")
        part = await universities.search(s, "политех")
        by_site = await universities.search(s, "lk.poly.example")
        nothing = await universities.search(s, "ничего подобного")
        empty = await universities.search(s, "  ")
        limited = await universities.search(s, "университет", limit=3)
    top = by_alias[0]
    assert (top.id, top.title, top.short_title, top.score) == (
        "vysshaya-shkola-ekonomiki",
        "НИУ ВШЭ",
        "НИУ ВШЭ",
        1.0,
    )
    assert "Высшая школа экономики" in top.aliases
    assert "Вышка" in top.aliases
    assert "НИУ ВШЭ" not in top.aliases  # the names it is shown under are not repeated
    assert by_short[0].id == "mephi"
    assert typo[0].id == "vysshaya-shkola-ekonomiki"
    assert 0 < typo[0].score < 1
    assert {c.id for c in part} >= {"learned-poly"}
    assert part == sorted(part, key=lambda c: -c.score)
    assert [c.id for c in by_site] == ["learned-poly"]
    assert nothing == []
    assert empty == []
    assert 0 < len(limited) <= 3


async def test_searching_saves_nothing(tx_sessionmaker: Sm) -> None:
    async with tx_sessionmaker() as s:
        await universities.search(s, "вшэ")
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(m.University)) == 0


# --- the sources of a university --------------------------------------------------------------------


async def _university_with_sources(sm: Sm, user_id: str) -> dict[str, str]:
    async with sm() as s:
        space = await service.ensure_space(s, user_id, "vysshaya-shkola-ekonomiki")
        materials = await service.ensure_space(s, user_id, None)
        await s.commit()
    portal = await make_connection(
        sm, user_id, space.id, origin="https://lk.hse.example", kind="portal"
    )
    lms = await make_connection(sm, user_id, space.id, origin="https://lms.hse.example", kind="lms")
    paper = await make_connection(
        sm, user_id, materials.id, origin="https://upload.botai.local", kind="document"
    )
    return {
        "space": space.id,
        "materials": materials.id,
        "portal": portal,
        "lms": lms,
        "paper": paper,
    }


async def _job(
    sm: Sm,
    user_id: str,
    connection_id: str,
    *,
    kind: str,
    state: str,
    thread_id: str | None,
    age_minutes: int = 0,
) -> str:
    async with sm() as s:
        job = m.Job(
            user_id=user_id,
            kind=kind,
            state=state,
            connection_id=connection_id,
            thread_id=thread_id,
            payload={},
            run_after=NOW,
            created_at=NOW - timedelta(minutes=age_minutes),
        )
        s.add(job)
        await s.commit()
        return job.id


async def _thread(sm: Sm, user_id: str) -> str:
    async with sm() as s:
        thread = m.ChatThread(user_id=user_id, title="Чат")
        s.add(thread)
        await s.commit()
        return thread.id


async def test_list_sources_groups_sources_by_university_with_their_task_and_thread(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    ids = await _university_with_sources(tx_sessionmaker, user)
    old_thread, new_thread = (
        await _thread(tx_sessionmaker, user),
        await _thread(tx_sessionmaker, user),
    )
    await _job(
        tx_sessionmaker,
        user,
        ids["lms"],
        kind="connect",
        state="done",
        thread_id=old_thread,
        age_minutes=60,
    )
    running = await _job(
        tx_sessionmaker,
        user,
        ids["lms"],
        kind="repair",
        state="waiting_user",
        thread_id=new_thread,
        age_minutes=5,
    )
    await _job(
        tx_sessionmaker, user, ids["lms"], kind="sync", state="done", thread_id=None, age_minutes=1
    )
    await _job(tx_sessionmaker, user, ids["portal"], kind="sync", state="done", thread_id=None)
    async with tx_sessionmaker() as s:
        result = await service.list_sources(s, user)
    assert [(u.space.id, u.short_title) for u in result] == [
        (ids["space"], "НИУ ВШЭ"),
        (ids["materials"], "Материалы"),
    ]
    hse, materials = result
    assert "Вышка" in hse.aliases
    assert hse.university is not None
    assert hse.university.id == "vysshaya-shkola-ekonomiki"
    assert materials.university is None
    by_id = {v.connection.id: v for v in hse.sources}
    assert set(by_id) == {ids["portal"], ids["lms"]}
    lms = by_id[ids["lms"]]
    assert lms.thread_id == new_thread  # the thread of its latest task that has one
    assert lms.active_task is not None
    assert (lms.active_task.job_id, lms.active_task.kind, lms.active_task.state) == (
        running,
        "repair",
        "waiting_user",
    )
    assert (by_id[ids["portal"]].thread_id, by_id[ids["portal"]].active_task) == (None, None)
    assert [v.connection.id for v in materials.sources] == [ids["paper"]]


async def test_a_deleted_thread_is_not_a_link_and_other_users_are_not_listed(
    tx_sessionmaker: Sm,
) -> None:
    user, other = await make_user(tx_sessionmaker), await make_user(tx_sessionmaker)
    ids = await _university_with_sources(tx_sessionmaker, user)
    gone = await _thread(tx_sessionmaker, user)
    await _job(tx_sessionmaker, user, ids["lms"], kind="connect", state="done", thread_id=gone)
    async with tx_sessionmaker() as s:
        await s.delete(await s.get(m.ChatThread, gone))
        await s.commit()
        mine = await service.list_sources(s, user)
        theirs = await service.list_sources(s, other)
    assert {v.connection.id: v.thread_id for u in mine for v in u.sources}[ids["lms"]] is None
    assert theirs == []


async def test_find_source_by_origin_belongs_to_the_user(tx_sessionmaker: Sm) -> None:
    user, other = await make_user(tx_sessionmaker), await make_user(tx_sessionmaker)
    ids = await _university_with_sources(tx_sessionmaker, user)
    async with tx_sessionmaker() as s:
        by_origin = await service.find_source(s, user, "https://lk.hse.example")
        by_page = await service.find_source(s, user, "LK.hse.example/schedule?week=1#x")
        foreign = await service.find_source(s, other, "https://lk.hse.example")
        missing = await service.find_source(s, user, "https://nowhere.example")
        invalid = await service.find_source(s, user, "ftp://lk.hse.example")
    assert by_origin is not None
    assert by_origin.id == ids["portal"]
    assert by_page is not None
    assert by_page.id == ids["portal"]
    assert (foreign, missing, invalid) == (None, None, None)


# --- what the API says about a source ---------------------------------------------------------------


async def test_connection_out_has_a_human_title_a_kind_the_page_and_the_task(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    ids = await _university_with_sources(tx_sessionmaker, acc.id)
    thread = await _thread(tx_sessionmaker, acc.id)
    job = await _job(
        tx_sessionmaker, acc.id, ids["portal"], kind="connect", state="running", thread_id=thread
    )
    async with tx_sessionmaker() as s:
        portal = await s.get(m.Connection, ids["portal"])
        lms = await s.get(m.Connection, ids["lms"])
        assert portal is not None
        assert lms is not None
        portal.title = "lk.hse.example"  # what the old flow stored: a bare host
        portal.entry_url = "https://lk.hse.example/schedule"
        lms.kind, lms.title = "mail", "Smart LMS"  # a kind of the old vocabulary
        await s.commit()
    listed = {
        c["id"]: c for c in (await client.get(f"/api/v1/spaces/{ids['space']}/connections")).json()
    }
    portal_out, lms_out = listed[ids["portal"]], listed[ids["lms"]]
    assert portal_out["title"] == "Личный кабинет"  # the kind's name, not the host
    assert portal_out["kind"] == "portal"
    assert portal_out["entryUrl"] == "https://lk.hse.example/schedule"
    assert portal_out["threadId"] == thread
    assert portal_out["activeTask"] == {"jobId": job, "kind": "connect", "state": "running"}
    assert (lms_out["title"], lms_out["kind"]) == ("Smart LMS", "other")
    assert (lms_out["threadId"], lms_out["activeTask"], lms_out["entryUrl"]) == (None, None, None)
    one = (await client.get(f"/api/v1/connections/{ids['portal']}")).json()
    assert one == portal_out


def test_a_source_is_titled_by_its_name_then_its_page_then_its_kind() -> None:
    title = views.source_title
    assert title(kind="lms", title="Smart LMS") == "Smart LMS"
    assert title(kind="lms", title="lk.example.org", page_title="Единый личный кабинет") == (
        "Единый личный кабинет"
    )
    assert title(kind="portal", title="https://lk.example.org/") == "Личный кабинет"
    assert title(kind="sheet", title=None) == "Таблица"
    assert title(kind="something-new") == "Источник"
    assert title(kind="site", title="  Документация   istudents  ") == "Документация istudents"
    assert len(title(kind="site", title="я" * 500)) == 120
    for kind in views.CONNECTION_KINDS:
        assert views.KIND_TITLES[kind]


def test_entry_urls_keep_pages_and_refuse_secrets() -> None:
    assert views.clean_entry_url("https://lk.example.org/schedule?week=3#/day") == (
        "https://lk.example.org/schedule?week=3#/day"
    )
    for bad in (
        "http://lk.example.org/",
        "https://u:p@lk.example.org/",
        "https://lk.example.org/cb?access_token=abc",
        "https://127.0.0.1/",
        None,
    ):
        assert views.clean_entry_url(bad) is None, bad
