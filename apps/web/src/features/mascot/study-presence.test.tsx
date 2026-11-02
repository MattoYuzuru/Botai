import { screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();
beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => {
  current.server.close();
  current = createDataServer();
  current.server.use(http.get("*/api/v1/spaces/:spaceId/connections", () => HttpResponse.json([])));
  current.server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

describe("Учебные экраны после подключения вуза", () => {
  it.each([
    ["/", "today"],
    ["/schedule", "schedule"],
    ["/tasks", "tasks"],
  ])(
    "%s: v4-вуз без старых подключений показывает данные без птицы и предложения подключиться",
    async (path, name) => {
      const { container } = renderApp(path);
      await waitFor(() =>
        expect(container.querySelector(`[data-screen=${name}]`)).toHaveAttribute("data-state", "ok"),
      );
      expect(container.querySelector("[data-mascot-stage]")).toBeNull();
      expect(screen.queryByRole("button", { name: "Подключить вуз" })).toBeNull();
      expect(screen.queryByText("Вузов пока нет")).toBeNull();
    },
  );
});
