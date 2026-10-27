import { toast } from "@botai/design";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { queryKeys } from "@/api/queries";

/**
 * «Войти снова»: ставит в очередь обновление подключения. Если вузу нужен пароль, который BotAI не хранит,
 * задание остановится на вопросе, и человек ответит на него в потоке подключения (`/connect/<задание>`).
 * Это бизнес-действие, а не «повторить запрос»: повторы запросов идут в фоне.
 */
export function useReconnect(spaceId?: string) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (connectionId: string) =>
      call(
        api.POST("/api/v1/connections/{connection_id}/sync", {
          params: { path: { connection_id: connectionId } },
        }),
      ),
    onSuccess: async ({ jobId }) => {
      if (spaceId) await queryClient.invalidateQueries({ queryKey: queryKeys.connections(spaceId) });
      await navigate({ href: `/connect/${encodeURIComponent(jobId)}` });
    },
    onError: (error) =>
      toast("Не удалось начать вход", { tone: "warning", description: humanMessage(error) }),
  });
}
