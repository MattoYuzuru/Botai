/**
 * Лента чата: один тред — один таймлайн (§3.1). Из того, что лежит в истории на сервере, того, что ждёт отправки,
 * и того, что приходит по потокам заданий прямо сейчас (ход агента и фоновые задачи), собирается один список.
 * Чистая функция: «сейчас» приходит аргументом.
 *
 * Порядок — серверный: блок получает один ключ до и после сохранения, поэтому поток заданий только дополняет
 * хвост и заменяет блок на месте (например, план обновляется тем же `id`), а ответ человека не переносит вопрос.
 *
 * Мышление — исключение, как в ChatGPT: пока задание работает, его блок «Думает» стоит последним в ленте, под всем,
 * что появилось за время работы; когда оно закончилось, свёрнутый «Думал · N» стоит прямо над итоговым ответом.
 */
import { ApiError, humanMessage } from "@/api/errors";
import type { Message } from "@/api/types";
import { type AttachmentBlock, isTaskKind, type JobOut, type ThreadRuntime } from "@/api/v3";
import { type ParsedBlock, parseBlock } from "@/features/jobs/blocks";
import {
  initialJob,
  isTerminal,
  type JobSnapshot,
  jobFailed,
  jobProblem,
  type StepSummary,
  thinkingSteps,
} from "@/features/jobs/job-state";
import type { OutboxAttachment, OutboxItem } from "./outbox";
import { type MessageQuote, quoteOf } from "./quote";

export type UserStatus = "sent" | "sending" | "queued";

/** Задание треда: ход агента (`chat`) или фоновая задача (`connect`, `repair`, `sync`, `ingest`). */
export interface ThreadJob {
  id: string;
  kind: string;
  connectionId: string | null;
  snapshot: JobSnapshot;
}

export type Turn =
  | {
      kind: "user";
      key: string;
      text: string;
      attachments: readonly (AttachmentBlock | OutboxAttachment)[];
      /** Вопрос, на который отвечено (кнопка быстрого ответа, ответ на вопрос задачи). */
      quote: MessageQuote | null;
      at: number;
      status: UserStatus;
    }
  | { kind: "block"; key: string; jobId: string | null; block: ParsedBlock; at: number }
  | {
      kind: "thinking";
      key: string;
      jobId: string | null;
      steps: StepSummary[];
      running: boolean;
      finishedAt?: number;
    }
  | {
      kind: "failure";
      key: string;
      jobId: string;
      /** Человеческий текст сбоя; кодов в нём нет. */
      text: string;
      /** Источник можно перепроверить («Войти заново»). */
      connectionId: string | null;
    };

export interface Timeline {
  turns: Turn[];
  /** Быстрые ответы к последнему тексту агента, пока после него ничего нет; `quote` — вопрос, на который они отвечают. */
  quickReplies: { key: string; options: readonly string[]; quote: MessageQuote | null } | null;
}

/** Как долго после последнего вопроса считаем, что ответ ещё может прийти (без известного задания). */
export const AWAIT_REPLY_MS = 3 * 60_000;

const stamp = (value: string | null | undefined): number | null => {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};
const num = (iso: string): number => stamp(iso) ?? 0;

/** Цитата в сообщении человека: у текстового блока, который сервер сохранил вместе с ответом. */
export function userQuote(message: Message): MessageQuote | null {
  for (const block of message.blocks) {
    const quote = block.type === "text" ? (block as { quote?: unknown }).quote : undefined;
    if (typeof quote !== "object" || quote === null) continue;
    const { text, blockId } = quote as { text?: unknown; blockId?: unknown };
    if (typeof text === "string" && text.trim())
      return { text, ...(typeof blockId === "string" ? { blockId } : {}) };
  }
  return null;
}

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

/**
 * Человеческий текст сбоя задания: код ошибки в интерфейс не попадает. Остановка и вход, который не завершился,
 * называются своими словами.
 */
export function failureText(job: JobSnapshot): string {
  if (job.state === "cancelled" && !job.problem) return "Остановлено. Уже найденные данные сохранены.";
  const problem = jobProblem(job);
  if (!problem) return "Не удалось получить ответ. Спросите ещё раз чуть позже.";
  if (problem.code === "login_failed" && !problem.detail) return "Вход не завершён. Можно войти заново.";
  return humanMessage(new ApiError(500, problem.code, problem.detail, problem.extra));
}

/**
 * Состояние задания из серверного состояния треда плюс то, что пришло по потоку. Серверное — основа после
 * перезагрузки; поток добавляет свежие блоки и ответы. Незавершённое задание берёт у потока состояние и стадию,
 * как только оттуда пришли события.
 */
export function mergeJob(saved: JobOut, live: JobSnapshot | undefined): JobSnapshot {
  const current = live?.jobId === saved.id ? live : undefined;
  const events = Boolean(
    current && (current.lastEventId !== null || (isTerminal(current.state) && current.problem !== null)),
  );
  const savedTerminal = isTerminal(saved.state);
  const error = saved.error;
  const problem =
    error && typeof error.code === "string"
      ? { code: error.code, detail: typeof error.detail === "string" ? error.detail : "", extra: {} }
      : null;
  return {
    ...initialJob(saved.id),
    ...current,
    state: !savedTerminal && events && current ? current.state : saved.state,
    stage: !savedTerminal && events && current ? current.stage : saved.stage,
    startedAt: current?.startedAt ?? stamp(saved.startedAt) ?? stamp(saved.createdAt),
    problem: savedTerminal ? problem : (current?.problem ?? problem),
    result: savedTerminal ? (saved.result ?? null) : (current?.result ?? saved.result ?? null),
  };
}

/** Задания, поток которых нужен треду: ход агента и незавершённые задачи из серверного состояния. */
export function runtimeJobs(runtime: ThreadRuntime | undefined): JobOut[] {
  if (!runtime) return [];
  const rows = new Map<string, JobOut>();
  if (runtime.activeJob) rows.set(runtime.activeJob.id, runtime.activeJob);
  for (const task of runtime.tasks) if (!isTerminal(task.state)) rows.set(task.id, task);
  return [...rows.values()];
}

export interface TimelineInput {
  messages: readonly Message[];
  pending: readonly OutboxItem[];
  jobs: readonly ThreadJob[];
  nowMs: number;
}

type ThinkingTurn = Extract<Turn, { kind: "thinking" }>;

/**
 * Куда встаёт блок мышления задания. Идущее задание — в самый конец ленты («Думает» всегда внизу, в актуальном
 * состоянии). Закончившееся — прямо над итоговым ответом этого задания: над сбоем, если он был, иначе над его
 * последним текстом, иначе над его последним блоком. Если от задания в ленте больше ничего нет, блок остаётся там,
 * где появился (`after` — ключ записи, которая стояла перед ним).
 */
function placeThinking(
  turns: readonly Turn[],
  thoughts: readonly { turn: ThinkingTurn; after: string | null }[],
): Turn[] {
  const live = thoughts
    .filter(({ turn }) => turn.running)
    .map(({ turn }) => turn)
    .sort((a, b) => (a.steps[0]?.at ?? 0) - (b.steps[0]?.at ?? 0));
  const above = new Map<string, ThinkingTurn[]>();
  const below = new Map<string | null, ThinkingTurn[]>();
  const push = <K>(map: Map<K, ThinkingTurn[]>, key: K, turn: ThinkingTurn) =>
    map.set(key, [...(map.get(key) ?? []), turn]);
  for (const { turn, after } of thoughts) {
    if (turn.running) continue;
    const own = turns.filter((item) => "jobId" in item && item.jobId === turn.jobId && turn.jobId !== null);
    const answer =
      own.findLast((item) => item.kind === "failure") ??
      own.findLast((item) => item.kind === "block" && item.block.type === "text") ??
      own.findLast((item) => item.kind === "block");
    if (answer) push(above, answer.key, turn);
    else push(below, after, turn);
  }
  const out: Turn[] = [...(below.get(null) ?? [])];
  for (const turn of turns) {
    out.push(...(above.get(turn.key) ?? []), turn, ...(below.get(turn.key) ?? []));
  }
  return [...out, ...live];
}

export function buildTimeline({ messages, pending, jobs, nowMs }: TimelineInput): Timeline {
  const turns: Turn[] = [];
  const rows = new Map<string, number>();
  const thoughts = new Map<string, { blocks: Map<string, ParsedBlock>; after: string | null }>();
  const lastDurableAt = new Map<string, number>();
  const jobById = new Map(jobs.map((job) => [job.id, job]));

  const add = (block: ParsedBlock, jobId: string | null, fallback: string, live: boolean) => {
    if (block.type === "thinking") {
      if (!jobId) return;
      let group = thoughts.get(jobId);
      if (!group) {
        group = { blocks: new Map(), after: turns.at(-1)?.key ?? null };
        thoughts.set(jobId, group);
      }
      group.blocks.set(block.id, block);
      return;
    }
    const key = `block:${jobId ?? fallback}:${block.id}`;
    const existing = rows.get(key);
    const at = block.at ? num(block.at) : 0;
    if (existing !== undefined) {
      // Живой блок свежее сохранённого (план обновился, из карточки входа ушёл тикет); место остаётся прежним.
      if (live) turns[existing] = { kind: "block", key, jobId, block, at };
      return;
    }
    rows.set(key, turns.length);
    turns.push({ kind: "block", key, jobId, block, at });
  };

  for (const message of messages) {
    const at = num(message.createdAt);
    if (message.jobId && at > 0) {
      lastDurableAt.set(message.jobId, Math.max(lastDurableAt.get(message.jobId) ?? at, at));
    }
    const blocks = parseBlocks(message);
    if (message.role === "user") {
      turns.push({
        kind: "user",
        key: message.id,
        text: userText(message),
        attachments: blocks.filter((block): block is AttachmentBlock => block.type === "attachment"),
        quote: userQuote(message),
        at,
        status: "sent",
      });
      continue;
    }
    for (const block of blocks) add(block, message.jobId, message.id, false);
  }
  for (const job of jobs) for (const block of job.snapshot.blocks) add(block, job.id, job.id, true);

  // Шаги размышления каждого задания собираются в один блок «Думал · 12 с»; место ему назначает `placeThinking`.
  const thinking: { turn: ThinkingTurn; after: string | null }[] = [];
  for (const [jobId, { blocks, after }] of thoughts) {
    const job = jobById.get(jobId);
    const live = job ? !isTerminal(job.snapshot.state) : false;
    // Заданий, о которых сервер больше не говорит, давно нет в работе: незавершённый шаг закрывается.
    const snapshot: JobSnapshot = {
      ...initialJob(jobId),
      state: job ? job.snapshot.state : "done",
      blocks: [...blocks.values()],
    };
    let finishedAt: number | undefined;
    if (job && isTerminal(job.snapshot.state)) {
      finishedAt = lastBlockAt(job.snapshot) ?? lastDurableAt.get(jobId);
    } else if (!job) finishedAt = lastDurableAt.get(jobId);
    thinking.push({
      after,
      turn: {
        kind: "thinking",
        key: `thinking:${jobId}`,
        jobId,
        steps: thinkingSteps(snapshot),
        running: live,
        finishedAt,
      },
    });
  }

  // Задание идёт, а шагов ещё нет: честно говорим, что ждём, и не придумываем шаги.
  for (const job of jobs) {
    if (isTerminal(job.snapshot.state) || thoughts.has(job.id)) continue;
    const waiting = job.snapshot.state === "waiting_user";
    thinking.push({
      after: null,
      turn: {
        kind: "thinking",
        key: `thinking:${job.id}`,
        jobId: job.id,
        steps: [
          {
            id: "waiting",
            label: job.snapshot.stage ?? (job.kind === "chat" ? "Жду ответ агента" : "Думаю"),
            state: waiting ? "waiting" : "running",
            at: job.snapshot.startedAt ?? nowMs,
          },
        ],
        running: true,
      },
    });
  }

  for (const item of pending) {
    turns.push({
      kind: "user",
      key: `local-${item.id}`,
      text: item.text,
      attachments: item.attachments,
      quote: item.quote ?? null,
      at: item.createdAt,
      status: item.status === "queued" ? "queued" : "sending",
    });
  }

  const running = jobs.some((job) => !isTerminal(job.snapshot.state));
  if (jobs.length === 0 && awaitingReply(messages, pending, nowMs)) {
    // Задание неизвестно (страницу перезагрузили, ответ потерялся), но вопрос свежий: ответ ещё может прийти.
    const last = messages.at(-1);
    const started = last ? num(last.createdAt) : nowMs;
    thinking.push({
      after: null,
      turn: {
        kind: "thinking",
        key: "thinking:awaiting",
        jobId: null,
        steps: [{ id: "waiting-for-agent", label: "Жду ответ агента", state: "running", at: started }],
        running: true,
      },
    });
  }

  for (const job of jobs) {
    if (!jobFailed(job.snapshot) || job.snapshot.state === "queued") continue;
    turns.push({
      kind: "failure",
      key: `failure:${job.id}`,
      jobId: job.id,
      text: failureText(job.snapshot),
      connectionId: isTaskKind(job.kind) && job.kind !== "ingest" ? job.connectionId : null,
    });
  }

  const last = turns.at(-1);
  const quickReplies =
    last?.kind === "block" && last.block.type === "text" && last.block.quickReplies && !running
      ? {
          key: last.key,
          options: last.block.quickReplies,
          quote: quoteOf(last.block.markdown, last.block.id),
        }
      : null;
  return { turns: placeThinking(turns, thinking), quickReplies };
}

/** Когда пришёл последний блок задания: так «Думал · 12 с» замирает, а не тикает дальше. */
function lastBlockAt(job: JobSnapshot): number | undefined {
  const times = job.blocks.map((block) => (block.at ? num(block.at) : 0)).filter((ms) => ms > 0);
  return times.length > 0 ? Math.max(...times) : undefined;
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
