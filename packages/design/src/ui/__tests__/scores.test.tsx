import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CourseGradeCard } from "../course-grade-card/course-grade-card";
import { DeadlineItem } from "../deadline-item/deadline-item";
import { ScoreBar, ScoreStrip } from "../score/score";
import { UpdateRow } from "../update-row/update-row";

const NOW = Date.UTC(2026, 9, 27, 12);

describe("ScoreBar", () => {
  it("ширина — доля одной записи, за пределами 0…1 — к границе; для скринридера скрыта", () => {
    const { container, rerender } = render(<ScoreBar fill={0.8} />);
    const bar = container.querySelector("[data-score-bar]") as HTMLElement;
    expect(bar).toHaveAttribute("aria-hidden", "true");
    expect((bar.firstElementChild as HTMLElement).style.width).toBe("80%");
    rerender(<ScoreBar fill={1.7} />);
    expect((bar.firstElementChild as HTMLElement).style.width).toBe("100%");
    rerender(<ScoreBar fill={Number.NaN} />);
    expect((bar.firstElementChild as HTMLElement).style.width).toBe("0%");
  });
});

describe("ScoreStrip", () => {
  const items = [
    { id: "1", fill: 0.6, text: "6 из 10", title: "Коллоквиум" },
    { id: "2", fill: null, text: "зачтено", title: "Семинары" },
    { id: "3", fill: 0, text: "0 из 10", title: null },
  ];

  it("список работ с подписью для скринридера; оценка без числа — словом источника", () => {
    render(<ScoreStrip items={items} label="Последние работы" />);
    const list = screen.getByRole("list", { name: "Последние работы" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByText("Коллоквиум: 6 из 10")).toBeInTheDocument();
    expect(within(list).getByText("Семинары: зачтено")).toBeInTheDocument();
    expect(within(list).getByText("0 из 10")).toBeInTheDocument();
    // Нулевая оценка — пустой столбик, а не выдуманная точка.
    const zero = list.querySelectorAll("[data-score-column]")[1]?.firstElementChild as HTMLElement;
    expect(zero.style.height).toBe("0%");
  });

  it("декоративная полоса внутри кнопки — без списка и скрыта от скринридера; пустая — не рисуется", () => {
    const { container, rerender } = render(<ScoreStrip items={items} label="Работы" decorative />);
    expect(screen.queryByRole("list")).toBeNull();
    expect(container.querySelector("[data-score-strip]")).toHaveAttribute("aria-hidden", "true");
    rerender(<ScoreStrip items={[]} label="Работы" />);
    expect(container.querySelector("[data-score-strip]")).toBeNull();
  });
});

describe("CourseGradeCard", () => {
  it("кнопка с именем из названия, результата источника и подписи; нажатие открывает", async () => {
    const onOpen = vi.fn();
    render(
      <CourseGradeCard
        title="Математический анализ"
        headline={{ label: "Накоплено", value: "7,6", suffix: "из 10", period: "1 модуль", fill: 0.76 }}
        caption="Проверено: 4 работы"
        strip={[{ id: "1", fill: 0.8, text: "8 из 10" }]}
        onOpen={onOpen}
      />,
    );
    const button = screen.getByRole("button", {
      name: "Математический анализ. Накоплено (1 модуль): 7,6 из 10. Проверено: 4 работы",
    });
    expect(button).toHaveTextContent("Накоплено · 1 модуль");
    expect(button.querySelector("[data-score-bar]")).not.toBeNull();
    await userEvent.click(button);
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("без результата и без полосы — только название и подпись; без onOpen — не кнопка", () => {
    render(<CourseGradeCard title="История" caption="Работ с оценкой пока нет" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("История")).toBeInTheDocument();
    expect(document.querySelector("[data-score-bar]")).toBeNull();
  });
});

describe("UpdateRow", () => {
  it("оценка: значение крупно справа; задание: статус меткой и значок «готово»", async () => {
    const onOpen = vi.fn();
    const { rerender, container } = render(
      <UpdateRow
        kind="grade"
        title="Контрольная №2"
        meta="Математический анализ"
        value="8 из 10"
        onOpen={onOpen}
      />,
    );
    const row = screen.getByRole("button", { name: /Контрольная №2/ });
    expect(row).toHaveTextContent("8 из 10");
    expect(container.querySelector('[data-icon="grade"]')).not.toBeNull();
    await userEvent.click(row);
    expect(onOpen).toHaveBeenCalledOnce();
    rerender(<UpdateRow kind="deadline" done title="ДЗ №3" status={<span>Решение прикреплено</span>} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector('[data-icon="ok"]')).not.toBeNull();
    expect(screen.getByText("Решение прикреплено")).toBeInTheDocument();
  });
});

describe("DeadlineItem: статус источника и балл", () => {
  const base = { title: "ДЗ", dueAt: NOW - 3_600_000, spaceId: "s", nowMs: NOW } as const;

  it("у сданного слова источника заменяют «Сдано»; откуда статус — пометкой; балл — меткой", () => {
    render(
      <DeadlineItem
        {...base}
        urgency="overdue"
        done
        statusText="Решение прикреплено"
        statusNote="по вашему правилу"
        score="8 из 10"
      />,
    );
    expect(screen.getByText("Решение прикреплено")).toBeInTheDocument();
    expect(screen.queryByText("Сдано")).toBeNull();
    expect(screen.getByText("по вашему правилу")).toBeInTheDocument();
    expect(screen.getByText("8 из 10")).toBeInTheDocument();
  });

  it("у несданного подпись источника стоит рядом со сроком; без неё — как раньше", () => {
    const { rerender } = render(<DeadlineItem {...base} urgency="overdue" statusText="На доработке" />);
    expect(screen.getByText("На доработке")).toBeInTheDocument();
    expect(screen.getByText("Просрочено")).toBeInTheDocument();
    rerender(<DeadlineItem {...base} urgency="overdue" done />);
    expect(screen.getByText("Сдано")).toBeInTheDocument();
  });
});
