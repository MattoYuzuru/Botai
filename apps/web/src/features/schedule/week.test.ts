import { describe, expect, it } from "vitest";
import type { ScheduleEvent } from "@/api/types";
import { eventFixture } from "@/test/data-fixtures";
import { conflictingCount, conflictSummary, eventsOnDay, stripDays, weekOf, weekRangeLabel } from "./week";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 14, 40);

function event(id: string, startsAt: string, endsAt: string, conflictsWith: string[] = []): ScheduleEvent {
  return eventFixture({ id, title: id, startsAt, endsAt, conflictsWith });
}

describe("неделя расписания", () => {
  it("период — с понедельника 00:00 до следующего понедельника по часам вуза", () => {
    const range = weekOf(NOW, MSK);
    expect(new Date(range.start).toISOString()).toBe("2026-10-25T21:00:00.000Z");
    expect(new Date(range.end).toISOString()).toBe("2026-11-01T21:00:00.000Z");
    expect(range.days).toHaveLength(7);
    expect(weekRangeLabel(range, NOW, MSK)).toBe("26 октября – 1 ноября");
  });

  it("лента дней считает пары и отмечает день с пересечением; пара через полночь — в двух днях", () => {
    const range = weekOf(NOW, MSK);
    const events = [
      event("a", "2026-10-27T08:30:00Z", "2026-10-27T10:00:00Z", ["b"]),
      event("b", "2026-10-27T09:00:00Z", "2026-10-27T10:30:00Z", ["a"]),
      event("night", "2026-10-27T20:00:00Z", "2026-10-27T22:00:00Z"),
    ];
    const strip = stripDays(range.days, events, MSK);
    expect(strip.map((day) => day.count)).toEqual([0, 3, 1, 0, 0, 0, 0]);
    expect(strip.map((day) => day.hasConflict)).toEqual([false, true, false, false, false, false, false]);
    expect(eventsOnDay(events, range.days[2] as number, MSK).map((row) => row.id)).toEqual(["night"]);
  });

  it("сводка пересечений согласуется по-русски", () => {
    expect(conflictSummary(1)).toBe("1 пара пересекается по времени с парами другого вуза");
    expect(conflictSummary(2)).toBe("2 пары пересекаются по времени с парами другого вуза");
    expect(conflictSummary(5)).toBe("5 пар пересекаются по времени с парами другого вуза");
    expect(
      conflictingCount([
        event("a", "2026-10-27T08:00:00Z", "2026-10-27T09:00:00Z", ["x"]),
        event("b", "2026-10-27T08:00:00Z", "2026-10-27T09:00:00Z"),
      ]),
    ).toBe(1);
  });
});
