"""Continuity of a chat turn: when a finished segment continues by itself and when it stops (D-040).

A segment is one `AgentRuntime.run` that ended on a soft limit or a transient provider error. The
runtime keeps stopping on a limit (D-009); this module is the only place that decides about the
chain, as a pure function of the segment's result, the state of the chain (money from `llm_usage`,
counters from the checkpoints, the clock), the quota snapshot and the settings. Nothing here
touches the database, the model or the clock, so the decision table is a plain unit test.

Order of the conditions (the first that holds wins):

1. an answer ends the turn;
2. a cancelled run stops;
3. the money of the whole chain reached `agent_chain_hard_stop_usd` (mandatory, always checked);
4. the number of automatic continuations reached `agent_continuations_max` (0 = manual mode);
5. the chain has been going longer than `agent_chain_max_seconds`;
6. the day's quota is used for 90 % or more, or the quota error is not a per-minute one;
7. a per-minute quota error waits up to a minute;
8. `llm_unavailable` waits 60/180/600 seconds, at most 3 times in a row;
9. every other error and a pause stop;
10. no progress (fewer than 3 steps, except after a time/token/cost limit where a few heavy steps
    are healthy, or 3 identical tool calls in a row) stops;
11. `hard_stop` stops;
12. a soft limit (steps, time, tokens, cost) continues at once.

The texts are Russian, what the student reads in the chat: a stage line of at most 120 characters
and a detail of at most 2000.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from botai.agent.v4.runtime import RunResult
from botai.settings import Settings

STAGE_MAX = 120
DETAIL_MAX = 2000
QUOTA_STOP_SHARE = 0.9
TRANSIENT_DELAYS_S: tuple[float, ...] = (60.0, 180.0, 600.0)
MINUTE_QUOTA_MAX_DELAY_S = 60.0
NO_PROGRESS_STEPS = 3
IDENTICAL_CALLS_IN_A_ROW = 3
# A segment that ended on one of these may be short in steps and still healthy (heavy steps).
HEAVY_STEP_LIMITS: frozenset[str] = frozenset({"cost_limit", "token_limit", "time_limit"})

SOFT_LIMIT_STATUSES: frozenset[str] = frozenset(
    {"step_limit", "time_limit", "token_limit", "cost_limit"}
)

StopReason = Literal[
    "answer",
    "cancelled",
    "money",
    "count",
    "manual",
    "time",
    "quota",
    "transient",
    "no_progress",
    "hard_stop",
    "paused",
    "error",
]
ContinueReason = Literal["steps", "seconds", "tokens", "usd", "transient", "quota_minute"]

LIMIT_REASONS: dict[str, ContinueReason] = {
    "step_limit": "steps",
    "time_limit": "seconds",
    "token_limit": "tokens",
    "cost_limit": "usd",
}
LIMIT_TEXTS: dict[str, str] = {
    "steps": "достигнут лимит шагов",
    "seconds": "достигнут лимит времени",
    "tokens": "достигнут лимит токенов",
    "usd": "достигнут лимит стоимости",
}


@dataclass(frozen=True, slots=True)
class QuotaView:
    """What is left of the day: the user's tokens and the pool's dollars (0 limit = no cap)."""

    tokens_today: int = 0
    tokens_limit: int = 0
    pool_spent_usd: float = 0.0
    pool_limit_usd: float = 0.0
    resets_at: datetime | None = None

    @property
    def share(self) -> float:
        shares = []
        if self.tokens_limit > 0:
            shares.append(self.tokens_today / self.tokens_limit)
        if self.pool_limit_usd > 0:
            shares.append(self.pool_spent_usd / self.pool_limit_usd)
        return max(shares, default=0.0)


@dataclass(frozen=True, slots=True)
class ChainState:
    """The chain a segment belongs to: where it started, what it spent, how often it went on."""

    segment: int  # 0-based index of the segment that just ended
    continuations: int  # automatic continuations made before the next segment
    spent_usd: float  # all segments, from `llm_usage` by job (survives a crashed worker)
    started_at: datetime
    now: datetime
    transient_streak: int = 0  # consecutive transient failures, the segment that ended included

    @property
    def elapsed_s(self) -> float:
        return max(0.0, (self.now - self.started_at).total_seconds())


@dataclass(frozen=True, slots=True)
class Continue:
    delay_s: float
    reason: ContinueReason


@dataclass(frozen=True, slots=True)
class Stop:
    reason: StopReason


Decision = Continue | Stop


def decide_continuation(
    result: RunResult,
    chain: ChainState,
    quota: QuotaView | None,
    settings: Settings,
) -> Decision:
    """Continue the chain as another segment, or stop it (see the module docstring for the order)."""
    if result.status == "answer":
        return Stop("answer")
    if result.status == "cancelled":
        return Stop("cancelled")
    if chain.spent_usd >= settings.agent_chain_hard_stop_usd:
        return Stop("money")
    if chain.continuations >= settings.agent_continuations_max:
        return Stop("manual" if settings.agent_continuations_max == 0 else "count")
    if chain.elapsed_s >= settings.agent_chain_max_seconds:
        return Stop("time")
    if quota is not None and quota.share >= QUOTA_STOP_SHARE:
        return Stop("quota")
    if result.status == "error" and result.error_code == "quota_exceeded":
        if result.error_extra.get("scope") != "minute":
            return Stop("quota")
        return Continue(_minute_delay(result, chain.now), "quota_minute")
    if result.status == "error" and result.error_code == "llm_unavailable":
        if chain.transient_streak > len(TRANSIENT_DELAYS_S):
            return Stop("transient")
        return Continue(TRANSIENT_DELAYS_S[max(chain.transient_streak, 1) - 1], "transient")
    if result.status == "error":
        return Stop("error")
    if result.status == "paused":
        return Stop("paused")
    if no_progress(result):
        return Stop("no_progress")
    if result.status == "hard_stop":
        return Stop("hard_stop")
    reason = LIMIT_REASONS.get(result.status)
    return Continue(0.0, reason) if reason is not None else Stop("error")


def no_progress(result: RunResult) -> bool:
    """A segment that did next to nothing, or only repeated one call, is not worth another one."""
    if result.steps < NO_PROGRESS_STEPS and result.status not in HEAVY_STEP_LIMITS:
        return True
    return identical_calls_in_a_row(result) >= IDENTICAL_CALLS_IN_A_ROW


def identical_calls_in_a_row(result: RunResult) -> int:
    """The longest run of the same tool with the same arguments, in the order they were called."""
    longest = 0
    current = 0
    previous: str | None = None
    for event in result.trace.events:
        if event.kind != "tool_call":
            continue
        signature = json.dumps(
            [event.payload.get("tool"), event.payload.get("arguments")],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        current = current + 1 if signature == previous else 1
        previous = signature
        longest = max(longest, current)
    return longest


def _minute_delay(result: RunResult, now: datetime) -> float:
    raw = result.error_extra.get("resetsAt")
    if isinstance(raw, str):
        try:
            resets = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return MINUTE_QUOTA_MAX_DELAY_S
        if resets.tzinfo is not None:
            return min(MINUTE_QUOTA_MAX_DELAY_S, max(1.0, (resets - now).total_seconds()))
    return MINUTE_QUOTA_MAX_DELAY_S


# --- texts the student reads ---------------------------------------------------------------------


def _usd(value: float) -> str:
    return f"${value:.2f}"


def _wait(seconds: float) -> str:
    if seconds < 90:
        return f"{max(1, math.ceil(seconds))} с"
    return f"{max(1, round(seconds / 60))} мин"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def continue_stage(decision: Continue, *, number: int, total: int, transient_streak: int) -> str:
    """The stage line of a pause or an automatic continuation (at most 120 characters)."""
    if decision.reason == "transient":
        text = (
            f"Модель временно недоступна. Попробую снова через {_wait(decision.delay_s)} "
            f"(попытка {max(transient_streak, 1)} из {len(TRANSIENT_DELAYS_S)})."
        )
    elif decision.reason == "quota_minute":
        text = f"Слишком много запросов к модели. Продолжу через {_wait(decision.delay_s)}."
    else:
        text = f"Продолжаю автоматически ({number} из {total}): {LIMIT_TEXTS[decision.reason]}."
    return _clip(text, STAGE_MAX)


def continue_detail(decision: Continue, *, spent_usd: float, cap_usd: float) -> str:
    """The detail under the stage: what the chain has spent so far (honest numbers only)."""
    return _clip(f"Потрачено {_usd(spent_usd)} из {_usd(cap_usd)}. Прогресс сохранён.", DETAIL_MAX)


def stop_text(
    reason: StopReason,
    *,
    spent_usd: float = 0.0,
    cap_usd: float = 0.0,
    continuations: int = 0,
    elapsed_s: float = 0.0,
    saved: bool = True,
) -> str:
    """The sentence that explains why the agent stopped; `saved=False` says progress is lost."""
    tail = (
        "Прогресс сохранён — нажмите «Продолжить», чтобы идти дальше."
        if saved
        else "Прогресс сохранить не удалось: придётся начать с вашего последнего вопроса."
    )
    if reason == "money":
        text = f"Остановился: потрачено {_usd(spent_usd)} из {_usd(cap_usd)} на эту задачу. {tail}"
    elif reason == "count":
        noun = (
            "автоматического продолжения"
            if continuations % 10 == 1 and continuations % 100 != 11
            else "автоматических продолжений"
        )
        text = f"Остановился после {continuations} {noun}. {tail}"
    elif reason == "time":
        text = f"Остановился: задача идёт уже {_wait(elapsed_s)}. {tail}"
    elif reason == "quota":
        text = (
            "Дневной лимит модели исчерпан или почти исчерпан. Он обновится в полночь по Москве; "
            + ("прогресс сохранён, продолжу по кнопке «Продолжить»." if saved else tail)
        )
    elif reason == "transient":
        text = f"Модель по-прежнему недоступна после {len(TRANSIENT_DELAYS_S)} попыток. " + (
            "Прогресс сохранён — нажмите «Продолжить», когда она вернётся." if saved else tail
        )
    elif reason == "no_progress":
        text = f"Остановился: последние шаги ничего не дали. {tail}"
    elif reason == "cancelled":
        text = "Остановлено. " + (
            "Прогресс сохранён — можно продолжить." if saved else "Прогресс сохранить не удалось."
        )
    elif reason == "hard_stop":
        text = f"Остановился: достигнут аварийный предел стоимости. {tail}"
    elif reason == "paused":
        text = f"Работа приостановлена. {tail}"
    elif reason == "manual":
        text = tail
    else:
        text = f"Работа прервана. {tail}"
    return _clip(text, DETAIL_MAX)


__all__ = [
    "DETAIL_MAX",
    "HEAVY_STEP_LIMITS",
    "IDENTICAL_CALLS_IN_A_ROW",
    "LIMIT_REASONS",
    "LIMIT_TEXTS",
    "MINUTE_QUOTA_MAX_DELAY_S",
    "NO_PROGRESS_STEPS",
    "QUOTA_STOP_SHARE",
    "SOFT_LIMIT_STATUSES",
    "STAGE_MAX",
    "TRANSIENT_DELAYS_S",
    "ChainState",
    "Continue",
    "ContinueReason",
    "Decision",
    "QuotaView",
    "Stop",
    "StopReason",
    "continue_detail",
    "continue_stage",
    "decide_continuation",
    "identical_calls_in_a_row",
    "no_progress",
    "stop_text",
]
