"""The agent's map of the app: real labels only, and a university is created by the agent.

The production incident of 2026-11-01: the chat agent sent the owner to "раздел с вузами -> добавить
вуз" — a section and a button that do not exist — because it neither knew the real UI nor could
create a space. The prompt text is product behaviour here, so it is pinned by tests: `UI_GUIDE`
reaches both user-facing prompts, names the five real tabs and the one real button, and states
plainly that there is no create-university form.
"""

from __future__ import annotations

from botai.agent.ui_map import DISCOVERY_FLOW, UI_GUIDE, UI_MAP, UNIVERSITY_FLOW
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


def test_the_map_mentions_the_panels_inside_study_and_adds_no_tab() -> None:
    flat = " ".join(UI_MAP.split())
    assert "Вверху «Учёбы» блок «Успеваемость»" in flat
    assert "Новых вкладок для них нет" in flat
    assert "Пять вкладок" in UI_MAP  # still five


def test_connect_and_repair_prompts_carry_the_discovery_rules() -> None:
    # REPAIR runs on the same system prompt as CONNECT, so one assertion covers both jobs.
    assert DISCOVERY_FLOW in CONNECT_SYSTEM_PROMPT
    assert DISCOVERY_FLOW in CHAT_SYSTEM_PROMPT
    flat = " ".join(DISCOVERY_FLOW.split())
    assert "Отказ ничего не меняет" in flat
    assert "канарейка обязательна" in flat
    assert "ui_propose_panel" in flat
    assert "panel_list" in flat
    assert "Находки" in " ".join(CONNECT_SYSTEM_PROMPT.split())
