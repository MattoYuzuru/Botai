"""The body of an upload is capped before it is read: no gigabytes of multipart in a temp file."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import func, select

from botai.attachments.limits import BODY_LIMIT
from botai.core.clock import FrozenClock
from botai.db import models as m
from tests.auth.support import Sm, make_space, signup

MIB = 1024 * 1024
BOUNDARY = "botai-test-boundary"


def _body_chunks(total: int) -> list[bytes]:
    """A multipart body of `total` bytes in pieces of 64 KiB."""
    head = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    filler = b"x" * (total - len(head) - len(tail))
    pieces = [filler[i : i + 64 * 1024] for i in range(0, len(filler), 64 * 1024)]
    return [head, *pieces, tail]


async def _stream(chunks: list[bytes], seen: list[int]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        seen.append(len(chunk))
        yield chunk


CHUNKED = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}


@pytest.mark.parametrize("path", ["/api/v1/attachments", "/api/v1/spaces/{space}/documents/upload"])
async def test_a_declared_oversized_body_is_refused_without_being_read(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, path: str
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    big = await client.post(
        path.format(space=space), files={"file": ("big.txt", b"x" * (23 * MIB), "text/plain")}
    )
    assert big.status_code == 413
    assert big.headers["content-type"] == "application/problem+json"
    assert big.json()["code"] == "validation"
    assert "20 МБ" in big.json()["detail"]
    assert big.headers["connection"] == "close"
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Attachment)) == 0


@pytest.mark.parametrize("path", ["/api/v1/attachments", "/api/v1/spaces/{space}/documents/upload"])
async def test_a_body_that_declares_no_length_is_counted_and_cut_off(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, path: str
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, acc.id)
    chunks, sent = _body_chunks(40 * MIB), []
    r = await client.post(path.format(space=space), content=_stream(chunks, sent), headers=CHUNKED)
    assert r.status_code == 413
    assert r.json()["code"] == "validation"
    assert sum(sent) <= BODY_LIMIT + 2 * 64 * 1024  # it stopped reading soon after the limit
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Attachment)) == 0


async def test_a_streamed_body_within_the_limit_is_an_ordinary_upload(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    r = await client.post(
        "/api/v1/attachments", content=_stream(_body_chunks(3 * MIB), []), headers=CHUNKED
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "document"


async def test_other_routes_are_not_limited(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    r = await client.post(
        "/api/v1/auth/login", json={"email": "a@b.c", "password": "x" * (3 * MIB)}
    )
    assert r.status_code != 413
    # and a GET of an upload route is not an upload
    assert (await client.get("/api/v1/attachments")).status_code != 413
