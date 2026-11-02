import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { ConflictWarning } from "./conflict-warning";

const label = "Скрыть предупреждение о пересечениях";
const conflict = eventFixture({ id: "first", conflictsWith: ["second"] });
afterEach(() => window.localStorage.clear());

describe("Предупреждение о пересечениях", () => {
  it("закрывается кнопкой и помнит известные конфликты после повторного открытия и обновления источника", async () => {
    const { unmount } = render(<ConflictWarning userId="one" events={[conflict]} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    expect(screen.queryByText(/пересекается по времени/)).toBeNull();
    unmount();
    render(
      <ConflictWarning
        userId="one"
        events={[{ ...conflict, provenance: { ...conflict.provenance, fetchedAt: "2026-11-02T12:00:00Z" } }]}
      />,
    );
    expect(screen.queryByRole("button", { name: label })).toBeNull();
  });

  it("новый партнёр или перенос пары возвращает предупреждение; исчезновение старого конфликта — нет", async () => {
    const { rerender } = render(
      <ConflictWarning userId="one" events={[{ ...conflict, conflictsWith: ["second", "third"] }]} />,
    );
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(<ConflictWarning userId="one" events={[]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" events={[conflict]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" events={[{ ...conflict, conflictsWith: ["second", "new"] }]} />);
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(<ConflictWarning userId="one" events={[{ ...conflict, startsAt: "2026-10-27T08:40:00Z" }]} />);
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });

  it("закрытие принадлежит только текущему пользователю", async () => {
    const { unmount } = render(<ConflictWarning userId="one" events={[conflict]} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    unmount();
    render(<ConflictWarning userId="two" events={[conflict]} />);
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });
});
