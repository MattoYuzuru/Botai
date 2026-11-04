import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { GradePointOut, PanelOut } from "@/api/panels";
import { PanelView } from "./panel-view";

const point = (id: string, partial: Partial<GradePointOut> = {}): GradePointOut => ({
  id,
  title: `Работа ${id}`,
  course: "Алгоритмы",
  courseId: null,
  value: "8,5",
  scale: "из 10",
  valueNum: 8.5,
  scaleMax: 10,
  gradedAt: "2026-10-19T07:00:00Z",
  ...partial,
});

function tablePanel(points: GradePointOut[], columns?: string[]): PanelOut {
  return {
    id: "t",
    key: "t",
    type: "table",
    spec: {
      v: 1,
      type: "table",
      title: "Оценки за семестр",
      icon: "table",
      dataset: { kind: "grades" },
      view: { columns, limit: 20 },
    } as PanelOut["spec"],
    status: "active",
    visible: true,
    hiddenInAll: false,
    position: 0,
    origin: "agent",
    sourceId: null,
    createdAt: "2026-10-01T00:00:00Z",
    updatedAt: "2026-10-01T00:00:00Z",
    space: { id: "s", title: "Вуз", shortTitle: "В", accentHue: 262 },
    data: { kind: "grades", total: points.length, numeric: points.length, points, latest: null, best: null },
  };
}

const view = (panel: PanelOut) =>
  render(<PanelView panel={panel} context={{ zone: "Europe/Moscow", preview: false }} headingLevel={3} />);

describe("реестр: таблица", () => {
  it("значение со шкалой источника, когда колонки «Шкала» нет; пустые ячейки без прочерков", () => {
    view(
      tablePanel(
        [point("1"), point("2", { course: null, valueNum: null, scaleMax: null, value: "зачтено" })],
        ["course", "value", "date"],
      ),
    );
    const table = screen.getByRole("table", { name: "Оценки за семестр" });
    expect(within(table).getByText("8,5 / 10")).toBeInTheDocument();
    expect(within(table).getByText("зачтено")).toBeInTheDocument();
    expect(table.textContent).not.toContain("—");
  });

  it("колонка «Шкала» показывает слова источника, значение без «/ 10»", () => {
    view(tablePanel([point("1")], ["title", "value", "scale"]));
    const table = screen.getByRole("table");
    expect(within(table).getByText("из 10")).toBeInTheDocument();
    expect(within(table).queryByText("8,5 / 10")).toBeNull();
  });

  it("таблица без оценок — пустое состояние, а не пустая таблица", () => {
    view(tablePanel([]));
    expect(screen.getByText("Оценок пока нет")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("панель неизвестного типа ничего не рисует", () => {
    const odd = { ...tablePanel([point("1")]), type: "heatmap" } as unknown as PanelOut;
    const { container } = view(odd);
    expect(container).toBeEmptyDOMElement();
  });

  it("показатели: подпись «из них с баллом» только когда числовые не все", () => {
    const base = tablePanel([point("1"), point("2")]);
    const kpi = (numeric: number): PanelOut =>
      ({
        ...base,
        type: "kpi",
        spec: { ...base.spec, type: "kpi", view: { metrics: ["count"] } },
        data: { kind: "grades", total: 3, numeric, points: [], latest: null, best: null },
      }) as PanelOut;
    const { unmount } = view(kpi(2));
    expect(screen.getByText("из них с баллом: 2")).toBeInTheDocument();
    unmount();
    view(kpi(3));
    expect(screen.queryByText(/из них/)).toBeNull();
  });
});
