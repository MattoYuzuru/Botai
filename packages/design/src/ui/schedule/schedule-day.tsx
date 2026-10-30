import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { countRu, DEFAULT_TIME_ZONE, formatDayWithWeekday, formatTime } from "../../lib/format";
import { Badge } from "../badge/badge";
import { EmptyState } from "../empty-state/empty-state";
import { Skeleton } from "../skeleton/skeleton";
import { KIND_BADGE, type ScheduleFact, ScheduleFacts, ScheduleProvenance } from "./schedule-facts";

export interface ScheduleItem {
  id: string;
  /** Начало и конец занятия, мс. */
  start: number;
  end: number;
  title: string;
  /** Тип занятия бейджем: «Лекция» или слова источника («Текущий рубежный контроль»). */
  badge?: string | null;
  /** Детали в порядке показа (аудитория, преподаватель, адрес, группа…): их готовит пресет `eventDetails`. */
  facts?: readonly ScheduleFact[];
  spaceId: string;
  spaceTitle?: string;
  /** Занятие изменилось по сравнению с прошлым снимком. */
  changed?: boolean;
  /** Пересекается по времени с другим занятием. */
  conflict?: boolean;
  /** Источники, из которых склеена пара, основной первым; строка видна, когда их больше одного (FR-054). */
  sources?: readonly string[];
  /** Источники расходятся или пары нет в основном источнике: готовая спокойная строка под деталями. */
  note?: string | null;
}

export type { ScheduleFact };

export type ScheduleDayState = "loading" | "empty" | "ok" | "stale";

export interface ScheduleDayProps {
  /** Любой момент нужного дня, мс. */
  date: number;
  items: readonly ScheduleItem[];
  state: ScheduleDayState;
  nowMs: number;
  timeZone?: string;
  /** Метка вуза у каждой строки (сводный экран). */
  showSpace?: boolean;
  /** Компактный вид без заголовка дня — колонка недели на ноутбуке. */
  compact?: boolean;
  onOpenItem?: (item: ScheduleItem) => void;
  /** Подпись свежести данных под списком. */
  freshness?: ReactNode;
  className?: string;
}

const PAIRS = ["пара", "пары", "пар"] as const;

/**
 * Занятия одного дня. Состояния: loading — скелетон, empty — «Пар нет», ok, stale. Изменение — метка
 * «Изменено», пересечение по времени — метка и полоса `warning`, идущее сейчас занятие — «Идёт сейчас».
 */
export function ScheduleDay({
  date,
  items,
  state,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  showSpace = false,
  compact = false,
  onOpenItem,
  freshness,
  className,
}: ScheduleDayProps) {
  const sorted = [...items].sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
  return (
    <section
      aria-label={formatDayWithWeekday(date, nowMs, timeZone)}
      aria-busy={state === "loading" || undefined}
      data-state={state}
      className={cn("flex flex-col gap-3", className)}
    >
      {!compact && (
        <header className="flex items-baseline justify-between gap-3 px-1">
          <h2 className="type-heading text-fg">{formatDayWithWeekday(date, nowMs, timeZone)}</h2>
          {state !== "loading" && sorted.length > 0 && (
            <span className="type-caption text-fg-muted">{countRu(sorted.length, PAIRS)}</span>
          )}
        </header>
      )}

      {state === "loading" && (
        <div role="status" className="flex flex-col gap-2">
          <span className="sr-only">Загрузка расписания</span>
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-16 rounded-lg" />
          ))}
        </div>
      )}

      {state === "empty" && <EmptyState icon="schedule" title="Пар нет" size="sm" as="h3" />}

      {(state === "ok" || state === "stale") && (
        <ol className="flex flex-col gap-2">
          {sorted.map((item) => {
            const live = item.start <= nowMs && nowMs < item.end;
            const Row = onOpenItem ? "button" : "div";
            return (
              <li key={item.id}>
                <Row
                  {...(onOpenItem ? { type: "button" as const, onClick: () => onOpenItem(item) } : {})}
                  data-space-id={item.spaceId}
                  data-conflict={item.conflict || undefined}
                  data-live={live || undefined}
                  className={cn(
                    "relative flex min-h-16 w-full gap-3 rounded-lg border border-border bg-surface px-3 py-3 text-left",
                    onOpenItem &&
                      "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
                    item.conflict && "border-warning",
                  )}
                >
                  <span className="flex w-12 shrink-0 flex-col type-mono text-fg" data-numeric="">
                    <time dateTime={new Date(item.start).toISOString()}>
                      {formatTime(item.start, timeZone)}
                    </time>
                    <time dateTime={new Date(item.end).toISOString()} className="text-fg-subtle">
                      {formatTime(item.end, timeZone)}
                    </time>
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="type-subheading text-fg">{item.title}</span>
                      {item.badge && (
                        <Badge tone="info" className={KIND_BADGE}>
                          {item.badge}
                        </Badge>
                      )}
                    </span>
                    <ScheduleFacts facts={item.facts} layout="inline" />
                    <ScheduleProvenance sources={item.sources} note={item.note} />
                    <span className="flex flex-wrap gap-1.5 empty:hidden">
                      {live && <Badge tone="accent">Идёт сейчас</Badge>}
                      {item.changed && (
                        <Badge tone="warning" icon="edit">
                          Изменено
                        </Badge>
                      )}
                      {item.conflict && (
                        <Badge tone="warning" icon="warning">
                          Пересекается по времени
                        </Badge>
                      )}
                      {showSpace && item.spaceTitle && <Badge tone="neutral">{item.spaceTitle}</Badge>}
                    </span>
                  </span>
                </Row>
              </li>
            );
          })}
        </ol>
      )}

      {freshness && <p className="px-1 type-caption text-fg-subtle">{freshness}</p>}
    </section>
  );
}
