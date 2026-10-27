/**
 * Срочность работ (styleguide §8 «Учебные данные»). Единственное место с порогами 72/24 ч:
 * компоненты (`DeadlineItem`), экраны и `resolveMascot` берут расчёт отсюда, а не считают сами.
 * Чистые функции: «сейчас» приходит аргументом.
 *
 *   calm     > 72 ч до срока — нейтрально, время текстом;
 *   soon     24–72 ч         — «Скоро · 2 д»;
 *   urgent   ≤ 24 ч          — «Осталось 5 ч»;
 *   overdue  срок прошёл     — «Просрочено», без укоров;
 *   unknown  срока нет или он не разобрался — «Срок не указан», без придуманного отсчёта.
 */
import { dayNumber, formatLeftShort, formatWhen, NBSP } from "./format";

export type Urgency = "calm" | "soon" | "urgent" | "overdue" | "unknown";

export const HOUR_MS = 3_600_000;
export const SOON_THRESHOLD_MS = 72 * HOUR_MS;
export const URGENT_THRESHOLD_MS = 24 * HOUR_MS;

/** Срок в мс или null, если его нет или он не разбирается. */
export function parseDue(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const ms = typeof value === "number" ? value : Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** Срок, по которому считается срочность: назначенный преподавателем (`effectiveDueAt`) важнее базового. */
export function effectiveDue(item: { effectiveDueAt?: string | null; dueAt?: string | null }): number | null {
  return parseDue(item.effectiveDueAt) ?? parseDue(item.dueAt);
}

export function urgencyOf(dueMs: number | null | undefined, nowMs: number): Urgency {
  if (dueMs === null || dueMs === undefined || !Number.isFinite(dueMs)) return "unknown";
  const left = dueMs - nowMs;
  if (left <= 0) return "overdue";
  if (left <= URGENT_THRESHOLD_MS) return "urgent";
  if (left <= SOON_THRESHOLD_MS) return "soon";
  return "calm";
}

/** Порядок «что горит»: меньше — срочнее. unknown в конце. */
export const URGENCY_RANK: Record<Urgency, number> = { overdue: 0, urgent: 1, soon: 2, calm: 3, unknown: 4 };

export interface UrgencyLabel {
  /** Короткая метка для бейджа. */
  badge: string | null;
  /** Строка срока рядом с названием. */
  due: string;
}

/** Тексты по срочности. Для `unknown` — «Срок не указан» и никакого отсчёта. */
export function urgencyLabel(
  urgency: Urgency,
  dueMs: number | null,
  nowMs: number,
  timeZone: string,
): UrgencyLabel {
  if (urgency === "unknown" || dueMs === null) return { badge: null, due: "Срок не указан" };
  const left = dueMs - nowMs;
  const when = formatWhen(dueMs, nowMs, timeZone);
  switch (urgency) {
    case "overdue":
      return { badge: "Просрочено", due: `Срок был ${when}` };
    case "urgent":
      return { badge: `Осталось${NBSP}${formatLeftShort(left)}`, due: when };
    case "soon":
      return { badge: `Скоро · ${formatLeftShort(left)}`, due: when };
    default:
      return { badge: null, due: when };
  }
}

export type DeadlineGroup = "today" | "week" | "later" | "none" | "done" | "overdue";

/** Группа списка заданий §7.1: «Сегодня / Неделя / Позже / Без срока / Сдано»; просроченное — сверху. */
export function deadlineGroup(
  dueMs: number | null,
  nowMs: number,
  timeZone: string,
  done: boolean,
): DeadlineGroup {
  if (done) return "done";
  if (dueMs === null) return "none";
  if (dueMs <= nowMs) return "overdue";
  const days = dayNumber(dueMs, timeZone) - dayNumber(nowMs, timeZone);
  if (days <= 0) return "today";
  if (days <= 7) return "week";
  return "later";
}
