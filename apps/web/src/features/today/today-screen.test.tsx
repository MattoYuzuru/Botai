import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { hrefOf } from "@/test/data-fixtures";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useScenario(scenario: Scenario, variant: DataVariant | null = null) {
  current.server.close();
  current = createDataServer(scenario, variant);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  // Часы подменяем только у Date: таймеры настоящие, `waitFor` и запросы работают как обычно.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  vi.restoreAllMocks();
  // Менеджер сети React Query хранит «офлайн» между тестами: возвращаем сеть.
  window.dispatchEvent(new Event("online"));
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const screenState = () => document.querySelector("[data-screen=today]")?.getAttribute("data-state");

describe("«Сегодня»: пары и сроки", () => {
  it("пары сегодня из обоих вузов идут одной лентой по времени, у каждой метка вуза", async () => {
    renderApp("/");
    const classes = await screen.findByRole("region", { name: "Пары" });
    const rows = await within(classes).findAllByRole("listitem");
    expect(rows.map((row) => row.querySelector("time")?.textContent)).toEqual([
      "09:30",
      "11:10",
      "11:30",
      "15:00",
    ]);
    // Вуз подписан на каждой строке, чтобы две ленты не слились.
    expect(within(rows[0] as HTMLElement).getByText("ВШЭ")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("ЦУ")).toBeInTheDocument();
    expect(await screen.findByText("4 пары")).toBeInTheDocument();
  });

  it("пересечение пар разных вузов отмечено на обеих, изменённая пара — «Изменено»", async () => {
    renderApp("/");
    const classes = await screen.findByRole("region", { name: "Пары" });
    await within(classes).findAllByRole("listitem");
    expect(within(classes).getAllByText("Пересекается по времени")).toHaveLength(2);
    expect(within(classes).getAllByText("Изменено")).toHaveLength(1);
  });

  it("три ближайших срока: просроченный, горящий и скорый; сданные и без срока не показаны", async () => {
    renderApp("/");
    const deadlines = await screen.findByRole("region", { name: "Ближайшие сроки" });
    const rows = await within(deadlines).findAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(deadlines).getByText("Курсовая: выбор темы")).toBeInTheDocument();
    expect(within(deadlines).getByText("Просрочено")).toBeInTheDocument();
    expect(within(deadlines).getByText("Домашнее задание №4: ряды")).toBeInTheDocument();
    expect(within(deadlines).getByText("Осталось 5 ч")).toBeInTheDocument();
    expect(within(deadlines).getByText(/Скоро · 2 д/)).toBeInTheDocument();
    expect(within(deadlines).queryByText("Прочитать главы 3–4")).toBeNull();
    expect(within(deadlines).queryByText("Домашнее задание №3: пределы")).toBeNull();
    expect(within(deadlines).getByRole("link", { name: /Все задания/ })).toHaveAttribute("href", "/tasks");
  });

  it("свежие данные: у каждого вуза подпись свежести, полосы внимания нет, состояние ok", async () => {
    renderApp("/");
    await screen.findByRole("region", { name: "Пары" });
    await screen.findByText("Обновлено 5 минут назад");
    expect(screen.getByText("Обновлено 30 минут назад")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Состояние источников" })).toBeNull();
    await waitFor(() => expect(screenState()).toBe("ok"));
  });

  it("карточка пары: аудитория, преподаватель, пересекающаяся пара другого вуза и источник со временем", async () => {
    renderApp("/");
    const classes = await screen.findByRole("region", { name: "Пары" });
    await userEvent.click(
      await within(classes).findByRole("button", { name: /Алгоритмы и структуры данных/ }),
    );
    const sheet = await screen.findByRole("dialog", { name: "Алгоритмы и структуры данных" });
    expect(within(sheet).getByText("ауд. 412")).toBeInTheDocument();
    expect(within(sheet).getByText("Сидорова Е. Н.")).toBeInTheDocument();
    expect(within(sheet).getByText(/«Основы продуктового менеджмента», ЦУ, 11:30–13:00/)).toBeInTheDocument();
    expect(within(sheet).getByText(/Получено 12 минут назад/)).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть в личном кабинете/ })).toHaveAttribute(
      "href",
      expect.stringMatching(/^https:\/\/lk\.fakeuni\.test\//),
    );
  });

  it("карточка срока: срок, назначенный преподавателем, объяснён, условие — по ссылке на источник", async () => {
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: /Лабораторная: алгоритмы сортировки/ }));
    const sheet = await screen.findByRole("dialog", { name: "Лабораторная: алгоритмы сортировки" });
    expect(within(sheet).getByText("Срок по плану курса")).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть задание/ })).toBeInTheDocument();
  });
});

describe("«Сегодня»: состояния данных", () => {
  it("нет вузов — приглашение подключить, и это не «всё сдано»", async () => {
    useScenario("empty");
    renderApp("/");
    expect(await screen.findByText("Вузов пока нет")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Подключить вуз" });
    expect(hrefOf(link).pathname).toBe("/chat");
    expect(hrefOf(link).draft).toBeTruthy();
    expect(screen.queryByText(/сдано/i)).toBeNull();
  });

  it("loading: пока данных нет, скелетон с aria-busy и никаких кнопок повтора", async () => {
    current.server.use(http.get("*/api/v1/today", () => new Promise(() => {})));
    renderApp("/");
    await waitFor(() => expect(screenState()).toBe("loading"));
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
    expect(document.querySelector("[aria-busy=true]")).not.toBeNull();
  });

  it("empty: вузы подключены и свежи, но данных нет — «Пар нет» и «Ближайших сроков нет»", async () => {
    useScenario("connected", "quiet");
    renderApp("/");
    expect(await screen.findByText("Пар нет")).toBeInTheDocument();
    expect(screen.getByText("Ближайших сроков нет")).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("empty"));
    expect(screen.queryByText(/всё сдано/i)).toBeNull();
  });

  it("reauth: полоса «Нужно снова войти» с действием «Войти снова», данные помечены устаревшими", async () => {
    useScenario("reauth");
    renderApp("/");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByText(/Нужно снова войти в личный кабинет ЦУ/)).toBeInTheDocument();
    expect(within(notices).getByRole("link", { name: "Войти снова" })).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("reauth"));
    // Пары ЦУ остаются на экране, но отмечены устаревшими в подписи свежести.
    expect(await screen.findByText("Основы продуктового менеджмента")).toBeInTheDocument();
  });

  it("«Войти снова» без треда задачи ведёт в новый чат с готовой просьбой, ничего не запуская", async () => {
    useScenario("reauth");
    const { router } = renderApp("/");
    const link = await screen.findByRole("link", { name: "Войти снова" });
    expect(hrefOf(link).pathname).toBe("/chat");
    expect(hrefOf(link).draft).toContain("LMS");
    expect(hrefOf(link).draft).toContain("Центральный университет");
    await userEvent.click(link);
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search.draft).toContain("Центральный университет");
    expect(router.state.location.search.thread).toBeUndefined();
  });

  it("плашка источника с тредом задачи ведёт прямо в него: /chat?thread=<id>", async () => {
    useScenario("reauth");
    const cu = current.db.connections.find((row) => row.status === "needs_reauth");
    if (cu) cu.threadId = "01JTHREADTASK00000000000AA";
    const { router } = renderApp("/");
    const link = await screen.findByRole("link", { name: "Войти снова" });
    expect(hrefOf(link)).toEqual({ pathname: "/chat", thread: "01JTHREADTASK00000000000AA" });
    await userEvent.click(link);
    await waitFor(() => expect(router.state.location.search.thread).toBe("01JTHREADTASK00000000000AA"));
    expect(router.state.location.search.draft).toBeUndefined();
  });

  it("stale: подключение устарело — полоса объясняет, что показан снимок, обновление идёт само", async () => {
    useScenario("stale");
    renderApp("/");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByText(/Показаны данные от/)).toBeInTheDocument();
    expect(within(notices).queryByRole("button")).toBeNull();
    await waitFor(() => expect(screenState()).toBe("stale"));
  });

  it("repairing: «Вуз что-то поменял, Варя чинит», прежние данные на месте, кнопок нет", async () => {
    useScenario("connected", "repairing");
    renderApp("/");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByText("Вуз что-то поменял, Варя чинит")).toBeInTheDocument();
    expect(within(notices).queryByRole("button")).toBeNull();
    await waitFor(() => expect(screenState()).toBe("repairing"));
    expect((await screen.findAllByText("Математический анализ")).length).toBeGreaterThan(0);
  });

  it("error: подключение не обновляется — понятный текст и «Спросить в чате»", async () => {
    useScenario("connected", "error");
    const { router } = renderApp("/");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByText("Сайт вуза не отвечает уже вторые сутки.")).toBeInTheDocument();
    await userEvent.click(within(notices).getByRole("link", { name: "Спросить в чате" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search.draft).toContain("Личный кабинет");
  });

  it("offline: последний снимок и подпись «Нет сети. Показан снимок от …»", async () => {
    renderApp("/");
    await screen.findByRole("region", { name: "Пары" });
    await screen.findAllByRole("listitem");
    vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    window.dispatchEvent(new Event("offline"));
    expect((await screen.findAllByText(/Нет сети\. Показан снимок от/)).length).toBeGreaterThan(0);
    await waitFor(() => expect(screenState()).toBe("offline"));
    expect(screen.getAllByText("Математический анализ").length).toBeGreaterThan(0);
  });

  it("snapshot: ответ из офлайн-кэша service worker помечен как устаревший снимок, обновление идёт само", async () => {
    const snapshotAt = new Date(NOW - 3 * 3_600_000).toISOString();
    current.server.use(
      http.get("*/api/v1/today", () =>
        HttpResponse.json(
          { date: "2026-10-27", events: [], deadlines: [], attention: [] },
          { headers: { "x-botai-snapshot-at": snapshotAt } },
        ),
      ),
    );
    renderApp("/");
    await waitFor(() => expect(screenState()).toBe("stale"));
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
    // Пустой снимок не выдаётся за «Пар нет»: данные могут быть неполными.
    expect(await screen.findByText("Пар в этих данных нет")).toBeInTheDocument();
  });

  it("сбой связи при первой загрузке: скелетон остаётся, экран не падает и кнопки повтора нет", async () => {
    current.server.use(http.get("*/api/v1/today", () => HttpResponse.error()));
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Сегодня" });
    await waitFor(() => expect(screenState()).toBe("loading"));
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
  });
});
