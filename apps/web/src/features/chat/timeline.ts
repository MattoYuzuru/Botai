/**
 * Лента чата: из того, что лежит в истории на сервере, того, что ждёт отправки, и того, что приходит по потоку
 * задания прямо сейчас, собирается один список сообщений. Чистая функция: «сейчас» приходит аргументом.
 */
import { ApiError, humanMessage } from "@/api/errors";
import type { Message } from "@/api/types";
import { type ParsedBlock, parseBlock } from "@/features/jobs/blocks";
import { isTerminal, type JobSnapshot, type StepSummary, thinkingSteps } from "@/features/jobs/job-state";
import { resultConfidence } from "./confidence";
import type { OutboxItem } from "./outbox";

export type UserStatus = "sent" | "sending" | "queued";

export type TimelineItem =
  | { kind: "user"; key: string; text: string; at: number; status: UserStatus }
  | { kind: "agent"; key: string; blocks: ParsedBlock[]; at: number; jobId: string | null }
  | {
      kind: "live";
      key: string;
      jobId: string | null;
      at: number;
      steps: StepSummary[];
      blocks: ParsedBlock[];
      /** Идёт ли ещё работа агента. */
      running: boolean;
      /** Когда работа закончилась, мс (для «Думал · 12 с»). */
      finishedAt?: number;
      /** Человеческий текст сбоя; кодов в нём нет. */
      failure: string | null;
      /** Итог задания сообщает, что агент остановился не ответом. */
      unsure: boolean;
    };

/** Как долго после последнего вопроса считаем, что ответ ещё может прийти (без известного задания). */
export const AWAIT_REPLY_MS = 3 * 60_000;

const num = (iso: string): number => {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : 0;
};

/** Текст сообщения человека: его текстовые блоки подряд. */
export function userText(message: Message): string {
  return message.blocks
    .map((block) => (block.type === "text" && typeof block.markdown === "string" ? block.markdown : ""))
    .filter(Boolean)
    .join("\n\n");
}

function parseBlocks(message: Message): ParsedBlock[] {
  return message.blocks
    .map((raw, index) => parseBlock(raw, `${message.id}-${index}`))
    .filter((block): block is ParsedBlock => block !== null);
}

/** Человеческий текст сбоя задания: код ошибки в интерфейс не попадает. */
export function failureText(job: JobSnapshot): string {
  if (job.state === "cancelled") return "Ответ остановлен.";
  const problem = job.problem;
  if (!problem) return "Не удалось получить ответ. Спросите ещё раз чуть позже.";
  return humanMessage(new ApiError(500, problem.code, problem.detail, problem.extra));
}

/** Когда пришёл последний блок задания: так «Думал · 12 с» замирает, а не тикает дальше. */
function lastBlockAt(job: JobSnapshot, fallback: number): number {
  const times = job.blocks.map((block) => (block.at ? num(block.at) : 0)).filter((ms) => ms > 0);
  return times.length > 0 ? Math.max(...times) : fallback;
}

/**
 * Блоки задания, которые сервер не сохраняет в истории (в ней остаётся только итоговый текст): готовый текст для
 * ручной отправки, одобренный запрос, сводка. Пока задание привязано к чату, они остаются в ленте над ответом.
 */
export function keptBlocks(job: JobSnapshot): ParsedBlock[] {
  return job.blocks.filter((block) => block.type !== "thinking" && block.type !== "text");
}

export interface TimelineInput {
  messages: readonly Message[];
  pending: readonly OutboxItem[];
  job: JobSnapshot | null;
  nowMs: number;
}

export function buildTimeline({ messages, pending, job, nowMs }: TimelineInput): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      items.push({
        kind: "user",
        key: message.id,
        text: userText(message),
        at: num(message.createdAt),
        status: "sent",
      });
    } else {
      items.push({
        kind: "agent",
        key: message.id,
        blocks: parseBlocks(message),
        at: num(message.createdAt),
        jobId: message.jobId,
      });
    }
  }
  for (const item of pending) {
    items.push({
      kind: "user",
      key: `local-${item.id}`,
      text: item.text,
      at: item.createdAt,
      status: item.status === "queued" ? "queued" : "sending",
    });
  }

  const persisted = job
    ? messages.some((message) => message.role === "agent" && message.jobId === job.jobId)
    : false;
  if (job && !persisted) {
    const steps = thinkingSteps(job);
    const blocks = job.blocks.filter((block) => block.type !== "thinking");
    const running = !isTerminal(job.state);
    const failure = job.state === "failed" || job.state === "cancelled" ? failureText(job) : null;
    const started = job.startedAt ?? nowMs;
    items.push({
      kind: "live",
      key: `live-${job.jobId}`,
      jobId: job.jobId,
      at: started,
      // Задание ещё ничего не прислало: честно говорим, что ждём, а не придумываем шаги.
      steps:
        steps.length === 0 && running
          ? [{ id: "waiting-for-agent", label: "Жду ответ агента", state: "running", at: started }]
          : steps,
      blocks,
      running,
      finishedAt: running ? undefined : lastBlockAt(job, nowMs),
      failure,
      unsure: job.state === "done" && resultConfidence(job.result) === "unsure",
    });
  } else if (job && persisted && isTerminal(job.state) && keptBlocks(job).length > 0) {
    // Ответ уже в истории, но готовый текст для ручной отправки или одобренный запрос в ней не хранятся.
    const at = items.findIndex((item) => item.kind === "agent" && item.jobId === job.jobId);
    const kept: TimelineItem = {
      kind: "live",
      key: `kept-${job.jobId}`,
      jobId: job.jobId,
      at: job.startedAt ?? nowMs,
      steps: [],
      blocks: keptBlocks(job),
      running: false,
      failure: null,
      unsure: false,
    };
    items.splice(at >= 0 ? at : items.length, 0, kept);
  } else if (!job && awaitingReply(messages, pending, nowMs)) {
    // Задание неизвестно (страницу перезагрузили, ответ потерялся), но вопрос свежий: ответ ещё может прийти.
    const last = messages.at(-1);
    const started = last ? num(last.createdAt) : nowMs;
    items.push({
      kind: "live",
      key: "live-awaiting",
      jobId: null,
      at: started,
      steps: [{ id: "waiting-for-agent", label: "Жду ответ агента", state: "running", at: started }],
      blocks: [],
      running: true,
      failure: null,
      unsure: false,
    });
  }
  return items;
}

/** Последний в истории — вопрос человека, ему меньше трёх минут, очереди нет: ответ, скорее всего, в пути. */
export function awaitingReply(
  messages: readonly Message[],
  pending: readonly OutboxItem[],
  nowMs: number,
): boolean {
  if (pending.length > 0) return false;
  const last = messages.at(-1);
  return last?.role === "user" && nowMs - num(last.createdAt) < AWAIT_REPLY_MS;
}

/** Агент сейчас отвечает или ждёт человека: новое сообщение отправить нельзя (сервер ответит 409). */
export function isReplying(job: JobSnapshot | null, awaiting: boolean): boolean {
  if (job) return !isTerminal(job.state);
  return awaiting;
}
