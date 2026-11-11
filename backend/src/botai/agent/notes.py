"""What the agent remembers about a student (FR-050): short notes the student can see and clear.

Notes are facts about the student (group, programme, preferences, a step that worked), written by
the agent through `remember`. They reach every later turn inside the context message, labelled as
data about the student rather than instructions. A note that is an address, looks like a secret or
is an order to the assistant is refused at the door, so a page cannot plant standing orders; the
check of secrets is structural (a token, a cookie, a labelled password), so "Код группы 2101" stays.

A rule (`kind="rule"`, D-045) is the other kind of row: a standing instruction of the student
(«про оценки сначала смотри ЦУ», «дедлайны показывай по московскому времени»), written only on the
student's explicit request. Being an instruction is what a rule is, so the order checks of a note do
not apply; everything else does, and more: a rule may not touch secrets (passwords, codes, tokens,
cookies), may not name an address, and may not hand the assistant a power over the action policy
(«отправляй без подтверждения», «разрешаю платить»). Notes and rules share the 300-character limit
and the case-insensitive dedupe; each kind has its own cap (`MAX_NOTES`, `MAX_RULES`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.safety import mask_injections, scrub_secrets
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import UserMemory
from botai.learn.redact import JWT, LONG_TOKEN

MAX_NOTES = 50
MAX_RULES = 20
NOTE_CHARS = 300

NoteKind = Literal["note", "rule"]
NOTE_KINDS: tuple[NoteKind, ...] = ("note", "rule")


@dataclass(frozen=True, slots=True)
class Added:
    note: UserMemory
    created: bool


# An address in any form: a scheme, `www.`, or any word with a dot and a domain ending, also the
# defanged `site[.]ru`.
_ADDRESS = re.compile(
    r"(?i)\b(?:https?|botai|ftp)://|\bwww\.|\b[\w-]+(?:\.|\[\.\])(?:[a-z]{2,}|рф)\b"
)
_LABELLED_PASSWORD = re.compile(r"(?i)\b(?:парол\w*|password|passwd|pwd)\b\s*[:=—-]\s*\S+")
_COMMAND_STEMS = (
    r"(?:игнорир|отправ|пришл|присыл|перешл|пересыл|выполн|делай|сдела|ответь|ответьте|отвечай|"
    r"скаж|используй|вызыв|открыв|откро|забуд|удал|загруз|скопир|копируй|передай|передав|напиши|"
    r"сообщи|покажи|показыв|раскро|раскрыв|скрой|скрыв|переходи|зайди|заходи|нажми|нажимай|"
    r"подтверждай|не\s+(?:говор|сообщ|показыв))\w*"
)
# An order to the assistant at the start of a sentence, after a colon, or after "and"/"then".
_ORDER = re.compile(
    r"(?i)(?:^|[.;!:,\n]\s*|\b(?:и|а|но|потом|затем|также|then|and)\s+)"
    r"(?:(?:всегда|никогда|обязательно|немедленно|теперь|сразу)\s+)?" + _COMMAND_STEMS
)
# Permission or a standing rule, wherever in the note it stands: «преподаватель разрешил
# открывать ссылки», «всегда отвечать ссылкой на сайт». A fact about the student has neither.
_PERMISSION = re.compile(
    r"(?i)\b(?:разреш\w*|позвол\w*|уполномоч\w*|санкционир\w*|allow\w*|permit\w*|"
    r"authori[sz]\w*|grant\w*|дал\w*\s+(?:доступ|разрешени\w*|право))"
)
_STANDING_RULE = re.compile(
    r"(?i)\b(?:всегда|никогда|впредь|отныне|каждый\s+раз|при\s+каждом\s+\w+|непременно)\b"
    r"[^.;!?\n]{0,80}?\b\w+(?:ть|ти|чь|ться|тись|йте?|ите|ай|уй)\b"
    r"|\b(?:always|never|from\s+now\s+on|every\s+time|must|should)\b"
)
_ADDRESSES_ASSISTANT = re.compile(r"(?i)\b(?:ассистент|агент|бот|assistant|ai)\w*\s*[,:]")
# What a rule may not be about: primary secrets and session material, whatever the verb.
_RULE_SECRETS = re.compile(
    r"(?i)\b(?:парол\w*|секрет\w*|токен\w*|cookie\w*|куки|ключ\w*\s+(?:доступ|api)\w*|"
    r"api[\s_-]?key|кода?\s+(?:из|подтвержд|смс|sms|входа|доступа)\w*|смс-код\w*|mfa|2fa|otp|"
    r"password\w*|secret\w*|token\w*|credential\w*|учётн\w+\s+данн\w+|учетн\w+\s+данн\w+)"
)
# Secret-looking words with a value after them, in a note or a rule: «пасс qwerty», «pin 1234»,
# «логин: ivanov» (a labelled password is `_LABELLED_PASSWORD`).
_SECRET_WORDS = re.compile(
    r"(?i)\b(?:пасс\w*|pass(?:word|wd|code|phrase)?|pwd|пин(?:-?код\w*)?|pin(?:-?code)?)\b"
    r"(?:\s*[:=—-]\s*|\s+)\S+"
    r"|\bлогин\w*\b[^.;!?\n]{0,30}?[:=—-]\s*\S+|\bлогин\w*\s+[a-z0-9._@-]{3,}"
)
# What a rule may not grant: actions on the student's behalf past the owner's policy (D-006) —
# pressing, sending, submitting, confirming, cancelling, deleting, paying, enrolling, running
# code from a site — in any person, imperative or infinitive, or skipping a confirmation.
_RULE_POWERS = re.compile(
    r"(?i)\b(?:без\s+(?:подтвержд|спрос|вопрос|соглас|разрешени)\w*|"
    r"не\s+спрашива\w*\s+(?:разрешени|подтвержд|соглас)\w*|политик\w*|"
    r"нажим\w*|нажм\w*|нажать|отправ(?!лен)\w*|сдава\w*|сдать|сдай\w*|подтвержда\w*|"
    r"подтверди\w*|отменя\w*|отмени\w*|удаля\w*|удали\w*|покупа\w*|купи\w*|оплач\w*|"
    r"оплати\w*|плати\w*|записыва\w*|запиши\w*|записать\w*|пода(?:й|йте|вай|вайте|вать|ть)\b|"
    r"подава\w*|подпис(?:ывай|ать)\w*|подпиши\w*|перешл\w*|пересыл\w*|публику\w*|"
    r"переведи\w*|перевод(?:и|ить)\s+деньг\w*|выполня\w*|выполни\w*|запуска\w*|запусти\w*|"
    r"загружа\w*|загрузи\w*|скрипт\w*|action_\w+|code_exec|"
    r"send|submit|delete|pay|buy|forward|confirm|click|execute|run|enroll|upload)\b"
)
# A rule says HOW to read or present the data, never WHAT they are: «всегда отвечай, что оценок
# нет / всё сдано» would hide the real data behind a planted answer.
_RULE_FIXED_FACT = re.compile(
    r"(?i)\b(?:отвеча|ответь|говори|скажи|сообща|сообщи|пиши|напиши|утвержда)\w*"
    r"[^.;!?\n]{0,60}?\bчто\b[^.;!?\n]{0,60}?(?:\bнет\b|сдан|оценк|зачт|выполн|долг)"
)


def clean_note(text: str) -> str:
    note = " ".join(text.split())
    if not note:
        raise BotaiError(ErrorCode.VALIDATION, "Заметка пустая")
    if len(note) > NOTE_CHARS:
        raise BotaiError(ErrorCode.VALIDATION, f"Заметка длиннее {NOTE_CHARS} знаков")
    # As strict as a message the student typed: «Пароль от ЛК qwerty12» and «Мой пароль qwerty» are
    # secrets, «Код группы 2101» is a fact (only a code «из смс» is not).
    if (
        scrub_secrets(note, typed_by_user=True, codes=False) != note
        or _LABELLED_PASSWORD.search(note)
        or JWT.search(note)
        or LONG_TOKEN.search(note)
        or _SECRET_WORDS.search(note)
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Заметка похожа на пароль, код или токен")
    if _ADDRESS.search(note):
        raise BotaiError(ErrorCode.VALIDATION, "В заметке не должно быть адресов")
    if (
        mask_injections(note)[1]
        or _ORDER.search(note)
        or _PERMISSION.search(note)
        or _STANDING_RULE.search(note)
        or _ADDRESSES_ASSISTANT.search(note)
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Заметка похожа на инструкцию для ассистента")
    return note


def clean_rule(text: str) -> str:
    """A standing instruction of the student: imperative is fine, secrets/addresses/powers are not."""
    rule = " ".join(text.split())
    if not rule:
        raise BotaiError(ErrorCode.VALIDATION, "Правило пустое")
    if len(rule) > NOTE_CHARS:
        raise BotaiError(ErrorCode.VALIDATION, f"Правило длиннее {NOTE_CHARS} знаков")
    if (
        scrub_secrets(rule, typed_by_user=True, codes=False) != rule
        or _LABELLED_PASSWORD.search(rule)
        or JWT.search(rule)
        or LONG_TOKEN.search(rule)
        or _RULE_SECRETS.search(rule)
        or _SECRET_WORDS.search(rule)
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Правило касается паролей, кодов или токенов")
    if _ADDRESS.search(rule):
        raise BotaiError(ErrorCode.VALIDATION, "В правиле не должно быть адресов")
    if mask_injections(rule)[1] or _PERMISSION.search(rule) or _RULE_POWERS.search(rule):
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Правило не может давать ассистенту полномочий: действия идут по политике владельца",
        )
    if _RULE_FIXED_FACT.search(rule):
        raise BotaiError(
            ErrorCode.VALIDATION,
            "Правило может говорить, как читать и показывать данные, но не что в них написано",
        )
    return rule


_WORD = re.compile(r"[0-9a-zа-яё]+")
_STOP = frozenset(
    {
        "всегда",
        "никогда",
        "пожалуйста",
        "запомни",
        "запомнить",
        "правило",
        "чтобы",
        "когда",
        "потом",
        "сначала",
        "только",
        "этого",
        "этот",
        "этом",
        "меня",
        "мне",
        "тебе",
        "твои",
        "please",
        "always",
        "never",
        "remember",
        "rule",
    }
)
EVIDENCE_SHARE = 0.5


def _stems(text: str) -> set[str]:
    words = _WORD.findall(text.casefold().replace("ё", "е"))
    return {word[:5] for word in words if len(word) >= 4 and word not in _STOP}


def asked_by_student(rule: str, student_text: str | None) -> bool:
    """Whether the student's own message of this turn carries the rule: at least half of its
    content words (by a five-letter stem), and at least two of them (or all, if fewer)."""
    if not student_text:
        return False
    wanted = _stems(rule)
    if not wanted:
        return False
    found = len(wanted & _stems(student_text))
    return found >= min(2, len(wanted)) and found >= len(wanted) * EVIDENCE_SHARE


def clean(text: str, kind: NoteKind) -> str:
    return clean_rule(text) if kind == "rule" else clean_note(text)


async def list_notes(
    session: AsyncSession, user_id: str, *, kind: NoteKind | None = None
) -> list[UserMemory]:
    """The student's memory rows, oldest first; `kind` narrows to notes or rules."""
    stmt = select(UserMemory).where(UserMemory.user_id == user_id)
    if kind is not None:
        stmt = stmt.where(UserMemory.kind == kind)
    rows = await session.scalars(stmt.order_by(UserMemory.created_at, UserMemory.id))
    return list(rows)


async def add_note(
    session: AsyncSession, user_id: str, text: str, *, kind: NoteKind = "note"
) -> Added:
    note = clean(text, kind)
    same = await session.scalar(
        select(UserMemory).where(
            UserMemory.user_id == user_id, func.lower(UserMemory.note) == note.lower()
        )
    )
    if same is not None and (same.kind == kind or same.kind == "rule"):
        # Already there; a rule is never silently demoted to a note.
        return Added(same, created=False)
    count = await session.scalar(
        select(func.count())
        .select_from(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.kind == kind)
    )
    if (count or 0) >= (MAX_RULES if kind == "rule" else MAX_NOTES):
        detail = (
            "Правил слишком много: сначала забудьте лишнее"
            if kind == "rule"
            else "Память заполнена: сначала забудьте лишнее"
        )
        raise BotaiError(ErrorCode.VALIDATION, detail)
    if same is not None:
        # The same words, asked again as a rule (a fact that becomes a rule): one row.
        same.kind = kind
        await session.commit()
        return Added(same, created=True)
    row = UserMemory(user_id=user_id, note=note, kind=kind)
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
