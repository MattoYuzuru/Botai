import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { routeTree } from "@/routeTree.gen";

/** Рендерит настоящее дерево маршрутов в памяти: гварды, редиректы и каркас — как в приложении. */
export function renderApp(path = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const history = createMemoryHistory({ initialEntries: [path] });
  const router = createRouter({ routeTree, context: { queryClient }, history });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, queryClient, ...utils };
}
