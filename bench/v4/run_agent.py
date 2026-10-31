"""CLI of the v4 agent acceptance bench: a real L3 run and a deterministic L2 run.

Both scenarios build the product wiring exactly as the `AGENT` job handler does
(`build_agent_wiring`: provider by settings, `LocalExecutor`, full browser, secret broker, tools)
and run the product `AgentRuntime`. Differences from production are only the owner channel
(Keychain instead of the PWA) and the in-memory trace store (no Postgres).

    # prerequisites only, nothing is opened, no money is spent
    backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse --dry-run

    # real L3: Keychain credentials + DeepSeek + Chromium on the live HSE cabinet
    BOTAI_LIVE_LLM=1 backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse

    # deterministic L2: mock provider, loopback fakeuni, $0
    backend/.venv/bin/python -m bench.v4.run_agent --scenario l2-fakeuni

The live scenario is money-bounded (`--max-usd`, `--max-seconds`, `--max-steps`, plus the
owner's `BOTAI_LIVE_LLM_BUDGET_USD` ceiling). It refuses to start without explicit
`BOTAI_LIVE_LLM=1`, like the v3 benchmark. Evidence: sanitized JSON + README under
`docs/v4/_evidence/2026-10-31-v4-agent/`; raw artifacts under
`~/.local/state/botai-v4-bench/` (0700). Secret values are never printed or written.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bench.v4 import __version__, support  # noqa: E402

LIVE_TASK = (
    "Открой личный кабинет ВШЭ (https://lk.hse.ru/), войди в него. "
    "Найди расписание занятий или страницу/API-эндпоинт, которым оно грузится. "
    "Сохрани найденные данные расписания в файл в workspace задачи (JSON или таблицу). "
    "Ответь кратко: что нашёл, в каком файле лежит и какой источник (URL или файл). "
    "Если войти не удалось — точно скажи, на каком шаге и почему."
)

SCENARIOS = {
    "live-hse": {
        "title": "ВШЭ: живой вход по кредам из Keychain и расписание агентом v4 (L3)",
        "live": True,
    },
    "l2-fakeuni": {
        "title": "fakeuni: connect-подобный сценарий инструментами v4, mock-модель (L2, $0)",
        "live": False,
    },
}


# --- CLI -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench.v4.run_agent",
        description=(
            "Приёмка настоящего v4-harness (build_agent_wiring): живой L3 на ВШЭ и "
            "детерминированный L2 на fakeuni. Секреты — только из Keychain, значения не печатаются."
        ),
    )
    parser.add_argument("--scenario", default="live-hse", help="live-hse | l2-fakeuni")
    parser.add_argument("--list", action="store_true", help="список сценариев")
    parser.add_argument(
        "--dry-run", action="store_true", help="проверить предпосылки, ничего не открывать"
    )
    parser.add_argument(
        "--headed", action="store_true", help="показать окно Chromium (диагностика)"
    )
    parser.add_argument("--date", default=None, help="дата каталога запуска (по умолчанию сегодня)")
    parser.add_argument("--max-steps", type=int, default=28, help="жёсткий cap шагов модели")
    parser.add_argument("--max-seconds", type=float, default=540.0, help="мягкий лимит времени run")
    parser.add_argument(
        "--max-usd", type=float, default=0.50, help="потолок стоимости одного прогона"
    )
    parser.add_argument(
        "--inject-task-message",
        action="store_true",
        help=(
            "WORKAROUND, не product-path: положить задачу явным user-сообщением в RunTask.messages "
            "(обходит баг «task не попадает в диалог», runtime.py); в evidence помечается как workaround"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        for scenario, meta in SCENARIOS.items():
            print(f"{scenario:<14} {meta['title']}")
        return 0
    if args.scenario not in SCENARIOS:
        print(f"unknown scenario: {args.scenario}", file=sys.stderr)
        return 2
    support.bootstrap_imports()
    support.ensure_backend_python()
    if SCENARIOS[args.scenario]["live"]:
        return run_live(args)
    return run_l2(args)


# --- shared helpers -------------------------------------------------------------------------------


def _budget_usd(args: argparse.Namespace) -> float:
    cap = float(args.max_usd)
    env_cap = os.environ.get("BOTAI_LIVE_LLM_BUDGET_USD")
    if env_cap:
        with contextlib.suppress(ValueError):
            cap = min(cap, float(env_cap))
    return cap


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _answer_facts(content: str, redactor: Any) -> dict[str, Any]:
    """Shape of the final answer without its text (the raw trace keeps the full text)."""
    safe = redactor.redact(content)
    lowered = safe.lower()
    return {
        "chars": len(content),
        "sha256": _sha256_text(content),
        "mentionsSource": any(
            token in lowered
            for token in (
                "lk.hse.ru",
                "schedule",
                "расписан",
                ".json",
                "файл",
                "endpoint",
                "api",
                "источник",
            )
        ),
        "mentionsBarrier": any(
            token in lowered
            for token in (
                "не удалось",
                "не получилось",
                "барьер",
                "ошиб",
                "не смог",
                "captcha",
                "капч",
            )
        ),
    }


def _write_raw(run: Any, redactor: Any, name: str, payload: Any) -> str:
    from bench.live.redact import write_private_json

    path = run.scenario_dir("v4-agent") / name
    write_private_json(path, payload, redactor)
    return run.relative(path)


def _new_summary(
    *,
    scenario: str,
    run_id: str,
    started_at: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "benchVersion": __version__,
        "scenario": scenario,
        "title": SCENARIOS[scenario]["title"],
        "runId": run_id,
        "startedAt": started_at,
        "finishedAt": None,
        "headSha": support.git_head_sha(),
        "host": support.host_facts(),
        "product": {
            "engine": "v4",
            "wiring": "botai.agent.v4.factory.build_agent_wiring",
            "traceStore": "memory (no DB in the bench process)",
            "benchVersion": __version__,
            **product,
        },
        "durationSec": 0.0,
        "status": "running",
        "outcome": "unknown",
        "barrier": None,
        "metrics": {},
        "tools": [],
        "endpoints": [],
        "workspace": [],
        "answer": {},
        "secretFlow": {},
        "checks": {},
        "proven": [],
        "notProven": [],
        "evidence": {},
    }


def _finish(summary: dict[str, Any], *, started_mono: float) -> dict[str, Any]:
    summary["durationSec"] = round(time.monotonic() - started_mono, 3)
    summary["finishedAt"] = support.now_iso()
    return summary


def _fill_from_result(
    summary: dict[str, Any],
    result: Any,
    *,
    redactor: Any,
    limits: Any,
    expected: list[str],
    not_proven: list[str],
) -> dict[str, Any]:
    """Status, counters, tool sequence, endpoints, answer facts, secret-leak checks.

    `expected` are the claims the scenario was designed to prove. They become `proven` **only if
    the run actually succeeded**: a barrier run keeps an honest empty/partial `proven` list and
    says exactly what was not reached, so the evidence never claims a success that did not happen.
    """
    checks = summary.setdefault("checks", {})
    metrics = summary.setdefault("metrics", {})
    if result is not None:
        summary["status"] = result.status
        metrics.update(
            {
                "steps": result.steps,
                "toolCalls": result.tool_calls,
                "totalTokens": result.total_tokens,
                "costUsd": round(result.cost_usd, 6),
                "modelCalls": sum(1 for event in result.trace.events if event.kind == "model_call"),
                "traceEvents": len(result.trace.events),
                "runStartedAt": result.trace.started_at.isoformat(),
                "runFinishedAt": result.trace.finished_at.isoformat(),
                "provider": result.trace.provider,
                "model": result.trace.model,
            }
        )
        summary["answer"] = _answer_facts(result.content, redactor)
        summary["tools"] = support.tool_sequence(result, redactor)
        summary["endpoints"] = support.observed_endpoints(result.trace, redactor)
        messages_text = json.dumps(
            [message.model_dump(mode="json") for message in result.messages],
            ensure_ascii=False,
            default=str,
        )
        trace_text = json.dumps(
            [event.model_dump(mode="json") for event in result.trace.events],
            ensure_ascii=False,
            default=str,
        )
        checks["messagesLeak"] = support.secret_leaks(messages_text, redactor)
        checks["traceLeak"] = support.secret_leaks(trace_text, redactor)
        if result.status != "answer":
            summary["barrier"] = {
                "status": result.status,
                "errorCode": result.error_code,
                "errorDetail": redactor.redact(result.error_detail)[:500],
                "limitReached": result.limit_reached,
                "lastToolErrors": [
                    {"tool": item["tool"], "error": item["error"]}
                    for item in summary["tools"]
                    if item.get("error")
                ][-4:],
            }
    else:
        summary["status"] = "error"
        for key in ("steps", "toolCalls", "totalTokens"):
            metrics.setdefault(key, 0)
        metrics.setdefault("costUsd", 0.0)

    metrics["limits"] = limits.model_dump(mode="json", exclude_none=True)
    success = (
        summary["status"] == "answer"
        and not checks.get("messagesLeak")
        and not checks.get("traceLeak")
    )
    summary["outcome"] = "success" if success else "barrier"
    facts = _observed_facts(summary)
    if success:
        summary["proven"] = [*expected, *facts]
        summary["notProven"] = list(not_proven)
    else:
        summary["proven"] = facts
        status = summary.get("status")
        barrier = summary.get("barrier") or {}
        barrier_code = barrier.get("limitReached") or barrier.get("errorCode") or status
        summary["notProven"] = [
            f"успех не доказан: прогон закончился статусом `{status}` (барьер {barrier_code})",
            *not_proven,
        ]
    return summary


def _observed_facts(summary: dict[str, Any]) -> list[str]:
    """Facts actually visible in this run's artifacts (safe to claim even on a partial run)."""
    facts: list[str] = []
    tools = summary.get("tools") or []
    if tools:
        ok = sum(1 for item in tools if item.get("ok"))
        facts.append(
            f"инструменты v4 исполнены: {ok}/{len(tools)} вызовов ok (последовательность ниже)"
        )
    for item in summary.get("workspace") or []:
        if item.get("scheduleLike") or (item.get("records") or 0) > 0:
            facts.append(
                f"файл данных в workspace: {item.get('path')} "
                f"({item.get('bytes')} байт, записей: {item.get('records')})"
            )
        elif str(item.get("path", "")).startswith("screenshots/"):
            facts.append(f"скриншот в workspace: {item.get('path')} ({item.get('bytes')} байт)")
    secret_flow = summary.get("secretFlow") or {}
    checks = summary.get("checks") or {}
    if (
        secret_flow.get("registeredValues")
        and not checks.get("traceLeak")
        and not checks.get("messagesLeak")
    ):
        facts.append("первичные секреты: только handle/Vault; в трассе и сообщениях значений нет")
    for signal in summary.get("liveSignals") or []:
        if signal.get("ok") and signal.get("fact"):
            facts.append(str(signal["fact"]))
    return facts


def live_signals(result: Any, redactor: Any) -> list[dict[str, Any]]:
    """What actually happened on the live site, derived from tool results (no page content)."""
    if result is None:
        return []
    summaries = [
        redactor.redact(str((event.payload or {}).get("summary") or ""))
        for event in result.trace.events
        if event.kind == "tool_result"
    ]
    joined = "\n".join(summaries)
    signals: list[dict[str, Any]] = []
    if "keycloak-auth" in joined:
        signals.append(
            {
                "check": "login_confirmed",
                "ok": True,
                "fact": "вход подтверждён: submit формы SSO → redirect на lk.hse.ru/api/keycloak-auth/",
            }
        )
    if "lk.hse.ru/schedule" in joined:
        signals.append(
            {
                "check": "schedule_page_opened",
                "ok": True,
                "fact": "страница расписания открыта: lk.hse.ru/schedule (begin_date в query)",
            }
        )
    network_counts = [item for item in summaries if item.startswith("Сеть: ")]
    if network_counts:
        signals.append(
            {
                "check": "network_observed",
                "ok": True,
                "fact": f"сетевой лог наблюдался ({network_counts[-1][:80]})",
            }
        )
    return signals


# --- L3: live HSE ---------------------------------------------------------------------------------


def run_live(args: argparse.Namespace) -> int:
    from bench.live.paths import iso_now, make_run_dir
    from bench.live.redact import Redactor
    from bench.live.secret_broker import has_secret

    settings, problems = support.load_settings()
    budget_usd = _budget_usd(args)
    limits = support.clamped_limits(
        settings, max_steps=args.max_steps, max_seconds=args.max_seconds, max_usd=budget_usd
    )
    preconditions = {
        "scenario": "live-hse",
        "headSha": support.git_head_sha(),
        "llmProvider": settings.llm_provider,
        "providerModel": support.provider_model_name(settings),
        "deepseekKeyPresent": bool(settings.deepseek_api_key.get_secret_value()),
        "keychain": {name: has_secret(name) for name in ("hse_mail", "hse_password")},
        "browser": "headed custom BrowserControl"
        if args.headed
        else "playwright chromium headless",
        "limits": limits.model_dump(mode="json", exclude_none=True),
        "budgetUsd": budget_usd,
        "liveLlmEnabled": os.environ.get("BOTAI_LIVE_LLM") == "1",
        "evidence": str(support.EVIDENCE_ROOT / support.EVIDENCE_DATE_DIR),
        "problems": problems,
    }
    if args.dry_run:
        print(json.dumps(preconditions, ensure_ascii=False, indent=2))
        ready = not problems and all(preconditions["keychain"].values())
        return 0 if ready else 2
    if problems:
        print(json.dumps({"refused": problems}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    if os.environ.get("BOTAI_LIVE_LLM") != "1":
        print(
            json.dumps(
                {
                    "refused": "живой прогон тратит деньги: нужен BOTAI_LIVE_LLM=1",
                    "estimated": f"не больше ${budget_usd:.2f} за запуск",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    missing = [name for name, present in preconditions["keychain"].items() if not present]
    if missing:
        print(
            json.dumps(
                {
                    "refused": f"нет в Keychain: {', '.join(missing)}",
                    "hint": "python -m bench.live.secret_broker status",
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    redactor = Redactor()
    run = make_run_dir(date=args.date)
    started_at = iso_now()
    started_mono = time.monotonic()
    print(f"== v4 acceptance live-hse | run {run.run_id}", flush=True)
    print(f"== raw artifacts: {run.root}", flush=True)
    print(
        f"== caps: steps<={args.max_steps} time<={args.max_seconds:g}s cost<=${budget_usd:.2f}",
        flush=True,
    )
    try:
        summary = asyncio.run(
            _live_async(args, run, redactor, settings, limits, budget_usd, started_at, started_mono)
        )
    except Exception as exc:  # a bench bug is reported honestly, not hidden
        summary = _new_summary(
            scenario="live-hse", run_id="bench-crash", started_at=started_at, product={}
        )
        summary.update(
            {
                "status": "bench_crash",
                "outcome": "barrier",
                "barrier": {
                    "status": "bench_crash",
                    "errorCode": type(exc).__name__,
                    "errorDetail": redactor.redact(str(exc))[:500],
                },
                "notProven": ["прогон не завершён: ошибка самого bench-скрипта, не продукта"],
            }
        )
        summary = _finish(summary, started_mono=started_mono)
        print(json.dumps(summary["barrier"], ensure_ascii=False, indent=2), file=sys.stderr)
    evidence_path = support.write_evidence(summary, redactor)
    print(json.dumps(_printable(summary), ensure_ascii=False, indent=2))
    print(f"== raw:      {run.root}")
    print(f"== evidence: {evidence_path}")
    return 0 if summary.get("outcome") == "success" else 1


async def _live_async(
    args: argparse.Namespace,
    run: Any,
    redactor: Any,
    settings: Any,
    limits: Any,
    budget_usd: float,
    started_at: str,
    started_mono: float,
) -> dict[str, Any]:
    from botai.agent.v4.factory import build_agent_wiring, workspace_for
    from botai.agent.v4.handlers import SYSTEM_PROMPT
    from botai.agent.v4.providers.factory import build_provider
    from botai.agent.v4.runtime import RunTask
    from botai.agent.v4.traces import MemoryTraceStore
    from botai.core.ids import new_id

    run_id = new_id()
    workspace_root = run.raw / "agent-workspace"
    ask = support.KeychainAskChannel(redactor)
    services = support.build_services(settings)
    provider = build_provider(settings)
    control = None
    wiring = None
    result = None
    failure: dict[str, Any] | None = None
    run_started = time.monotonic()
    try:
        try:
            if args.headed:
                from botai.browser.control import BrowserControl, ControlConfig

                workspace = workspace_for(workspace_root, run_id)
                control = await BrowserControl.launch(
                    gateway=None,
                    config=ControlConfig(
                        headless=False,
                        upload_root=workspace.root,
                        redact_body=redactor.redact,
                    ),
                )
            wiring = await build_agent_wiring(
                services,
                user_id="bench-owner",
                run_id=run_id,
                job_id=None,
                workspace_root=workspace_root,
                provider=provider,
                trace_store=MemoryTraceStore(),
                with_browser=True,
                control=control,
            )
            messages = ()
            if args.inject_task_message:
                from botai.core.llm import LlmMessage

                # WORKAROUND for the product bug "RunTask.task never enters the conversation"
                # (runtime.py builds convo from system + task.messages only). Labeled in evidence
                # as NOT product-path; the acceptance run uses the product path without this flag.
                messages = (LlmMessage(role="user", content=LIVE_TASK),)
            result = await wiring.runtime.run(
                RunTask(
                    run_id=run_id,
                    task=LIVE_TASK,
                    messages=messages,
                    system=SYSTEM_PROMPT,
                    purpose="agent",
                    workspace=str(wiring.workspace.root),
                    user_id="bench-owner",
                    metadata={"bench": "lane-e", "scenario": "live-hse"},
                ),
                provider,
                wiring.tools,
                trace_store=wiring.trace_store,
                limits=limits,
                ask=ask,
            )
        except Exception as exc:  # recorded as an honest barrier below
            failure = {
                "status": "error",
                "errorCode": type(exc).__name__,
                "errorDetail": redactor.redact(str(exc))[:500],
            }
    finally:
        run_duration = round(time.monotonic() - run_started, 3)
        if wiring is not None:
            await wiring.aclose()
        else:
            if control is not None:
                await control.close()
            await provider.aclose()

    # Raw evidence (outside the repository; exact secret values are redacted in place).
    _write_raw(
        run,
        redactor,
        "run-meta.json",
        {
            "scenario": "live-hse",
            "runId": run_id,
            "headSha": support.git_head_sha(),
            "task": LIVE_TASK,
            "limits": limits.model_dump(mode="json", exclude_none=True),
            "browser": "headed" if args.headed else "headless",
            "durationSec": run_duration,
            "host": support.host_facts(),
        },
    )
    _write_raw(run, redactor, "ask-log.json", ask.summary())
    if result is not None:
        _write_raw(
            run,
            redactor,
            "trace.json",
            [event.model_dump(mode="json") for event in result.trace.events],
        )
        _write_raw(
            run,
            redactor,
            "messages.json",
            [message.model_dump(mode="json") for message in result.messages],
        )
        _write_raw(run, redactor, "result.json", result.model_dump(mode="json"))

    summary = _new_summary(
        scenario="live-hse",
        run_id=run_id,
        started_at=started_at,
        product={
            "env": settings.env,
            "agentFlag": settings.agent_engine,
            "provider": "deepseek",
            "model": support.provider_model_name(settings),
            "executor": "LocalExecutor (ENV=dev; prod guard not triggered)",
            "db": "guarded offline session factory: source_*/rag_* not exercised in this scenario",
            "browser": "headed custom BrowserControl"
            if args.headed
            else "playwright chromium headless",
            "limits": limits.model_dump(mode="json", exclude_none=True),
            "budgetUsd": budget_usd,
            "task": LIVE_TASK,
            "runDurationSec": run_duration,
            "injectedTaskMessage": bool(args.inject_task_message),
        },
    )
    if args.inject_task_message:
        summary["workaround"] = {
            "name": "inject-task-message",
            "why": (
                "продуктовый баг: RunTask.task не попадает в диалог модели "
                "(backend/src/botai/agent/v4/runtime.py, convo = system + task.messages); "
                "задача передана явным user-сообщением"
            ),
            "productPath": False,
            "acceptanceRun": False,
        }
    summary["secretFlow"] = {
        **ask.summary(),
        "registeredValues": redactor.registeredCount,
        "policy": (
            "креды только из Keychain (botai.live.hse.*); модели — handle; "
            "значения не печатаются и не пишутся в репозиторий"
        ),
    }
    summary["checks"] = {}
    _fill_from_result(
        summary,
        result,
        redactor=redactor,
        limits=limits,
        expected=[
            "агент v4 прошёл задачу своими инструментами v4 (без рецепта в коде) и дал ответ",
            "живой вход ВШЭ выполнен с кредами из Keychain (модель видела только handle)",
            "данные расписания найдены и сохранены файлом в workspace задачи",
        ],
        not_proven=[
            "устойчивость и качество агента: один прогон одного SHA, без повторов и медиан",
            "ветки MFA/капчи проверены, только если они реально встретились в этом прогоне",
            "ЦУ и другие вузы в этот прогон не входили (scope lane E: только ВШЭ)",
        ],
    )
    if failure is not None:
        summary["status"] = "error"
        summary["outcome"] = "barrier"
        summary["barrier"] = failure
        summary["notProven"] = ["задача не завершена — успех не доказан", *summary["notProven"]]
    summary["workspace"] = support.workspace_inventory(workspace_root / run_id, redactor)
    summary["liveSignals"] = live_signals(result, redactor)
    if result is not None:
        summary["proven"] = (
            _observed_facts(summary) if summary["outcome"] != "success" else summary["proven"]
        )
    sweep = support.secret_sweep(run.root, redactor)
    summary["checks"]["secretSweep"] = sweep
    summary["checks"]["artifactPolicy"] = (
        "raw (трасса, сообщения, workspace) — вне репозитория, 0700/0600; "
        "значения первичных секретов вычищены и в raw"
    )
    if sweep["filesRedacted"]:
        summary["notProven"].append(
            f"финальный sweep переписал {sweep['filesRedacted']} файл(ов) в raw — "
            "проверить, почему значение секрета оказалось в артефакте"
        )
    summary["evidence"] = {
        "repoSummary": f"docs/v4/_evidence/{support.EVIDENCE_DATE_DIR}/live-hse-{run_id}.json",
        "rawRoot": "~/.local/state/botai-v4-bench/<date>/<run>/raw/v4-agent/",
        "rawFiles": [
            "run-meta.json",
            "ask-log.json",
            "trace.json",
            "messages.json",
            "result.json",
        ],
    }
    return _finish(summary, started_mono=started_mono)


# --- L2: deterministic fakeuni --------------------------------------------------------------------


def run_l2(args: argparse.Namespace) -> int:
    from bench.live.paths import iso_now, make_run_dir
    from bench.live.redact import Redactor

    plan = {
        "scenario": "l2-fakeuni",
        "headSha": support.git_head_sha(),
        "site": "loopback fakeuni (tests/agent/v4/agent_wiring_support.create_agent_site)",
        "provider": "mock (scripted decisions; no model calls, no money)",
        "browser": "playwright chromium headless",
        "wiring": "botai.agent.v4.factory.build_agent_wiring",
        "limits": {"steps": 12, "seconds": 180, "usd": 0.0},
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    redactor = Redactor()
    run = make_run_dir(date=args.date)
    started_at = iso_now()
    started_mono = time.monotonic()
    print(f"== v4 acceptance l2-fakeuni | run {run.run_id}", flush=True)
    summary = asyncio.run(_l2_async(run, redactor, started_at, started_mono))
    evidence_path = support.write_evidence(summary, redactor)
    print(json.dumps(_printable(summary), ensure_ascii=False, indent=2))
    print(f"== raw:      {run.root}")
    print(f"== evidence: {evidence_path}")
    return 0 if summary.get("outcome") == "success" else 1


async def _l2_async(
    run: Any, redactor: Any, started_at: str, started_mono: float
) -> dict[str, Any]:
    from botai.agent.v4.factory import build_agent_wiring
    from botai.agent.v4.handlers import SYSTEM_PROMPT
    from botai.agent.v4.providers import MockProvider
    from botai.agent.v4.runtime import RunLimits, RunTask
    from botai.agent.v4.traces import MemoryTraceStore
    from botai.core.ids import new_id
    from botai.core.llm import LlmResponse, LlmToolCall, LlmUsage
    from botai.settings import Settings
    from tests.agent.v4.agent_wiring_support import SCHEDULE_ROWS, serve_agent_site

    zero = LlmUsage(model="mock", latency_ms=0)

    def tool_response(*calls: LlmToolCall) -> LlmResponse:
        return LlmResponse(content="", tool_calls=list(calls), usage=zero)

    def answer_response(text: str) -> LlmResponse:
        return LlmResponse(content=text, usage=zero)

    def call(name: str, arguments: dict[str, Any], call_id: str) -> LlmToolCall:
        return LlmToolCall(id=call_id, name=name, arguments=arguments)

    settings = Settings(_env_file=None, env="test", agent_engine="v4", llm_provider="mock")
    limits = RunLimits(
        max_steps=12, max_seconds=180.0, max_usd=0.01, hard_stop_usd=0.01, auto_continue=False
    )
    run_id = new_id()
    workspace_root = run.raw / "agent-workspace"
    services = support.build_services(settings)
    result = None
    failure: dict[str, Any] | None = None
    run_started = time.monotonic()
    async with serve_agent_site() as base:
        rows = [{"day": day, "subject": subject, "time": at} for day, subject, at in SCHEDULE_ROWS]
        saved = {"source": f"{base}/cabinet", "rows": rows}
        provider = MockProvider.from_responses(
            tool_response(call("browser_navigate", {"url": f"{base}/"}, "l2-1")),
            tool_response(call("browser_read", {"mode": "links"}, "l2-2")),
            tool_response(call("browser_click", {"target": {"text": "Расписание"}}, "l2-3")),
            tool_response(call("browser_read", {"mode": "tables"}, "l2-4")),
            tool_response(
                call(
                    "fs_write",
                    {"path": "schedule.json", "content": json.dumps(saved, ensure_ascii=False)},
                    "l2-5",
                )
            ),
            tool_response(call("fs_read", {"path": "schedule.json"}, "l2-6")),
            answer_response(f"Расписание сохранено в schedule.json; источник: {base}/cabinet"),
        )
        wiring = None
        try:
            try:
                wiring = await build_agent_wiring(
                    services,
                    user_id="bench-owner",
                    run_id=run_id,
                    job_id=None,
                    workspace_root=workspace_root,
                    provider=provider,
                    trace_store=MemoryTraceStore(),
                    with_browser=True,
                )
                result = await wiring.runtime.run(
                    RunTask(
                        run_id=run_id,
                        task=(
                            "Зайди на fakeuni, найди расписание занятий, сохрани его в файл "
                            "workspace и ответь с источником."
                        ),
                        system=SYSTEM_PROMPT,
                        purpose="agent",
                        workspace=str(wiring.workspace.root),
                        user_id="bench-owner",
                        metadata={"bench": "lane-e", "scenario": "l2-fakeuni"},
                    ),
                    provider,
                    wiring.tools,
                    trace_store=wiring.trace_store,
                    limits=limits,
                )
            except Exception as exc:
                failure = {
                    "status": "error",
                    "errorCode": type(exc).__name__,
                    "errorDetail": redactor.redact(str(exc))[:500],
                }
        finally:
            run_duration = round(time.monotonic() - run_started, 3)
            if wiring is not None:
                await wiring.aclose()

    if result is not None:
        _write_raw(
            run,
            redactor,
            "trace.json",
            [event.model_dump(mode="json") for event in result.trace.events],
        )
        _write_raw(
            run,
            redactor,
            "messages.json",
            [message.model_dump(mode="json") for message in result.messages],
        )
        _write_raw(run, redactor, "result.json", result.model_dump(mode="json"))

    summary = _new_summary(
        scenario="l2-fakeuni",
        run_id=run_id,
        started_at=started_at,
        product={
            "env": "test",
            "agentFlag": settings.agent_engine,
            "provider": "mock",
            "model": "mock",
            "executor": "LocalExecutor",
            "db": "guarded offline session factory: source_*/rag_* not exercised in this scenario",
            "browser": "playwright chromium headless",
            "limits": limits.model_dump(mode="json", exclude_none=True),
            "runDurationSec": run_duration,
        },
    )
    _fill_from_result(
        summary,
        result,
        redactor=redactor,
        limits=limits,
        expected=[
            "v4-wiring исполняет connect-подобный сценарий инструментами: навигация → чтение "
            "ссылок → клик → таблица → файл → проверка файла, без рецепта в коде",
            "fakeuni — тот же L1-стенд, что в тестах (`create_agent_site`), браузер настоящий",
            "mock-модель: детерминированно и без стоимости; токенов/денег не тратится",
        ],
        not_proven=[
            "качество LLM не проверяется: решения заскриптованы mock-провайдером",
            "L2 fakeuni не заменяет L3 живого вуза (D-010)",
            "прямого аналога `make bench` для v4 нет: make bench меряет v3-путь и тратит деньги",
        ],
    )
    if failure is not None:
        summary["status"] = "error"
        summary["outcome"] = "barrier"
        summary["barrier"] = failure
        summary["notProven"] = ["задача не завершена — успех не доказан", *summary["notProven"]]
    summary["workspace"] = support.workspace_inventory(workspace_root / run_id, redactor)
    summary["liveSignals"] = live_signals(result, redactor)
    if result is not None:
        summary["proven"] = (
            _observed_facts(summary) if summary["outcome"] != "success" else summary["proven"]
        )
    sweep = support.secret_sweep(run.root, redactor)
    summary["checks"]["secretSweep"] = sweep
    summary["checks"]["artifactPolicy"] = "raw вне репозитория; первичных секретов в сценарии нет"
    summary["comparisonWithMakeBench"] = {
        "makeBench": (
            "python -m botai.agent.bench (`make bench`): v3-путь (handle_chat/learn), DeepSeek, "
            "throw-away Postgres + synthetic university; money caps $3 за запуск и $12 всего "
            "(bench/results/ledger.json); нужен BOTAI_LIVE_LLM=1"
        ),
        "thisL2": "v4 runtime + MockProvider + настоящий Chromium на fakeuni; $0, без БД",
        "verdict": (
            "сравнение не «яблоко к яблоку»: make bench меряет качество модели на v3; "
            "v4-бенчмарка L2 (план 4.3) пока нет — этот сценарий доказывает только tool-loop v4"
        ),
    }
    summary["evidence"] = {
        "repoSummary": f"docs/v4/_evidence/{support.EVIDENCE_DATE_DIR}/l2-fakeuni-{run_id}.json",
        "rawRoot": "~/.local/state/botai-v4-bench/<date>/<run>/raw/v4-agent/",
        "rawFiles": ["trace.json", "messages.json", "result.json"],
    }
    return _finish(summary, started_mono=started_mono)


# --- output ---------------------------------------------------------------------------------------


def _printable(summary: dict[str, Any]) -> dict[str, Any]:
    """A short, secret-free console view (full facts are in the evidence JSON)."""
    metrics = summary.get("metrics") or {}
    workspace = summary.get("workspace") or []
    return {
        "scenario": summary.get("scenario"),
        "runId": summary.get("runId"),
        "headSha": summary.get("headSha"),
        "status": summary.get("status"),
        "outcome": summary.get("outcome"),
        "durationSec": summary.get("durationSec"),
        "steps": metrics.get("steps"),
        "toolCalls": metrics.get("toolCalls"),
        "totalTokens": metrics.get("totalTokens"),
        "costUsd": metrics.get("costUsd"),
        "barrier": summary.get("barrier"),
        "tools": [
            {"tool": item.get("tool"), "ok": item.get("ok"), "error": item.get("error")}
            for item in summary.get("tools") or []
        ],
        "workspace": [
            {
                "path": item.get("path"),
                "bytes": item.get("bytes"),
                "scheduleLike": item.get("scheduleLike"),
                "records": item.get("records"),
            }
            for item in workspace
        ],
        "checks": summary.get("checks"),
        "secretFlow": {
            "registeredValues": (summary.get("secretFlow") or {}).get("registeredValues"),
            "deliveries": (summary.get("secretFlow") or {}).get("deliveries"),
        },
    }


if __name__ == "__main__":
    sys.exit(main())
