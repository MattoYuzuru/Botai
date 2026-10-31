"""A local HTTP site for `browser/control.py` and `browser/profiles.py` tests.

Plain loopback HTTP (no TLS, no proxy, no fakeuni): the control layer is supposed to reach any
host directly, so the test site must not depend on the v3 EgressProxy at all. It is the whole
"internet" of the test: pages with fields, buttons, cookies and storage, a redirect, JSON, a
download, an upload, a delayed element, an alert and a page whose whole text is a marker used to
prove that page content never reaches the logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SECRET_PAGE_MARKER = "СЕКРЕТ-СТРАНИЦА-МАРКЕР"
COOKIE_VALUE = "secret-cookie-value"
LOCAL_STORAGE_VALUE = "secret-local-storage-value"
SESSION_STORAGE_VALUE = "secret-session-storage-value"


def _page(title: str, body: str, head: str = "") -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{title}</title>
{head}</head><body>{body}</body></html>"""
    )


def create_local_site() -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return _page(
            "Control Lab",
            """
<h1 id="heading">Проверка браузера</h1>
<p id="greeting">привет</p>
<button id="btn" onclick="document.getElementById('out').textContent = 'нажато'">Нажми меня</button>
<p id="out"></p>
<label for="name">Имя</label><input id="name" name="name" autocomplete="off">
<label for="pick">Выбор</label>
<select id="pick">
  <option value="a">Первый</option><option value="b">Второй</option><option value="c">Третий</option>
</select>
<a id="next" href="/page2">Дальше</a>
<a id="dl" href="/download">Скачать отчёт</a>
<a id="popup" href="/popup">Окно</a>
<table id="table"><tr><th>Предмет</th><th>Оценка</th></tr>
<tr><td>Математика</td><td>5</td></tr></table>
<form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
  <input id="file" type="file" name="file">
  <button id="send" type="submit">Загрузить</button>
</form>
""",
        )

    @app.get("/page2", response_class=HTMLResponse)
    async def page2() -> HTMLResponse:
        return _page("Страница два", '<h1>Вторая страница</h1><p id="second">вторая</p>')

    @app.get("/form", response_class=HTMLResponse)
    async def form() -> HTMLResponse:
        return _page(
            "Форма",
            """
<form id="form" action="/echo" method="post">
  <label for="name">Имя</label><input id="name" name="name" autocomplete="off">
  <label for="pass">Пароль</label><input id="pass" name="pass" type="password">
  <button id="submit" type="submit">Отправить</button>
</form>
""",
        )

    @app.post("/echo", response_class=HTMLResponse)
    async def echo(name: str = Form(""), pass_: str = Form("", alias="pass")) -> HTMLResponse:
        return _page("Ответ", f"<h1>Результат: {name}</h1><p id='pass-len'>{len(pass_)}</p>")

    @app.get("/enter", response_class=HTMLResponse)
    async def enter() -> HTMLResponse:
        return _page(
            "Ввод",
            """
<form id="form" action="/echo" method="post">
  <input id="q" name="name" autocomplete="off">
</form>
""",
        )

    @app.get("/js", response_class=HTMLResponse)
    async def js() -> HTMLResponse:
        return _page(
            "Состояние",
            f"""<h1>Страница состояния</h1>
<script>
localStorage.setItem('token', '{LOCAL_STORAGE_VALUE}');
sessionStorage.setItem('once', '{SESSION_STORAGE_VALUE}');
document.cookie = 'lab_cookie={COOKIE_VALUE}; path=/; max-age=3600';
</script>""",
        )

    @app.get("/json")
    async def json_page() -> JSONResponse:
        return JSONResponse({"ok": True, "items": [1, 2, 3]})

    @app.get("/redirect")
    async def redirect() -> RedirectResponse:
        return RedirectResponse("/page2", status_code=302)

    @app.get("/download")
    async def download() -> Response:
        return Response(
            content=b"payload-bytes",
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="report.txt"'},
        )

    @app.post("/upload", response_class=HTMLResponse)
    async def upload(file: UploadFile = File(...)) -> HTMLResponse:
        body = await file.read()
        return _page("Загрузка", f"<h1>uploaded:{file.filename}:{len(body)}</h1>")

    @app.get("/slow", response_class=HTMLResponse)
    async def slow() -> HTMLResponse:
        return _page(
            "Медленно",
            """
<p id="waiting">ждём</p>
<script>setTimeout(() => {
  const node = document.createElement('div'); node.id = 'late'; node.textContent = 'поздно';
  document.body.appendChild(node);
}, 400);</script>
""",
        )

    @app.get("/dialog", response_class=HTMLResponse)
    async def dialog() -> HTMLResponse:
        return _page(
            "Диалог",
            """<button id="alert" onclick="alert('привет из диалога')">Оповестить</button>""",
        )

    @app.get("/popup", response_class=HTMLResponse)
    async def popup() -> HTMLResponse:
        return _page(
            "Окно",
            """<button id="open" onclick="window.open('/page2', '_blank')">Открыть</button>""",
        )

    @app.get("/secret", response_class=HTMLResponse)
    async def secret() -> HTMLResponse:
        return _page("Секретная", f"<p id='secret'>{SECRET_PAGE_MARKER}</p>")

    @app.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"cookies": dict(request.cookies), "host": request.headers.get("host")})

    return app


@dataclass(frozen=True, slots=True)
class LocalSite:
    base_url: str
    port: int

    def url(self, path: str = "/") -> str:
        return f"{self.base_url}{path}"
