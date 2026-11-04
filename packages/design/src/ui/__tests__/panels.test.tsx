import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ACCENT_HUE_WINDOW, accentScale, UNIVERSITY_ACCENTS } from "../../lib/accents";
import { contrastRatio, WCAG_AA_TEXT } from "../../lib/color";
import { DataTable, type DataTableColumn } from "../data-table/data-table";
import { ICON_NAMES, Icon, type IconName, iconRegistry, isIconName } from "../icon";
import { ChartBlock } from "../markdown/chart-block";
import type { ChartSpec } from "../markdown/chart-spec";
import { MonogramBadge, monogramText } from "../monogram-badge/monogram-badge";
import { Panel } from "../panel/panel";
import { ProfileCard } from "../profile-card/profile-card";
import { RankList, RankRow, rankSummary } from "../rank-list/rank-list";
import { StatGrid, StatTile } from "../stat-tile/stat-tile";

describe("реестр иконок: панели", () => {
  const PANEL_ICONS = ["chart", "trophy", "star", "idCard", "trend", "trend-down"] as const;

  it("новые имена есть в реестре и в ICON_NAMES", () => {
    for (const name of PANEL_ICONS) {
      expect(iconRegistry).toHaveProperty(name);
      expect(ICON_NAMES).toContain(name);
    }
  });

  it("ICON_NAMES — все ключи реестра без повторов; каждое имя рисуется", () => {
    expect([...ICON_NAMES].sort()).toEqual(Object.keys(iconRegistry).sort());
    expect(new Set(ICON_NAMES).size).toBe(ICON_NAMES.length);
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} />);
      expect(container.querySelector(`[data-icon="${name}"]`)).not.toBeNull();
      unmount();
    }
  });

  it("isIconName отвергает чужие строки и прототипные ключи", () => {
    expect(isIconName("trophy")).toBe(true);
    expect(isIconName("constructor")).toBe(false);
    expect(isIconName("<svg/>")).toBe(false);
    expect(isIconName(42)).toBe(false);
  });

  it("имена типизированы: компилятор принимает IconName из списка", () => {
    const first: IconName = ICON_NAMES[0] as IconName;
    expect(isIconName(first)).toBe(true);
  });
});

describe("MonogramBadge", () => {
  it("монограмма: короткое название как есть, длинное — первые буквы, не больше трёх", () => {
    expect(monogramText("ВШЭ", "Высшая школа экономики")).toBe("ВШЭ");
    expect(monogramText("ЦУ", "Центральный университет")).toBe("ЦУ");
    expect(monogramText(null, "Высшая школа экономики")).toBe("ВШЭ");
    expect(monogramText("", "Центральный университет")).toBe("ЦУ");
    expect(monogramText(undefined, "Московский государственный технический университет")).toBe("МГТ");
    expect(monogramText("Резерв", "Резерв")).toBe("РЕ");
    expect(monogramText("  ", "   ")).toBe("");
  });

  it("имя для скринридера — полное название, буквы скрыты от дублирования", () => {
    render(<MonogramBadge title="Высшая школа экономики" shortTitle="ВШЭ" hue={262.2} />);
    const badge = screen.getByRole("img", { name: "Высшая школа экономики" });
    expect(badge).toHaveTextContent("ВШЭ");
    expect(badge.querySelector('[aria-hidden="true"]')).toHaveTextContent("ВШЭ");
  });

  it("декоративный знак скрыт от скринридера", () => {
    const { container } = render(<MonogramBadge title="ЦУ" shortTitle="ЦУ" decorative />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });

  it("акцент: реестр — data-space, свой тон — переменные на самом знаке, вне окна — ВШЭ", () => {
    const { rerender } = render(<MonogramBadge title="ЦУ" hue={292} />);
    const badge = () => screen.getByRole("img", { name: "ЦУ" });
    expect(badge()).toHaveAttribute("data-space", "cu");
    rerender(<MonogramBadge title="ЦУ" hue={300} />);
    expect(badge()).toHaveAttribute("data-space", "custom");
    expect(badge().style.getPropertyValue("--color-accent")).toMatch(/^oklch\(/);
    rerender(<MonogramBadge title="ЦУ" hue={25} />);
    expect(badge()).toHaveAttribute("data-space", "hse");
    expect(badge().style.getPropertyValue("--color-accent")).toBe("");
    rerender(<MonogramBadge title="ЦУ" hue={null} />);
    expect(badge()).toHaveAttribute("data-space", "hse");
  });

  it("размеры sm/md/lg различаются; заливка — токены accent и fg-on-accent", () => {
    const { rerender } = render(<MonogramBadge title="ВШЭ" size="sm" />);
    const cls = () => screen.getByRole("img", { name: "ВШЭ" }).className;
    expect(cls()).toContain("size-8");
    expect(cls()).toContain("bg-accent");
    expect(cls()).toContain("text-fg-on-accent");
    rerender(<MonogramBadge title="ВШЭ" size="md" />);
    expect(cls()).toContain("size-10");
    rerender(<MonogramBadge title="ВШЭ" size="lg" />);
    expect(cls()).toContain("size-12");
  });

  it("короткое без букв заменяется названием; совсем пустое — иконка вуза", () => {
    const { container } = render(<MonogramBadge title="Вуз" shortTitle="…" />);
    expect(container.querySelector('[data-icon="university"]')).toBeNull();
    expect(container.querySelector("[data-monogram]")).toHaveTextContent(/^ВУЗ$/);
    const empty = render(<MonogramBadge title=" " />);
    expect(empty.container.querySelector('[data-icon="university"]')).not.toBeNull();
  });

  it("белый на заливке акцента: AA для всех вузов реестра и по всему окну тонов", () => {
    const white = { l: 1, c: 0, h: 0, alpha: 1 };
    const hues: number[] = UNIVERSITY_ACCENTS.map((row) => row.hue);
    for (let hue = ACCENT_HUE_WINDOW.min; hue <= ACCENT_HUE_WINDOW.max; hue += 5) hues.push(hue);
    for (const hue of hues) {
      expect(contrastRatio(white, accentScale(hue)[600]), `H=${hue}`).toBeGreaterThanOrEqual(WCAG_AA_TEXT);
    }
  });
});

describe("StatTile и StatGrid", () => {
  it("значение, шкала «8,4 / 10», единица и подпись; группа названа подписью", () => {
    render(<StatTile label="Средний балл" value="8,4" scale="10" unit="баллов" caption="по 7 предметам" />);
    const tile = screen.getByRole("group", { name: "Средний балл" });
    expect(within(tile).getByText("8,4")).toBeInTheDocument();
    expect(tile).toHaveTextContent("8,4/ из 10баллов");
    expect(within(tile).getByText("по 7 предметам")).toBeInTheDocument();
  });

  it("шкала «из» читается словом, косая черта скрыта", () => {
    render(<StatTile label="Оценка" value={8} scale={10} />);
    const tile = screen.getByRole("group", { name: "Оценка" });
    expect(within(tile).getByText("из", { exact: false, selector: ".sr-only" })).toBeInTheDocument();
    expect(tile.querySelector('[aria-hidden="true"]')).toHaveTextContent("/");
  });

  it("без шкалы и единицы показывает только значение", () => {
    render(<StatTile label="Результат" value="зачтено" scale={null} />);
    const tile = screen.getByRole("group", { name: "Результат" });
    expect(tile.querySelector("[data-stat-value]")).toHaveTextContent(/^зачтено$/);
  });

  it("тренд: иконка и слово, не только цвет; нейтральные цвета", () => {
    const { container } = render(
      <>
        <StatTile label="А" value={1} trend={{ direction: "up", label: "+0,4 к прошлому семестру" }} />
        <StatTile label="Б" value={2} trend={{ direction: "down", label: "-1 место" }} />
        <StatTile label="В" value={3} trend={{ direction: "flat", label: "как раньше" }} />
      </>,
    );
    const trends = container.querySelectorAll("[data-trend]");
    expect(Array.from(trends, (node) => node.getAttribute("data-trend"))).toEqual(["up", "down", "flat"]);
    expect(trends[0]).toHaveTextContent("рост: +0,4 к прошлому семестру");
    expect(trends[1]).toHaveTextContent("снижение: -1 место");
    expect(trends[2]).toHaveTextContent("без изменений: как раньше");
    expect(trends[0]?.querySelector('[data-icon="trend"]')).not.toBeNull();
    expect(trends[1]?.querySelector('[data-icon="trend-down"]')).not.toBeNull();
    expect(container.innerHTML).not.toMatch(/text-(success|danger)/);
  });

  it("иконка плитки декоративна; compact уменьшает отступы и кегль", () => {
    const { container, rerender } = render(<StatTile label="Место" value={12} icon="trophy" />);
    expect(container.querySelector('[data-icon="trophy"]')).toHaveAttribute("aria-hidden", "true");
    const tile = () => container.querySelector("[data-stat-tile]") as HTMLElement;
    const value = () => container.querySelector("[data-stat-value]") as HTMLElement;
    expect(tile().className).toContain("md:p-4");
    expect(value().className).toContain("md:type-display");
    rerender(<StatTile label="Место" value={12} compact />);
    expect(tile().className).not.toContain("md:p-4");
    expect(value().className).not.toContain("md:type-display");
  });

  it("длинное значение переносится: плитка может сжиматься, горизонтальной прокрутки нет", () => {
    const { container } = render(<StatTile label="Очень длинная подпись показателя" value="86,25" />);
    const tile = container.querySelector("[data-stat-tile]") as HTMLElement;
    expect(tile.className).toContain("min-w-0");
    expect(container.querySelector("[data-stat-value]")?.className).toContain("break-words");
    expect(container.innerHTML).not.toMatch(/overflow-x|whitespace-nowrap/);
  });

  it("StatGrid: две колонки на телефоне, больше на ноутбуке, без min-width и прокрутки", () => {
    const { container } = render(
      <StatGrid columns={4} label="Показатели">
        <StatTile label="А" value={1} />
        <StatTile label="Б" value={2} />
        <StatTile label="В" value={3} />
      </StatGrid>,
    );
    const grid = screen.getByRole("group", { name: "Показатели" });
    expect(grid).toBe(container.querySelector("[data-stat-grid]"));
    expect(grid.className).toContain("grid-cols-2");
    expect(grid.className).toContain("md:grid-cols-3");
    expect(grid.className).toContain("lg:grid-cols-4");
    expect(grid.className).toContain("col-span-2");
    expect(grid.className).not.toMatch(/overflow|min-w-\[|w-\[/);
    expect(grid.className).toContain("min-w-0");
  });

  it("StatGrid без подписи не объявляет группу", () => {
    render(
      <StatGrid>
        <StatTile label="А" value={1} />
      </StatGrid>,
    );
    expect(screen.getAllByRole("group")).toHaveLength(1);
  });
});

describe("RankRow и RankList", () => {
  const items = [
    { position: 1, label: "Анна К.", score: "9,1" },
    { position: 2, label: "Борис Л.", score: "8,8" },
    { position: 12, label: "Вы", score: "8,42", isMe: true },
  ];

  it("порядок как у источника, список назван, место и балл читаются словами", () => {
    render(<RankList label="Рейтинг потока" items={items} />);
    const list = screen.getByRole("list", { name: "Рейтинг потока" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Место 1Анна К.Балл 9,1",
      "Место 2Борис Л.Балл 8,8",
      "Место 12ВыЭто выБалл 8,42",
    ]);
  });

  it("«это вы» выделено не только цветом: метка, вес шрифта и aria-current", () => {
    render(<RankList label="Рейтинг" items={items} />);
    const me = screen.getByText("Вы").closest("li") as HTMLElement;
    expect(me).toHaveAttribute("aria-current", "true");
    expect(within(me).getByText("Это вы")).toBeInTheDocument();
    expect(screen.getByText("Вы").className).toContain("font-bold");
    const other = screen.getByText("Анна К.").closest("li") as HTMLElement;
    expect(other).not.toHaveAttribute("aria-current");
    expect(within(other).queryByText("Это вы")).toBeNull();
    expect(screen.getByText("Анна К.").className).not.toContain("font-bold");
  });

  it("итог «Вы: 12 из 140» текстом; без своей строки — «Всего: 140»; без total итога нет", () => {
    const { rerender } = render(<RankList label="Рейтинг" items={items} total={140} caption="осень 2026" />);
    const summary = () => document.querySelector("[data-rank-summary]");
    expect(summary()).toHaveTextContent("Вы: 12 из 140 · осень 2026");
    rerender(<RankList label="Рейтинг" items={items.slice(0, 2)} total={140} />);
    expect(summary()).toHaveTextContent("Всего: 140");
    rerender(<RankList label="Рейтинг" items={items} />);
    expect(summary()).toBeNull();
    expect(rankSummary(12, 140)).toBe("12 из 140");
    expect(rankSummary("1–3", null)).toBe("1–3");
  });

  it("строка без балла не рисует пустую ячейку; одиночная RankRow живёт в списке", () => {
    render(
      <ul>
        <RankRow position={3} label="Группа БИ-21" meta="поток" />
      </ul>,
    );
    const row = screen.getByRole("listitem");
    expect(row).toHaveTextContent("Место 3Группа БИ-21поток");
    expect(row.textContent).not.toContain("Балл");
    expect(row.className).toContain("min-h-14");
  });
});

interface GradeRow {
  course: string;
  value: string;
  scale: string;
}

describe("DataTable", () => {
  const columns: DataTableColumn<GradeRow>[] = [
    { key: "course", header: "Предмет" },
    { key: "value", header: "Оценка", numeric: true },
    { key: "scale", header: "Шкала" },
  ];
  const rows: GradeRow[] = [
    { course: "Алгоритмы", value: "8", scale: "10 баллов" },
    { course: "Матанализ", value: "86", scale: "100 баллов" },
  ];

  it("настоящая таблица: подпись, заголовки колонок, заголовок строки, ячейки", () => {
    render(<DataTable caption="Оценки за семестр" columns={columns} rows={rows} />);
    const table = screen.getByRole("table", { name: "Оценки за семестр" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Предмет", "Оценка", "Шкала"]);
    const body = within(table).getAllByRole("row").slice(1);
    expect(body).toHaveLength(2);
    expect(within(body[0] as HTMLElement).getByRole("rowheader")).toHaveTextContent("Алгоритмы");
    expect(within(body[1] as HTMLElement).getAllByRole("cell")[0]).toHaveTextContent(/86/);
  });

  it("числа выровнены вправо, табличные цифры; текст влево", () => {
    render(<DataTable caption="Оценки" columns={columns} rows={rows} />);
    const [, valueHeader] = screen.getAllByRole("columnheader");
    expect(valueHeader?.className).toContain("text-right");
    const cell = screen.getAllByRole("cell")[0] as HTMLElement;
    expect(cell).toHaveAttribute("data-numeric");
    expect(cell.className).toContain("md:text-right");
    const textCell = screen.getAllByRole("cell")[1] as HTMLElement;
    expect(textCell).not.toHaveAttribute("data-numeric");
    expect(textCell.className).toContain("md:text-left");
  });

  it("телефон: строки — карточки, шапка скрыта визуально (но остаётся для скринридера), подпись колонки у значения", () => {
    const { container } = render(<DataTable caption="Оценки" columns={columns} rows={rows} />);
    const table = screen.getByRole("table");
    expect(table.className).toContain("block");
    expect(table.className).toContain("md:table");
    expect(container.querySelector("thead")?.className).toContain("max-md:sr-only");
    const bodyRow = container.querySelector("tbody tr") as HTMLElement;
    expect(bodyRow.className).toContain("block");
    expect(bodyRow.className).toContain("md:table-row");
    const labels = Array.from(
      bodyRow.querySelectorAll('td [aria-hidden="true"]'),
      (node) => node.textContent,
    );
    expect(labels).toEqual(["Оценка", "Шкала"]);
    for (const node of bodyRow.querySelectorAll('td [aria-hidden="true"]')) {
      expect(node.className).toContain("md:hidden");
    }
    expect(container.firstElementChild?.className).toContain("max-w-full");
    expect(container.firstElementChild?.className).toContain("md:overflow-x-auto");
    expect(container.firstElementChild?.className).not.toMatch(/(^|\s)overflow-x-auto/);
  });

  it("captionHidden прячет подпись визуально, но оставляет для скринридера", () => {
    render(<DataTable caption="Оценки" captionHidden columns={columns} rows={rows} />);
    const caption = screen.getByText("Оценки", { selector: "caption" });
    expect(caption.className).toContain("sr-only");
    expect(screen.getByRole("table", { name: "Оценки" })).toBeInTheDocument();
  });

  it("render колонки заменяет значение; rowKey даёт ключ", () => {
    render(
      <DataTable
        caption="Оценки"
        rowKey={(row) => row.course}
        columns={[
          { key: "course", header: "Предмет" },
          { key: "value", header: "Оценка", render: (row) => <strong>{row.value} из 10</strong> },
        ]}
        rows={rows.slice(0, 1)}
      />,
    );
    expect(screen.getByText("8 из 10").tagName).toBe("STRONG");
  });

  it("пустая таблица: сообщение вместо строк, без прочерков", () => {
    render(<DataTable caption="Оценки" columns={columns} rows={[]} empty="Оценок пока нет" />);
    expect(screen.getByText("Оценок пока нет")).toBeInTheDocument();
    expect(screen.queryByRole("rowheader")).toBeNull();
  });
});

describe("ProfileCard", () => {
  it("dl с парами «поле — значение», знак вуза в слоте, имя и подпись", () => {
    render(
      <ProfileCard
        name="Студент Тестовый"
        subtitle="Бакалавриат, очная форма"
        monogram={<MonogramBadge title="Высшая школа экономики" shortTitle="ВШЭ" />}
        fields={[
          { label: "Группа", value: "БИ-21" },
          { label: "Программа", value: "Бизнес-информатика" },
          { label: "Курс", value: "3" },
        ]}
      />,
    );
    expect(screen.getByText("Студент Тестовый")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Высшая школа экономики" })).toBeInTheDocument();
    const terms = screen.getAllByRole("term");
    const definitions = screen.getAllByRole("definition");
    expect(terms.map((node) => node.textContent)).toEqual(["Группа", "Программа", "Курс"]);
    expect(definitions.map((node) => node.textContent)).toEqual(["БИ-21", "Бизнес-информатика", "3"]);
  });

  it("поля без значения скрыты, прочерков нет; без полей список не рисуется", () => {
    const { container, rerender } = render(
      <ProfileCard
        fields={[
          { label: "Группа", value: "БИ-21" },
          { label: "Статус", value: "" },
          { label: "Форма", value: null },
          { label: "Курс", value: "  " },
        ]}
      />,
    );
    expect(screen.getAllByRole("term")).toHaveLength(1);
    expect(container.textContent).not.toContain("—");
    rerender(<ProfileCard fields={[{ label: "Статус", value: undefined }]} />);
    expect(container.querySelector("dl")).toBeNull();
  });

  it("телефон: поле над значением, широкий экран — в строку; рамка по желанию", () => {
    const { container, rerender } = render(<ProfileCard fields={[{ label: "Группа", value: "БИ-21" }]} />);
    const root = () => container.firstElementChild as HTMLElement;
    const row = container.querySelector("dl > div") as HTMLElement;
    expect(row.className).toContain("flex-col");
    expect(row.className).toContain("sm:flex-row");
    expect(root().className).not.toContain("border");
    rerender(<ProfileCard framed fields={[{ label: "Группа", value: "БИ-21" }]} />);
    expect(root().className).toContain("border-border");
  });
});

describe("Panel", () => {
  it("секция названа заголовком нужного уровня; иконка декоративна", () => {
    render(
      <Panel title="Успеваемость" icon="chart" headingLevel={2}>
        <p>тело</p>
      </Panel>,
    );
    const panel = screen.getByRole("region", { name: "Успеваемость" });
    expect(within(panel).getByRole("heading", { level: 2, name: "Успеваемость" })).toBeInTheDocument();
    expect(panel.querySelector('[data-icon="chart"]')).toHaveAttribute("aria-hidden", "true");
    expect(within(panel).getByText("тело")).toBeInTheDocument();
  });

  it("по умолчанию h3; знак вуза и действия справа, подзаголовок под названием", () => {
    render(
      <Panel
        title="Рейтинг"
        subtitle="обновлено 3 ноября"
        university={{ title: "Центральный университет", shortTitle: "ЦУ", hue: 292 }}
        actions={<button type="button">Скрыть панель</button>}
      >
        тело
      </Panel>,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Рейтинг" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Центральный университет" })).toHaveTextContent("ЦУ");
    expect(screen.getByRole("button", { name: "Скрыть панель" })).toBeInTheDocument();
    expect(screen.getByText("обновлено 3 ноября")).toBeInTheDocument();
  });

  it("сворачиваемая панель: кнопка в заголовке, aria-expanded, тело скрывается, хозяин получает событие", async () => {
    const onCollapsedChange = vi.fn();
    render(
      <Panel title="Оценки" collapsible onCollapsedChange={onCollapsedChange}>
        <p>тело</p>
      </Panel>,
    );
    const toggle = within(screen.getByRole("heading", { level: 3 })).getByRole("button", { name: "Оценки" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle.className).toContain("min-h-11");
    expect(screen.getByText("тело")).toBeVisible();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("тело")).not.toBeVisible();
    expect(document.getElementById(toggle.getAttribute("aria-controls") as string)).toHaveAttribute("hidden");
    expect(onCollapsedChange).toHaveBeenLastCalledWith(true);
    await userEvent.click(toggle);
    expect(onCollapsedChange).toHaveBeenLastCalledWith(false);
  });

  it("defaultCollapsed начинает свёрнутой; без collapsible кнопки в заголовке нет", () => {
    const { rerender } = render(
      <Panel title="Оценки" collapsible defaultCollapsed>
        <p>тело</p>
      </Panel>,
    );
    expect(screen.getByRole("button", { name: "Оценки" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("тело")).not.toBeVisible();
    rerender(
      <Panel title="Оценки" defaultCollapsed>
        <p>тело</p>
      </Panel>,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("тело")).toBeVisible();
  });

  it("состояния: загрузка — занятая область со статусом, пусто — текст и иконка панели, не «всё хорошо»", () => {
    const { rerender } = render(
      <Panel title="Рейтинг" state="loading">
        тело
      </Panel>,
    );
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Загрузка: Рейтинг")).toBeInTheDocument();
    expect(screen.queryByText("тело")).toBeNull();
    rerender(
      <Panel title="Рейтинг" icon="trophy" state="empty" emptyDescription="Источник пока не дал рейтинг">
        тело
      </Panel>,
    );
    expect(screen.getByText("Данных пока нет")).toBeInTheDocument();
    expect(screen.getByText("Источник пока не дал рейтинг")).toBeInTheDocument();
    expect(screen.queryByText("тело")).toBeNull();
  });

  it("одна рамка: внутри панели составные блоки без своих рамок", () => {
    const spec: ChartSpec = {
      type: "bar",
      title: "Оценки по предметам",
      labels: ["Алгоритмы", "Матанализ"],
      series: [{ name: "Оценка", values: [8, 9] }],
    };
    const { container } = render(
      <Panel title="Оценки по предметам" icon="chart">
        <ChartBlock spec={spec} bare tableToggle={false} />
        <ProfileCard fields={[{ label: "Группа", value: "БИ-21" }]} />
      </Panel>,
    );
    const frames = container.querySelectorAll(".rounded-lg.border");
    expect(frames).toHaveLength(1);
    expect(frames[0]).toBe(container.querySelector("[data-panel]"));
  });
});

describe("ChartBlock: режим bare", () => {
  const spec: ChartSpec = {
    type: "line",
    title: "Динамика",
    labels: ["Сентябрь", "Октябрь", "Ноябрь"],
    series: [{ name: "Балл", values: [7, 8, 9] }],
  };

  it("по умолчанию график как в чате: рамка, видимый заголовок, кнопка «Показать таблицу»", () => {
    const { container } = render(<ChartBlock spec={spec} />);
    const figure = container.querySelector("figure") as HTMLElement;
    expect(figure.className).toContain("border");
    expect(figure.className).toContain("bg-surface");
    expect(figure).not.toHaveAttribute("data-bare");
    expect(within(figure).getByText("Динамика", { selector: "figcaption" }).className).not.toContain(
      "sr-only",
    );
    expect(screen.getByRole("button", { name: "Показать таблицу" })).toBeInTheDocument();
  });

  it("bare: без рамки и фона, заголовок только для скринридера, график и таблица данных на месте", () => {
    const { container } = render(<ChartBlock spec={spec} bare />);
    const figure = container.querySelector("figure") as HTMLElement;
    expect(figure).toHaveAttribute("data-bare");
    expect(figure.className).not.toMatch(/border|bg-surface|(^|\s)p-3/);
    expect(within(figure).getByText("Динамика", { selector: "figcaption" }).className).toContain("sr-only");
    expect(screen.getByRole("img", { name: /Линейный график «Динамика»/ })).toBeInTheDocument();
    expect(container.querySelector("[data-chart-table] table")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Показать таблицу" })).toBeInTheDocument();
  });

  it("tableToggle={false}: кнопки нет, таблица для скринридера остаётся", () => {
    const { container } = render(<ChartBlock spec={spec} bare tableToggle={false} />);
    expect(screen.queryByRole("button")).toBeNull();
    const table = container.querySelector("[data-chart-table]") as HTMLElement;
    expect(table.className).toContain("sr-only");
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });
});
