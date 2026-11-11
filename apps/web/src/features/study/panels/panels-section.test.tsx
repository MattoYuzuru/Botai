import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import { createPanelsHandlers } from "@/mocks/panels-handlers";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
/**
 * Панели — ноутбучной шириной: на телефоне «Успеваемость» сворачивает их под кнопку (это проверяет
 * `performance-view.test.tsx`), здесь проверяются сами панели.
 */
function emulateLaptop() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("min-width: 1024px"),
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

beforeEach(emulateLaptop);
beforeEach(() => {
  current.server.close();
  current = createDataServer("connected");
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const hse = () => current.db.spaces[0] as { id: string };
const section = async () =>
  (await screen.findByRole("heading", { level: 2, name: "Панели" })).closest("section") as HTMLElement;
const problem = (status: number, code: string) =>
  new HttpResponse(
    JSON.stringify({
      type: `https://botai.mnema.app/errors/${code}`,
      title: code,
      status,
      code,
      detail: "Сбой",
    }),
    { status, headers: { "content-type": "application/problem+json" } },
  );

describe("«Успеваемость»: панели из реестра", () => {
  it("«Все вузы»: группы по вузам, показатели, график, рейтинг и профиль только из ответа сервера", async () => {
    renderApp("/study");
    const panels = await section();
    // Внутри «Успеваемости» (D-046) блок подписан «Панели»: двух «Успеваемостей» на экране нет.
    expect(within(panels).getByRole("heading", { level: 2, name: "Панели" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Успеваемость" })).toBeNull();
    const groups = await within(panels).findAllByRole("group", { name: /экономики|Центральный/ });
    expect(groups.map((group) => group.getAttribute("data-panels-space"))).toEqual([
      hse().id,
      (current.db.spaces[1] as { id: string }).id,
    ]);

    const first = groups[0] as HTMLElement;
    // KPI: значение и шкала источника, без вычислений.
    const tiles = within(first).getByRole("group", { name: "Показатели" });
    const latest = within(tiles).getByRole("group", { name: "Последняя оценка" });
    expect(latest).toHaveTextContent("8/ из 10");
    expect(latest).toHaveTextContent("Контрольная №2");
    expect(within(tiles).getByRole("group", { name: "Лучшая оценка" })).toHaveTextContent("10/ из 10");
    // 10 из 11 оценок за работы числовые: подпись говорит об этом словами, без выдуманного «среднего».
    expect(within(tiles).getByRole("group", { name: "Оценок" })).toHaveTextContent("11из них с баллом: 10");
    // График со шкалой источника.
    // Шкала названа словами источника (и у графика, и у предпросмотра предложения агента).
    expect(within(first).getAllByText("Шкала источника: 10 баллов").length).toBeGreaterThan(0);
    expect(
      within(first).getByRole("img", { name: /Линейный график «Оценки, шкала 10 баллов»/ }),
    ).toBeInTheDocument();
    // Рейтинг: «это вы» и итог.
    const rating = within(first).getByRole("list", { name: "Рейтинг потока" });
    expect(within(rating).getByText("Вы").closest("li")).toHaveAttribute("aria-current", "true");
    expect(within(first).getByText("Вы: 12 из 140")).toBeInTheDocument();
    // Профиль.
    expect(within(first).getByText("Анна Тестова")).toBeInTheDocument();
    expect(within(first).getByText("БИ-21")).toBeInTheDocument();
    // Панель с hiddenInAll в этом режиме не показана; панелей другого вуза в группе нет.
    expect(within(panels).queryByRole("heading", { name: "Последние оценки" })).toBeNull();
    // Средних и процентов нет нигде.
    expect(panels.textContent).not.toMatch(/средн|\d\s*%/i);
  });

  it("один вуз: его панели без группы, панель hiddenInAll снова на месте, вузов других нет", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    renderApp("/study");
    const panels = await section();
    await within(panels).findByRole("heading", { level: 3, name: "Рейтинг потока" });
    expect(within(panels).getByRole("heading", { level: 3, name: "Последние оценки" })).toBeInTheDocument();
    expect(within(panels).queryByText("Центральный университет")).toBeNull();
    expect(within(panels).queryByText("Проектная работа, этап 2", { exact: false })).toBeNull();
  });

  it("список и таблица: «значение / шкала» у каждой оценки, даты словами", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    renderApp("/study");
    const list = await screen.findByRole("list", { name: "Последние оценки" });
    const row = within(list).getByText("Контрольная №2").closest("li") as HTMLElement;
    expect(row).toHaveTextContent("8 / 10");
    expect(row).toHaveTextContent("Математический анализ");
    expect(row).toHaveTextContent("26 октября");
  });

  it("неизвестный тип панели скрыт, остальные на месте", async () => {
    current.server.use(...createPanelsHandlers(current.db, { variant: "unknown" }));
    renderApp("/study");
    await section();
    await screen.findAllByRole("heading", { name: "Рейтинг потока" });
    expect(screen.queryByText("from-the-future")).toBeNull();
    expect(document.querySelector('[data-panel-type="heatmap"]')).toBeNull();
  });

  it("нет панелей — блока нет; сервер без панелей (404) — тоже", async () => {
    current.server.use(...createPanelsHandlers(current.db, { variant: "none" }));
    renderApp("/study");
    await screen.findByRole("heading", { level: 1, name: "Учёба" });
    await screen.findByRole("heading", { level: 2, name: "Предметы" });
    expect(screen.queryByRole("heading", { name: "Панели" })).toBeNull();
  });

  it("сбой сервера: спокойная строка, остальная «Учёба» работает", async () => {
    current.server.use(http.get("*/api/v1/panels", () => problem(500, "internal_error")));
    renderApp("/study");
    expect(await screen.findByText(/Панели успеваемости не загрузились/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "Предметы" })).toBeInTheDocument();
  });

  it("v3-движок (404): блока нет и ошибки нет", async () => {
    current.server.use(http.get("*/api/v1/panels", () => problem(404, "not_found")));
    renderApp("/study");
    await screen.findByRole("heading", { level: 2, name: "Предметы" });
    expect(screen.queryByText(/Панели успеваемости не загрузились/)).toBeNull();
    expect(screen.queryByRole("heading", { name: "Панели" })).toBeNull();
  });

  it("пустые данные: честный текст вместо пустой рамки", async () => {
    current.server.use(
      http.get("*/api/v1/panels", ({ request }) => {
        const status = new URL(request.url).searchParams.get("status");
        if (status === "proposed") return HttpResponse.json([]);
        const base = current.db.panels.find((item) => item.id === "panel-u-hse-ranking:potok-1a2b");
        const empty = {
          ...base,
          space: { id: hse().id, title: "Высшая школа экономики", shortTitle: "ВШЭ", accentHue: 262.2 },
          data: { kind: "ranking", board: "Пустой", total: 0, participants: null, period: null, rows: [] },
        };
        const grades = {
          ...empty,
          id: "g",
          type: "kpi",
          spec: {
            v: 1,
            type: "kpi",
            title: "Оценки курса",
            icon: "grade",
            dataset: { kind: "grades" },
            view: {},
          },
          data: { kind: "grades", total: 0, numeric: 0, points: [], latest: null, best: null },
        };
        return HttpResponse.json([empty, grades]);
      }),
    );
    useActiveSpaceStore.setState({ id: hse().id });
    renderApp("/study");
    expect(await screen.findByText("Рейтинга пока нет")).toBeInTheDocument();
    expect(screen.getByText("Оценок пока нет")).toBeInTheDocument();
  });

  it("сворачивание помнится в браузере по пользователю", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    const user = userEvent.setup();
    const first = renderApp("/study");
    const heading = await screen.findByRole("heading", { level: 3, name: "Рейтинг потока" });
    await user.click(within(heading).getByRole("button", { name: /Рейтинг потока/ }));
    const stored = Object.entries(window.localStorage).filter(([key]) =>
      key.startsWith("botai.study.panels.collapsed."),
    );
    expect(stored).toHaveLength(1);
    expect(stored[0]?.[1]).toContain("panel-u-hse-ranking:potok-1a2b");
    first.unmount();
    renderApp("/study");
    const again = await screen.findByRole("heading", { level: 3, name: "Рейтинг потока" });
    expect(within(again).getByRole("button", { name: /Рейтинг потока/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

describe("настройка панелей", () => {
  async function openSettings() {
    const user = userEvent.setup();
    renderApp("/study");
    await section();
    await screen.findAllByRole("heading", { name: "Рейтинг потока" });
    await user.click(screen.getByRole("button", { name: "Настроить" }));
    const sheet = await screen.findByRole("dialog", { name: "Панели успеваемости" });
    return { user, sheet };
  }
  const row = (sheet: HTMLElement, title: string) =>
    within(sheet).getAllByRole("group", { name: new RegExp(`^${title}`) })[0] as HTMLElement;

  it("показывает все панели вуза, в том числе скрытые в «Все вузы», с двумя переключателями", async () => {
    const { sheet } = await openSettings();
    const recent = row(sheet, "Последние оценки");
    expect(within(recent).getByRole("switch", { name: "Показывать" })).toBeChecked();
    expect(within(recent).getByRole("switch", { name: "Показывать в «Все вузы»" })).not.toBeChecked();
    expect(
      within(row(sheet, "Рейтинг потока")).getByRole("switch", { name: "Показывать в «Все вузы»" }),
    ).toBeChecked();
  });

  it("«Показывать»: панель исчезает сразу и остаётся выключенной после ответа сервера", async () => {
    const { user, sheet } = await openSettings();
    const toggle = within(row(sheet, "Рейтинг потока")).getByRole("switch", { name: "Показывать" });
    await user.click(toggle);
    expect(toggle).not.toBeChecked();
    await waitFor(() =>
      expect(current.db.panels.find((item) => item.key === "ranking:potok-1a2b")?.visible).toBe(false),
    );
    await waitFor(() => expect(document.querySelector('[data-panel-type="ranking"]')).toBeNull());
    // Выключенная панель остаётся в настройках: её можно вернуть.
    expect(
      within(row(sheet, "Рейтинг потока")).getByRole("switch", { name: "Показывать" }),
    ).not.toBeChecked();
  });

  it("«Показывать в “Все вузы”»: панель уходит из сводного вида, но остаётся включённой", async () => {
    const { user, sheet } = await openSettings();
    const toggle = within(row(sheet, "Профиль")).getByRole("switch", { name: "Показывать в «Все вузы»" });
    await user.click(toggle);
    expect(toggle).not.toBeChecked();
    await waitFor(() =>
      expect(current.db.panels.find((item) => item.key === "student-profile")?.hiddenInAll).toBe(true),
    );
    expect(within(row(sheet, "Профиль")).getByRole("switch", { name: "Показывать" })).toBeChecked();
  });

  it("ошибка сервера: переключатель возвращается, говорит тост, данные не потеряны", async () => {
    current.server.use(http.patch("*/api/v1/panels/:id", () => problem(500, "internal_error")));
    const { user, sheet } = await openSettings();
    const toggle = within(row(sheet, "Рейтинг потока")).getByRole("switch", { name: "Показывать" });
    await user.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(await screen.findByText("Не удалось изменить панель")).toBeInTheDocument();
    expect(current.db.panels.find((item) => item.key === "ranking:potok-1a2b")?.visible).toBe(true);
    expect(document.querySelector('[data-panel-type="ranking"]')).not.toBeNull();
  });
});

describe("предложения агента", () => {
  const proposal = () =>
    screen.findByRole("article", { name: /Агент предлагает добавить панель «Оценки по предметам»/ });

  it("карточка с предпросмотром и двумя действиями; «Добавить» делает панель активной", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    const user = userEvent.setup();
    renderApp("/study");
    const card = await proposal();
    expect(
      within(card).getByRole("img", { name: "Предпросмотр панели «Оценки по предметам»" }),
    ).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: /Добавить панель «Оценки по предметам»/ }));
    await waitFor(() => expect(screen.queryByRole("article", { name: /Агент предлагает/ })).toBeNull());
    const item = current.db.panels.find((panel) => panel.key === "grades-by-course");
    expect(item).toMatchObject({ status: "active", visible: true });
    expect(await screen.findByRole("heading", { name: "Оценки по предметам" })).toBeInTheDocument();
  });

  it("предпросмотр столбчатого графика не обрезан: ось доходит до нуля", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    renderApp("/study");
    const card = await proposal();
    const preview = card.querySelector("[data-panel-preview]") as HTMLElement;
    // Оси y графика в SVG: подписи делений, нулевая — последняя. Обрезка по высоте её скрыла бы.
    const ticks = [...preview.querySelectorAll("svg text")].map((node) => node.textContent);
    expect(ticks).toContain("0");
    expect(preview.closest("[role=img]")?.className).not.toMatch(/max-h|overflow-hidden/);
  });

  it("«Не сейчас» отклоняет предложение, панель не появляется", async () => {
    useActiveSpaceStore.setState({ id: hse().id });
    const user = userEvent.setup();
    renderApp("/study");
    const card = await proposal();
    await user.click(within(card).getByRole("button", { name: /Не сейчас: панель/ }));
    await waitFor(() => expect(screen.queryByRole("article", { name: /Агент предлагает/ })).toBeNull());
    expect(current.db.panels.find((panel) => panel.key === "grades-by-course")?.status).toBe("dismissed");
    expect(screen.queryByRole("heading", { name: "Оценки по предметам" })).toBeNull();
  });

  it("ошибка принятия: карточка возвращается и объясняет", async () => {
    current.server.use(http.post("*/api/v1/panels/:id/accept", () => problem(409, "conflict")));
    useActiveSpaceStore.setState({ id: hse().id });
    const user = userEvent.setup();
    renderApp("/study");
    const card = await proposal();
    await user.click(within(card).getByRole("button", { name: /Добавить панель/ }));
    expect(await screen.findByText("Не удалось добавить панель")).toBeInTheDocument();
    expect(await proposal()).toBeInTheDocument();
  });
});
