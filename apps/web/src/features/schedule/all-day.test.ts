import { describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { isAllDayEvent } from "./all-day";
import { eventDetails } from "./event-details";
import { eventsOnDay } from "./week";

const MSK = "Europe/Moscow";
const wholeDay = eventFixture({
  startsAt: "2026-10-26T21:00:00Z",
  endsAt: "2026-10-27T21:00:00Z",
});

describe("calendar date events", () => {
  it("uses the source attribute when available, including an explicit timed midnight event", () => {
    expect(isAllDayEvent({ ...wholeDay, attributes: [{ label: "Весь день", value: "Да" }] }, MSK)).toBe(true);
    expect(isAllDayEvent({ ...wholeDay, attributes: [{ label: "allDay", value: "false" }] }, MSK)).toBe(
      false,
    );
  });

  it("recognizes old midnight spans in the university timezone, not the browser timezone", () => {
    expect(isAllDayEvent(wholeDay, MSK)).toBe(true);
    expect(isAllDayEvent(wholeDay, "UTC")).toBe(false);
    expect(isAllDayEvent({ ...wholeDay, endsAt: "2026-10-27T20:59:59Z" }, MSK)).toBe(false);
    expect(isAllDayEvent({ ...wholeDay, endsAt: "2026-10-28T21:00:00Z" }, MSK)).toBe(true);
  });

  it("handles the 25-hour DST calendar day and exclusive day end", () => {
    const event = eventFixture({ startsAt: "2026-10-24T22:00:00Z", endsAt: "2026-10-25T23:00:00Z" });
    expect(isAllDayEvent(event, "Europe/Berlin")).toBe(true);
    expect(eventsOnDay([event], Date.parse("2026-10-25T12:00:00Z"), "Europe/Berlin")).toEqual([event]);
    expect(eventsOnDay([event], Date.parse("2026-10-26T12:00:00Z"), "Europe/Berlin")).toEqual([]);
  });

  it("shows 'Весь день' instead of 00:00–00:00 without repeating the source marker", () => {
    const details = eventDetails(
      { ...wholeDay, attributes: [{ label: "Весь день", value: "Да" }] },
      { timeZone: MSK },
    );
    expect(details.facts.find((fact) => fact.key === "time")?.text).toBe("Весь день");
    expect(details.facts.some((fact) => fact.key.startsWith("attribute"))).toBe(false);
  });
});
