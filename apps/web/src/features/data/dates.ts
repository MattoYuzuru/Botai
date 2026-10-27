/**
 * Календарная арифметика в часовом поясе вуза. Расписание, «Сегодня» и задания считают дни не по
 * часам браузера, а по поясу пространства: пара в 00:30 по Москве — это уже завтра, где бы ни был студент.
 * Чистые функции: «сейчас» приходит аргументом.
 */
import { DEFAULT_TIME_ZONE, dayNumber, zonedParts } from "@botai/design";

const MINUTE = 60_000;

/** Смещение пояса от UTC в мс в данный момент (с точностью до минуты). */
function offsetAt(ms: number, timeZone: string): number {
  const parts = zonedParts(ms, timeZone);
  return (
    Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute) -
    Math.floor(ms / MINUTE) * MINUTE
  );
}

/** Полночь календарного дня (год, месяц 1–12, день; переполнение дня переносится на соседние месяцы). */
export function zonedMidnight(year: number, month: number, day: number, timeZone: string): number {
  const wall = Date.UTC(year, month - 1, day);
  // Второй проход нужен, когда смещение в полночь отличается от смещения в «наивный» момент (переход на летнее время).
  const first = wall - offsetAt(wall, timeZone);
  return wall - offsetAt(first, timeZone);
}

/** Начало календарного дня, в который попадает момент. */
export function startOfDay(ms: number, timeZone = DEFAULT_TIME_ZONE): number {
  const p = zonedParts(ms, timeZone);
  return zonedMidnight(p.year, p.month, p.day, timeZone);
}

/** Полночь через `days` календарных дней (отрицательное — назад). */
export function addDays(ms: number, days: number, timeZone = DEFAULT_TIME_ZONE): number {
  const p = zonedParts(ms, timeZone);
  return zonedMidnight(p.year, p.month, p.day + days, timeZone);
}

/** Понедельник недели, в которую попадает момент (в России неделя начинается с понедельника). */
export function startOfWeek(ms: number, timeZone = DEFAULT_TIME_ZONE): number {
  const p = zonedParts(ms, timeZone);
  return zonedMidnight(p.year, p.month, p.day - (p.weekday - 1), timeZone);
}

/** Семь полуночей недели, начиная с понедельника. */
export function weekDays(ms: number, timeZone = DEFAULT_TIME_ZONE): number[] {
  const p = zonedParts(ms, timeZone);
  return Array.from({ length: 7 }, (_, index) =>
    zonedMidnight(p.year, p.month, p.day - (p.weekday - 1) + index, timeZone),
  );
}

export function isSameDay(a: number, b: number, timeZone = DEFAULT_TIME_ZONE): boolean {
  return dayNumber(a, timeZone) === dayNumber(b, timeZone);
}

/** Занятие затрагивает день (пара через полночь попадает в оба дня). */
export function overlapsDay(start: number, end: number, day: number, timeZone = DEFAULT_TIME_ZONE): boolean {
  const from = startOfDay(day, timeZone);
  const to = addDays(day, 1, timeZone);
  return start < to && end > from;
}
