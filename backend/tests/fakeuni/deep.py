"""A synthetic cabinet whose classes are behind a deep link and a same-URL SPA tab."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tests.fakeuni import data
from tests.fakeuni.login import _start_session, is_logged_in

router = APIRouter()
STORAGE_LOCAL = "private-local-storage-fixture"
STORAGE_SESSION = "private-session-storage-fixture"
COURSE_TITLE = "Навигация в графах"


@dataclass
class DeepState:
    visits: list[str] = field(default_factory=list)
    panels: list[str] = field(default_factory=list)
    writes: int = 0
    storage_reads: int = 0


STATE = DeepState()


def reset() -> None:
    STATE.visits.clear()
    STATE.panels.clear()
    STATE.writes = STATE.storage_reads = 0


def _login_required(request: Request) -> RedirectResponse | None:
    if not is_logged_in(request):
        return RedirectResponse("/auth/login", status_code=302)
    STATE.visits.append(str(request.url.path))
    return None


@router.get("/deep-portal", response_model=None)
async def portal(request: Request) -> HTMLResponse | RedirectResponse:
    if (login := _login_required(request)) is not None:
        return login
    return HTMLResponse(
        "<!doctype html><title>Учебный кабинет</title><main><nav>"
        + "".join(f'<a href="/deep-other/{i}">Сервис {i}</a>' for i in range(9))
        + '<a href="/deep-courses">Мои курсы</a>'
        + '<a href="https://evil.localhost/collect">Посторонний сервис</a>'
        + "</nav></main><script>"
        + "fetch('/deep-write',{method:'POST'}).catch(()=>{});"
        + "</script>"
    )


@router.get("/deep-courses", response_model=None)
async def courses(request: Request) -> HTMLResponse | RedirectResponse:
    if (login := _login_required(request)) is not None:
        return login
    return HTMLResponse(
        "<!doctype html><title>Мои курсы</title>"
        f'<main><a href="/deep-course">{COURSE_TITLE}</a></main>'
    )


@router.get("/deep-course", response_model=None)
async def course(request: Request) -> HTMLResponse | RedirectResponse:
    if (login := _login_required(request)) is not None:
        return login
    # Both views keep the URL unchanged. The panel endpoint also keeps its URL: the newest
    # response must invalidate an earlier expansion rather than replay the cumulative history.
    return HTMLResponse(
        """<!doctype html><title>Учебный курс</title><main>
<div role="tablist">
<button type="button" role="tab" aria-controls="panel" id="about">О курсе</button>
<button type="button" role="tab" aria-controls="panel" id="classes">Занятия</button>
</div><section role="tabpanel" id="panel"></section></main><script>
const out = document.getElementById('panel');
async function show(view) {
  const body = await (await fetch('/api/deep-panel', {headers: {'X-View': view}})).json();
  if (view === 'schedule') {
    const schedule = await (await fetch('/api/deep-schedule')).json();
    out.innerHTML = '<h2>Занятия курса</h2><article>' + schedule.items[0].subject.name + '</article>';
  } else {
    out.innerHTML = '<h2>Описание курса</h2><article>Старое содержимое вкладки</article>';
  }
}
document.getElementById('about').onclick = () => show('courses');
document.getElementById('classes').onclick = () => show('schedule');
show('courses');
</script>"""
    )


@router.get("/api/deep-panel", response_model=None)
async def panel(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({"title": "Unauthorized"}, status_code=401)
    view = request.headers.get("x-view", "courses")
    STATE.panels.append(view)
    if view == "schedule":
        return JSONResponse({"items": data.timetable()})
    return JSONResponse({"items": [{"id": "course-graphs", "title": COURSE_TITLE}]})


@router.get("/api/deep-schedule", response_model=None)
async def schedule(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return JSONResponse({"title": "Unauthorized"}, status_code=401)
    return JSONResponse({"items": data.timetable()})


@router.post("/deep-write")
async def forbidden_write() -> dict[str, bool]:
    STATE.writes += 1
    return {"ok": True}


@router.get("/deep-storage-login")
async def storage_login() -> HTMLResponse:
    response = HTMLResponse(
        "<!doctype html><title>Вход завершён</title><h1>Личный кабинет</h1><script>"
        f"localStorage.setItem('fixtureLocal','{STORAGE_LOCAL}');"
        f"sessionStorage.setItem('fixtureSession','{STORAGE_SESSION}');"
        "</script>"
    )
    response.raw_headers.extend(
        (name, value) for name, value in _start_session().raw_headers if name == b"set-cookie"
    )
    return response


@router.get("/deep-storage-page", response_model=None)
async def storage_page(request: Request) -> HTMLResponse | RedirectResponse:
    if (login := _login_required(request)) is not None:
        return login
    return HTMLResponse(
        "<!doctype html><title>Кабинет со storage</title><main id='out'></main><script>"
        "const local = localStorage.getItem('fixtureLocal');"
        "const session = sessionStorage.getItem('fixtureSession');"
        f"if(local !== '{STORAGE_LOCAL}' || session !== '{STORAGE_SESSION}') {{"
        "document.querySelector('#out').textContent='Вы не вошли в аккаунт. Для продолжения войдите';"
        "} else { fetch('/api/deep-storage',{headers:{'X-Local':local,'X-Session':session}})"
        ".then(r=>r.json()).then(j=>{document.querySelector('#out').textContent=j.title;}); }"
        "</script>"
    )


@router.get("/api/deep-storage")
async def storage_data(request: Request) -> JSONResponse:
    if (
        not is_logged_in(request)
        or request.headers.get("x-local") != STORAGE_LOCAL
        or request.headers.get("x-session") != STORAGE_SESSION
    ):
        return JSONResponse({"title": "Unauthorized"}, status_code=401)
    STATE.storage_reads += 1
    return JSONResponse({"title": "Сессия браузера восстановлена"})
