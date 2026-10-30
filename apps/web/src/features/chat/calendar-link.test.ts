import { describe, expect, it } from "vitest";
import vectors from "../../../../../contracts/calendar-link-vectors.json";
import { hasCalendarLink, isCalendarLink, withoutCalendarLinks } from "./calendar-link";

interface Case {
  name: string;
  text: string;
}

const KEY = "k9Xq2LmZp7Vb4NwR8sTd";

/** Общие примеры с сервером: `backend/tests/core/test_calendar_link.py` читает тот же файл. */
describe("личная ссылка календаря: общие примеры клиента и сервера", () => {
  it.each(vectors.secret as Case[])("узнаётся: $name", ({ text }) => {
    expect(hasCalendarLink(text)).toBe(true);
    expect(hasCalendarLink(`смотри ${text} и подключи`)).toBe(true);
  });

  it.each(vectors.ordinary as Case[])("обычное сообщение: $name", ({ text }) => {
    expect(hasCalendarLink(text)).toBe(false);
    expect(hasCalendarLink(`смотри ${text} и всё`)).toBe(false);
  });

  it.each(vectors.without as { text: string; rest: string }[])("без ссылки: $text", ({ text, rest }) => {
    expect(withoutCalendarLinks(text)).toBe(rest);
  });
});

describe("правила узнавания", () => {
  it("открытый экспорт остаётся открытым, тот же экспорт с ключом — нет", () => {
    expect(isCalendarLink("https://uni.edu/events?format=ics")).toBe(false);
    expect(isCalendarLink(`https://uni.edu/events?format=ics&token=${KEY}`)).toBe(true);
    expect(isCalendarLink("https://uni.edu/ical/")).toBe(false);
    expect(isCalendarLink(`https://uni.edu/ical/${KEY}`)).toBe(true);
    expect(isCalendarLink("https://it.uni.edu/help/ical/how-to-subscribe")).toBe(false);
    expect(isCalendarLink("https://uni.edu/export/ics/semester-2026-autumn-schedule.ics")).toBe(false);
  });

  it("проверяется каждая ссылка сообщения", () => {
    expect(
      hasCalendarLink(`сначала https://www.uni.edu/ical/ а потом webcal://cal.example.org/feed?key=${KEY}`),
    ).toBe(true);
  });

  it("после вырезания ссылки остальной текст на месте, а чужие ссылки не трогаются", () => {
    const rest = withoutCalendarLinks(
      `Вот https://calendar.yandex.ru/export/ics.xml?private_token=${KEY} и https://uni.edu/ical/ — подключи`,
    );
    expect(rest).toBe("Вот и https://uni.edu/ical/ — подключи");
    expect(rest).not.toContain(KEY);
  });
});
