"""A clarification changes the task before authoring; the entry URL is actual evidence."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from sqlalchemy import select

from botai.contracts.agent_ui import ConfirmBlock, InputBlock
from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from botai.core.dialogue import DialogueTurn
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.llm import LlmResponse
from botai.db import models as m
from botai.learn.explorer import Explorer
from tests.fakeuni import data
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources.helpers import NOW

ENTRY = f"{data.ORIGIN}/schedule?begin_date=2026-10-26"


class ConversationContext(InMemoryJobContext):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.turns: list[DialogueTurn] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.delivered: set[str] = set()

    async def ask_dialogue(self, block: InputBlock) -> str:
        answer = await self.ask(block)
        return answer.values if isinstance(answer.values, str) else ""

    async def drain_dialogue(self) -> list[DialogueTurn]:
        self.delivered.update(turn.message_id for turn in self.turns)
        return list(self.turns)

    async def checkpoint_learning(self, intent: dict[str, Any]) -> None:
        self.checkpoints.append(copy.deepcopy(intent))
        self.turns = [turn for turn in self.turns if turn.message_id not in self.delivered]
        self.delivered.clear()


def schedule_browser() -> FakeBrowserGateway:
    event = (
        '<article class="event"><h4 class="title">Математический анализ</h4>'
        '<time datetime="2026-10-26T09:30:00+03:00">09:30</time>'
        '<time datetime="2026-10-26T10:50:00+03:00">10:50</time></article>'
    )
    return FakeBrowserGateway(
        {
            ENTRY: FakePage(
                "<body><main>" + "<div>" * 20 + event + "</div>" * 20 + "</main></body>",
                "Расписание",
                requests=[
                    ObservedRequest(
                        method="GET",
                        url=f"{data.ORIGIN}/api/v2/timetable?from=2026-10-26&to=2026-11-01",
                        resource_type="xhr",
                        status=200,
                        response_content_type="application/json",
                        response_sample=json.dumps({"items": data.timetable()}, ensure_ascii=False),
                    )
                ],
            )
        },
        NOW,
        data.ORIGIN,
    )


async def test_default_connect_observes_exact_selected_week_and_does_not_ask_deadlines(
    make_env: EnvFactory,
) -> None:
    env = await make_env([ls.timetable_answer()])
    env.services.browser = schedule_browser()
    result = await env.run(payload={"url": ENTRY})
    assert result["status"] == "ok"
    assert result["kinds"] == ["scheduleEvents"]
    assert [type(b) for b in env.asked] == [ConfirmBlock]
    assert len(env.llm.requests) == 1
    prompt = env.llm.requests[0].messages[1].content
    assert "[page] /schedule?begin_date=2026-10-26" in prompt
    assert 'class="title"' in prompt
    assert "Математический анализ" in prompt
    assert await env.count(m.ScheduleEvent) == 4
    assert await env.count(m.Deadline) == 0
    rows = (await env.db.scalars(select(m.ScheduleEvent))).all()
    assert any(row.starts_at.date().isoformat() == "2026-10-26" for row in rows)


@pytest.mark.parametrize("bootstrap_note", [False, True])
async def test_chat_clarification_excludes_deadline_author_and_questions(
    make_env: EnvFactory, bootstrap_note: bool
) -> None:
    classifier = LlmResponse(
        content=json.dumps(
            {
                "linkIndices": [] if bootstrap_note else [0],
                "kinds": ["scheduleEvents"],
                "absentKinds": ["deadlines"],
                "note": "Эта страница и есть расписание. Дедлайнов здесь нет.",
            },
            ensure_ascii=False,
        )
    )
    env = await make_env([classifier, ls.timetable_answer()])
    env.services.browser = schedule_browser()
    ctx = ConversationContext(
        user_id=env.seeded.user_id,
        connection_id=env.seeded.connection_id,
        responder=lambda block: True if isinstance(block, ConfirmBlock) else None,
    )
    note = f"Эта ссылка и есть расписание: {ENTRY}. Дедлайнов тут нет."
    payload: dict[str, Any] = {"kinds": ["scheduleEvents", "deadlines"]}
    if bootstrap_note:
        payload["learning_intent"] = {
            "entry_urls": [ENTRY],
            "requested_kinds": ["scheduleEvents", "deadlines"],
            "absent_kinds": [],
            "user_note": note,
        }
    else:
        ctx.turns = [DialogueTurn("turn-fixture", note, NOW)]
    result = await env.handlers[JobKind.CONNECT](ctx, env.services, payload)
    assert result["status"] == "ok"
    assert result["kinds"] == ["scheduleEvents"]
    assert [request.purpose for request in env.llm.requests] == ["classify", "learn"]
    assert not any(isinstance(block, InputBlock) for block in ctx.blocks)
    assert "Дедлайнов здесь нет" in env.llm.requests[-1].messages[1].content
    assert ctx.checkpoints[-1]["entry_urls"] == [ENTRY]
    assert ctx.checkpoints[-1]["absent_kinds"] == ["deadlines"]
    assert await env.count(m.Deadline) == 0


async def test_drain_checkpoints_only_after_the_complete_delivered_batch(
    make_env: EnvFactory,
) -> None:
    env = await make_env([])
    ctx = ConversationContext(user_id=env.seeded.user_id, connection_id=env.seeded.connection_id)
    ctx.turns = [
        DialogueTurn("first-fixture", ENTRY, NOW),
        DialogueTurn("second-fixture", f"{data.ORIGIN}/courses", NOW),
    ]
    explorer = Explorer(ctx, env.services)
    explorer._start_intent({})
    assert await explorer._drain(
        await explorer._redactor(await explorer._info(env.seeded.connection_id))
    )
    assert len(ctx.checkpoints) == 1
    assert ctx.checkpoints[0]["entry_urls"] == [ENTRY, f"{data.ORIGIN}/courses"]
    assert ctx.turns == []
