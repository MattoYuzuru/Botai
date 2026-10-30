import type { ScheduleItem } from "@botai/design";
import type { ScheduleEvent } from "@/api/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { compactFacts, eventDetails } from "./event-details";
import { disagreementNote, mergedSources } from "./sources";

export const startMs = (event: ScheduleEvent): number => Date.parse(event.startsAt);
export const endMs = (event: ScheduleEvent): number => Date.parse(event.endsAt);

/** Пары по времени начала, затем по названию — порядок стабилен между обновлениями. */
export function sortEvents(events: readonly ScheduleEvent[]): ScheduleEvent[] {
  return [...events].sort(
    (a, b) => startMs(a) - startMs(b) || endMs(a) - endMs(b) || a.id.localeCompare(b.id),
  );
}

/**
 * Строка `ScheduleDay` и карточка недели: вуз подписан коротким именем, пересечение с другим вузом — флагом, а
 * тип занятия и детали (аудитория, адрес, преподаватель, группа) идут компактной частью пресета `eventDetails`.
 * Склеенная из нескольких источников пара подписана ими, расхождение источников — спокойной строкой (FR-054).
 */
export function toScheduleItem(event: ScheduleEvent, spaces: ReadonlyMap<string, SpaceInfo>): ScheduleItem {
  const details = eventDetails(event);
  return {
    id: event.id,
    start: startMs(event),
    end: endMs(event),
    title: event.title,
    badge: details.badge,
    facts: compactFacts(details),
    spaceId: event.provenance.spaceId,
    spaceTitle: spaces.get(event.provenance.spaceId)?.shortTitle,
    changed: event.changed,
    conflict: event.conflictsWith.length > 0,
    sources: mergedSources(event),
    note: disagreementNote(event),
  };
}
