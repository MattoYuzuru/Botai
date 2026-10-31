"""A client-side cabinet whose data exists only behind its JSON API.

`/client-cabinet/app` serves a shell with an empty `#app` div and a script that takes a bearer from
localStorage, calls `/client-cabinet/api/schedule` and draws the classes. Without JavaScript the
page has no data at all: a runner that parsed the HTML of this page would honestly find nothing.
The API is the product here, exactly like the SPA of a real university cabinet.

The table below is deliberately small and deterministic; counters expose what the page really asked
for, so a test can prove the runner reused the observed API answer instead of fetching it again.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/client-cabinet")

CLIENT_TOKEN = "client-cabinet-bearer-FIXTURE-DO-NOT-LEAK"  # marker grepped by security check C1
STORAGE_KEY = "client_token"
CLASSES = (
    (
        "cc-1",
        "Теория вероятностей",
        "2026-10-27T09:00:00+03:00",
        "2026-10-27T10:20:00+03:00",
        "А-101",
    ),
    (
        "cc-2",
        "Архитектура компьютера",
        "2026-10-28T11:10:00+03:00",
        "2026-10-28T12:30:00+03:00",
        "Б-204",
    ),
)


@dataclass
class ClientState:
    schedule_reads: int = 0  # API answers given to the page
    refused: int = 0  # API calls without the bearer
    track_writes: int = 0  # POSTs from the page's analytics

    def reset(self) -> None:
        self.schedule_reads = self.refused = self.track_writes = 0


STATE = ClientState()


def _authorized(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bearer {CLIENT_TOKEN}"


@router.get("/app", response_class=HTMLResponse)
async def app_shell() -> HTMLResponse:
    """Public shell, like a real application: the data is only behind the API."""
    return HTMLResponse(
        "<!doctype html><html><head><title>Кабинет — занятия</title></head><body>"
        '<div id="app"></div><script>'
        "const token = localStorage.getItem('" + STORAGE_KEY + "') || '';"
        "fetch('/client-cabinet/api/schedule', {headers: {Authorization: 'Bearer ' + token}})"
        "  .then((r) => r.json()).then((j) => {"
        "    const list = document.createElement('ul');"
        "    list.id = 'schedule';"
        "    for (const item of j.items) {"
        "      const row = document.createElement('li');"
        "      row.className = 'class';"
        "      row.textContent = item.title + ' — ' + item.room + ' — ' + item.starts_at;"
        "      list.appendChild(row);"
        "    }"
        "    document.getElementById('app').appendChild(list);"
        "  });"
        "fetch('/client-cabinet/api/track', {method: 'POST', body: '{}'}).catch(() => {});"
        "</script></body></html>"
    )


@router.get("/api/schedule", response_model=None)
async def schedule(request: Request) -> JSONResponse:
    if not _authorized(request):
        STATE.refused += 1
        return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
    STATE.schedule_reads += 1
    return JSONResponse(
        {
            "items": [
                {
                    "uid": uid,
                    "title": title,
                    "starts_at": starts,
                    "ends_at": ends,
                    "room": room,
                }
                for uid, title, starts, ends, room in CLASSES
            ]
        }
    )


@router.post("/api/track")
async def track() -> dict[str, bool]:
    STATE.track_writes += 1
    return {"ok": True}


def reset() -> None:
    STATE.reset()


def class_titles() -> list[str]:
    return [title for _, title, _, _, _ in CLASSES]


def storage_state(origin: str, *, session: str | None = None) -> dict[str, object]:
    """A Playwright storage state with the bearer the page reads from localStorage."""
    state: dict[str, object] = {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [{"name": STORAGE_KEY, "value": CLIENT_TOKEN}],
            }
        ],
    }
    if session is not None:
        state["cookies"] = [
            {
                "name": session,
                "value": session,
                "domain": "localhost",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ]
    return state
