import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ScheduleItem } from "../schedule/schedule-day";
import { ScheduleGrid, type ScheduleGridDay } from "../schedule/schedule-grid";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 14, 40); // вторник, 17:40 МСК
const at = (day: number, h: number, m = 0) => Date.UTC(2026, 9, day, h - 3, m);

const item = (
  patch: Partial<ScheduleItem> & Pick<ScheduleItem, "id" | "start" | "end" | "title">,
): ScheduleItem => ({
  spaceId: "hse",
  ...patch,
});

/** Неделя с понедельника 26 октября по воскресенье 1 ноября: полдень каждого дня. */
const week = (items: Record<number, ScheduleItem[]>): ScheduleGridDay[] =>
  Array.from({ length: 7 }, (_, index) => ({
    date: Date.UTC(2026, 9, 26 + index, 9),
    items: items[index] ?? [],
  }));

describe("ScheduleGrid: неделя сеткой для ноутбука", () => {
  it("семь колонок по дням, у каждой скрытый заголовок с полной датой", () => {
    render(<ScheduleGrid days={week({})} state="ok" nowMs={NOW} timeZone={MSK} />);
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(7);
    expect(screen.getByRole("heading", { level: 3, name: "вторник, 27 октября" })).toBeInTheDocument();
  });

  it("пары идут по времени, у пересечения и изменения — метки с текстом", () => {
    const days = week({
      1: [
        item({ id: "b", start: at(27, 15), end: at(27, 16, 20), title: "Английский язык" }),
        item({
          id: "a",
          start: at(27, 11, 10),
          end: at(27, 12, 30),
          title: "Алгоритмы",
          badge: "Текущий рубежный контроль",
          facts: [
            { key: "room", icon: "room", label: "Аудитория", text: "ауд. 412" },
            { key: "address", icon: "address", label: "Адрес", text: "Покровский б-р, д.11" },
          ],
          conflict: true,
          changed: true,
          spaceTitle: "ВШЭ",
        }),
      ],
    });
    render(<ScheduleGrid days={days} state="ok" nowMs={NOW} timeZone={MSK} showSpace />);
    const rows = screen.getAllByRole("listitem");
    expect(rows.map((row) => within(row).getByText(/Алгоритмы|Английский язык/).textContent)).toEqual([
      "Алгоритмы",
      "Английский язык",
    ]);
    expect(screen.getByText("Пересечение")).toBeInTheDocument();
    expect(screen.getByText("Изменено")).toBeInTheDocument();
    expect(screen.getByText("ВШЭ")).toBeInTheDocument();
    expect(screen.getByText("ауд. 412")).toBeInTheDocument();
    expect(screen.getByText("Покровский б-р, д.11")).toBeInTheDocument();
    expect(screen.getByText("Текущий рубежный контроль")).toBeInTheDocument();
    // В узкой колонке детали идут столбцом, по строке на деталь.
    expect(document.querySelector("[data-fact=address] svg")).toHaveAttribute("data-icon", "address");
  });

  it("день без пар подписан «Пар нет», а не пустой", () => {
    render(<ScheduleGrid days={week({})} state="ok" nowMs={NOW} timeZone={MSK} />);
    expect(screen.getAllByText("Пар нет")).toHaveLength(7);
  });

  it("идущая сейчас пара помечена текстом", () => {
    const days = week({ 1: [item({ id: "now", start: at(27, 17), end: at(27, 18, 30), title: "Лекция" })] });
    render(<ScheduleGrid days={days} state="ok" nowMs={NOW} timeZone={MSK} />);
    expect(screen.getByText("Идёт сейчас")).toBeInTheDocument();
  });

  it("loading — скелетон с aria-busy, пар нет", () => {
    const { container } = render(<ScheduleGrid days={week({})} state="loading" nowMs={NOW} timeZone={MSK} />);
    expect(container.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(screen.queryByText("Пар нет")).toBeNull();
  });

  it("нажатие на пару отдаёт её наверх", async () => {
    const onOpen = vi.fn();
    const target = item({
      id: "x",
      start: at(28, 10, 30),
      end: at(28, 11, 50),
      title: "Дискретная математика",
    });
    render(
      <ScheduleGrid days={week({ 2: [target] })} state="ok" nowMs={NOW} timeZone={MSK} onOpenItem={onOpen} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Дискретная математика/ }));
    expect(onOpen).toHaveBeenCalledWith(target);
  });
});

describe("overlap cards", () => {
  const available = item({
    id: "available",
    start: at(27, 0),
    end: at(28, 0),
    title: "Свободные окна",
    allDay: true,
    sourceTitle: "Яндекс",
  });
  const booking = item({
    id: "booking",
    start: at(27, 10),
    end: at(27, 11),
    title: "Запись на встречу",
    sourceTitle: "Google",
  });

  it("opens all distinct events from one neutral group button", async () => {
    const onOpenGroup = vi.fn();
    render(
      <ScheduleGrid
        days={week({ 1: [booking, available] })}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
        onOpenGroup={onOpenGroup}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(1);
    const card = screen.getByRole("button", { name: /Свободные окна.*Запись на встречу/ });
    expect(card).toHaveAttribute("data-event-count", "2");
    expect(within(card).getByText("Весь день")).toBeInTheDocument();
    expect(within(card).getByText("Яндекс")).toBeInTheDocument();
    expect(within(card).getByText("Google")).toBeInTheDocument();
    expect(within(card).queryByText(/Пересечение|Идёт сейчас/)).toBeNull();
    await userEvent.tab();
    expect(card).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onOpenGroup).toHaveBeenCalledWith([available, booking], week({})[1]?.date);
  });

  it("uses a compact preview in the desktop grid while keeping details available in the card", () => {
    render(
      <ScheduleGrid
        days={week({
          1: [
            item({
              ...booking,
              facts: [{ key: "teacher", icon: "teacher", label: "Преподаватель", text: "Иванов" }],
            }),
          ],
        })}
        state="ok"
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    const card = screen.getByRole("listitem").firstElementChild;
    expect(card).toHaveAttribute("data-schedule-card", "compact");
    expect(card?.querySelector("[data-part=event-preview]")).toHaveClass("schedule-card-details");
    expect(within(card as HTMLElement).getByText("Иванов")).toBeInTheDocument();
  });
});

it("keeps events separately actionable for a caller that only supports opening one event", async () => {
  const onOpenItem = vi.fn();
  const first = item({ id: "one", start: at(27, 10), end: at(27, 11), title: "Первое" });
  const second = item({ id: "two", start: at(27, 10), end: at(27, 11), title: "Второе" });
  render(
    <ScheduleGrid
      days={week({ 1: [first, second] })}
      state="ok"
      nowMs={NOW}
      timeZone={MSK}
      onOpenItem={onOpenItem}
    />,
  );
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: /Второе/ }));
  expect(onOpenItem).toHaveBeenCalledWith(second);
});
