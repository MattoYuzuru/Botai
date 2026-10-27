/**
 * Форматирование текстов интерфейса (styleguide §9 «Тексты интерфейса»): русский язык,
 * время «14:05», дата «1 октября» (для другого года — «1 октября 2025 г.»), относительное
 * «5 минут назад», срок «через 5 ч» / «вчера в 23:59». Числа и единицы — неразрывным пробелом.
 * Все функции чистые: «сейчас» передаётся аргументом, а не берётся из часов.
 */

export const NBSP = " ";
export const DEFAULT_TIME_ZONE = "Europe/Moscow";

/** Согласование: pluralRu(3, ['пара', 'пары', 'пар']) → «пары». */
export function pluralRu(n: number, forms: readonly [string, string, string]): string {
  const abs = Math.abs(Math.trunc(n));
  const mod100 = abs % 100;
  const mod10 = abs % 10;
  if (mod100 >= 11 && mod100 <= 19) return forms[2];
  if (mod10 === 1) return forms[0];
  if (mod10 >= 2 && mod10 <= 4) return forms[1];
  return forms[2];
}

/** «3 пары», число и слово связаны неразрывным пробелом. */
export function countRu(n: number, forms: readonly [string, string, string]): string {
  return `${n}${NBSP}${pluralRu(n, forms)}`;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const formatters = new Map<string, Intl.DateTimeFormat>();
function formatter(key: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  let cached = formatters.get(key);
  if (!cached) {
    cached = new Intl.DateTimeFormat("ru-RU", options);
    formatters.set(key, cached);
  }
  return cached;
}

interface Parts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  weekday: number;
}

/** Календарные части момента в часовом поясе (weekday: 1 — понедельник … 7 — воскресенье). */
export function zonedParts(ms: number, timeZone: string): Parts {
  const f = formatter(`parts:${timeZone}`, {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    weekday: "short",
  });
  const values: Record<string, string> = {};
  for (const part of f.formatToParts(ms)) values[part.type] = part.value;
  const weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
  return {
    year: Number(values.year),
    month: Number(values.month),
    day: Number(values.day),
    hour: Number(values.hour),
    minute: Number(values.minute),
    weekday: weekdays.indexOf((values.weekday ?? "").toLowerCase().replace(".", "")) + 1,
  };
}

/** Номер календарного дня в часовом поясе — для сравнения «сегодня / вчера». */
export function dayNumber(ms: number, timeZone: string): number {
  const p = zonedParts(ms, timeZone);
  return Math.floor(Date.UTC(p.year, p.month - 1, p.day) / DAY);
}

/** «14:05». */
export function formatTime(ms: number, timeZone = DEFAULT_TIME_ZONE): string {
  return formatter(`time:${timeZone}`, {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(ms);
}

/** «1 октября» или «1 октября 2025 г.» для другого года. */
export function formatDay(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  const sameYear = zonedParts(ms, timeZone).year === zonedParts(nowMs, timeZone).year;
  if (sameYear) return formatter(`day:${timeZone}`, { timeZone, day: "numeric", month: "long" }).format(ms);
  return `${formatter(`dayYear:${timeZone}`, { timeZone, day: "numeric", month: "long", year: "numeric" })
    .format(ms)
    .replace(/\sг\.$/, "")} г.`;
}

/** «понедельник, 1 октября» — заголовок дня расписания. */
export function formatDayWithWeekday(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  const weekday = formatter(`weekday:${timeZone}`, { timeZone, weekday: "long" }).format(ms);
  return `${weekday}, ${formatDay(ms, nowMs, timeZone)}`;
}

/** «пн» — короткое название дня недели. */
export function formatWeekdayShort(ms: number, timeZone = DEFAULT_TIME_ZONE): string {
  return formatter(`weekdayShort:${timeZone}`, { timeZone, weekday: "short" }).format(ms).replace(".", "");
}

/** «14 мин», «1 ч 5 мин», «12 с» — длительность без округления вверх. */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return minutes > 0 ? `${hours}${NBSP}ч ${minutes}${NBSP}мин` : `${hours}${NBSP}ч`;
  if (minutes > 0) return seconds > 0 ? `${minutes}${NBSP}мин ${seconds}${NBSP}с` : `${minutes}${NBSP}мин`;
  return `${seconds}${NBSP}с`;
}

const MINUTES = ["минуту", "минуты", "минут"] as const;
const HOURS = ["час", "часа", "часов"] as const;

/**
 * Относительное время в прошлом: «только что», «5 минут назад», «3 часа назад»,
 * «вчера в 23:59», дальше — дата. Время из будущего считается «только что» (часы клиента и сервера расходятся).
 */
export function formatAgo(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  const diff = nowMs - ms;
  if (diff < MINUTE) return "только что";
  if (diff < HOUR) {
    const n = Math.floor(diff / MINUTE);
    return n === 1 ? "минуту назад" : `${countRu(n, MINUTES)} назад`;
  }
  const days = dayNumber(nowMs, timeZone) - dayNumber(ms, timeZone);
  if (days === 0) {
    const n = Math.floor(diff / HOUR);
    return n === 1 ? "час назад" : `${countRu(n, HOURS)} назад`;
  }
  if (days === 1) return `вчера в ${formatTime(ms, timeZone)}`;
  return formatDay(ms, nowMs, timeZone);
}

/** Подпись свежести данных: «Обновлено 5 минут назад». */
export function formatUpdated(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  return `Обновлено ${formatAgo(ms, nowMs, timeZone)}`;
}

/** Остаток до срока в одной единице: «5 ч», «2 д», «40 мин». */
export function formatLeftShort(ms: number): string {
  const abs = Math.max(0, ms);
  if (abs >= DAY) return `${Math.floor(abs / DAY)}${NBSP}д`;
  if (abs >= HOUR) return `${Math.floor(abs / HOUR)}${NBSP}ч`;
  return `${Math.max(1, Math.floor(abs / MINUTE))}${NBSP}мин`;
}

/** Срок в будущем: «через 5 ч», «завтра в 09:00», «через 2 д». Прошедший срок — «вчера в 23:59» / дата. */
export function formatDue(dueMs: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  const diff = dueMs - nowMs;
  if (diff <= 0) return `срок прошёл ${formatAgo(dueMs, nowMs, timeZone)}`;
  if (diff < DAY) return `через ${formatLeftShort(diff)}`;
  const days = dayNumber(dueMs, timeZone) - dayNumber(nowMs, timeZone);
  if (days === 1) return `завтра в ${formatTime(dueMs, timeZone)}`;
  if (days > 1 && days <= 6) return `через ${days}${NBSP}д`;
  return `${formatDay(dueMs, nowMs, timeZone)} в ${formatTime(dueMs, timeZone)}`;
}

/** Абсолютный момент без отсчёта: «сегодня в 19:05», «завтра в 09:00», «вчера в 23:59», «29 октября, 16:05». */
export function formatWhen(ms: number, nowMs: number, timeZone = DEFAULT_TIME_ZONE): string {
  const days = dayNumber(ms, timeZone) - dayNumber(nowMs, timeZone);
  const clock = formatTime(ms, timeZone);
  if (days === 0) return `сегодня в ${clock}`;
  if (days === 1) return `завтра в ${clock}`;
  if (days === -1) return `вчера в ${clock}`;
  return `${formatDay(ms, nowMs, timeZone)}, ${clock}`;
}
