import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { ThreadOut } from "@/api/v3";
import { chatApi } from "./api";
import { chatKeys } from "./queries";

function visible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/**
 * Отметка прочтения: тред открыт, лента прокручена до конца и вкладка на виду — `POST /read`, точка
 * непрочитанного гаснет сразу (оптимистично), а новое сообщение агента зажжёт её снова.
 */
export function useMarkRead(threadId: string | null, unread: boolean, atBottom: boolean, ready: boolean) {
  const queryClient = useQueryClient();
  const [seen, setSeen] = useState(visible);
  useEffect(() => {
    const onChange = () => setSeen(visible());
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  useEffect(() => {
    if (!threadId || !unread || !atBottom || !ready || !seen) return;
    queryClient.setQueryData<ThreadOut[]>(chatKeys.threads, (rows) =>
      rows?.map((thread) => (thread.id === threadId ? { ...thread, unread: false } : thread)),
    );
    void chatApi.markRead(threadId).catch(() => {
      // Не отметилось: список обновится сам и точка вернётся, пока человек не дочитает ещё раз.
      void queryClient.invalidateQueries({ queryKey: chatKeys.threads });
    });
  }, [threadId, unread, atBottom, ready, seen, queryClient]);
}
