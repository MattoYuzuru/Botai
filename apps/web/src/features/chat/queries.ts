import { queryOptions } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import { chatApi } from "./api";

/** Как часто опрашивается список чатов: так человек узнаёт, что агент просит войти, не открывая нужный чат. */
export const THREADS_POLL_MS = 10_000;

/**
 * Запросы чата. Обновление идёт в фоне; кнопок «Обновить» нет. Сообщения и шкала контекста обновляются,
 * когда задание ответа продвинулось или завершилось (`invalidate` в экране), а не по таймеру.
 */
export const chatKeys = {
  all: ["chat"] as const,
  threads: ["chat", "threads"] as const,
  messages: (threadId: string) => ["chat", "threads", threadId, "messages"] as const,
  context: (threadId: string) => ["chat", "threads", threadId, "context"] as const,
  runtime: (threadId: string) => ["chat", "threads", threadId, "runtime"] as const,
};

export const threadsQuery = queryOptions({
  queryKey: chatKeys.threads,
  queryFn: ({ signal }) => chatApi.threads(signal),
  staleTime: 5_000,
  refetchInterval: THREADS_POLL_MS,
  refetchOnWindowFocus: true,
});

export const messagesQuery = (threadId: string) =>
  queryOptions({
    queryKey: chatKeys.messages(threadId),
    queryFn: ({ signal }) => chatApi.messages(threadId, signal),
    staleTime: 10_000,
  });

export const runtimeQuery = (threadId: string) =>
  queryOptions({
    queryKey: chatKeys.runtime(threadId),
    queryFn: ({ signal }) => chatApi.runtime(threadId, signal),
    staleTime: 0,
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
