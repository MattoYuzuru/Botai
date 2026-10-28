import { Badge, Banner, Button } from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useRouterState } from "@tanstack/react-router";
import { useEffect } from "react";
import { threadRuntimeKey, threadRuntimeQuery } from "@/features/chat/connection-api";
import { connectionSnapshot } from "@/features/chat/connection-timeline";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager, useJob } from "@/features/jobs/job-manager";
import { isTerminal, jobFailed, needsHuman } from "@/features/jobs/job-state";

/**
 * Свёрнутая долгая работа видна в шапке всех экранов: «Подключаю ВШЭ — Читаю список курсов», «Нужен ответ»,
 * итог. Поток задания продолжает жить вне экрана (`jobManager`), поэтому свёртывание ничего не обрывает.
 */
export function ActiveJobBar() {
  const active = useActiveJob((state) => state.job);
  const clear = useActiveJob((state) => state.clear);
  const job = useJob(active?.jobId);
  const queryClient = useQueryClient();
  const runtime = useQuery({
    ...threadRuntimeQuery(active?.threadId ?? ""),
    enabled: Boolean(active?.threadId),
  });
  const matchingRuntime = runtime.data?.activeJob?.id === active?.jobId ? runtime.data : undefined;
  const current = matchingRuntime ? connectionSnapshot(matchingRuntime, job) : job;
  const onConnect = useRouterState({ select: (state) => state.location.pathname.startsWith("/connect/") });
  const onConversation = useRouterState({
    select: (state) =>
      state.location.pathname === "/chat" &&
      new URLSearchParams(state.location.searchStr).get("thread") === active?.threadId,
  });

  // После перезагрузки страницы поток задания поднимается заново: сервер отдаёт события из буфера.
  useEffect(() => {
    if (active) jobManager.start(active.jobId);
  }, [active]);
  useEffect(() => {
    if (active?.threadId && job?.lastEventId)
      void queryClient.invalidateQueries({ queryKey: threadRuntimeKey(active.threadId) });
  }, [active?.threadId, job?.lastEventId, queryClient]);

  if (!active || onConnect || onConversation) return null;
  const terminal = current ? isTerminal(current.state) : false;
  const attention = active.threadId
    ? (matchingRuntime?.pending.length ?? 0) > 0
    : current
      ? needsHuman(current, active.connectionId)
      : false;
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
            {active.threadId ? (
              <Link to="/chat" search={{ thread: active.threadId }}>
                Открыть
              </Link>
            ) : (
              <Link to="/connect/$jobId" params={{ jobId: active.jobId }}>
                Открыть
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
              : `${active.spaceTitle ?? "Вуз"} подключён`
            : active.title}
        </span>
        {attention && <Badge tone="warning">Нужен ответ</Badge>}
      </div>
      {!terminal && current?.stage && <p className="type-caption">{current.stage}</p>}
    </Banner>
  );
}
