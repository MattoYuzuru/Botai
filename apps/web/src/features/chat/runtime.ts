/**
 * Рабочая очередь чата в браузере: настоящие запросы к API вместо заглушек `Outbox`. Живёт вне компонентов,
 * поэтому сообщение уходит, даже если человек успел перейти на другую вкладку, и вернувшаяся сеть сама
 * запускает отправку. Привязывает ответившее задание к чату и поднимает его поток (`jobManager`).
 */
import { useSyncExternalStore } from "react";
import { api, call } from "@/api/client";
import { isUnauthorized } from "@/api/errors";
import { jobManager } from "@/features/jobs/job-manager";
import { useChatJobs } from "./chat-jobs";
import { Outbox, type OutboxDeps, type OutboxItem } from "./outbox";

let meCache: { id: string | null; at: number } | null = null;
const ME_TTL_MS = 30_000;

async function currentUserId(): Promise<string | null> {
  if (meCache && Date.now() - meCache.at < ME_TTL_MS) return meCache.id;
  try {
    const me = await call(api.GET("/api/v1/me"));
    meCache = { id: me.user.id, at: Date.now() };
  } catch (error) {
    if (!isUnauthorized(error)) throw error;
    meCache = { id: null, at: Date.now() };
  }
  return meCache.id;
}

const safeStorage = (): OutboxDeps["storage"] => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

export function createChatDeps(): OutboxDeps {
  return {
    now: () => Date.now(),
    online: () => navigator.onLine,
    currentUserId,
    createThread: (spaceId) =>
      call(api.POST("/api/v1/chat/threads", { body: { spaceId, title: null, purpose: "chat" } })),
    postMessage: (threadId, text) =>
      call(
        api.POST("/api/v1/chat/threads/{thread_id}/messages", {
          params: { path: { thread_id: threadId } },
          body: { text },
        }),
      ),
    listMessages: (threadId) =>
      call(
        api.GET("/api/v1/chat/threads/{thread_id}/messages", { params: { path: { thread_id: threadId } } }),
      ),
    storage: safeStorage(),
    setTimer: (run, ms) => window.setTimeout(run, ms),
    clearTimer: (handle) => window.clearTimeout(handle as number),
    newId: () =>
      typeof crypto?.randomUUID === "function" ? crypto.randomUUID() : `m-${Date.now()}-${Math.random()}`,
  };
}

export const outbox = new Outbox(createChatDeps());

// Ответ принят: задание привязывается к чату, и поток начинает приходить сразу.
outbox.on((event) => {
  if (event.type !== "sent" || !event.jobId) return;
  // Новое сообщение закрывает прошлый ответ: его поток и привязка больше не нужны.
  const previous = useChatJobs.getState().byThread[event.threadId];
  if (previous && previous.jobId !== event.jobId) jobManager.stop(previous.jobId);
  useChatJobs.getState().track(event.threadId, event.jobId);
  jobManager.start(event.jobId);
});

if (typeof window !== "undefined") {
  // Сеть вернулась — очередь уходит сама, без кнопки.
  window.addEventListener("online", () => {
    meCache = null;
    outbox.kick();
  });
  // Сообщение, не ушедшее до перезагрузки страницы, уходит при старте.
  if (outbox.snapshot.length > 0) queueMicrotask(() => outbox.kick());
}

/** Очередь отправки: сообщения, которые ещё не дошли до сервера. */
export function useOutbox(): readonly OutboxItem[] {
  return useSyncExternalStore(
    outbox.subscribe,
    () => outbox.snapshot,
    () => outbox.snapshot,
  );
}

/** Выход из аккаунта: очередь и привязки заданий стираются. */
export function resetChatRuntime(): void {
  outbox.clear();
  useChatJobs.getState().clearAll();
  meCache = null;
}
