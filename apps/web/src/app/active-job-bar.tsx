import { Badge, Banner, Button } from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect } from "react";
import { threadOfJob } from "@/features/chat/job-thread";
import { useOpenThread } from "@/features/chat/open-thread";
import { chatKeys, runtimeQuery } from "@/features/chat/queries";
import { mergeJob } from "@/features/chat/timeline";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager, useJob } from "@/features/jobs/job-manager";
import { isTerminal, jobFailed } from "@/features/jobs/job-state";

/**
 * Свёрнутая долгая работа видна в шапке всех экранов: «Подключаю ВШЭ — Читаю список курсов», «Нужен ответ»,
 * итог. Поток задания продолжает жить вне экрана (`jobManager`), поэтому свёртывание ничего не обрывает.
 * «Открыть» ведёт в тред, где идёт работа.
 */
export function ActiveJobBar() {
  const active = useActiveJob((state) => state.job);
  const clear = useActiveJob((state) => state.clear);
  const job = useJob(active?.jobId);
  const queryClient = useQueryClient();
  const openThread = useOpenThread((state) => state.id);
  // Тред задачи: из самой записи, иначе из списка чатов и задания. Не нашли — плашка ведёт к списку чатов.
  const lookup = useQuery({
    queryKey: ["job-thread", active?.jobId],
    queryFn: () => threadOfJob(queryClient, active?.jobId ?? ""),
    enabled: Boolean(active) && !active?.threadId,
    staleTime: 15_000,
  });
  const threadId = active?.threadId ?? lookup.data ?? null;
  const runtime = useQuery({
    ...runtimeQuery(threadId ?? ""),
    enabled: threadId !== null,
  });
  const saved = runtime.data
    ? [runtime.data.activeJob, ...runtime.data.tasks].find((item) => item?.id === active?.jobId)
    : undefined;
  const current = saved ? mergeJob(saved, job) : job;

  // После перезагрузки страницы поток задания поднимается заново: сервер отдаёт события из буфера.
  useEffect(() => {
    if (active) jobManager.start(active.jobId);
  }, [active]);
  useEffect(() => {
    if (threadId && job?.lastEventId)
      void queryClient.invalidateQueries({ queryKey: chatKeys.runtime(threadId) });
  }, [threadId, job?.lastEventId, queryClient]);

  if (!active || (threadId !== null && openThread === threadId)) return null;
  // Отменил сам человек — сообщать нечего: плашка уходит без тревоги.
  if (current?.state === "cancelled") return null;
  const terminal = current ? isTerminal(current.state) : false;
  const attention = (runtime.data?.pending ?? []).some((item) => item.jobId === active.jobId);
  const failed = current ? jobFailed(current) : false;

  return (
    <Banner
      tone={terminal ? (failed ? "danger" : "success") : attention ? "warning" : "info"}
      icon={terminal ? (failed ? "warning" : "ok") : attention ? "waiting" : "sync"}
      live={terminal || attention}
      className="rounded-none px-safe-4 pt-safe-3"
      action={
        <div className="flex items-center gap-1">
          <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
            {threadId ? (
              <Link to="/chat" search={{ thread: threadId }}>
                Открыть
              </Link>
            ) : (
              <Link to="/chat" search={{ threads: 1 }}>
                К чатам
              </Link>
            )}
          </Button>
          {terminal && (
            <Button variant="ghost" size="sm" data-touch-expanded="" onClick={() => clear()}>
              Скрыть
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="type-label">
          {terminal
            ? failed
              ? `${active.title}: не завершено`
              : doneLabel(saved?.kind, active.title, active.spaceTitle)
            : active.title}
        </span>
        {attention && <Badge tone="warning">Нужен ответ</Badge>}
      </div>
      {!terminal && current?.stage && <p className="type-caption">{current.stage}</p>}
    </Banner>
  );
}

/** Итог по виду задачи: документ не «подключает вуз», обновление не «подключает». */
function doneLabel(kind: string | undefined, title: string, spaceTitle: string | undefined | null): string {
  if (kind === "ingest") return `${title}: готово`;
  if (kind === "sync") return `${title}: данные обновлены`;
  if (kind === "repair") return `${title}: починка завершена`;
  return `${spaceTitle ?? "Вуз"}: источник подключён`;
}
