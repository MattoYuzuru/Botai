import type { Message } from "@/api/types";
import {
  type MessageAccepted,
  type MessageIn,
  type ThreadCreateIn,
  type ThreadOut,
  type ThreadRuntime,
  v3,
} from "@/api/v3";

const base = "/api/v1/chat/threads";
const thread = (id: string) => `${base}/${encodeURIComponent(id)}`;

/** Эндпоинты чата агент-хаба v3 (§3.2). Один агент на тред: ни `purpose`, ни отдельного разговора подключения. */
export const chatApi = {
  threads: (signal?: AbortSignal) => v3<ThreadOut[]>("GET", base, { signal, cache: "no-store" }),
  createThread: (body: ThreadCreateIn) => v3<ThreadOut>("POST", base, { body }),
  renameThread: (id: string, title: string) => v3<ThreadOut>("PATCH", thread(id), { body: { title } }),
  deleteThread: (id: string) => v3<void>("DELETE", thread(id)),
  /** Прочитано до `upTo` (`createdAt` последнего показанного сообщения): пришедшее позже остаётся непрочитанным. */
  markRead: (id: string, upTo?: string | null) =>
    v3<void>("POST", `${thread(id)}/read`, upTo ? { body: { upTo } } : {}),
  messages: (id: string, signal?: AbortSignal) =>
    v3<Message[]>("GET", `${thread(id)}/messages`, { signal, cache: "no-store" }),
  postMessage: (id: string, body: MessageIn) =>
    v3<MessageAccepted>("POST", `${thread(id)}/messages`, { body }),
  runtime: (id: string, signal?: AbortSignal) =>
    v3<ThreadRuntime>("GET", `${thread(id)}/runtime`, { signal, cache: "no-store" }),
};
