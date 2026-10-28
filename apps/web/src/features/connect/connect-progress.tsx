import { Banner, Button, SkeletonText } from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { api, call } from "@/api/client";
import { humanMessage } from "@/api/errors";
import { attachConnectionThread } from "@/features/chat/connection-api";

/** Старая ссылка на job лишь восстанавливает canonical conversation; новое задание не создаётся. */
export function ConnectProgress({ jobId }: { jobId: string }) {
  const navigate = useNavigate();
  const resolved = useQuery({
    queryKey: ["jobs", jobId, "conversation"],
    queryFn: async ({ signal }) => {
      const job = await call(
        api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } }, signal }),
      );
      if (!job.connectionId) throw new Error("job_without_connection");
      return attachConnectionThread(job.connectionId, jobId, signal);
    },
    staleTime: Number.POSITIVE_INFINITY,
  });
  useEffect(() => {
    if (resolved.data) void navigate({ to: "/chat", search: { thread: resolved.data.id }, replace: true });
  }, [navigate, resolved.data]);
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
