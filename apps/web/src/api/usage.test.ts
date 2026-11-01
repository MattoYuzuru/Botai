import { describe, expect, it } from "vitest";
import { limitKnown, quotaDayKey, sumPool, sumUsageDays, todayUsage } from "./usage";

const NOW = Date.UTC(2026, 9, 27, 14, 40); // 27 октября 2026, 17:40 по Москве

const day = (date: string, tokens: number, costUsd = 0) => ({ day: date, tokens, costUsd });

describe("день квоты", () => {
  it("ключ дня — календарная дата по Москве, а не UTC", () => {
    expect(quotaDayKey(NOW)).toBe("2026-10-27");
    // 22:30 UTC — в Москве уже следующие сутки.
    expect(quotaDayKey(Date.UTC(2026, 9, 27, 22, 30))).toBe("2026-10-28");
    expect(quotaDayKey(Date.UTC(2026, 9, 26, 23, 30))).toBe("2026-10-27");
  });

  it("расход за сегодня — только день пояса квоты, а не последняя строка списка", () => {
    const days = [day("2026-10-26", 10, 0.1), day("2026-10-27", 20, 0.2)];
    expect(todayUsage(days, NOW)?.tokens).toBe(20);
    expect(todayUsage([day("2026-10-26", 10)], NOW)).toBeNull();
    expect(todayUsage([], NOW)).toBeNull();
  });
});

describe("итоги по настоящим числам", () => {
  it("суммирует токены и стоимость, сохраняя число дней", () => {
    const totals = sumUsageDays([day("a", 10, 0.1), day("b", 5, 0.2)]);
    expect(totals.tokens).toBe(15);
    expect(totals.costUsd).toBeCloseTo(0.3);
    expect(totals.days).toBe(2);
  });

  it("пустая история — нули и ноль дней, а не выдуманный ноль процентов", () => {
    expect(sumUsageDays([])).toEqual({ tokens: 0, costUsd: 0, days: 0 });
  });

  it("пул: итоги по людям и их число", () => {
    const totals = sumPool([
      {
        userId: "u1",
        email: "a@example.test",
        displayName: "Анна",
        tokensToday: 10,
        costUsdToday: 0.1,
        tokensWeek: 70,
        costUsdWeek: 0.7,
      },
      {
        userId: "u2",
        email: "b@example.test",
        displayName: "Мария",
        tokensToday: 5,
        costUsdToday: 0.05,
        tokensWeek: 35,
        costUsdWeek: 0.35,
      },
    ]);
    expect(totals.users).toBe(2);
    expect(totals.tokensToday).toBe(15);
    expect(totals.tokensWeek).toBe(105);
    expect(totals.costUsdToday).toBeCloseTo(0.15);
    expect(totals.costUsdWeek).toBeCloseTo(1.05);
    expect(sumPool([]).users).toBe(0);
  });

  it("лимит: ноль у сервера — «не задан», а не «ноль токенов»", () => {
    expect(limitKnown(1_000_000)).toBe(true);
    expect(limitKnown(0)).toBe(false);
    expect(limitKnown(-1)).toBe(false);
    expect(limitKnown(Number.NaN)).toBe(false);
  });
});
