"""What the agent remembers about a student (FR-050): short notes the student can see and clear.

Notes are facts about the student (group, programme, preferences, a step that worked), written by
the agent through `remember`. They reach every later turn inside the context message, labelled as
data about the student rather than instructions. A note that is an address, looks like a secret or
is an order to the assistant is refused at the door, so a page cannot plant standing orders; the
check of secrets is structural (a token, a cookie, a labelled password), so "Код группы 2101" stays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.safety import mask_injections, scrub_secrets
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import UserMemory
from botai.learn.redact import JWT, LONG_TOKEN

MAX_NOTES = 50
NOTE_CHARS = 300


@dataclass(frozen=True, slots=True)
class Added:
    note: UserMemory
    created: bool


_ADDRESS = re.compile(
    r"(?i)\b(?:https?|botai|ftp)://|\bwww\.|\b[a-z0-9-]+\.(?:ru|com|org|net|edu|io|рф|su)\b"
)
_LABELLED_PASSWORD = re.compile(r"(?i)\b(?:парол\w*|password|passwd|pwd)\b\s*[:=—-]\s*\S+")
_ORDER = re.compile(
    r"(?i)(?:^|[.;!:\n]\s*)(?:(?:всегда|никогда|обязательно|немедленно|теперь|сразу)\s+)?"
    r"(?:игнорир|отправ|пришл|перешл|выполн|делай|сдела|ответь|ответьте|скаж|используй|вызыв|"
    r"открыв|откро|забуд|удал|загруз|скопир|передай|напиши|сообщи|не\s+(?:говор|сообщ|показыв))\w*"
)
_ADDRESSES_ASSISTANT = re.compile(r"(?i)\b(?:ассистент|агент|бот|assistant|ai)\w*\s*[,:]")


def clean_note(text: str) -> str:
    note = " ".join(text.split())
    if not note:
        raise BotaiError(ErrorCode.VALIDATION, "Заметка пустая")
    if len(note) > NOTE_CHARS:
        raise BotaiError(ErrorCode.VALIDATION, f"Заметка длиннее {NOTE_CHARS} знаков")
    if (
        scrub_secrets(note) != note
        or _LABELLED_PASSWORD.search(note)
        or JWT.search(note)
        or LONG_TOKEN.search(note)
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Заметка похожа на пароль, код или токен")
    if _ADDRESS.search(note):
        raise BotaiError(ErrorCode.VALIDATION, "В заметке не должно быть адресов")
    if mask_injections(note)[1] or _ORDER.search(note) or _ADDRESSES_ASSISTANT.search(note):
        raise BotaiError(ErrorCode.VALIDATION, "Заметка похожа на инструкцию для ассистента")
    return note


async def list_notes(session: AsyncSession, user_id: str) -> list[UserMemory]:
    rows = await session.scalars(
        select(UserMemory)
        .where(UserMemory.user_id == user_id)
        .order_by(UserMemory.created_at, UserMemory.id)
    )
    return list(rows)


async def add_note(session: AsyncSession, user_id: str, text: str) -> Added:
    note = clean_note(text)
    same = await session.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id, func.lower(UserMemory.note) == note.lower()
        )
    )
    if same is not None:
        return Added(same, created=False)
    count = await session.scalar(
        select(func.count()).select_from(UserMemory).where(UserMemory.user_id == user_id)
    )
    if (count or 0) >= MAX_NOTES:
        raise BotaiError(ErrorCode.VALIDATION, "Память заполнена: сначала забудьте лишнее")
    row = UserMemory(user_id=user_id, note=note)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:  # the same note arrived twice at once
        await session.rollback()
        same = await session.scalar(
            select(UserMemory).where(
                UserMemory.user_id == user_id, func.lower(UserMemory.note) == note.lower()
            )
        )
        if same is None:
            raise
        return Added(same, created=False)
    return Added(row, created=True)


async def delete_note(session: AsyncSession, user_id: str, note_id: str) -> None:
    deleted = await session.execute(
        delete(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.id == note_id)
        .returning(UserMemory.id)
    )
    if deleted.first() is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Заметка не найдена")
    await session.commit()


async def clear_notes(session: AsyncSession, user_id: str) -> None:
    await session.execute(delete(UserMemory).where(UserMemory.user_id == user_id))
    await session.commit()
