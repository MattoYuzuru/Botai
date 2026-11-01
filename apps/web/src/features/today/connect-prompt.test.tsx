import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { hrefOf } from "@/test/data-fixtures";
import { ConnectPrompt } from "./connect-prompt";

/**
 * Изолированный стенд пустого состояния: нужен роутер, потому что действие — ссылка в чат, а не форма.
 * Другие маршруты — заглушки: проверяем только то, куда ведут подписи.
 */
function mount() {
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: () => <ConnectPrompt />,
  });
  const chatRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/chat",
    component: () => <p>чат</p>,
  });
  const welcomeRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/welcome",
    component: () => <p>онбординг</p>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute, chatRoute, welcomeRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("ConnectPrompt: пустое состояние до первого вуза", () => {
  it("называет вуз, ссылку и голос как путь подключения; primary ведёт в чат с заготовкой", async () => {
    mount();
    expect(await screen.findByRole("heading", { level: 2, name: "Вузов пока нет" })).toBeInTheDocument();

    const description = screen.getByText(/Подключение идёт в чате/);
    expect(description).toHaveTextContent(/назовите вуз и пришлите ссылку на личный кабинет/);
    expect(description).toHaveTextContent(/агент создаст вуз и подключит источники/);
    // Честно про голос: распознавание уже встроено в поле ввода.
    expect(description).toHaveTextContent(/голосом/);
    expect(description).toHaveTextContent(/распознавание речи/);

    const link = screen.getByRole("link", { name: "Подключить вуз" });
    const href = hrefOf(link);
    expect(href.pathname).toBe("/chat");
    expect(href.draft).toBeTruthy();
    expect(href.draft).toContain("вуз");
    // Это новый чат с заготовкой, а не открытый тред.
    expect(href.thread).toBeUndefined();

    expect(screen.getByRole("link", { name: "Как это работает" })).toHaveAttribute("href", "/welcome");
  });

  it("не обещает того, чего нет: ни кнопки, ни формы «Создать вуз»", async () => {
    mount();
    await screen.findByRole("heading", { level: 2, name: "Вузов пока нет" });
    expect(screen.queryByRole("button", { name: /Создать вуз/ })).toBeNull();
    // Оба действия — ссылки: в чат и в онбординг. Кнопок-действий на экране нет.
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.querySelector("form")).toBeNull();
  });
});
