import type { QueryClient } from "@tanstack/react-query";
import { threadsQuery } from "./queries";

/** Тред, в котором идёт задача: задачи привязаны к треду, где их попросили (§3.1). Нет такого — `null`. */
export async function threadOfJob(queryClient: QueryClient, jobId: string): Promise<string | null> {
  const threads = await queryClient.fetchQuery({ ...threadsQuery, staleTime: 0 });
  return threads.find((thread) => thread.activeTask?.jobId === jobId)?.id ?? null;
}
