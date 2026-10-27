"""Reading login pages as steps (pure, no browser)."""

from __future__ import annotations

import pytest

from botai.browser.forms import (
    CaptchaPage,
    ErrorPage,
    FormStep,
    HardBlock,
    PushWait,
    StepField,
    UnknownPage,
    as_preset_step,
    best_step,
    detect,
    has_credential_fields,
    login_area,
    looks_personal,
    replay,
    role_of,
    safe_label,
    safe_path,
    secretish,
)
from botai.browser.pagescan import RichField
from botai.contracts.specs import LoginField, LoginStep
from botai.core.browser import FormInfo, PageSnapshot


def field(selector: str = "#f", **kw: object) -> RichField:
    return RichField(selector=selector, **kw)  # type: ignore[arg-type]


def page(*forms: FormInfo, url: str = "https://uni.test/login", **kw: object) -> PageSnapshot:
    return PageSnapshot(url=url, forms=list(forms), **kw)  # type: ignore[arg-type]


def form(*fields: RichField, submit: str | None = "#go", selector: str = "#form") -> FormInfo:
    return FormInfo(selector=selector, fields=list(fields), submit_selector=submit)


@pytest.mark.parametrize(
    ("kw", "role"),
    [
        ({"input_type": "password"}, "password"),
        ({"input_type": "text", "autocomplete": "current-password"}, "password"),
        ({"input_type": "text", "autocomplete": "new-password"}, "newpassword"),
        ({"input_type": "email"}, "username"),
        ({"input_type": "text", "autocomplete": "username"}, "username"),
        ({"input_type": "text", "name": "login"}, "username"),
        ({"input_type": "text", "label": "Электронная почта"}, "username"),
        ({"input_type": "text", "autocomplete": "one-time-code"}, "otp"),
        ({"input_type": "text", "name": "otp"}, "otp"),
        ({"input_type": "text", "label": "Код из SMS"}, "otp"),
        ({"input_type": "text", "inputmode": "numeric", "maxlength": 6}, "otp"),
        ({"input_type": "tel", "inputmode": "numeric", "maxlength": 4}, "otp"),
        ({"input_type": "text", "name": "q"}, "text"),
        ({"input_type": "checkbox"}, "checkbox"),
        ({"input_type": "hidden"}, None),
        ({"input_type": "text", "visible": False}, None),
        ({"input_type": "file"}, None),
    ],
)
def test_role_of(kw: dict[str, object], role: str | None) -> None:
    assert role_of(field(**kw)) == role


def test_username_and_password_form() -> None:
    snap = page(
        form(
            field("#u", input_type="email", label="Почта", autocomplete="username"),
            field("#p", input_type="password", label="Пароль"),
        )
    )
    step = detect(snap)
    assert isinstance(step, FormStep)
    assert [f.role for f in step.fields] == ["username", "password"]
    assert step.id == "username-password"
    assert step.submit == "#go"
    assert has_credential_fields(snap)


def test_otp_only_form() -> None:
    step = detect(page(form(field("#c", autocomplete="one-time-code", maxlength=6))))
    assert isinstance(step, FormStep)
    assert step.id == "otp"
    assert step.fields[0].maxlength == 6


def test_best_of_several_forms_prefers_the_credentials() -> None:
    search = form(field("#q", input_type="search", name="q"), selector="#search")
    login = form(field("#p", input_type="password"), selector="#login")
    step = detect(page(search, login))
    assert isinstance(step, FormStep)
    assert step.form_selector == "#login"


def test_a_search_box_alone_is_not_a_credential() -> None:
    snap = page(form(field("#q", input_type="search", name="q")))
    assert not has_credential_fields(snap)


@pytest.mark.parametrize(
    "bad",
    [
        form(
            field("#a", autocomplete="new-password", input_type="password"),
            field("#b", input_type="password"),
        ),
        form(field("#p1", input_type="password"), field("#p2", input_type="password")),
        form(
            field("#u", input_type="text", name="login"),
            field("#t", input_type="checkbox", required=True),
        ),
    ],
)
def test_forms_we_must_not_fill_go_to_the_human(bad: FormInfo) -> None:
    assert isinstance(detect(page(bad)), UnknownPage)
    assert isinstance(best_step(page(bad)), UnknownPage)


def test_optional_checkbox_is_left_alone() -> None:
    step = detect(
        page(form(field("#u", name="login"), field("#r", input_type="checkbox", label="Запомнить")))
    )
    assert isinstance(step, FormStep)
    assert [f.role for f in step.fields] == ["username"]


def test_captcha_beats_a_form() -> None:
    snap = page(form(field("#p", input_type="password")), captcha_suspected=True)
    assert isinstance(detect(snap), CaptchaPage)
    assert isinstance(best_step(snap), FormStep)  # the caller can still mirror the fields


def test_push_wait_page() -> None:
    snap = page(text="Подтвердите вход на телефоне. Мы отправили запрос в приложение.")
    assert isinstance(detect(snap), PushWait)
    assert isinstance(detect(page(text="Check your phone and approve the request")), PushWait)


def test_hard_block_and_chrome_error_and_unknown() -> None:
    assert isinstance(
        detect(page(title="Access denied", text="Your request was blocked")), HardBlock
    )
    assert isinstance(detect(page(url="chrome-error://chromewebdata/")), ErrorPage)
    assert isinstance(detect(page(text="Войти через Госуслуги")), UnknownPage)


def test_a_form_beats_the_word_forbidden_in_the_text() -> None:
    snap = page(form(field("#p", input_type="password")), text="Forbidden characters: <>")
    assert isinstance(detect(snap), FormStep)


# -- values that must never reach a preset ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ivan.petrov@uni.test",
        "Код отправлен на +7 900 123-45-67",
        "student-12345678",
        "3f2b8c1e-4a5d-4e6f",
        "eyJhbGciOiJub25l",
    ],
)
def test_personal_looking_text_is_recognised(text: str) -> None:
    assert looks_personal(text)


@pytest.mark.parametrize("text", ["Почта", "Код из SMS", "Пароль", "input#username", "/auth/otp"])
def test_plain_text_is_not_personal(text: str) -> None:
    assert not looks_personal(text)


def test_safe_label_falls_back_to_the_role_default() -> None:
    assert safe_label("Код из SMS", "otp") == "Код из SMS"
    assert safe_label("Код отправлен на i***@mail.ru", "otp") == "Код подтверждения"
    assert safe_label(None, "password") == "Пароль"
    assert safe_label("x" * 100, "username") == "Логин"


def test_paths_are_cut_before_id_like_segments() -> None:
    assert (
        safe_path("/realms/uni/login-actions/authenticate")
        == "/realms/uni/login-actions/authenticate"
    )
    assert safe_path("/login/9f8e7d6c5b4a39281706") == "/login"
    assert safe_path("/session/12345678/form") == "/session"
    assert safe_path("/") == "/"


def test_matrix_parameters_and_typed_names_never_stay_in_a_path() -> None:
    assert safe_path("/cas/login;jsessionid=A1B2C3D4") == "/cas/login"
    assert safe_path("/students/ivanov-ivan/dashboard", ["ivanov"]) == "/students"
    assert safe_path("/students/ivanov-ivan/dashboard") == "/students/ivanov-ivan/dashboard"
    assert login_area("/u/ivanov/login", ["ivanov"]) == "/u"


def test_masked_contact_details_are_personal() -> None:
    assert looks_personal("Код на +7 9** *** ** 12")
    assert looks_personal("Код на i***@mail.ru")
    assert safe_label("Код на +7 9** *** ** 12", "otp") == "Код подтверждения"


def test_security_questions_are_secrets() -> None:
    for label in ("Ответ на секретный вопрос", "PIN", "Секретное слово", "Security answer"):
        assert secretish(label), label
    for label in ("Группа", "Номер студенческого", None):
        assert not secretish(label), label


def test_a_step_never_stores_what_the_student_typed() -> None:
    step = FormStep(
        id="username",
        form_selector="#f",
        fields=(
            StepField(
                role="username",
                selector="#ivanov-login",
                label="Логин ivanov",
                autocomplete="username",
            ),
        ),
        submit="#go",
        url="https://uni.test/u/ivanov/login",
    )
    learned = as_preset_step(step, ["ivanov"])
    assert learned is not None
    assert "ivanov" not in learned.model_dump_json()
    assert learned.fields[0].selector == 'input[autocomplete="username"]'
    assert learned.fields[0].label == "Логин"
    assert learned.match["urlPathPrefix"] == "/u"


def test_a_form_signature_ignores_the_address() -> None:
    fields = (StepField(role="password", selector="#p", label="Пароль"),)
    a = FormStep(id="p", form_selector="#f", fields=fields, submit=None, url="https://u.test/a")
    b = FormStep(id="p", form_selector="#f", fields=fields, submit=None, url="https://u.test/b/c")
    assert a.signature == b.signature


def test_login_area() -> None:
    assert login_area("/auth/login") == "/auth/"
    assert login_area("/login") == "/login"
    assert (
        login_area("/realms/uni/protocol/openid-connect/auth")
        == "/realms/uni/protocol/openid-connect/"
    )


def test_learned_step_has_no_personal_values() -> None:
    step = FormStep(
        id="otp",
        form_selector="#f",
        fields=(
            StepField(
                role="otp",
                selector="#input-9876543210",
                label="Код отправлен на i***@mail.ru",
                autocomplete="one-time-code",
            ),
        ),
        submit="#go",
        url="https://uni.test/auth/session-7f3a9c1d2b4e5f6a7b8c/otp",
    )
    learned = as_preset_step(step)
    assert learned is not None
    dumped = learned.model_dump_json()
    assert not looks_personal(dumped.replace("https://", ""))
    assert learned.fields[0].selector == 'input[autocomplete="one-time-code"]'
    assert learned.fields[0].label == "Код подтверждения"
    assert learned.match["urlPathPrefix"] == "/auth"


def test_step_with_an_unsafe_selector_and_no_generic_one_is_not_learned() -> None:
    step = FormStep(
        id="text",
        form_selector="#f",
        fields=(StepField(role="text", selector="#field-98765432", label="Группа"),),
        submit=None,
        url="https://uni.test/x",
    )
    assert as_preset_step(step) is None


# -- replaying a learned step --------------------------------------------------------------------


def test_replay_uses_the_learned_roles_even_when_the_page_gives_no_hints() -> None:
    snap = page(
        form(field("#a1", input_type="text"), field("#a2", input_type="password")),
        url="https://uni.test/odd/login",
        html='<form id="form"><input id="a1"><input id="a2" type="password"></form>',
    )
    learned = [
        LoginStep(
            id="odd",
            match={"urlHost": "uni.test", "urlPathPrefix": "/odd", "selectorPresent": "#a1"},
            fields=[
                LoginField(role="username", selector="#a1", label="Логин"),
                LoginField(role="password", selector="#a2", label="Пароль"),
            ],
            submit="#go",
        )
    ]
    step = replay(learned, snap)
    assert isinstance(step, FormStep)
    assert [(f.role, f.selector) for f in step.fields] == [("username", "#a1"), ("password", "#a2")]
    assert step.submit == "#go"
    assert step.form_selector == "#form"


def test_replay_skips_a_step_whose_selectors_are_gone_or_whose_match_differs() -> None:
    learned = [
        LoginStep(
            id="old",
            match={"urlHost": "uni.test", "urlPathPrefix": "/login"},
            fields=[LoginField(role="password", selector="#gone", label="Пароль")],
        ),
        LoginStep(
            id="elsewhere",
            match={"urlHost": "other.test"},
            fields=[LoginField(role="password", selector="#p", label="Пароль")],
        ),
    ]
    snap = page(form(field("#p", input_type="password")))
    assert replay(learned, snap) is None


def test_replay_of_push_and_captcha_kinds() -> None:
    snap = page(url="https://uni.test/wait")
    push = LoginStep(id="push", match={"urlPathPrefix": "/wait"}, fields=[], kind="push_wait")
    captcha = LoginStep(id="cap", match={"urlPathPrefix": "/wait"}, fields=[], kind="captcha")
    assert isinstance(replay([push], snap), PushWait)
    assert isinstance(replay([captcha], snap), CaptchaPage)
