"""Owner: W2. The synthetic university's ways in.

* `/auth/login` → `/auth/otp` → `/app`: Keycloak-like username+password, then an SMS code (default
  path; `/` redirects here through `/app`).
* `/plain/login`: username+password only.
* `/two/username` → `/two/password`: the two-page flow.
* `/push/login` → `/push/wait`: wait for an approval on a phone (tests call `approve_push()`).
* `/captcha/login`: a form with a captcha stand-in iframe that a human has to tick.
* `/sso/start` → `id.fakeuni.test/sso/login` → `/sso/callback`: login on a second host.
* `/blocked`: the site refuses the visit (bot protection page).
* `/cu/app` → `id.fakeuni.test/cu/realms/...` → `/cu/login-actions/authenticate`: a Keycloak-like
  JS login in three steps (e-mail, password, SMS code); see the section below.
* `/assets/login`, `/assets/plain`, `/assets/legacy`, `/sso-assets/start`, `/sibling/login`: pages whose
  CSS and JS come from another host (`cdn.fakeuni-assets.test`, or a sibling `static.fakeuni.test`);
  `/redirect/open` is an open redirect. See the last section.

Wrong credentials re-render the same form with an error, like real sites. Every successful path
ends with the `FAKEUNI_SESSION` cookie on fakeuni.test. Nothing here resembles a real university.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from html import escape

from fastapi import APIRouter, Form, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from tests.fakeuni import data

router = APIRouter()
SESSIONS: set[str] = set()
_PENDING_OTP: set[str] = set()
COOKIE = "FAKEUNI_SESSION"
ROOT_TARGET = "/app"  # where `/` sends a visitor; tests point it at other entrances
OTP_REQUIRED = True  # False: the default path logs in on the password alone (code asked once)

PUSH_PENDING: set[str] = set()
PUSH_APPROVED: set[str] = set()
HUMANS: set[str] = set()
_SSO_CODES: set[str] = set()
ATTEMPTS: list[tuple[str, str]] = []  # (flow, "user"/"otp"): wrong tries, for lockout tests


def approve_push() -> None:
    """The student taps "approve" on the phone."""
    PUSH_APPROVED.update(PUSH_PENDING)


def expire_all_sessions() -> None:
    SESSIONS.clear()


def is_logged_in(request: Request) -> bool:
    return request.cookies.get(COOKIE) in SESSIONS


# A cabinet whose analytics never stop: every page polls forever, so the network is never quiet.
BUSY_PAGES = False
_BUSY = "<script>setInterval(() => fetch('/cu/api/ping').catch(() => {}), 150);</script>"


def _page(title: str, body: str) -> str:
    busy = _BUSY if BUSY_PAGES else ""
    return (
        f"<!doctype html><html><head><title>{escape(title)}</title></head>"
        f"<body>{body}{busy}</body></html>"
    )


def _error(text: str) -> str:
    return f'<div class="kc-feedback-text" role="alert">{escape(text)}</div>'


def _start_session(to: str = "/app") -> RedirectResponse:
    token = secrets.token_hex(16)
    SESSIONS.add(token)
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, secure=True)
    return resp


def _credentials_ok(username: str, password: str) -> bool:
    return username == data.USERNAME and password == data.PASSWORD


@router.get("/", response_model=None)
async def root() -> RedirectResponse:
    return RedirectResponse(ROOT_TARGET, status_code=302)


# --- default: credentials, then an SMS code ---------------------------------------------------


def _login_form(error: str = "") -> str:
    return _page(
        "Вход — Фейкуни",
        f"""{error}<form id="kc-form-login" method="post" action="/auth/login">
  <label for="username">Почта</label><input id="username" name="username" type="email" autocomplete="username">
  <label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password">
  <button id="kc-login" type="submit">Войти</button>
</form>""",
    )


def _otp_form(error: str = "") -> str:
    return _page(
        "Код подтверждения",
        f"""{error}<p class="hint">Код отправлен на s***@fakeuni.test</p>
<form id="kc-otp-login-form" method="post" action="/auth/otp">
  <label for="otp">Код из SMS</label><input id="otp" name="otp" autocomplete="one-time-code" inputmode="numeric" maxlength="6">
  <button id="kc-otp-submit" type="submit">Подтвердить</button>
</form>""",
    )


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page() -> str:
    return _login_form()


@router.post("/auth/login", response_model=None)
async def login_submit(
    username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if not _credentials_ok(username, password):
        ATTEMPTS.append(("auth", "user"))
        return HTMLResponse(_login_form(_error("Неверный логин или пароль")), status_code=401)
    if not OTP_REQUIRED:
        return _start_session()
    pending = secrets.token_hex(8)
    _PENDING_OTP.add(pending)
    resp = RedirectResponse("/auth/otp", status_code=303)
    resp.set_cookie("FAKEUNI_OTP", pending, httponly=True, secure=True)
    return resp


@router.get("/auth/otp", response_class=HTMLResponse)
async def otp_page() -> str:
    return _otp_form()


@router.post("/auth/otp", response_model=None)
async def otp_submit(request: Request, otp: str = Form(...)) -> HTMLResponse | RedirectResponse:
    pending = request.cookies.get("FAKEUNI_OTP")
    if pending not in _PENDING_OTP or otp != data.OTP:
        ATTEMPTS.append(("auth", "otp"))
        return HTMLResponse(_otp_form(_error("Неверный код")), status_code=401)
    _PENDING_OTP.discard(pending)
    return _start_session()


# --- username and password only ---------------------------------------------------------------


def _plain_form(error: str = "") -> str:
    return _page(
        "Вход",
        f"""{error}<form id="plain" method="post" action="/plain/login">
  <label for="u">Логин</label><input id="u" name="login" type="text" autocomplete="username">
  <label for="p">Пароль</label><input id="p" name="pass" type="password" autocomplete="current-password">
  <button type="submit">Войти</button>
</form>""",
    )


@router.get("/plain/login", response_class=HTMLResponse)
async def plain_page() -> str:
    return _plain_form()


@router.post("/plain/login", response_model=None)
async def plain_submit(
    login: str = Form(...), pass_: str = Form(..., alias="pass")
) -> HTMLResponse | RedirectResponse:
    if not _credentials_ok(login, pass_):
        ATTEMPTS.append(("plain", "user"))
        return HTMLResponse(_plain_form(_error("Неверный логин или пароль")), status_code=401)
    return _start_session()


# --- two pages: username, then password ---------------------------------------------------------


def _two_user_form(error: str = "") -> str:
    return _page(
        "Вход: логин",
        f"""{error}<form id="two-user" method="post" action="/two/username">
  <label for="username">Электронная почта</label><input id="username" name="username" type="text" autocomplete="username">
  <button type="submit">Далее</button>
</form>""",
    )


def _two_pass_form(error: str = "") -> str:
    return _page(
        "Вход: пароль",
        f"""{error}<form id="two-pass" method="post" action="/two/password">
  <label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password">
  <button type="submit">Войти</button>
</form>""",
    )


@router.get("/two/username", response_class=HTMLResponse)
async def two_username_page() -> str:
    return _two_user_form()


@router.post("/two/username", response_model=None)
async def two_username_submit(username: str = Form(...)) -> HTMLResponse | RedirectResponse:
    if username != data.USERNAME:
        return HTMLResponse(_two_user_form(_error("Пользователь не найден")), status_code=401)
    resp = RedirectResponse("/two/password", status_code=303)
    resp.set_cookie("FAKEUNI_TWO", "1", httponly=True, secure=True)
    return resp


@router.get("/two/password", response_class=HTMLResponse)
async def two_password_page() -> str:
    return _two_pass_form()


@router.post("/two/password", response_model=None)
async def two_password_submit(
    request: Request, password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if request.cookies.get("FAKEUNI_TWO") != "1" or password != data.PASSWORD:
        ATTEMPTS.append(("two", "user"))
        return HTMLResponse(_two_pass_form(_error("Неверный пароль")), status_code=401)
    return _start_session()


# --- a form that is refused at another address than it was served from (Keycloak-like) -------


def _kc_form(error: str = "") -> str:
    return _page(
        "Вход (kc)",
        f"""{error}<form id="kc" method="post" action="/kc/auth/authenticate">
  <input id="u" name="username" type="text" autocomplete="username">
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">Войти</button>
</form>""",
    )


@router.get("/kc/auth", response_class=HTMLResponse)
async def kc_page() -> str:
    return _kc_form()


@router.post("/kc/auth/authenticate", response_model=None)
async def kc_submit(
    username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if not _credentials_ok(username, password):
        ATTEMPTS.append(("kc", "user"))
        # the refusal is shown at /kc/auth/authenticate, not at /kc/auth
        return HTMLResponse(_kc_form(_error("Неверный логин или пароль")), status_code=401)
    return _start_session()


# --- Keycloak-like JS login: e-mail, then password, then an SMS code ---------------------------
#
# The cabinet `/cu/app` sends a visitor to the identity host. Its e-mail page is a form with *no*
# `action`: a script checks the login with a POST to another host and path, then sets the action to
# `/cu/login-actions/authenticate?session_code=…&execution=…&client_id=…&tab_id=…&client_data=…`
# and submits. The page also posts "telemetry" on load, which nobody asked for. The password and
# code steps are ordinary forms with the same kind of action. Every page gets a fresh session code.

CU_LOGIN_HOST = "https://id.fakeuni.test"
CU_CHECK_ORIGIN = "https://fakeuni.test"  # where the page's own login check is posted
CU_ACTION_ORIGIN = ""  # prefix of the action the script sets: "" is the page's own host
CU_PRE_SUBMIT_POSTS = (
    0  # how many telemetry posts the script makes after its check, before it submits
)
CU_BEACON_ORIGIN = ""  # when set, the e-mail page also posts a beacon there as it submits
CU_PASSWORD_CHECK_ORIGIN = ""  # when set, the password page checks the step there before it posts
CU_FORM_POST = False  # the site ends the login with an auto-submitted `form_post` page
CU_FORM_POST_EXTRA = ""  # a field of its own the auto-submitted form adds (not the protocol's)
_CU_FLOWS: dict[str, dict[str, str]] = {}
_CU_CODES: set[str] = set()


class CuStats:
    """What actually reached the server: a blocked request never shows up here."""

    def __init__(self) -> None:
        self.checks = 0
        self.telemetry = 0
        self.beacons = 0
        self.password_checks = 0
        self.callbacks = 0  # auto-submitted form_post messages that reached the site
        self.submissions: list[str] = []

    def reset(self) -> None:
        self.checks = self.telemetry = self.beacons = self.password_checks = self.callbacks = 0
        self.submissions.clear()


CU = CuStats()


def cu_reset() -> None:
    CU.reset()
    _CU_FLOWS.clear()
    _CU_CODES.clear()


def _cu_action(stage: str, user: str = "") -> str:
    """A page of the flow, registered under a fresh session code (Keycloak style)."""
    code = secrets.token_urlsafe(18)
    _CU_FLOWS[code] = {"stage": stage, "user": user}
    return (
        f"/cu/login-actions/authenticate?session_code={code}&execution={secrets.token_hex(8)}"
        f"&client_id=cabinet&tab_id={secrets.token_urlsafe(8)}&client_data={secrets.token_urlsafe(24)}"
    )


def _cu_email_page(error: str = "") -> str:
    action = CU_ACTION_ORIGIN + _cu_action("email")
    return _page(
        "Вход — Фейкуни ID",
        f"""{error}<form id="cu-form" method="post">
  <label for="cu-login">Почта</label><input id="cu-login" name="username" type="email" autocomplete="username">
  <button id="cu-next" type="submit">Далее</button>
</form><p id="cu-note"></p>
<script>
fetch('{CU_CHECK_ORIGIN}/cu/api/telemetry', {{method: 'POST'}}).catch(() => {{}});
const form = document.getElementById('cu-form');
form.addEventListener('submit', async (event) => {{
  event.preventDefault();
  if ('{CU_BEACON_ORIGIN}') fetch('{CU_BEACON_ORIGIN}/cu/api/beacon', {{method: 'POST', mode: 'no-cors'}}).catch(() => {{}});
  let known = false;
  try {{
    const answer = await fetch('{CU_CHECK_ORIGIN}/cu/api/check-login', {{
      method: 'POST', headers: {{'content-type': 'application/json'}},
      body: JSON.stringify({{login: form.elements.username.value}}),
    }});
    known = (await answer.json()).known === true;
  }} catch (_) {{}}
  if (!known) {{ document.getElementById('cu-note').textContent = 'Не удалось проверить почту'; return; }}
  for (let i = 0; i < {CU_PRE_SUBMIT_POSTS}; i++) {{
    await fetch('{CU_CHECK_ORIGIN}/cu/api/telemetry', {{method: 'POST'}}).catch(() => {{}});
  }}
  form.action = {json.dumps(action)};
  form.submit();
}});
</script>""",
    )


def _cu_password_page(user: str, error: str = "") -> str:
    check = (
        f"""<p id="cu-password-note"></p>
<script>
const passwordForm = document.getElementById('cu-password-form');
passwordForm.addEventListener('submit', async (event) => {{
  event.preventDefault();
  let ok = false;
  try {{
    const answer = await fetch('{CU_PASSWORD_CHECK_ORIGIN}/cu/api/check-password', {{
      method: 'POST', headers: {{'content-type': 'application/json'}},
      body: JSON.stringify({{step: 'password'}}),
    }});
    ok = (await answer.json()).ok === true;
  }} catch (_) {{}}
  if (!ok) {{ document.getElementById('cu-password-note').textContent = 'Не удалось проверить шаг'; return; }}
  passwordForm.submit();
}});
</script>"""
        if CU_PASSWORD_CHECK_ORIGIN
        else ""
    )
    return _page(
        "Вход — пароль",
        f"""{error}<form id="cu-password-form" method="post" action="{escape(_cu_action("password", user))}">
  <label for="cu-password">Пароль</label><input id="cu-password" name="password" type="password" autocomplete="current-password">
  <button id="cu-login-submit" type="submit">Войти</button>
</form>{check}""",
    )


def _cu_otp_page(user: str, error: str = "") -> str:
    return _page(
        "Вход — код из SMS",
        f"""{error}<p class="hint">Код отправлен на s***@fakeuni.test</p>
<form id="cu-otp-form" method="post" action="{escape(_cu_action("otp", user))}">
  <label for="cu-otp">Код из SMS</label><input id="cu-otp" name="otp" autocomplete="one-time-code" inputmode="numeric" maxlength="6">
  <button id="cu-otp-submit" type="submit">Подтвердить</button>
</form>""",
    )


@router.get("/cu/gate", response_model=None)
async def cu_gate(request: Request) -> HTMLResponse:
    """A cabinet landing without fields: «Вы не вошли — Войти» leads to the SSO only by a click."""
    if is_logged_in(request):  # the same address shows the cabinet once logged in (an SPA route)
        return HTMLResponse(
            "<!doctype html><title>Кабинет ЦУ</title><main><h1>Мои занятия</h1>"
            "<article>Семинар по тестированию</article></main>"
        )
    return HTMLResponse(
        "<!doctype html><title>Кабинет</title><main><h1>Вы не вошли в аккаунт</h1>"
        "<p>Для продолжения, пожалуйста, войдите</p>"
        "<button type=button id=go>Войти</button></main><script>"
        "document.getElementById('go').addEventListener('click', () => {"
        "location.href = '/cu/app'; });</script>"
    )


@router.get("/cu/app", response_model=None)
async def cu_app(request: Request) -> HTMLResponse | RedirectResponse:
    if not is_logged_in(request):
        return RedirectResponse(
            f"{CU_LOGIN_HOST}/cu/realms/fakeuni/protocol/openid-connect/auth"
            "?client_id=cabinet&redirect_uri=https%3A%2F%2Ffakeuni.test%2Fcu%2Fcallback"
            "&response_type=code&scope=openid&state=" + secrets.token_hex(6),
            status_code=302,
        )
    return HTMLResponse(
        "<!doctype html><title>Кабинет ЦУ</title><main><h1>Мои занятия</h1>"
        "<article>Семинар по тестированию</article></main><script>"
        "fetch('/api/v2/me').catch(() => {});"
        "fetch('/cu/api/telemetry', {method: 'POST'}).catch(() => {});</script>"
    )


@router.get("/cu/realms/fakeuni/protocol/openid-connect/auth", response_class=HTMLResponse)
async def cu_login_page() -> str:
    return _cu_email_page()


_CU_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "POST",
}


@router.options("/cu/api/check-login")
async def cu_check_login_preflight() -> Response:
    """The login check is cross-origin and sends JSON: the browser asks first."""
    return Response(status_code=204, headers=_CU_CORS)


@router.post("/cu/api/check-login")
async def cu_check_login(request: Request) -> JSONResponse:
    CU.checks += 1
    known = (await request.json()).get("login") == data.USERNAME
    return JSONResponse({"known": known}, headers=_CU_CORS)


@router.options("/cu/api/check-password")
async def cu_check_password_preflight() -> Response:
    return Response(status_code=204, headers=_CU_CORS)


@router.post("/cu/api/check-password")
async def cu_check_password() -> JSONResponse:
    CU.password_checks += 1
    return JSONResponse({"ok": True}, headers=_CU_CORS)


@router.get("/cu/api/ping")
async def cu_ping() -> Response:
    await asyncio.sleep(0.12)
    return Response(status_code=204)


@router.post("/cu/api/telemetry")
async def cu_telemetry() -> Response:
    CU.telemetry += 1
    return Response(status_code=204)


@router.post("/cu/api/beacon")
async def cu_beacon() -> Response:
    CU.beacons += 1
    return Response(status_code=204)


@router.post("/cu/login-actions/authenticate", response_model=None)
async def cu_authenticate(
    session_code: str,
    username: str | None = Form(None),
    password: str | None = Form(None),
    otp: str | None = Form(None),
) -> HTMLResponse | RedirectResponse:
    flow = _CU_FLOWS.pop(session_code, None)
    if flow is None:
        return HTMLResponse(_cu_email_page(_error("Сессия входа устарела")), status_code=400)
    stage, user = flow["stage"], flow["user"]
    CU.submissions.append(stage)
    if stage == "email":
        if username != data.USERNAME:
            ATTEMPTS.append(("cu", "user"))
            return HTMLResponse(_cu_email_page(_error("Пользователь не найден")), status_code=401)
        return HTMLResponse(_cu_password_page(username or ""))
    if stage == "password":
        if password != data.PASSWORD:
            ATTEMPTS.append(("cu", "user"))
            return HTMLResponse(_cu_password_page(user, _error("Неверный пароль")), status_code=401)
        return HTMLResponse(_cu_otp_page(user))
    if otp != data.OTP:
        ATTEMPTS.append(("cu", "otp"))
        return HTMLResponse(_cu_otp_page(user, _error("Неверный код")), status_code=401)
    code = secrets.token_hex(8)
    _CU_CODES.add(code)
    if CU_FORM_POST:
        extra = (
            f'<input type="hidden" name="{CU_FORM_POST_EXTRA}" value="x">'
            if CU_FORM_POST_EXTRA
            else ""
        )
        return HTMLResponse(
            _page(
                "Вход выполнен",
                f"""<form id="auto" method="post" action="https://fakeuni.test/cu/callback-post">
  <input type="hidden" name="code" value="{code}"><input type="hidden" name="state" value="s1">{extra}
  <noscript><button>Продолжить</button></noscript>
</form><script>document.getElementById('auto').submit();</script>""",
            )
        )
    return RedirectResponse(f"https://fakeuni.test/cu/callback?code={code}", status_code=303)


@router.post("/cu/callback-post", response_model=None)
async def cu_callback_post(code: str = Form(...)) -> RedirectResponse | HTMLResponse:
    CU.callbacks += 1
    return await cu_callback(code)


@router.get("/cu/callback", response_model=None)
async def cu_callback(code: str) -> RedirectResponse | HTMLResponse:
    if code not in _CU_CODES:
        return HTMLResponse(_error("Код входа недействителен"), status_code=400)
    _CU_CODES.discard(code)
    return _start_session("/cu/app")


# --- push approval ----------------------------------------------------------------------------


def _push_form(error: str = "") -> str:
    return _page(
        "Вход (push)",
        f"""{error}<form id="push-login" method="post" action="/push/login">
  <input id="u" name="username" type="text" autocomplete="username">
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">Войти</button>
</form>""",
    )


@router.get("/push/login", response_class=HTMLResponse)
async def push_page() -> str:
    return _push_form()


@router.post("/push/login", response_model=None)
async def push_submit(
    username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if not _credentials_ok(username, password):
        ATTEMPTS.append(("push", "user"))
        return HTMLResponse(_push_form(_error("Неверный логин или пароль")), status_code=401)
    pending = secrets.token_hex(8)
    PUSH_PENDING.add(pending)
    resp = RedirectResponse("/push/wait", status_code=303)
    resp.set_cookie("FAKEUNI_PUSH", pending, httponly=True, secure=True)
    return resp


@router.get("/push/wait", response_class=HTMLResponse)
async def push_wait() -> str:
    return _page(
        "Подтверждение входа",
        """<h1>Подтвердите вход на телефоне</h1>
<p>Мы отправили запрос в приложение университета. Нажмите «Это я».</p>
<script>
setInterval(async () => {
  const j = await (await fetch('/push/status')).json();
  if (j.approved) location.href = '/push/finish';
}, 250);
</script>""",
    )


@router.get("/push/status")
async def push_status(request: Request) -> dict[str, bool]:
    return {"approved": request.cookies.get("FAKEUNI_PUSH") in PUSH_APPROVED}


@router.get("/push/finish", response_model=None)
async def push_finish(request: Request) -> RedirectResponse:
    pending = request.cookies.get("FAKEUNI_PUSH")
    if pending not in PUSH_APPROVED:
        return RedirectResponse("/push/login", status_code=303)
    PUSH_PENDING.discard(pending or "")
    PUSH_APPROVED.discard(pending or "")
    return _start_session()


# --- captcha stand-in ---------------------------------------------------------------------------


def _captcha_form(error: str = "") -> str:
    return _page(
        "Вход",
        f"""{error}<form id="login" method="post" action="/captcha/login">
  <input id="u" name="username" type="text" autocomplete="username">
  <input id="p" name="password" type="password" autocomplete="current-password">
  <div class="g-recaptcha" data-sitekey="fixture"></div>
  <iframe src="/captcha/widget" title="reCAPTCHA" width="300" height="76"></iframe>
  <button id="go" type="submit">Войти</button>
</form>""",
    )


@router.get("/captcha/login", response_class=HTMLResponse)
async def captcha_login() -> str:
    return _captcha_form()


@router.post("/captcha/login", response_model=None)
async def captcha_submit(
    request: Request, username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if request.cookies.get("FAKEUNI_HUMAN") not in HUMANS:
        return HTMLResponse(_captcha_form(_error("Подтвердите, что вы не робот")), status_code=401)
    if not _credentials_ok(username, password):
        ATTEMPTS.append(("captcha", "user"))
        return HTMLResponse(_captcha_form(_error("Неверный логин или пароль")), status_code=401)
    return _start_session()


@router.get("/captcha/widget", response_class=HTMLResponse)
async def captcha_widget() -> str:
    return _page(
        "reCAPTCHA",
        """<label><input type="checkbox" id="human"> Я не робот</label>
<script>
document.getElementById('human').addEventListener('change', () => fetch('/captcha/solve', {method: 'POST'}));
</script>""",
    )


@router.post("/captcha/solve")
async def captcha_solve() -> Response:
    token = secrets.token_hex(8)
    HUMANS.add(token)
    resp = Response(status_code=204)
    resp.set_cookie("FAKEUNI_HUMAN", token, secure=True, samesite="none")
    return resp


# --- SSO on a second host ----------------------------------------------------------------------


@router.get("/sso/start", response_model=None)
async def sso_start() -> RedirectResponse:
    return RedirectResponse("https://id.fakeuni.test/sso/login", status_code=302)


def _sso_form(error: str = "") -> str:
    return _page(
        "Единый вход",
        f"""{error}<form id="sso" method="post" action="/sso/login">
  <label for="login">Логин</label><input id="login" name="username" type="text" autocomplete="username">
  <label for="pw">Пароль</label><input id="pw" name="password" type="password" autocomplete="current-password">
  <button type="submit">Продолжить</button>
</form>""",
    )


@router.get("/sso/login", response_class=HTMLResponse)
async def sso_login_page() -> str:
    return _sso_form()


@router.post("/sso/login", response_model=None)
async def sso_login_submit(
    username: str = Form(...), password: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    if not _credentials_ok(username, password):
        ATTEMPTS.append(("sso", "user"))
        return HTMLResponse(_sso_form(_error("Неверный логин или пароль")), status_code=401)
    code = secrets.token_hex(8)
    _SSO_CODES.add(code)
    resp = RedirectResponse(f"https://fakeuni.test/sso/callback?code={code}", status_code=303)
    resp.set_cookie("IDP_SESSION", secrets.token_hex(8), httponly=True, secure=True)
    return resp


@router.get("/sso/callback", response_model=None)
async def sso_callback(code: str) -> RedirectResponse | HTMLResponse:
    if code not in _SSO_CODES:
        return HTMLResponse(_error("Код входа недействителен"), status_code=400)
    _SSO_CODES.discard(code)
    return _start_session()


# --- a site that refuses the visit ----------------------------------------------------------------


@router.get("/blocked", response_class=HTMLResponse)
async def blocked() -> HTMLResponse:
    return HTMLResponse(
        _page("Access denied", "<h1>Access denied</h1><p>Your request was blocked.</p>"),
        status_code=403,
    )


# --- a page for the remote-screen tests ---------------------------------------------------------


@router.get("/screen/lab", response_class=HTMLResponse)
async def screen_lab() -> str:
    """A full-screen black/white box that flips on every click, a text input, a link, a tall page."""
    return """<!doctype html><html><head><title>Screen lab</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0;font:18px sans-serif}
#box{position:fixed;inset:0;background:#000;z-index:-1}
#t{position:fixed;top:10px;left:10px;width:180px;height:36px;font-size:18px}
#a{position:fixed;top:70px;left:10px;padding:8px;background:#fff}</style></head>
<body><div id="box"></div><input id="t" autocomplete="off"><a id="a" href="/screen/lab2">next page</a>
<div style="height:4000px"></div>
<script>
if (location.search.includes('busy')) {
  // a page that is expensive to encode: random 6px blocks, like a photo-heavy portal
  const c = document.createElement('canvas');
  c.width = 400; c.height = 800;
  c.style.cssText = 'position:fixed;left:0;top:0;z-index:-2';
  const g = c.getContext('2d');
  for (let y = 0; y < 800; y += 6) for (let x = 0; x < 400; x += 6) {
    g.fillStyle = 'hsl(' + ((x * 7 + y * 13) % 360) + ',' + (30 + (x * y) % 60) + '%,' + (20 + (x + y) % 60) + '%)';
    g.fillRect(x, y, 6, 6);
  }
  document.body.appendChild(c);
  document.getElementById('box').style.cssText = 'position:fixed;top:auto;left:auto;right:0;bottom:0;width:60px;height:60px;z-index:-1;background:#000';
}
let on = false;
const box = document.getElementById('box');
if (location.search.includes('anim')) setInterval(() => { on = !on; box.style.background = on ? '#fff' : '#000'; }, 20);
const flip = () => { on = !on; box.style.background = on ? '#fff' : '#000'; };
window.__log = [];
document.addEventListener('mousedown', (e) => { window.__log.push('md'); if (!['INPUT', 'A'].includes(e.target.tagName)) flip(); });
document.addEventListener('keydown', (e) => { window.__log.push('kd:' + e.code); if (e.key === 'F9') flip(); });
document.addEventListener('wheel', () => window.__log.push('wheel'), {passive: true});
</script></body></html>"""


@router.get("/screen/lab2", response_class=HTMLResponse)
async def screen_lab2() -> str:
    return _page("Screen lab 2", "<h1>Second page</h1>")


# --- a bearer-token SPA, a download (gateway tests) ------------------------------------------------


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


@router.get("/spa/api/big")
async def spa_big(n: int = 10) -> list[dict[str, object]]:
    """n timetable-like items: a real portal answers with tens of kilobytes of JSON."""
    return [
        {"id": i, "title": f"Занятие {i}", "room": f"А-{100 + i % 50}", "teacher": "Иванов И. И."}
        for i in range(n)
    ]


@router.get("/spa/big", response_class=HTMLResponse)
async def spa_big_page(n: int = 10) -> str:
    return f"""<!doctype html><html><head><title>Большой ответ</title></head><body><script>
fetch('/spa/api/big?n={n}').then((r) => r.json()).then((j) => {{ document.title = 'done ' + j.length; }});
</script></body></html>"""


@router.get("/download")
async def download() -> Response:
    return Response(
        b"secret report",
        media_type="application/octet-stream",
        headers={"content-disposition": 'attachment; filename="report.bin"'},
    )


# --- redirects to hosts the browser must never reach (security check C2) ---------------------------


@router.get("/redirect/evil")
async def redirect_evil() -> RedirectResponse:
    return RedirectResponse("https://evil.localhost/collect?c=1", status_code=302)


@router.get("/redirect/ip")
async def redirect_ip() -> RedirectResponse:
    return RedirectResponse("https://127.0.0.1:8443/secret", status_code=302)


@router.get("/redirect/metadata")
async def redirect_metadata() -> RedirectResponse:
    return RedirectResponse("https://169.254.169.254/latest/meta-data/", status_code=302)


READ_ONLY_WRITES = 0
READ_ONLY_SOCKETS = 0


@router.get("/readonly/page", response_class=HTMLResponse)
async def readonly_page() -> str:
    return _page(
        "Reader fixture",
        """<main><h1>Calendar</h1></main><script>
fetch('/readonly/read').then(r=>r.json()).then(()=>document.body.dataset.read='ok');
fetch('/readonly/write', {method:'POST'}).then(()=>document.body.dataset.write='sent')
.catch(()=>document.body.dataset.write='blocked');
const channel = new WebSocket('wss://fakeuni.test/readonly/socket');
channel.onclose = ()=>document.body.dataset.socket='closed';
</script>""",
    )


@router.get("/readonly/read")
async def readonly_read() -> dict[str, bool]:
    return {"ok": True}


@router.post("/readonly/write")
async def readonly_write() -> dict[str, bool]:
    global READ_ONLY_WRITES
    READ_ONLY_WRITES += 1
    return {"ok": True}


@router.websocket("/readonly/socket")
async def readonly_socket(socket: WebSocket) -> None:
    global READ_ONLY_SOCKETS
    READ_ONLY_SOCKETS += 1
    await socket.accept()
    await socket.close()


# --- pages whose style sheets and scripts come from another host ----------------------------------
#
# An identity provider (or a cabinet) loads its CSS and JS from a CDN or static host that is not the
# login host: `cdn.fakeuni-assets.test` is such a host (not a subdomain of the origin);
# `static.fakeuni.test` is a *sibling* under the origin's own registered domain, where a cookie with
# `Domain=.fakeuni.test` would travel. The stand answers under every name; what reached it is
# recorded per Host, so a refused request is the one that never shows up here.

ASSET_HOST = "cdn.fakeuni-assets.test"
SIBLING_HOST = "static.fakeuni.test"
_CDN = f"https://{ASSET_HOST}"


class AssetStats:
    """Requests that reached the stand, with the headers a leak would ride on."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str | None]] = []

    def reset(self) -> None:
        self.requests.clear()

    def to(self, host: str) -> list[dict[str, str | None]]:
        return [r for r in self.requests if r["host"] == host]

    def paths(self, host: str) -> list[str]:
        return [str(r["path"]) for r in self.to(host)]


ASSETS = AssetStats()


def _note(request: Request | WebSocket, body: bytes | None = None) -> None:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    ASSETS.requests.append(
        {
            "host": host,
            "method": request.scope.get("method", "WS"),
            "path": request.url.path,
            "query": request.url.query,
            "body": None if body is None else body.decode("utf-8", "replace"),
            "cookie": request.headers.get("cookie"),
            "authorization": request.headers.get("authorization"),
            "referer": request.headers.get("referer"),
        }
    )


_ASSET_CSS = """#app { display: block; }
body { background: rgb(1, 2, 3); }
#a-step2[hidden] { display: none; }
"""

# The login form is built by this script: without it the page is an empty `#app` that the inline
# style hides. «Далее» shows the password step. The probes at the end try what a page of the
# university must not be able to do to an asset host.
_ASSET_JS = (
    """(function () {
  var CDN = '"""
    + _CDN
    + """';
  var app = document.getElementById('app');
  app.innerHTML =
    '<label for="a-email">Почта</label>' +
    '<input id="a-email" name="username" type="email" autocomplete="username">' +
    '<button id="a-next" type="button">Далее</button>' +
    '<div id="a-step2" hidden><label for="a-password">Пароль</label>' +
    '<input id="a-password" name="password" type="password" autocomplete="current-password">' +
    '</div><p id="a-state">step1</p><p id="a-probes"></p>' +
    '<a id="a-cdn" href="' + CDN + '/assets/document">Продолжить на другом сайте</a>' +
    '<img id="a-pixel" src="' + CDN + '/assets/pixel.png" width="1" height="1" alt="">';
  document.getElementById('a-next').addEventListener('click', function () {
    document.getElementById('a-step2').hidden = false;
    document.getElementById('a-state').textContent = 'step2';
  });
  var results = {};
  function done(name, value) {
    results[name] = value;
    if (Object.keys(results).length === 5) {
      document.getElementById('a-probes').setAttribute('data-done', JSON.stringify(results));
    }
  }
  var x = new XMLHttpRequest();
  x.open('GET', CDN + '/assets/data');
  x.onload = function () { done('xhr', 'reached'); };
  x.onerror = function () { done('xhr', 'blocked'); };
  x.send();
  fetch(CDN + '/assets/data').then(function () { done('fetch', 'reached'); },
                                   function () { done('fetch', 'blocked'); });
  fetch(CDN + '/assets/data', {method: 'POST', body: 'x', mode: 'no-cors'}).then(
    function () { done('post', 'reached'); }, function () { done('post', 'blocked'); });
  try { navigator.sendBeacon(CDN + '/assets/beacon', 'x'); } catch (e) {}
  done('beacon', 'sent');
  var socket = new WebSocket('wss://' + CDN.slice(8) + '/assets/socket');
  socket.onclose = function () { done('socket', 'closed'); };
})();
"""
)

_ASSET_HEAD = (
    '<style>#app{display:none}</style><link rel="stylesheet" href="' + _CDN + '/assets/login.css">'
)


def _asset_response(body: str, media_type: str) -> Response:
    return Response(
        body,
        media_type=media_type,
        headers={
            "set-cookie": "CDN_TRACK=1; Path=/; Secure; SameSite=None",  # the CDN's own cookie
            "access-control-allow-origin": "*",
            "cache-control": "no-store",
        },
    )


@router.get("/assets/login", response_class=HTMLResponse)
async def assets_login(request: Request) -> str:
    """The form is built by a script from the asset host; its style sheet shows it."""
    _note(request)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Вход — внешние файлы</title>"
        + _ASSET_HEAD
        + f'<script src="{_CDN}/assets/login.js" defer></script></head>'
        "<body><div id='app'></div></body></html>"
    )


@router.get("/assets/login.css")
async def assets_css(request: Request) -> Response:
    _note(request)
    return _asset_response(_ASSET_CSS, "text/css")


@router.get("/assets/login.js")
async def assets_js(request: Request) -> Response:
    _note(request)
    return _asset_response(_ASSET_JS, "application/javascript")


@router.get("/assets/enable.js")
async def assets_enable_js(request: Request) -> Response:
    """The submit button of the identity page is disabled until this script runs."""
    _note(request)
    return _asset_response(
        "document.addEventListener('DOMContentLoaded', function () {"
        " document.getElementById('go').disabled = false; });",
        "application/javascript",
    )


@router.get("/assets/pixel.png")
async def assets_pixel(request: Request) -> Response:
    _note(request)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    return Response(png, media_type="image/png", headers={"cache-control": "no-store"})


@router.get("/assets/data")
async def assets_data(request: Request) -> JSONResponse:
    _note(request)
    return JSONResponse({"ok": True}, headers={"access-control-allow-origin": "*"})


@router.post("/assets/data")
async def assets_data_post(request: Request) -> JSONResponse:
    _note(request, await request.body())
    return JSONResponse({"ok": True}, headers={"access-control-allow-origin": "*"})


@router.post("/assets/beacon")
async def assets_beacon(request: Request) -> Response:
    _note(request)
    return Response(status_code=204)


@router.websocket("/assets/socket")
async def assets_socket(socket: WebSocket) -> None:
    _note(socket)
    await socket.accept()
    await socket.close()


@router.get("/assets/document", response_class=HTMLResponse)
async def assets_document(request: Request) -> str:
    """A document served by the asset host: the page of a site the student never approved."""
    _note(request)
    return _page("Чужой документ", "<p id='foreign'>Страница стороннего сайта</p>")


@router.get("/assets/plain", response_class=HTMLResponse)
async def assets_plain(request: Request) -> str:
    """An ordinary form (posting to /plain/login) on a page that also loads CDN assets."""
    _note(request)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Вход</title>"
        f'<link rel="stylesheet" href="{_CDN}/assets/login.css">'
        f'<script src="{_CDN}/assets/enable.js" defer></script></head><body>'
        '<form id="plain" method="post" action="/plain/login">'
        '<label for="u">Логин</label><input id="u" name="login" type="text" autocomplete="username">'
        '<label for="p">Пароль</label>'
        '<input id="p" name="pass" type="password" autocomplete="current-password">'
        '<button id="go" type="submit" disabled>Войти</button></form></body></html>'
    )


@router.get("/sso-assets/start", response_model=None)
async def sso_assets_start() -> RedirectResponse:
    return RedirectResponse("https://id.fakeuni.test/sso-assets/login", status_code=302)


@router.get("/sso-assets/login", response_class=HTMLResponse)
async def sso_assets_login(request: Request) -> str:
    """The identity host of the incident: its submit button is enabled by a script from a CDN."""
    _note(request)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Единый вход</title>"
        f'<link rel="stylesheet" href="{_CDN}/assets/login.css">'
        f'<script src="{_CDN}/assets/enable.js" defer></script></head><body>'
        '<form id="sso" method="post" action="/sso/login">'
        '<label for="login">Логин</label>'
        '<input id="login" name="username" type="text" autocomplete="username">'
        '<label for="pw">Пароль</label>'
        '<input id="pw" name="password" type="password" autocomplete="current-password">'
        '<button id="go" type="submit" disabled>Продолжить</button></form></body></html>'
    )


@router.get("/assets/arm.js")
async def assets_arm_js(request: Request) -> Response:
    """A script from any host: asking for it is what makes the host an asset host of a context."""
    _note(request)
    return _asset_response("/* an asset */", "application/javascript")


@router.get("/assets/x.png")
async def assets_exfil_pixel(request: Request) -> Response:
    """An image whose address carries data: what a page uses to send a value out by GET."""
    _note(request)
    return await assets_pixel(request)


@router.api_route("/redirect/open", methods=["GET", "POST"])
async def redirect_open(to: str, code: int = 302) -> RedirectResponse:
    """An open redirect, as some sites have: to anywhere, with the status the caller asks for."""
    return RedirectResponse(to, status_code=code)


@router.get("/assets/legacy", response_class=HTMLResponse)
async def assets_legacy(request: Request) -> HTMLResponse:
    """A page with a legacy referrer policy: its whole address (with the query) goes to a CDN."""
    _note(request)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>Вход</title>"
        + _ASSET_HEAD
        + f'<script src="{_CDN}/assets/login.js" defer></script></head>'
        "<body><div id='app'></div></body></html>",
        headers={"referrer-policy": "unsafe-url"},
    )


@router.get("/sibling/login", response_class=HTMLResponse)
async def sibling_login(request: Request, needed: int = 0) -> HTMLResponse:
    """A form whose files come from a sibling under the domain of a session cookie the page itself
    sets (`Domain=.fakeuni.test`): a browser would send that cookie to the sibling. With `needed`,
    the submit button stays disabled until the script from the sibling runs."""
    _note(request)
    sibling = f"https://{SIBLING_HOST}"
    script = f'<script src="{sibling}/assets/enable.js" defer></script>' if needed else ""
    button = '<button id="go" type="submit"' + (" disabled" if needed else "") + ">Войти</button>"
    response = HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>Вход</title>"
        f'<link rel="stylesheet" href="{sibling}/assets/login.css">{script}</head><body>'
        '<form id="plain" method="post" action="/plain/login">'
        '<label for="u">Логин</label><input id="u" name="login" type="text" autocomplete="username">'
        '<label for="p">Пароль</label>'
        '<input id="p" name="pass" type="password" autocomplete="current-password">'
        f"{button}</form></body></html>"
    )
    response.set_cookie("SRC_DOMAIN", "1", domain="fakeuni.test", secure=True, httponly=True)
    return response


@router.get("/assets/sw.js")
async def assets_service_worker(request: Request) -> Response:
    """A service worker that, as soon as it is installed, posts to the asset host."""
    _note(request)
    return Response(
        "self.addEventListener('install', () => {"
        f" fetch('{_CDN}/assets/data', {{method: 'POST', body: 'from-the-worker'}}).catch(() => {{}});"
        " });",
        media_type="application/javascript",
        headers={"cache-control": "no-store"},
    )
