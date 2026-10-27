import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { queryKeys } from "@/api/queries";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";

/** Экран хода подключения: `ProgressWall` на потоке задания. Свёртывание ничего не обрывает. */
export function ConnectProgress({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const active = useActiveJob((state) => (state.job?.jobId === jobId ? state.job : null));
  const clear = useActiveJob((state) => state.clear);

  const finish = async () => {
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
      <ProgressWall
        jobId={jobId}
        title={active?.title ?? "Подключение вуза"}
        spaceTitle={active?.spaceTitle}
        onMinimize={() => void navigate({ to: "/" })}
        onFinish={() => void finish()}
        onCancel={() => void jobManager.cancel(jobId)}
        onAskInChat={() => void navigate({ to: "/chat" })}
      />
    </main>
  );
}
