"""Chat files (FR-044, plan §3.6): upload, type from the bytes, safe serving, owner isolation,
binding to a message, indexing as a document."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from botai.attachments import service
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError
from botai.core.jobs import JobKind
from botai.db import models as m
from tests.auth.support import Sm, make_space, signup
from tests.fakeuni import pages
from tests.sources.test_documents import make_docx, make_pdf, make_xlsx

Make = Callable[[], httpx.AsyncClient]
URL = "/api/v1/attachments"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 24
OGG = b"OggS\x00\x02" + b"\x00" * 24
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 24
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 16
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 28  # MPEG-1 layer III, 128 kbit/s, 44.1 kHz, no tag
UTF16_TSV = "Предмет\tДата\r\nАлгоритмы\t20.12\r\n".encode("utf-16")  # BOM FF FE: Excel's export
UTF16_BE = b"\xfe\xff" + "Предмет\tДата\r\n".encode("utf-16-be")
HEIC = b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00" + b"\x00" * 16
HTML = b"<!doctype html><html><body><script>alert(document.cookie)</script></body></html>"


async def _upload(
    c: httpx.AsyncClient,
    data: bytes,
    name: str = "file.bin",
    content_type: str = "application/octet-stream",
    **form: str,
) -> httpx.Response:
    return await c.post(URL, files={"file": (name, data, content_type)}, data=form)


# --- access ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", URL),
        ("GET", f"{URL}/x"),
        ("GET", f"{URL}/x/content"),
        ("DELETE", f"{URL}/x"),
    ],
)
async def test_every_route_needs_a_login(client: httpx.AsyncClient, method: str, path: str) -> None:
    r = await client.request(method, path)
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


# --- upload and type detection ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "name", "declared", "kind", "mime"),
    [
        (PNG, "x.bin", "application/octet-stream", "image", "image/png"),
        (JPEG, "photo.png", "image/png", "image", "image/jpeg"),  # the bytes decide, not the name
        (GIF, "a.gif", "", "image", "image/gif"),
        (WEBP, "a.webp", "", "image", "image/webp"),
        (WEBM, "voice.webm", "audio/webm;codecs=opus", "audio", "audio/webm"),
        (OGG, "voice.ogg", "audio/ogg", "audio", "audio/ogg"),
        (MP3, "a.mp3", "audio/mpeg", "audio", "audio/mpeg"),
        (MP3_FRAME, "b.mp3", "audio/mpeg", "audio", "audio/mpeg"),
        (UTF16_TSV, "t.tsv", "", "document", "text/tab-separated-values"),  # FF FE is a BOM
        (UTF16_BE, "t.csv", "text/csv", "document", "text/csv"),
        (UTF16_TSV, "notes.txt", "", "document", "text/plain"),
        (M4A, "a.m4a", "audio/mp4", "audio", "audio/mp4"),
        (WAV, "a.wav", "audio/wav", "audio", "audio/wav"),
        (make_pdf("Привет"), "a.pdf", "application/pdf", "document", "application/pdf"),
        (make_docx(), "a.docx", "", "document", pages.DOCX),
        (make_xlsx(), "a.xlsx", "", "document", pages.XLSX),
        (b"a,b\n1,2\n", "t.csv", "text/csv", "document", "text/csv"),
        (b"a\tb\n1\t2\n", "t.tsv", "", "document", "text/tab-separated-values"),
        ("# Заметки\nтекст".encode(), "n.md", "", "document", "text/markdown"),
        ("просто текст".encode(), "n.txt", "text/plain", "document", "text/plain"),
        (HTML, "page.html", "text/html", "document", "text/html"),
    ],
)
async def test_the_type_comes_from_the_bytes(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    data: bytes,
    name: str,
    declared: str,
    kind: str,
    mime: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    r = await _upload(client, data, name, declared)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["kind"], body["mime"], body["size"]) == (kind, mime, len(data))
    assert body["name"] == name
    assert (body["documentId"], body["ingest"]) == (None, "none")
    assert (await client.get(f"{URL}/{body['id']}")).json() == body


@pytest.mark.parametrize(
    ("data", "name", "declared"),
    [
        (b"", "empty.txt", "text/plain"),
        (b"\x7fELF\x00\x00\x01", "tool", "application/x-executable"),
        (HEIC, "photo.heic", "image/heic"),
        (WEBM, "clip.webm", "video/webm"),
        (b"%PDF-1.4 not really", "x.pdf", "application/pdf"),  # a name cannot make bytes a PDF
    ],
)
async def test_what_cannot_be_read_or_shown_is_refused(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    data: bytes,
    name: str,
    declared: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    r = await _upload(client, data, name, declared)
    if name == "x.pdf":  # magic bytes say PDF: it is stored, and reading it fails later, honestly
        assert r.status_code == 201
        return
    assert r.status_code == 422, r.text
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Attachment)) == 0


async def test_a_zip_that_is_not_an_office_file_is_refused(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    import io
    import zipfile

    await signup(client, tx_sessionmaker, clock)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "x")
    assert (await _upload(client, buf.getvalue(), "a.zip", "application/zip")).status_code == 422


async def test_the_size_limit_is_twenty_mebibytes(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    ok = await _upload(client, b"x" * (20 * 1024 * 1024), "big.txt", "text/plain")
    assert ok.status_code == 201
    too_big = await _upload(client, b"x" * (20 * 1024 * 1024 + 1), "bigger.txt", "text/plain")
    assert too_big.status_code == 413
    assert too_big.json()["code"] == "validation"


async def test_a_name_with_a_path_or_nothing_is_cleaned(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    from botai.attachments import kinds

    await signup(client, tx_sessionmaker, clock)
    named = await _upload(client, PNG, "../../etc/passwd.png")
    assert named.json()["name"] == "passwd.png"
    assert kinds.clean_filename("C:\\docs\\a\x00b.pdf") == "ab.pdf"
    blank = await _upload(client, PNG, "folder/")
    assert blank.status_code == 201
    assert blank.json()["name"] == "Изображение"  # nothing left of the name: the kind names it


async def test_a_users_files_have_a_storage_quota(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    monkeypatch.setattr(service, "MAX_USER_BYTES", 100)
    assert (await _upload(client, PNG)).status_code == 201  # 32 bytes
    assert (await _upload(client, JPEG)).status_code == 201  # 64
    assert (await _upload(client, GIF)).status_code == 201  # 96
    full = await _upload(client, PNG)
    assert full.status_code == 413
    assert full.json()["code"] == "quota_exceeded"


async def test_uploads_nobody_used_are_dropped_on_the_next_upload(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    stale = (await _upload(client, PNG, "stale.png")).json()["id"]
    sent = (await _upload(client, JPEG, "sent.jpg")).json()["id"]
    async with tx_sessionmaker() as s:
        thread = m.ChatThread(user_id=acc.id, title="Чат")
        s.add(thread)
        await s.flush()
        message = m.ChatMessage(user_id=acc.id, thread_id=thread.id, role="user", blocks=[])
        s.add(message)
        await s.flush()
        old = datetime.now(UTC) - timedelta(days=2)
        await s.execute(update(m.Attachment).values(created_at=old))
        await s.execute(
            update(m.Attachment)
            .where(m.Attachment.id == sent)
            .values(thread_id=thread.id, message_id=message.id)
        )
        await s.commit()
    await _upload(client, GIF, "fresh.gif")
    assert (await client.get(f"{URL}/{stale}")).status_code == 404
    assert (await client.get(f"{URL}/{sent}")).status_code == 200  # a sent file stays


# --- serving -------------------------------------------------------------------------------


async def test_an_image_is_served_in_place_and_never_cached(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    att = (await _upload(client, PNG, "схема.png", "image/png")).json()
    r = await client.get(f"{URL}/{att['id']}/content")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline;")
    assert "filename*=UTF-8''%D1%81%D1%85%D0%B5%D0%BC%D0%B0.png" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["accept-ranges"] == "bytes"


async def test_html_is_never_served_as_html(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    att = (await _upload(client, HTML, "page.html", "text/html")).json()
    assert att["mime"] == "text/html"  # what it is
    r = await client.get(f"{URL}/{att['id']}/content")
    assert r.headers["content-type"] == "text/plain; charset=utf-8"  # what it is served as
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == HTML


@pytest.mark.parametrize(
    ("data", "name", "content_type", "disposition"),
    [
        (make_pdf("x"), "a.pdf", "application/pdf", "inline"),
        (make_docx(), "a.docx", pages.DOCX, "attachment"),
        (make_xlsx(), "a.xlsx", pages.XLSX, "attachment"),
        (b"a,b\n", "a.csv", "text/csv; charset=utf-8", "inline"),
        (OGG, "v.ogg", "audio/ogg", "inline"),
    ],
)
async def test_documents_and_audio_are_served_with_their_own_type(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    data: bytes,
    name: str,
    content_type: str,
    disposition: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    att = (await _upload(client, data, name)).json()
    r = await client.get(f"{URL}/{att['id']}/content")
    assert r.headers["content-type"] == content_type
    assert r.headers["content-disposition"].startswith(disposition)
    assert r.content == data


async def test_audio_can_be_sought_with_byte_ranges(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    body = OGG + bytes(range(64))
    att = (await _upload(client, body, "v.ogg")).json()
    url = f"{URL}/{att['id']}/content"
    first = await client.get(url, headers={"range": "bytes=0-3"})
    assert (first.status_code, first.content) == (206, b"OggS")
    assert first.headers["content-range"] == f"bytes 0-3/{len(body)}"
    tail = await client.get(url, headers={"range": "bytes=-4"})
    assert (tail.status_code, tail.content) == (206, body[-4:])
    open_ended = await client.get(url, headers={"range": f"bytes={len(body) - 2}-"})
    assert open_ended.content == body[-2:]
    clipped = await client.get(url, headers={"range": f"bytes=0-{len(body) + 100}"})
    assert (clipped.status_code, clipped.content) == (206, body)
    for bad in ("bytes=999-", "bytes=5-2", "bytes=-", "items=0-1", "bytes=a-b"):
        r = await client.get(url, headers={"range": bad})
        assert r.status_code == 416, bad
        assert r.headers["content-range"] == f"bytes */{len(body)}"


# --- isolation ------------------------------------------------------------------------------


async def test_user_b_cannot_see_or_touch_user_a_files(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    await signup(a, tx_sessionmaker, clock, email="a@example.com")
    await signup(b, tx_sessionmaker, clock, email="b@example.com")
    att = (await _upload(a, PNG)).json()["id"]
    ghost = "01J0000000000000000000GHOST"
    for method, path in (
        ("GET", f"{URL}/{att}"),
        ("GET", f"{URL}/{att}/content"),
        ("DELETE", f"{URL}/{att}"),
    ):
        r = await b.request(method, path)
        assert r.status_code == 404, (method, path)
        # the same answer as for an id that never existed
        assert (
            r.json()["detail"]
            == (await b.request(method, path.replace(att, ghost))).json()["detail"]
        )
    assert (await a.get(f"{URL}/{att}/content")).content == PNG  # still there


async def test_a_file_cannot_be_put_in_a_thread_of_someone_else(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    await signup(b, tx_sessionmaker, clock, email="b@example.com")
    async with tx_sessionmaker() as s:
        thread = m.ChatThread(user_id=alice.id, title="Чат")
        s.add(thread)
        await s.commit()
        thread_id = thread.id
    assert (await _upload(b, PNG, threadId=thread_id)).status_code == 404
    mine = await _upload(a, PNG, threadId=thread_id)
    assert mine.status_code == 201
    async with tx_sessionmaker() as s:
        row = await s.get(m.Attachment, mine.json()["id"])
        assert row is not None
        assert (row.thread_id, row.message_id) == (thread_id, None)


# --- delete ---------------------------------------------------------------------------------


async def test_only_an_unsent_file_can_be_deleted(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    free = (await _upload(client, PNG)).json()["id"]
    sent = (await _upload(client, JPEG)).json()["id"]
    async with tx_sessionmaker() as s:
        thread = m.ChatThread(user_id=acc.id, title="Чат")
        s.add(thread)
        await s.flush()
        message = m.ChatMessage(user_id=acc.id, thread_id=thread.id, role="user", blocks=[])
        s.add(message)
        await s.flush()
        await s.execute(
            update(m.Attachment)
            .where(m.Attachment.id == sent)
            .values(thread_id=thread.id, message_id=message.id)
        )
        await s.commit()
    assert (await client.delete(f"{URL}/{free}")).status_code == 204
    assert (await client.get(f"{URL}/{free}")).status_code == 404
    assert (await client.delete(f"{URL}/{free}")).status_code == 404
    refused = await client.delete(f"{URL}/{sent}")
    assert refused.status_code == 409
    assert refused.json()["code"] == "conflict"
    assert (await client.get(f"{URL}/{sent}/content")).content == JPEG


async def test_deleting_a_thread_takes_its_files_with_it(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    async with tx_sessionmaker() as s:
        thread = m.ChatThread(user_id=acc.id, title="Чат")
        s.add(thread)
        await s.commit()
        thread_id = thread.id
    att = (await _upload(client, PNG, threadId=thread_id)).json()["id"]
    async with tx_sessionmaker() as s:
        await s.delete(await s.get(m.ChatThread, thread_id))
        await s.commit()
    assert (await client.get(f"{URL}/{att}")).status_code == 404


# --- binding to a message (for the chat) ---------------------------------------------------------


async def _thread_and_message(sm: Sm, user_id: str) -> tuple[str, str]:
    async with sm() as s:
        thread = m.ChatThread(user_id=user_id, title="Чат")
        s.add(thread)
        await s.flush()
        message = m.ChatMessage(user_id=user_id, thread_id=thread.id, role="user", blocks=[])
        s.add(message)
        await s.commit()
        return thread.id, message.id


async def _make(sm: Sm, user_id: str, data: bytes, name: str) -> str:
    async with sm() as s:
        att = await service.create(s, user_id, filename=name, content_type=None, data=data)
        await s.commit()
        return att.id


async def test_bind_to_message_attaches_the_users_unsent_files_in_order(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    thread_id, message_id = await _thread_and_message(tx_sessionmaker, acc.id)
    first = await _make(tx_sessionmaker, acc.id, PNG, "a.png")
    second = await _make(tx_sessionmaker, acc.id, OGG, "b.ogg")
    async with tx_sessionmaker() as s:
        refs = await service.bind_to_message(
            s, acc.id, thread_id, message_id, [second, first, second]
        )
        await s.commit()
    assert [(r.id, r.name, r.kind, r.mime, r.size) for r in refs] == [
        (second, "b.ogg", "audio", "audio/ogg", len(OGG)),
        (first, "a.png", "image", "image/png", len(PNG)),
    ]
    async with tx_sessionmaker() as s:
        for att_id in (first, second):
            row = await s.get(m.Attachment, att_id)
            assert row is not None
            assert (row.thread_id, row.message_id) == (thread_id, message_id)


async def test_bind_to_message_refuses_foreign_missing_and_sent_files_and_binds_nothing(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="b@example.com")
    thread_id, message_id = await _thread_and_message(tx_sessionmaker, alice.id)
    other_thread, other_message = await _thread_and_message(tx_sessionmaker, alice.id)
    mine = await _make(tx_sessionmaker, alice.id, PNG, "a.png")
    bobs = await _make(tx_sessionmaker, bob.id, PNG, "b.png")
    sent = await _make(tx_sessionmaker, alice.id, JPEG, "c.jpg")
    async with tx_sessionmaker() as s:
        await service.bind_to_message(s, alice.id, other_thread, other_message, [sent])
        await s.commit()

    async def bind(*ids: str, user: str = alice.id, message: str = message_id) -> None:
        async with tx_sessionmaker() as s:
            await service.bind_to_message(s, user, thread_id, message, list(ids))

    with pytest.raises(BotaiError) as foreign:
        await bind(mine, bobs)
    assert foreign.value.status == 404
    with pytest.raises(BotaiError) as missing:
        await bind(mine, "01J0000000000000000000GHOST")
    assert missing.value.status == 404
    with pytest.raises(BotaiError) as already:
        await bind(mine, sent)
    assert already.value.status == 409
    with pytest.raises(BotaiError) as not_mine:
        await bind(mine, user=bob.id)  # alice's message and file, bob asking
    assert not_mine.value.status == 404
    with pytest.raises(BotaiError) as too_many:
        await bind(*[mine] * 1, *[f"01J{i:023d}" for i in range(11)])
    assert too_many.value.status == 422
    async with tx_sessionmaker() as s:
        row = await s.get(m.Attachment, mine)
        assert row is not None
        assert row.message_id is None  # a failed bind leaves everything unbound
    assert await bind() is None  # nothing to bind is fine


# --- indexing as a document (for the chat) ---------------------------------------------------------


async def test_start_ingest_makes_a_document_source_and_queues_the_job_in_the_thread(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    thread_id, _ = await _thread_and_message(tx_sessionmaker, acc.id)
    att_id = await _make(tx_sessionmaker, acc.id, make_docx(), "Правила пересдачи.docx")
    async with tx_sessionmaker() as s:
        att = await service.get(s, acc.id, att_id)
        started = await service.start_ingest(s, att, space, thread_id, now=clock.now())
        await s.commit()
    async with tx_sessionmaker() as s:
        job = (await s.execute(select(m.Job))).scalar_one()
        conn = (await s.execute(select(m.Connection))).scalar_one()
        att = await s.get(m.Attachment, att_id)
    assert att is not None
    assert (job.id, job.kind, job.thread_id, job.user_id) == (
        started.job_id,
        JobKind.INGEST,
        thread_id,
        acc.id,
    )
    assert job.connection_id == conn.id == started.connection_id
    assert job.payload["attachment_id"] == att_id  # the job reads the bytes from the attachment
    assert "content_b64" not in job.payload
    assert (conn.kind, conn.title, conn.space_id, conn.status) == (
        "document",
        "Правила пересдачи",
        space,
        "draft",
    )
    assert att.ingest_state == "queued"
    out = (await client.get(f"{URL}/{att_id}")).json()
    assert out["ingest"] == "queued"
    # one file is indexed once
    async with tx_sessionmaker() as s:
        att = await service.get(s, acc.id, att_id)
        with pytest.raises(BotaiError) as twice:
            await service.start_ingest(s, att, space, thread_id)
    assert twice.value.status == 409


async def test_start_ingest_is_for_documents_in_the_users_own_space_and_thread(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="b@example.com")
    bobs_space = await make_space(tx_sessionmaker, bob.id)
    space = await make_space(tx_sessionmaker, alice.id)
    thread_id, _ = await _thread_and_message(tx_sessionmaker, alice.id)
    picture = await _make(tx_sessionmaker, alice.id, PNG, "a.png")
    doc = await _make(tx_sessionmaker, alice.id, make_docx(), "a.docx")
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError) as image:
            await service.start_ingest(s, await service.get(s, alice.id, picture), space, None)
        assert image.value.status == 422
        with pytest.raises(BotaiError) as foreign_space:
            await service.start_ingest(s, await service.get(s, alice.id, doc), bobs_space, None)
        assert foreign_space.value.status == 404
        bobs_thread, _ = await _thread_and_message(tx_sessionmaker, bob.id)
        with pytest.raises(BotaiError) as foreign_thread:
            await service.start_ingest(s, await service.get(s, alice.id, doc), space, bobs_thread)
        assert foreign_thread.value.status == 404
        assert await s.scalar(select(func.count()).select_from(m.Job)) == 0
        assert thread_id


async def test_extract_text_reads_a_document_for_the_agent(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    docx = await _make(tx_sessionmaker, acc.id, make_docx(), "rules.docx")
    sheet = await _make(tx_sessionmaker, acc.id, make_xlsx(), "exams.xlsx")
    picture = await _make(tx_sessionmaker, acc.id, PNG, "a.png")
    voice = await _make(tx_sessionmaker, acc.id, OGG, "v.ogg")
    broken = await _make(tx_sessionmaker, acc.id, b"%PDF-1.4 garbage", "scan.pdf")
    async with tx_sessionmaker() as s:
        whole = await service.extract_text(s, acc.id, docx, 10_000)
        short = await service.extract_text(s, acc.id, docx, 20)
        table = await service.extract_text(s, acc.id, sheet, 10_000)
        image = await service.extract_text(s, acc.id, picture, 10_000)
        audio = await service.extract_text(s, acc.id, voice, 10_000)
        bad = await service.extract_text(s, acc.id, broken, 10_000)
    assert whole.kind == "document"
    assert "Правила пересдачи" in whole.text
    assert "до 5 ноября" in whole.text  # a table of the document is part of its text
    assert not whole.truncated
    assert len(short.text) == 20
    assert short.truncated
    assert "Базы данных" in table.text
    assert (image.text, image.kind, audio.text, audio.kind) == ("", "image", "", "audio")
    assert (bad.text, bad.kind) == ("", "document")
    assert bad.error  # said honestly, not an exception in the middle of a chat turn


async def test_extract_text_of_an_indexed_document_reads_its_stored_text(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    att_id = await _make(tx_sessionmaker, acc.id, make_docx(), "rules.docx")
    async with tx_sessionmaker() as s:
        doc = m.Document(
            user_id=acc.id,
            space_id=space,
            kind="file",
            title="rules",
            fetched_at=clock.now(),
            hash="0" * 64,
        )
        s.add(doc)
        await s.flush()
        for i, text in enumerate(["первый кусок", "второй кусок"]):
            s.add(m.Chunk(user_id=acc.id, document_id=doc.id, ord=i, text=text, tokens=2))
        await s.execute(
            update(m.Attachment)
            .where(m.Attachment.id == att_id)
            .values(document_id=doc.id, ingest_state="indexed")
        )
        await s.commit()
    async with tx_sessionmaker() as s:
        got = await service.extract_text(s, acc.id, att_id, 10_000)
    assert got.text == "первый кусок\nвторой кусок"  # the stored text, in order
    assert (await client.get(f"{URL}/{att_id}")).json()["ingest"] == "indexed"


async def test_extract_text_of_someone_elses_file_is_not_found(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="b@example.com")
    att_id = await _make(tx_sessionmaker, alice.id, make_docx(), "rules.docx")
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError) as exc:
            await service.extract_text(s, bob.id, att_id, 100)
    assert exc.value.status == 404


# --- a queued flag is only as good as the job behind it -----------------------------------------------


async def _queued_without_a_job(sm: Sm, user_id: str, name: str = "rules.docx") -> str:
    """What a job cancelled before it started, or one that died, leaves behind."""
    att_id = await _make(sm, user_id, make_docx(), name)
    async with sm() as s:
        await s.execute(
            update(m.Attachment).where(m.Attachment.id == att_id).values(ingest_state="queued")
        )
        await s.commit()
    return att_id


async def _ingest_job(sm: Sm, user_id: str, attachment_id: str, state: str) -> None:
    async with sm() as s:
        s.add(
            m.Job(
                user_id=user_id,
                kind="ingest",
                state=state,
                payload={"attachment_id": attachment_id},
                run_after=datetime.now(UTC),
            )
        )
        await s.commit()


async def test_a_queued_file_without_a_live_job_is_failed_deletable_and_can_be_indexed_again(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    stuck = await _queued_without_a_job(tx_sessionmaker, acc.id)
    cancelled = await _queued_without_a_job(tx_sessionmaker, acc.id, "other.docx")
    await _ingest_job(tx_sessionmaker, acc.id, cancelled, "cancelled")  # finished: not alive
    for att_id in (stuck, cancelled):
        assert (await client.get(f"{URL}/{att_id}")).json()["ingest"] == "failed"
    async with tx_sessionmaker() as s:
        started = await service.start_ingest(s, await service.get(s, acc.id, stuck), space, None)
        await s.commit()
    assert started.job_id
    assert (await client.get(f"{URL}/{stuck}")).json()["ingest"] == "queued"  # a job lives now
    assert (await client.delete(f"{URL}/{cancelled}")).status_code == 204


@pytest.mark.parametrize("state", ["queued", "running", "waiting_user"])
async def test_a_file_with_a_live_ingest_job_is_busy(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, state: str
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    busy = await _queued_without_a_job(tx_sessionmaker, acc.id)
    await _ingest_job(tx_sessionmaker, acc.id, busy, state)
    assert (await client.get(f"{URL}/{busy}")).json()["ingest"] == "queued"
    refused = await client.delete(f"{URL}/{busy}")
    assert (refused.status_code, refused.json()["code"]) == (409, "conflict")
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError) as twice:
            await service.start_ingest(s, await service.get(s, acc.id, busy), space, None)
    assert twice.value.status == 409


async def test_the_cleanup_of_unused_uploads_keeps_a_file_that_is_being_indexed(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    busy = await _queued_without_a_job(tx_sessionmaker, acc.id, "busy.docx")
    dead = await _queued_without_a_job(tx_sessionmaker, acc.id, "dead.docx")
    idle = await _make(tx_sessionmaker, acc.id, PNG, "idle.png")
    await _ingest_job(tx_sessionmaker, acc.id, busy, "running")
    async with tx_sessionmaker() as s:
        await s.execute(
            update(m.Attachment).values(created_at=datetime.now(UTC) - timedelta(days=3))
        )
        await s.commit()
    assert (await _upload(client, GIF, "fresh.gif")).status_code == 201
    assert (await client.get(f"{URL}/{busy}")).status_code == 200  # in use: kept
    assert (await client.get(f"{URL}/{dead}")).status_code == 404  # a flag with no job: cleaned
    assert (await client.get(f"{URL}/{idle}")).status_code == 404


async def test_a_utf16_text_is_read_not_taken_for_audio_or_binary(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    tsv = await _make(tx_sessionmaker, acc.id, UTF16_TSV, "exams.tsv")
    async with tx_sessionmaker() as s:
        table = await service.extract_text(s, acc.id, tsv, 10_000)
    assert table.error is None
    assert "Предмет: Алгоритмы; Дата: 20.12" in table.text  # decoded, and read as a table
    # without a byte order mark, UTF-16 is just binary with NULs: not a file we can read
    plain = "Заметка про пересдачу".encode("utf-16-le")
    refused = await _upload(client, plain, "n.txt", "text/plain")
    assert refused.status_code == 422
