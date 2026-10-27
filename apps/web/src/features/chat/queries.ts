import { queryOptions } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import type { components } from "@/api/schema.gen";

export type MessageAccepted = components["schemas"]["MessageAccepted"];

/**
 * Запросы чата. Обновление идёт в фоне; кнопок «Обновить» нет. Сообщения и шкала контекста обновляются,
 * когда задание ответа завершилось (`invalidate` в экране), а не по таймеру.
 */
export const chatKeys = {
  all: ["chat"] as const,
  threads: ["chat", "threads"] as const,
  messages: (threadId: string) => ["chat", "threads", threadId, "messages"] as const,
  context: (threadId: string) => ["chat", "threads", threadId, "context"] as const,
};

export const threadsQuery = queryOptions({
  queryKey: chatKeys.threads,
  queryFn: () => call(api.GET("/api/v1/chat/threads")),
  staleTime: 15_000,
});

export const messagesQuery = (threadId: string) =>
  queryOptions({
    queryKey: chatKeys.messages(threadId),
    queryFn: () =>
      call(
        api.GET("/api/v1/chat/threads/{thread_id}/messages", { params: { path: { thread_id: threadId } } }),
      ),
    staleTime: 10_000,
  });

export const contextQuery = (threadId: string) =>
  queryOptions({
    queryKey: chatKeys.context(threadId),
    queryFn: () =>
      call(
        api.GET("/api/v1/chat/threads/{thread_id}/context", { params: { path: { thread_id: threadId } } }),
      ),
    staleTime: 10_000,
  });
