import type { Message } from "@/api/types";
import { type ParsedBlock, parseBlock } from "@/features/jobs/blocks";
import {
  initialJob,
  isTerminal,
  type JobSnapshot,
  type StepSummary,
  thinkingSteps,
} from "@/features/jobs/job-state";
import type { ThreadRuntime } from "./connection-api";
import { userText } from "./timeline";

export type ConnectTurn =
  | { kind: "user"; key: string; text: string; at: number }
  | { kind: "block"; key: string; jobId: string | null; block: ParsedBlock }
  | {
      kind: "thinking";
      key: string;
      jobId: string;
      steps: StepSummary[];
      running: boolean;
      finishedAt?: number;
    };

export type ConnectionEcho = {
  id: string;
  text: string;
  at: number;
  precedingKeys: readonly string[];
  message: Message | null;
};

const stamp = (at: string | null | undefined) => (at ? Date.parse(at) : null);
export const interactionKey = (jobId: string, blockId: string) => `${jobId}:${blockId}`;

/** Runtime восстанавливает связь после reload; SSE добавляет свежие blocks и answer state. */
export function connectionSnapshot(
  runtime: ThreadRuntime | undefined,
  live: JobSnapshot | undefined,
): JobSnapshot | null {
  const saved = runtime?.activeJob;
  if (!saved) return null;
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

/**
 * Durable turns идут в серверном порядке. SSE дополняет хвост, но не создаёт второй job bucket:
 * каждый block имеет один key до/после persistence, ответ пользователя не переносит вопрос.
 */
export function connectionTurns(
  messages: readonly Message[],
  job: JobSnapshot | null,
  runtime: ThreadRuntime,
  echoes: readonly ConnectionEcho[] = [],
): ConnectTurn[] {
  const turns: ConnectTurn[] = [];
  const rows = new Map<string, number>();
  const thoughts = new Map<string, Map<string, ParsedBlock>>();
  const lastDurableAt = new Map<string, number>();
  const pending = new Set(runtime.pending.map((item) => interactionKey(item.jobId, item.blockId)));
  const add = (block: ParsedBlock, jobId: string | null, fallback: string, live = false) => {
    if (block.type === "thinking") {
      if (!jobId) return;
      let group = thoughts.get(jobId);
      if (!group) {
        group = new Map();
        thoughts.set(jobId, group);
        rows.set(`thinking:${jobId}`, turns.length);
        turns.push({ kind: "thinking", key: `thinking:${jobId}`, jobId, steps: [], running: false });
      }
      group.set(block.id, block);
      return;
    }
    const key = `block:${jobId ?? fallback}:${block.id}`;
    const existing = rows.get(key);
    if (existing !== undefined) {
      // Only an active block may replace its durable, ticket-free browser placeholder.
      if (live && jobId && pending.has(interactionKey(jobId, block.id)))
        turns[existing] = { kind: "block", key, jobId, block };
      return;
    }
    rows.set(key, turns.length);
    turns.push({ kind: "block", key, jobId, block });
  };
  for (const message of messages) {
    const at = stamp(message.createdAt);
    if (message.jobId && at !== null && Number.isFinite(at)) {
      lastDurableAt.set(message.jobId, Math.max(lastDurableAt.get(message.jobId) ?? at, at));
    }
    if (message.role === "user") {
      turns.push({
        kind: "user",
        key: `message:${echoes.find((echo) => echo.message?.id === message.id)?.id ?? message.id}`,
        text: userText(message),
        at: stamp(message.createdAt) ?? 0,
      });
      continue;
    }
    for (const raw of message.blocks) {
      const block = parseBlock(raw);
      if (block) add(block, message.jobId, message.id);
    }
  }
  if (job) for (const block of job.blocks) add(block, job.jobId, job.jobId, true);
  for (const [jobId, blocks] of thoughts) {
    const index = rows.get(`thinking:${jobId}`);
    if (index === undefined) continue;
    const active = job?.jobId === jobId ? job : null;
    const snapshot = { ...initialJob(jobId), blocks: [...blocks.values()] };
    // У прошлых jobs нет runtime.finishedAt: фиксируем фактический видимый интервал истории.
    let finishedAt: number | undefined;
    if (active && isTerminal(active.state)) finishedAt = stamp(runtime.activeJob?.finishedAt) ?? undefined;
    else if (!active) finishedAt = lastDurableAt.get(jobId);
    turns[index] = {
      kind: "thinking",
      key: `thinking:${jobId}`,
      jobId,
      steps: thinkingSteps(snapshot),
      running: active !== null && !isTerminal(active.state),
      finishedAt,
    };
  }
  if (job && !isTerminal(job.state) && !thoughts.has(job.jobId)) {
    turns.push({
      kind: "thinking",
      key: `thinking:${job.jobId}`,
      jobId: job.jobId,
      steps: [{ id: "waiting", label: job.stage ?? "Думаю", state: "running", at: job.startedAt ?? 0 }],
      running: true,
    });
  }
  for (const echo of echoes) {
    const key = `message:${echo.id}`;
    const existing = turns.findIndex((turn) => turn.key === key);
    if (existing >= 0) turns.splice(existing, 1);
    // A thinking group keeps its key while new steps arrive, so it cannot
    // anchor a user's reply relative to future questions in that group.
    const preceding = new Set(echo.precedingKeys.filter((item) => !item.startsWith("thinking:")));
    const after = turns.findLastIndex((turn) => preceding.has(turn.key));
    turns.splice(after + 1, 0, {
      kind: "user",
      key,
      text: echo.message ? userText(echo.message) : echo.text,
      at: echo.message ? (stamp(echo.message.createdAt) ?? echo.at) : echo.at,
    });
  }
  return turns;
}
