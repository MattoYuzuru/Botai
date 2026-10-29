"""The spaces/sources/attachments migration on a disposable database only: duplicate spaces are
merged with everything that hangs on them, kinds and titles move to the new vocabulary."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
BEFORE = "e4a91b7c603d"


async def test_duplicate_spaces_are_merged_and_sources_get_human_kinds_and_titles(
    database_url: str,
) -> None:
    name = f"botai_ws4_migration_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)

    async def execute(sql: str, **params: object) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        for user in ("u1", "u2"):
            await execute(
                "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
                "VALUES (:u, :e, 'Fixture', 'x', false, false)",
                u=user,
                e=f"{user}@fakeuni.test",
            )
        await execute(
            "INSERT INTO universities (id,title,short_title,origins,accent_hue,accent_verified) "
            "VALUES ('hse','НИУ ВШЭ','ВШЭ','[]',250,false)"
        )
        spaces = [  # (id, user, university, title, created)
            ("s-old", "u1", "hse", "lk.hse.ru", "2026-10-01"),
            ("s-dup", "u1", "hse", "hse.ru", "2026-10-02"),
            ("m-old", "u1", None, "Материалы", "2026-10-01"),
            ("m-dup", "u1", None, "Ещё", "2026-10-03"),
            ("other", "u2", "hse", "Вышка", "2026-10-01"),
        ]
        for sid, user, uni, title, created in spaces:
            await execute(
                "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile,"
                "created_at) VALUES (:i, :u, :uni, :t, 250, 'Europe/Moscow', '{}', "
                ":c)",
                i=sid,
                u=user,
                uni=uni,
                t=title,
                c=datetime.fromisoformat(created).replace(tzinfo=UTC),
            )
        connections = [  # (id, space, origin, kind, title)
            ("c-portal", "s-old", "https://lk.hse.ru", "portal", "lk.hse.ru"),
            ("c-lms", "s-dup", "https://lms.hse.ru", "lms", "Smart LMS"),
            ("c-mail", "s-dup", "https://mail.hse.ru", "mail", "www.mail.hse.ru"),
            ("c-link", "m-dup", "https://files.example.org", "document", "files.example.org"),
            ("c-file", "m-dup", "https://upload.botai.local", "document", "Конспект"),
            ("c-sheet", "other", "https://docs.google.com", "sheet", "docs.google.com"),
        ]
        for cid, space, origin, kind, title in connections:
            user = "u2" if space == "other" else "u1"
            await execute(
                "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts) "
                "VALUES (:i, :u, :s, :o, :k, :t, 'active', '[]')",
                i=cid,
                u=user,
                s=space,
                o=origin,
                k=kind,
                t=title,
            )
        await execute(
            "INSERT INTO documents (id,user_id,space_id,connection_id,kind,title,fetched_at,hash) "
            "VALUES ('d1','u1','m-dup','c-file','file','Конспект',now(),'0')"
        )
        await execute(
            "INSERT INTO facts (id,user_id,space_id,question,answer,sources,confirmed_at,"
            "stale_after,confidence) VALUES ('f1','u1','s-dup','q','a','[]',now(),now(),'high')"
        )
        await execute(
            "INSERT INTO chat_threads (id,user_id,space_id,title,purpose,connect_context) "
            "VALUES ('t1','u1','m-dup','Чат','chat','{}')"
        )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        # one space per (user, university) and one "no university" space per user: the oldest
        assert await execute("SELECT id, title FROM spaces ORDER BY id") == [
            ("m-old", "Материалы"),
            ("other", "НИУ ВШЭ"),  # a space of a university takes the university's name
            ("s-old", "НИУ ВШЭ"),
        ]
        # everything that hung on a merged space moved to the one that stayed
        assert await execute("SELECT id, space_id FROM connections ORDER BY id") == [
            ("c-file", "m-old"),
            ("c-link", "m-old"),
            ("c-lms", "s-old"),
            ("c-mail", "s-old"),
            ("c-portal", "s-old"),
            ("c-sheet", "other"),
        ]
        assert await execute("SELECT space_id FROM documents") == [("m-old",)]
        assert await execute("SELECT space_id FROM facts") == [("s-old",)]
        assert await execute("SELECT space_id FROM chat_threads") == [("m-old",)]
        # kinds: the new vocabulary; a link to a document is not an uploaded file
        assert await execute("SELECT id, kind FROM connections ORDER BY id") == [
            ("c-file", "document"),
            ("c-link", "doc"),
            ("c-lms", "lms"),
            ("c-mail", "other"),
            ("c-portal", "portal"),
            ("c-sheet", "sheet"),
        ]
        # titles: a bare host becomes the name of the kind, a real name stays
        assert await execute("SELECT id, title FROM connections ORDER BY id") == [
            ("c-file", "Конспект"),
            ("c-link", "Документ"),
            ("c-lms", "Smart LMS"),
            ("c-mail", "Источник"),
            ("c-portal", "Личный кабинет"),
            ("c-sheet", "Таблица"),
        ]
        assert await execute("SELECT entry_url FROM connections WHERE entry_url IS NOT NULL") == []

        # the rules hold from now on
        for sql in (
            "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile) "
            "VALUES ('x1','u1','hse','Дубль',250,'Europe/Moscow','{}')",
            "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile) "
            "VALUES ('x2','u1',NULL,'Дубль',250,'Europe/Moscow','{}')",
        ):
            with pytest.raises(Exception, match="uq_spaces"):
                await execute(sql)

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        assert await execute("SELECT count(*) FROM spaces") == [(3,)]
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT count(*) FROM attachments") == [(0,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
