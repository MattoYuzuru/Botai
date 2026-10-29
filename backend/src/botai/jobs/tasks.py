"""Tasks: the long jobs a thread shows as cards (connect, repair, sync, ingest)."""

from __future__ import annotations

TASK_NOUN = {
    "connect": "Подключение",
    "repair": "Починка",
    "sync": "Обновление",
    "ingest": "Документ",
}


def task_title(kind: str, connection_title: str | None) -> str:
    noun = TASK_NOUN.get(kind, "Задача")
    return f"{noun}: {connection_title}" if connection_title else noun
