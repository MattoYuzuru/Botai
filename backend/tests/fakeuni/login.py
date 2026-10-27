"""Owner: W2. Keycloak-like login: username+password form, then an OTP step, then a session cookie."""

from __future__ import annotations

import base64
import json
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from tests.fakeuni import data

router = APIRouter()
SESSIONS: set[str] = set()
_PENDING_OTP: set[str] = set()
COOKIE = "FAKEUNI_SESSION"


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page() -> str:
    return """<!doctype html><html><head><title>Вход — Фейкуни</title></head><body>
<form id="kc-form-login" method="post" action="/auth/login">
  <label for="username">Почта</label><input id="username" name="username" type="email" autocomplete="username">
  <label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password">
  <button id="kc-login" type="submit">Войти</button>
</form></body></html>"""


@router.post("/auth/login", response_model=None)
async def login_submit(
    username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if username != data.USERNAME or password != data.PASSWORD:
        return HTMLResponse(
            '<div class="kc-feedback-text">Неверный логин или пароль</div>', status_code=401
        )
    pending = secrets.token_hex(8)
    _PENDING_OTP.add(pending)
    resp = RedirectResponse("/auth/otp", status_code=303)
    resp.set_cookie("FAKEUNI_OTP", pending, httponly=True, secure=True)
    return resp


@router.get("/auth/otp", response_class=HTMLResponse)
async def otp_page() -> str:
    return """<!doctype html><html><head><title>Код подтверждения</title></head><body>
<form id="kc-otp-login-form" method="post" action="/auth/otp">
  <label for="otp">Код из SMS</label><input id="otp" name="otp" autocomplete="one-time-code" inputmode="numeric">
  <button id="kc-otp-submit" type="submit">Подтвердить</button>
</form></body></html>"""


@router.post("/auth/otp", response_model=None)
async def otp_submit(request: Request, otp: str = Form(...)) -> HTMLResponse | RedirectResponse:
    pending = request.cookies.get("FAKEUNI_OTP")
    if pending not in _PENDING_OTP or otp != data.OTP:
        return HTMLResponse('<div class="kc-feedback-text">Неверный код</div>', status_code=401)
    _PENDING_OTP.discard(pending)
    token = secrets.token_hex(16)
    SESSIONS.add(token)
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, secure=True)
    return resp


def is_logged_in(request: Request) -> bool:
    return request.cookies.get(COOKIE) in SESSIONS


# --- a bearer-token SPA, a captcha page and a download (gateway tests) ------------------------


def _fake_jwt(exp: int) -> str:
    def part(obj: dict[str, object]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'sub': 'u-1001', 'exp': exp})}.fixturesig"


SPA_JWT = _fake_jwt(4_102_444_800)  # 2100-01-01
SPA_JWT_EXPIRES = 4_102_444_800


@router.get("/spa", response_class=HTMLResponse)
async def spa() -> str:
    return f"""<!doctype html><html><head><title>Кабинет (SPA)</title></head><body>
<div id="out">загрузка</div><script>
sessionStorage.setItem('tok', '{SPA_JWT}');
fetch('/spa/api/ping', {{headers: {{Authorization: 'Bearer {SPA_JWT}'}}}})
  .then((r) => r.json()).then((j) => {{ document.getElementById('out').textContent = JSON.stringify(j); }});
fetch('/spa/api/token').then((r) => r.json());
</script></body></html>"""


@router.get("/spa/api/ping", response_model=None)
async def spa_ping(request: Request) -> JSONResponse | dict[str, object]:
    if request.headers.get("authorization") != f"Bearer {SPA_JWT}":
        return JSONResponse({"title": "Unauthorized"}, status_code=401)
    return {"ok": True, "user": "u-1001"}


@router.get("/spa/api/token")
async def spa_token() -> dict[str, object]:
    return {"access_token": SPA_JWT, "expires_in": 300}


@router.get("/captcha/login", response_class=HTMLResponse)
async def captcha_login() -> str:
    return """<!doctype html><html><head><title>Вход</title></head><body>
<form id="login" method="post" action="/captcha/login">
  <input id="u" name="username" type="text" autocomplete="username">
  <input id="p" name="password" type="password" autocomplete="current-password">
  <div class="g-recaptcha" data-sitekey="fixture"></div>
  <iframe src="/captcha/widget" title="reCAPTCHA" width="300" height="76"></iframe>
  <button id="go" type="submit">Войти</button>
</form></body></html>"""


@router.get("/captcha/widget", response_class=HTMLResponse)
async def captcha_widget() -> str:
    return """<!doctype html><html><body><label><input type="checkbox" id="human"> Я не робот</label></body></html>"""


@router.get("/download")
async def download() -> Response:
    return Response(
        b"secret report",
        media_type="application/octet-stream",
        headers={"content-disposition": 'attachment; filename="report.bin"'},
    )


# --- redirects to hosts the browser must never reach (security check C2) ---------------------


@router.get("/redirect/evil")
async def redirect_evil() -> RedirectResponse:
    return RedirectResponse("https://evil.localhost/collect?c=1", status_code=302)


@router.get("/redirect/ip")
async def redirect_ip() -> RedirectResponse:
    return RedirectResponse("https://127.0.0.1:8443/secret", status_code=302)


@router.get("/redirect/metadata")
async def redirect_metadata() -> RedirectResponse:
    return RedirectResponse("https://169.254.169.254/latest/meta-data/", status_code=302)
