import { queryOptions } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import type { components } from "@/api/schema.gen";
import type { Thread } from "@/api/types";

export type ConnectContext = components["schemas"]["ConnectContext"];
export type ThreadRuntime = components["schemas"]["ThreadRuntime"];

export const threadRuntimeKey = (threadId: string) => ["chat", "threads", threadId, "runtime"] as const;

export const threadRuntimeQuery = (threadId: string) =>
  queryOptions({
    queryKey: threadRuntimeKey(threadId),
    queryFn: ({ signal }) =>
      call(
        api.GET("/api/v1/chat/threads/{thread_id}/runtime", {
          params: { path: { thread_id: threadId } },
          signal,
        }),
      ),
    staleTime: 0,
  });

export function attachConnectionThread(
  connectionId: string,
  jobId?: string,
  signal?: AbortSignal,
): Promise<Thread> {
  return call(
    api.POST("/api/v1/connections/{connection_id}/conversation", {
      params: { path: { connection_id: connectionId } },
      body: jobId ? { jobId } : {},
      signal,
    }),
  );
}

export function startConnectThread(input: {
  clientRequestId: string;
  spaceId?: string | null;
  connectContext?: ConnectContext;
}): Promise<Thread> {
  const body = { ...input, spaceId: input.spaceId ?? null, title: null, purpose: "connect" as const };
  return call(api.POST("/api/v1/chat/threads", { body }));
}

/** CONNECT text не проходит через persisted Outbox; target и id защищают от stale/retry race. */
export async function sendConnectText(
  threadId: string,
  text: string,
  clientMessageId: string,
  targetJobId?: string,
  signal?: AbortSignal,
) {
  const body = { text, clientMessageId, targetJobId };
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, 15_000);
  try {
    return await call(
      api.POST("/api/v1/chat/threads/{thread_id}/messages", {
        params: { path: { thread_id: threadId } },
        body,
        signal: controller.signal,
      }),
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
