import type { ScheduleEvent } from "@/api/types";
import { endMs, startMs } from "@/features/schedule/events";

export interface RoomInfo {
  /** Название аудитории так, как оно написано в расписании. */
  room: string;
  /** Предстоящие пары в аудитории, по времени. */
  upcoming: ScheduleEvent[];
}

const key = (room: string) => room.trim().replace(/\s+/g, " ").toLowerCase();

/**
 * Аудитории из расписания. Отдельного списка аудиторий API не отдаёт, поэтому раздел собирается из пар
 * ближайших недель: аудитория, предстоящие в ней пары. Аудитории по алфавиту (естественный порядок чисел).
 */
export function roomsFromEvents(events: readonly ScheduleEvent[], nowMs: number): RoomInfo[] {
  const rooms = new Map<string, RoomInfo>();
  for (const event of events) {
    const room = event.location?.trim();
    if (!room || endMs(event) <= nowMs) continue;
    const id = key(room);
    const info = rooms.get(id) ?? { room: room.replace(/\s+/g, " "), upcoming: [] };
    info.upcoming.push(event);
    rooms.set(id, info);
  }
  return [...rooms.values()]
    .map((info) => ({ ...info, upcoming: [...info.upcoming].sort((a, b) => startMs(a) - startMs(b)) }))
    .sort((a, b) => a.room.localeCompare(b.room, "ru", { numeric: true }));
}
