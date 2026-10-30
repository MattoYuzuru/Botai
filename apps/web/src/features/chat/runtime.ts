/**
 * Рабочая очередь чата в браузере: настоящие запросы к API вместо заглушек `Outbox`. Живёт вне компонентов,
 * поэтому сообщение уходит, даже если человек успел перейти на другую вкладку, и вернувшаяся сеть сама
 * запускает отправку. Привязывает ответившее задание к чату и поднимает его поток (`jobManager`).
 */
import { useSyncExternalStore } from "react";
import { api, call } from "@/api/client";
import { isUnauthorized } from "@/api/errors";
import { onDataReset, onQueueReset } from "@/features/auth/data-reset";
import { jobManager } from "@/features/jobs/job-manager";
import { clientId } from "@/lib/client-id";
import { chatApi } from "./api";
import { useChatJobs } from "./chat-jobs";
import { Outbox, type OutboxDeps, type OutboxItem } from "./outbox";

async function currentUserId(): Promise<string | null> {
  try {
    // Offline-снимок личности подходит для просмотра, но не разрешает отправку очереди.
    const me = await call(api.GET("/api/v1/me", { cache: "no-store" }));
    return me.user.id;
  } catch (error) {
    if (!isUnauthorized(error)) throw error;
    return null;
  }
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
    createThread: (spaceId, clientRequestId) =>
      chatApi.createThread({ spaceId, title: null, clientRequestId }),
    postMessage: (threadId, post) => chatApi.postMessage(threadId, post),
    listMessages: (threadId) => chatApi.messages(threadId),
    storage: safeStorage(),
    setTimer: (run, ms) => window.setTimeout(run, ms),
    clearTimer: (handle) => window.clearTimeout(handle as number),
    // ULID: он же уходит на сервер как `clientMessageId` и `clientRequestId`.
    newId: clientId,
  };
}

export const outbox = new Outbox(createChatDeps());
onQueueReset(({ userId, resetAt }) => {
  outbox.discardBefore(userId, resetAt);
});
onDataReset(({ userId, resetAt }) => {
  outbox.discardBefore(userId, resetAt);
  useChatJobs.getState().clearAll();
  jobManager.stopAll();
});

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
}
