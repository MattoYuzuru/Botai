/**
 * Состояние чата мок-режима: треды, сообщения, задания, вложения. Форма — по контракту агент-хаба v3
 * (`docs/v4/05-implementation-plan.md` §3.2, §3.6). «Агент» мока и обработчики лежат рядом (`chat-agent.ts`, `chat-handlers.ts`).
 */
import type { MessageOut, ThreadOut } from "@/api/v3";
import { isTaskKind } from "@/api/v3";
import type { KnownBlock } from "@/features/jobs/blocks";
import type { MockDb } from "./db";
import { ulid } from "./db";
import type { JobSim } from "./job-sim";

export type Message = MessageOut;
export type Source = { title: string; url: string; fetchedAt: string; stale?: boolean };

export interface MockThread {
  id: string;
  spaceId: string | null;
  title: string;
  createdAt: string;
  lastMessageAt: string;
  /** Сколько первых сообщений прочитано: всё, что агент написал дальше, — непрочитанное. */
  readCount: number;
}

export interface MockAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "document" | "audio" | "other";
  createdAt: string;
  threadId: string | null;
  /** Привязано к отправленному сообщению: такое удалить нельзя. */
  attached: boolean;
}

export interface ChatState {
  threads: MockThread[];
  messages: Record<string, Message[]>;
  /** Тред → задания, запущенные в нём: ходы агента и фоновые задачи, по порядку. */
  jobs: Record<string, string[]>;
  /** Тред → ход агента, который сейчас отвечает. */
  busy: Record<string, string>;
  /** Что записал последний завершённый ход: для шкалы контекста. */
  last: Record<string, { data: number; documents: number; sources: Source[] }>;
  requests: Record<string, string>;
  accepted: Record<string, { message: Message; jobId: string }>;
  attachments: Record<string, MockAttachment>;
}

export const DEFAULT_TITLE = "Новый чат";
const STORAGE_KEY = "botai.mock.chat";
const states = new WeakMap<MockDb, ChatState>();
const bytes = new WeakMap<MockDb, Map<string, { data: ArrayBuffer; mime: string }>>();

export const attachmentBytes = (db: MockDb) => {
  let map = bytes.get(db);
  if (!map) {
    map = new Map();
    bytes.set(db, map);
  }
  return map;
};

/** Блок в виде, в котором сервер отдаёт его в сообщении. */
export const asStored = (block: KnownBlock): Message["blocks"][number] =>
  block as unknown as Message["blocks"][number];

export const textBlock = (
  markdown: string,
  at: string,
  sources?: Source[],
  quickReplies?: string[],
): KnownBlock => ({
  type: "text",
  id: ulid(),
  at,
  markdown,
  ...(sources && sources.length > 0 ? { sources } : {}),
  ...(quickReplies && quickReplies.length > 0 ? { quickReplies } : {}),
});

function empty(): ChatState {
  return {
    threads: [],
    messages: {},
    jobs: {},
    busy: {},
    last: {},
    requests: {},
    accepted: {},
    attachments: {},
  };
}

/** Тред с готовой перепиской: показывает историю, внутренние ссылки и источники одного подключения. */
function seed(db: MockDb): ChatState {
  const state = empty();
  const connection = db.connections.find((item) => item.kind === "portal") ?? db.connections[0];
  if (!connection) return state;
  const at = (minutesAgo: number) => new Date(db.now() - minutesAgo * 60_000).toISOString();
  const thread: MockThread = {
    id: ulid(),
    spaceId: null,
    title: "Что сегодня по расписанию?",
    createdAt: at(180),
    lastMessageAt: at(176),
    readCount: 2,
  };
  const fetchedAt = at(12);
  const sources: Source[] = [
    { title: "Расписание — личный кабинет", url: `botai://connections/${connection.id}`, fetchedAt },
    { title: "Расписание на сайте вуза", url: "https://lk.fakeuni.test/timetable", fetchedAt },
  ];
  state.threads.push(thread);
  state.messages[thread.id] = [
    {
      id: ulid(),
      role: "user",
      blocks: [asStored(textBlock("Что сегодня по расписанию?", at(180)))],
      jobId: null,
      createdAt: at(180),
    },
    {
      id: ulid(),
      role: "agent",
      blocks: [
        asStored(
          textBlock(
            "Сегодня три пары. Первая — **Алгоритмы и структуры данных** в 9:30, затем «Базы данных».",
            at(176),
            sources,
          ),
        ),
      ],
      jobId: ulid(),
      createdAt: at(176),
    },
  ];
  state.last[thread.id] = { data: 4_200, documents: 0, sources };
  return state;
}

export function loadState(db: MockDb, persist: boolean): ChatState {
  let state = states.get(db);
  if (state) return state;
  if (persist) {
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY);
      if (raw) state = { ...empty(), ...(JSON.parse(raw) as ChatState), busy: {} };
    } catch {
      /* хранилище недоступно */
    }
  }
  state ??= seed(db);
  states.set(db, state);
  return state;
}

export function saveState(db: MockDb, persist: boolean): void {
  if (!persist) return;
  try {
    const state = states.get(db);
    if (state) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* хранилище недоступно */
  }
}

/** Для тестов: сбросить состояние чата базы. */
export function resetChatState(db: MockDb): void {
  states.delete(db);
  bytes.delete(db);
}

const textOfBlocks = (blocks: readonly Record<string, unknown>[]): string =>
  blocks
    .map((block) => (block.type === "text" && typeof block.markdown === "string" ? block.markdown : ""))
    .filter(Boolean)
    .join(" ");

/** Тред глазами клиента: непрочитанное, внимание и активная задача считаются по заданиям треда. */
export function threadOut(db: MockDb, state: ChatState, thread: MockThread): ThreadOut {
  const messages = state.messages[thread.id] ?? [];
  const sims = (state.jobs[thread.id] ?? [])
    .map((id) => db.sims.get(id))
    .filter((sim): sim is JobSim => Boolean(sim));
  const live = sims.filter((sim) => !sim.terminal);
  const task = live.findLast((sim) => isTaskKind(sim.kind));
  const lastText = messages.findLast((message) => textOfBlocks(message.blocks) !== "");
  const preview = lastText ? textOfBlocks(lastText.blocks).replace(/\s+/g, " ").trim() : "";
  const latest = sims.at(-1);
  return {
    id: thread.id,
    spaceId: thread.spaceId,
    title: thread.title,
    createdAt: thread.createdAt,
    lastMessageAt: thread.lastMessageAt,
    unread: messages.slice(thread.readCount).some((message) => message.role === "agent"),
    attention: live.some((sim) => sim.state === "waiting_user")
      ? "waiting_user"
      : live.length > 0
        ? "running"
        : latest?.state === "failed"
          ? "failed"
          : null,
    activeTask:
      task && isTaskKind(task.kind)
        ? {
            jobId: task.jobId,
            kind: task.kind,
            connectionId: task.connectionId,
            state: task.state,
            title:
              db.connections.find((item) => item.id === task.connectionId)?.title ?? "Подключение источника",
          }
        : null,
    preview: preview ? (preview.length > 120 ? `${preview.slice(0, 119)}…` : preview) : null,
  };
}
