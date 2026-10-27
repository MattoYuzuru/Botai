"""Deterministic synthetic data. Times are relative to a fixed week so tests are stable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

ORIGIN = "https://fakeuni.test"
HOST = "fakeuni.test"
USERNAME = "student@fakeuni.test"
PASSWORD = "P@ssw0rd-FIXTURE"  # marker grepped by security check C1
OTP = "424242"  # marker grepped by security check C1
WEEK_START = datetime(2026, 10, 26, tzinfo=UTC)  # Monday


def timetable() -> list[dict[str, object]]:
    def ev(i: int, day: int, h: int, m: int, title: str, room: str, kind: str) -> dict[str, object]:
        start = WEEK_START + timedelta(days=day, hours=h - 3, minutes=m)  # MSK = UTC+3
        return {
            "uid": f"ev-{i}",
            "subject": {"name": title},
            "slot": {
                "begin": start.isoformat(),
                "end": (start + timedelta(minutes=80)).isoformat(),
            },
            "place": {"room": room},
            "type": kind,
        }

    return [
        ev(1, 0, 9, 30, "Математический анализ", "А-101", "lecture"),
        ev(2, 0, 11, 10, "Алгоритмы и структуры данных", "Б-204", "seminar"),
        ev(3, 2, 13, 0, "Базы данных", "В-310", "lab"),
        ev(4, 4, 15, 10, "Английский язык", "online", "seminar"),
    ]


def deadlines() -> list[dict[str, object]]:
    return [
        {
            "taskId": "t-1",
            "name": "ДЗ 3: деревья отрезков",
            "due": int((WEEK_START + timedelta(days=2, hours=20)).timestamp()),
            "course": "Алгоритмы и структуры данных",
            "status": "open",
        },
        {
            "taskId": "t-2",
            "name": "Лабораторная 2: индексы",
            "due": int((WEEK_START + timedelta(days=5, hours=20)).timestamp()),
            "course": "Базы данных",
            "status": "open",
        },
    ]
