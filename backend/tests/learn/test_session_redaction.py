"""Known session values never become planner/author evidence, including refresh and reauth."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr

from botai.browser.session import encode_session
from botai.core.browser import (
    FakeBrowserGateway,
    FakeBrowserHandle,
    FakePage,
    ObservedRequest,
    SessionCookie,
    SessionOriginStorage,
    SessionState,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import ScriptedLlm
from botai.core.vault import SecretKind
from botai.learn.discovery import adaptive_observe
from botai.learn.intent import LearningIntent
from botai.learn.redact import Redactor
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.learn.test_discovery import plan
from tests.sources.helpers import NOW
from tests.sources.support import FakeSessions

ORIGIN = "https://fakeuni.test"


def protected_shapes() -> Redactor:
    state = SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        cookies=[
            SessionCookie(name="sid", domain="fakeuni.test", value=SecretStr("opaqueprivatecookie"))
        ],
        browser_storage=[
            SessionOriginStorage(
                origin=ORIGIN,
                local_storage={
                    "neutral": SecretStr(
                        json.dumps(
                            {
                                "neutral": 4321,
                                "when": "2026-10-29",
                                "timestamp": 1_800_000_000,
                            }
                        )
                    )
                },
            )
        ],
    )
    redactor = Redactor()
    redactor.protect_session(state)
    return redactor


@pytest.mark.parametrize("value", [4321, 1_800_000_000, "2026-10-29"])
def test_protected_json_scalar_overrides_numeric_date_and_timestamp_shapes(value: object) -> None:
    redactor = protected_shapes()
    assert str(value) not in redactor.json_text({"neutral": value}, 1000)


@pytest.mark.parametrize(
    "encoded", ["opaqueprivatecookie", "%6fpaqueprivatecookie", "%256fpaqueprivatecookie"]
)
def test_protected_values_in_url_paths_query_keys_and_json_keys_are_removed(encoded: str) -> None:
    redactor = protected_shapes()
    assert encoded not in redactor.url(ORIGIN + "/data/" + encoded, ORIGIN)
    assert encoded not in redactor.url(ORIGIN + "/data?" + encoded + "=ordinary", ORIGIN)
    assert encoded not in redactor.json_text({encoded: "ordinary"}, 1000)
    assert encoded not in (redactor.text(encoded) or "")
    for rendered in (
        redactor.url(ORIGIN + "/data/" + encoded, ORIGIN),
        redactor.json_text({encoded: "ordinary"}, 1000),
    ):
        assert "opaqueprivatecookie" not in rendered
        assert "<secret>" in rendered


@pytest.mark.parametrize("value", ["4321", "%34%33%32%31", "2026-10-29", "1800000000"])
def test_protected_query_values_are_checked_before_public_number_or_date_fast_paths(
    value: str,
) -> None:
    shown = protected_shapes().url(ORIGIN + "/data?neutral=" + value, ORIGIN)
    assert value not in shown
    assert "<secret>" in shown


def test_protected_html_date_and_relative_href_are_checked_before_truncation() -> None:
    shown = protected_shapes().html(
        '<main><time datetime="2026-10-29">Дата</time>'
        '<a href="relative/opaqueprivatecookie">Ссылка</a></main>'
    )
    assert "2026-10-29" not in shown
    assert "opaqueprivatecookie" not in shown


def test_plain_and_encoded_credentials_in_one_value_are_both_removed() -> None:
    redactor = protected_shapes()
    value = "opaqueprivatecookie-%34%33%32%31"
    assert redactor.text(value) == "<secret>"
    assert redactor.url(ORIGIN + "/data?neutral=" + value, ORIGIN) == "/data?neutral=<secret>"


def test_session_redaction_budget_counts_utf8_bytes() -> None:
    state = session("small")
    state.browser_storage[0].local_storage = {"neutral": SecretStr("я" * 524_289)}
    with pytest.raises(BotaiError) as error:
        Redactor().protect_session(state)
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE


def markers(generation: str) -> tuple[str, ...]:
    return tuple(f"opaque-{kind}-{generation}" for kind in ("cookie", "bearer", "local", "session"))


def session(generation: str) -> SessionState:
    cookie, bearer, local, transient = markers(generation)
    return SessionState(
        origin=ORIGIN,
        captured_at=NOW,
        cookies=[SessionCookie(name="sid", domain="fakeuni.test", value=SecretStr(cookie))],
        bearer=SecretStr(bearer),
        browser_storage=[
            SessionOriginStorage(
                origin=ORIGIN,
                local_storage={"neutral": SecretStr(json.dumps({"neutral": local}))},
                session_storage={"neutral": SecretStr(transient)},
            )
        ],
    )


class ReflectingHandle(FakeBrowserHandle):
    def __init__(self) -> None:
        pages = {}
        for path, generation in (("/", "first"), ("/next", "refreshed")):
            values = markers(generation)
            pages[ORIGIN + path] = FakePage(
                "<main><article>"
                + " ".join(values)
                + '</article><a href="/next">Дальше</a></main>',
                requests=[
                    ObservedRequest(
                        method="GET",
                        url=ORIGIN + "/api/neutral",
                        status=200,
                        resource_type="xhr",
                        response_content_type="application/json",
                        response_sample=json.dumps({"items": [{"description": " ".join(values)}]}),
                    )
                ],
            )
        super().__init__(pages, NOW, ORIGIN, read_only=True)

    async def export_session(self) -> SessionState:
        return session("refreshed" if self._url == ORIGIN + "/next" else "first")


async def test_reflected_cookie_bearer_and_structured_storage_are_removed_before_both_model_steps() -> (
    None
):
    llm = ScriptedLlm(discovery_queue=[plan(1, "navigate", "l0"), plan(2, "finish")])
    obs = await adaptive_observe(
        ReflectingHandle(),
        ORIGIN,
        Redactor(),
        start_urls=[ORIGIN + "/"],
        intent=LearningIntent(),
        llm=llm,
    )
    planner = "\n".join(
        message.content for request in llm.observation_requests for message in request.messages
    )
    author = obs.render()
    for value in (*markers("first"), *markers("refreshed")):
        assert value not in planner
        assert value not in author
    assert "<secret>" in planner


@pytest.mark.parametrize("slow", [False, True])
async def test_failed_or_timed_out_secret_capture_stops_before_any_model_call(
    slow: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(ReflectingHandle):
        async def export_session(self) -> SessionState:
            if slow:
                await asyncio.sleep(10)
            raise RuntimeError("opaque-cookie-first")

    monkeypatch.setattr("botai.learn.observe.SESSION_CAPTURE_SECONDS", 0.01)
    llm = ScriptedLlm()
    with pytest.raises(BotaiError) as error:
        await adaptive_observe(
            Broken(),
            ORIGIN,
            Redactor(),
            start_urls=[ORIGIN + "/"],
            intent=LearningIntent(),
            llm=llm,
        )
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "opaque-cookie-first" not in str(error.value)
    assert llm.observation_requests == llm.requests == []


async def test_reauthentication_protects_the_new_session_before_recipe_authoring(
    make_env: EnvFactory,
) -> None:
    class Reauthenticated(FakeSessions):
        async def ensure(self, ctx, *, user_id, connection_id, force=False):
            valid = await super().ensure(
                ctx, user_id=user_id, connection_id=connection_id, force=force
            )
            protected = session("reauth" if force else "first")
            protected.cookies.extend(valid.cookies)
            assert self.vault is not None
            await self.vault.put(
                user_id, connection_id, SecretKind.SESSION, encode_session(protected)
            )
            return protected

    class Gateway(FakeBrowserGateway):
        async def open(self, **kwargs):
            generation = "reauth" if self.handles else "first"
            self._pages = {
                ORIGIN + "/": FakePage(
                    "<main><h2>Вы не вошли в аккаунт</h2></main>"
                    if generation == "first"
                    else "<main><article>" + " ".join(markers("reauth")) + "</article></main>"
                )
            }
            return await super().open(**kwargs)

    env = await make_env([ls.timetable_answer()], sessions=Reauthenticated())
    env.services.browser = Gateway({}, NOW, ORIGIN)
    result = await env.run(payload={"kinds": ["scheduleEvents"]})
    assert result["status"] == "ok"
    sent = "\n".join(
        message.content for request in env.llm.requests for message in request.messages
    )
    for value in markers("reauth"):
        assert value not in sent
    assert env.sessions.forced == [False, True]
