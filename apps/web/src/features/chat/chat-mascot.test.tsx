import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { queryKeys } from "@/api/queries";
import { jobManager } from "@/features/jobs/job-manager";
import { useAcknowledged } from "@/features/mascot/acknowledged";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";
import { useChatJobs } from "./chat-jobs";
import { chatKeys } from "./queries";
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

  it.each(["before", "after"] as const)(
    "принятие сообщения %s навигации не возвращает Варю на сцену",
    async (acceptance) => {
      let releaseMessage = () => {};
      const messageGate = new Promise<void>((resolve) => {
        releaseMessage = resolve;
      });
      current.server.use(
        http.post("*/api/v1/chat/threads/:threadId/messages", async () => {
          await messageGate;
          // После паузы ответит обычный обработчик стенда.
        }),
      );
      const user = userEvent.setup();
      const { router, queryClient } = renderApp("/chat?thread=new");
      await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
      let releaseNavigation = () => {};
      const navigationGate = new Promise<void>((resolve) => {
        releaseNavigation = resolve;
      });
      const navigate = router.navigate.bind(router);
      vi.spyOn(router, "navigate").mockImplementation((options) => {
        const search = options.search;
        if (options.to === "/chat" && typeof search === "object" && search && "thread" in search) {
          return navigationGate.then(() => navigate(options));
        }
        return navigate(options);
      });
      const before = sprite();
      const modes: (string | null)[] = [];
      const observer = new MutationObserver(() => modes.push(dock()?.getAttribute("data-dock") ?? null));
      observer.observe(dock() as Element, { attributes: true, attributeFilter: ["data-dock"] });
      try {
        await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Что сдавать на неделе?");
        await user.click(screen.getByRole("button", { name: "Отправить" }));
        await waitFor(() => {
          expect(outbox.snapshot).toHaveLength(1);
          expect(typeof outbox.snapshot[0]?.threadId).toBe("string");
        }, wait);
        const threadId = outbox.snapshot[0]?.threadId;
        expect(router.state.location.search).toMatchObject({ thread: "new" });
        expect(dock()).toHaveAttribute("data-dock", "perch");
        expect(
          await screen.findByText("Что сдавать на неделе?", { selector: "p" }, wait),
        ).toBeInTheDocument();

        if (acceptance === "after") {
          releaseNavigation();
          await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: threadId }), wait);
          await waitFor(
            () => expect(queryClient.getQueryData(chatKeys.messages(threadId ?? ""))).toEqual([]),
            wait,
          );
        }

        releaseMessage();
        await waitFor(() => expect(outbox.snapshot).toHaveLength(0), wait);
        expect(router.state.location.search).toMatchObject({
          thread: acceptance === "before" ? "new" : threadId,
        });
        expect(dock()).toHaveAttribute("data-dock", "perch");
        expect(screen.queryByRole("heading", { level: 2, name: "Спросите про пары и сроки" })).toBeNull();

        releaseNavigation();
        await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: threadId }), wait);
        await screen.findByText(/Ближайшие работы/, undefined, wait);
        expect(modes).toEqual(["perch"]);
        expect(sprite()).toBe(before);
      } finally {
        observer.disconnect();
        releaseMessage();
        releaseNavigation();
      }
    },
  );

  it.each(["new", "existing"] as const)(
    "создание прежнего чата не уводит из другого открытого черновика (%s)",
    async (destination) => {
      let releaseThread = () => {};
      const threadGate = new Promise<void>((resolve) => {
        releaseThread = resolve;
      });
      current.server.use(
        http.post("*/api/v1/chat/threads", async () => {
          await threadGate;
        }),
      );
      const user = userEvent.setup();
      const { router } = renderApp("/chat?thread=new");
      await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
      try {
        await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Прежний вопрос");
        await user.click(screen.getByRole("button", { name: "Отправить" }));
        await waitFor(() => expect(outbox.snapshot).toHaveLength(1), wait);
        if (destination === "new") {
          await user.click(screen.getByRole("button", { name: "Новый чат" }));
          await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
          expect(dock()).toHaveAttribute("data-dock", "floor");
          expect(screen.queryByText("Прежний вопрос", { selector: "p" })).toBeNull();
        } else {
          await user.click(screen.getByRole("button", { name: "Чаты" }));
          await user.click(await screen.findByRole("link", { name: /Что сегодня по расписанию\?/ }, wait));
          await screen.findByText("Что сегодня по расписанию?", { selector: "p" }, wait);
        }
        const opened = router.state.location.search;
        await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), "Новый черновик");
        releaseThread();
        await waitFor(() => expect(outbox.snapshot).toHaveLength(0), wait);
        expect(router.state.location.search).toEqual(opened);
        expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toHaveValue("Новый черновик");
        expect(screen.queryByText("Прежний вопрос", { selector: "p" })).toBeNull();
        if (destination === "new") expect(dock()).toHaveAttribute("data-dock", "floor");
      } finally {
        releaseThread();
      }
    },
  );

  it.each(["self", "other"] as const)(
    "сохранённый черновик ждёт владельца /me и не показывает чужую очередь (%s)",
    async (owner) => {
      const online = vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
      const ownerId = owner === "self" ? current.db.users[0]?.id : "different-owner";
      if (!ownerId) throw new Error("Нужен пользователь стенда");
      outbox.send("Что сдавать на неделе?", { ownerId, threadId: null, spaceId: null });
      let meAvailable = false;
      current.server.use(http.get("*/api/v1/me", () => (meAvailable ? undefined : HttpResponse.error())));
      const { router, queryClient } = renderApp("/chat?thread=new");
      await screen.findByRole("heading", { level: 1, name: "Новый чат" }, wait);
      expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
      expect(screen.queryByText("Что сдавать на неделе?", { selector: "p" })).toBeNull();

      meAvailable = true;
      await queryClient.invalidateQueries({ queryKey: queryKeys.me });
      if (owner === "self") {
        await screen.findByText("Что сдавать на неделе?", { selector: "p" }, wait);
        expect(dock()).toHaveAttribute("data-dock", "perch");
      } else {
        await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
        expect(dock()).toHaveAttribute("data-dock", "floor");
        expect(screen.queryByText("Что сдавать на неделе?", { selector: "p" })).toBeNull();
      }
      online.mockReturnValue(true);
      window.dispatchEvent(new Event("online"));
      await waitFor(() => expect(outbox.snapshot).toHaveLength(0), wait);
      if (owner === "self") {
        await waitFor(() => expect(router.state.location.search.thread).not.toBe("new"), wait);
        await screen.findByText(/Ближайшие работы/, undefined, wait);
      } else {
        expect(router.state.location.search).toMatchObject({ thread: "new" });
        expect(screen.queryByText("Что сдавать на неделе?", { selector: "p" })).toBeNull();
      }
    },
  );

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

  it("лента получает нижний отступ под выступ Вари: последнее сообщение не уходит под неё", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    await screen.findByText("Что сегодня по расписанию?", { selector: "p" }, wait);
    expect(document.querySelector("[data-chat-content]")?.className).toContain("pb-10");
    await user.click(screen.getByRole("button", { name: "Новый чат" }));
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    // На пустом чате место сцены входит в центрируемый блок, а не стоит пустой полосой над полем.
    expect(document.querySelector("[data-empty-chat] [data-stage-floor]")).not.toBeNull();
  });

  it("спокойный режим: Варя статична, пузыри остаются (это текст)", async () => {
    window.localStorage.setItem("botai.motion", "calm");
    renderApp("/chat?thread=new");
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    await waitFor(() => expect(sprite()?.classList.contains("still")).toBe(true), wait);
    await waitFor(() => expect(bubble()).not.toBeNull(), wait);
  });
});
