import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { jobManager } from "@/features/jobs/job-manager";
import { useAcknowledged } from "@/features/mascot/acknowledged";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";
import { useChatJobs } from "./chat-jobs";
import { outbox } from "./runtime";

let current = createDataServer();

function setScenario(scenario: Scenario) {
  current.server.close();
  current = createDataServer(scenario);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => setScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  outbox.clear();
  useChatJobs.getState().clearAll();
  useAcknowledged.setState({ keys: [] });
  jobManager.stopAll();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  window.dispatchEvent(new Event("online"));
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const wait = { timeout: 8000 };
const dock = () => document.querySelector("[data-dock]");
const sprite = () => document.querySelector("[data-dock] svg.varya");
const bubble = () => document.querySelector("[data-dock-bubble] [data-mascot-bubble]");

describe("Варя в чате: сцена, насест, реплики", () => {
  it("пустой чат — на сцене, одна Варя; после первого сообщения она на насесте, и это тот же экземпляр", async () => {
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    expect(dock()).toHaveAttribute("data-dock", "floor");
    expect(document.querySelectorAll("svg.varya")).toHaveLength(1);
    const before = sprite();
    expect(before).not.toBeNull();
    // Место сцены в раскладке зарезервировано: подсказки её не перекрывают.
    expect(document.querySelector("[data-stage-floor]")).not.toBeNull();

    await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Что сдавать на неделе?");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(dock()).toHaveAttribute("data-dock", "perch"), wait);
    expect(sprite()).toBe(before);
    expect(document.querySelectorAll("svg.varya")).toHaveLength(1);
  });

  it("на насесте она думает, пока агент отвечает, и стоит спокойно, когда ответ пришёл", async () => {
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await screen.findByRole("textbox", { name: "Сообщение агенту" }, wait);
    await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Что сдавать на неделе?");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await screen.findByText(/Ближайшие работы/, undefined, wait);
    await waitFor(() => expect(sprite()?.getAttribute("data-sit")).toBe("perch"), wait);
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeEnabled();
  });

  it("приветствие из данных — один раз за открытие, пузырь aria-hidden; нажатие даёт следующую реплику", async () => {
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    await waitFor(() => expect(bubble()).not.toBeNull(), wait);
    const first = bubble()?.textContent ?? "";
    expect(first.length).toBeGreaterThan(0);
    expect([...first].length).toBeLessThanOrEqual(48);
    expect(bubble()).toHaveAttribute("aria-hidden", "true");
    expect(first).not.toMatch(/[!]/);
    // Нажатие по пузырю закрывает его.
    await user.click(bubble() as Element);
    await waitFor(() => expect(bubble()).toBeNull());
    // Нажатие по Варе — реплика её ситуации.
    await user.click(sprite() as Element);
    await waitFor(() => expect(bubble()).not.toBeNull());
  });

  it("пока человек печатает, она молчит, а пузырь не мешает полю ввода", async () => {
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    await waitFor(() => expect(bubble()).not.toBeNull(), wait);
    await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Ка");
    await waitFor(() => expect(bubble()).toBeNull());
    // И не появляется снова, пока в поле есть текст.
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(bubble()).toBeNull();
  });

  it("три быстрых нажатия — взмах крылом, один раз за сессию", async () => {
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    const target = () => sprite()?.parentElement?.parentElement as Element;
    await user.tripleClick(target());
    await waitFor(() => expect(sprite()?.getAttribute("class")).toContain("a-wave"), wait);
    expect(window.sessionStorage.getItem("botai.mascot.waved")).toBe("1");
  });

  it("нужен повторный вход — у Вари ключ, и это же сказано текстом вне картинки", async () => {
    setScenario("reauth");
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    await waitFor(() => expect(sprite()?.getAttribute("data-sit")).toBe("reauth"), wait);
    expect(sprite()?.getAttribute("class")).toContain("p-key");
    // Состояние источника Варя не заменяет: в чате она лишь отражает его.
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeEnabled();
  });

  it("список сообщений получает нижний отступ под выступ Вари, пустой чат — нет", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    await screen.findByText("Что сегодня по расписанию?", { selector: "p" }, wait);
    expect(document.querySelector("[data-radix-scroll-area-viewport] > div > div")?.className).toContain(
      "pb-10",
    );
    await user.click(screen.getByRole("button", { name: "Новый чат" }));
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    expect(document.querySelector("[data-radix-scroll-area-viewport] > div > div")?.className).toContain(
      "pb-4",
    );
  });

  it("спокойный режим: Варя статична, пузыри остаются (это текст)", async () => {
    window.localStorage.setItem("botai.motion", "calm");
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    await waitFor(() => expect(sprite()?.classList.contains("still")).toBe(true), wait);
    await waitFor(() => expect(bubble()).not.toBeNull(), wait);
  });
});
