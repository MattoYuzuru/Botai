/**
 * MSW-обработчики трасс хода агента v4: `GET /api/v1/agent/runs/{id}` и `/events?afterSeq&limit`.
 *
 * Форма ответов — по OpenAPI (`AgentRunOut`, `AgentRunEventsOut`), поэтому экран трассы в e2e
 * идёт тем же путём, что и к настоящему API. Набор состояний из адреса (`?run=`):
 * `unavailable` (движок v3 — 404), `fail` (сервер упал), `running`, `limit`, `paused`, `error`,
 * `empty`, `long` (страницы шагов). Без параметра — завершённый ход `run-answer` с полной трассой.
 */
import { delay, HttpResponse, http } from "msw";
import type { components } from "@/api/schema.gen";
import { findUser, type MockDb } from "./db";

type S = components["schemas"];
type Event = S["AgentRunEventOut"];
type Run = S["AgentRunOut"];

const MINUTE = 60_000;

export interface AgentRunHandlerOptions {
  latency?: number;
}

function problem(status: number, code: string, detail: string) {
  return new HttpResponse(
    JSON.stringify({ type: `https://botai.mnema.app/errors/${code}`, title: code, status, code, detail }),
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

type Variant = "answer" | "running" | "limit" | "paused" | "error" | "empty" | "long";

const TASK = "Разбери расписание на неделю и найди ближайшие сроки по заданиям.";

function runOf(now: number, variant: Variant): Run {
  const startedAt = new Date(now - 4 * MINUTE).toISOString();
  const finishedAt = variant === "running" ? null : new Date(now - 2 * MINUTE).toISOString();
  const status: Record<Variant, string> = {
    answer: "answer",
    running: "running",
    limit: "step_limit",
    paused: "paused",
    error: "error",
    empty: "answer",
    long: "answer",
  };
  return {
    runId: `run-${variant}`,
    jobId: `01JMKCDB00000000000000JOB`,
    task: TASK,
    provider: "deepseek",
    model: "deepseek-chat",
    status: status[variant],
    startedAt,
    finishedAt,
    steps: variant === "running" ? 1 : 2,
    totalTokens: variant === "running" ? 2_912 : 4_322,
    costUsd: variant === "running" ? 0.0021 : 0.0064,
    eventCount: 0,
  };
}

function event(
  seq: number,
  kind: string,
  step: number,
  at: number,
  payload: Record<string, unknown>,
  durationMs = 0,
): Event {
  return {
    seq,
    kind,
    step,
    durationMs,
    at: new Date(at).toISOString(),
    payload,
  };
}

function startedEvent(seq: number, at: number): Event {
  return event(seq, "run_started", 0, at, {
    task: TASK,
    provider: "deepseek",
    model: "deepseek-chat",
    purpose: "chat",
    limits: { max_steps: 40, max_seconds: 300, max_cost_usd: 0.5 },
    tool_count: 12,
    metadata: { thread_id: "01JMKCDB00000000000000THR" },
  });
}

function modelEvent(seq: number, step: number, at: number, content: string, withTool: boolean): Event {
  return event(
    seq,
    "model_call",
    step,
    at,
    {
      model: "deepseek-chat",
      input_cache_hit: 3_000,
      input_cache_miss: 812,
      output: 210,
      reasoning: 0,
      latency_ms: 4_200,
      cost_usd: 0.0032,
      content,
      reasoning_present: true,
      tool_calls: withTool
        ? [{ id: "call_1", name: "sources_sync", arguments: { source_id: "src-hse-portal" } }]
        : [],
    },
    4_200,
  );
}

const TOOL_RESULT: Record<string, unknown> = {
  ok: true,
  summary: "Источник обновлён: 12 занятий и 4 срока на две недели.",
  data: { counts: { schedule: 12, deadlines: 4 } },
  truncated: false,
  next: null,
  error: null,
  cost: { ms: 1_800 },
  artifacts: [
    {
      id: "artifact-schedule-1",
      kind: "page",
      bytes: 18_420,
      sha256: "a".repeat(64),
      url: "https://lk.fakeuni.test/schedule",
    },
    {
      id: "artifact-json-2",
      kind: "json",
      bytes: 6_240,
      sha256: "b".repeat(64),
      url: null,
    },
  ],
};

function toolEvents(seq: number, step: number, at: number): Event[] {
  return [
    event(seq, "tool_call", step, at, {
      call_id: "call_1",
      tool: "sources_sync",
      arguments: { source_id: "src-hse-portal" },
      policy: "read",
    }),
    event(
      seq + 1,
      "tool_result",
      step,
      at + 1_800,
      {
        call_id: "call_1",
        tool: "sources_sync",
        ok: true,
        summary: "Источник обновлён: 12 занятий и 4 срока на две недели.",
        truncated: false,
        text_chars: 640,
        duration_ms: 1_800,
        error: null,
        result: TOOL_RESULT,
      },
      1_800,
    ),
  ];
}

function finishedEvent(seq: number, at: number, status: string, errorCode: string | null = null): Event {
  return event(seq, "run_finished", 2, at, {
    status,
    steps: 2,
    tool_calls: 1,
    total_tokens: 4_322,
    cost_usd: 0.0064,
    error_code: errorCode,
  });
}

/** Полная трасса варианта: стабильные seq, времена и payload'ы как у настоящего runtime. */
export function traceFor(now: number, variant: Variant): { run: Run; events: Event[] } {
  const run = runOf(now, variant);
  const base = now - 4 * MINUTE;
  const at = (seconds: number) => base + seconds * 1_000;
  const events: Event[] = [];
  if (variant === "empty") {
    run.eventCount = 0;
    return { run, events };
  }
  if (variant === "long") {
    events.push(startedEvent(0, at(0)));
    for (let index = 1; index <= 105; index++) {
      events.push(
        event(index, "note", 1, at(index), {
          text: `Шаг наблюдения ${index}: проверяю страницу источника.`,
        }),
      );
    }
    events.push(finishedEvent(106, at(106), "answer"));
    run.eventCount = events.length;
    return { run, events };
  }
  if (variant === "running") {
    events.push(startedEvent(0, at(0)));
    events.push(modelEvent(1, 1, at(2), "Смотрю расписание и сроки в подключённых источниках.", true));
    run.eventCount = events.length;
    return { run, events };
  }
  events.push(startedEvent(0, at(0)));
  events.push(
    modelEvent(1, 1, at(2), "Посмотрю расписание и сроки в подключённых источниках.", variant !== "paused"),
  );
  if (variant === "limit") {
    events.push(
      event(2, "limit", 1, at(6), {
        limit: "steps",
        steps: 40,
        elapsed_s: 120,
        total_tokens: 4_322,
        cost_usd: 0.0064,
        auto_continue: true,
      }),
    );
    events.push(finishedEvent(3, at(7), "step_limit"));
    run.eventCount = events.length;
    return { run, events };
  }
  if (variant === "paused") {
    events.push(event(2, "pause", 1, at(5), { reason: "control" }));
    events.push(finishedEvent(3, at(6), "paused"));
    run.eventCount = events.length;
    return { run, events };
  }
  events.push(...toolEvents(2, 1, at(7)));
  if (variant === "error") {
    events.push(
      event(4, "error", 1, at(9), {
        code: "llm_error",
        detail: "Провайдер модели завершился ошибкой.",
        retryable: true,
      }),
    );
    events.push(finishedEvent(5, at(9), "error", "llm_error"));
    run.eventCount = events.length;
    return { run, events };
  }
  events.push(
    modelEvent(
      4,
      2,
      at(10),
      "На этой неделе 12 пар и два близких срока: контрольная по алгоритмам 30 октября и эссе 2 ноября. Оба срока есть в личном кабинете.",
      false,
    ),
  );
  events.push(finishedEvent(5, at(12), "answer"));
  run.eventCount = events.length;
  return { run, events };
}

function variantOf(): Variant | "unavailable" | "fail" | null {
  try {
    const raw = new URLSearchParams(window.location.search).get("run");
    if (raw === "unavailable" || raw === "fail") return raw;
    const values: readonly Variant[] = ["answer", "running", "limit", "paused", "error", "empty", "long"];
    return raw && (values as readonly string[]).includes(raw) ? (raw as Variant) : null;
  } catch {
    return null;
  }
}

function variantForId(runId: string): Variant | null {
  const match = /^run-(answer|running|limit|paused|error|empty|long)$/.exec(runId);
  return match ? (match[1] as Variant) : null;
}

export function createAgentRunHandlers(db: MockDb, options: AgentRunHandlerOptions = {}) {
  const { latency = 0 } = options;
  const wait = async () => {
    if (latency > 0) await delay(latency);
  };
  const signedIn = () => Boolean(findUser(db, db.sessionUserId));
  const unauthorized = () => problem(401, "unauthorized", "Нужно войти в BotAI");
  const forcedVariant = (): Variant | null => {
    const value = variantOf();
    return value === "unavailable" || value === "fail" ? null : value;
  };
  const resolve = (runId: string): Variant => variantForId(runId) ?? forcedVariant() ?? "answer";

  return [
    http.get("*/api/v1/agent/runs/:runId", async ({ params }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const forced = variantOf();
      if (forced === "unavailable")
        return problem(404, "not_found", "Агент v4 выключен: включите BOTAI_AGENT_ENGINE=v4");
      if (forced === "fail") return problem(500, "internal_error", "Трасса временно недоступна");
      const runId = String(params.runId);
      const { run } = traceFor(db.now(), resolve(runId));
      return HttpResponse.json({ ...run, runId });
    }),

    http.get("*/api/v1/agent/runs/:runId/events", async ({ params, request }) => {
      await wait();
      if (!signedIn()) return unauthorized();
      const forced = variantOf();
      if (forced === "unavailable")
        return problem(404, "not_found", "Агент v4 выключен: включите BOTAI_AGENT_ENGINE=v4");
      if (forced === "fail") return problem(500, "internal_error", "Трасса временно недоступна");
      const runId = String(params.runId);
      const { events } = traceFor(db.now(), resolve(runId));
      const url = new URL(request.url);
      const afterSeq = Number(url.searchParams.get("afterSeq") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const start = Number.isFinite(afterSeq) ? Math.max(0, Math.trunc(afterSeq)) : 0;
      const size = Number.isFinite(limit) ? Math.min(500, Math.max(1, Math.trunc(limit))) : 100;
      const page = events.filter((item) => item.seq >= start);
      const shown = page.slice(0, size);
      return HttpResponse.json({
        runId,
        events: shown,
        hasMore: page.length > size,
      } satisfies S["AgentRunEventsOut"]);
    }),
  ];
}
