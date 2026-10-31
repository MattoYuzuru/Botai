"""Owner policy: defaults, action-class inference and scope precedence (D-006, D-025)."""

from __future__ import annotations

import pytest

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest
from botai.actions.policy import DEFAULT_MODES, infer_action_class, resolve_policy
from botai.core.errors import BotaiError, ErrorCode
from tests.actions.conftest import ORIGIN, RUN_ID, WRITE_URL
from tests.actions.support import allow, write_request
from tests.agent.conftest import Api
from tests.agent.seed import add_space

pytestmark = pytest.mark.db

FAKE_SOURCE = "01M68Z000000000000000000S1"


def test_defaults_are_documented() -> None:
    assert DEFAULT_MODES == {
        "read": "auto",
        "write_soft": "ask",
        "write_destructive": "ask",
        "money": "ask",
        "auth": "ask",
    }


def test_class_inference_never_lowers_the_request() -> None:
    assert (
        infer_action_class(method="POST", url=WRITE_URL, intent="Подать заявку на справку")
        == "write_soft"
    )
    assert (
        infer_action_class(method="POST", url=f"{ORIGIN}/account/delete", intent="удалить курс")
        == "write_destructive"
    )
    assert (
        infer_action_class(method="DELETE", url=WRITE_URL, intent="Удалить заявку")
        == "write_destructive"
    )
    assert (
        infer_action_class(method="POST", url=WRITE_URL, intent="Оплатить взнос за общежитие")
        == "money"
    )
    assert (
        infer_action_class(method="POST", url=WRITE_URL, intent="Сменить пароль в кабинете")
        == "auth"
    )
    # The model may name a class, but not lower what the request implies.
    assert (
        infer_action_class(method="DELETE", url=WRITE_URL, intent="удалить", declared="read")
        == "write_destructive"
    )
    # Raising is allowed: a request the model knows spends money is money even without markers.
    assert (
        infer_action_class(method="POST", url=WRITE_URL, intent="Оформить заявку", declared="money")
        == "money"
    )


def test_browser_steps_are_write_soft_unless_they_are_destructive_or_money() -> None:
    assert infer_action_class(transport="browser", intent="Нажать «Отправить»") == "write_soft"
    assert infer_action_class(transport="browser", intent="Оплатить заказ") == "money"


async def test_no_rows_mean_documented_defaults(action_engine: ActionEngine, api: Api) -> None:
    effective = await action_engine.effective(user_id=api.user_id, url=WRITE_URL)
    modes = {item.action_class: item.mode for item in effective}
    assert modes == DEFAULT_MODES


async def test_most_specific_scope_wins(action_engine: ActionEngine, api: Api) -> None:
    user = api.user_id
    space = await add_space(api.sm, user)

    async def resolved():  # type: ignore[no-untyped-def]
        async with api.sm() as session:
            return await resolve_policy(
                session,
                user_id=user,
                action_class="write_soft",
                url=WRITE_URL,
                source_id=FAKE_SOURCE,
                space_id=space,
            )

    assert (await resolved()).mode == "ask"  # no rows: the default

    await allow(action_engine, user, mode="ask")
    assert (await resolved()).mode == "ask"

    await action_engine.upsert_policy(
        user_id=user,
        scope_kind="site",
        scope_id=ORIGIN,
        action_class="write_soft",
        mode="auto",
    )
    site = await resolved()
    assert (site.mode, site.scope_kind, site.scope_id) == ("auto", "site", "fakeuni.test")

    await action_engine.upsert_policy(
        user_id=user,
        scope_kind="space",
        scope_id=space,
        action_class="write_soft",
        mode="never",
    )
    space_row = await resolved()
    assert (space_row.mode, space_row.scope_kind) == ("never", "space")

    await action_engine.upsert_policy(
        user_id=user,
        scope_kind="source",
        scope_id=FAKE_SOURCE,
        action_class="write_soft",
        mode="auto",
    )
    source = await resolved()
    assert (source.mode, source.scope_kind) == ("auto", "source")

    # A row for another class or another site does not leak into this one.
    await action_engine.upsert_policy(
        user_id=user,
        scope_kind="site",
        scope_id="other.test",
        action_class="write_soft",
        mode="never",
    )
    await action_engine.upsert_policy(
        user_id=user,
        scope_kind="source",
        scope_id=FAKE_SOURCE,
        action_class="money",
        mode="never",
    )
    assert (await resolved()).mode == "auto"


async def test_upsert_validates_and_updates_in_place(action_engine: ActionEngine, api: Api) -> None:
    user = api.user_id
    row = await action_engine.upsert_policy(
        user_id=user,
        scope_kind="site",
        scope_id="https://FakeUni.Test/portal",
        action_class="write_soft",
        mode="ask",
    )
    assert (row.scope_kind, row.scope_id) == ("site", "fakeuni.test")
    updated = await action_engine.upsert_policy(
        user_id=user,
        scope_kind="site",
        scope_id="fakeuni.test",
        action_class="write_soft",
        mode="auto",
        limits={"max_per_day": 3},
    )
    assert updated.id == row.id
    assert (updated.mode, updated.limits) == ("auto", {"max_per_day": 3})
    assert len(await action_engine.list_policies(user)) == 1

    with pytest.raises(BotaiError) as global_scope:
        await action_engine.upsert_policy(
            user_id=user,
            scope_kind="global",
            scope_id="fakeuni.test",
            action_class="write_soft",
            mode="auto",
        )
    assert global_scope.value.code == ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as bad_limits:
        await action_engine.upsert_policy(
            user_id=user,
            scope_kind="global",
            scope_id=None,
            action_class="write_soft",
            mode="auto",
            limits={"max_per_day": 0},
        )
    assert bad_limits.value.code == ErrorCode.VALIDATION


async def test_owner_limits_escalate_auto_to_ask(action_engine: ActionEngine, api: Api) -> None:
    user = api.user_id
    await allow(action_engine, user, limits={"max_per_day": 1})
    first = await action_engine.run(user_id=user, request=write_request(), run_id=RUN_ID)
    assert first.status == "executed"

    prepared = await action_engine.prepare(
        user_id=user,
        request=write_request(intent="Вторая заявка на справку"),
        run_id="01M68Z000000000000000000A2",
    )
    assert prepared.policy.mode == "ask"
    assert prepared.policy.escalated is not None
    assert "max_per_day" in prepared.policy.escalated
    assert prepared.entry.decision == "asked"


def test_request_hash_is_stable_and_body_sensitive() -> None:
    same = write_request()
    reordered = ActionRequest.model_validate(
        {
            "transport": "http",
            "intent": "Подать заявку на справку об обучении",
            "method": "POST",
            "url": WRITE_URL,
            "body": {"student_id": "S-12345", "language": "ru"},
            "body_kind": "form",
        }
    )
    assert same.canonical_hash() == reordered.canonical_hash()
    assert same.body_sha256() == reordered.body_sha256()
    changed = write_request(body={"language": "en", "student_id": "S-12345"})
    assert changed.body_sha256() != same.body_sha256()
