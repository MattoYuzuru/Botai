"""Tools of the conversation itself: memory, plan, quick replies, the title of the thread."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, update

from botai.agent import notes
from botai.agent.env import ChatEnv, clean, spec
from botai.contracts.agent_ui import PlanBlock, PlanStep
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.db.models import ChatMessage, ChatThread
from botai.jobs import dialogue

PLAN_STATES = ("pending", "active", "done", "skipped")
MAX_STEPS = 12
MAX_CHOICES = 4
CHOICE_CHARS = 60
TITLE_CHARS = 60


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.casefold()) if len(w) >= 3}


def _said_by_the_student(note: str, question: str) -> bool:
    """Most of the note's own words are in what the student wrote in this question."""
    wanted = _words(note)
    return bool(wanted) and len(wanted & _words(question)) >= 0.8 * len(wanted)


async def remember(env: ChatEnv, args: dict[str, Any]) -> str:
    note = str(args.get("note", ""))
    if env.outside_read and not _said_by_the_student(note, env.public_question):
        return (
            "Не запомнено: в этом ходе вы уже читали страницу или документ, а запоминать можно "
            "только то, что студент сказал сам в этом вопросе."
        )
    async with env.services.db() as session:
        try:
            added = await notes.add_note(session, env.user_id, note)
        except BotaiError as exc:
            return f"Не запомнено: {exc.detail}."
    if added.created:
        env.remembered.append(added.note.note)  # shown under the answer: the student sees it
    return "Запомнено." if added.created else "Эта заметка уже есть в памяти."


async def forget(env: ChatEnv, args: dict[str, Any]) -> str:
    async with env.services.db() as session:
        try:
            await notes.delete_note(session, env.user_id, str(args.get("note_id", "")).strip())
        except BotaiError:
            return "Такой заметки нет. Возьмите note_id из блока «Память» в контексте."
    return "Забыто."


async def update_plan(env: ChatEnv, args: dict[str, Any]) -> str:
    title = clean(str(args.get("title", "")), 200)
    raw = args.get("steps")
    if not title or not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
        return f"Ошибка аргументов: нужны title и от 1 до {MAX_STEPS} шагов."
    steps: list[PlanStep] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return "Ошибка аргументов: шаг — объект с title и state."
        state = str(item.get("state") or "pending")
        step_title = clean(str(item.get("title", "")), 200)
        if state not in PLAN_STATES or not step_title:
            return (
                f"Ошибка аргументов: у шага нужен title, state — одно из {', '.join(PLAN_STATES)}."
            )
        steps.append(PlanStep(id=f"s{index}", title=step_title, state=state))  # type: ignore[arg-type]
    now = env.services.clock.now()
    async with env.services.db() as session:
        if env.plan_id is None and env.job_id is not None:
            # A retried turn keeps the plan its first run published instead of adding another.
            earlier = await session.scalar(
                select(ChatMessage.blocks[0]["id"].astext).where(
                    ChatMessage.user_id == env.user_id,
                    ChatMessage.thread_id == env.thread_id,
                    ChatMessage.job_id == env.job_id,
                    ChatMessage.role == "agent",
                    ChatMessage.blocks[0]["type"].astext == "plan",
                )
            )
            env.plan_id = earlier
        plan_id = env.plan_id = env.plan_id or new_id()
        block = PlanBlock(id=plan_id, at=now, title=title, steps=steps)
        data = block.model_dump(by_alias=True, mode="json", exclude_none=True)
        message = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.user_id == env.user_id,
                ChatMessage.thread_id == env.thread_id,
                ChatMessage.role == "agent",
                ChatMessage.blocks[0]["id"].astext == plan_id,
            )
        )
        if message is None:
            session.add(
                ChatMessage(
                    id=new_id(),
                    user_id=env.user_id,
                    thread_id=env.thread_id,
                    role="agent",
                    blocks=[data],
                    job_id=env.job_id,
                    created_at=await dialogue.message_time(
                        session, env.user_id, env.thread_id, now
                    ),
                )
            )
        else:
            message.blocks = [data]  # the plan keeps its place in the history
        await session.commit()
    if env.ctx is not None:
        await env.ctx.emit(block)
    return "План показан студенту. Вызывайте update_plan снова, когда меняется состояние шагов."


async def offer_choices(env: ChatEnv, args: dict[str, Any]) -> str:
    raw = args.get("options")
    if not isinstance(raw, list):
        return "Ошибка аргументов: options — список строк."
    options = list(
        dict.fromkeys(clean(str(o), CHOICE_CHARS) for o in raw if clean(str(o), CHOICE_CHARS))
    )[:MAX_CHOICES]
    if not options:
        return "Ошибка аргументов: нужен хотя бы один вариант."
    env.quick_replies = options
    return "Быстрые ответы появятся под вашим ответом. Сам ответ должен задавать вопрос."


async def rename_thread(env: ChatEnv, args: dict[str, Any]) -> str:
    title = clean(str(args.get("title", "")), TITLE_CHARS)
    if not title:
        return "Ошибка аргументов: нужен непустой title."
    async with env.services.db() as session:
        await session.execute(
            update(ChatThread)
            .where(ChatThread.id == env.thread_id, ChatThread.user_id == env.user_id)
            .values(title=title)
        )
        await session.commit()
    return "Название чата обновлено."


_STEP = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Что делается на шаге"},
        "state": {"type": "string", "enum": list(PLAN_STATES)},
    },
    "required": ["title", "state"],
}
SPECS = {
    "remember": spec(
        "remember",
        "Запомнить факт о студенте надолго: группа, программа, курс, предпочтения, полезный "
        "шаг. Коротко (до 300 знаков), только то, что студент сам сказал или что подтверждено. "
        "Не пароли и не чужие данные.",
        {"note": {"type": "string"}},
        ["note"],
    ),
    "forget": spec(
        "forget",
        "Забыть заметку памяти. note_id — из блока «Память» в контексте.",
        {"note_id": {"type": "string"}},
        ["note_id"],
    ),
    "update_plan": spec(
        "update_plan",
        "Показать студенту план ответа на сложный вопрос и обновлять его по ходу: каждый вызов "
        "заменяет план целиком, состояния шагов — pending, active, done, skipped.",
        {
            "title": {"type": "string"},
            "steps": {"type": "array", "items": _STEP, "minItems": 1, "maxItems": MAX_STEPS},
        },
        ["title", "steps"],
    ),
    "offer_choices": spec(
        "offer_choices",
        "Кнопки быстрых ответов под вашим финальным текстом (до 4, каждый до 60 знаков): "
        "когда спрашиваете студента о выборе или подтверждении. Нажатие отправит текст кнопки.",
        {"options": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CHOICES}},
        ["options"],
    ),
    "rename_thread": spec(
        "rename_thread",
        "Дать чату короткое название по сути разговора (до 60 знаков).",
        {"title": {"type": "string"}},
        ["title"],
    ),
}
STAGES = {
    "remember": "Запоминаю",
    "forget": "Забываю",
    "update_plan": "Составляю план",
    "offer_choices": "Готовлю варианты ответа",
    "rename_thread": "Называю чат",
}
IMPLS = {
    "remember": remember,
    "forget": forget,
    "update_plan": update_plan,
    "offer_choices": offer_choices,
    "rename_thread": rename_thread,
}
