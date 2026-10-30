/**
 * Контракт агент-хаба v3 (docs/plan/agent-hub-v3.md §3.2, §3.3, §3.6). Типы данных берутся из сгенерированной
 * схемы (`schema.gen.ts`, `agent-ui.gen.ts`: они не правятся руками, их пересобирает `scripts/gen.sh`); здесь
 * только тонкие алиасы под привычные имена, константы пределов и запросы.
 */
import type { AgentUiBlock } from "./agent-ui.gen";
import { NetworkError, parseProblem } from "./errors";
import type { components } from "./schema.gen";

type S = components["schemas"];

/** Сообщение треда; `clientMessageId` — идентификатор из очереди клиента (по нему сервер не дублирует отправку). */
export type MessageOut = S["MessageOut"] & { clientMessageId?: string | null };
export type JobOut = S["JobOut"];
export type PendingRef = S["PendingRef"];
export type DialogueReceipt = S["DialogueReceipt"];

// ---------- Треды (§3.2) ----------

export type ThreadOut = S["ThreadOut"];
export type ActiveTask = S["ActiveTask"];
export type ThreadAttention = NonNullable<ThreadOut["attention"]>;
export type TaskKind = ActiveTask["kind"];
export const TASK_KINDS: readonly TaskKind[] = ["connect", "repair", "sync", "ingest"];
export const isTaskKind = (kind: string): kind is TaskKind =>
  (TASK_KINDS as readonly string[]).includes(kind);

export type ThreadCreateIn = S["ThreadCreate"];

/** `text`: 0..8000 знаков; пустой текст допустим только вместе с вложениями (сервер заполняет его по умолчанию). */
export type MessageIn = Omit<S["MessageIn"], "text"> & { text?: string };
export type MessageAccepted = Omit<S["MessageAccepted"], "message"> & { message: MessageOut };
export type ThreadRuntime = S["ThreadRuntime"];

/** Машинные коды 409 при отправке сообщения (`contracts/conversation.md`). */
export type MessageConflictCode = "agent_busy" | "secret_in_chat" | "stale_target" | "request_reused";

// ---------- Вложения (§3.6) ----------

export type AttachmentOut = S["AttachmentOut"];
export type AttachmentKind = AttachmentOut["kind"];
export type IngestState = AttachmentOut["ingest"];

/** Предел одного файла (§3.6). */
export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
/** Не больше десяти вложений в сообщении (§3.2). */
export const MAX_MESSAGE_ATTACHMENTS = 10;

export const attachmentContentUrl = (id: string): string =>
  `/api/v1/attachments/${encodeURIComponent(id)}/content`;

// ---------- Блоки интерфейса агента, добавленные в v3 (§3.3) ----------

export type PlanBlock = Extract<AgentUiBlock, { type: "plan" }>;
export type PlanStepState = PlanBlock["steps"][number]["state"];
export type AttachmentBlock = Extract<AgentUiBlock, { type: "attachment" }>;
export type TaskBlock = Extract<AgentUiBlock, { type: "task" }>;

/** Быстрые ответы к тексту: до четырёх кнопок, каждая до 60 знаков. */
export const MAX_QUICK_REPLIES = 4;
export const MAX_QUICK_REPLY_CHARS = 60;

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
