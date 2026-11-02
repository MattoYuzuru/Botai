import type { ScheduleEvent } from "@/api/types";
import { addDays, startOfDay } from "@/features/data/dates";

/** VALUE=DATE is kept as a source attribute; older snapshots only have the midnight boundaries. */
export function isAllDayEvent(
  event: Pick<ScheduleEvent, "startsAt" | "endsAt" | "attributes">,
  timeZone: string,
): boolean {
  const marker = event.attributes?.find((attribute) =>
    /^(весь день|all[ _-]?day)$/i.test(attribute.label.trim()),
  );
  if (marker) return /^(да|true|1|весь день)$/i.test(marker.value.trim());
  const start = Date.parse(event.startsAt);
  const end = Date.parse(event.endsAt);
  return (
    start === startOfDay(start, timeZone) &&
    end === startOfDay(end, timeZone) &&
    end >= addDays(start, 1, timeZone)
  );
}
