import { describe, expect, it } from "vitest";
import {
  countRu,
  dayNumber,
  formatAgo,
  formatDay,
  formatDayWithWeekday,
  formatDue,
  formatDuration,
  formatLeftShort,
  formatTime,
  formatUpdated,
  formatWeekdayShort,
  formatWhen,
  NBSP,
  pluralRu,
} from "./format";

const MSK = "Europe/Moscow";
// 27 октября 2026, 14:05 по Москве (UTC+3).
const NOW = Date.UTC(2026, 9, 27, 11, 5);
const at = (y: number, mo: number, d: number, h: number, mi: number) => Date.UTC(y, mo - 1, d, h - 3, mi);

describe("pluralRu", () => {
  const pairs = ["пара", "пары", "пар"] as const;
  it.each([
    [0, "пар"],
    [1, "пара"],
    [2, "пары"],
    [4, "пары"],
    [5, "пар"],
    [11, "пар"],
    [12, "пар"],
    [14, "пар"],
    [21, "пара"],
    [22, "пары"],
    [25, "пар"],
    [101, "пара"],
    [111, "пар"],
  ])("%i → %s", (n, word) => {
    expect(pluralRu(n, pairs)).toBe(word);
  });

  it("число и слово связаны неразрывным пробелом", () => {
    expect(countRu(3, pairs)).toBe(`3${NBSP}пары`);
  });
});

describe("время и дата", () => {
  it("время «14:05» в часовом поясе пространства", () => {
    expect(formatTime(NOW, MSK)).toBe("14:05");
    expect(formatTime(NOW, "Asia/Yekaterinburg")).toBe("16:05");
    expect(formatTime(at(2026, 10, 27, 0, 0), MSK)).toBe("00:00");
  });

  it("дата «1 октября»; для другого года — «1 октября 2025 г.»", () => {
    expect(formatDay(at(2026, 10, 1, 12, 0), NOW, MSK)).toBe("1 октября");
    expect(formatDay(at(2025, 10, 1, 12, 0), NOW, MSK)).toBe("1 октября 2025 г.");
  });

  it("день недели короткий и в заголовке дня", () => {
    expect(formatWeekdayShort(NOW, MSK)).toBe("вт");
    expect(formatDayWithWeekday(NOW, NOW, MSK)).toBe("вторник, 27 октября");
  });

  it("номер дня учитывает часовой пояс на границе суток", () => {
    const lateMoscow = at(2026, 10, 27, 23, 30);
    expect(dayNumber(lateMoscow, MSK)).toBe(dayNumber(NOW, MSK));
    expect(dayNumber(lateMoscow, "Asia/Vladivostok")).toBe(dayNumber(NOW, MSK) + 1);
  });
});

describe("длительность", () => {
  it.each([
    [0, `0${NBSP}с`],
    [12_000, `12${NBSP}с`],
    [72_000, `1${NBSP}мин 12${NBSP}с`],
    [120_000, `2${NBSP}мин`],
    [3_900_000, `1${NBSP}ч 5${NBSP}мин`],
    [7_200_000, `2${NBSP}ч`],
  ])("%i мс → %s", (ms, text) => {
    expect(formatDuration(ms)).toBe(text);
  });
});

describe("относительное время", () => {
  it.each([
    [NOW - 20_000, "только что"],
    [NOW + 60_000, "только что"],
    [NOW - 60_000, "минуту назад"],
    [NOW - 5 * 60_000, `5${NBSP}минут назад`],
    [NOW - 21 * 60_000, `21${NBSP}минуту назад`],
    [NOW - 60 * 60_000, "час назад"],
    [NOW - 3 * 3_600_000, `3${NBSP}часа назад`],
    [at(2026, 10, 26, 23, 59), "вчера в 23:59"],
    [at(2026, 10, 24, 9, 0), "24 октября"],
  ])("%i", (ms, text) => {
    expect(formatAgo(ms, NOW, MSK)).toBe(text);
  });

  it("подпись свежести данных", () => {
    expect(formatUpdated(NOW - 5 * 60_000, NOW, MSK)).toBe(`Обновлено 5${NBSP}минут назад`);
  });
});

describe("срок", () => {
  it("остаток в одной единице", () => {
    expect(formatLeftShort(5 * 3_600_000 + 1)).toBe(`5${NBSP}ч`);
    expect(formatLeftShort(2 * 86_400_000 + 5)).toBe(`2${NBSP}д`);
    expect(formatLeftShort(40 * 60_000)).toBe(`40${NBSP}мин`);
    expect(formatLeftShort(10_000)).toBe(`1${NBSP}мин`);
  });

  it("срок в будущем и в прошлом", () => {
    expect(formatDue(NOW + 5 * 3_600_000, NOW, MSK)).toBe(`через 5${NBSP}ч`);
    expect(formatDue(at(2026, 10, 28, 15, 0), NOW, MSK)).toBe("завтра в 15:00");
    expect(formatDue(at(2026, 10, 30, 9, 0), NOW, MSK)).toBe(`через 3${NBSP}д`);
    expect(formatDue(at(2026, 12, 5, 18, 30), NOW, MSK)).toBe("5 декабря в 18:30");
    expect(formatDue(at(2026, 10, 26, 23, 59), NOW, MSK)).toBe("срок прошёл вчера в 23:59");
  });
});

describe("абсолютный срок", () => {
  it("сегодня, завтра, вчера и дата", () => {
    expect(formatWhen(at(2026, 10, 27, 19, 5), NOW, MSK)).toBe("сегодня в 19:05");
    expect(formatWhen(at(2026, 10, 28, 9, 0), NOW, MSK)).toBe("завтра в 09:00");
    expect(formatWhen(at(2026, 10, 26, 23, 59), NOW, MSK)).toBe("вчера в 23:59");
    expect(formatWhen(at(2026, 10, 29, 16, 5), NOW, MSK)).toBe("29 октября, 16:05");
  });
});
