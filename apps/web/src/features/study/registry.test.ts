import { describe, expect, it } from "vitest";
import { isStudyKind, RETIRED_STUDY_KINDS, SECTION_REGISTRY, visibleSections } from "./registry";

describe("SECTION_REGISTRY", () => {
  it("порядок фиксирован: Предметы, Оценки, Преподаватели, Аудитории, Новости", () => {
    expect(SECTION_REGISTRY.map((def) => def.title)).toEqual([
      "Предметы",
      "Оценки",
      "Преподаватели",
      "Аудитории",
      "Новости",
    ]);
  });

  it("у каждого вида одна иконка реестра и свой адрес экрана", () => {
    expect(new Set(SECTION_REGISTRY.map((def) => def.route)).size).toBe(SECTION_REGISTRY.length);
    for (const def of SECTION_REGISTRY) expect(def.route).toBe(`/study/${def.kind}`);
    expect(isStudyKind("grades")).toBe(true);
    expect(isStudyKind("tabs")).toBe(false);
  });

  it("«Документы и вложения» и «Источники» — не разделы, а прежние места (D-013)", () => {
    expect(isStudyKind("documents")).toBe(false);
    expect(isStudyKind("connections")).toBe(false);
    expect([...RETIRED_STUDY_KINDS]).toEqual(["connections", "documents"]);
  });
});

describe("visibleSections", () => {
  it("показывает только виды с данными, в порядке реестра; чужие виды пропускаются", () => {
    const result = visibleSections([
      { kind: "news", count: 2 },
      { kind: "courses", count: 6 },
      { kind: "grades", count: 0 },
      { kind: "scheduleEvents", count: 40 },
      { kind: "deadlines", count: 7 },
      { kind: "documents", count: 3 },
    ]);
    expect(result.map((row) => [row.def.kind, row.count])).toEqual([
      ["courses", 6],
      ["news", 2],
    ]);
  });

  it("у пространства без данных список разделов пуст", () => {
    expect(visibleSections([])).toEqual([]);
  });
});
