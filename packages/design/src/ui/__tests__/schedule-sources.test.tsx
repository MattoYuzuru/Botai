import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScheduleDay, type ScheduleItem } from "../schedule/schedule-day";
import { ScheduleGrid } from "../schedule/schedule-grid";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 6, 0); // вторник, 09:00 МСК
const at = (h: number, m = 0) => Date.UTC(2026, 9, 27, h - 3, m);

const item = (patch: Partial<ScheduleItem> = {}): ScheduleItem => ({
  id: "a",
  start: at(11),
  end: at(12, 20),
  title: "Алгоритмы",
  spaceId: "hse",
  ...patch,
});

describe("Карточка пары: источники и расхождение (FR-054)", () => {
  it("склеенная пара подписана источниками через «·», основной первым", () => {
    render(
      <ScheduleDay
        date={NOW}
        items={[item({ sources: ["Кабинет", "Календарь"] })]}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    const row = screen.getByRole("listitem");
    const line = row.querySelector("[data-part=sources]");
    expect(line).toHaveTextContent("Источники: Кабинет · Календарь");
    expect(line?.querySelector("svg")).toHaveAttribute("data-icon", "source");
    expect(row.querySelector("[data-part=disagreement]")).toBeNull();
  });

  it("один источник и пустой список — строки источников нет", () => {
    render(
      <ScheduleDay
        date={NOW}
        items={[item({ id: "one", sources: ["Кабинет"] }), item({ id: "none", title: "Право" })]}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    expect(document.querySelector("[data-part=sources]")).toBeNull();
  });

  it("расхождение — спокойная строка с иконкой и словами, без рамки и заливки карточки", () => {
    render(
      <ScheduleDay
        date={NOW}
        items={[item({ note: "Источники расходятся: «Календарь» — ауд. 305" })]}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    const row = screen.getByRole("listitem");
    const note = row.querySelector("[data-part=disagreement]");
    expect(note).toHaveTextContent("Источники расходятся: «Календарь» — ауд. 305");
    expect(note?.querySelector("svg")).toHaveAttribute("data-icon", "warning");
    expect(note).toHaveClass("text-warning-fg");
    expect(note?.parentElement).toHaveClass("type-caption");
    // это не пересечение вузов: рамка карточки остаётся обычной
    expect(row.firstElementChild).not.toHaveAttribute("data-conflict");
    expect(row.firstElementChild).not.toHaveClass("border-warning");
  });

  it("длинное название источника переносится, а не вытесняет строку за край карточки", () => {
    render(
      <ScheduleDay
        date={NOW}
        items={[item({ sources: ["Единый личный кабинет студента", "Календарь расписания группы"] })]}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    const text = document.querySelector("[data-part=sources] > span:last-child");
    expect(text).toHaveClass("min-w-0", "break-words");
  });

  it("в колонке недели то же: источники и расхождение под деталями", () => {
    const days = Array.from({ length: 7 }, (_, index) => ({
      date: Date.UTC(2026, 9, 26 + index, 9),
      items:
        index === 1 ? [item({ sources: ["Кабинет", "Календарь"], note: "Нет в основном источнике" })] : [],
    }));
    render(<ScheduleGrid days={days} state="ok" nowMs={NOW} timeZone={MSK} />);
    const card = within(screen.getByRole("region", { name: "Неделя" })).getByRole("listitem");
    expect(card.querySelector("[data-part=sources]")).toHaveTextContent("Кабинет · Календарь");
    expect(card.querySelector("[data-part=disagreement]")).toHaveTextContent("Нет в основном источнике");
  });
});
