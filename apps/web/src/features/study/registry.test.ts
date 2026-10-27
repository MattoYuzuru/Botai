import { describe, expect, it } from "vitest";
import { isStudyKind, SECTION_REGISTRY, visibleSections } from "./registry";

describe("SECTION_REGISTRY", () => {
  it("порядок фиксирован: Предметы, Оценки, Преподаватели, Аудитории, Новости, Документы, Подключения", () => {
    expect(SECTION_REGISTRY.map((def) => def.title)).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
      "Документы",
      "Подключения",
    ]);
  });

  it("у каждого вида одна иконка реестра и свой адрес экрана", () => {
    expect(new Set(SECTION_REGISTRY.map((def) => def.route)).size).toBe(SECTION_REGISTRY.length);
    for (const def of SECTION_REGISTRY) expect(def.route).toBe(`/study/${def.kind}`);
    expect(isStudyKind("grades")).toBe(true);
    expect(isStudyKind("tabs")).toBe(false);
  });
});

describe("visibleSections", () => {
  it("показывает только виды с данными, в порядке реестра, а «Подключения» — всегда", () => {
    const result = visibleSections(
      [
        { kind: "news", count: 2 },
        { kind: "courses", count: 6 },
        { kind: "grades", count: 0 },
        { kind: "scheduleEvents", count: 40 },
        { kind: "deadlines", count: 7 },
      ],
      3,
    );
    expect(result.map((row) => [row.def.kind, row.count])).toEqual([
      ["courses", 6],
      ["news", 2],
      ["connections", 3],
    ]);
  });

  it("у пространства без данных остаётся только «Подключения»", () => {
    expect(visibleSections([], 1).map((row) => row.def.kind)).toEqual(["connections"]);
    expect(visibleSections([], 0).map((row) => row.def.kind)).toEqual(["connections"]);
  });
});
