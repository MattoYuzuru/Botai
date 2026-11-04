import { describe, expect, it } from "vitest";
import type { GradePointOut, GradesData, PanelOut } from "@/api/panels";
import {
  chartGroups,
  gradeText,
  groupBySpace,
  layoutPanels,
  panelsForScope,
  profileView,
  rankTotal,
  readPanel,
} from "./model";

const space = (id: string, hue = 262.2): PanelOut["space"] => ({
  id,
  title: `Вуз ${id}`,
  shortTitle: id.toUpperCase(),
  accentHue: hue,
});

function point(partial: Partial<GradePointOut> & { id: string }): GradePointOut {
  return {
    title: "Работа",
    course: "Предмет",
    courseId: null,
    value: "8",
    scale: "10 баллов",
    valueNum: 8,
    scaleMax: 10,
    gradedAt: "2026-10-01T10:00:00Z",
    ...partial,
  };
}

const grades = (points: GradePointOut[]): GradesData => ({
  kind: "grades",
  total: points.length,
  numeric: points.filter((p) => p.valueNum !== null).length,
  points,
  latest: points[0] ?? null,
  best: null,
});

function panel(partial: Partial<PanelOut> & { id: string; type: PanelOut["type"] }): PanelOut {
  const { type } = partial;
  return {
    key: partial.id,
    spec: {
      v: 1,
      type,
      title: partial.id,
      icon: "grade",
      dataset: { kind: "grades" },
      view: {},
    } as PanelOut["spec"],
    status: "active",
    visible: true,
    hiddenInAll: false,
    position: 0,
    origin: "system",
    sourceId: null,
    createdAt: "2026-10-01T00:00:00Z",
    updatedAt: "2026-10-01T00:00:00Z",
    space: space("a"),
    data: null,
    ...partial,
  };
}

describe("readPanel: что умеем рисовать", () => {
  it("известный тип с данными той же формы читается", () => {
    const body = readPanel(panel({ id: "k", type: "kpi", data: grades([point({ id: "1" })]) }));
    expect(body?.type).toBe("kpi");
  });

  it("неизвестный тип скрывается, а не падает", () => {
    const odd = { ...panel({ id: "x", type: "kpi" }), type: "heatmap" } as unknown as PanelOut;
    expect(readPanel(odd)).toBeNull();
  });

  it("spec другого типа и данные другой формы — панель скрыта", () => {
    const mismatch = panel({ id: "m", type: "chart" });
    mismatch.spec = { ...mismatch.spec, type: "table" } as PanelOut["spec"];
    expect(readPanel(mismatch)).toBeNull();
    const wrongData = panel({
      id: "w",
      type: "ranking",
      data: grades([point({ id: "1" })]),
    });
    expect(readPanel(wrongData)).toBeNull();
    const broken = panel({ id: "b", type: "list", data: { kind: "grades", points: "oops" } as never });
    expect(readPanel(broken)).toBeNull();
  });

  it("выключенная панель (data: null) читается: тело ей не нужно", () => {
    expect(readPanel(panel({ id: "off", type: "profile", visible: false, data: null }))).not.toBeNull();
  });
});

describe("область экрана", () => {
  const panels = [
    panel({ id: "a1", type: "kpi", space: space("a"), position: 1 }),
    panel({ id: "a0", type: "chart", space: space("a"), position: 0 }),
    panel({ id: "b0", type: "kpi", space: space("b"), position: 0 }),
    panel({ id: "a-hidden", type: "list", space: space("a"), hiddenInAll: true, position: 2 }),
  ];

  it("все вузы: порядок групп задаёт экран, панели с hiddenInAll пропущены", () => {
    const result = panelsForScope(panels, { spaceIds: ["b", "a"], grouped: true });
    expect(result.map((p) => p.id)).toEqual(["b0", "a0", "a1"]);
    expect(groupBySpace(result).map((g) => g.space.id)).toEqual(["b", "a"]);
  });

  it("один вуз: только его панели, hiddenInAll не действует; чужие отброшены", () => {
    const result = panelsForScope(panels, { spaceIds: ["a"], grouped: false });
    expect(result.map((p) => p.id)).toEqual(["a0", "a1", "a-hidden"]);
  });

  it("раскладка: показатели, основные и боковые панели", () => {
    const layout = layoutPanels([
      panel({ id: "p", type: "profile" }),
      panel({ id: "c", type: "chart" }),
      panel({ id: "k", type: "kpi" }),
      panel({ id: "r", type: "ranking" }),
    ]);
    expect(layout.band.map((p) => p.id)).toEqual(["k"]);
    expect(layout.main.map((p) => p.id)).toEqual(["c"]);
    expect(layout.side.map((p) => p.id)).toEqual(["p", "r"]);
  });
});

describe("оценки как у источника", () => {
  it("«8 / 10» только если сервер дал верх шкалы; без него — значение как есть", () => {
    expect(gradeText(point({ id: "1", value: "8,5", scaleMax: 10 }))).toBe("8,5 / 10");
    expect(gradeText(point({ id: "2", value: "зачтено", valueNum: null, scaleMax: null }))).toBe("зачтено");
  });

  it("график: своя шкала — свой график; оценки без числа или шкалы не рисуются, средних нет", () => {
    const data = grades([
      point({ id: "1", valueNum: 8, scaleMax: 10, gradedAt: "2026-10-01T10:00:00Z" }),
      point({ id: "2", valueNum: 9, scaleMax: 10, gradedAt: "2026-10-08T10:00:00Z" }),
      point({ id: "3", value: "86", valueNum: 86, scaleMax: 100, scale: "100 баллов" }),
      point({ id: "4", value: "зачтено", valueNum: null, scaleMax: null, scale: null }),
      point({ id: "5", value: "5", valueNum: 5, scaleMax: null }),
    ]);
    const groups = chartGroups(data, { variant: "line", x: "date", title: "Оценки" });
    expect(groups.map((g) => g.key)).toEqual(["10", "100"]);
    expect(groups[0]?.spec.series[0]?.values).toEqual([8, 9]);
    expect(groups[0]?.scaleLabel).toBe("10 баллов");
    expect(groups[1]?.spec.series[0]?.values).toEqual([86]);
    for (const group of groups) {
      expect(group.spec.series).toHaveLength(1);
      expect(group.spec.series[0]?.name).toContain(group.scaleLabel);
    }
  });

  it("по предметам подписи — названия предметов; без даты оценка на график по дате не попадает", () => {
    const data = grades([
      point({ id: "1", course: "Алгебра", gradedAt: null }),
      point({ id: "2", course: "Анализ" }),
    ]);
    expect(chartGroups(data, { variant: "bar", x: "course", title: "t" })[0]?.spec.labels).toEqual([
      "Алгебра",
      "Анализ",
    ]);
    expect(chartGroups(data, { variant: "bar", x: "date", title: "t" })[0]?.spec.labels).toHaveLength(1);
  });
});

describe("рейтинг и профиль", () => {
  it("«из скольких» — сначала у строки «Вы», затем размер, который назвал источник", () => {
    const base = { kind: "ranking", board: "b", total: 3, period: null } as const;
    const row = (position: number, isMe: boolean, total: number | null) => ({
      position,
      label: isMe ? "Вы" : `У${position}`,
      score: null,
      scoreNum: null,
      isMe,
      total,
      period: null,
    });
    expect(rankTotal({ ...base, participants: 200, rows: [row(1, false, 150), row(12, true, 140)] })).toBe(
      140,
    );
    expect(rankTotal({ ...base, participants: 200, rows: [row(1, false, 150)] })).toBe(200);
    expect(rankTotal({ ...base, participants: null, rows: [row(1, false, null)] })).toBeNull();
  });

  it("имя из профиля уходит в шапку, одинаковые подписи различаются группой", () => {
    const view = profileView({
      kind: "profile",
      total: 4,
      fields: [
        { field: "name", label: "ФИО", value: "Анна Тестова", group: null },
        { field: "a", label: "Статус", value: "Учится", group: "Обучение" },
        { field: "b", label: "Статус", value: "Активна", group: "Общежитие" },
        { field: "c", label: "Курс", value: "3", group: null },
      ],
    });
    expect(view.name).toBe("Анна Тестова");
    expect(view.fields.map((f) => f.label)).toEqual(["Статус", "Статус (Общежитие)", "Курс"]);
  });
});
