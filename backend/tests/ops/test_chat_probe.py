"""The pure parts of the chat speed probe (`botai.ops.chat_probe`): text, line, arguments."""

from __future__ import annotations

from botai.ops.chat_probe import ProbeResult, _answer_text, _line, _parser


def test_the_answer_text_joins_only_text_blocks_and_collapses_spaces() -> None:
    blocks = [
        {"type": "thinking", "stage": "Думаю"},
        {"type": "text", "markdown": "Да,  за ДЗ 7\n— 9 из 10."},
        {"type": "text", "markdown": "Обновлено в 14:05."},
    ]
    assert _answer_text(blocks) == "Да, за ДЗ 7 — 9 из 10. Обновлено в 14:05."


def test_the_line_names_the_counters_tools_and_answer() -> None:
    result = ProbeResult(
        question="Мне поставили оценку за ДЗ 7?",
        thread_id="t",
        job_id="j",
        state="done",
        seconds=3.4,
        steps=2,
        llm_seconds=2.9,
        tool_seconds=0.1,
        cost_usd=0.0012,
        tools=["study_data"],
        answer="Да: 9 из 10.",
    )
    line = _line(result)
    assert "3.4 с" in line
    assert "шагов   2" in line
    assert "$0.0012" in line
    assert "инструменты: study_data" in line
    assert line.rstrip().endswith("ответ: Да: 9 из 10.")
    assert "инструменты: нет" in _line(ProbeResult(question="q", thread_id="t", job_id="j"))


def test_the_parser_takes_repeated_questions_and_defaults() -> None:
    args = _parser().parse_args(["--user", "U1", "--question", "а?", "--question", "б?"])
    assert args.question == ["а?", "б?"]
    assert args.timeout == 900.0
    assert args.json is False
    assert args.space is None
