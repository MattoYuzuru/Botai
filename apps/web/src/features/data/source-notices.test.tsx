import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { connectionFixture } from "@/test/data-fixtures";
import { SourceNotices } from "./source-notices";

/**
 * Плашка «Нужно снова войти» ведёт в тред задачи источника (`/chat?thread=…`). Переход обязан нести
 * `resetScroll: false` из `ChatLink`: иначе роутер после рендера сбрасывает страницу наверх и перебивает
 * прокрутку чата к последнему сообщению (`apps/web/e2e/chat.spec.ts` — регрессия навигации в чат).
 */
function spaceWithThread(): SpaceInfo {
  return {
    id: "s1",
    title: "НИУ ВШЭ",
    shortTitle: "ВШЭ",
    timeZone: "Europe/Moscow",
    hue: 262,
    universityId: "u1",
    connections: [
      connectionFixture({
        id: "c1",
        spaceId: "s1",
        status: "needs_reauth",
        errorCode: "login_failed",
        threadId: "01JTHREAD",
      }),
    ],
    health: { state: "reauth", updatedAt: Date.parse("2026-10-27T14:30:00.000Z") },
  };
}

function mount(space: SpaceInfo) {
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <SourceNotices spaces={[space]} nowMs={Date.parse("2026-10-27T15:00:00.000Z")} />
        <Outlet />
      </>
    ),
  });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <p>сегодня</p>,
  });
  const chatRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/chat",
    component: () => <p>чат</p>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, chatRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(<RouterProvider router={router} />);
  return router;
}

describe("плашки состояния источников", () => {
  it("действие открывает тред источника без сброса скролла (чат владеет им сам)", async () => {
    const router = mount(spaceWithThread());
    const navigate = vi.spyOn(router, "navigate");

    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    const action = notices.querySelector("a") as HTMLAnchorElement;
    expect(action).not.toBeNull();
    await userEvent.click(action);

    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search).toEqual({ thread: "01JTHREAD" });
    expect(navigate).toHaveBeenCalledWith(expect.objectContaining({ to: "/chat", resetScroll: false }));
  });

  it("источник без треда открывает новый чат с готовой просьбой, тоже без сброса скролла", async () => {
    const space = spaceWithThread();
    const connection = connectionFixture({ id: "c2", spaceId: "s1", status: "error", threadId: null });
    const router = mount({ ...space, connections: [connection] });
    const navigate = vi.spyOn(router, "navigate");

    const notices = await screen.findByRole("list", { name: "Состояние источников" });
    const action = notices.querySelector("a") as HTMLAnchorElement;
    await userEvent.click(action);

    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(navigate).toHaveBeenCalledWith(expect.objectContaining({ to: "/chat", resetScroll: false }));
  });
});
