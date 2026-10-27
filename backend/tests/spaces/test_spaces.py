"""Spaces and the learned university registry (FR-002, FR-014, FR-032, NFR-001)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import (
    Connection,
    Course,
    Deadline,
    Document,
    Fact,
    ScheduleEvent,
    SecretRow,
    Space,
    University,
)
from botai.spaces import universities
from tests.auth.support import Sm, make_connection, make_space, signup

Make = Callable[[], httpx.AsyncClient]
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


async def _login(
    new_client: Make, tx: Sm, clock: FrozenClock, email: str
) -> tuple[httpx.AsyncClient, str]:
    c = new_client()
    acc = await signup(c, tx, clock, email=email)
    return c, acc.id


# --- registry -----------------------------------------------------------------------------------


def test_slug_ignores_case_spacing_and_punctuation() -> None:
    assert universities.slugify("Высшая школа экономики") == universities.slugify(
        "  высшая  Школа «экономики» "
    )
    assert universities.slugify("Центральный университет") == "tsentralnyi-universitet"


def test_slug_fits_the_id_column_and_stays_unique_when_truncated() -> None:
    a = universities.slugify("Государственный университет " + "а" * 80 + " один")
    b = universities.slugify("Государственный университет " + "а" * 80 + " два")
    assert len(a) <= universities.SLUG_MAX
    assert len(b) <= universities.SLUG_MAX
    assert a != b
    assert universities.slugify("日本語").startswith("university-")


@pytest.mark.parametrize(
    "title", ["Вуз", "Университет с длинным названием", "Some College", "日本語"]
)
def test_accent_hue_is_deterministic_and_in_the_styleguide_window(title: str) -> None:
    hue = universities.accent_hue_for(universities.slugify(title))
    assert hue == universities.accent_hue_for(universities.slugify(title))
    assert universities.HUE_MIN <= hue <= universities.HUE_MAX
    assert round(hue, 1) == hue


def test_hues_are_spread_over_the_window() -> None:
    hues = {universities.accent_hue_for(f"uni-{n}") for n in range(60)}
    assert len(hues) > 40
    assert min(hues) < 260
    assert max(hues) > 320


def test_short_title() -> None:
    assert universities.short_title_for("ВШЭ") == "ВШЭ"
    assert universities.short_title_for("Центральный университет") == "ЦУ"
    assert len(universities.short_title_for("А" * 100)) <= universities.SHORT_TITLE_MAX


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Lms.Example.org/path?q=1#frag", "https://lms.example.org"),
        ("http://example.org:8080/x", "http://example.org:8080"),
        ("https://example.org:443/", "https://example.org"),
        ("example.org/login", "https://example.org"),
    ],
)
def test_origin_of_keeps_scheme_host_and_port_only(url: str, origin: str) -> None:
    assert universities.origin_of(url) == origin


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org",
        "https://user:pass@example.org",
        "javascript:alert(1)",
        "https://",
        "https://example.org:99999999",
    ],
)
def test_origin_of_rejects_unsafe_urls(url: str) -> None:
    from botai.core.errors import BotaiError

    with pytest.raises(BotaiError):
        universities.origin_of(url)


# --- API ----------------------------------------------------------------------------------------


async def test_create_space_learns_the_university(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    c, _ = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    r = await c.post("/api/v1/spaces", json={"title": "  Высшая   школа экономики "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Высшая школа экономики"
    assert body["universityId"] == universities.slugify("Высшая школа экономики")
    assert 235 <= body["accentHue"] <= 345
    assert body["timeZone"] == "Europe/Moscow"

    async with tx_sessionmaker() as s:
        uni = await s.get(University, body["universityId"])
    assert uni is not None
    assert uni.accent_hue == body["accentHue"]
    assert uni.accent_verified is False
    assert uni.third_party_warning is None  # filled by the learning agent, not by us
    assert uni.origins == []


async def test_a_second_student_of_the_same_university_shares_the_registry_row(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, _ = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    b, _ = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    first = (await a.post("/api/v1/spaces", json={"title": "Центральный университет"})).json()
    second = (await b.post("/api/v1/spaces", json={"title": "центральный  университет"})).json()
    assert first["universityId"] == second["universityId"]
    assert first["id"] != second["id"]
    assert first["accentHue"] == second["accentHue"]
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(University)) == 1


async def test_homepage_url_dedupes_and_stores_the_origin_only(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, _ = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    b, _ = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    first = (
        await a.post(
            "/api/v1/spaces",
            json={
                "title": "Политех",
                "homepageUrl": "https://www.Poly.example.edu/students?x=1#top",
            },
        )
    ).json()
    second = (
        await b.post(
            "/api/v1/spaces",
            json={
                "title": "Политехнический университет",
                "homepageUrl": "https://www.poly.example.edu/",
            },
        )
    ).json()
    assert first["universityId"] == second["universityId"]
    async with tx_sessionmaker() as s:
        uni = await s.get(University, first["universityId"])
    assert uni is not None
    assert uni.origins == ["https://www.poly.example.edu"]


async def test_shared_registry_rows_are_never_rewritten_by_a_student(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, _ = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    b, _ = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    await a.post(
        "/api/v1/spaces", json={"title": "Институт", "homepageUrl": "https://inst.example.edu"}
    )
    await b.post(
        "/api/v1/spaces", json={"title": "Институт", "homepageUrl": "https://evil.example.com"}
    )
    async with tx_sessionmaker() as s:
        uni = await s.get(University, "institut")
    assert uni is not None
    assert uni.origins == ["https://inst.example.edu"]


async def test_explicit_university_id(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    known = (await client.post("/api/v1/spaces", json={"title": "Академия"})).json()["universityId"]
    ok = await client.post("/api/v1/spaces", json={"title": "Моя академия", "universityId": known})
    assert ok.status_code == 201
    assert ok.json()["universityId"] == known
    assert ok.json()["title"] == "Моя академия"
    missing = await client.post(
        "/api/v1/spaces", json={"title": "X", "universityId": "no-such-uni"}
    )
    assert missing.status_code == 404


async def test_unsafe_homepage_url_is_rejected(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    for url in ("https://user:pw@example.org/", "ftp://example.org"):
        r = await client.post("/api/v1/spaces", json={"title": "Вуз", "homepageUrl": url})
        assert r.status_code == 422, url
        assert "pw" not in r.text
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(Space)) == 0


async def test_search_universities(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    for title in ("Высшая школа экономики", "Центральный университет", "Технологический институт"):
        await client.post("/api/v1/spaces", json={"title": title})
    everything = (await client.get("/api/v1/universities")).json()
    assert len(everything) == 3
    assert [u["title"] for u in everything] == sorted(u["title"] for u in everything)
    found = (await client.get("/api/v1/universities", params={"q": "ЦЕНТРАЛЬН"})).json()
    assert [u["title"] for u in found] == ["Центральный университет"]
    assert found[0]["shortTitle"] == "ЦУ"
    assert (await client.get("/api/v1/universities", params={"q": "нет такого"})).json() == []


async def test_universities_need_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/universities")).status_code == 401


async def test_list_update_delete_space(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    first = (await client.post("/api/v1/spaces", json={"title": "Первый вуз"})).json()
    second = (await client.post("/api/v1/spaces", json={"title": "Второй вуз"})).json()
    listed = (await client.get("/api/v1/spaces")).json()
    assert {s["id"] for s in listed} == {first["id"], second["id"]}

    patched = await client.patch(
        f"/api/v1/spaces/{first['id']}",
        json={"title": " Мой вуз ", "timeZone": "Asia/Yekaterinburg"},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Мой вуз"
    assert patched.json()["timeZone"] == "Asia/Yekaterinburg"
    assert patched.json()["universityId"] == first["universityId"]  # the registry row is untouched

    for bad in ({"timeZone": "Mars/Base"}, {"timeZone": "../../etc/passwd"}, {"title": "   "}):
        r = await client.patch(f"/api/v1/spaces/{first['id']}", json=bad)
        assert r.status_code == 422, bad

    assert (await client.delete(f"/api/v1/spaces/{first['id']}")).status_code == 204
    assert [s["id"] for s in (await client.get("/api/v1/spaces")).json()] == [second["id"]]
    assert (await client.delete(f"/api/v1/spaces/{first['id']}")).status_code == 404


# --- tenant isolation ---------------------------------------------------------------------------


async def test_user_b_cannot_see_or_touch_user_a_spaces(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, alice = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    b, _ = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    space = (await a.post("/api/v1/spaces", json={"title": "Вуз Алисы"})).json()
    sid = space["id"]
    conn_id = await make_connection(tx_sessionmaker, alice, sid)

    assert (await b.get("/api/v1/spaces")).json() == []
    for method, path, kwargs in (
        ("PATCH", f"/api/v1/spaces/{sid}", {"json": {"title": "Взлом"}}),
        ("DELETE", f"/api/v1/spaces/{sid}", {}),
        ("GET", f"/api/v1/spaces/{sid}/sections", {}),
        ("GET", f"/api/v1/spaces/{sid}/connections", {}),
        ("POST", f"/api/v1/spaces/{sid}/connections", {"json": {"url": "https://x.example.org"}}),
    ):
        r = await b.request(method, path, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
        assert r.json()["code"] == "not_found"
    # 404 for a foreign id is indistinguishable from 404 for an id that never existed
    ghost = await b.get(f"/api/v1/spaces/{new_id()}/sections")
    assert (
        ghost.json()["detail"] == (await b.get(f"/api/v1/spaces/{sid}/sections")).json()["detail"]
    )

    # nothing of Alice's changed
    mine = (await a.get("/api/v1/spaces")).json()
    assert [s["title"] for s in mine] == ["Вуз Алисы"]
    async with tx_sessionmaker() as s:
        assert await s.get(Connection, conn_id) is not None


# --- deletion cascades (FR-023) -------------------------------------------------------------------


async def _seed_domain(sm: Sm, user_id: str, connection_id: str, space_id: str) -> None:
    common = {
        "user_id": user_id,
        "connection_id": connection_id,
        "source_url": "https://lms.example.org/x",
        "fetched_at": NOW,
        "seen_at": NOW,
    }
    async with sm() as s:
        s.add(Course(external_id="c1", title="Матан", **common))
        s.add(ScheduleEvent(external_id="e1", title="Лекция", starts_at=NOW, ends_at=NOW, **common))
        s.add(Deadline(external_id="d1", title="Сдать ДЗ", due_at=NOW, **common))
        s.add(
            Document(
                id=new_id(),
                user_id=user_id,
                space_id=space_id,
                connection_id=connection_id,
                kind="file",
                title="Программа",
                fetched_at=NOW,
                hash="0" * 64,
            )
        )
        s.add(
            Fact(
                id=new_id(),
                user_id=user_id,
                space_id=space_id,
                question="q",
                answer="a",
                sources=[],
                confirmed_at=NOW,
                stale_after=NOW,
                confidence="high",
            )
        )
        await s.commit()


async def _count(sm: Sm, model: type, user_id: str) -> int:
    async with sm() as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)  # type: ignore[attr-defined]
            )
            or 0
        )


async def test_deleting_a_space_removes_its_data_secrets_and_nothing_else(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    a, alice = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    _, bob = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    doomed = await make_space(tx_sessionmaker, alice, "Удаляемый")
    kept = await make_space(tx_sessionmaker, alice, "Остаётся")
    bob_space = await make_space(tx_sessionmaker, bob, "Чужой")
    doomed_conn = await make_connection(tx_sessionmaker, alice, doomed)
    kept_conn = await make_connection(tx_sessionmaker, alice, kept)
    bob_conn = await make_connection(tx_sessionmaker, bob, bob_space)
    for uid, cid, sid in (
        (alice, doomed_conn, doomed),
        (alice, kept_conn, kept),
        (bob, bob_conn, bob_space),
    ):
        await _seed_domain(tx_sessionmaker, uid, cid, sid)
        await services.vault.put(uid, cid, SecretKind.PASSWORD, "P@ssw0rd-FIXTURE")
        await services.vault.put(uid, cid, SecretKind.TOKEN, "token-FIXTURE")

    assert (await a.delete(f"/api/v1/spaces/{doomed}")).status_code == 204

    async with tx_sessionmaker() as s:
        assert await s.get(Connection, doomed_conn) is None
        assert await s.get(Connection, kept_conn) is not None
        remaining = (await s.execute(select(SecretRow.connection_id))).scalars().all()
    assert set(remaining) == {kept_conn, bob_conn}  # the doomed connection kept no secret
    assert not await services.vault.has(alice, doomed_conn, SecretKind.PASSWORD)
    assert await services.vault.has(alice, kept_conn, SecretKind.PASSWORD)
    # Alice keeps one space worth of snapshots, Bob keeps all of his
    for model in (Course, ScheduleEvent, Deadline, Document, Fact):
        assert await _count(tx_sessionmaker, model, alice) == 1, model
        assert await _count(tx_sessionmaker, model, bob) == 1, model


# --- sections (FR-014) --------------------------------------------------------------------------


async def test_sections_list_only_kinds_with_data(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, alice = await _login(new_client, tx_sessionmaker, clock, "a@example.com")
    _, bob = await _login(new_client, tx_sessionmaker, clock, "b@example.com")
    space = await make_space(tx_sessionmaker, alice)
    other_space = await make_space(tx_sessionmaker, alice, "Другой")
    conn = await make_connection(tx_sessionmaker, alice, space)
    other_conn = await make_connection(tx_sessionmaker, alice, other_space)
    bob_space = await make_space(tx_sessionmaker, bob)
    bob_conn = await make_connection(tx_sessionmaker, bob, bob_space)

    assert (await a.get(f"/api/v1/spaces/{space}/sections")).json() == []

    await _seed_domain(tx_sessionmaker, alice, conn, space)
    await _seed_domain(tx_sessionmaker, alice, other_conn, other_space)  # another space of hers
    await _seed_domain(tx_sessionmaker, bob, bob_conn, bob_space)
    sections = (await a.get(f"/api/v1/spaces/{space}/sections")).json()
    assert sections == [
        {"kind": "courses", "count": 1},
        {"kind": "scheduleEvents", "count": 1},
        {"kind": "deadlines", "count": 1},
        {"kind": "documents", "count": 1},
    ]
