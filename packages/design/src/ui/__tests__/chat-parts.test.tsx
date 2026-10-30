import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { NBSP } from "../../lib/format";
import { ChatLayout } from "../chat-layout/chat-layout";
import { ContextMeter, ContextMeterBar, formatTokens } from "../context-meter/context-meter";
import { ScreenHeader } from "../screen-header/screen-header";
import { SourceCitation } from "../source-citation/source-citation";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const plain = (text: string | null) => (text ?? "").replaceAll(NBSP, " ");

const SEGMENTS = [
  { kind: "system", label: "Правила и навыки", tokens: 9_800 },
  { kind: "profile", label: "Профиль", tokens: 600 },
  { kind: "data", label: "Данные вуза", tokens: 12_400 },
  { kind: "documents", label: "Документы", tokens: 0 },
  { kind: "history", label: "Переписка", tokens: 3_450 },
  { kind: "free", label: "Свободно", tokens: 973_750 },
];

describe("formatTokens", () => {
  it("одна единица без ложной точности", () => {
    expect(plain(formatTokens(850))).toBe("850");
    expect(plain(formatTokens(12_400))).toBe("12,4 тыс.");
    expect(plain(formatTokens(41_250))).toBe("41,3 тыс.");
    expect(plain(formatTokens(1_000_000))).toBe("1 млн");
    expect(plain(formatTokens(1_250_000))).toBe("1,3 млн");
    expect(plain(formatTokens(-5))).toBe("0");
  });
});

describe("ContextMeter", () => {
  it("одна плоская полоса: сегменты ненулевые, «свободно» — остаток трека, а не сегмент", () => {
    const { container } = render(<ContextMeterBar limit={1_000_000} segments={SEGMENTS} />);
    const bar = container.querySelector("[data-context-bar]");
    const kinds = [...(bar?.querySelectorAll("[data-segment]") ?? [])].map((item) =>
      item.getAttribute("data-segment"),
    );
    expect(kinds).toEqual(["system", "profile", "data", "history"]);
    // Ширина сегмента — его доля окна, без выдуманного округления вверх.
    const widths = [...(bar?.querySelectorAll<HTMLElement>("[data-segment]") ?? [])].map((item) =>
      Number.parseFloat(item.style.width),
    );
    expect(widths[0]).toBeCloseTo(0.98, 2);
    expect(widths[2]).toBeCloseTo(1.24, 2);
    expect(widths.reduce((sum, value) => sum + value, 0)).toBeLessThan(3);
  });

  it("число занятого и окно, легенда с токенами, список источников со временем", () => {
    render(
      <ContextMeter
        limit={1_000_000}
        used={26_250}
        segments={SEGMENTS}
        nowMs={NOW}
        sources={[
          { title: "Расписание — личный кабинет", kind: "data", fetchedAt: NOW - 12 * 60_000 },
          { title: "Правила сдачи курсовых", kind: "document", fetchedAt: null },
        ]}
      />,
    );
    expect(plain(screen.getByText(/токенов/).textContent)).toBe("26,3 тыс. из 1 млн токенов");
    const legend = screen.getByRole("list", { name: "Из чего состоит контекст" });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(4);
    expect(within(legend).getByText("Данные вуза")).toBeInTheDocument();
    expect(within(legend).queryByText("Документы")).toBeNull();
    expect(within(legend).queryByText("Свободно")).toBeNull();
    expect(screen.getByRole("heading", { name: "Источники ответа" })).toBeInTheDocument();
    expect(screen.getByText("Расписание — личный кабинет")).toBeInTheDocument();
    expect(screen.getByText("Получено 12 минут назад")).toBeInTheDocument();
    // Время неизвестно — подписи нет, придуманного времени тоже.
    expect(screen.getAllByText(/Получено/)).toHaveLength(1);
  });

  it("нет источников и пустой контекст: честные подписи, а не пустая шкала", () => {
    render(<ContextMeter limit={1_000_000} used={0} segments={[]} nowMs={NOW} />);
    expect(screen.getByText("Пока в контексте только правила и профиль.")).toBeInTheDocument();
    expect(screen.getByText("В этом чате агент пока ничего не подключал к ответам.")).toBeInTheDocument();
  });
});

describe("SourceCitation: внутренняя ссылка", () => {
  const base = { title: "Правила сдачи", fetchedAt: NOW - 3_600_000, nowMs: NOW };

  it("ведёт в приложение, в той же вкладке, вместо хоста — подпись", async () => {
    const onOpen = vi.fn();
    render(
      <SourceCitation
        {...base}
        url="botai://documents/doc-2"
        internal={{
          href: "/study/documents",
          label: "Открыть документ в «Учёбе»",
          icon: "document",
          onOpen,
        }}
      />,
    );
    const link = screen.getByRole("link", { name: /Правила сдачи/ });
    expect(link).toHaveAttribute("href", "/study/documents");
    expect(link).not.toHaveAttribute("target");
    expect(screen.getByText("Открыть документ в «Учёбе»")).toBeInTheDocument();
    expect(screen.queryByText("Источник недоступен")).toBeNull();
    await userEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("щелчок с Ctrl или Cmd оставляет поведение ссылки браузеру", async () => {
    const onOpen = vi.fn();
    render(
      <SourceCitation
        {...base}
        url={null}
        internal={{ href: "/study/documents", label: "Открыть", onOpen }}
      />,
    );
    const link = screen.getByRole("link");
    link.addEventListener("click", (event) => event.preventDefault());
    await userEvent.keyboard("{Control>}");
    await userEvent.click(link);
    await userEvent.keyboard("{/Control}");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("без внутренней ссылки всё как раньше: http(s) — новая вкладка, остальное — «Источник недоступен»", () => {
    const { rerender } = render(<SourceCitation {...base} url="https://lk.fakeuni.test/timetable" />);
    expect(screen.getByRole("link")).toHaveAttribute("target", "_blank");
    rerender(<SourceCitation {...base} url="botai://sources/3" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Источник недоступен")).toBeInTheDocument();
  });
});

describe("ChatLayout и ScreenHeader narrow", () => {
  it("страница прокручивается целиком: липкая шапка и липкое поле, колонка max-w-chat; клавиатура опускает поле к краю", () => {
    const { container, rerender } = render(
      <ChatLayout header={<header>Шапка</header>} composer={<div>Поле</div>} floating={<span>Вниз</span>}>
        <p>Сообщения</p>
      </ChatLayout>,
    );
    const dock = container.querySelector("[data-chat-dock]") as HTMLElement;
    const root = container.querySelector("[data-chat-layout]") as HTMLElement;
    // Прокрутка — у страницы: ни фиксированной высоты, ни собственного скролла у ленты.
    expect(root.style.height).toBe("");
    expect(container.querySelector("[data-chat-transcript]")?.className).toContain("max-w-chat");
    expect(container.querySelector("[data-chat-header]")?.className).toContain("sticky");
    expect(dock.className).toContain("sticky");
    // Над нижней панелью телефона: поле встаёт выше её, ноутбук — отступ от низа.
    expect(dock.className).toContain("bottom-[calc(var(--spacing-tabbar)");
    expect(dock.className).toContain("lg:pb-safe-5");
    expect(root.className).toContain("pb-[calc(var(--spacing-tabbar)");
    expect(screen.getByText("Вниз")).toBeInTheDocument();
    rerender(
      <ChatLayout keyboardOpen header={<header>Шапка</header>} composer={<div>Поле</div>}>
        <p>Сообщения</p>
      </ChatLayout>,
    );
    const open = container.querySelector("[data-chat-dock]") as HTMLElement;
    expect(open.className).toContain("bottom-0");
    expect(open.className).not.toContain("--spacing-tabbar");
    expect((container.querySelector("[data-chat-layout]") as HTMLElement).className).not.toContain(
      "--spacing-tabbar",
    );
  });

  it("узкая шапка не растягивается до ширины страницы на ноутбуке", () => {
    const { container, rerender } = render(<ScreenHeader title="Чат" />);
    expect(container.querySelector("header > div")?.className).toContain("lg:max-w-page");
    rerender(<ScreenHeader title="Чат" narrow />);
    expect(container.querySelector("header > div")?.className).not.toContain("lg:max-w-page");
  });
});
