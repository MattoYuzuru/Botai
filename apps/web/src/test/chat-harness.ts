import { toast } from "@botai/design";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, vi } from "vitest";
import { useChatJobs } from "@/features/chat/chat-jobs";
import { outbox } from "@/features/chat/runtime";
import { jobManager } from "@/features/jobs/job-manager";
import { resetChatState } from "@/mocks/chat-handlers";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "./data-server";
import { renderApp } from "./render-app";
import { installUploadShim } from "./upload-shim";

export const wait = { timeout: 8000 };

/**
 * Общая оснастка тестов чата: стенд MSW с фиксированным «сейчас» (часы подменены только у `Date`, таймеры настоящие,
 * поэтому `waitFor` и потоки заданий работают как обычно), сброс очереди, потоков и хранилищ после каждого теста.
 */
export function chatHarness() {
  let current = createDataServer();

  const setScenario = (scenario: Scenario) => {
    current.server.close();
    current = createDataServer(scenario);
    resetChatState(current.db);
    current.server.listen({ onUnhandledRequest: "error" });
  };

  beforeAll(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });
  beforeEach(() => {
    setScenario("connected");
    installUploadShim();
  });
  afterEach(() => {
    current.server.resetHandlers();
    outbox.clear();
    useChatJobs.getState().clearAll();
    jobManager.stopAll();
    window.localStorage.clear();
    window.sessionStorage.clear();
    toast.dismiss();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.dispatchEvent(new Event("online"));
  });
  afterAll(() => {
    current.server.close();
    vi.useRealTimers();
  });

  return {
    get server() {
      return current.server;
    },
    get db() {
      return current.db;
    },
    setScenario,
  };
}

export const composer = () => screen.getByRole("textbox", { name: "Сообщение агенту" });

export async function openNewChat(path = "/chat?thread=new") {
  const app = renderApp(path);
  await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
  return { ...app, user: userEvent.setup() };
}

export async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(composer(), text);
  await user.click(screen.getByRole("button", { name: "Отправить" }));
}
