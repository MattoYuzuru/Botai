import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useScenario(scenario: Scenario, variant: DataVariant | null = null) {
  current.server.close();
  current = createDataServer(scenario, variant);
  current.server.listen({ onUnhandledRequest: "error" });
}

/** Ноутбук: `(min-width: 64rem)` совпадает, остальные запросы — нет. */
function useLaptop() {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query: string) =>
      ({
        matches: query.includes("min-width: 64rem"),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList,
  );
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
  vi.restoreAllMocks();
  window.dispatchEvent(new Event("online"));
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const screenState = () => document.querySelector("[data-screen=schedule]")?.getAttribute("data-state");
const dayButtons = async () =>
  within(await screen.findByRole("navigation", { name: "Дни недели" })).getAllByRole("button");

describe("«Расписание»: неделя и день", () => {
  it("лента из семи дней, выбран сегодняшний; в списке пары дня из обоих вузов по времени", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const days = await dayButtons();
    expect(days).toHaveLength(7);
    expect(days[1]).toHaveAttribute("aria-pressed", "true");
    expect(days[1]).toHaveAttribute("aria-current", "date");
    const list = screen.getByRole("region", { name: "вторник, 27 октября" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.querySelector("time")?.textContent)).toEqual([
      "09:30",
      "11:10",
      "11:30",
      "15:00",
    ]);
    expect(within(list).getByText("ЦУ")).toBeInTheDocument();
  });

  it("выбор другого дня меняет список; день без пар — «Пар нет», а не пустота", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const days = await dayButtons();
    await userEvent.click(days[2] as HTMLElement); // среда
    const list = await screen.findByRole("region", { name: "среда, 28 октября" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByText("Дискретная математика")).toBeInTheDocument();
    await userEvent.click((await dayButtons())[6] as HTMLElement); // воскресенье
    expect(await screen.findByText("Пар нет")).toBeInTheDocument();
  });

  it("пересечения между вузами видны: полоса недели, метка в ленте дней и на строках", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    expect(
      await screen.findByText("4 пары пересекаются по времени с парами другого вуза"),
    ).toBeInTheDocument();
    const days = await dayButtons();
    expect(within(days[1] as HTMLElement).getByText(/есть пересечение/)).toBeInTheDocument();
    expect(within(days[0] as HTMLElement).queryByText(/есть пересечение/)).toBeNull();
    const list = screen.getByRole("region", { name: "вторник, 27 октября" });
    expect(within(list).getAllByText("Пересекается по времени")).toHaveLength(2);
  });

  it("недели листаются кнопками, «Сегодня» возвращает; границы недели — понедельник и воскресенье", async () => {
    renderApp("/schedule");
    expect(await screen.findByText("26 октября – 1 ноября")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Сегодня" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Следующая неделя" }));
    expect(await screen.findByText("2 ноября – 8 ноября")).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("ok"));
    expect(screen.getByRole("region", { name: "вторник, 3 ноября" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Предыдущая неделя" }));
    await userEvent.click(screen.getByRole("button", { name: "Предыдущая неделя" }));
    expect(await screen.findByText("19 октября – 25 октября")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Сегодня" }));
    expect(await screen.findByText("26 октября – 1 ноября")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "вторник, 27 октября" })).toBeInTheDocument();
  });

  it("фильтр по вузу: при выборе ЦУ остаются только пары ЦУ, запрос уходит с пространством", async () => {
    const requested: string[] = [];
    current.server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/v1/schedule")) requested.push(request.url);
    });
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    await userEvent.click(await screen.findByRole("button", { name: /Все вузы/ }));
    const sheet = await screen.findByRole("dialog", { name: "Вуз" });
    await userEvent.click(within(sheet).getByRole("button", { name: /Центральный университет/ }));
    await waitFor(() => expect(requested.some((url) => url.includes("space_id="))).toBe(true));
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(1));
    expect(within(list).getByText("Основы продуктового менеджмента")).toBeInTheDocument();
    expect(within(list).queryByText("Математический анализ")).toBeNull();
  });

  it("карточка пары из отфильтрованной недели знает пару другого вуза, с которой есть пересечение", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const spaces = current.db.spaces;
    useActiveSpaceStore.getState().select((spaces[1] as { id: string }).id);
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    await userEvent.click(
      await within(list).findByRole("button", { name: /Основы продуктового менеджмента/ }),
    );
    const sheet = await screen.findByRole("dialog", { name: "Основы продуктового менеджмента" });
    expect(
      await within(sheet).findByText(/«Алгоритмы и структуры данных», ВШЭ, 11:10–12:30/),
    ).toBeInTheDocument();
  });
});

describe("«Расписание»: ноутбук", () => {
  it("неделя сеткой в семь колонок вместо ленты дней, пересечения подписаны", async () => {
    useLaptop();
    renderApp("/schedule");
    const grid = await screen.findByRole("region", { name: "Неделя" });
    await waitFor(() => expect(screenState()).toBe("ok"));
    expect(screen.queryByRole("navigation", { name: "Дни недели" })).toBeNull();
    expect(within(grid).getAllByRole("heading", { level: 3 })).toHaveLength(7);
    expect(within(grid).getAllByText("Пересечение")).toHaveLength(4);
    expect(within(grid).getAllByText("Пар нет")).toHaveLength(2);
    await userEvent.click(
      within(grid).getAllByRole("button", { name: /Математический анализ/ })[0] as HTMLElement,
    );
    expect(await screen.findByRole("dialog", { name: "Математический анализ" })).toBeInTheDocument();
  });
});

describe("«Расписание»: состояния данных", () => {
  it("нет вузов — приглашение подключить", async () => {
    useScenario("empty");
    renderApp("/schedule");
    expect(await screen.findByText("Вузов пока нет")).toBeInTheDocument();
  });

  it("empty: расписание получено, но пар нет — «Пар нет» без полосы пересечений", async () => {
    useScenario("connected", "quiet");
    renderApp("/schedule");
    expect(await screen.findByText("Пар нет")).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("empty"));
    expect(screen.queryByText(/пересека/)).toBeNull();
  });

  it("reauth: полоса «Войти снова», данные вуза остаются на экране", async () => {
    useScenario("reauth");
    renderApp("/schedule");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByRole("button", { name: "Войти снова" })).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("reauth"));
    expect(await screen.findByText("Основы продуктового менеджмента")).toBeInTheDocument();
  });

  it("repairing: «Варя чинит» и прежнее расписание, кнопок повтора нет", async () => {
    useScenario("connected", "repairing");
    renderApp("/schedule");
    expect((await screen.findAllByText("Вуз что-то поменял, Варя чинит")).length).toBeGreaterThan(0);
    await waitFor(() => expect(screenState()).toBe("repairing"));
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
    expect(await screen.findByText("Математический анализ")).toBeInTheDocument();
  });

  it("offline: последний снимок, подпись «Нет сети. Показан снимок от …»", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    window.dispatchEvent(new Event("offline"));
    expect((await screen.findAllByText(/Нет сети\. Показан снимок от/)).length).toBeGreaterThan(0);
    await waitFor(() => expect(screenState()).toBe("offline"));
    expect(screen.getAllByText("Математический анализ").length).toBeGreaterThan(0);
  });

  it("loading: скелетон дня, пока неделя не пришла, и никаких кнопок повтора", async () => {
    current.server.use(http.get("*/api/v1/schedule", () => new Promise(() => {})));
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("loading"));
    await screen.findByRole("heading", { level: 1, name: "Расписание" });
    expect(document.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
  });
});
