"""Owner: W2. The synthetic university's ways in.

* `/auth/login` → `/auth/otp` → `/app`: Keycloak-like username+password, then an SMS code (default
  path; `/` redirects here through `/app`).
* `/plain/login`: username+password only.
* `/two/username` → `/two/password`: the two-page flow.
* `/push/login` → `/push/wait`: wait for an approval on a phone (tests call `approve_push()`).
* `/captcha/login`: a form with a captcha stand-in iframe that a human has to tick.
* `/sso/start` → `id.fakeuni.test/sso/login` → `/sso/callback`: login on a second host.
* `/blocked`: the site refuses the visit (bot protection page).

Wrong credentials re-render the same form with an error, like real sites. Every successful path
ends with the `FAKEUNI_SESSION` cookie on fakeuni.test. Nothing here resembles a real university.
"""

from __future__ import annotations

import base64
import json
import secrets
from html import escape

from fastapi import APIRouter, Form, Request
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


def _page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><title>{escape(title)}</title></head><body>{body}</body></html>"


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
