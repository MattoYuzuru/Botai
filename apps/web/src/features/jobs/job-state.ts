/**
 * Состояние задания по событиям потока (contracts/events.md §1). Чистые функции: событие → новое состояние.
 * Повторная доставка блока с тем же `id` заменяет его, а не дублирует (идемпотентность по `id`).
 */
import { type ParsedBlock, parseBlock, patchBlock } from "./blocks";

export type JobStateName = "queued" | "running" | "waiting_user" | "done" | "failed" | "cancelled";

const STATES: readonly JobStateName[] = ["queued", "running", "waiting_user", "done", "failed", "cancelled"];
export const isTerminal = (state: JobStateName) =>
  state === "done" || state === "failed" || state === "cancelled";

export interface JobProblem {
  code: string;
  detail: string;
  /** Дополнительные поля problem+json (например, `host` у `egress_blocked`). */
  extra: Record<string, unknown>;
}

export interface AnswerState {
  status: "sending" | "answered" | "failed" | "expired";
  /** Что показать в истории как ответ человека; для секрета — `null` (покажем «введено»). */
  shown: string | null;
  error: string | null;
  /** Для предложения действия: чем закончилось. */
  outcome?: "approved" | "rejected";
}

export type JobLink = "connecting" | "live" | "reconnecting" | "closed";

export interface JobSnapshot {
  jobId: string;
  state: JobStateName;
  /** Текущая стадия словами сервера («Читаю список курсов»). */
  stage: string | null;
  startedAt: number | null;
  /** Блоки в порядке появления. */
  blocks: ParsedBlock[];
  answers: Record<string, AnswerState>;
  problem: JobProblem | null;
  result: Record<string, unknown> | null;
  link: JobLink;
  lastEventId: string | null;
}

/** Старые задания могли завершиться done с бизнес-ошибкой внутри result. */
export function jobProblem(job: JobSnapshot): JobProblem | null {
  if (job.problem) return job.problem;
  if (job.state !== "done" || job.result?.status !== "error") return null;
  return { code: job.result.detail === "login_failed" ? "login_failed" : "unknown", detail: "", extra: {} };
}

export function jobFailed(job: JobSnapshot): boolean {
  return job.state === "failed" || job.state === "cancelled" || jobProblem(job) !== null;
}

export function initialJob(jobId: string): JobSnapshot {
  return {
    jobId,
    state: "queued",
    stage: null,
    startedAt: null,
    blocks: [],
    answers: {},
    problem: null,
    result: null,
    link: "connecting",
    lastEventId: null,
  };
}

export interface RawEvent {
  id?: string;
  event: string;
  data: string;
}

const asRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

const parseTime = (value: unknown): number | null => {
  if (typeof value !== "string") return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};

/** Применяет одно событие потока. Неразбираемое событие игнорируется, `Last-Event-ID` всё равно запоминается. */
export function applyEvent(job: JobSnapshot, event: RawEvent): JobSnapshot {
  const next: JobSnapshot = event.id ? { ...job, lastEventId: event.id } : job;
  const payload = parseJson(event.data);
  switch (event.event) {
    case "status": {
      const data = asRecord(payload);
      if (!data) return next;
      const state = STATES.includes(data.state as JobStateName) ? (data.state as JobStateName) : next.state;
      return {
        ...next,
        state,
        stage: typeof data.stage === "string" ? data.stage : next.stage,
        startedAt: parseTime(data.startedAt) ?? next.startedAt,
      };
    }
    case "block": {
      const block = parseBlock(payload, `event-${event.id ?? next.blocks.length}`);
      if (!block) return next;
      const index = next.blocks.findIndex((item) => item.id === block.id);
      const blocks =
        index >= 0 ? next.blocks.map((item, i) => (i === index ? block : item)) : [...next.blocks, block];
      return { ...next, blocks };
    }
    case "block_update": {
      const data = asRecord(payload);
      if (!data || typeof data.id !== "string") return next;
      const blocks = next.blocks.map((item) => (item.id === data.id ? patchBlock(item, data.patch) : item));
      return { ...next, blocks };
    }
    case "error": {
      const data = asRecord(payload);
      const { type: _t, title: _ti, status: _s, code, detail, traceId: _tr, ...extra } = data ?? {};
      return {
        ...next,
        state: "failed",
        problem: {
          code: typeof code === "string" ? code : "unknown",
          detail: typeof detail === "string" ? detail : "",
          extra,
        },
      };
    }
    case "done": {
      const data = asRecord(payload);
      return { ...next, state: "done", result: asRecord(data?.result) ?? {} };
    }
    default:
      return next;
  }
}

/** Семантика поля от сервера; текст вопроса и адрес сайта не определяют способ входа. */
export function isAuthInput(block: ParsedBlock): boolean {
  return (
    block.type === "input" &&
    block.inputKind !== "url" &&
    (block.inputKind === "password" ||
      block.inputKind === "code" ||
      block.autocomplete === "username" ||
      block.autocomplete === "current-password" ||
      block.autocomplete === "one-time-code")
  );
}

/** Реальный экран того же подключения сменил старые поля. Ответ при этом не подставляется. */
export function supersededAuthQuestions(job: JobSnapshot, connectionId?: string): Set<string> {
  if (!connectionId) return new Set();
  const screen = job.blocks.findLastIndex(
    (block) => block.type === "login" && block.connectionId === connectionId,
  );
  return new Set(
    job.blocks
      .slice(0, Math.max(0, screen))
      .filter(isAuthInput)
      .map((block) => block.id),
  );
}

/** Блоки, ждущие ответа: вопрос без ответа, пока задание не закончилось. */
export function pendingQuestions(job: JobSnapshot, connectionId?: string): ParsedBlock[] {
  if (isTerminal(job.state)) return [];
  const superseded = supersededAuthQuestions(job, connectionId);
  return job.blocks.filter((block) => {
    if (
      block.type !== "choice" &&
      block.type !== "input" &&
      block.type !== "confirm" &&
      block.type !== "approval"
    )
      return false;
    return job.answers[block.id]?.status !== "answered" && !superseded.has(block.id);
  });
}

/**
 * Блоки `wait` и `login`, которые человек должен закрыть сейчас. Блок закрыт, когда за ним пришёл вопрос
 * или итог, а также когда задание вернулось к работе (не `waiting_user`) и дописало шаги: подтверждение на
 * телефоне прошло, запасной экран входа пройден. Закрытый блок на экране не остаётся.
 */
export function activeAttention(job: JobSnapshot): ParsedBlock[] {
  if (isTerminal(job.state)) return [];
  return job.blocks.filter((block, index) => {
    if (block.type !== "wait" && block.type !== "login") return false;
    const later = job.blocks.slice(index + 1);
    if (later.some((item) => item.type !== "thinking")) return false;
    return job.state === "waiting_user" || later.length === 0;
  });
}

/** Требует ли задание человека прямо сейчас (для «Нужен ответ» в шапке). */
export function needsHuman(job: JobSnapshot, connectionId?: string): boolean {
  if (isTerminal(job.state)) return false;
  return (
    job.state === "waiting_user" ||
    pendingQuestions(job, connectionId).length > 0 ||
    activeAttention(job).length > 0
  );
}

export interface StepSummary {
  id: string;
  label: string;
  state: "running" | "done" | "failed" | "waiting" | "skipped";
  detail?: string;
  at: number;
}

/**
 * Лог шагов для `ThinkingStream`: блоки `thinking` по порядку. Последний бегущий шаг при `waiting_user`
 * получает состояние «ждёт» — это слышно словами («Жду ответа»), а не только анимацией.
 */
export function thinkingSteps(job: JobSnapshot): StepSummary[] {
  const steps: StepSummary[] = [];
  for (const block of job.blocks) {
    if (block.type !== "thinking") continue;
    const state = block.state ?? "running";
    steps.push({
      id: block.id,
      label: block.stage,
      detail: block.detail,
      state: state === "ok" ? "done" : state,
      at: Date.parse(block.at) || 0,
    });
  }
  if (job.state === "waiting_user") {
    for (let i = steps.length - 1; i >= 0; i--) {
      const step = steps[i];
      if (step?.state === "running") {
        steps[i] = { ...step, state: "waiting" };
        break;
      }
    }
  }
  // Задание завершилось, а шаг «бежит»: считаем его завершённым, иначе рельс будет врать.
  if (isTerminal(job.state)) {
    const closing = job.state === "done" ? "done" : job.state === "failed" ? "failed" : "skipped";
    return steps.map((step) =>
      step.state === "running" || step.state === "waiting" ? { ...step, state: closing } : step,
    );
  }
  return steps;
}
