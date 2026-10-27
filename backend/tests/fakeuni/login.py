"""Owner: W2. Keycloak-like login: username+password form, then an OTP step, then a session cookie."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


@router.post("/auth/login")
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


@router.post("/auth/otp")
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
