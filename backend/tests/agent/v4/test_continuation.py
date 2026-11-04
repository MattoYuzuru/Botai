"""The decision of a chat chain (L0, D-040): order of the conditions, delays and the Russian texts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from botai.agent.v4.continuation import (
    DETAIL_MAX,
    STAGE_MAX,
    ChainState,
    Continue,
    QuotaView,
    Stop,
    continue_detail,
    continue_stage,
    decide_continuation,
    identical_calls_in_a_row,
    no_progress,
    stop_text,
)
from botai.agent.v4.runtime import RunResult
from botai.agent.v4.traces import RunStatus, RunTrace, TraceEvent
from tests.agent.v4.runtime_support import make_settings

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def result(
    status: RunStatus = "step_limit",
    *,
    steps: int = 10,
    error_code: str | None = None,
    extra: dict[str, Any] | None = None,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> RunResult:
    events = tuple(
        TraceEvent(
            run_id="r",
            seq=index,
            at=T0,
            kind="tool_call",
            payload={"tool": tool, "arguments": arguments, "call_id": f"c{index}"},
        )
        for index, (tool, arguments) in enumerate(
            calls if calls is not None else [("echo", {"n": n}) for n in range(steps)]
        )
    )
    return RunResult(
        run_id="r",
        status=status,
        steps=steps,
        error_code=error_code,
        error_extra=extra or {},
        trace=RunTrace(run_id="r", status=status, events=events),
    )


def chain(
    *,
    spent: float = 0.5,
    continuations: int = 0,
    elapsed_s: float = 60,
    streak: int = 0,
) -> ChainState:
    return ChainState(
        segment=continuations,
        continuations=continuations,
        spent_usd=spent,
        started_at=T0,
        now=T0 + timedelta(seconds=elapsed_s),
        transient_streak=streak,
    )


def decide(
    res: RunResult, state: ChainState | None = None, quota: QuotaView | None = None, **s: Any
):
    return decide_continuation(res, state or chain(), quota, make_settings(**s))


# --- the table, in the order of the conditions ----------------------------------------------------


def test_an_answer_ends_the_turn_even_when_everything_else_says_stop() -> None:
    state = chain(spent=99, continuations=99, elapsed_s=10**6)
    assert decide(result("answer"), state) == Stop("answer")


def test_a_cancelled_run_stops_before_the_money_check() -> None:
    assert decide(result("cancelled"), chain(spent=99)) == Stop("cancelled")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("step_limit", "steps"),
        ("time_limit", "seconds"),
        ("token_limit", "tokens"),
        ("cost_limit", "usd"),
    ],
)
def test_a_soft_limit_continues_at_once(status: RunStatus, reason: str) -> None:
    assert decide(result(status)) == Continue(0.0, reason)  # type: ignore[arg-type]


def test_chain_money_is_a_mandatory_stop() -> None:
    assert decide(result(), chain(spent=2.0)) == Stop("money")
    assert decide(result(), chain(spent=1.99)) == Continue(0.0, "steps")
    assert decide(result(), chain(spent=1.0), agent_chain_hard_stop_usd=1.0) == Stop("money")
    # money wins over a pending retry and over the count
    down = result("error", error_code="llm_unavailable", steps=0)
    assert decide(down, chain(spent=2.5, streak=1)) == Stop("money")
    assert decide(result(), chain(spent=2.5, continuations=99)) == Stop("money")


def test_the_number_of_automatic_continuations_is_bounded() -> None:
    assert decide(result(), chain(continuations=4)) == Continue(0.0, "steps")
    assert decide(result(), chain(continuations=5)) == Stop("count")
    assert decide(result(), chain(continuations=1), agent_continuations_max=1) == Stop("count")


def test_zero_continuations_is_the_manual_mode_for_everything() -> None:
    assert decide(result(), agent_continuations_max=0) == Stop("manual")
    assert decide(
        result("error", error_code="llm_unavailable", steps=0),
        chain(streak=1),
        agent_continuations_max=0,
    ) == Stop("manual")
    assert decide(result("hard_stop"), agent_continuations_max=0) == Stop("manual")


def test_the_chain_has_a_wall_time() -> None:
    assert decide(result(), chain(elapsed_s=3599)) == Continue(0.0, "steps")
    assert decide(result(), chain(elapsed_s=3600)) == Stop("time")
    assert decide(result(), chain(elapsed_s=61), agent_chain_max_seconds=60) == Stop("time")


def test_a_day_quota_nearly_used_stops_before_another_segment() -> None:
    nearly = QuotaView(tokens_today=899, tokens_limit=1000, pool_limit_usd=20.0)
    used = QuotaView(tokens_today=900, tokens_limit=1000, pool_limit_usd=20.0)
    assert decide(result(), quota=nearly) == Continue(0.0, "steps")
    assert decide(result(), quota=used) == Stop("quota")
    pool = QuotaView(tokens_today=0, tokens_limit=1000, pool_spent_usd=18.0, pool_limit_usd=20.0)
    assert decide(result(), quota=pool) == Stop("quota")
    unlimited = QuotaView(tokens_today=10**9, tokens_limit=0, pool_limit_usd=0.0)
    assert decide(result(), quota=unlimited) == Continue(0.0, "steps")


def test_a_quota_error_waits_only_when_it_is_a_minute_window() -> None:
    soon = (T0 + timedelta(seconds=75, minutes=0)).isoformat().replace("+00:00", "Z")
    state = chain(elapsed_s=0)
    minute = result(
        "error", steps=0, error_code="quota_exceeded", extra={"scope": "minute", "resetsAt": soon}
    )
    assert decide(minute, state) == Continue(60.0, "quota_minute")  # capped at a minute
    near = (T0 + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    short = result(
        "error", steps=0, error_code="quota_exceeded", extra={"scope": "minute", "resetsAt": near}
    )
    assert decide(short, state) == Continue(20.0, "quota_minute")
    nodate = result("error", steps=0, error_code="quota_exceeded", extra={"scope": "minute"})
    assert decide(nodate, state) == Continue(60.0, "quota_minute")
    for scope in ("daily", "pool", None):
        extra = {"scope": scope} if scope else {}
        daily = result("error", steps=0, error_code="quota_exceeded", extra=extra)
        assert decide(daily, state) == Stop("quota")


@pytest.mark.parametrize(("streak", "delay"), [(1, 60.0), (2, 180.0), (3, 600.0)])
def test_a_provider_outage_waits_longer_each_time_up_to_three_times(
    streak: int, delay: float
) -> None:
    down = result("error", steps=0, error_code="llm_unavailable")
    assert decide(down, chain(streak=streak, continuations=streak - 1)) == Continue(
        delay, "transient"
    )


def test_the_fourth_outage_in_a_row_stops() -> None:
    down = result("error", steps=0, error_code="llm_unavailable")
    assert decide(down, chain(streak=4, continuations=3)) == Stop("transient")
    # an outage with a high count still obeys the count
    assert decide(down, chain(streak=1, continuations=5)) == Stop("count")


def test_other_errors_and_a_pause_stop() -> None:
    for code in ("llm_error", "internal_error", "validation"):
        assert decide(result("error", error_code=code)) == Stop("error")
    assert decide(result("paused")) == Stop("paused")


def test_no_progress_stops_a_limit_segment() -> None:
    assert decide(result(steps=2)) == Stop("no_progress")
    same = [("echo", {"url": "x"})] * 3
    assert decide(result(calls=same, steps=9)) == Stop("no_progress")
    mixed = [("echo", {"url": "x"}), ("echo", {"url": "y"}), ("echo", {"url": "x"})] * 2
    assert decide(result(calls=mixed, steps=9)) == Continue(0.0, "steps")


def test_hard_stop_stops_and_is_not_a_continuation() -> None:
    assert decide(result("hard_stop", steps=10)) == Stop("hard_stop")


def test_identical_calls_compare_tool_and_arguments_in_order() -> None:
    calls = [("a", {"x": 1}), ("a", {"x": 1}), ("a", {"x": 2}), ("a", {"x": 2}), ("a", {"x": 2})]
    res = result(calls=calls, steps=9)
    assert identical_calls_in_a_row(res) == 3
    assert no_progress(res)
    keys_in_other_order = [
        ("a", {"x": 1, "y": 2}),
        ("a", {"y": 2, "x": 1}),
        ("a", {"x": 1, "y": 2}),
    ]
    assert identical_calls_in_a_row(result(calls=keys_in_other_order, steps=9)) == 3


# --- texts -------------------------------------------------------------------------------------


def test_the_stage_of_an_automatic_continuation_names_the_count_and_the_limit() -> None:
    stage = continue_stage(Continue(0.0, "tokens"), number=2, total=5, transient_streak=0)
    assert stage == "Продолжаю автоматически (2 из 5): достигнут лимит токенов."
    detail = continue_detail(Continue(0.0, "tokens"), spent_usd=0.4219, cap_usd=2.0)
    assert detail == "Потрачено $0.42 из $2.00. Прогресс сохранён."


def test_the_stage_of_a_pause_says_how_long_and_which_attempt() -> None:
    assert continue_stage(Continue(180.0, "transient"), number=2, total=5, transient_streak=2) == (
        "Модель временно недоступна. Попробую снова через 3 мин (попытка 2 из 3)."
    )
    assert continue_stage(
        Continue(45.0, "quota_minute"), number=1, total=5, transient_streak=0
    ) == ("Слишком много запросов к модели. Продолжу через 45 с.")


@pytest.mark.parametrize(
    "reason", ["steps", "seconds", "tokens", "usd", "transient", "quota_minute"]
)
def test_stage_and_detail_fit_the_block_limits(reason: str) -> None:
    decision = Continue(600.0, reason)  # type: ignore[arg-type]
    assert len(continue_stage(decision, number=50, total=50, transient_streak=3)) <= STAGE_MAX
    assert len(continue_detail(decision, spent_usd=1234.5678, cap_usd=1000)) <= DETAIL_MAX


def test_stop_texts_explain_the_reason_and_what_is_saved() -> None:
    money = stop_text("money", spent_usd=2.0, cap_usd=2.0)
    assert money == (
        "Остановился: потрачено $2.00 из $2.00 на эту задачу. "
        "Прогресс сохранён — нажмите «Продолжить», чтобы идти дальше."
    )
    count = stop_text("count", continuations=5)
    assert count.startswith("Остановился после 5 автоматических продолжений.")
    one = stop_text("count", continuations=1)
    assert one.startswith("Остановился после 1 автоматического продолжения.")
    assert stop_text("count", continuations=21).startswith("Остановился после 21 автоматического")
    assert stop_text("count", continuations=11).startswith("Остановился после 11 автоматических")
    assert "«Продолжить»" in count
    assert "последние шаги ничего не дали" in stop_text("no_progress")
    assert "в полночь по Москве" in stop_text("quota")
    assert "после 3 попыток" in stop_text("transient")
    assert stop_text("cancelled") == "Остановлено. Прогресс сохранён — можно продолжить."
    assert "30 мин" in stop_text("time", elapsed_s=1800)


def test_when_nothing_could_be_saved_the_text_says_so_and_offers_no_button() -> None:
    for reason in ("money", "count", "time", "quota", "transient", "no_progress", "cancelled"):
        text = stop_text(reason, saved=False, spent_usd=2.0, cap_usd=2.0)  # type: ignore[arg-type]
        assert "не удалось" in text
        assert "нажмите «Продолжить»" not in text
