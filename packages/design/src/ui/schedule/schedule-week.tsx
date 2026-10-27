import { useEffect, useRef } from "react";
import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE, dayNumber, formatWeekdayShort, zonedParts } from "../../lib/format";

export interface WeekDay {
  /** Любой момент нужного дня, мс. */
  date: number;
  /** Сколько занятий в этот день; 0 — точки нет. */
  count: number;
  /** В этот день есть пересечение по времени. */
  hasConflict?: boolean;
}

export interface ScheduleWeekProps {
  days: readonly WeekDay[];
  /** Выбранный день, мс (любой момент дня). */
  selected: number;
  onSelect: (date: number) => void;
  nowMs: number;
  timeZone?: string;
  className?: string;
}

/**
 * Лента дней недели со `scroll-snap`: единственная разрешённая горизонтальная прокрутка. Выбранный
 * день — заливка `accent`, сегодняшний — `accent-fg`, день с пересечением — метка `warning`.
 * На 320 px лента прокручивается; зона каждого дня — не меньше 44 px.
 */
export function ScheduleWeek({
  days,
  selected,
  onSelect,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  className,
}: ScheduleWeekProps) {
  const selectedDay = dayNumber(selected, timeZone);
  const today = dayNumber(nowMs, timeZone);
  const selectedRef = useRef<HTMLButtonElement>(null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: выбранный день меняется — лента прокручивается к нему
  useEffect(() => {
    // Выбранный день всегда в зоне видимости ленты.
    selectedRef.current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [selectedDay]);

  return (
    <nav aria-label="Дни недели" className={cn("-mx-4 xs:-mx-5", className)}>
      <ul className="flex snap-x snap-mandatory gap-1 overflow-x-auto px-4 pb-1 xs:px-5">
        {days.map((day) => {
          const number = dayNumber(day.date, timeZone);
          const isSelected = number === selectedDay;
          const isToday = number === today;
          return (
            <li key={number} className="min-w-11 flex-1 snap-start">
              <button
                ref={isSelected ? selectedRef : undefined}
                type="button"
                onClick={() => onSelect(day.date)}
                aria-pressed={isSelected}
                aria-current={isToday ? "date" : undefined}
                data-today={isToday || undefined}
                className={cn(
                  "flex min-h-16 w-full cursor-pointer flex-col items-center justify-center gap-0.5 rounded-lg px-1 py-2",
                  "transition-colors duration-fast ease-soft",
                  isSelected
                    ? "bg-accent text-fg-on-accent"
                    : cn("hover:bg-hover active:bg-pressed", isToday ? "text-accent-fg" : "text-fg"),
                )}
              >
                <span
                  className={cn("type-caption uppercase", isSelected ? "text-fg-on-accent" : "text-fg-muted")}
                >
                  {formatWeekdayShort(day.date, timeZone)}
                </span>
                <span className="type-heading" data-numeric="">
                  {zonedParts(day.date, timeZone).day}
                </span>
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-1.5 rounded-full",
                    day.count === 0 && "opacity-0",
                    isSelected ? "bg-fg-on-accent" : day.hasConflict ? "bg-warning" : "bg-control",
                  )}
                />
                {day.count > 0 && (
                  <span className="sr-only">
                    {day.count} занятий{day.hasConflict ? ", есть пересечение" : ""}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
