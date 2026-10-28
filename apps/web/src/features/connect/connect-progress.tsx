import { Banner, Button, SkeletonText } from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { queryKeys } from "@/api/queries";
import type { Job } from "@/api/types";
import { attachConnectionThread } from "@/features/chat/connection-api";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";
import { ProgressWall } from "@/widgets/progress-wall/progress-wall";
import { WallMascot } from "./wall-mascot";

/** Старая ссылка на job лишь восстанавливает canonical conversation; новое задание не создаётся. */
export function ConnectProgress({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const resolved = useQuery({
    queryKey: ["jobs", jobId, "conversation"],
    queryFn: async ({ signal }) => {
      const job = await call(
        api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } }, signal }),
      );
      // INGEST задаёт native-вопросы таблицы и не читает conversational inbox.
      if (job.kind === "ingest") return { kind: "ingest" as const, job };
      if (!job.connectionId) throw new Error("job_without_connection");
      return {
        kind: "conversation" as const,
        thread: await attachConnectionThread(job.connectionId, jobId, signal),
      };
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  useEffect(() => {
    if (resolved.data?.kind === "conversation")
      void navigate({ to: "/chat", search: { thread: resolved.data.thread.id }, replace: true });
  }, [navigate, resolved.data]);
  if (resolved.data?.kind === "ingest") return <IngestWall key={jobId} job={resolved.data.job} />;
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-col flex-col gap-4 px-safe-4 pt-safe-6 xs:px-safe-5">
      {resolved.isError ? (
        <div className="flex flex-col items-start gap-3">
          <Banner tone="warning" icon="warning" live>
            {humanMessage(resolved.error)}
          </Banner>
          <Button variant="secondary" asChild>
            <Link to="/chat">Открыть чаты</Link>
          </Button>
        </div>
      ) : (
        <SkeletonText lines={2} label="Открываю чат подключения" />
      )}
    </main>
  );
}

/** Legacy INGEST сохраняет native answer flow; прочие подключения живут в обычном чате. */
function IngestWall({ job }: { job: Job }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [finished, setFinished] = useState(false);
  const active = useActiveJob((state) => (state.job?.jobId === job.id ? state.job : null));
  useEffect(() => {
    if (finished || active) return;
    useActiveJob
      .getState()
      .set({ jobId: job.id, title: "Добавляю документ", connectionId: job.connectionId ?? undefined });
  }, [job.id, job.connectionId, active, finished]);
  const finish = async () => {
    setFinished(true);
    jobManager.stop(job.id);
    if (useActiveJob.getState().job?.jobId === job.id) useActiveJob.getState().clear();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.spaces }),
      queryClient.invalidateQueries({ queryKey: queryKeys.today }),
    ]);
    await navigate({ to: "/" });
  };
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-col flex-col px-safe-4 pt-safe-6 xs:px-safe-5">
      <ProgressWall
        jobId={job.id}
        title={active?.title ?? "Добавляю документ"}
        spaceTitle={active?.spaceTitle}
        connectionId={job.connectionId ?? undefined}
        onMinimize={() => void navigate({ to: "/" })}
        onFinish={() => void finish()}
        onCancel={() => void jobManager.cancel(job.id)}
        onAskInChat={() => void navigate({ to: "/chat" })}
        renderMascot={(state, slot) => <WallMascot state={state} slot={slot} />}
      />
    </main>
  );
}
