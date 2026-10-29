import { cn } from "../../lib/cn";
import {
  DEFAULT_TIME_ZONE,
  dayNumber,
  formatDayWithWeekday,
  formatTime,
  formatWeekdayShort,
  zonedParts,
} from "../../lib/format";
import { Badge } from "../badge/badge";
import { Skeleton } from "../skeleton/skeleton";
import type { ScheduleDayState, ScheduleItem } from "./schedule-day";
import { ScheduleFacts } from "./schedule-facts";

export interface ScheduleGridDay {
  /** Любой момент нужного дня, мс. */
  date: number;
  items: readonly ScheduleItem[];
}

export interface ScheduleGridProps {
  /** Семь дней недели с понедельника. */
  days: readonly ScheduleGridDay[];
  state: ScheduleDayState;
  nowMs: number;
  timeZone?: string;
  /** Метка вуза у каждой пары (сводный экран). */
  showSpace?: boolean;
  onOpenItem?: (item: ScheduleItem) => void;
  className?: string;
}

/**
 * Неделя сеткой в семь колонок для ноутбука (styleguide §7, от 1024 px). Колонка — день: заголовок и пары
 * по времени. Пересечение с парой другого вуза и изменение — метки с текстом, не только цвет. На телефоне
 * её место занимают `ScheduleWeek` и `ScheduleDay`.
 */
export function ScheduleGrid({
  days,
  state,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  showSpace = false,
  onOpenItem,
  className,
}: ScheduleGridProps) {
  const today = dayNumber(nowMs, timeZone);
  return (
    <section
      aria-label="Неделя"
      aria-busy={state === "loading" || undefined}
      data-state={state}
      className={cn("grid grid-cols-7 items-start gap-2", className)}
    >
      {days.map((day) => {
        const number = dayNumber(day.date, timeZone);
        const isToday = number === today;
        const items = [...day.items].sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
        return (
          <div key={number} data-today={isToday || undefined} className="flex min-w-0 flex-col gap-2">
            <header
              className={cn(
                "flex min-h-14 flex-col items-center justify-center rounded-md px-1 py-2",
                isToday ? "bg-accent-subtle text-accent-fg" : "bg-bg-subtle text-fg",
              )}
            >
              <span aria-hidden="true" className={cn("type-caption uppercase", !isToday && "text-fg-muted")}>
                {formatWeekdayShort(day.date, timeZone)}
              </span>
              <span aria-hidden="true" className="type-heading" data-numeric="">
                {zonedParts(day.date, timeZone).day}
              </span>
              <h3 className="sr-only">{formatDayWithWeekday(day.date, nowMs, timeZone)}</h3>
            </header>

            {state === "loading" ? (
              <div role="status" className="flex flex-col gap-2">
                <span className="sr-only">Загрузка расписания</span>
                <Skeleton className="h-20 rounded-md" />
                <Skeleton className="h-20 rounded-md" />
              </div>
            ) : items.length === 0 ? (
              <p className="px-1 text-center type-caption text-fg-subtle">Пар нет</p>
            ) : (
              <ol className="flex flex-col gap-2">
                {items.map((item) => {
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
                          "flex w-full min-w-0 flex-col gap-1 rounded-md border border-border bg-surface p-2 text-left",
                          onOpenItem &&
                            "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
                          item.conflict && "border-warning",
                          live && "bg-accent-subtle",
                        )}
                      >
                        <span
                          className="flex flex-wrap items-baseline gap-x-1 type-caption text-fg-muted"
                          data-numeric=""
                        >
                          <time dateTime={new Date(item.start).toISOString()}>
                            {formatTime(item.start, timeZone)}
                          </time>
                          <span aria-hidden="true">–</span>
                          <time dateTime={new Date(item.end).toISOString()}>
                            {formatTime(item.end, timeZone)}
                          </time>
                        </span>
                        <span className="break-words type-label text-fg">{item.title}</span>
                        {item.badge && (
                          <Badge tone="info" className="max-w-full self-start">
                            <span className="truncate">{item.badge}</span>
                          </Badge>
                        )}
                        <ScheduleFacts facts={item.facts} layout="stacked" />
                        <span className="flex flex-wrap gap-1 empty:hidden">
                          {live && <Badge tone="accent">Идёт сейчас</Badge>}
                          {item.conflict && (
                            <Badge tone="warning" icon="warning">
                              Пересечение
                            </Badge>
                          )}
                          {item.changed && (
                            <Badge tone="warning" icon="edit">
                              Изменено
                            </Badge>
                          )}
                          {showSpace && item.spaceTitle && <Badge tone="neutral">{item.spaceTitle}</Badge>}
                        </span>
                      </Row>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
        );
      })}
    </section>
  );
}
