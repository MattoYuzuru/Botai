"""Reading a login page as steps: which fields mean login, password, one-time code.

Pure functions over `PageSnapshot`, no browser. Nothing here knows a university: the roles come
from standard HTML hints (`type`, `autocomplete`, `inputmode`) and common words in names and
labels. A step replayed from a learned preset takes precedence over these guesses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from botai.contracts.specs import LoginField, LoginStep
from botai.core.browser import FormField, FormInfo, PageSnapshot

Role = Literal["username", "password", "otp", "text", "checkbox"]

_OTP = re.compile(
    r"otp|totp|one[-_ ]?time|verif|passcode|2fa|mfa|\bsms\b|\bpin\b|\bcode\b|код|подтвержд",
    re.IGNORECASE,
)
_USER = re.compile(
    r"user|login|e-?mail|\bmail\b|account|uname|логин|почт|пользовател|идентификатор|"
    r"student|студент",
    re.IGNORECASE,
)
_SEARCH = re.compile(r"search|поиск|найти|искать", re.IGNORECASE)
_LOGIN_FIELD = re.compile(r"username|uname|\blogin\b|логин|имя пользовател", re.IGNORECASE)
_AUTH_CODE = re.compile(
    r"otp|totp|one[-_ ]?time|passcode|2fa|mfa|\bsms\b|\bpin\b|"
    r"verification|код[^.]{0,40}(?:подтвержден|смс|sms)",
    re.IGNORECASE,
)
_AUTH_CONTEXT = re.compile(
    r"log[-_ ]?in|sign[-_ ]?in|\bauth(?:entication)?\b|\bsso\b|вход|войти|авторизац",
    re.IGNORECASE,
)
_NEWSLETTER = re.compile(r"newsletter|subscrib|подписк|рассылк", re.IGNORECASE)
_PUSH = re.compile(
    r"подтвердите[^.]{0,60}(?:на телефон|в приложени|push|уведомлени)|"
    r"(?:push|пуш)[- ]?уведомлени|запрос на вход[^.]{0,40}(?:отправлен|телефон)|"
    r"approve[^.]{0,40}(?:phone|device|app)|check your (?:phone|device)|"
    r"ожида(?:ем|ю)[^.]{0,30}подтвержден|waiting for (?:approval|confirmation)",
    re.IGNORECASE,
)
_BLOCK = re.compile(
    r"access denied|доступ запрещ|forbidden|you have been blocked|вы заблокирован|"
    r"too many requests|слишком много запросов|unusual traffic|необычн\w+ трафик|"
    r"request blocked|подозрительн\w+ активност",
    re.IGNORECASE,
)
_PERSONAL = re.compile(
    r"@|\*{2,}|\d{4,}|\+?\d(?:[\s().-]?\d){6,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}|\beyJ[\w-]{6,}"
)
_SECRETISH = re.compile(
    r"секрет|secret|парол|password|\bpin\b|пин[- ]?код|passcode|кодов\w+ слов|security answer|"
    r"ответ на (?:секретный )?вопрос",
    re.IGNORECASE,
)
_IDLIKE_SEGMENT = re.compile(r"^(?=.*\d)[\w-]{16,}$|^\d{5,}$|^[0-9a-fA-F]{8}-")

DEFAULT_LABEL: dict[str, str] = {
    "username": "Логин",
    "password": "Пароль",
    "otp": "Код подтверждения",
    "text": "Значение",
    "checkbox": "Согласие",
}


@dataclass(frozen=True, slots=True)
class StepField:
    role: Role
    selector: str
    label: str  # as shown to the user; learned presets store `safe_label` of it
    input_type: str = "text"
    autocomplete: str | None = None
    maxlength: int | None = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class FormStep:
    """A page the user's native fields can stand in for."""

    id: str
    form_selector: str
    fields: tuple[StepField, ...]
    submit: str | None
    url: str

    @property
    def signature(self) -> tuple[tuple[str, str], ...]:
        """The same fields twice in a row mean the page refused what was submitted. The path is
        left out on purpose: sites re-render a refusal at another address than the form's."""
        return tuple(sorted((f.role, f.selector) for f in self.fields))

    @property
    def roles(self) -> set[str]:
        return {f.role for f in self.fields}


@dataclass(frozen=True, slots=True)
class PushWait:
    pass


@dataclass(frozen=True, slots=True)
class CaptchaPage:
    pass


@dataclass(frozen=True, slots=True)
class HardBlock:
    """The site refuses the visit itself; a human at the screen cannot fix that."""


@dataclass(frozen=True, slots=True)
class ErrorPage:
    """Chromium's own error page: the navigation failed."""


@dataclass(frozen=True, slots=True)
class UnknownPage:
    reason: str  # for the logs and the screen hint, never page content


Detection = FormStep | PushWait | CaptchaPage | HardBlock | ErrorPage | UnknownPage


def _is_search(field: FormField) -> bool:
    hint = " ".join(filter(None, (field.name, field.label)))
    return field.input_type.lower() == "search" or bool(_SEARCH.search(hint))


def role_of(field: FormField) -> Role | Literal["newpassword"] | None:
    kind = (field.input_type or "text").lower()
    visible = getattr(field, "visible", True)
    if (
        kind in ("hidden", "submit", "button", "reset", "image", "file", "range", "color")
        or not visible
        or _is_search(field)
    ):
        return None
    # These controls can occur in ordinary data forms. A word such as "student"
    # or "user" in their name must not turn them into a login or one-time code.
    if kind in ("url", "textarea", "select"):
        return "text"
    ac = (field.autocomplete or "").lower()
    if "new-password" in ac:
        return "newpassword"
    if kind == "password" or "current-password" in ac:
        return "password"
    if kind in ("checkbox", "radio"):
        return "checkbox"
    if "one-time-code" in ac:
        return "otp"
    if "username" in ac or ac == "email":
        return "username"
    hint = " ".join(filter(None, (field.name, field.label)))
    if _OTP.search(hint):
        return "otp"
    inputmode = (getattr(field, "inputmode", None) or "").lower()
    maxlength = getattr(field, "maxlength", None)
    if inputmode in ("numeric", "tel") and maxlength is not None and 4 <= maxlength <= 8:
        return "otp"
    if kind == "email" or _USER.search(hint):
        return "username"
    if kind in ("text", "tel", "number"):
        return "text"
    return None


def looks_personal(text: str) -> bool:
    """E-mail, long digit runs, UUIDs, JWT starts: nothing a preset may carry (check C4)."""
    return bool(_PERSONAL.search(text))


def safe_label(label: str | None, role: str) -> str:
    if label and len(label) <= 60 and not looks_personal(label):
        return label
    return DEFAULT_LABEL.get(role, "Значение")


def secretish(label: str | None) -> bool:
    """A field of an unknown kind whose label says its answer is a secret (a security question,
    a PIN): it is asked like a password, never kept as plain text in the job."""
    return bool(label and _SECRETISH.search(label))


def safe_path(path: str, avoid: Sequence[str] = ()) -> str:
    """The path up to the first segment that looks like an id or a session handle, or that holds
    something the student typed (`avoid`). Matrix parameters (`;jsessionid=...`) are dropped."""
    kept: list[str] = []
    for raw in path.split("/"):
        segment = raw.split(";", 1)[0]
        lowered = segment.lower()
        if segment and (_IDLIKE_SEGMENT.match(segment) or any(t in lowered for t in avoid)):
            break
        kept.append(segment)
    return "/".join(kept) or "/"


def login_area(path: str, avoid: Sequence[str] = ()) -> str:
    """`/auth/login` → `/auth/`; a one-segment path stays itself. Used for expiry detection."""
    clean = safe_path(path, avoid)
    segments = [s for s in clean.split("/") if s]
    if len(segments) >= 2:
        return "/" + "/".join(segments[:-1]) + "/"
    return clean


def _score(fields: list[StepField]) -> int:
    weights = {"password": 4, "otp": 4, "username": 3, "text": 1, "checkbox": 0}
    return sum(weights[f.role] for f in fields)


def _step_id(fields: list[StepField]) -> str:
    roles: list[str] = []
    for f in fields:
        if f.role != "checkbox" and f.role not in roles:
            roles.append(f.role)
    return "-".join(roles)[:30] or "form"


def _auth_context(form: FormInfo, url: str, title: str) -> bool:
    form_hint = " ".join((form.selector, form.action or "")).replace("_", " ")
    if _NEWSLETTER.search(form_hint):
        return False
    return bool(_AUTH_CONTEXT.search(" ".join((form_hint, urlsplit(url).path, title))))


def _auth_field(field: FormField, *, context: bool) -> bool:
    """A credential signal, rather than merely a fillable control or an e-mail address."""
    role = role_of(field)
    ac = (field.autocomplete or "").lower()
    hint = " ".join(filter(None, (field.name, field.label)))
    if role in ("password", "newpassword"):
        return True
    if role == "username":
        return context or "username" in ac or bool(_LOGIN_FIELD.search(hint))
    if role == "otp":
        return context or "one-time-code" in ac or bool(_AUTH_CODE.search(hint))
    return role == "text" and secretish(field.label)


def _step_from_form(form: FormInfo, url: str, title: str = "") -> FormStep | UnknownPage | None:
    context = _auth_context(form, url, title)
    if not any(_auth_field(f, context=context) for f in form.fields):
        return None
    fields: list[StepField] = []
    password_fields = 0
    for f in form.fields:
        role = role_of(f)
        if role is None:
            continue
        if role == "newpassword":
            return UnknownPage("password change or registration form")
        if role == "password":
            password_fields += 1
        fields.append(
            StepField(
                role=role,
                selector=f.selector,
                label=f.label or DEFAULT_LABEL[role],
                input_type=f.input_type,
                autocomplete=f.autocomplete,
                maxlength=getattr(f, "maxlength", None),
                required=bool(getattr(f, "required", False)),
            )
        )
    if password_fields > 1:
        return UnknownPage("several password fields")
    if any(f.role == "checkbox" and f.required for f in fields):
        return UnknownPage("a required consent checkbox")
    fillable = [f for f in fields if f.role != "checkbox"]
    if not fillable:
        return None
    if any(f.role == "text" and not secretish(f.label) for f in fillable):
        return UnknownPage("unrecognised extra fields in a login form")
    return FormStep(
        id=_step_id(fillable),
        form_selector=form.selector,
        fields=tuple(fillable),
        submit=form.submit_selector,
        url=url,
    )


def best_step(snapshot: PageSnapshot) -> FormStep | UnknownPage | None:
    """The form to mirror on this page (captcha or not), an UnknownPage if every candidate is
    something we must not fill (password change, required consent), or None."""
    steps: list[tuple[int, FormStep]] = []
    problem: UnknownPage | None = None
    for form in snapshot.forms:
        built = _step_from_form(form, snapshot.url, snapshot.title)
        if isinstance(built, UnknownPage):
            problem = problem or built
        elif built is not None:
            steps.append((_score(list(built.fields)), built))
    if steps:
        return max(steps, key=lambda pair: pair[0])[1]
    return problem


def detect(snapshot: PageSnapshot) -> Detection:
    """What the current page asks for."""
    if snapshot.url.startswith("chrome-error:"):
        return ErrorPage()
    if snapshot.captcha_suspected:
        return CaptchaPage()
    found = best_step(snapshot)
    if found is not None:
        return found
    if _BLOCK.search(snapshot.title + " " + snapshot.text[:600]):
        return HardBlock()
    if _PUSH.search(snapshot.text[:3000]):
        return PushWait()
    return UnknownPage("no fields to mirror")


def has_credential_fields(snapshot: PageSnapshot) -> bool:
    """True if the page asks for a login, password or code (so it cannot be the logged-in page)."""
    for form in snapshot.forms:
        if _step_from_form(form, snapshot.url, snapshot.title) is not None:
            return True
    return False


# -- learned steps ---------------------------------------------------------------------------


def _present(html: str, selector: str) -> bool:
    try:
        return LexborHTMLParser(html).css_first(selector) is not None
    except Exception:  # selectolax rejects selectors it does not support
        return False


def _replay_form(form: FormInfo, known: Sequence[LoginField], snapshot: PageSnapshot) -> bool:
    """Validate the live form with the roles already learned for the same compatible controls."""
    form_hint = " ".join((form.selector, form.action or ""))
    context = _auth_context(form, snapshot.url, snapshot.title)
    if _NEWSLETTER.search(form_hint):
        return False
    if not context and not any(_auth_field(f, context=False) for f in form.fields):
        return False
    fields: list[FormField] = []
    hints = {"username": "username", "password": "current-password", "otp": "one-time-code"}
    compatible = {
        "username": {"text", "email", "tel", "number"},
        "password": {"text", "password"},
        "otp": {"text", "tel", "number"},
    }
    by_selector = {field.selector: field for field in known}
    for current in form.fields:
        learned = by_selector.get(current.selector)
        if learned is not None and learned.role in hints:
            ac = (current.autocomplete or "").lower()
            if (
                _is_search(current)
                or current.input_type.lower() not in compatible[learned.role]
                or "new-password" in ac
            ):
                return False
            explicit = next((role for role, hint in hints.items() if hint in ac), None)
            if explicit is not None and explicit != learned.role:
                return False
            # This is classifier-only metadata on a copy. The live DOM and its
            # values stay untouched; arbitrary new selectors receive no hints.
            current = current.model_copy(update={"autocomplete": hints[learned.role]})
        elif learned is not None and learned.role == "text" and secretish(learned.label):
            current = current.model_copy(update={"label": learned.label})
        fields.append(current)
    live = _step_from_form(form.model_copy(update={"fields": fields}), snapshot.url, snapshot.title)
    return isinstance(live, FormStep) and {field.selector for field in live.fields} == {
        field.selector for field in known if field.role != "checkbox"
    }


def replay(
    steps: list[LoginStep], snapshot: PageSnapshot
) -> FormStep | PushWait | CaptchaPage | None:
    """The learned step that matches this page, if its selectors are all still there."""
    parts = urlsplit(snapshot.url)
    live = {f.selector: f for form in snapshot.forms for f in form.fields}
    for step in steps:
        match = step.match
        if (host := match.get("urlHost")) and host.lower() != (parts.hostname or "").lower():
            continue
        if (prefix := match.get("urlPathPrefix")) and not parts.path.startswith(prefix):
            continue
        if (probe := match.get("selectorPresent")) and not _present(snapshot.html, probe):
            continue
        if step.kind == "push_wait":
            return PushWait()
        if step.kind == "captcha":
            return CaptchaPage()
        if step.kind != "form" or not step.fields:
            continue
        if not all(f.selector in live for f in step.fields):
            continue
        # Old presets may have learned an ordinary search box as a login step.
        # Live search semantics override those stale roles; the detector can still
        # choose a separate genuine login form on the same page.
        if any(_is_search(live[f.selector]) for f in step.fields):
            continue
        fields = tuple(
            StepField(
                role=f.role,
                selector=f.selector,
                label=f.label,
                input_type=live[f.selector].input_type,
                autocomplete=live[f.selector].autocomplete,
                maxlength=getattr(live[f.selector], "maxlength", None),
                required=bool(getattr(live[f.selector], "required", False)),
            )
            for f in step.fields
            if f.role != "checkbox"
        )
        if not fields:
            continue
        if not any(f.role in ("username", "password", "otp") or secretish(f.label) for f in fields):
            continue
        form_info = next(
            (
                frm
                for frm in snapshot.forms
                if fields[0].selector in {x.selector for x in frm.fields}
            ),
            None,
        )
        if form_info is None or not _replay_form(form_info, step.fields, snapshot):
            continue
        return FormStep(
            id=step.id,
            form_selector=form_info.selector,
            fields=fields,
            submit=step.submit,
            url=snapshot.url,
        )
    return None


def generic_selector(field: StepField) -> str | None:
    """A selector without ids or generated names, for presets when the real one looks personal."""
    if field.role == "password":
        return 'input[type="password"]'
    if field.autocomplete in ("username", "one-time-code", "email"):
        return f'input[autocomplete="{field.autocomplete}"]'
    return None


def as_preset_step(step: FormStep, avoid: Sequence[str] = ()) -> LoginStep | None:
    """A step safe to store: no personal values anywhere. None when it cannot be made safe.
    `avoid` are lower-case fragments of what the student typed (login, local part of an e-mail)."""

    def known(text: str) -> bool:
        return looks_personal(text) or any(t in text.lower() for t in avoid)

    fields: list[LoginField] = []
    for f in step.fields:
        selector = f.selector
        if known(selector) or len(selector) > 300:
            alternative = generic_selector(f)
            if alternative is None:
                return None
            selector = alternative
        label = safe_label(f.label, f.role)
        if known(label):
            label = DEFAULT_LABEL.get(f.role, "Значение")
        fields.append(LoginField(role=f.role, selector=selector, label=label))
    parts = urlsplit(step.url)
    match: dict[str, str] = {
        "urlHost": (parts.hostname or "").lower(),
        "urlPathPrefix": safe_path(parts.path, avoid),
    }
    if len(fields[0].selector) <= 300:
        match["selectorPresent"] = fields[0].selector
    submit = step.submit if step.submit and not known(step.submit) else None
    return LoginStep(id=step.id, match=match, fields=fields, submit=submit, kind="form")
