import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { ThreadOut } from "@/api/v3";
import { compareInstants } from "@/lib/instants";
import { chatApi } from "./api";
import { chatKeys } from "./queries";

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/**
 * Отметка прочтения: тред открыт, лента прокручена до конца и вкладка на виду — `POST /read`, точка
 * непрочитанного гаснет сразу (оптимистично), а новое сообщение агента зажжёт её снова.
 */
export function useMarkRead(
  threadId: string | null,
  unread: boolean,
  atBottom: boolean,
  ready: boolean,
  upTo: string | null = null,
) {
  const queryClient = useQueryClient();
  const [seen, setSeen] = useState(visible);
  const pending = useRef(new Set<string>());
  const acknowledged = useRef(new Map<string, string | null>());
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const onChange = () => setSeen(visible());
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: retry a newer watermark when the in-flight read completes
  useEffect(() => {
    if (!threadId || !unread || !atBottom || !ready || !seen) return;
    const clearSeen = () => {
      queryClient.setQueryData<ThreadOut[]>(chatKeys.threads, (rows) =>
        rows?.map((thread) =>
          thread.id === threadId && (!upTo || compareInstants(thread.lastMessageAt, upTo) <= 0)
            ? { ...thread, unread: false }
            : thread,
        ),
      );
    };
    // A list fetched before POST /read may arrive afterwards. Do not send the same read again.
    if (acknowledged.current.has(threadId) && acknowledged.current.get(threadId) === upTo) {
      clearSeen();
      return;
    }
    if (pending.current.has(threadId)) return;
    pending.current.add(threadId);
    clearSeen();
    void chatApi.markRead(threadId, upTo).then(
      async () => {
        acknowledged.current.set(threadId, upTo);
        // Only the list: messages/runtime must keep loading while this acknowledgement is saved.
        await queryClient.cancelQueries({ queryKey: chatKeys.threads, exact: true });
        clearSeen();
        pending.current.delete(threadId);
        setRevision((value) => value + 1);
      },
      () => {
        pending.current.delete(threadId);
        // A subsequent list response restores unread and allows a retry after a failed request.
        void queryClient.invalidateQueries({ queryKey: chatKeys.threads, exact: true });
      },
    );
  }, [threadId, unread, atBottom, ready, seen, upTo, queryClient, revision]);
}
