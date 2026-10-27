/**
 * Сезонные образы Вари (docs/design/mascot.md §7). Реестр — данные, а не код: окно, приоритет, образ.
 * Календарь считается в часовом поясе пространства вуза; окно `[from, to)` — `from` включительно,
 * `to` исключительно, переход через год (декабрь → январь) обработан явно. Правила не загружаются по сети
 * и не содержат исполняемого кода.
 */
import { zonedParts } from "../lib/format";
import type { MascotSkinId } from "./types";

/** Слот на теле: два образа в один слот не накладываются (§7), выбирается один. */
export type MascotSlot = "head" | "beak" | "side";

export interface MascotEvent {
  eventId: string;
  /** Начало окна, включительно, 00:00. */
  from: { month: number; day: number };
  /** Конец окна, исключительно, 00:00. */
  to: { month: number; day: number };
  /** `"space"` — часовой пояс активного пространства; иначе IANA-имя пояса. */
  timeZone: "space" | string;
  /** Больше — важнее. */
  priority: number;
  skinId: MascotSkinId;
  slot: MascotSlot;
  /** Готовый спрайт образа для `motion: 'static'`: файл в `mascot/vendor/sprites/`. */
  staticFallback: string;
}

export const MASCOT_EVENTS: readonly MascotEvent[] = [
  {
    eventId: "new-year",
    from: { month: 12, day: 20 },
    to: { month: 1, day: 11 },
    timeZone: "space",
    priority: 100,
    skinId: "new-year",
    slot: "head",
    staticFallback: "varya-skin-new-year.svg",
  },
  {
    eventId: "session-winter",
    from: { month: 1, day: 10 },
    to: { month: 2, day: 1 },
    timeZone: "space",
    priority: 50,
    skinId: "mug",
    slot: "side",
    staticFallback: "varya-skin-mug.svg",
  },
  {
    eventId: "session-summer",
    from: { month: 6, day: 1 },
    to: { month: 7, day: 1 },
    timeZone: "space",
    priority: 50,
    skinId: "mug",
    slot: "side",
    staticFallback: "varya-skin-mug.svg",
  },
  {
    eventId: "knowledge-day",
    from: { month: 9, day: 1 },
    to: { month: 9, day: 2 },
    timeZone: "space",
    priority: 60,
    skinId: "leaf",
    slot: "beak",
    staticFallback: "varya-skin-leaf.svg",
  },
];

const ordinal = (point: { month: number; day: number }) => point.month * 100 + point.day;

/** Попадает ли календарный день (месяц, число) в окно `[from, to)`; окно может переходить через год. */
export function inWindow(
  date: { month: number; day: number },
  from: { month: number; day: number },
  to: { month: number; day: number },
): boolean {
  const day = ordinal(date);
  const start = ordinal(from);
  const end = ordinal(to);
  return start <= end ? day >= start && day < end : day >= start || day < end;
}

/** Все окна, в которые попадает момент `nowMs` в часовом поясе пространства, по убыванию важности. */
export function activeEvents(
  nowMs: number,
  spaceTimeZone: string,
  events: readonly MascotEvent[] = MASCOT_EVENTS,
): MascotEvent[] {
  return events
    .filter((event) => {
      const zone = event.timeZone === "space" ? spaceTimeZone : event.timeZone;
      const parts = zonedParts(nowMs, zone);
      return inWindow({ month: parts.month, day: parts.day }, event.from, event.to);
    })
    .sort((a, b) => b.priority - a.priority || (a.eventId < b.eventId ? -1 : a.eventId > b.eventId ? 1 : 0));
}

/**
 * Образ на момент `nowMs`: пересечение окон — максимальный `priority`, при равенстве стабильно по `eventId`.
 * `occupied` — слоты, уже занятые позой (у `night` голова в ночном колпаке, сбоку своя кружка): образ в занятый
 * слот не накладывается, берётся следующий подходящий.
 */
export function activeSeasonalEvent(
  nowMs: number,
  spaceTimeZone: string,
  options: { occupied?: readonly MascotSlot[]; events?: readonly MascotEvent[] } = {},
): MascotEvent | null {
  const occupied = options.occupied ?? [];
  return (
    activeEvents(nowMs, spaceTimeZone, options.events).find((event) => !occupied.includes(event.slot)) ?? null
  );
}
