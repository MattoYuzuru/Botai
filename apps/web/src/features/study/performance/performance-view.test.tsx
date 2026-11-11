import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import { createPanelsHandlers } from "@/mocks/panels-handlers";
import { hrefOf } from "@/test/data-fixtures";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useServer(variant: DataVariant | null = null) {
  current.server.close();
  current = createDataServer("connected", variant);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useServer());
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  window.sessionStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const region = (name: string) => screen.findByRole("region", { name });
const card = (course: string) => screen.findByRole("button", { name: new RegExp(`^${course}\\.`) });

describe("«Учёба» → «Успеваемость» (D-046)", () => {
  it("открывается успеваемостью; переключатель меняет раздел и пишет его в адрес", async () => {
    const { router } = renderApp("/study");
    expect(await screen.findByRole("heading", { level: 2, name: "Предметы" })).toBeInTheDocument();
    const toggle = screen.getByRole("radiogroup", { name: "Раздел «Учёбы»" });
    expect(within(toggle).getByRole("radio", { name: "Успеваемость" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    // Источников и настроек в этом разделе нет.
    expect(screen.queryByRole("region", { name: /^Высшая школа экономики/ })).toBeNull();

    await userEvent.click(within(toggle).getByRole("radio", { name: "Источники" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ view: "sources" }));
    expect(await screen.findByRole("region", { name: /^Высшая школа экономики/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Предметы" })).toBeNull();
  });

  it("карточка предмета: результат словами источника, число работ, без средних и процентов", async () => {
    renderApp("/study");
    const math = await card("Математический анализ");
    expect(math).toHaveAccessibleName(/Накоплено \(1 модуль\): 7,6 из 10/);
    expect(math).toHaveTextContent("Проверено: 4 работы");
    expect(await card("Теория вероятностей")).toHaveAccessibleName(
      /Итог \(1 модуль 2026\/27\): зачтено, зачёт/,
    );
    // Предмет только с работами показывает последнюю оценку, а не выдуманный итог.
    expect(await card("Дискретная математика")).toHaveAccessibleName(/Последняя оценка: 7 из 10/);
    // БРС: накопленное по 100-балльной шкале как дал источник.
    expect(await card("Продуктовый менеджмент")).toHaveAccessibleName(/Накоплено \(Осень 2026\): 64 из 100/);
    const courses = await region("Предметы");
    expect(courses.textContent).not.toMatch(/средн|\d\s*%/i);
    // Предмет только с итогом прошлого периода живёт в зачётке, не карточкой.
    expect(screen.queryByRole("button", { name: /^Линейная алгебра\./ })).toBeNull();
  });

  it("лист предмета: накопленное, работы с баллом и весом, комментарий проверяющего, ссылка на оценку", async () => {
    renderApp("/study");
    await userEvent.click(await card("Математический анализ"));
    const sheet = await screen.findByRole("dialog", { name: "Математический анализ" });
    expect(within(sheet).getByRole("region", { name: "Накоплено" })).toHaveTextContent("7,6из 10");
    const works = within(sheet).getByRole("region", { name: /Работы/ });
    expect(within(works).getAllByRole("listitem")).toHaveLength(4);
    expect(works).toHaveTextContent("вес 0,2");
    expect(within(works).getByText(/не обоснован переход к пределу/)).toBeInTheDocument();
    expect(within(works).getByText("Комментарий проверяющего")).toBeInTheDocument();
    expect(within(works).getByRole("link", { name: "Открыть в источнике: Контрольная №2" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^https:\/\/lk\.fakeuni\.test\/grades\/\d+$/),
    );
    expect(within(sheet).getByText(/Получено/)).toBeInTheDocument();
  });

  it("«Новое»: свежие изменения, «Показать все», оценка открывает лист предмета с подсвеченной работой", async () => {
    renderApp("/study");
    const updates = await region("Новое");
    const rows = within(updates).getAllByRole("listitem");
    // На телефоне сначала три записи: предметы должны остаться на первом экране.
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Лабораторная: алгоритмы сортировки");
    expect(rows[0]).toHaveTextContent("На доработке");
    await userEvent.click(within(updates).getByRole("button", { name: /Показать все/ }));
    expect(within(updates).getAllByRole("listitem").length).toBeGreaterThan(3);

    await userEvent.click(within(updates).getByRole("button", { name: /Контрольная №2/ }));
    const sheet = await screen.findByRole("dialog", { name: "Математический анализ" });
    const focused = sheet.querySelector("[data-focused]");
    expect(focused).toHaveTextContent("Контрольная №2");
    expect(focused).toHaveTextContent("Новое");
  });

  it("«Новое»: смена статуса задания открывает карточку задания", async () => {
    renderApp("/study");
    const updates = await region("Новое");
    await userEvent.click(
      within(updates).getByRole("button", { name: /Лабораторная: алгоритмы сортировки/ }),
    );
    const sheet = await screen.findByRole("dialog", { name: "Лабораторная: алгоритмы сортировки" });
    expect(within(sheet).getByRole("button", { name: "Отметить сданным" })).toBeInTheDocument();
  });

  it("зачётка по периодам и сводные показатели источника", async () => {
    renderApp("/study");
    const book = await region("Зачётка");
    const periods = within(book)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(periods).toEqual(["ВШЭ · 1 модуль 2026/27", "ВШЭ · 2025/26, весна"]);
    expect(within(book).getByRole("button", { name: /Линейная алгебра/ })).toHaveTextContent("9 из 10");
    await userEvent.click(within(book).getByRole("button", { name: /Показать всю зачётку/ }));
    expect(within(book).getByText("ВШЭ · 2025/26, осень")).toBeInTheDocument();
    const stats = await region("Показатели");
    expect(within(stats).getByRole("group", { name: "GPA" })).toHaveTextContent("4,6/ из 5");
    expect(within(stats).getByRole("group", { name: "Средний балл" })).toHaveTextContent("8,62/ из 10");
    expect(within(stats).getByRole("group", { name: "Кредиты" })).toHaveTextContent("60кредитов");

    // Строка зачётки открывает лист предмета с разделом «Итог».
    await userEvent.click(within(book).getByRole("button", { name: /Английский язык/ }));
    const sheet = await screen.findByRole("dialog", { name: "Английский язык" });
    expect(within(sheet).getByRole("region", { name: "Итог" })).toHaveTextContent("отлично");
  });

  it("панели D-039 — ниже, под заголовком «Панели», без второй «Успеваемости»", async () => {
    renderApp("/study");
    expect(await screen.findByRole("heading", { level: 2, name: "Панели" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Успеваемость" })).toBeNull();
  });

  it("один вуз в переключателе — только его предметы и «Новое»", async () => {
    useActiveSpaceStore.setState({ id: (current.db.spaces[1] as { id: string }).id });
    renderApp("/study");
    expect(await card("Продуктовый менеджмент")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Математический анализ\./ })).toBeNull();
  });
});

describe("«Успеваемость»: старые записи без слоёв (форма production)", () => {
  it("каждая оценка за курс — своя карточка «Результат»; подпись — не шкала; «Без предмета» — только работы", async () => {
    useServer("legacy");
    renderApp("/study");
    const obzh = await card("Безопасность жизнедеятельности \\(2025\\/2026 модули: 1,2\\)");
    expect(obzh).toHaveAccessibleName(/Результат: 5, оценка LMS; ещё 1/);
    expect(obzh).not.toHaveTextContent("Проверено");
    expect(obzh.querySelector("[data-score-strip]")).toBeNull();
    expect(obzh.querySelector("[data-headline-meta]")).toHaveTextContent("Результат · оценка LMS · ещё 1");
    const none = await card("Без предмета");
    expect(none).toHaveTextContent("Проверено: 5 работ");
    expect(none).toHaveAccessibleName(/Последняя оценка: 17 из 20/);
    // Чипов без числа не больше одного, остальные свёрнуты в «+N».
    expect(none.querySelector("[data-score-more]")).toHaveTextContent("+2");

    await userEvent.click(obzh);
    const sheet = await screen.findByRole("dialog", { name: /^Безопасность жизнедеятельности/ });
    const results = within(sheet).getByRole("region", { name: "Результат" });
    expect(
      within(results)
        .getAllByRole("listitem")
        .map((row) => row.textContent),
    ).toEqual([expect.stringContaining("5"), expect.stringContaining("5,44")]);
    expect(results).toHaveTextContent("оценка LMS");
  });

  it("на телефоне при непустых «Предметах» панели свёрнуты под «Показать панели», «Настроить» доступно", async () => {
    renderApp("/study");
    const toggle = await screen.findByRole("button", { name: /Показать панели/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: "Настроить" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Рейтинг потока" })).toBeNull();
    await userEvent.click(toggle);
    expect(await screen.findAllByRole("heading", { name: "Рейтинг потока" })).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "Скрыть панели" })).toHaveAttribute("aria-expanded", "true");
  });
});

describe("«Успеваемость»: пусто", () => {
  it("ни оценок, ни «Нового», ни панелей — экран открывается источниками", async () => {
    useServer("quiet");
    current.server.use(...createPanelsHandlers(current.db, { variant: "none" }));
    renderApp("/study");
    expect(await screen.findByRole("region", { name: /^Высшая школа экономики/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Источники" })).toHaveAttribute("aria-checked", "true");
  });

  it("явно выбранная «Успеваемость» без оценок — честный текст и «Найти оценки» в чат", async () => {
    useServer("quiet");
    current.server.use(...createPanelsHandlers(current.db, { variant: "none" }));
    renderApp("/study?view=grades");
    expect(await screen.findByRole("heading", { name: "Оценок пока нет" })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Найти оценки" });
    expect(hrefOf(link).pathname).toBe("/chat");
    expect(hrefOf(link).draft).toContain("Найди мои оценки во всех вузах");
  });
});
