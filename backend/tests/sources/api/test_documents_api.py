"""The documents API: add by link or upload, list, delete; always scoped to the user."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select

from botai.core.clock import FrozenClock
from botai.core.jobs import JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db import models as m
from tests.auth.support import Sm, signup
from tests.fakeuni import pages

Make = Callable[[], httpx.AsyncClient]
XLSX = ("exams.xlsx", pages.exams_xlsx(), pages.XLSX)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def _jobs(sm: Sm) -> list[m.Job]:
    async with sm() as s:
        return list((await s.execute(select(m.Job).order_by(m.Job.id))).scalars())


async def _space(c: httpx.AsyncClient, title: str = "Вуз") -> str:
    return (await c.post("/api/v1/spaces", json={"title": title})).json()["id"]


async def _login_with_space(
    client: httpx.AsyncClient, sm: Sm, clock: FrozenClock
) -> tuple[str, str]:
    acc = await signup(client, sm, clock)
    return acc.id, await _space(client)


# --- access ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/spaces/x/documents"),
        ("POST", "/api/v1/spaces/x/documents/url"),
        ("POST", "/api/v1/spaces/x/documents/upload"),
        ("DELETE", "/api/v1/documents/x"),
    ],
)
async def test_every_route_needs_a_login(client: httpx.AsyncClient, method: str, path: str) -> None:
    r = await client.request(method, path)
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


# --- upload ---------------------------------------------------------------------------------


async def test_upload_stores_an_attachment_and_queues_an_ingest_job_that_reads_it(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    user_id, space = await _login_with_space(client, tx_sessionmaker, clock)
    r = await client.post(
        f"/api/v1/spaces/{space}/documents/upload",
        files={"file": XLSX},
        data={"note": "Мои экзамены"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["documentId"] is None  # the document appears when the job has read it
    (job,) = await _jobs(tx_sessionmaker)
    assert job.id == body["jobId"]
    assert job.kind == JobKind.INGEST
    assert job.connection_id == body["connectionId"]
    payload = job.payload
    async with tx_sessionmaker() as s:
        att = (await s.execute(select(m.Attachment))).scalar_one()
        conn = (await s.execute(select(m.Connection))).scalar_one()
        data = (await s.execute(select(m.Attachment.data))).scalar_one()
    assert payload == {
        "source": {"kind": "upload"},
        "attachment_id": att.id,
        "filename": "exams.xlsx",
        "note": "Мои экзамены",
    }  # the bytes are not in the job
    assert data == XLSX[1]
    assert (att.user_id, att.kind, att.ingest_state) == (user_id, "document", "queued")
    assert (conn.user_id, conn.space_id, conn.kind, conn.status) == (
        user_id,
        space,
        "sheet",
        "draft",
    )
    assert (conn.title, conn.origin, conn.allow_hosts) == (
        "exams",
        "https://upload.botai.local",
        [],
    )


async def test_upload_refuses_what_cannot_be_read(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    _, space = await _login_with_space(client, tx_sessionmaker, clock)
    url = f"/api/v1/spaces/{space}/documents/upload"
    empty = await client.post(url, files={"file": ("a.txt", b"", "text/plain")})
    assert empty.status_code == 422
    too_big = await client.post(
        url, files={"file": ("big.txt", b"x" * (20 * 1024 * 1024 + 1), "text/plain")}
    )
    assert too_big.status_code == 413
    binary = await client.post(
        url, files={"file": ("tool.bin", b"\x7fELF\x00\x00\x01", "application/octet-stream")}
    )
    assert binary.status_code == 422
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
    archive = await client.post(url, files={"file": ("a.zip", buf.getvalue(), "application/zip")})
    assert archive.status_code == 422
    picture = await client.post(url, files={"file": ("a.png", PNG, "image/png")})
    assert picture.status_code == 422  # a picture is for a chat, not for the library of documents
    assert await _jobs(tx_sessionmaker) == []  # nothing was started, nothing is left behind
    async with tx_sessionmaker() as s:
        for model in (m.Connection, m.Attachment):
            assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0


async def test_upload_into_another_users_space_is_not_found(
    client: httpx.AsyncClient,
    new_client: Make,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    await _login_with_space(client, tx_sessionmaker, clock)
    owner_space = (await client.get("/api/v1/spaces")).json()[0]["id"]
    other = new_client()
    await signup(other, tx_sessionmaker, clock)
    r = await other.post(f"/api/v1/spaces/{owner_space}/documents/upload", files={"file": XLSX})
    assert r.status_code == 404
    assert await _jobs(tx_sessionmaker) == []


# --- by link --------------------------------------------------------------------------------


async def test_a_link_starts_a_job_with_a_typed_source(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    _, space = await _login_with_space(client, tx_sessionmaker, clock)
    url = f"/api/v1/spaces/{space}/documents/url"
    plain = await client.post(
        url,
        json={"url": "https://files.uni.example/exams.xlsx", "title": "Экзамены", "note": "список"},
    )
    sheet = await client.post(
        url,
        json={"url": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUv/edit#gid=0"},
    )
    assert plain.status_code == sheet.status_code == 201
    first, second = await _jobs(tx_sessionmaker)
    assert first.payload["source"] == {
        "kind": "url",
        "url": "https://files.uni.example/exams.xlsx",
        "refreshEveryMinutes": 720,
    }
    assert first.payload["note"] == "список"
    assert second.payload["source"]["kind"] == "google_sheet"
    async with tx_sessionmaker() as s:
        conns = {c.id: c for c in (await s.execute(select(m.Connection))).scalars()}
    one, two = conns[first.connection_id or ""], conns[second.connection_id or ""]
    assert (one.title, one.origin, one.allow_hosts, one.kind) == (
        "Экзамены",
        "https://files.uni.example",
        ["files.uni.example"],
        "doc",
    )
    assert one.entry_url == "https://files.uni.example/exams.xlsx"
    # no name given: the kind's name, never the host; the document names it once it is read
    assert (two.title, two.allow_hosts, two.kind) == ("Таблица", ["docs.google.com"], "sheet")


async def test_google_slides_and_drive_links_are_documents_and_a_folder_is_refused(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    _, space = await _login_with_space(client, tx_sessionmaker, clock)
    url = f"/api/v1/spaces/{space}/documents/url"
    slides = await client.post(
        url, json={"url": "https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUv/edit"}
    )
    drive = await client.post(
        url, json={"url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view"}
    )
    folder = await client.post(
        url, json={"url": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv"}
    )
    form = await client.post(url, json={"url": "https://docs.google.com/forms/d/e/1FAIpQ/viewform"})
    assert (slides.status_code, drive.status_code) == (201, 201)
    assert (folder.status_code, form.status_code) == (422, 422)
    assert "не на папку" in folder.json()["detail"]
    jobs = await _jobs(tx_sessionmaker)
    assert [j.payload["source"]["kind"] for j in jobs] == ["url", "url"]
    async with tx_sessionmaker() as s:
        hosts = {
            c.origin: (c.kind, c.allow_hosts)
            for c in (await s.execute(select(m.Connection))).scalars()
        }
    assert hosts == {
        "https://docs.google.com": ("doc", ["docs.google.com"]),
        "https://drive.google.com": ("doc", ["drive.google.com"]),
    }


@pytest.mark.parametrize(
    "link",
    [
        "http://files.uni.example/a.pdf",
        "https://user:pw@files.uni.example/a.pdf",
        "https://127.0.0.1/a.pdf",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/a.pdf",
        "https://printer.local/a.pdf",
        "https://files.uni.example:8443/a.pdf",
        "ftp://files.uni.example/a.pdf",
        "file:///etc/passwd",
        "not a link",
    ],
)
async def test_unsafe_links_are_refused(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, link: str
) -> None:
    _, space = await _login_with_space(client, tx_sessionmaker, clock)
    r = await client.post(f"/api/v1/spaces/{space}/documents/url", json={"url": link})
    assert r.status_code == 422
    assert await _jobs(tx_sessionmaker) == []


# --- list and delete ------------------------------------------------------------------------


async def _document(
    sm: Sm, user_id: str, space_id: str, *, title: str = "Правила"
) -> tuple[str, str]:
    """A finished document with a connection, a spec revision, text and a secret."""
    async with sm() as s:
        conn = m.Connection(
            user_id=user_id,
            space_id=space_id,
            origin="https://files.uni.example",
            kind="document",
            adapter="document",
            title=title,
            status="active",
            allow_hosts=["files.uni.example"],
        )
        s.add(conn)
        await s.flush()
        doc = m.Document(
            user_id=user_id,
            space_id=space_id,
            connection_id=conn.id,
            kind="file",
            title=title,
            url="https://files.uni.example/rules.pdf",
            mime="application/pdf",
            fetched_at=datetime(2026, 10, 27, tzinfo=UTC),
            hash="a" * 64,
        )
        s.add(doc)
        await s.flush()
        s.add(
            m.Chunk(user_id=user_id, document_id=doc.id, ord=0, text="Правила пересдачи", tokens=4)
        )
        s.add(
            m.AdapterSpecRow(
                origin=f"doc:{conn.id}",
                adapter="document",
                revision=1,
                state="active",
                spec={"adapter": "document", "source": {"kind": "upload"}},
            )
        )
        await s.commit()
        return doc.id, conn.id


async def test_list_returns_only_the_users_documents_of_that_space(
    client: httpx.AsyncClient,
    new_client: Make,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    user_id, space = await _login_with_space(client, tx_sessionmaker, clock)
    other_space = await _space(client, "Другой вуз")
    assert (await client.get(f"/api/v1/spaces/{space}/documents")).json() == []
    doc_id, conn_id = await _document(tx_sessionmaker, user_id, space)
    await _document(tx_sessionmaker, user_id, other_space, title="Чужое пространство")

    rows = (await client.get(f"/api/v1/spaces/{space}/documents")).json()
    assert len(rows) == 1
    assert rows[0]["id"] == doc_id
    assert rows[0]["connectionId"] == conn_id
    assert (rows[0]["kind"], rows[0]["title"], rows[0]["mime"]) == (
        "file",
        "Правила",
        "application/pdf",
    )

    stranger = new_client()
    await signup(stranger, tx_sessionmaker, clock)
    assert (await stranger.get(f"/api/v1/spaces/{space}/documents")).status_code == 404


async def test_delete_removes_the_document_its_text_its_spec_and_its_secrets(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    user_id, space = await _login_with_space(client, tx_sessionmaker, clock)
    doc_id, conn_id = await _document(tx_sessionmaker, user_id, space)
    await services.vault.put(user_id, conn_id, SecretKind.API_KEY, "key-FIXTURE")

    r = await client.delete(f"/api/v1/documents/{doc_id}")
    assert r.status_code == 204
    async with tx_sessionmaker() as s:
        for model in (m.Document, m.Chunk, m.Connection):
            assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0
        specs = (await s.execute(select(func.count()).select_from(m.AdapterSpecRow))).scalar_one()
        assert specs == 0
    assert not await services.vault.has(user_id, conn_id, SecretKind.API_KEY)
    assert (await client.delete(f"/api/v1/documents/{doc_id}")).status_code == 404


async def test_deleting_a_document_drops_the_file_it_was_uploaded_as_unless_a_chat_carries_it(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    user_id, space = await _login_with_space(client, tx_sessionmaker, clock)
    library, _ = await _document(tx_sessionmaker, user_id, space, title="Из библиотеки")
    chat, _ = await _document(tx_sessionmaker, user_id, space, title="Из чата")
    async with tx_sessionmaker() as s:
        thread = m.ChatThread(user_id=user_id, title="Чат")
        s.add(thread)
        await s.flush()
        message = m.ChatMessage(user_id=user_id, thread_id=thread.id, role="user", blocks=[])
        s.add(message)
        await s.flush()
        for doc_id, message_id in ((library, None), (chat, message.id)):
            s.add(
                m.Attachment(
                    user_id=user_id,
                    thread_id=thread.id if message_id else None,
                    message_id=message_id,
                    document_id=doc_id,
                    name=f"{doc_id}.txt",
                    mime="text/plain",
                    size=1,
                    kind="document",
                    data=b"x",
                    sha256="0" * 64,
                    ingest_state="indexed",
                )
            )
        await s.commit()
    assert (await client.delete(f"/api/v1/documents/{library}")).status_code == 204
    assert (await client.delete(f"/api/v1/documents/{chat}")).status_code == 204
    async with tx_sessionmaker() as s:
        names = list(await s.scalars(select(m.Attachment.name)))
    assert names == [f"{chat}.txt"]  # the message still shows its file; the library copy is gone


async def test_a_document_of_someone_else_cannot_be_deleted(
    client: httpx.AsyncClient, new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    user_id, space = await _login_with_space(client, tx_sessionmaker, clock)
    doc_id, _ = await _document(tx_sessionmaker, user_id, space)
    stranger = new_client()
    await signup(stranger, tx_sessionmaker, clock)
    assert (await stranger.delete(f"/api/v1/documents/{doc_id}")).status_code == 404
    assert (await client.get(f"/api/v1/spaces/{space}/documents")).json()[0]["id"] == doc_id
