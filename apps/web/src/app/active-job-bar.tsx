import { Badge, Banner, Button } from "@botai/design";
import { Link, useRouterState } from "@tanstack/react-router";
import { useEffect } from "react";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager, useJob } from "@/features/jobs/job-manager";
import { isTerminal, needsHuman } from "@/features/jobs/job-state";

/**
 * Свёрнутая долгая работа видна в шапке всех экранов: «Подключаю ВШЭ — Читаю список курсов», «Нужен ответ»,
 * итог. Поток задания продолжает жить вне экрана (`jobManager`), поэтому свёртывание ничего не обрывает.
 */
export function ActiveJobBar() {
  const active = useActiveJob((state) => state.job);
  const clear = useActiveJob((state) => state.clear);
  const job = useJob(active?.jobId);
  const onConnect = useRouterState({ select: (state) => state.location.pathname.startsWith("/connect/") });

  // После перезагрузки страницы поток задания поднимается заново: сервер отдаёт события из буфера.
  useEffect(() => {
    if (active) jobManager.start(active.jobId);
  }, [active]);

  if (!active || onConnect) return null;
  const terminal = job ? isTerminal(job.state) : false;
  const attention = job ? needsHuman(job) : false;
  const failed = job?.state === "failed" || job?.state === "cancelled";

  return (
    <Banner
      tone={terminal ? (failed ? "danger" : "success") : attention ? "warning" : "info"}
      icon={terminal ? (failed ? "warning" : "ok") : attention ? "waiting" : "sync"}
      live={terminal || attention}
      className="rounded-none px-safe-4 pt-safe-3"
      action={
        <div className="flex items-center gap-1">
          <Button variant="secondary" size="sm" data-touch-expanded="" asChild>
            <Link to="/connect/$jobId" params={{ jobId: active.jobId }}>
              Открыть
            </Link>
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
      {!terminal && job?.stage && <p className="type-caption">{job.stage}</p>}
    </Banner>
  );
}
