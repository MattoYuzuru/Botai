"""A student's reply shows what it answers: a quote of the question, and the chosen option in words.

A quick reply carries its quote in the message; an answer to a question of a task is written by the
server, which knows the question. A password or a code is only said to be passed. Real PG, no sites.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from botai.agent.memory import history_messages, turn_from_blocks
from botai.contracts.agent_ui import ChoiceBlock, ChoiceOption, ConfirmBlock, InputBlock
from botai.core.ids import new_id
from botai.core.vault import SecretKind
from tests.jobs.env import Env
from tests.jobs.test_conversations import (
    SECRET,
    ctx,
    dialogue_question,
    password_question,
    post,
    start,
    wait_pending,
)

pytestmark = pytest.mark.db

QUESTION = "Какой вариант выбираете?"


async def messages(client: httpx.AsyncClient, thread: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    assert response.status_code == 200, response.text
    return response.json()


def user_texts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row["blocks"][0] for row in rows if row["role"] == "user"]


async def test_a_quick_reply_keeps_the_quote_of_its_question(env: Env) -> None:
    client, _user, thread, _job = await start(env)
    block_id = new_id()
    body = {
        "text": "Да, НИУ ВШЭ",
        "quote": {"text": "Вышка — это НИУ ВШЭ?", "blockId": block_id},
        "clientMessageId": new_id(),
    }
    accepted = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=body)
    assert accepted.status_code == 202, accepted.text
    block = accepted.json()["message"]["blocks"][0]
    assert block["markdown"] == "Да, НИУ ВШЭ"
    assert block["quote"] == {"text": "Вышка — это НИУ ВШЭ?", "blockId": block_id}
    assert user_texts(await messages(client, thread))[0]["quote"]["text"] == "Вышка — это НИУ ВШЭ?"

    again = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=body)
    assert again.status_code == 202
    assert again.json() == accepted.json()
    other = {**body, "quote": {"text": "Совсем другой вопрос"}}
    reused = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=other)
    assert (reused.status_code, reused.json()["code"]) == (409, "request_reused")


async def test_the_quote_is_one_short_plain_line_and_never_a_secret_link(env: Env) -> None:
    client, _user, thread, _job = await start(env)
    long_question = "Подробный вопрос.\n\n" + "слово " * 80
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Первое", "quote": {"text": long_question}},
    )
    assert response.status_code == 202, response.text
    quote = response.json()["message"]["blocks"][0]["quote"]
    assert len(quote["text"]) == 200
    assert quote["text"].endswith("…")
    assert "\n" not in quote["text"]

    # A quote only decorates: one with a secret link is dropped and the message still goes.
    second = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    secret = await client.post(
        f"/api/v1/chat/threads/{second}/messages",
        json={
            "text": "Второе",
            "quote": {"text": "Откройте https://x.test/a?token=abcdefghijklmnop12345"},
        },
    )
    assert secret.status_code == 202, secret.text
    assert "quote" not in secret.json()["message"]["blocks"][0]
    too_long = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Третье", "quote": {"text": "x" * 1001}},
    )
    assert too_long.status_code == 422


async def test_an_answer_in_the_composer_quotes_the_open_question_of_the_task(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    question = dialogue_question(env, "Что подключаем?")
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    await wait_pending(client, thread)
    response = await post(client, thread, "Расписание")
    assert response.status_code == 202, response.text
    await waiting
    block = response.json()["message"]["blocks"][0]
    assert block["markdown"] == "Расписание"
    assert block["quote"] == {"text": "Что подключаем?", "blockId": question.id}


def choice(env: Env, *, mode: str = "single") -> ChoiceBlock:
    return ChoiceBlock(
        id=new_id(),
        at=env.clock.now(),
        question=QUESTION,
        mode=mode,  # type: ignore[arg-type]
        required=True,
        options=[
            ChoiceOption(value="0", label="Вариант 1: 35 занятий"),
            ChoiceOption(value="1", label="Вариант 2: 12 занятий"),
        ],
    )


async def answer(
    client: httpx.AsyncClient, job_id: str, block_id: str, values: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": block_id, "values": values}
    )


async def test_a_choice_answer_shows_the_picked_option_under_the_question(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    block = choice(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask(block))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, block.id, "1")).status_code == 204
    await waiting
    (reply,) = user_texts(await messages(client, thread))
    assert reply["markdown"] == "Вариант 2: 12 занятий"  # the label, not the value "1"
    assert reply["quote"] == {"text": QUESTION, "blockId": block.id}


async def test_multi_choice_and_free_text_answers_are_said_in_words(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    block = choice(env, mode="multi").model_copy(update={"allow_other": True})
    waiting = asyncio.create_task(ctx(env, user, job_id).ask(block))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, block.id, ["0", "Свой вариант"])).status_code == 204
    await waiting
    (reply,) = user_texts(await messages(client, thread))
    assert reply["markdown"] == "Вариант 1: 35 занятий, Свой вариант"


async def test_a_confirm_answer_is_the_label_of_the_pressed_button(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    block = ConfirmBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Всё верно?",
        confirm_label="Да, всё верно",
        cancel_label="Нет, не то",
        details="Нашла **35 занятий**.",
    )
    waiting = asyncio.create_task(ctx(env, user, job_id).ask(block))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, block.id, True)).status_code == 204
    await waiting
    rows = await messages(client, thread)
    asked = next(row for row in rows if row["blocks"][0]["id"] == block.id)
    # What is confirmed reads together with the question in the history.
    assert asked["blocks"][0]["markdown"] == "Нашла **35 занятий**.\n\nВсё верно?"
    (reply,) = user_texts(rows)
    assert reply["markdown"] == "Да, всё верно"
    assert reply["quote"] == {"text": "Всё верно?", "blockId": block.id}


async def test_secret_answers_only_say_they_were_passed_under_the_question(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    password = password_question(env)
    waiting = asyncio.create_task(worker.ask_secret(password))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, password.id, SECRET)).status_code == 204
    assert (await waiting).get_secret_value() == SECRET
    code = InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Код из SMS",
        required=True,
        input_kind="code",
        secret=True,
        autocomplete="one-time-code",
    )
    waiting = asyncio.create_task(worker.ask_secret(code))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, code.id, "424242")).status_code == 204
    await waiting
    login = InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Почта для входа",
        required=True,
        input_kind="email",
        autocomplete="username",
    )
    waiting = asyncio.create_task(worker.ask(login))
    await wait_pending(client, thread)
    assert (await answer(client, job_id, login.id, "ivanov@example.test")).status_code == 204
    await waiting

    rows = await messages(client, thread)
    replies = user_texts(rows)
    assert [(r["markdown"], r["quote"]["text"]) for r in replies] == [
        ("Пароль передан", "Пароль"),
        ("Код передан", "Код из SMS"),
        ("Логин передан", "Почта для входа"),
    ]
    dump = str(rows)
    for value in (SECRET, "424242", "ivanov@example.test"):
        assert value not in dump
    assert not await env.services.vault.has(user, password.id, SecretKind.TRANSIENT)


def test_the_agent_reads_a_reply_after_the_question_it_answers() -> None:
    blocks = [
        {
            "type": "text",
            "id": new_id(),
            "at": "2026-10-30T10:00:00Z",
            "markdown": "Да, всё верно",
            "quote": {"text": "Всё верно?"},
        }
    ]
    turn = turn_from_blocks(new_id(), "user", blocks)
    assert turn is not None
    assert turn.text == "Да, всё верно"  # what the student typed stays itself
    assert turn.quote == "Всё верно?"
    (message,) = history_messages([turn])
    assert message.content == "[В ответ на: «Всё верно?»]\nДа, всё верно"
    plain = turn_from_blocks(new_id(), "user", [{**blocks[0], "quote": None}])
    assert plain is not None
    assert history_messages([plain])[0].content == "Да, всё верно"
