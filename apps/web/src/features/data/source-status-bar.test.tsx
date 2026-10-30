import { screen, waitFor, within } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { DataVariant } from "@/mocks/data-handlers";
import type { Scenario } from "@/mocks/db";
import { spaceFixture } from "@/test/data-fixtures";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";
import { spaceShortTitle } from "./use-space-directory";

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

/** Шапка экрана — первый `<header>` страницы (заголовки дней внутри содержимого — не шапка). */
const pageHeader = () => document.querySelector("header") as HTMLElement;

const SCREENS = [
  ["«Сегодня»", "/", "today"],
  ["«Расписание»", "/schedule", "schedule"],
  ["«Задания»", "/tasks", "tasks"],
] as const;

describe.each(SCREENS)("SourceStatusBar на экране %s", (_name, path, screenId) => {
  it("статусы вузов стоят в шапке чипами с короткими названиями; внизу экрана подписи свежести нет", async () => {
    renderApp(path);
    await screen.findByRole("heading", { level: 1 });
    const bar = await within(pageHeader()).findByRole("list", { name: "Статусы вузов" });
    const chips = within(bar).getAllByRole("button");
    expect(chips.map((chip) => chip.getAttribute("data-space-id"))).toEqual(
      current.db.spaces.map((space) => space.id),
    );
    expect(chips.map((chip) => chip.querySelector("span")?.textContent)).toEqual(["ВШЭ", "ЦУ"]);
    expect(chips.map((chip) => chip.getAttribute("data-source-state"))).toEqual(["ok", "ok"]);
    await waitFor(() =>
      expect(document.querySelector(`[data-screen=${screenId}]`)?.getAttribute("data-state")).toBe("ok"),
    );
    // Один список статусов на весь экран: подвала «Свежесть данных» нет.
    expect(screen.getAllByRole("list", { name: /Статусы вузов|Свежесть данных/ })).toHaveLength(1);
    expect(screen.queryByRole("list", { name: "Свежесть данных" })).toBeNull();
  });

  it("пояснение чипа — тот же текст, что и раньше в подписи: «Обновлено … назад»", async () => {
    renderApp(path);
    const bar = await screen.findByRole("list", { name: "Статусы вузов" });
    const hse = within(bar).getAllByRole("button")[0] as HTMLElement;
    expect(hse.getAttribute("aria-label")).toMatch(/^ВШЭ: Обновлено 5\sминут назад$/);
  });

  it("ошибка источника — полоса над содержимым, а не в шапке и не в подвале; чип показывает состояние", async () => {
    useScenario("reauth");
    renderApp(path);
    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    const header = pageHeader();
    expect(header.contains(notices)).toBe(false);
    expect(screen.getByRole("main").contains(notices)).toBe(true);
    expect(within(notices).getByRole("link", { name: "Войти снова" })).toBeInTheDocument();
    const bar = within(header).getByRole("list", { name: "Статусы вузов" });
    const cu = within(bar).getAllByRole("button")[1] as HTMLElement;
    expect(cu).toHaveAttribute("data-source-state", "reauth");
    expect(cu.getAttribute("aria-label")).toContain("Нужно снова войти в ЛК ЦУ");
  });

  it("вуз называется своим именем, а не адресом сайта: shortTitle пространства важнее справочника", async () => {
    const [hse] = current.db.spaces;
    if (hse) Object.assign(hse, { title: "lk.fakeuni.test", shortTitle: "НИУ ВШЭ", universityId: null });
    renderApp(path);
    const bar = await screen.findByRole("list", { name: "Статусы вузов" });
    const first = within(bar).getAllByRole("button")[0] as HTMLElement;
    expect(first.querySelector("span")?.textContent).toBe("НИУ ВШЭ");
    expect(first.getAttribute("aria-label")).not.toContain("lk.fakeuni.test");
  });
});

describe("spaceShortTitle: чем подписан вуз", () => {
  const universities = [
    {
      id: "u-hse",
      title: "Высшая школа экономики",
      shortTitle: "ВШЭ",
      aliases: [],
      accentHue: 262,
      origins: [],
    },
  ];

  it("shortTitle пространства, затем справочник вузов, затем название; адрес сайта именем не становится", () => {
    expect(
      spaceShortTitle(spaceFixture({ shortTitle: "НИУ ВШЭ", universityId: "u-hse" }), universities),
    ).toBe("НИУ ВШЭ");
    expect(spaceShortTitle(spaceFixture({ shortTitle: null, universityId: "u-hse" }), universities)).toBe(
      "ВШЭ",
    );
    expect(
      spaceShortTitle(spaceFixture({ shortTitle: null, title: "Синтетический университет" }), universities),
    ).toBe("Синтетический университет");
    expect(spaceShortTitle(spaceFixture({ shortTitle: null }), undefined)).toBe("Синтетический университет");
  });
});
