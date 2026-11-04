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
  finishedAt?: number;
  /** Блоки в порядке появления. */
  blocks: ParsedBlock[];
  answers: Record<string, AnswerState>;
  problem: JobProblem | null;
  result: Record<string, unknown> | null;
  link: JobLink;
  lastEventId: string | null;
}

const asRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

/** Известные коды backend/core/errors.py; произвольный legacy detail не становится кодом или текстом UI. */
const RESULT_ERROR_CODES = new Set([
  "source_auth_required",
  "source_unavailable",
  "source_rate_limited",
  "source_bot_protection",
  "login_failed",
  "login_guard_refused",
  "recipe_drift",
  "recipe_rejected",
  "egress_blocked",
  "action_changed",
  "action_expired",
  "quota_exceeded",
  "injection_blocked",
  "llm_unavailable",
  "not_found",
  "forbidden",
  "unauthorized",
  "validation",
  "conflict",
  "block_answered",
  "not_implemented",
  "too_many_attempts",
  "internal_error",
]);

/**
 * Остановленный ход агента: сервер сохранил его работу (чекпоинт) и говорит, можно ли продолжить с того же
 * места. `runId` уходит в `resumeRunId` сообщения кнопки «Продолжить».
 */
export interface JobResume {
  resumable: boolean;
  runId: string;
  /** Почему остановился: money, count, time, quota, transient, no_progress, hard_stop, manual, error… */
  reason: string;
  spentUsd: number;
  capUsd: number;
  continuations: number;
}

const finite = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);

/** Разбор `result.resume` задания-хода; всё, что не похоже на описание чекпоинта, — `null`. */
export function jobResume(result: Record<string, unknown> | null | undefined): JobResume | null {
  const raw = asRecord(result?.resume);
  if (!raw || typeof raw.resumable !== "boolean" || typeof raw.runId !== "string" || !raw.runId) return null;
  return {
    resumable: raw.resumable,
    runId: raw.runId,
    reason: typeof raw.reason === "string" ? raw.reason : "",
    spentUsd: finite(raw.spentUsd),
    capUsd: finite(raw.capUsd),
    continuations: finite(raw.continuations),
  };
}

/** Старые задания могли завершиться done с бизнес-ошибкой внутри result. */
export function jobProblem(job: JobSnapshot): JobProblem | null {
  if (job.problem) return job.problem;
  if (job.state !== "done") return null;
  const resume = jobResume(job.result);
  if (resume) {
    // Ход остановился, не дав ответа: текст причины уже в ответе агента, здесь — что с работой дальше.
    return {
      code: resume.reason === "cancelled" ? "agent_stopped" : "agent_limit",
      detail: resume.resumable
        ? "Работа остановлена, прогресс сохранён. Можно продолжить с того же места."
        : "Работа остановлена, прогресс сохранить не удалось. Опишите задачу ещё раз.",
      extra: {},
    };
  }
  const stopped = job.result?.status;
  const limit =
    ["token_limit", "time_limit", "step_limit", "cost_limit", "hard_stop"].includes(String(stopped)) ||
    job.result?.stopped === "budget";
  if (limit)
    return {
      code: "agent_limit",
      detail: "Агент остановился по лимиту. Действия сохранены в трассе; можно продолжить в чате.",
      extra: {},
    };
  if (stopped === "cancelled" || stopped === "paused")
    return {
      code: "agent_stopped",
      detail: "Работа агента остановлена. Действия сохранены в трассе.",
      extra: {},
    };
  if (stopped !== "error") return null;
  const detail = job.result?.detail;
  const code = typeof detail === "string" && RESULT_ERROR_CODES.has(detail) ? detail : "unknown";
  return { code, detail: "", extra: {} };
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

/** Updating a step closes its state, but never moves the moment it first appeared. */
function preserveThinkingStart(previous: ParsedBlock, next: ParsedBlock): ParsedBlock {
  return previous.type === "thinking" &&
    next.type === "thinking" &&
    Date.parse(previous.at) < Date.parse(next.at)
    ? { ...next, at: previous.at }
    : next;
}

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
        finishedAt: parseTime(data.finishedAt) ?? next.finishedAt,
      };
    }
    case "block": {
      const block = parseBlock(payload, `event-${event.id ?? next.blocks.length}`);
      if (!block) return next;
      const index = next.blocks.findIndex((item) => item.id === block.id);
      const blocks =
        index >= 0
          ? next.blocks.map((item, i) => (i === index ? preserveThinkingStart(item, block) : item))
          : [...next.blocks, block];
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

/** Семантика поля от сервера (`login` — поле входа, помеченное автором, остальное — по виду поля); текст вопроса и адрес сайта не определяют способ входа. */
export function isAuthInput(block: ParsedBlock): boolean {
  return (
    block.type === "input" &&
    (block.login === true ||
      (block.inputKind !== "url" &&
        (block.inputKind === "password" ||
          block.inputKind === "code" ||
          block.autocomplete === "username" ||
          block.autocomplete === "current-password" ||
          block.autocomplete === "one-time-code")))
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
 * Лог шагов для `ThinkingStream`: блоки `thinking` по порядку. Все бегущие шаги при `waiting_user`
 * получают состояние «ждёт» — это слышно словами («Жду ответа»), а не только анимацией.
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
    return steps.map((step) => (step.state === "running" ? { ...step, state: "waiting" } : step));
  }
  // Ход ждёт следующей части (пауза после сбоя модели или автоматическое продолжение): последняя строка шагов —
  // сама эта стадия, и она «ждёт», пока сервер не вернёт задание в работу.
  if (job.state === "queued" && job.stage) {
    const last = steps.at(-1);
    if (last && last.label === job.stage && last.state === "done") {
      return [...steps.slice(0, -1), { ...last, state: "waiting" }];
    }
  }
  // Задание завершилось, а шаг «бежит»: считаем его завершённым, иначе рельс будет врать.
  if (isTerminal(job.state)) {
    const closing = job.state === "cancelled" ? "skipped" : jobFailed(job) ? "failed" : "done";
    return steps.map((step) =>
      step.state === "running" || step.state === "waiting" ? { ...step, state: closing } : step,
    );
  }
  return steps;
}
