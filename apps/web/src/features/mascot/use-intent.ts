import type { MascotIntent } from "@botai/design";
import { useNavigate } from "@tanstack/react-router";
import { useCallback } from "react";
import { CONNECT_DRAFT } from "@/features/chat/drafts";
import { useReconnect } from "@/features/data/use-reconnect";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { useActiveJob } from "@/features/jobs/active-job";

/**
 * Что делает нажатие на действие Вари. Renderer ничего не исполняет: он отдаёт наверх типизированное намерение
 * (mascot.md §1), а разрешает его приложение — переходом на экран или бизнес-действием. Кнопки «Повторить» среди
 * намерений нет.
 */
export function useMascotIntent(): (intent: MascotIntent) => void {
  const navigate = useNavigate();
  const directory = useSpaceDirectory();
  const reconnect = useReconnect();
  const activeJob = useActiveJob((state) => state.job);

  return useCallback(
    (intent: MascotIntent) => {
      switch (intent.kind) {
        case "reauth": {
          // «Войти снова»: первое подключение, которому нужен вход; ход заданий откроется сам.
          for (const space of directory.spaces) {
            const connection = space.connections.find((item) => item.status === "needs_reauth");
            if (connection) {
              reconnect.mutate({
                connectionId: connection.id,
                spaceId: space.id,
                spaceTitle: space.shortTitle,
              });
              return;
            }
          }
          void navigate({ to: "/study" });
          return;
        }
        case "connect":
          void navigate({ to: "/chat", search: { draft: CONNECT_DRAFT } });
          return;
        case "open-progress":
          if (activeJob?.threadId) void navigate({ to: "/chat", search: { thread: activeJob.threadId } });
          else void navigate({ to: "/chat" });
          return;
        case "open-chat":
          void navigate({ to: "/chat" });
          return;
        case "open-schedule":
          void navigate({ to: "/schedule" });
          return;
        case "open-deadline":
          void navigate({ to: "/tasks" });
          return;
        case "open-source":
          void navigate({ to: "/study" });
          return;
      }
    },
    [navigate, directory.spaces, reconnect, activeJob],
  );
}
