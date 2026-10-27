import { Button, EmptyState, Toaster } from "@botai/design";
import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, Link, Outlet } from "@tanstack/react-router";
import { PwaPrompts } from "@/features/pwa/pwa-prompts";

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: Root,
  notFoundComponent: () => (
    <main className="grid min-h-dvh place-items-center px-safe-4">
      <EmptyState
        icon="search"
        title="Такой страницы нет"
        description="Возможно, ссылка устарела."
        action={
          <Button variant="primary" asChild>
            <Link to="/">На главную</Link>
          </Button>
        }
      />
    </main>
  ),
  // Сбой отрисовки экрана: человеческий текст и выход на главную. Кнопки «Повторить» нет.
  errorComponent: () => (
    <main className="grid min-h-dvh place-items-center px-safe-4">
      <EmptyState
        icon="error"
        title="Экран не открылся"
        description="Что-то пошло не так. Остальные разделы работают."
        action={
          <Button variant="primary" asChild>
            <Link to="/">На главную</Link>
          </Button>
        }
      />
    </main>
  ),
});

function Root() {
  return (
    <>
      <Outlet />
      <Toaster />
      <PwaPrompts />
    </>
  );
}
