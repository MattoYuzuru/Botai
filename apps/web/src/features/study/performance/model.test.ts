import { describe, expect, it } from "vitest";
import type { Grade, StudyUpdate } from "@/features/data/types";
import { provenanceFixture } from "@/test/data-fixtures";
import {
  activeCourses,
  courseKey,
  fillOf,
  gradeFromUpdate,
  groupCourses,
  headlineOf,
  layerOf,
  NO_COURSE,
  scoreLabel,
  scoreText,
  sortUpdates,
  stripOf,
  summariesOf,
  updateTitle,
  zachetkaOf,
} from "./model";

const day = (n: number) => `2026-10-${String(n).padStart(2, "0")}T10:00:00.000Z`;

function grade(patch: Partial<Grade> & { id: string }): Grade {
  return {
    title: patch.id,
    value: "8",
    scale: "10 баллов",
    weight: null,
    courseTitle: "Математический анализ",
    gradedAt: day(10),
    provenance: provenanceFixture({ spaceId: "hse" }),
    valueNum: 8,
    scaleMax: 10,
    layer: "item",
    ...patch,
  };
}

describe("значение и шкала словами", () => {
  it("число с максимумом — «8 из 10»; десятичная запятая остаётся как у источника", () => {
    expect(scoreLabel({ value: "8", scale: "10 баллов", scaleMax: 10 })).toBe("8 из 10");
    expect(scoreText({ value: "7,5", scale: null, scaleMax: 10 })).toEqual({ value: "7,5", suffix: "из 10" });
    expect(scoreLabel({ value: "4,6", scale: null, scaleMax: 5 })).toBe("4,6 из 5");
  });

  it("без максимума — шкала источника; слово без шкалы — как есть; уже «8 из 10» не дополняется", () => {
    expect(scoreLabel({ value: "86", scale: "100 баллов", scaleMax: null })).toBe("86 · 100 баллов");
    expect(scoreLabel({ value: "зачтено", scale: null, scaleMax: null })).toBe("зачтено");
    expect(scoreLabel({ value: "зачтено", scale: "зачтено" })).toBe("зачтено");
    expect(scoreLabel({ value: "8 из 10", scale: null, scaleMax: 10 })).toBe("8 из 10");
    expect(scoreLabel({ value: "86%", scale: null, scaleMax: 100 })).toBe("86%");
  });

  it("доля полосы — только из пары чисел одной записи, в пределах 0…1", () => {
    expect(fillOf({ valueNum: 8, scaleMax: 10 })).toBe(0.8);
    expect(fillOf({ valueNum: 12, scaleMax: 10 })).toBe(1);
    expect(fillOf({ valueNum: 8, scaleMax: null })).toBeNull();
    expect(fillOf({ valueNum: null, scaleMax: 10 })).toBeNull();
    expect(fillOf({ valueNum: 1, scaleMax: 0 })).toBeNull();
    expect(fillOf({})).toBeNull();
  });
});

describe("предметы по слоям", () => {
  const rows = [
    grade({ id: "hw1", gradedAt: day(1) }),
    grade({ id: "hw2", gradedAt: day(5), value: "зачтено", valueNum: null, scaleMax: null, scale: null }),
    grade({ id: "cur", layer: "current", value: "7,6", valueNum: 7.6, gradedAt: day(6), period: "1 модуль" }),
    grade({ id: "legacy", layer: null, gradedAt: day(3) }),
    grade({ id: "fin-old", layer: "final", gradedAt: day(2), period: "1 модуль" }),
    grade({ id: "alg", courseTitle: "Алгоритмы", gradedAt: day(20) }),
    grade({ id: "gpa", layer: "summary", courseTitle: null, title: "GPA", value: "4,6", scaleMax: 5 }),
    grade({ id: "free", courseTitle: null, gradedAt: day(25) }),
    grade({
      id: "cu",
      courseTitle: "Математический анализ",
      provenance: provenanceFixture({ spaceId: "cu" }),
    }),
  ];

  it("слой `null` — оценка за работу; сводные показатели к предметам не относятся", () => {
    expect(layerOf({ layer: null })).toBe("item");
    const courses = groupCourses(rows);
    expect(
      courses.flatMap((course) => [...course.items, ...course.current, ...course.finals]).map((g) => g.id),
    ).not.toContain("gpa");
    const math = courses.find((course) => course.key === courseKey(rows[0] as Grade));
    expect(math?.items.map((g) => g.id)).toEqual(["hw2", "legacy", "hw1"]);
    expect(math?.current.map((g) => g.id)).toEqual(["cur"]);
    expect(math?.finals.map((g) => g.id)).toEqual(["fin-old"]);
  });

  it("одноимённые предметы разных вузов — разные; свежие первыми, «Без предмета» последним", () => {
    const courses = groupCourses(rows);
    expect(courses.map((course) => course.title)).toEqual([
      "Алгоритмы",
      "Математический анализ",
      "Математический анализ",
      null,
    ]);
    expect(new Set(courses.map((course) => course.spaceId))).toEqual(new Set(["hse", "cu"]));
    expect(NO_COURSE).toBe("Без предмета");
  });

  it("главный результат: итог, иначе накопленное, иначе последняя работа — ничего не считается", () => {
    const [alg, , , free] = groupCourses(rows);
    const math = groupCourses(rows).find((course) => course.current.length > 0);
    expect(headlineOf(math as never)).toMatchObject({ label: "Итог", grade: { id: "fin-old" } });
    const noFinal = groupCourses(rows.filter((row) => row.id !== "fin-old")).find(
      (c) => c.current.length > 0,
    );
    expect(headlineOf(noFinal as never)).toMatchObject({ label: "Накоплено", grade: { id: "cur" } });
    expect(headlineOf(alg as never)).toMatchObject({ label: "Последняя оценка", grade: { id: "alg" } });
    expect(free?.title).toBeNull();
  });

  it("карточки — предметы с работами или накопленным; предмет только с итогом живёт в зачётке", () => {
    const onlyFinal = grade({ id: "hist", courseTitle: "История", layer: "final", period: "весна" });
    const courses = groupCourses([...rows, onlyFinal]);
    expect(activeCourses(courses).map((course) => course.title)).not.toContain("История");
    expect(courses.map((course) => course.title)).toContain("История");
  });

  it("полоса: до восьми последних работ, слева старые; оценка без числа — текстом", () => {
    const many = Array.from({ length: 10 }, (_, index) =>
      grade({
        id: `w${index}`,
        gradedAt: day(index + 1),
        valueNum: index,
        scaleMax: 10,
        value: String(index),
      }),
    );
    const [course] = groupCourses(many);
    const strip = stripOf(course?.items ?? []);
    expect(strip).toHaveLength(8);
    expect(strip.map((item) => item.id)).toEqual(["w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9"]);
    expect(strip[7]).toMatchObject({ fill: 0.9, text: "9 из 10" });
    const math = groupCourses(rows).find((c) => c.current.length > 0);
    expect(stripOf(math?.items ?? []).find((item) => item.id === "hw2")).toMatchObject({
      fill: null,
      text: "зачтено",
    });
  });
});

describe("зачётка и показатели", () => {
  it("итоги по периодам вуза: свежие периоды первыми, «Без периода» последним, предметы по алфавиту", () => {
    const rows = [
      grade({
        id: "a",
        layer: "final",
        courseTitle: "Линейная алгебра",
        period: "Осень 2025",
        gradedAt: day(1),
      }),
      grade({ id: "b", layer: "final", courseTitle: "Английский", period: "Весна 2026", gradedAt: day(9) }),
      grade({ id: "c", layer: "final", courseTitle: "Алгебра", period: "Весна 2026", gradedAt: day(8) }),
      grade({ id: "d", layer: "final", courseTitle: "Физкультура", period: null, gradedAt: day(30) }),
      grade({
        id: "e",
        layer: "final",
        courseTitle: "Python",
        period: "Весна 2026",
        gradedAt: day(7),
        provenance: provenanceFixture({ spaceId: "cu" }),
      }),
      grade({ id: "item", layer: "item" }),
    ];
    const periods = zachetkaOf(rows);
    expect(periods.map((p) => [p.spaceId, p.period])).toEqual([
      ["hse", "Весна 2026"],
      ["cu", "Весна 2026"],
      ["hse", "Осень 2025"],
      ["hse", null],
    ]);
    expect(periods[0]?.grades.map((g) => g.courseTitle)).toEqual(["Алгебра", "Английский"]);
  });

  it("сводные показатели — только слой summary, свежие первыми", () => {
    const rows = [
      grade({ id: "gpa", layer: "summary", gradedAt: day(1) }),
      grade({ id: "rating", layer: "summary", gradedAt: day(9) }),
      grade({ id: "item" }),
    ];
    expect(summariesOf(rows).map((g) => g.id)).toEqual(["rating", "gpa"]);
  });
});

describe("«Новое»", () => {
  const update = (patch: Partial<StudyUpdate> & { id: string }): StudyUpdate => ({
    kind: "grade",
    title: "Контрольная №2",
    courseTitle: "Математический анализ",
    value: "8",
    scale: "10 баллов",
    valueNum: 8,
    scaleMax: 10,
    layer: "item",
    state: null,
    stateText: null,
    url: "https://lk.fakeuni.test/grades/1",
    changedAt: day(10),
    provenance: provenanceFixture({ spaceId: "hse" }),
    ...patch,
  });

  it("свежие первыми, порядок одинакового времени стабилен", () => {
    const rows = [update({ id: "b" }), update({ id: "c", changedAt: day(12) }), update({ id: "a" })];
    expect(sortUpdates(rows).map((row) => row.id)).toEqual(["c", "a", "b"]);
  });

  it("подпись без названия — по слою и виду", () => {
    expect(updateTitle(update({ id: "x", title: null, layer: "final" }))).toBe("Итог по предмету");
    expect(updateTitle(update({ id: "x", title: null, layer: "current" }))).toBe("Накопленная оценка");
    expect(updateTitle(update({ id: "x", title: " ", kind: "deadline" }))).toBe("Задание");
    expect(updateTitle(update({ id: "x" }))).toBe("Контрольная №2");
  });

  it("оценка из записи «Нового» попадает в тот же предмет, что и в списке оценок", () => {
    const fromUpdate = gradeFromUpdate(update({ id: "g1" }));
    expect(fromUpdate).toMatchObject({ id: "g1", value: "8", scaleMax: 10, layer: "item" });
    expect(courseKey(fromUpdate)).toBe(courseKey(grade({ id: "other" })));
  });
});
