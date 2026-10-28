import { toast } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { queryKeys } from "@/api/queries";
import { attachConnectionThread } from "@/features/chat/connection-api";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";

/** Берёт задание под наблюдение: поток открывается в фоне, а свёрнутый ход виден в шапке экранов. */
export function trackJob(job: {
  jobId: string;
  title: string;
  spaceTitle?: string;
  connectionId?: string;
  threadId?: string;
}) {
  useActiveJob.getState().set(job);
  jobManager.start(job.jobId);
}

export interface ReconnectTarget {
  connectionId: string;
  spaceId?: string;
  /** «ВШЭ»: для заголовка свёрнутого задания. */
  spaceTitle: string;
}

/**
 * «Войти снова»: ставит в очередь обновление подключения и открывает ход задания. Если вузу нужен пароль,
 * которого BotAI не хранит, задание остановится на вопросе, и человек ответит на него в потоке подключения
 * в том же чате источника. Это бизнес-действие, а не «повторить запрос»: повторы запросов идут в фоне.
 */
export function useReconnect() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: async (target: ReconnectTarget) => {
      const { jobId } = await call(
        api.POST("/api/v1/connections/{connection_id}/sync", {
          params: { path: { connection_id: target.connectionId } },
        }),
      );
      return { jobId, target };
    },
    onSuccess: async ({ jobId, target }) => {
      const tracked = {
        jobId,
        title: `Вхожу в ${target.spaceTitle}`,
        spaceTitle: target.spaceTitle,
        connectionId: target.connectionId,
      };
      trackJob(tracked);
      const thread = await attachConnectionThread(target.connectionId, jobId);
      trackJob({ ...tracked, threadId: thread.id });
      await queryClient.invalidateQueries({
        queryKey: target.spaceId ? queryKeys.connections(target.spaceId) : queryKeys.spaces,
      });
      await navigate({ to: "/chat", search: { thread: thread.id } });
    },
    onError: (error) =>
      toast("Не удалось начать вход", { tone: "warning", description: humanMessage(error) }),
  });
}
