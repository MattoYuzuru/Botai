"""The agent's map of the app: real labels only, and a university is created by the agent.

The production incident of 2026-11-01: the chat agent sent the owner to "раздел с вузами -> добавить
вуз" — a section and a button that do not exist — because it neither knew the real UI nor could
create a space. The prompt text is product behaviour here, so it is pinned by tests: `UI_GUIDE`
reaches both user-facing prompts, names the five real tabs and the one real button, and states
plainly that there is no create-university form.
"""

from __future__ import annotations

from botai.agent.ui_map import UI_GUIDE, UI_MAP, UNIVERSITY_FLOW
from botai.agent.v4.chat import CHAT_SYSTEM_PROMPT
from botai.agent.v4.dispatch import CONNECT_SYSTEM_PROMPT

TABS = ("Сегодня", "Расписание", "Задания", "Чат", "Учёба")


def test_the_map_names_the_real_tabs_and_the_one_real_button() -> None:
    for tab in TABS:
        assert f"«{tab}»" in UI_MAP
    assert "«Учёба» — дом вузов и источников" in UI_MAP
    assert "«Добавить источник»" in UI_MAP
    assert "«Добавить вуз»" in UI_MAP
    assert "«Перечитать»" in UI_MAP
    assert "«Выключить источник»" in UI_MAP
    assert "«Вузов пока нет»" in UI_MAP
    assert "«Подключить вуз»" in UI_MAP
    # The incident's phantom: there is no create-university form in the app.
    assert "Отдельной формы или кнопки «создать вуз» в приложении нет" in UI_MAP
    # Concepts removed from the v4 model must not come back through the prompt.
    assert "Мои материалы" not in UI_GUIDE
    assert "Документы и вложения" not in UI_GUIDE


def test_the_flow_tells_the_agent_to_create_the_university_itself() -> None:
    assert "space_create" in UNIVERSITY_FLOW
    assert "source_create" in UNIVERSITY_FLOW
    assert "Вузы создаёшь ты, а не студент" in UNIVERSITY_FLOW
    # The agent must not send the student to a non-existent screen ("зайти в раздел вузов").
    assert "такого действия у него" in UNIVERSITY_FLOW


def test_both_user_facing_prompts_carry_the_map() -> None:
    assert UI_GUIDE in CHAT_SYSTEM_PROMPT
    assert UI_MAP in CONNECT_SYSTEM_PROMPT
    # The chat prompt also lists the tool that creates the university.
    assert "space_create" in CHAT_SYSTEM_PROMPT
