import { describe, expect, it } from "vitest";
import { NBSP } from "./format";
import {
  deadlineGroup,
  effectiveDue,
  HOUR_MS,
  parseDue,
  URGENCY_RANK,
  urgencyLabel,
  urgencyOf,
} from "./urgency";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 11, 5); // 14:05 МСК

describe("urgencyOf: пороги 72 ч и 24 ч", () => {
  it.each([
    [null, "unknown"],
    [undefined, "unknown"],
    [Number.NaN, "unknown"],
    [NOW - 1, "overdue"],
    [NOW, "overdue"],
    [NOW + 1, "urgent"],
    [NOW + 5 * HOUR_MS, "urgent"],
    [NOW + 24 * HOUR_MS, "urgent"],
    [NOW + 24 * HOUR_MS + 1, "soon"],
    [NOW + 72 * HOUR_MS, "soon"],
    [NOW + 72 * HOUR_MS + 1, "calm"],
    [NOW + 400 * HOUR_MS, "calm"],
  ])("срок %s → %s", (due, expected) => {
    expect(urgencyOf(due, NOW)).toBe(expected);
  });

  it("порядок «что горит» ставит просроченное выше срочного, а срок без даты — в конец", () => {
    const order = Object.entries(URGENCY_RANK)
      .sort((a, b) => a[1] - b[1])
      .map(([key]) => key);
    expect(order).toEqual(["overdue", "urgent", "soon", "calm", "unknown"]);
  });
});

describe("разбор срока", () => {
  it("пустой и невалидный срок — null, а не выдуманная дата", () => {
    expect(parseDue(null)).toBeNull();
    expect(parseDue("")).toBeNull();
    expect(parseDue("не дата")).toBeNull();
    expect(parseDue("2026-10-28T09:00:00Z")).toBe(Date.UTC(2026, 9, 28, 9, 0));
  });

  it("effectiveDueAt важнее dueAt", () => {
    expect(effectiveDue({ dueAt: "2026-10-28T09:00:00Z", effectiveDueAt: "2026-10-29T09:00:00Z" })).toBe(
      Date.UTC(2026, 9, 29, 9, 0),
    );
    expect(effectiveDue({ dueAt: "2026-10-28T09:00:00Z", effectiveDueAt: null })).toBe(
      Date.UTC(2026, 9, 28, 9, 0),
    );
    expect(effectiveDue({ dueAt: null })).toBeNull();
  });
});

describe("тексты срочности", () => {
  it("unknown — «Срок не указан» без отсчёта", () => {
    expect(urgencyLabel("unknown", null, NOW, MSK)).toEqual({ badge: null, due: "Срок не указан" });
  });

  it("urgent — явный остаток, soon — «Скоро · 2 д», overdue — без укоров", () => {
    const due5h = NOW + 5 * HOUR_MS;
    expect(urgencyLabel("urgent", due5h, NOW, MSK)).toEqual({
      badge: `Осталось${NBSP}5${NBSP}ч`,
      due: "сегодня в 19:05",
    });
    const due2d = NOW + 50 * HOUR_MS;
    expect(urgencyLabel("soon", due2d, NOW, MSK).badge).toBe(`Скоро · 2${NBSP}д`);
    const past = NOW - 2 * HOUR_MS;
    expect(urgencyLabel("overdue", past, NOW, MSK)).toEqual({
      badge: "Просрочено",
      due: "Срок был сегодня в 12:05",
    });
  });

  it("calm — время текстом, без бейджа", () => {
    const due = NOW + 10 * 24 * HOUR_MS;
    const label = urgencyLabel("calm", due, NOW, MSK);
    expect(label.badge).toBeNull();
    expect(label.due).toBe("6 ноября, 14:05");
  });
});

describe("группы заданий", () => {
  it("«Сегодня / Неделя / Позже / Без срока / Сдано»; просроченное отдельно", () => {
    expect(deadlineGroup(null, NOW, MSK, false)).toBe("none");
    expect(deadlineGroup(NOW + HOUR_MS, NOW, MSK, true)).toBe("done");
    expect(deadlineGroup(NOW - HOUR_MS, NOW, MSK, false)).toBe("overdue");
    expect(deadlineGroup(NOW + 2 * HOUR_MS, NOW, MSK, false)).toBe("today");
    expect(deadlineGroup(NOW + 30 * HOUR_MS, NOW, MSK, false)).toBe("week");
    expect(deadlineGroup(NOW + 20 * 24 * HOUR_MS, NOW, MSK, false)).toBe("later");
  });
});
