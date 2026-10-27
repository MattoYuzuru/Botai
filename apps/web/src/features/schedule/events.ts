import type { ScheduleItem } from "@botai/design";
import type { ScheduleEvent } from "@/api/types";
import type { EventFormat, EventKind } from "@/features/data/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";

export const EVENT_KIND_LABEL: Record<EventKind, string | null> = {
  lecture: "Лекция",
  seminar: "Семинар",
  lab: "Лабораторная",
  exam: "Экзамен",
  consultation: "Консультация",
  other: null,
};

export const EVENT_FORMAT_LABEL: Record<EventFormat, string | null> = {
  offline: "Очно",
  online: "Онлайн",
  hybrid: "Смешанный формат",
  unknown: null,
};

export const startMs = (event: ScheduleEvent): number => Date.parse(event.startsAt);
export const endMs = (event: ScheduleEvent): number => Date.parse(event.endsAt);

/** Пары по времени начала, затем по названию — порядок стабилен между обновлениями. */
export function sortEvents(events: readonly ScheduleEvent[]): ScheduleEvent[] {
  return [...events].sort(
    (a, b) => startMs(a) - startMs(b) || endMs(a) - endMs(b) || a.id.localeCompare(b.id),
  );
}

/** Строка `ScheduleDay`: вуз подписан коротким именем, пересечение с другим вузом — флагом. */
export function toScheduleItem(event: ScheduleEvent, spaces: ReadonlyMap<string, SpaceInfo>): ScheduleItem {
  return {
    id: event.id,
    start: startMs(event),
    end: endMs(event),
    title: event.title,
    room: event.location,
    teacher: event.teacher,
    spaceId: event.provenance.spaceId,
    spaceTitle: spaces.get(event.provenance.spaceId)?.shortTitle,
    changed: event.changed,
    conflict: event.conflictsWith.length > 0,
  };
}
