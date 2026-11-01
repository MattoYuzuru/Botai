import {
  Badge,
  Button,
  formatAgo,
  type IconName,
  ListRowBody,
  listRowClass,
  SkeletonText,
} from "@botai/design";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type SourceRunOut, sourceRunsQuery } from "@/api/sources";
import { describeSourceKind } from "./status";
import type { StudySource } from "./use-study-sources";

/**
 * История запусков источника (docs/v4/17-study-ui.md, `SourceRunOut`): задания `connect/sync/repair/
 * ingest/chat/action/agent`, их итог, ошибка и ссылки на тред задачи и трассу хода агента.
 * Пока запусков нет — честный текст, а не выдуманные строки.
 */
export function SourceRunsSection({
  source,
  nowMs,
  timeZone,
}: {
  source: StudySource;
  nowMs: number;
  timeZone: string;
}) {
  // История запусков — read-API v4; в fallback-режиме (движок v3) её нет.
  const runs = useInfiniteQuery({
    ...sourceRunsQuery(source.id),
    enabled: source.mode === "v4",
  });
  if (source.mode !== "v4") {
    return (
      <p className="type-caption text-fg-subtle">
        Запуски появятся после перехода на новые источники: прежняя модель подключений истории не отдаёт.
      </p>
    );
  }
  const items = runs.data?.pages.flat() ?? [];
  return (
    <section aria-labelledby="source-runs-title" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 id="source-runs-title" className="type-subheading text-fg">
          История запусков
        </h3>
        {items.length > 0 && <span className="type-caption text-fg-muted">{items.length}</span>}
      </div>
      {runs.isPending ? (
        <SkeletonText lines={2} label="Загрузка истории запусков" />
      ) : runs.isError ? (
        <p className="type-ui text-fg-muted">История запусков не загрузилась: обновление идёт в фоне.</p>
      ) : items.length === 0 ? (
        <p className="type-ui text-fg-muted">Запусков пока нет: они появятся после первого обновления.</p>
      ) : (
        <ul aria-label="Запуски источника" className="flex flex-col divide-y divide-border">
          {items.map((run) => (
            <RunRow key={run.id} run={run} nowMs={nowMs} timeZone={timeZone} />
          ))}
        </ul>
      )}
      {runs.hasNextPage && (
        <Button
          variant="secondary"
          size="sm"
          loading={runs.isFetchingNextPage}
          onClick={() => void runs.fetchNextPage()}
          className="self-start"
        >
          Показать ещё
        </Button>
      )}
    </section>
  );
}

const RUN_KIND: Record<string, { label: string; icon: IconName }> = {
  connect: { label: "Подключение", icon: "connection" },
  sync: { label: "Обновление", icon: "sync" },
  repair: { label: "Починка", icon: "repairing" },
  ingest: { label: "Чтение файла", icon: "document" },
  chat: { label: "Чат", icon: "chat" },
  action: { label: "Действие", icon: "settings" },
  agent: { label: "Задача агента", icon: "space" },
};

interface RunStatusView {
  label: string;
  tone: "success" | "info" | "warning" | "danger" | "neutral";
  icon: IconName;
}

function describeRun(run: SourceRunOut): RunStatusView {
  if (run.status === "done") {
    return run.ok === false
      ? { label: "Не удалось", tone: "danger", icon: "error" }
      : { label: "Готово", tone: "success", icon: "ok" };
  }
  if (run.status === "failed") return { label: "Не удалось", tone: "danger", icon: "error" };
  if (run.status === "running") return { label: "Выполняется", tone: "info", icon: "sync" };
  if (run.status === "queued") return { label: "В очереди", tone: "neutral", icon: "waiting" };
  if (run.status === "waiting_user") return { label: "Ждёт вас", tone: "warning", icon: "waiting" };
  return { label: "Отменено", tone: "neutral", icon: "paused" };
}

/** Счётчики запуска словами: какие данные он принёс; пустые не рисуются. */
function countsText(run: SourceRunOut): string | null {
  const parts = Object.entries(run.counts ?? {})
    .filter(([, value]) => Number.isFinite(value) && value > 0)
    .slice(0, 3)
    .map(([kind, value]) => `${describeSourceKind(kind).label}: ${value}`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function RunRow({ run, nowMs, timeZone }: { run: SourceRunOut; nowMs: number; timeZone: string }) {
  const kind = RUN_KIND[run.kind] ?? { label: "Запуск", icon: "source" as IconName };
  const status = describeRun(run);
  const started = run.startedAt ? Date.parse(run.startedAt) : Number.NaN;
  const counts = countsText(run);
  return (
    <li className="flex flex-col gap-2 py-2" data-run-kind={run.kind} data-run-status={run.status}>
      <div className={listRowClass}>
        <ListRowBody
          icon={kind.icon}
          title={kind.label}
          meta={
            <>
              <Badge tone={status.tone} icon={status.icon}>
                {status.label}
              </Badge>
              <time dateTime={run.startedAt ?? undefined}>
                {Number.isFinite(started)
                  ? `начат ${formatAgo(started, nowMs, timeZone)}`
                  : "время не указано"}
              </time>
              {run.finishedAt && (
                <span>завершён {formatAgo(Date.parse(run.finishedAt), nowMs, timeZone)}</span>
              )}
              {counts && <span>{counts}</span>}
            </>
          }
        />
      </div>
      {run.errorDetail && <p className="px-3 type-caption text-warning-fg">{run.errorDetail}</p>}
      {(run.threadId || run.runId) && (
        <div className="flex flex-wrap gap-2 px-3">
          {run.threadId && (
            <Button variant="ghost" size="sm" leadingIcon="chat" asChild data-touch-expanded="">
              <Link to="/chat" search={{ thread: run.threadId }} resetScroll={false}>
                Тред задачи
              </Link>
            </Button>
          )}
          {run.runId && (
            <Button variant="ghost" size="sm" leadingIcon="history" asChild data-touch-expanded="">
              <Link to="/agent/runs/$runId" params={{ runId: run.runId }}>
                Трасса
              </Link>
            </Button>
          )}
        </div>
      )}
    </li>
  );
}
