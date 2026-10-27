import { describe, expect, it } from "vitest";
import { addDays, isSameDay, overlapsDay, startOfDay, startOfWeek, weekDays, zonedMidnight } from "./dates";

const MSK = "Europe/Moscow";
// 2026-10-27 (вторник) 15:20 по Москве.
const TUESDAY = Date.UTC(2026, 9, 27, 12, 20);

describe("календарные дни в поясе вуза", () => {
  it("начало дня — полночь по Москве, а не по UTC", () => {
    expect(new Date(startOfDay(TUESDAY, MSK)).toISOString()).toBe("2026-10-26T21:00:00.000Z");
  });

  it("пара в 00:30 по Москве относится к новому дню, хотя в UTC это вчера", () => {
    const late = Date.UTC(2026, 9, 26, 21, 30);
    expect(isSameDay(late, TUESDAY, MSK)).toBe(true);
    expect(new Date(startOfDay(late, MSK)).toISOString()).toBe("2026-10-26T21:00:00.000Z");
  });

  it("неделя начинается с понедельника и состоит из семи дней", () => {
    expect(new Date(startOfWeek(TUESDAY, MSK)).toISOString()).toBe("2026-10-25T21:00:00.000Z");
    const days = weekDays(TUESDAY, MSK);
    expect(days).toHaveLength(7);
    expect(new Date(days[0] as number).toISOString()).toBe("2026-10-25T21:00:00.000Z");
    expect(new Date(days[6] as number).toISOString()).toBe("2026-10-31T21:00:00.000Z");
  });

  it("воскресенье остаётся в своей неделе", () => {
    const sunday = Date.UTC(2026, 10, 1, 9, 0);
    expect(new Date(startOfWeek(sunday, MSK)).toISOString()).toBe("2026-10-25T21:00:00.000Z");
  });

  it("addDays переходит через границу месяца и года", () => {
    const newYearEve = Date.UTC(2026, 11, 31, 10, 0);
    expect(new Date(addDays(newYearEve, 1, MSK)).toISOString()).toBe("2026-12-31T21:00:00.000Z");
    expect(new Date(addDays(newYearEve, -31, MSK)).toISOString()).toBe("2026-11-29T21:00:00.000Z");
  });

  it("летнее время: сутки в Нью-Йорке в день перехода длятся 23 часа", () => {
    const NY = "America/New_York";
    const before = zonedMidnight(2026, 3, 8, NY);
    const after = zonedMidnight(2026, 3, 9, NY);
    expect(after - before).toBe(23 * 3_600_000);
  });

  it("пара через полночь попадает в оба дня", () => {
    const start = Date.UTC(2026, 9, 26, 20, 0);
    const end = Date.UTC(2026, 9, 26, 22, 0);
    expect(overlapsDay(start, end, TUESDAY, MSK)).toBe(true);
    expect(overlapsDay(start, end, addDays(TUESDAY, -1, MSK), MSK)).toBe(true);
    expect(overlapsDay(start, end, addDays(TUESDAY, 1, MSK), MSK)).toBe(false);
  });
});
