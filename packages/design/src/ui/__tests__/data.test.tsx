import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { NBSP } from "../../lib/format";
import { HOUR_MS, urgencyOf } from "../../lib/urgency";
import { DeadlineItem } from "../deadline-item/deadline-item";
import { ScheduleDay, type ScheduleItem } from "../schedule/schedule-day";
import { ScheduleWeek, type WeekDay } from "../schedule/schedule-week";
import { SourceCitation } from "../source-citation/source-citation";
import { type SourceState, SourceStatus } from "../source-status/source-status";
import { SyncIndicator } from "../sync-indicator/sync-indicator";
import { TabBar, type TabItem } from "../tab-bar/tab-bar";
import { UniversitySpaceSwitcher } from "../university-space-switcher/university-space-switcher";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 11, 5); // вторник, 14:05 МСК
const at = (day: number, h: number, m = 0) => Date.UTC(2026, 9, day, h - 3, m);
/** Testing Library нормализует неразрывные пробелы в тексте узла; запрос приводим так же. */
const plain = (text: string) => text.replaceAll(NBSP, " ");

describe("SourceStatus: шесть видимо разных состояний, кнопки повтора нет", () => {
  it("компактный статус раскрывает пояснение, не запускает обновление и доступен скринридеру", async () => {
    const reauth = vi.fn();
    render(
      <SourceStatus
        variant="compact"
        state="stale"
        spaceId="fakeuni"
        spaceTitle="Учебный вуз"
        updatedAt={null}
        nowMs={NOW}
        onReauth={reauth}
      />,
    );
    const status = screen.getByRole("button", { name: "Учебный вуз: Данные устарели" });
    expect(status).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(status);
    expect(status).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(status);
    expect(status).toHaveAttribute("aria-expanded", "false");
    expect(reauth).not.toHaveBeenCalled();
  });
  const states: [SourceState, RegExp][] = [
    ["ok", /^Обновлено 5\sминут назад$/],
    ["syncing", /^Обновляется$/],
    ["stale", /^Данные от 24 октября$/],
    ["offline", /^Нет сети\. Показан снимок от 24 октября, 09:00$/],
    ["reauth", /^Нужно снова войти в ЛК ВШЭ$/],
    ["repairing", /^Вуз что-то поменял, Варя чинит$/],
  ];
  const updatedAt: Record<SourceState, number> = {
    ok: NOW - 5 * 60_000,
    syncing: NOW - 5 * 60_000,
    stale: at(24, 9),
    offline: at(24, 9),
    reauth: at(24, 9),
    repairing: at(24, 9),
  };

  it.each(states)("%s", (state, text) => {
    render(
      <SourceStatus
        state={state}
        spaceId="hse"
        spaceTitle="ВШЭ"
        updatedAt={updatedAt[state]}
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each(states.map(([state]) => state))(
    "%s: нет ни «Повторить», ни «Обновить», ни «Загрузить»",
    (state) => {
      render(
        <SourceStatus
          state={state}
          spaceId="hse"
          updatedAt={updatedAt[state]}
          nowMs={NOW}
          variant="banner"
          timeZone={MSK}
          onReauth={() => {}}
        />,
      );
      for (const button of screen.queryAllByRole("button")) {
        expect(button.textContent).not.toMatch(/Повторить|Обновить|Загрузить|Попробовать/);
      }
    },
  );

  it("«Войти снова» — единственное действие, и только у reauth", async () => {
    const onReauth = vi.fn();
    const { rerender } = render(
      <SourceStatus
        state="reauth"
        spaceId="hse"
        spaceTitle="ВШЭ"
        updatedAt={null}
        nowMs={NOW}
        onReauth={onReauth}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Войти снова" }));
    expect(onReauth).toHaveBeenCalledTimes(1);
    rerender(<SourceStatus state="stale" spaceId="hse" updatedAt={null} nowMs={NOW} onReauth={onReauth} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("banner: полоса несёт иконку и текст, а не только цвет", () => {
    render(
      <SourceStatus
        state="offline"
        spaceId="hse"
        updatedAt={at(24, 9)}
        nowMs={NOW}
        variant="banner"
        timeZone={MSK}
      />,
    );
    const banner = screen.getByText(/Нет сети/).closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "info");
    expect(banner?.querySelector("svg")).not.toBeNull();
  });
});

describe("SyncIndicator", () => {
  it("idle ничего не рисует, syncing — точка с пульсом", () => {
    const { container, rerender } = render(<SyncIndicator state="idle" />);
    expect(container.querySelector("[data-sync-dot]")).toBeNull();
    rerender(<SyncIndicator state="syncing" />);
    expect(container.querySelector("[data-sync-dot=syncing]")).toHaveClass("animate-pulse-soft");
  });

  it("объявляет только итог: «Данные обновлены» после syncing → idle", () => {
    const { rerender } = render(<SyncIndicator state="syncing" />);
    const live = screen.getAllByRole("status")[0] as HTMLElement;
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toBeEmptyDOMElement();
    rerender(<SyncIndicator state="idle" />);
    expect(live).toHaveTextContent("Данные обновлены");
  });

  it("size=mascot показывает слот Вари", () => {
    const { container } = render(
      <SyncIndicator state="syncing" size="mascot" mascot={<i data-testid="varya" />} />,
    );
    expect(screen.getByTestId("varya")).toBeInTheDocument();
    expect(container.querySelector("[data-sync-dot]")).toBeNull();
  });
});

describe("DeadlineItem: срочность", () => {
  const base = {
    spaceId: "hse",
    spaceTitle: "ВШЭ",
    nowMs: NOW,
    timeZone: MSK,
    title: "Домашнее задание 3",
  } as const;

  it("calm — время текстом, без метки", () => {
    const due = NOW + 10 * 24 * HOUR_MS;
    render(<DeadlineItem {...base} dueAt={due} urgency={urgencyOf(due, NOW)} />);
    expect(screen.getByText(/^6 ноября, 14:05$/)).toBeInTheDocument();
    expect(screen.queryByText(/Скоро|Осталось|Просрочено/)).toBeNull();
  });

  it("soon — «Скоро · 2 д»", () => {
    const due = NOW + 50 * HOUR_MS;
    render(<DeadlineItem {...base} dueAt={due} urgency={urgencyOf(due, NOW)} />);
    expect(screen.getByText(plain(`Скоро · 2${NBSP}д`))).toBeInTheDocument();
  });

  it("urgent — заливка warning и явный остаток", () => {
    const due = NOW + 5 * HOUR_MS;
    render(<DeadlineItem {...base} dueAt={due} urgency={urgencyOf(due, NOW)} />);
    const badge = screen.getByText(plain(`Осталось${NBSP}5${NBSP}ч`));
    expect(badge).toHaveClass("bg-warning");
  });

  it("overdue — «Просрочено» без укоров", () => {
    const due = NOW - 2 * HOUR_MS;
    render(<DeadlineItem {...base} dueAt={due} urgency={urgencyOf(due, NOW)} />);
    expect(screen.getByText("Просрочено")).toHaveClass("bg-danger-subtle");
  });

  it("unknown — «Срок не указан» и никакого отсчёта", () => {
    render(<DeadlineItem {...base} dueAt={null} urgency="unknown" />);
    expect(screen.getByText("Срок не указан")).toBeInTheDocument();
    expect(screen.queryByText(/Осталось|Скоро|через/)).toBeNull();
  });

  it("сдано — зачёркнуто, без метки срочности", () => {
    const due = NOW - 2 * HOUR_MS;
    render(<DeadlineItem {...base} dueAt={due} urgency="overdue" done />);
    expect(screen.getByText("Домашнее задание 3")).toHaveClass("line-through");
    expect(screen.queryByText("Просрочено")).toBeNull();
    expect(screen.getByText("Сдано")).toBeInTheDocument();
  });

  it("строка нажимается целиком (не меньше 56 px) и метка вуза видна на сводном экране", async () => {
    const onOpen = vi.fn();
    render(<DeadlineItem {...base} dueAt={NOW + 100 * HOUR_MS} urgency="calm" onOpen={onOpen} showSpace />);
    const row = screen.getByRole("button");
    expect(row).toHaveClass("min-h-14");
    expect(within(row).getByText("ВШЭ")).toBeInTheDocument();
    await userEvent.click(row);
    expect(onOpen).toHaveBeenCalled();
  });
});

describe("ScheduleDay", () => {
  const items: ScheduleItem[] = [
    {
      id: "b",
      start: at(27, 15, 0),
      end: at(27, 16, 20),
      title: "Базы данных",
      room: "Ауд. 305",
      spaceId: "cu",
      spaceTitle: "ЦУ",
      changed: true,
    },
    {
      id: "a",
      start: at(27, 9, 30),
      end: at(27, 10, 50),
      title: "Алгоритмы",
      room: "Ауд. 214",
      teacher: "Иванов",
      spaceId: "hse",
      spaceTitle: "ВШЭ",
    },
    {
      id: "c",
      start: at(27, 14, 0),
      end: at(27, 15, 30),
      title: "Английский",
      spaceId: "hse",
      spaceTitle: "ВШЭ",
      conflict: true,
    },
  ];

  it("сортирует по времени, показывает время, аудиторию, изменения и пересечения", () => {
    render(<ScheduleDay date={NOW} items={items} state="ok" nowMs={NOW} timeZone={MSK} showSpace />);
    const rows = screen.getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Алгоритмы"),
      expect.stringContaining("Английский"),
      expect.stringContaining("Базы данных"),
    ]);
    expect(screen.getByText("09:30")).toBeInTheDocument();
    expect(screen.getByText("Изменено")).toBeInTheDocument();
    expect(screen.getByText("Пересекается по времени")).toBeInTheDocument();
    expect(screen.getByText("Идёт сейчас")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "вторник, 27 октября" })).toBeInTheDocument();
    expect(screen.getByText(plain(`3${NBSP}пары`))).toBeInTheDocument();
  });

  it("empty: «Пар нет» — и это не «всё сдано»", () => {
    render(<ScheduleDay date={NOW} items={[]} state="empty" nowMs={NOW} timeZone={MSK} />);
    expect(screen.getByText("Пар нет")).toBeInTheDocument();
    expect(screen.queryByText(/сдано/i)).toBeNull();
  });

  it("loading: скелетон, aria-busy, данных нет", () => {
    const { container } = render(
      <ScheduleDay date={NOW} items={items} state="loading" nowMs={NOW} timeZone={MSK} />,
    );
    expect(container.querySelector("section")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("Алгоритмы")).toBeNull();
  });

  it("stale: данные остаются и подпись свежести видна", () => {
    render(
      <ScheduleDay
        date={NOW}
        items={items}
        state="stale"
        nowMs={NOW}
        timeZone={MSK}
        freshness="Данные от 24 октября"
      />,
    );
    expect(screen.getByText("Алгоритмы")).toBeInTheDocument();
    expect(screen.getByText("Данные от 24 октября")).toBeInTheDocument();
  });
});

describe("ScheduleWeek", () => {
  const days: WeekDay[] = Array.from({ length: 7 }, (_, index) => ({
    date: at(26 + index, 12),
    count: index % 2,
    hasConflict: index === 1,
  }));

  it("выбранный день — заливка accent, сегодняшний помечен aria-current", async () => {
    const onSelect = vi.fn();
    render(<ScheduleWeek days={days} selected={at(28, 12)} onSelect={onSelect} nowMs={NOW} timeZone={MSK} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(7);
    expect(buttons[2]).toHaveAttribute("aria-pressed", "true");
    expect(buttons[2]).toHaveClass("bg-accent");
    expect(buttons[1]).toHaveAttribute("aria-current", "date");
    await userEvent.click(buttons[4] as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith(days[4]?.date);
  });

  it("зона дня не меньше 44 px", () => {
    render(
      <ScheduleWeek
        days={days}
        selected={days[0]?.date ?? 0}
        onSelect={() => {}}
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    for (const button of screen.getAllByRole("button")) expect(button).toHaveClass("min-h-16");
  });
});

describe("TabBar", () => {
  const items: TabItem[] = [
    { id: "today", label: "Сегодня", icon: "today", href: "/" },
    { id: "schedule", label: "Расписание", icon: "schedule", href: "/schedule" },
    { id: "tasks", label: "Задания", icon: "deadline", href: "/tasks" },
    { id: "chat", label: "Чат", icon: "chat", href: "/chat", badge: 2 },
    { id: "study", label: "Учёба", icon: "university", href: "/study" },
  ];

  it("ровно пять пунктов, активный помечен aria-current и accent-fg", () => {
    render(<TabBar items={items} activeId="schedule" />);
    const nav = screen.getByRole("navigation", { name: "Основная навигация" });
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
    const active = within(nav).getByRole("link", { name: /Расписание/ });
    expect(active).toHaveAttribute("aria-current", "page");
    expect(active).toHaveClass("text-accent-fg", "font-medium");
    expect(within(nav).getByRole("link", { name: /Чат/ })).not.toHaveAttribute("aria-current");
  });

  it("счётчик на иконке", () => {
    render(<TabBar items={items} activeId="today" />);
    expect(screen.getByText("2")).toHaveAttribute("data-tab-badge");
  });

  it("шестая вкладка — ошибка в консоли", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<TabBar items={[...items, { id: "x", label: "Ещё", icon: "more" }]} activeId="today" />);
    expect(error).toHaveBeenCalledWith(expect.stringContaining("ровно 5"));
    error.mockRestore();
  });

  it("на 1024 px и шире превращается в боковую панель с Wordmark", () => {
    render(<TabBar items={items} activeId="today" brand={<span>BotAI</span>} />);
    const nav = screen.getByRole("navigation");
    expect(nav.className).toContain("lg:w-rail");
    expect(screen.getByText("BotAI").parentElement).toHaveClass("hidden", "lg:block");
  });

  it("скрывается при открытой клавиатуре", () => {
    render(<TabBar items={items} activeId="today" hidden />);
    expect(screen.getByRole("navigation", { hidden: true })).toHaveAttribute("data-hidden");
    expect(screen.getByRole("navigation", { hidden: true })).toHaveAttribute("inert");
  });
});

describe("UniversitySpaceSwitcher", () => {
  const spaces = [
    {
      id: "hse",
      title: "Высшая школа экономики",
      shortTitle: "ВШЭ",
      status: "ok" as const,
      updatedAt: NOW - 120_000,
    },
    {
      id: "cu",
      title: "Центральный университет",
      shortTitle: "ЦУ",
      status: "reauth" as const,
      updatedAt: null,
    },
  ];

  it("показывает активный вуз, открывает шит со статусами и переключает", async () => {
    const onChange = vi.fn();
    render(
      <UniversitySpaceSwitcher
        spaces={spaces}
        activeId="hse"
        onChange={onChange}
        allowAll
        nowMs={NOW}
        timeZone={MSK}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /ВШЭ/ }));
    const sheet = await screen.findByRole("dialog", { name: "Вуз" });
    expect(within(sheet).getByText(plain(`Обновлено 2${NBSP}минуты назад`))).toBeInTheDocument();
    expect(within(sheet).getByText("Нужно снова войти в ЛК ЦУ")).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: /Центральный университет/ }));
    expect(onChange).toHaveBeenCalledWith("cu");
  });

  it("«Все вузы» — сводный экран без цвета вуза", async () => {
    render(
      <UniversitySpaceSwitcher spaces={spaces} activeId="all" onChange={() => {}} allowAll nowMs={NOW} />,
    );
    expect(screen.getByRole("button", { name: /Все вузы/ })).toBeInTheDocument();
  });
});

describe("SourceCitation", () => {
  it("рабочая ссылка открывается в новой вкладке с noopener", () => {
    render(
      <SourceCitation
        title="Расписание"
        url="https://fakeuni.test/api/v2/timetable"
        fetchedAt={NOW - 3600_000}
        nowMs={NOW}
        timeZone={MSK}
        spaceTitle="ВШЭ"
      />,
    );
    const link = screen.getByRole("link", { name: /Расписание/ });
    expect(link).toHaveAttribute("href", "https://fakeuni.test/api/v2/timetable");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.getByText("fakeuni.test")).toBeInTheDocument();
    expect(screen.getByText(/Получено час назад/)).toBeInTheDocument();
  });

  it.each([null, "", "javascript:alert(1)", "/relative", "data:text/html,x", "ftp://x.test"])(
    "недоступный или небезопасный адрес %s: подпись остаётся, ссылки нет",
    (url) => {
      render(
        <SourceCitation title="Расписание" url={url} fetchedAt={NOW - 60_000} nowMs={NOW} timeZone={MSK} />,
      );
      expect(screen.queryByRole("link")).toBeNull();
      expect(screen.getByText("Расписание")).toBeInTheDocument();
      expect(screen.getByText("Источник недоступен")).toBeInTheDocument();
    },
  );
});
