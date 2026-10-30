import { describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { disagreementNote, mergedSources } from "./sources";

describe("склейка источников: подписи пары (FR-054)", () => {
  it("один источник — строки источников нет; склеенная пара — названия по порядку, основной первым", () => {
    expect(mergedSources(eventFixture())).toBeUndefined();
    const merged = eventFixture({
      sources: [
        { connectionId: "a", title: "Кабинет" },
        { connectionId: "b", title: "Календарь" },
      ],
    });
    expect(mergedSources(merged)).toEqual(["Кабинет", "Календарь"]);
  });

  it("расхождение полей — «Источники расходятся: …», слова сервера не переписываются", () => {
    const event = eventFixture({ disagreement: { kind: "location", detail: "«Календарь» — ауд. 305" } });
    expect(disagreementNote(event)).toBe("Источники расходятся: «Календарь» — ауд. 305");
  });

  it("пары нет в основном источнике — слова сервера без приставки «расходятся»", () => {
    const detail = "Нет в основном источнике «Кабинет»: возможно, пару перенесли или отменили";
    expect(disagreementNote(eventFixture({ disagreement: { kind: "not_in_primary", detail } }))).toBe(detail);
  });

  it("без расхождения строки нет", () => {
    expect(disagreementNote(eventFixture())).toBeNull();
  });
});
