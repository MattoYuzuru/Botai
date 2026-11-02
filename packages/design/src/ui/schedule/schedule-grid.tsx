import { cn } from "../../lib/cn";
import {
  DEFAULT_TIME_ZONE,
  dayNumber,
  formatDayWithWeekday,
  formatWeekdayShort,
  zonedParts,
} from "../../lib/format";
import { Skeleton } from "../skeleton/skeleton";
import { ScheduleCard } from "./schedule-card";
import type { ScheduleDayState, ScheduleItem } from "./schedule-day";
import { groupScheduleItems } from "./schedule-groups";

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
  /** Enables shared cards for overlapping events, opening every member together. */
  onOpenGroup?: (items: readonly ScheduleItem[], date: number) => void;
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
  onOpenGroup,
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
                {(onOpenGroup ? groupScheduleItems(items) : items.map((item) => [item])).map((group) => (
                  <li key={group[0]?.id}>
                    <ScheduleCard
                      items={group}
                      nowMs={nowMs}
                      timeZone={timeZone}
                      showSpace={showSpace}
                      compact
                      onOpenItem={onOpenItem}
                      onOpenGroup={onOpenGroup ? (items) => onOpenGroup(items, day.date) : undefined}
                    />
                  </li>
                ))}
              </ol>
            )}
          </div>
        );
      })}
    </section>
  );
}
