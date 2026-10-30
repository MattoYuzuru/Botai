import type { Thread } from "@/api/types";
import { v3 } from "@/api/v3";

/** Тред источника: открывает существующий разговор подключения, ничего не запускает. */
export function attachConnectionThread(
  connectionId: string,
  jobId?: string,
  signal?: AbortSignal,
): Promise<Thread> {
  return v3<Thread>("POST", `/api/v1/connections/${encodeURIComponent(connectionId)}/conversation`, {
    body: jobId ? { jobId } : {},
    signal,
  });
}
