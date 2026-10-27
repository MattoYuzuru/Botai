import { describe, expect, it } from "vitest";
import { activeEvents, activeSeasonalEvent, inWindow, MASCOT_EVENTS, type MascotEvent } from "./events";

const MSK = "Europe/Moscow";
const msk = (month: number, day: number, hour = 0, minute = 0, year = 2026) =>
  Date.UTC(year, month - 1, day, hour - 3, minute);
const skinAt = (nowMs: number, zone = MSK) => activeSeasonalEvent(nowMs, zone)?.eventId ?? null;

describe("календарь сезонных образов §7", () => {
  it("окно [from, to): from включительно, to исключительно", () => {
    expect(inWindow({ month: 9, day: 1 }, { month: 9, day: 1 }, { month: 9, day: 2 })).toBe(true);
    expect(inWindow({ month: 9, day: 2 }, { month: 9, day: 1 }, { month: 9, day: 2 })).toBe(false);
    expect(inWindow({ month: 8, day: 31 }, { month: 9, day: 1 }, { month: 9, day: 2 })).toBe(false);
  });

  it("окно через год: 20 декабря → 11 января", () => {
    const from = { month: 12, day: 20 };
    const to = { month: 1, day: 11 };
    for (const [month, day, expected] of [
      [12, 19, false],
      [12, 20, true],
      [12, 31, true],
      [1, 1, true],
      [1, 10, true],
      [1, 11, false],
      [6, 15, false],
    ] as const) {
      expect(inWindow({ month, day }, from, to), `${day}.${month}`).toBe(expected);
    }
  });

  it("новогодний образ — ровно с 20 декабря 00:00 по 11 января 00:00 по Москве", () => {
    expect(skinAt(msk(12, 19, 23, 59))).toBeNull();
    expect(skinAt(msk(12, 20, 0, 0))).toBe("new-year");
    expect(skinAt(msk(1, 10, 23, 59, 2027))).toBe("new-year");
    expect(skinAt(msk(1, 11, 0, 0, 2027))).toBe("session-winter");
  });

  it("окно считается в часовом поясе пространства, а не устройства", () => {
    const instant = msk(12, 19, 23, 59);
    expect(skinAt(instant, MSK)).toBeNull();
    // 23:59 в Москве — уже 03:59 20 декабря в Красноярске (UTC+7).
    expect(skinAt(instant, "Asia/Krasnoyarsk")).toBe("new-year");
    // И наоборот: в Калининграде (UTC+2) ещё 22:59 19 декабря.
    expect(skinAt(instant + 60_000, "Europe/Kaliningrad")).toBeNull();
  });

  it("пересечение окон: максимальный priority, при равенстве — стабильно по eventId", () => {
    // 10 января идут и «new-year» (100), и «session-winter» (50).
    expect(activeEvents(msk(1, 10, 12, 0, 2027), MSK).map((event) => event.eventId)).toEqual([
      "new-year",
      "session-winter",
    ]);
    expect(skinAt(msk(1, 10, 12, 0, 2027))).toBe("new-year");
    const tie = (id: string): MascotEvent => ({
      eventId: id,
      from: { month: 3, day: 1 },
      to: { month: 3, day: 5 },
      timeZone: "space",
      priority: 10,
      skinId: "mug",
      slot: "side",
      staticFallback: "varya-skin-mug.svg",
    });
    expect(activeSeasonalEvent(msk(3, 2), MSK, { events: [tie("b"), tie("a")] })?.eventId).toBe("a");
    expect(activeSeasonalEvent(msk(3, 2), MSK, { events: [tie("a"), tie("b")] })?.eventId).toBe("a");
  });

  it("реестр: окна, образы и приоритеты из §7", () => {
    const byId = Object.fromEntries(MASCOT_EVENTS.map((event) => [event.eventId, event]));
    expect(byId["new-year"]).toMatchObject({ from: { month: 12, day: 20 }, to: { month: 1, day: 11 }, priority: 100 });
    expect(byId["session-winter"]).toMatchObject({ from: { month: 1, day: 10 }, to: { month: 2, day: 1 }, priority: 50 });
    expect(byId["session-summer"]).toMatchObject({ from: { month: 6, day: 1 }, to: { month: 7, day: 1 }, priority: 50 });
    expect(byId["knowledge-day"]).toMatchObject({ from: { month: 9, day: 1 }, to: { month: 9, day: 2 }, priority: 60 });
    expect(new Set(MASCOT_EVENTS.map((event) => event.eventId)).size).toBe(MASCOT_EVENTS.length);
    expect(skinAt(msk(6, 15, 12))).toBe("session-summer");
    expect(skinAt(msk(7, 1, 0, 0))).toBeNull();
    expect(skinAt(msk(9, 1, 12))).toBe("knowledge-day");
    expect(skinAt(msk(5, 10, 12))).toBeNull();
  });

  it("образ в занятый слот не накладывается", () => {
    const winter = msk(12, 25, 12);
    expect(activeSeasonalEvent(winter, MSK, { occupied: ["head"] })).toBeNull();
    expect(activeSeasonalEvent(winter, MSK, { occupied: ["side"] })?.eventId).toBe("new-year");
    // 10 января: колпак занят — берётся образ другого слота, если окно идёт.
    expect(activeSeasonalEvent(msk(1, 10, 12, 0, 2027), MSK, { occupied: ["head"] })?.eventId).toBe(
      "session-winter",
    );
  });

  it("реестр — данные: правила не содержат функций и исполняемого кода", () => {
    for (const event of MASCOT_EVENTS) {
      for (const value of Object.values(event)) expect(typeof value).not.toBe("function");
    }
    expect(JSON.parse(JSON.stringify(MASCOT_EVENTS))).toEqual(MASCOT_EVENTS);
  });
});
