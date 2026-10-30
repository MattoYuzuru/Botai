import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChartBlock } from "../markdown/chart-block";
import { CHART_LIMITS, niceScale, parseChartSpec } from "../markdown/chart-spec";
import { parseInline, parseMarkdown } from "../markdown/parse";
import { SafeMarkdown } from "../markdown/safe-markdown";
import { ScrollArea } from "../scroll-area/scroll-area";

const TABLE = [
  "| Предмет | Время | Оценка |",
  "|:--------|:-----:|-------:|",
  "| **Алгоритмы** | 9:30 | 8,5 |",
  "| *Базы данных* | 11:10 | [10](https://lk.fakeuni.test/grades) |",
].join("\n");

describe("таблицы GFM", () => {
  it("разбор: заголовок, выравнивание, ячейки с разметкой", () => {
    const [table] = parseMarkdown(TABLE);
    expect(table?.type).toBe("table");
    if (table?.type !== "table") return;
    expect(table.align).toEqual(["left", "center", "right"]);
    expect(table.header).toHaveLength(3);
    expect(table.rows).toHaveLength(2);
    expect(table.rows[0]?.[0]?.[0]?.type).toBe("bold");
    expect(table.rows[1]?.[0]?.[0]?.type).toBe("italic");
    expect(table.rows[1]?.[2]?.[0]?.type).toBe("link");
  });

  it("рендер: шапка выделена, выравнивание, жирный и ссылка в ячейках, область фокусируема", () => {
    render(<SafeMarkdown source={TABLE} />);
    const region = screen.getByRole("region", { name: "Таблица" });
    expect(region).toHaveAttribute("tabindex", "0");
    const table = within(region).getByRole("table");
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((node) => node.textContent)).toEqual(["Предмет", "Время", "Оценка"]);
    expect(headers[0]).toHaveClass("font-semibold", "text-left");
    expect(headers[1]).toHaveClass("text-center");
    expect(headers[2]).toHaveClass("text-right", "tabular-nums");
    expect(table.querySelector("strong")).toHaveTextContent("Алгоритмы");
    expect(table.querySelector("em")).toHaveTextContent("Базы данных");
    const link = within(table).getByRole("link", { name: "10" });
    expect(link).toHaveAttribute("href", "https://lk.fakeuni.test/grades");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
  });

  it("строки без крайних «|» и неровные строки допустимы", () => {
    const [table] = parseMarkdown("a | b\n--|--\n1 | 2\n3");
    expect(table?.type).toBe("table");
    if (table?.type !== "table") return;
    expect(table.rows).toHaveLength(1);
    expect(table.rows[0]).toHaveLength(2);
  });

  it("таблица прерывает абзац и заканчивается пустой строкой", () => {
    const blocks = parseMarkdown(`Итог:\n${TABLE}\n\nПосле таблицы.`);
    expect(blocks.map((block) => block.type)).toEqual(["paragraph", "table", "paragraph"]);
  });

  it("без строки-разделителя или с другим числом колонок это абзац", () => {
    expect(parseMarkdown("| a | b |\n| c | d |").map((block) => block.type)).toEqual(["paragraph"]);
    expect(parseMarkdown("| a | b |\n|---|").map((block) => block.type)).toEqual(["paragraph"]);
    const { container } = render(<SafeMarkdown source={"| a | b |\n| c | d |"} />);
    expect(container.querySelector("table")).toBeNull();
  });

  it("экранированный «|» остаётся в ячейке", () => {
    const [table] = parseMarkdown("| a | b |\n|---|---|\n| x \\| y | z |");
    if (table?.type !== "table") throw new Error("ожидалась таблица");
    expect(table.rows[0]).toHaveLength(2);
    expect(table.rows[0]?.[0]).toEqual([{ type: "text", value: "x | y" }]);
  });
});

describe("курсив", () => {
  it("*слово* и _слово_ — курсив, ***слово*** — жирный курсив", () => {
    expect(parseInline("*раз* и _два_")).toEqual([
      { type: "italic", children: [{ type: "text", value: "раз" }] },
      { type: "text", value: " и " },
      { type: "italic", children: [{ type: "text", value: "два" }] },
    ]);
    expect(parseInline("***важно***")).toEqual([
      { type: "bold", children: [{ type: "italic", children: [{ type: "text", value: "важно" }] }] },
    ]);
  });

  it("snake_case, 2*3*4 и маркер списка не становятся курсивом", () => {
    for (const source of ["snake_case_name", "2*3*4", "file_name_v2.txt", "* пункт", "a * b * c"]) {
      expect(
        parseInline(source).some((node) => node.type === "italic"),
        source,
      ).toBe(false);
    }
    const { container } = render(
      <SafeMarkdown source={"Поле student_id_hash и 2*3*4\n\n* первый\n* второй"} />,
    );
    expect(container.querySelector("em")).toBeNull();
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("жирный не ломается рядом с курсивом", () => {
    const { container } = render(<SafeMarkdown source="**жирный** и *курсив*" />);
    expect(container.querySelector("strong")).toHaveTextContent("жирный");
    expect(container.querySelector("em")).toHaveTextContent("курсив");
  });
});

describe("ссылки", () => {
  it("кликабельны в заголовках, списках, цитатах, автоссылках и ячейках", () => {
    const source = [
      "# Смотри [заголовок](https://a.test/h)",
      "",
      "- пункт [список](https://a.test/l)",
      "",
      "> цитата https://a.test/q",
      "",
      "Автоссылка <https://a.test/auto> и голая https://a.test/bare.",
    ].join("\n");
    render(<SafeMarkdown source={source} />);
    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual([
      "https://a.test/h",
      "https://a.test/l",
      "https://a.test/q",
      "https://a.test/auto",
      "https://a.test/bare",
    ]);
    for (const link of screen.getAllByRole("link")) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });

  it.each([
    "[x](javascript:alert(1))",
    "[x](javascript://%0aalert(1))",
    "[x](data:text/html;base64,AAAA)",
    "[x](mailto:a@b.test)",
    "[x](/relative)",
    "[x](file:///etc/passwd)",
  ])("%s не ссылка, даже с резолвером", (source) => {
    const resolveLink = vi.fn(() => ({ href: "/chat" }));
    const { container } = render(<SafeMarkdown source={source} resolveLink={resolveLink} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container).toHaveTextContent("x");
  });

  it("botai:// без резолвера — просто текст", () => {
    const { container } = render(<SafeMarkdown source="Вот [чат](botai://threads/01ABC)" />);
    expect(container.querySelector("a")).toBeNull();
    expect(container).toHaveTextContent("Вот чат");
  });

  it("resolveLink: внутренняя ссылка с href и onOpen; Ctrl-щелчок идёт как у обычной ссылки", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const resolveLink = vi.fn((url: string) =>
      url.startsWith("botai://threads/")
        ? { href: `/chat?thread=${url.slice("botai://threads/".length)}`, onOpen }
        : null,
    );
    render(
      <SafeMarkdown source="Продолжим в [другом чате](botai://threads/01ABC)." resolveLink={resolveLink} />,
    );
    const link = screen.getByRole("link", { name: "другом чате" });
    expect(link).toHaveAttribute("href", "/chat?thread=01ABC");
    expect(link).not.toHaveAttribute("target");
    expect(resolveLink).toHaveBeenCalledWith("botai://threads/01ABC");
    await user.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
    await user.keyboard("{Control>}");
    await user.click(link);
    await user.keyboard("{/Control}");
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("голый botai:// получает подпись резолвера", () => {
    const resolveLink = () => ({ href: "/chat?thread=01ABC", label: "Чат подключения" });
    render(<SafeMarkdown source="Смотри botai://threads/01ABC" resolveLink={resolveLink} />);
    expect(screen.getByRole("link", { name: "Чат подключения" })).toHaveAttribute(
      "href",
      "/chat?thread=01ABC",
    );
    expect(screen.queryByText(/botai:\/\//)).toBeNull();
  });

  it("резолвер не может подсунуть javascript: или //host", () => {
    for (const href of ["javascript:alert(1)", "//evil.test/x", "data:text/html,1", "\\\\evil"]) {
      const { container, unmount } = render(
        <SafeMarkdown source="[x](botai://threads/1)" resolveLink={() => ({ href })} />,
      );
      expect(container.querySelector("a"), href).toBeNull();
      unmount();
    }
    const { container } = render(
      <SafeMarkdown source="[x](botai://threads/1)" resolveLink={() => ({ href: "https://ok.test/p" })} />,
    );
    expect(container.querySelector("a")).toHaveAttribute("href", "https://ok.test/p");
  });

  it("ссылка в ячейке таблицы идёт через резолвер", () => {
    render(
      <SafeMarkdown
        source={"| a |\n|---|\n| [файл](botai://attachments/9) |"}
        resolveLink={() => ({ href: "/api/v1/attachments/9/content" })}
      />,
    );
    expect(screen.getByRole("link", { name: "файл" })).toHaveAttribute(
      "href",
      "/api/v1/attachments/9/content",
    );
  });
});

const SPEC = {
  type: "bar",
  title: "Оценки по предметам",
  labels: ["Алгоритмы", "Базы данных", "Английский"],
  series: [{ name: "Семестр 1", values: [8, 9.5, 7] }],
};

describe("parseChartSpec", () => {
  it("корректная спецификация принимается, строки обрезаются, число в подписи становится строкой", () => {
    const spec = parseChartSpec(
      JSON.stringify({
        type: "line",
        title: "x".repeat(300),
        labels: [2024, "Очень длинная подпись категории, которая не помещается в оси"],
        series: [{ name: "  Ряд  ", values: [1, -2.5] }, { values: [0, 3] }],
      }),
    );
    expect(spec?.type).toBe("line");
    expect(spec?.title).toHaveLength(CHART_LIMITS.title);
    expect(spec?.labels[0]).toBe("2024");
    expect(spec?.labels[1]?.length).toBeLessThanOrEqual(CHART_LIMITS.label);
    expect(spec?.series.map((item) => item.name)).toEqual(["Ряд", "Серия 2"]);
  });

  it.each([
    ["не JSON", "{type:"],
    ["не объект", "[1,2]"],
    ["другой тип", JSON.stringify({ ...SPEC, type: "pie" })],
    ["нет подписей", JSON.stringify({ ...SPEC, labels: [] })],
    [
      "много подписей",
      JSON.stringify({
        ...SPEC,
        labels: Array(51).fill("a"),
        series: [{ name: "a", values: Array(51).fill(1) }],
      }),
    ],
    ["нет рядов", JSON.stringify({ ...SPEC, series: [] })],
    ["много рядов", JSON.stringify({ ...SPEC, series: Array(7).fill({ name: "a", values: [1, 2, 3] }) })],
    ["длина ряда не равна числу подписей", JSON.stringify({ ...SPEC, series: [{ name: "a", values: [1] }] })],
    ["нечисловое значение", JSON.stringify({ ...SPEC, series: [{ name: "a", values: [1, "2", 3] }] })],
    ["нет values", JSON.stringify({ ...SPEC, series: [{ name: "a" }] })],
    [
      "значение больше 1e15 по модулю",
      JSON.stringify({ ...SPEC, series: [{ name: "a", values: [1, 2e15, 3] }] }),
    ],
    [
      "отрицательное значение за пределом",
      JSON.stringify({ ...SPEC, series: [{ name: "a", values: [1, -1.5e15, 3] }] }),
    ],
  ])("отвергает: %s", (_name, raw) => {
    expect(parseChartSpec(raw)).toBeNull();
  });

  it("значения до 1e15 по модулю принимаются", () => {
    expect(
      parseChartSpec(JSON.stringify({ ...SPEC, series: [{ name: "a", values: [1e15, -1e15, 0] }] })),
    ).not.toBeNull();
  });

  it("niceScale: круглые границы и шаг, ноль на засечке", () => {
    expect(niceScale(0, 9.5)).toMatchObject({ min: 0, max: 10, step: 2 });
    expect(niceScale(-3, 12).ticks).toContain(0);
    expect(niceScale(5, 5).min).toBeLessThan(5);
    expect(niceScale(0, 1).ticks).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
  });
});

describe("график в markdown", () => {
  const fence = (spec: unknown) => `Вот график:\n\n\`\`\`chart\n${JSON.stringify(spec)}\n\`\`\`\n\nИтог.`;

  it("```chart с корректной спецификацией — график, а не код", () => {
    const blocks = parseMarkdown(fence(SPEC));
    expect(blocks.map((block) => block.type)).toEqual(["paragraph", "chart", "paragraph"]);
    const { container } = render(<SafeMarkdown source={fence(SPEC)} />);
    expect(container.querySelector("figure[data-chart='bar']")).not.toBeNull();
    expect(container.querySelector("pre")).toBeNull();
  });

  it("невалидная спецификация и другие языки остаются кодом", () => {
    const broken = "```chart\n{не json}\n```";
    expect(parseMarkdown(broken)).toEqual([{ type: "code", value: "{не json}", lang: "chart" }]);
    const { container } = render(<SafeMarkdown source={broken} />);
    expect(container.querySelector("pre")).toHaveTextContent("{не json}");
    expect(container.querySelector("figure")).toBeNull();
    expect(parseMarkdown("```json\n{}\n```")[0]).toEqual({ type: "code", value: "{}", lang: "json" });
    expect(parseMarkdown("```\nx\n```")[0]).toEqual({ type: "code", value: "x" });
  });
});

describe("ChartBlock", () => {
  const bar = parseChartSpec(JSON.stringify(SPEC));
  const multi = parseChartSpec(
    JSON.stringify({
      type: "line",
      title: "Посещаемость",
      labels: ["Пн", "Вт", "Ср"],
      series: [
        { name: "Лекции", values: [3, -1, 5] },
        { name: "Семинары", values: [2, 4, 1] },
      ],
    }),
  );
  if (!(bar && multi)) throw new Error("фикстуры графика");

  it("столбцы: role=img с описанием, подписи категорий и значения", () => {
    const { container } = render(<ChartBlock spec={bar} />);
    const image = screen.getByRole("img", { name: /Столбчатый график «Оценки по предметам»/ });
    expect(image.getAttribute("aria-label")).toMatch(/Категорий: 3, ряд: 1\. Значения от 7 до 9,5/);
    expect(container.querySelectorAll("svg[role='img'] rect")).toHaveLength(3);
    expect(screen.getByText("Алгоритмы", { selector: "svg text" })).toBeInTheDocument();
    expect(container.querySelectorAll("[data-chart-value]")).toHaveLength(3);
    expect(screen.getByText("Оценки по предметам", { selector: "figcaption" })).toBeInTheDocument();
    // Один ряд — легенды нет.
    expect(screen.queryByRole("list", { name: "Ряды" })).toBeNull();
  });

  it("линии: легенда для нескольких рядов, разные метки, нулевая линия при отрицательных значениях", () => {
    const { container } = render(<ChartBlock spec={multi} />);
    const legend = screen.getByRole("list", { name: "Ряды" });
    expect(
      within(legend)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Лекции", "Семинары"]);
    expect(
      container.querySelectorAll("svg[role='img'] path[stroke-dasharray]").length,
    ).toBeGreaterThanOrEqual(1);
    expect(container.querySelector("[data-chart-zero]")).not.toBeNull();
  });

  it("таблица-альтернатива есть всегда (sr-only), кнопка показывает её зрячим", async () => {
    const user = userEvent.setup();
    const { container } = render(<ChartBlock spec={multi} />);
    const wrapper = container.querySelector("[data-chart-table]");
    expect(wrapper).toHaveClass("sr-only");
    const table = screen.getByRole("table");
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Категория", "Лекции", "Семинары"]);
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByRole("rowheader", { name: "Вт" })).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Показать таблицу" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", wrapper?.id);
    await user.click(toggle);
    expect(wrapper).not.toHaveClass("sr-only");
    expect(screen.getByRole("button", { name: "Скрыть таблицу" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Таблица данных графика" })).toBeInTheDocument();
  });

  it("больше 12 категорий — шире контейнера и с горизонтальной прокруткой", () => {
    const labels = Array.from({ length: 20 }, (_, index) => `К${index + 1}`);
    const wide = parseChartSpec(
      JSON.stringify({ type: "bar", labels, series: [{ name: "n", values: labels.map((_, i) => i) }] }),
    );
    if (!wide) throw new Error("фикстура");
    const { container } = render(<ChartBlock spec={wide} />);
    const svg = container.querySelector("svg[role='img']");
    expect(Number(svg?.getAttribute("width"))).toBeGreaterThanOrEqual(20 * 48);
    expect(screen.getByRole("region", { name: /прокрутите по горизонтали/ })).toHaveAttribute(
      "tabindex",
      "0",
    );
    // Значения над столбцами при таком числе категорий не подписываются — они в таблице.
    expect(container.querySelectorAll("[data-chart-value]")).toHaveLength(0);
  });

  it("длинные подписи оси укорачиваются, полный текст остаётся в таблице", () => {
    const long = parseChartSpec(
      JSON.stringify({
        type: "bar",
        labels: ["Очень длинное название категории", "Другое очень длинное название"],
        series: [{ name: "n", values: [1, 2] }],
      }),
    );
    if (!long) throw new Error("фикстура");
    render(<ChartBlock spec={long} />);
    expect(screen.getByRole("rowheader", { name: "Очень длинное название категории" })).toBeInTheDocument();
  });
});

describe("ScrollArea", () => {
  it("по умолчанию бегунок появляется при наведении, ориентация вертикальная", () => {
    const { container } = render(
      <ScrollArea className="h-20">
        <p>Содержимое</p>
      </ScrollArea>,
    );
    expect(container.querySelector("[data-radix-scroll-area-viewport]")).not.toBeNull();
    // При type="hover" полоса не отрисована, пока нет наведения и прокрутки.
    expect(container.querySelector("[data-scrollbar]")).toBeNull();
  });

  it("type=always рисует полосу из токенов: размер --scrollbar-size, без внутренних отступов", () => {
    const { container } = render(
      <ScrollArea type="always" className="h-20">
        <p>Содержимое</p>
      </ScrollArea>,
    );
    const bar = container.querySelector("[data-scrollbar='vertical']");
    expect(bar).toHaveClass("w-(--scrollbar-size)", "p-0");
    expect(bar).not.toHaveClass("p-0.5");
  });

  it("горизонтальная полоса: высота из того же токена", () => {
    const { container } = render(
      <ScrollArea type="always" orientation="horizontal" className="w-20">
        <p>Содержимое</p>
      </ScrollArea>,
    );
    expect(container.querySelector("[data-scrollbar='horizontal']")).toHaveClass("h-(--scrollbar-size)");
  });
});
