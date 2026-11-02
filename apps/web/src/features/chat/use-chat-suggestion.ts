import { useQuery } from "@tanstack/react-query";
import type { Message } from "@/api/types";
import { jobFailed } from "@/features/jobs/job-state";
import { suggestionQuery } from "./queries";
import type { ThreadJob } from "./timeline";

/** Только последний сохранённый текст агента. Сервер также проверяет финальное состояние старых заданий. */
export function suggestionAnchor(messages: readonly Message[], jobs: readonly ThreadJob[]): string | null {
  const last = messages.at(-1);
  if (last?.role !== "agent") return null;
  if (
    !last.blocks.some(
      (block) => block.type === "text" && typeof block.markdown === "string" && block.markdown.trim(),
    )
  )
    return null;
  const job = jobs.find((item) => item.id === last.jobId);
  if (job && (job.snapshot.state !== "done" || jobFailed(job.snapshot))) return null;
  return last.id;
}

export function useChatSuggestion({
  ownerId,
  threadId,
  anchor,
  enabled,
}: {
  ownerId: string | undefined;
  threadId: string | null;
  anchor: string | null;
  enabled: boolean;
}): string | null {
  const ready = Boolean(enabled && ownerId && threadId && anchor);
  const query = useQuery({
    ...suggestionQuery(ownerId ?? "", threadId ?? "", anchor ?? ""),
    enabled: ready,
  });
  // Нет побочного заполнения поля: ответ относится только к текущему владельцу/треду/сообщению.
  // Поздний запрос может попасть в свой кэш, но не заменит уже набираемый текст.
  return ready && query.data?.afterMessageId === anchor ? query.data.text?.trim() || null : null;
}
