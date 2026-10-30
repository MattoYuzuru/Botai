"""A turn with files: the agent gets an answer even for ten big documents, the words of a file are
data, and a request that cannot fit is told so (L1)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import notes
from botai.agent.chat import TOO_LARGE, UNKNOWN_BUDGET, compose_answer, handle_chat
from botai.agent.env import ChatEnv
from botai.agent.harness import HarnessResult
from botai.attachments import service as attachments
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import ScriptedLlm
from botai.db.models import UserMemory
from tests.agent.rig import Rig, agent_messages, call, final, make_rig, step, text_block
from tests.agent.support import NOW, add_message

Sm = async_sessionmaker[AsyncSession]


async def ask_with_files(
    r: Rig, text: str, files: list[tuple[str, str]]
) -> tuple[InMemoryJobContext, dict[str, object] | None]:
    """The student's message with files: (name, text) pairs stored and bound as the API does."""
    question = await add_message(
        r.sm, r.user_id, r.thread_id, "user", text, at=NOW - timedelta(minutes=1)
    )
    async with r.sm() as session:
        ids = [
            (
                await attachments.create(
                    session, r.user_id, filename=name, content_type="text/plain", data=body.encode()
                )
            ).id
            for name, body in files
        ]
        await attachments.bind_to_message(session, r.user_id, r.thread_id, question, ids)
        await session.commit()
    ctx = InMemoryJobContext(user_id=r.user_id, job_id=new_id())
    out = await handle_chat(ctx, r.services, {"thread_id": r.thread_id, "message_id": question})
    return ctx, out


async def test_ten_big_documents_still_get_an_answer(sm: Sm, user_id: str) -> None:
    llm = ScriptedLlm(queue=[final("Вот что в файлах.")])
    r = await make_rig(sm, user_id, llm)
    files = [(f"d{i}.txt", f"Документ {i}. " + "слово " * 6000) for i in range(10)]
    ctx, out = await ask_with_files(r, "Что в этих файлах?", files)
    assert out is not None
    assert out["stopped"] == "answer"
    assert text_block(ctx).markdown == "Вот что в файлах."
    sent = llm.requests[0].messages[1].content
    assert sent.count("read_document(") == 10  # every one is left to be read on
    assert len(sent) < 50_000  # ten times thirty thousand characters would not have fitted
    (stored,) = await agent_messages(sm, r.thread_id)
    assert stored.blocks[0]["markdown"] == "Вот что в файлах."


async def test_the_words_of_a_file_and_of_the_memory_reach_the_model_as_they_are(
    sm: Sm, user_id: str
) -> None:
    llm = ScriptedLlm(queue=[final("Принято.")])
    r = await make_rig(sm, user_id, llm)
    async with sm() as session:
        session.add(UserMemory(user_id=user_id, note="Код группы 2101"))
        await session.commit()
    body = "Пароль можно сменить в профиле. Код группы 2101. access_token: aB3cD4eF5g"
    await ask_with_files(r, "Посмотри файл", [("memo.txt", body)])
    context = llm.requests[0].messages[1].content
    assert "Пароль можно сменить в профиле" in context  # not «Пароль: [скрыто]»
    assert context.count("Код группы 2101") == 2  # in the memory and in the file
    assert "aB3cD4eF5g" not in context  # a credential by its name is hidden all the same


async def test_after_a_file_was_read_only_the_students_own_words_are_remembered(
    sm: Sm, user_id: str
) -> None:
    llm = ScriptedLlm(
        queue=[
            step(
                call("remember", note="Староста группы Иванов"),
                call("remember", call_id="c2", note="Учусь на втором курсе"),
            ),
            final("Хорошо."),
        ]
    )
    r = await make_rig(sm, user_id, llm)
    ctx, out = await ask_with_files(
        r, "Запомни: учусь на втором курсе", [("list.txt", "Староста группы Иванов")]
    )
    assert out is not None
    async with sm() as session:
        saved = [n.note for n in await notes.list_notes(session, user_id)]
    assert saved == ["Учусь на втором курсе"]  # the file's claim is not a fact about the student
    assert "Запомнил: «Учусь на втором курсе»." in text_block(ctx).markdown


def test_a_request_that_does_not_fit_is_told_so_and_not_blamed_on_the_steps() -> None:
    env = ChatEnv(
        services=None,  # type: ignore[arg-type]
        user_id="u",
        thread_id="t",
        job_id=None,
        space_id=None,
    )
    too_big = HarnessResult(content="", steps=0, stopped="budget", overflow=True)
    out_of_steps = HarnessResult(content="", steps=30, stopped="budget")
    assert compose_answer(too_big, env, NOW)[0] == TOO_LARGE
    assert compose_answer(out_of_steps, env, NOW)[0] == UNKNOWN_BUDGET
    assert TOO_LARGE != UNKNOWN_BUDGET
