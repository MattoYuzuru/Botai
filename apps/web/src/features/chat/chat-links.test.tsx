import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { type ChatLinks, useChatLinks } from "./chat-links";

/** Каталог вузов не нужен: тред не зависит от подключений, а цитаты проверяются в citations.test. */
vi.mock("@/features/data/use-space-directory", () => ({
  useSpaceDirectory: () => ({
    spaces: [],
    byId: new Map(),
    loaded: true,
    connectionsLoaded: true,
    none: true,
    homeZone: "Europe/Moscow",
  }),
}));

/**
 * Внутренняя ссылка агента `botai://threads/<id>` ведёт в чат. Переход идёт через роутер без сброса
 * прокрутки: иначе тред открывается сверху, а чат обязан вставать к последнему сообщению (e2e
 * «переход в чат открывается у последнего сообщения»).
 */
function mount() {
  let links: ChatLinks | null = null;
  const rootRoute = createRootRoute({
    component: () => {
      links = useChatLinks();
      return <Outlet />;
    },
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
  const studyRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/study",
    component: () => <p>учёба</p>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, chatRoute, studyRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(<RouterProvider router={router} />);
  return {
    router,
    links: () => {
      if (!links) throw new Error("ChatLinks ещё не смонтированы");
      return links;
    },
  };
}

describe("внутренние ссылки агента", () => {
  it("тред открывается через navigate с resetScroll:false, а не сбросом страницы наверх", async () => {
    const { router, links } = mount();
    await waitFor(() => links());
    const navigate = vi.spyOn(router, "navigate");

    const resolved = links().resolveInternal("botai://threads/01JTHREAD");
    expect(resolved?.href).toBe("/chat?thread=01JTHREAD");

    act(() => resolved?.onOpen?.());

    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search).toEqual({ thread: "01JTHREAD" });
    expect(navigate).toHaveBeenCalledWith(expect.objectContaining({ to: "/chat", resetScroll: false }));
  });

  it("документы и подключения ведут на «Учёбу» обычным переходом: ей сброс наверх нужен", async () => {
    const { router, links } = mount();
    await waitFor(() => links());
    const navigate = vi.spyOn(router, "navigate");

    const resolved = links().resolveInternal("botai://documents/01JDOC");
    expect(resolved?.href).toBe("/study");

    act(() => resolved?.onOpen?.());

    await waitFor(() => expect(router.state.location.pathname).toBe("/study"));
    // Не чат: сюда идём обычным history.push, без опции чата (сброс наверх остаётся штатным).
    expect(navigate).not.toHaveBeenCalled();
  });
});
