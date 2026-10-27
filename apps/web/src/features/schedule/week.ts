import { countRu, formatDay, pluralRu, type WeekDay } from "@botai/design";
import type { ScheduleEvent } from "@/api/types";
import { addDays, overlapsDay, startOfWeek, weekDays } from "@/features/data/dates";
import { endMs, startMs } from "./events";

export interface WeekRange {
  /** Понедельник 00:00 в поясе вуза. */
  start: number;
  /** Следующий понедельник 00:00 (конец периода не включается). */
  end: number;
  days: number[];
}

/** Неделя, в которую попадает момент: запрос `/schedule` берёт период [start, end). */
export function weekOf(ms: number, timeZone: string): WeekRange {
  const start = startOfWeek(ms, timeZone);
  return { start, end: addDays(start, 7, timeZone), days: weekDays(ms, timeZone) };
}

/** Пары, затрагивающие день: пара через полночь попадает в оба дня. */
export function eventsOnDay(
  events: readonly ScheduleEvent[],
  day: number,
  timeZone: string,
): ScheduleEvent[] {
  return events.filter((event) => overlapsDay(startMs(event), endMs(event), day, timeZone));
}

/** Лента дней: сколько пар в каждом дне и есть ли пересечение между вузами. */
export function stripDays(
  days: readonly number[],
  events: readonly ScheduleEvent[],
  timeZone: string,
): WeekDay[] {
  return days.map((date) => {
    const mine = eventsOnDay(events, date, timeZone);
    return { date, count: mine.length, hasConflict: mine.some((event) => event.conflictsWith.length > 0) };
  });
}

/** Сколько пар недели пересекается с парой другого вуза. */
export function conflictingCount(events: readonly ScheduleEvent[]): number {
  return events.filter((event) => event.conflictsWith.length > 0).length;
}

const CONFLICT_FORMS = ["пара пересекается", "пары пересекаются", "пар пересекаются"] as const;

/** «2 пары пересекаются по времени с парами другого вуза». */
export function conflictSummary(count: number): string {
  return `${count} ${pluralRu(count, CONFLICT_FORMS)} по времени с парами другого вуза`;
}

/** «26 октября – 1 ноября». */
export function weekRangeLabel(range: WeekRange, nowMs: number, timeZone: string): string {
  const last = range.days[6] ?? range.start;
  return `${formatDay(range.start, nowMs, timeZone)} – ${formatDay(last, nowMs, timeZone)}`;
}

/** «5 пар на неделе». */
export function weekCountLabel(count: number): string {
  return `${countRu(count, ["пара", "пары", "пар"])} на неделе`;
}
