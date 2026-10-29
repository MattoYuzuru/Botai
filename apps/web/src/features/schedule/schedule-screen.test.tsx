import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { eventFixture, hrefOf, provenanceFixture } from "@/test/data-fixtures";
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

describe("«Расписание»: карточка пары по пресету eventDetails", () => {
  const openRichEvent = async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    await userEvent.click(within(list).getByRole("button", { name: /Алгоритмы и структуры данных/ }));
    return screen.findByRole("dialog", { name: "Алгоритмы и структуры данных" });
  };

  it("в списке дня у пары бейдж типа словами источника и детали с иконками: аудитория, адрес, преподаватель, группа", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    const card = within(list).getByRole("button", { name: /Алгоритмы и структуры данных/ });
    expect(within(card).getByText("Текущий рубежный контроль")).toBeInTheDocument();
    const facts = [...card.querySelectorAll("[data-fact]")].map((fact) => [
      fact.getAttribute("data-fact"),
      fact.querySelector("svg")?.getAttribute("data-icon"),
      fact.textContent,
    ]);
    expect(facts).toEqual([
      ["room", "room", "Аудитория: ауд. 412"],
      ["address", "address", "Адрес: ул. Учебная, д. 11"],
      ["attribute-0", "building", "Корпус: Главный корпус"],
      ["teacher", "teacher", "Преподаватель: Сидорова Е. Н."],
      ["group", "group", "Группа: ПМ-3 курс 26/27"],
    ]);
  });

  it("у пары без деталей (онлайн-лекция ЦУ без аудитории) нет ни пустых строк, ни прочерков", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    const card = within(list).getByRole("button", { name: /Английский язык/ });
    expect(card.querySelector("[data-fact=address]")).toBeNull();
    expect(card.querySelector("[data-fact=room]")).toBeNull();
    expect(card).not.toHaveTextContent("—");
  });

  it("шит: тип занятия бейджем, время, номер пары, аудитория, адрес, здание, преподаватель, группа — каждое со своей иконкой", async () => {
    const sheet = await openRichEvent();
    expect(within(sheet).getByText("Текущий рубежный контроль")).toBeInTheDocument();
    const rows = [...sheet.querySelectorAll("dl > div")].map((row) => [
      row.querySelector("svg")?.getAttribute("data-icon"),
      row.querySelector("dt")?.textContent,
      row.querySelector("dd")?.textContent,
    ]);
    expect(rows).toEqual([
      ["time", "Время", "11:10–12:30"],
      ["number", "Номер пары", "3-я пара"],
      ["room", "Аудитория", "ауд. 412"],
      ["address", "Адрес", "ул. Учебная, д. 11"],
      ["building", "Корпус", "Главный корпус"],
      ["teacher", "Преподаватель", "Сидорова Е. Н."],
      ["group", "Группа", "ПМ-3 курс 26/27"],
      ["course", "Предмет", "Алгоритмы и структуры данных"],
      ["info", "Форма контроля", "Контрольная работа"],
    ]);
  });

  it("онлайн-ссылка занятия — кнопка с подписью источника, открывается в новой вкладке", async () => {
    const sheet = await openRichEvent();
    const link = within(sheet).getByRole("link", { name: "Материалы занятия" });
    expect(link).toHaveAttribute("href", "https://lk.fakeuni.test/materials/algorithms");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("«Открыть в личном кабинете» и источник ведут на страницу интерфейса, а не на адрес API", async () => {
    const sheet = await openRichEvent();
    const open = within(sheet).getByRole("link", { name: /Открыть в личном кабинете/ });
    const href = open.getAttribute("href") ?? "";
    expect(href).toMatch(/^https:\/\/lk\.fakeuni\.test\/schedule\?begin_date=2026-10-26$/);
    expect(href).not.toContain("/api/");
    const citation = within(sheet)
      .getByText(/Получено/)
      .closest("a");
    expect(citation?.getAttribute("href")).toBe(href);
    // Ни одна ссылка карточки не ведёт на API.
    for (const link of sheet.querySelectorAll("a[href]"))
      expect(link.getAttribute("href")).not.toContain("/api/");
  });

  it("если у записи нет openUrl, ссылка откатывается к странице входа источника, но не к адресу API", async () => {
    const space = current.db.spaces[0] as { id: string };
    const portal = current.db.connections.find((row) => row.spaceId === space.id && row.kind === "portal");
    const event = eventFixture({
      id: "no-ui",
      title: "Пара без страницы",
      openUrl: null,
      provenance: provenanceFixture({
        connectionId: portal?.id,
        spaceId: space.id,
        openUrl: null,
        sourceUrl: "https://lk.fakeuni.test/api/get-myschedule?begin_date=2026-10-26",
      }),
    });
    current.server.use(http.get("*/api/v1/schedule", () => HttpResponse.json([event])));
    renderApp("/schedule");
    await userEvent.click(await screen.findByRole("button", { name: /Пара без страницы/ }));
    const sheet = await screen.findByRole("dialog", { name: "Пара без страницы" });
    expect(within(sheet).getByRole("link", { name: /Открыть в личном кабинете/ })).toHaveAttribute(
      "href",
      "https://lk.fakeuni.test/",
    );
    for (const link of sheet.querySelectorAll("a[href]"))
      expect(link.getAttribute("href")).not.toContain("/api/");
  });

  it("онлайн-пара ЦУ: ссылка «Подключиться к занятию» и атрибут платформы", async () => {
    renderApp("/schedule");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const list = await screen.findByRole("region", { name: "вторник, 27 октября" });
    await userEvent.click(within(list).getByRole("button", { name: /Основы продуктового менеджмента/ }));
    const sheet = await screen.findByRole("dialog", { name: "Основы продуктового менеджмента" });
    expect(within(sheet).getByText("Онлайн")).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Подключиться к занятию" })).toHaveAttribute(
      "href",
      "https://my.fakeuni.test/meet/product-101",
    );
    expect(within(sheet).getByText("Видеосвязь")).toBeInTheDocument();
    expect(hrefOf(within(sheet).getByRole("link", { name: /Открыть в личном кабинете/ })).pathname).toBe(
      "/schedule",
    );
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
    expect(within(notices).getByRole("link", { name: "Войти снова" })).toBeInTheDocument();
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
