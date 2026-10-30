import "@fontsource-variable/geist/wght.css";
// Курсив ответа агента — настоящий начертательный, а не синтетический наклон.
import "@fontsource-variable/geist/wght-italic.css";
import "@fontsource-variable/geist-mono/wght.css";
import "@botai/design/styles.css";
import { initPreferences } from "@botai/design";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { retryPolicy } from "./api/queries";
import { useActiveSpaceStore } from "./app/active-space";
import { onDataReset } from "./features/auth/data-reset";
import { useActiveJob } from "./features/jobs/active-job";
import { registerServiceWorker } from "./features/pwa/register";
import { routeTree } from "./routeTree.gen";

// Сбой сети и сайта вуза обрабатывается в фоне: ретраи с нарастающей паузой, без кнопки «Повторить».
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: retryPolicy,
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000),
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      // Первый запрос уходит и без сети: его отвечает service worker последним снимком (офлайн-чтение, FR-027).
      // Повторы при этом ждут сети, а не молотят впустую.
      networkMode: "offlineFirst",
    },
  },
});
const router = createRouter({ routeTree, context: { queryClient } });
onDataReset(() => {
  useActiveJob.getState().clear();
  useActiveSpaceStore.getState().select("all");
  queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== "me" });
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

initPreferences();

async function start() {
  // Мок-режим для разработки и e2e: бэкенд не нужен, ответы по сгенерированным типам OpenAPI.
  if (import.meta.env.DEV && import.meta.env.VITE_MOCKS === "1") {
    const { startMocks } = await import("./mocks/browser");
    await startMocks();
  }
  const root = document.getElementById("root");
  if (root) {
    createRoot(root).render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </StrictMode>,
    );
  }
}

void start();
void registerServiceWorker();
