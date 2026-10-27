import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
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

const screenState = () => document.querySelector("[data-screen=tasks]")?.getAttribute("data-state");
const group = (name: string) => screen.findByRole("region", { name: new RegExp(`^${name}`) });

describe("«Задания»: группы и срочность", () => {
  it("группы идут по порядку: просрочено, сегодня, на неделе, позже, без срока, сдано", async () => {
    renderApp("/tasks");
    await waitFor(() => expect(screenState()).toBe("ok"));
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Просрочено2", "Сегодня1", "На неделе2", "Позже2", "Без срока1", "Сдано2"]);
  });

  it("срочность по порогам: «Просрочено» без укоров, «Осталось 5 ч», «Скоро · 2 д», спокойные — без метки", async () => {
    renderApp("/tasks");
    const overdue = await group("Просрочено");
    expect(within(overdue).getByText("Курсовая: выбор темы")).toBeInTheDocument();
    expect(within(overdue).getByText("Реферат по истории науки")).toBeInTheDocument();
    expect(within(overdue).getAllByText("Просрочено", { selector: "span" })).toHaveLength(2);
    const today = await group("Сегодня");
    expect(within(today).getByText("Осталось 5 ч")).toBeInTheDocument();
    const week = await group("На неделе");
    expect(within(week).getByText(/Скоро · 2 д/)).toBeInTheDocument();
    const later = await group("Позже");
    expect(within(later).queryByText(/Скоро|Осталось|Просрочено/)).toBeNull();
  });

  it("срок, назначенный преподавателем, важнее базового: эссе (база +4 д, назначено +5 д) — на неделе", async () => {
    renderApp("/tasks");
    const week = await group("На неделе");
    expect(within(week).getByText("Эссе по продуктовой стратегии")).toBeInTheDocument();
  });

  it("задание без срока — «Срок не указан», без отсчёта и без метки срочности; группа объясняет почему", async () => {
    renderApp("/tasks");
    const none = await group("Без срока");
    expect(within(none).getByText("Прочитать главы 3–4")).toBeInTheDocument();
    expect(within(none).getByText("Срок не указан")).toBeInTheDocument();
    expect(within(none).getByText(/BotAI его не придумывает/)).toBeInTheDocument();
    expect(within(none).queryByText(/Осталось|Скоро|Просрочено/)).toBeNull();
  });

  it("«Сдано» свёрнуто, пока не раскрыть; сданное зачёркнуто и подписано «Сдано»", async () => {
    renderApp("/tasks");
    const done = await group("Сдано");
    expect(within(done).queryByText("Домашнее задание №3: пределы")).toBeNull();
    const toggle = within(done).getByRole("button", { name: /Показать 2\sзадания/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(within(done).getByText("Домашнее задание №3: пределы")).toBeInTheDocument();
    expect(within(done).getByRole("button", { name: /Скрыть/ })).toHaveAttribute("aria-expanded", "true");
    expect(within(done).getAllByText("Сдано").length).toBeGreaterThan(0);
  });

  it("у каждого задания метка вуза на сводном экране; при выборе ЦУ остаются только его задания", async () => {
    renderApp("/tasks");
    const today = await group("Сегодня");
    expect(within(today).getByText("ВШЭ")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: /Все вузы/ }));
    const sheet = await screen.findByRole("dialog", { name: "Вуз" });
    await userEvent.click(within(sheet).getByRole("button", { name: /Центральный университет/ }));
    await waitFor(() => expect(screen.queryByText("Курсовая: выбор темы")).toBeNull());
    expect(await screen.findByText("Эссе по продуктовой стратегии")).toBeInTheDocument();
    expect(screen.getByText("Прочитать главы 3–4")).toBeInTheDocument();
    expect(screen.queryByText("Домашнее задание №4: ряды")).toBeNull();
  });

  it("карточка: срок, статус, предмет, источник со временем получения и ссылка на условие", async () => {
    renderApp("/tasks");
    const week = await group("На неделе");
    await userEvent.click(within(week).getByRole("button", { name: /Лабораторная: алгоритмы сортировки/ }));
    const sheet = await screen.findByRole("dialog", { name: "Лабораторная: алгоритмы сортировки" });
    expect(within(sheet).getByText("Нужно сдать")).toBeInTheDocument();
    expect(within(sheet).getAllByText("Алгоритмы и структуры данных").length).toBeGreaterThan(0);
    expect(within(sheet).getByText("Срок по плану курса")).toBeInTheDocument();
    expect(within(sheet).getByText(/Получено/)).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Открыть задание/ })).toHaveAttribute(
      "href",
      expect.stringMatching(/^https:\/\/lk\.fakeuni\.test\//),
    );
  });

  it("карточка задания без срока говорит «Срок не указан», а не придумывает срок", async () => {
    renderApp("/tasks");
    const none = await group("Без срока");
    await userEvent.click(within(none).getByRole("button", { name: /Прочитать главы 3–4/ }));
    const sheet = await screen.findByRole("dialog", { name: "Прочитать главы 3–4" });
    expect(within(sheet).getByText("Срок не указан")).toBeInTheDocument();
    expect(within(sheet).queryByText(/Срок по плану курса/)).toBeNull();
  });
});

describe("«Задания»: состояния данных", () => {
  it("нет вузов — приглашение подключить", async () => {
    useScenario("empty");
    renderApp("/tasks");
    expect(await screen.findByText("Вузов пока нет")).toBeInTheDocument();
  });

  it("empty: «Заданий пока нет» без «всё сдано»", async () => {
    useScenario("connected", "quiet");
    renderApp("/tasks");
    expect(await screen.findByText("Заданий пока нет")).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("empty"));
    expect(screen.queryByText(/всё сдано/i)).toBeNull();
  });

  it("все задания сданы: об этом сказано словами, а сданные — в «Сдано»", async () => {
    current.server.use(
      http.get("*/api/v1/deadlines", () =>
        HttpResponse.json([
          {
            id: "x",
            title: "Тест",
            dueAt: new Date(NOW - 86_400_000).toISOString(),
            effectiveDueAt: null,
            state: "graded",
            courseTitle: null,
            url: null,
            provenance: {
              connectionId: "c",
              spaceId: "s",
              sourceUrl: "https://lk.fakeuni.test/x",
              fetchedAt: new Date(NOW - 60_000).toISOString(),
              stale: false,
            },
          },
        ]),
      ),
    );
    renderApp("/tasks");
    expect(await screen.findByText(/Заданий, которые нужно сдать, в источниках нет/)).toBeInTheDocument();
    expect(await group("Сдано")).toBeInTheDocument();
  });

  it("reauth: полоса «Войти снова», задания остаются", async () => {
    useScenario("reauth");
    renderApp("/tasks");
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    expect(within(notices).getByRole("button", { name: "Войти снова" })).toBeInTheDocument();
    await waitFor(() => expect(screenState()).toBe("reauth"));
    expect(await screen.findByText("Эссе по продуктовой стратегии")).toBeInTheDocument();
  });

  it("offline: последний снимок, срок и срочность считаются от часов, подпись про снимок", async () => {
    renderApp("/tasks");
    await waitFor(() => expect(screenState()).toBe("ok"));
    vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    window.dispatchEvent(new Event("offline"));
    expect((await screen.findAllByText(/Нет сети\. Показан снимок от/)).length).toBeGreaterThan(0);
    await waitFor(() => expect(screenState()).toBe("offline"));
    expect(screen.getByText("Осталось 5 ч")).toBeInTheDocument();
  });

  it("loading: скелетон, пока ответа нет, и никаких кнопок повтора", async () => {
    current.server.use(http.get("*/api/v1/deadlines", () => new Promise(() => {})));
    renderApp("/tasks");
    await waitFor(() => expect(screenState()).toBe("loading"));
    expect(document.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
  });
});
