/**
 * Панели «Учёбы» в моках (D-039): конфиги панелей двух вымышленных вузов и данные, которые сервер
 * строит по ним. Данные оценок берутся из тех же сидов, что и экран «Оценки» (`seedGrades`), поэтому панель и
 * раздел не рассказывают разное. Рейтинг и профиль — синтетические: имена вымышлены, настоящих студентов нет.
 */
import type { components } from "@/api/schema.gen";
import { seedGrades } from "./data-seed";
import type { MockDb, MockPanel, MockSpace } from "./db";

type S = components["schemas"];
type Point = S["GradePointOut"];

const ICON = "grade" as const;

function spec(
  type: S["PanelOut"]["type"],
  title: string,
  icon: S["PanelOut"]["spec"]["icon"],
  dataset: S["Dataset"],
  view: Record<string, unknown>,
): S["PanelOut"]["spec"] {
  return { v: 1, type, title, icon, dataset, view } as S["PanelOut"]["spec"];
}

function panel(
  space: MockSpace,
  key: string,
  position: number,
  specValue: S["PanelOut"]["spec"],
  extra: Partial<MockPanel>,
  at: string,
): MockPanel {
  return {
    id: `panel-${space.universityId}-${key}`,
    key,
    type: specValue.type,
    spec: specValue,
    status: "active",
    visible: true,
    hiddenInAll: false,
    position,
    origin: "system",
    sourceId: null,
    createdAt: at,
    updatedAt: at,
    spaceId: space.id,
    ...extra,
  };
}

const GRADES: S["Dataset"] = { kind: "grades", board: null, courseId: null };

/** Панели вуза, у которого есть данные: оценки у обоих, профиль и рейтинг — у ВШЭ. */
export function seedPanelWorld(db: MockDb, iso: (minutesAgo: number) => string): void {
  const hse = db.spaces.find((space) => space.universityId === "u-hse");
  const cu = db.spaces.find((space) => space.universityId === "u-cu");
  const at = iso(60 * 24 * 3);
  db.panels = [];
  if (hse) {
    db.panels.push(
      panel(
        hse,
        "grades-overview",
        0,
        spec("kpi", "Успеваемость", ICON, GRADES, { metrics: ["count", "latest", "best"] }),
        {},
        at,
      ),
      panel(
        hse,
        "grades-chart",
        1,
        spec("chart", "Оценки", "chart", GRADES, { variant: "line", x: "date", limit: 24 }),
        {},
        at,
      ),
      panel(
        hse,
        "ranking:potok-1a2b",
        2,
        spec(
          "ranking",
          "Рейтинг потока",
          "trophy",
          { kind: "ranking", board: "Рейтинг потока", courseId: null },
          { top: 5, highlightMe: true },
        ),
        {},
        at,
      ),
      panel(
        hse,
        "student-profile",
        3,
        spec("profile", "Профиль", "idCard", { kind: "profile", board: null, courseId: null }, {}),
        {},
        at,
      ),
      panel(
        hse,
        "recent-grades",
        4,
        spec("list", "Последние оценки", "grade", GRADES, { limit: 5 }),
        { origin: "agent", hiddenInAll: true },
        iso(60 * 24),
      ),
      panel(
        hse,
        "grades-by-course",
        5,
        spec("chart", "Оценки по предметам", "chart", GRADES, { variant: "bar", x: "course", limit: 12 }),
        { origin: "agent", status: "proposed", visible: false },
        iso(30),
      ),
    );
  }
  if (cu) {
    db.panels.push(
      panel(
        cu,
        "grades-overview",
        0,
        spec("kpi", "Успеваемость", ICON, GRADES, { metrics: ["count", "latest", "best"] }),
        {},
        at,
      ),
      panel(
        cu,
        "grades-chart",
        1,
        spec("chart", "Оценки", "chart", GRADES, { variant: "bar", x: "date", limit: 24 }),
        {},
        at,
      ),
    );
  }
}

// ---------- Данные панели ----------

function numbers(value: string, scale: string | null): { valueNum: number | null; scaleMax: number | null } {
  const valueNum = Number(value.replace(",", "."));
  const max = scale ? Number((scale.match(/\d+/) ?? [])[0]) : Number.NaN;
  return Number.isFinite(valueNum) && Number.isFinite(max) && valueNum <= max
    ? { valueNum, scaleMax: max }
    : { valueNum: null, scaleMax: null };
}

function gradePoints(db: MockDb, spaceId: string): Point[] {
  // Панели «Оценки» строятся по оценкам за работы: накопленное, итоги и сводные показатели — свои блоки (D-043).
  return seedGrades(db)
    .filter((grade) => grade.provenance.spaceId === spaceId && (grade.layer ?? "item") === "item")
    .map((grade) => ({
      id: grade.id,
      title: grade.title,
      course: grade.courseTitle,
      courseId: null,
      value: grade.value,
      scale: grade.scale,
      ...numbers(grade.value, grade.scale),
      gradedAt: grade.gradedAt,
    }));
}

const byDateDesc = (a: Point, b: Point) => Date.parse(b.gradedAt ?? "") - Date.parse(a.gradedAt ?? "");

function gradesData(db: MockDb, item: MockPanel): S["GradesData"] {
  const all = gradePoints(db, item.spaceId).sort(byDateDesc);
  const numeric = all.filter((point) => point.valueNum !== null);
  const view = (item.spec.view ?? {}) as { limit?: number; x?: string };
  const limit = view.limit ?? 20;
  // Самая частая шкала: оценки разных шкал между собой не сравниваются.
  const counts = new Map<number, number>();
  for (const point of numeric)
    counts.set(point.scaleMax as number, (counts.get(point.scaleMax as number) ?? 0) + 1);
  const common = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  const best = numeric
    .filter((point) => point.scaleMax === common)
    .sort((a, b) => (b.valueNum as number) - (a.valueNum as number))[0];
  let points: Point[] = [];
  if (item.type === "chart") {
    const dated = numeric.filter((point) => (view.x === "course" ? true : point.gradedAt !== null));
    const newest = dated.slice(0, limit);
    points = view.x === "course" ? newest : [...newest].reverse();
  } else if (item.type !== "kpi") {
    points = all.slice(0, limit);
  }
  return {
    kind: "grades",
    total: all.length,
    numeric: numeric.length,
    points,
    latest: all.find((point) => point.gradedAt !== null) ?? all[0] ?? null,
    best: best ?? null,
  };
}

const RANKING_ROWS: S["RankingRowData"][] = [
  {
    position: 1,
    label: "Участник 1",
    score: "9,31",
    scoreNum: 9.31,
    isMe: false,
    total: 140,
    period: "Осень 2026",
  },
  {
    position: 2,
    label: "Участник 2",
    score: "9,12",
    scoreNum: 9.12,
    isMe: false,
    total: 140,
    period: "Осень 2026",
  },
  {
    position: 3,
    label: "Участник 3",
    score: "8,97",
    scoreNum: 8.97,
    isMe: false,
    total: 140,
    period: "Осень 2026",
  },
  {
    position: 4,
    label: "Участник 4",
    score: "8,90",
    scoreNum: 8.9,
    isMe: false,
    total: 140,
    period: "Осень 2026",
  },
  {
    position: 5,
    label: "Участник 5",
    score: "8,84",
    scoreNum: 8.84,
    isMe: false,
    total: 140,
    period: "Осень 2026",
  },
  { position: 12, label: "Вы", score: "8,42", scoreNum: 8.42, isMe: true, total: 140, period: "Осень 2026" },
];

const PROFILE_FIELDS: S["ProfileFieldData"][] = [
  { field: "name", label: "ФИО", value: "Анна Тестова", group: null },
  { field: "group", label: "Группа", value: "БИ-21", group: "Обучение" },
  { field: "program", label: "Программа", value: "Бизнес-информатика", group: "Обучение" },
  { field: "year", label: "Курс", value: "3", group: "Обучение" },
  { field: "form", label: "Форма обучения", value: "Очная", group: "Обучение" },
  { field: "status", label: "Статус", value: "Учится", group: "Обучение" },
];

/** Данные панели, как их собрал бы сервер; `null` — панель выключена студентом или отклонена. */
export function panelData(db: MockDb, item: MockPanel): S["PanelOut"]["data"] {
  const shown = item.status === "proposed" || (item.status === "active" && item.visible);
  if (!shown) return null;
  switch (item.type) {
    case "ranking": {
      const view = (item.spec.view ?? {}) as { top?: number };
      const rows = RANKING_ROWS.filter((row) => row.isMe || row.position <= (view.top ?? 10));
      return {
        kind: "ranking",
        board: "Рейтинг потока",
        total: RANKING_ROWS.length,
        participants: 140,
        period: "Осень 2026",
        rows,
      };
    }
    case "profile":
      return { kind: "profile", total: PROFILE_FIELDS.length, fields: PROFILE_FIELDS };
    default:
      return gradesData(db, item);
  }
}
