import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GradeTile } from "../grade-tile/grade-tile";
import { ListRow, ListRowBody, listRowClass } from "../list-row/list-row";

describe("ListRow", () => {
  it("с onOpen — кнопка со стрелкой, нажатие открывает запись", async () => {
    const onOpen = vi.fn();
    render(
      <ListRow icon="course" title="Алгоритмы" meta="АСД-201 · Осень 2026" trailing="4" onOpen={onOpen} />,
    );
    const row = screen.getByRole("button", { name: /Алгоритмы/ });
    expect(row).toHaveTextContent("АСД-201 · Осень 2026");
    await userEvent.click(row);
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("без onOpen — обычный блок: не кнопка и не ссылка", () => {
    render(<ListRow title="Личный кабинет" meta="lk.fakeuni.test" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Личный кабинет")).toBeInTheDocument();
  });

  it("строка-ссылка собирается из listRowClass и ListRowBody; высота не меньше 56 px", () => {
    render(
      <a href="/study/grades" className={listRowClass}>
        <ListRowBody title="Оценки" trailing={5} chevron />
      </a>,
    );
    const link = screen.getByRole("link", { name: /Оценки/ });
    expect(link.className).toContain("min-h-14");
    expect(link).toHaveTextContent("5");
  });
});

describe("GradeTile", () => {
  it("показывает оценку как у источника: значение, шкалу, за что и подпись", () => {
    render(<GradeTile value="зачтено" title="Участие на семинарах" meta="вес 0,3" />);
    expect(screen.getByText("зачтено")).toBeInTheDocument();
    expect(screen.getByText("Участие на семинарах")).toBeInTheDocument();
    expect(screen.getByText("вес 0,3")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/%/);
  });

  it("с onOpen — кнопка; без него — блок", async () => {
    const onOpen = vi.fn();
    const { rerender } = render(
      <GradeTile value="8" scale="10 баллов" title="Контрольная №2" onOpen={onOpen} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Контрольная №2/ }));
    expect(onOpen).toHaveBeenCalledOnce();
    rerender(<GradeTile value="8" title="Контрольная №2" />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
