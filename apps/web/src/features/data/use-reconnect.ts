import { toast } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { queryKeys } from "@/api/queries";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";

export interface ReconnectTarget {
  connectionId: string;
  spaceId: string;
  /** «ВШЭ»: для заголовка свёрнутого задания. */
  spaceTitle: string;
}

/**
 * «Войти снова»: ставит в очередь обновление подключения и открывает ход задания. Если вузу нужен пароль,
 * которого BotAI не хранит, задание остановится на вопросе, и человек ответит на него в потоке подключения
 * (`/connect/<задание>`). Это бизнес-действие, а не «повторить запрос»: повторы запросов идут в фоне.
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
      useActiveJob.getState().set({
        jobId,
        title: `Вхожу в ${target.spaceTitle}`,
        spaceTitle: target.spaceTitle,
        connectionId: target.connectionId,
      });
      jobManager.start(jobId);
      await queryClient.invalidateQueries({ queryKey: queryKeys.connections(target.spaceId) });
      await navigate({ to: "/connect/$jobId", params: { jobId } });
    },
    onError: (error) =>
      toast("Не удалось начать вход", { tone: "warning", description: humanMessage(error) }),
  });
}
