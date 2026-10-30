"""Bare identifiers never reach the student; links to the app keep theirs."""

from __future__ import annotations

import pytest

from botai.agent.links import strip_bare_ids

ID = "01M63V34Q8E5R2K7ZT9XWB4HNC"
OTHER = "01M63AE9ZZ6YH8C1D3F5G7J9KM"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (f"Подключаю в чате {ID}.", "Подключаю в чате ."),
        (f"Источник (connection_id={ID}) работает", "Источник работает"),
        (f"connection_id: {ID}, готово", ", готово"),
        (f"Открой [чат](botai://threads/{ID})", f"Открой [чат](botai://threads/{ID})"),
        (
            f"[Smart LMS](botai://connections/{ID}) и [Файл](botai://documents/{OTHER})",
            f"[Smart LMS](botai://connections/{ID}) и [Файл](botai://documents/{OTHER})",
        ),
        (f"Тред botai://threads/{ID}, а не {OTHER}", f"Тред botai://threads/{ID}, а не "),
        (
            "Обычный текст с КАПСОМ и цифрами 2026 без ID",
            "Обычный текст с КАПСОМ и цифрами 2026 без ID",
        ),
    ],
)
def test_bare_ids_are_removed_and_app_links_survive(raw: str, clean: str) -> None:
    assert strip_bare_ids(raw) == clean
