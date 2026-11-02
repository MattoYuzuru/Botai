import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { eventFixture } from "@/test/data-fixtures";
import { ConflictWarning, conflictSignatures } from "./conflict-warning";

const label = "Скрыть предупреждение о пересечениях";
const conflict = eventFixture({ id: "first", conflictsWith: ["second"] });
const second = eventFixture({ id: "second", conflictsWith: ["first"] });
const partners = [conflict, second, eventFixture({ id: "third" }), eventFixture({ id: "new" })];
afterEach(() => window.localStorage.clear());

describe("Предупреждение о пересечениях", () => {
  it("закрывается кнопкой и помнит известные конфликты после повторного открытия и обновления источника", async () => {
    const { unmount } = render(<ConflictWarning userId="one" partnerEvents={partners} events={[conflict]} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    expect(screen.queryByText(/пересекается по времени/)).toBeNull();
    unmount();
    render(
      <ConflictWarning
        userId="one"
        partnerEvents={partners}
        events={[{ ...conflict, provenance: { ...conflict.provenance, fetchedAt: "2026-11-02T12:00:00Z" } }]}
      />,
    );
    expect(screen.queryByRole("button", { name: label })).toBeNull();
  });

  it("новый партнёр или перенос пары возвращает предупреждение; исчезновение старого конфликта — нет", async () => {
    const { rerender } = render(
      <ConflictWarning
        userId="one"
        partnerEvents={partners}
        events={[{ ...conflict, conflictsWith: ["second", "third"] }]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(<ConflictWarning userId="one" partnerEvents={partners} events={[]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" partnerEvents={partners} events={[conflict]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(
      <ConflictWarning
        userId="one"
        partnerEvents={partners}
        events={[{ ...conflict, conflictsWith: ["second", "new"] }]}
      />,
    );
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(
      <ConflictWarning
        userId="one"
        partnerEvents={partners}
        events={[{ ...conflict, startsAt: "2026-10-27T08:40:00Z" }]}
      />,
    );
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });

  it("закрытие принадлежит только текущему пользователю", async () => {
    const { unmount } = render(<ConflictWarning userId="one" partnerEvents={partners} events={[conflict]} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    unmount();
    render(<ConflictWarning userId="two" partnerEvents={partners} events={[conflict]} />);
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });

  it("закрытие в одном вузе действует для всех вузов и противоположной стороны той же пары", async () => {
    const { rerender } = render(
      <ConflictWarning userId="one" events={[conflict]} partnerEvents={partners} />,
    );
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(<ConflictWarning userId="one" events={[second, conflict]} partnerEvents={partners} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" events={[second]} partnerEvents={partners} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(
      <ConflictWarning
        userId="one"
        events={[conflict]}
        partnerEvents={[conflict, { ...second, endsAt: "2026-10-27T10:00:00Z" }]}
      />,
    );
    expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  });

  it("не показывает временное предупреждение, пока не пришла вторая сторона пересечения", async () => {
    const { rerender } = render(<ConflictWarning userId="one" events={[conflict]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" events={[conflict]} partnerEvents={partners} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    rerender(<ConflictWarning userId="one" events={[conflict]} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
    rerender(<ConflictWarning userId="one" events={[conflict]} partnerEvents={partners} />);
    expect(screen.queryByRole("button", { name: label })).toBeNull();
  });

  it("ограничивает сохранённую историю 4000 сигнатурами и сохраняет новое закрытие", async () => {
    const key = "botai.schedule.dismissed-conflicts.one";
    window.localStorage.setItem(
      key,
      JSON.stringify(Array.from({ length: 4010 }, (_, index) => `old-${index}`)),
    );
    render(<ConflictWarning userId="one" events={[conflict]} partnerEvents={partners} />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    const stored = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    expect(stored).toHaveLength(4000);
    expect(stored).toContain(conflictSignatures([conflict], partners)?.[0]);
  });
});
