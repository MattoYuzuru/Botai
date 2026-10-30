import type { QueryClient } from "@tanstack/react-query";
import { type JobOut, v3 } from "@/api/v3";
import { threadsQuery } from "./queries";

/**
 * Тред, в котором идёт задача: задачи привязаны к треду, где их попросили (§3.1). Ищем сначала среди активных
 * задач списка чатов, затем в самом задании — сервер может отдать в `JobOut` идентификатор треда. Не нашли — `null`:
 * вызывающий ведёт человека к списку чатов, а не в «последний тред» наугад.
 */
export async function threadOfJob(queryClient: QueryClient, jobId: string): Promise<string | null> {
  const threads = await queryClient.fetchQuery({ ...threadsQuery, staleTime: 0 });
  const listed = threads.find((thread) => thread.activeTask?.jobId === jobId)?.id;
  if (listed) return listed;
  try {
    const job = await v3<JobOut & { threadId?: string | null }>(
      "GET",
      `/api/v1/jobs/${encodeURIComponent(jobId)}`,
    );
    return job.threadId ?? null;
  } catch {
    return null;
  }
}
