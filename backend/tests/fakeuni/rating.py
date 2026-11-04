"""Owner: S4 (D-039). Synthetic grades, student card and rating boards of the personal cabinet.

Three logged-in JSON endpoints for the panels tests (all names and numbers are made up):

* `/api/v2/grades`: grades with a scale in several spellings, a grade in words, a placeholder
  ("None") and one without a scale: only the plain number-with-scale ones get `valueNum`;
* `/api/v2/profile`: the fields of the student's card, including a field that holds a secret;
* `/api/v2/ratings`: two boards, the student's own place marked with `me`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tests.fakeuni.login import is_logged_in

router = APIRouter()


def _unauth() -> JSONResponse:
    return JSONResponse(
        {"title": "Unauthorized", "status": 401},
        status_code=401,
        media_type="application/problem+json",
    )


def grades() -> list[dict[str, object]]:
    return [
        {
            "id": "g-1",
            "name": "Математический анализ: коллоквиум",
            "course": "mat-an",
            "mark": "8",
            "scale": "10 баллов",
            "date": "2026-10-12T10:00:00+03:00",
        },
        {
            "id": "g-2",
            "name": "Алгоритмы: контрольная",
            "course": "algo",
            "mark": "8,5",
            "scale": "из 10",
            "date": "2026-10-19T10:00:00+03:00",
        },
        {
            "id": "g-3",
            "name": "Базы данных: лабораторная",
            "course": "db",
            "mark": "92",
            "scale": "100",
            "date": "2026-10-22T10:00:00+03:00",
        },
        {
            "id": "g-4",
            "name": "Английский язык: зачёт",
            "course": "en",
            "mark": "зачтено",
            "scale": "зачёт/незачёт",
            "date": "2026-10-23T10:00:00+03:00",
        },
        {
            "id": "g-5",
            "name": "Философия: эссе",
            "course": "phil",
            "mark": "None",
            "scale": "10",
            "date": "2026-10-24T10:00:00+03:00",
        },
        {
            "id": "g-6",
            "name": "Физика: тест",
            "course": "phys",
            "mark": "7",
            "scale": None,
            "date": None,
        },
    ]


def profile() -> list[dict[str, object]]:
    return [
        {"key": "group", "caption": "Группа", "text": "ПМ-31", "section": "Обучение"},
        {"key": "programme", "caption": "Программа", "text": "Прикладная математика"},
        {"key": "year", "caption": "Курс", "text": 3, "section": "Обучение"},
        {"key": "status", "caption": "Статус", "text": "Учится", "section": "Обучение"},
        {"key": "portal_password", "caption": "Пароль от портала", "text": "never-store-me"},
    ]


def ratings() -> list[dict[str, object]]:
    def row(
        slug: str, board: str, place: int, who: str, score: str, *, me: bool = False
    ) -> dict[str, object]:
        return {
            "id": f"{slug}-{place}",
            "board": board,
            "place": place,
            "who": who,
            "score": score,
            "me": me,
            "of": 120,
            "period": "Осень 2026",
        }

    flow, group = "Рейтинг потока", "Рейтинг группы"
    return [
        row("flow", flow, 1, "Участник 17", "96,4"),
        row("flow", flow, 2, "Участник 08", "94,0"),
        row("flow", flow, 3, "Участник 31", "92,5"),
        row("flow", flow, 14, "Иванова А.", "81,2", me=True),
        row("group", group, 1, "Иванова А.", "88,0", me=True),
        row("group", group, 2, "Участник 05", "85,5"),
    ]


@router.get("/api/v2/grades", response_model=None)
async def grades_list(request: Request) -> Response | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    return {"items": grades()}


@router.get("/api/v2/profile", response_model=None)
async def profile_list(request: Request) -> Response | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    return {"items": profile()}


@router.get("/api/v2/ratings", response_model=None)
async def ratings_list(request: Request) -> Response | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    return {"items": ratings()}
