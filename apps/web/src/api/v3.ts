/**
 * Контракт агент-хаба v3 (docs/plan/agent-hub-v3.md §3.2, §3.3, §3.6), объявленный вручную: схема бэкенда
 * меняется параллельно в других потоках, а `schema.gen.ts` и `agent-ui.gen.ts` не правятся руками. Когда
 * интеграция пересоберёт сгенерированные типы, эти объявления заменяются на них без изменения экранов.
 */
import { NetworkError, parseProblem } from "./errors";
import type { components } from "./schema.gen";

type S = components["schemas"];

export type MessageOut = S["MessageOut"];
export type JobOut = S["JobOut"];
export type PendingRef = S["PendingRef"];
export type DialogueReceipt = S["DialogueReceipt"];

// ---------- Треды (§3.2) ----------

export type ThreadAttention = "waiting_user" | "running" | "failed";
export type TaskKind = "connect" | "repair" | "sync" | "ingest";
export const TASK_KINDS: readonly TaskKind[] = ["connect", "repair", "sync", "ingest"];
export const isTaskKind = (kind: string): kind is TaskKind =>
  (TASK_KINDS as readonly string[]).includes(kind);

export interface ActiveTask {
  jobId: string;
  kind: TaskKind;
  connectionId: string | null;
  state: string;
  title: string;
}

export interface ThreadOut {
  id: string;
  spaceId: string | null;
  title: string;
  createdAt: string;
  lastMessageAt: string;
  /** Есть сообщение агента новее `last_read_at`. */
  unread: boolean;
  /** По активной задаче или ходу агента. */
  attention: ThreadAttention | null;
  activeTask: ActiveTask | null;
  /** Начало последнего текстового сообщения, не длиннее 120 знаков. */
  preview: string | null;
}

export interface ThreadCreateIn {
  spaceId?: string | null;
  title?: string | null;
  clientRequestId?: string;
}

export interface MessageIn {
  /** 0..8000 знаков; пустой текст допустим только вместе с вложениями. */
  text: string;
  attachmentIds?: string[];
  clientMessageId?: string;
  targetJobId?: string;
}

export interface MessageAccepted {
  message: MessageOut;
  jobId: string;
}

export interface ThreadRuntime {
  activeJob: JobOut | null;
  tasks: JobOut[];
  pending: PendingRef[];
  receipts: DialogueReceipt[];
}

// ---------- Вложения (§3.6) ----------

export type AttachmentKind = "image" | "document" | "audio" | "other";
export type IngestState = "none" | "queued" | "indexed" | "failed";

export interface AttachmentOut {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
  createdAt: string;
  documentId: string | null;
  ingest: IngestState;
}

/** Предел одного файла (§3.6). */
export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
/** Не больше десяти вложений в сообщении (§3.2). */
export const MAX_MESSAGE_ATTACHMENTS = 10;

export const attachmentContentUrl = (id: string): string =>
  `/api/v1/attachments/${encodeURIComponent(id)}/content`;

// ---------- Блоки интерфейса агента, добавленные в v3 (§3.3) ----------

export type PlanStepState = "pending" | "active" | "done" | "skipped";

export interface PlanBlock {
  type: "plan";
  id: string;
  at: string;
  title: string;
  steps: { id: string; title: string; state: PlanStepState }[];
}

export interface AttachmentBlock {
  type: "attachment";
  id: string;
  at: string;
  attachmentId: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
}

export interface TaskBlock {
  type: "task";
  id: string;
  at: string;
  jobId: string;
  kind: TaskKind;
  title: string;
  state: string;
  threadId: string;
  connectionId?: string;
}

/** Быстрые ответы к тексту: до четырёх кнопок, каждая до 60 знаков. */
export const MAX_QUICK_REPLIES = 4;
export const MAX_QUICK_REPLY_CHARS = 60;

// ---------- Расширения источников, которых ещё нет в сгенерированной схеме (§3.5) ----------

/** Поля `ConnectionOut`, которые добавляет WS4: UI-адрес, тред источника и активная задача. */
export interface ConnectionExtras {
  entryUrl?: string | null;
  threadId?: string | null;
  activeTask?: ActiveTask | null;
}

// ---------- Запросы ----------

type Query = Record<string, string | number | boolean | undefined>;

export interface V3Options {
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
  cache?: RequestCache;
}

function urlFor(path: string, query?: Query): string {
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  const url = new URL(path, origin);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }
  return url.toString();
}

async function problemBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** Запрос к API v3: JSON туда и обратно, ошибка — `ApiError` из problem+json, обрыв связи — `NetworkError`. */
export async function v3<T>(method: string, path: string, options: V3Options = {}): Promise<T> {
  let response: Response;
  try {
    response = await globalThis.fetch(urlFor(path, options.query), {
      method,
      credentials: "include",
      cache: options.cache,
      signal: options.signal,
      headers:
        options.body === undefined
          ? { accept: "application/json" }
          : { accept: "application/json", "content-type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw new NetworkError(cause);
  }
  if (!response.ok) throw parseProblem(response.status, await problemBody(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface UploadOptions {
  threadId?: string | null;
  /** Доля отправленного от 0 до 1; браузер сообщает её только пока идёт загрузка. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/**
 * Загрузка файла (`POST /api/v1/attachments`, multipart). `fetch` не сообщает прогресс отправки, поэтому здесь
 * `XMLHttpRequest`: чип вложения показывает настоящую долю, а не придуманную.
 */
export function uploadAttachment(file: File, options: UploadOptions = {}): Promise<AttachmentOut> {
  return new Promise<AttachmentOut>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", urlFor("/api/v1/attachments"));
    request.withCredentials = true;
    request.responseType = "text";
    request.setRequestHeader("accept", "application/json");
    const abort = () => request.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    const done = () => options.signal?.removeEventListener("abort", abort);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) options.onProgress?.(event.loaded / event.total);
    };
    request.onload = () => {
      done();
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(request.responseText);
      } catch {
        /* тело не JSON */
      }
      if (request.status >= 200 && request.status < 300 && parsed) resolve(parsed as AttachmentOut);
      else reject(parseProblem(request.status, parsed));
    };
    request.onerror = () => {
      done();
      reject(new NetworkError());
    };
    request.onabort = () => {
      done();
      reject(new DOMException("Загрузка отменена", "AbortError"));
    };
    const form = new FormData();
    form.set("file", file, file.name);
    if (options.threadId) form.set("threadId", options.threadId);
    request.send(form);
  });
}
