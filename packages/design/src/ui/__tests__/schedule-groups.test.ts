import { describe, expect, it } from "vitest";
import { groupScheduleItems } from "../schedule/schedule-groups";

const item = (id: string, start: number, end: number) => ({ id, start, end });
const ids = (items: ReturnType<typeof item>[]) =>
  groupScheduleItems(items).map((group) => group.map((row) => row.id));

describe("visual calendar overlap groups", () => {
  it("preserves distinct simultaneous events and sorts them stably", () => {
    expect(ids([item("c", 30, 50), item("b", 10, 40), item("a", 10, 40)])).toEqual([["a", "b", "c"]]);
  });

  it("groups nested and transitive overlaps without hiding any event", () => {
    expect(ids([item("all-day", 0, 24), item("booking-a", 9, 10), item("booking-b", 14, 15)])).toEqual([
      ["all-day", "booking-a", "booking-b"],
    ]);
    expect(ids([item("a", 1, 3), item("b", 2, 5), item("c", 4, 6)])).toEqual([["a", "b", "c"]]);
  });

  it("uses half-open boundaries: adjacent events and separated events stay separate", () => {
    expect(ids([item("a", 1, 3), item("b", 3, 5), item("c", 7, 8)])).toEqual([["a"], ["b"], ["c"]]);
    expect(ids([])).toEqual([]);
  });
});
