import { Banner } from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { api, call } from "@/api/client";
import { queryKeys } from "@/api/queries";
import { useReconnect } from "@/features/data/use-reconnect";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";
import { WallMascot } from "./wall-mascot";

/** Экран хода подключения: `ProgressWall` на потоке задания. Свёртывание ничего не обрывает. */
export function ConnectProgress({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const active = useActiveJob((state) => (state.job?.jobId === jobId ? state.job : null));
  const clear = useActiveJob((state) => state.clear);
  const reconnect = useReconnect();
  const [finished, setFinished] = useState(false);
  const metadata = useQuery({
    queryKey: ["jobs", jobId, "connection"],
    queryFn: async ({ signal }) => {
      const job = await call(
        api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } }, signal }),
      );
      return { connectionId: job.connectionId };
    },
    enabled: !finished && !active?.connectionId,
    staleTime: Number.POSITIVE_INFINITY,
  });
  const connectionId = active?.connectionId ?? metadata.data?.connectionId ?? undefined;
  useEffect(() => {
    if (finished || !connectionId || active?.connectionId) return;
    useActiveJob.getState().set({
      jobId,
      title: active?.title ?? "Подключение вуза",
      spaceTitle: active?.spaceTitle,
      connectionId,
    });
  }, [jobId, active, connectionId, finished]);
  const openBrowser = useCallback(
    async (signal: AbortSignal) => {
      if (!connectionId) return;
      // Одноразовый ticket из HTTP не сохраняется: экран придёт отдельным свежим SSE-блоком.
      await call(
        api.POST("/api/v1/connections/{connection_id}/login-session", {
          params: { path: { connection_id: connectionId } },
          signal,
        }),
      );
    },
    [connectionId],
  );

  const finish = async () => {
    setFinished(true);
    // Итог прочитан: забываем задание и обновляем всё, что подключение могло изменить.
    jobManager.stop(jobId);
    clear();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.spaces }),
      queryClient.invalidateQueries({ queryKey: queryKeys.today }),
    ]);
    await navigate({ to: "/" });
  };

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-col flex-col px-safe-4 pt-safe-6 xs:px-safe-5">
      {metadata.isError && !connectionId && (
        <Banner tone="warning" icon="warning" live>
          Не удалось подготовить вход через браузер. Пока можно ответить в полях BotAI.
        </Banner>
      )}
      <ProgressWall
        key={jobId}
        jobId={jobId}
        title={active?.title ?? "Подключение вуза"}
        spaceTitle={active?.spaceTitle}
        connectionId={connectionId}
        onOpenBrowser={connectionId ? openBrowser : undefined}
        onLoginAgain={
          connectionId
            ? () =>
                reconnect.mutate({
                  connectionId,
                  spaceTitle: active?.spaceTitle ?? "личный кабинет",
                })
            : undefined
        }
        restarting={reconnect.isPending}
        onMinimize={() => void navigate({ to: "/" })}
        onFinish={() => void finish()}
        onCancel={() => void jobManager.cancel(jobId)}
        onAskInChat={() => void navigate({ to: "/chat" })}
        renderMascot={(state, slot) => <WallMascot state={state} slot={slot} />}
      />
    </main>
  );
}
