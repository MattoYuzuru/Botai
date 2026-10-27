import { useQuery } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import type { Job } from "@/api/types";
import { dataKeys } from "@/features/data/queries";

const TERMINAL: ReadonlySet<Job["state"]> = new Set(["done", "failed", "cancelled"]);
export const isJobFinished = (job: Job | undefined): boolean => job !== undefined && TERMINAL.has(job.state);

/**
 * Ход задания чтения документа: опрос `GET /jobs/{id}` раз в 1,5 с, пока задание не закончилось. Процентов у
 * сервера нет — показываем стадию словами и настоящее прошедшее время. Опрос идёт в фоне, кнопки нет.
 */
export function useIngestJob(jobId: string | null) {
  return useQuery({
    queryKey: dataKeys.job(jobId ?? "none"),
    enabled: jobId !== null,
    queryFn: () => call(api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId ?? "" } } })),
    refetchInterval: (query) => (isJobFinished(query.state.data) ? false : 1500),
    // Задание — рядом: сетевой сбой повторяется чаще обычного, а ответ сервера 4xx не повторяется.
    retry: 5,
    retryDelay: 1000,
    staleTime: 0,
  });
}

/** Что написать о задании: стадия словами сервера, без технических слов. */
export function describeJob(job: Job | undefined): {
  text: string;
  failed: boolean;
  done: boolean;
  asking: boolean;
} {
  if (!job) return { text: "Отправляю", failed: false, done: false, asking: false };
  switch (job.state) {
    case "queued":
      return { text: "В очереди", failed: false, done: false, asking: false };
    case "running":
      return { text: job.stage || "Читаю документ", failed: false, done: false, asking: false };
    case "waiting_user":
      return { text: job.stage || "Агенту нужен ваш ответ", failed: false, done: false, asking: true };
    case "done":
      return { text: "Документ добавлен", failed: false, done: true, asking: false };
    case "cancelled":
      return { text: "Чтение отменено", failed: true, done: false, asking: false };
    case "failed":
      return {
        text:
          typeof job.error?.detail === "string" && job.error.detail
            ? job.error.detail
            : "Не удалось прочитать документ",
        failed: true,
        done: false,
        asking: false,
      };
  }
}
