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


def test_the_prompts_carry_the_fast_answers_corrections_and_parser_rules() -> None:
    from botai.agent.v4.chat import CORRECTION_FLOW, FAST_ANSWERS

    assert FAST_ANSWERS in CHAT_SYSTEM_PROMPT
    assert CORRECTION_FLOW in CHAT_SYSTEM_PROMPT
    fast = " ".join(FAST_ANSWERS.split())
    assert "ОДИН вызов study_data" in fast
    assert "тот же один вызов с refresh=true" in fast
    assert "сразу отвечай по сохранённым данным" in fast
    assert "не чини источник, не ходи на сайт, не запускай код и не читай трассы" in fast
    assert "Не вызывай source_list, threads_search, rag_search" in fast
    assert "нужной записи нет и после успешного refresh" in fast
    assert "предложи расширить playbook" in fast
    assert len(CHAT_SYSTEM_PROMPT) < 9_500, "every character is paid on every step"
    corrections = " ".join(CORRECTION_FLOW.split())
    for tool in ("study_mark_task", "study_status_rule", "source_update_playbook"):
        assert tool in corrections
    assert "memory_remember(kind=rule)" in corrections
    # The tool sentence names the new tools; the old «сначала получи их инструментом» is gone.
    head = " ".join(CHAT_SYSTEM_PROMPT.split("Правила:")[0].split())
    assert "study_data" in head
    assert "study_status_rule, study_mark_task" in head
    # Connect and repair must read the guides and act on the warnings before finishing.
    discovery = " ".join(DISCOVERY_FLOW.split())
    assert "guides в source_schema (grades, task_status, links)" in discovery
    assert "assignmentId" in discovery
    assert "Подключаешь, расширяешь или чинишь источник — не заканчивай" in discovery
    assert "При обычном вопросе о данных warnings не повод чинить" in discovery
    assert DISCOVERY_FLOW in CONNECT_SYSTEM_PROMPT


def test_the_map_names_the_real_study_and_settings_labels() -> None:
    flat = " ".join(UI_MAP.split())
    for label in (
        "«Успеваемость / Источники»",
        "/study?view=grades",
        "/study?view=sources",
        "«Новое», «Предметы»",
        "«Показатели», «Зачётка», «Панели»",
        "«Открыть задание», «Отметить сданным», «Вернуть в работу», «Снять мою отметку»",
        "«Что помнит агент»: «Правила для агента», «Правила статусов», «Заметки»",
    ):
        assert label in flat, label
    # Links to connections and documents open the sources view of «Учёба».
    assert "botai://connections/… и botai://documents/… открывают этот вид" in flat
