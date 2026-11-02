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
import { describe, expect, it } from "vitest";
import { hrefOf } from "@/test/data-fixtures";
import { CONNECT_DRAFT } from "./drafts";
import { EmptyChat } from "./empty-chat";

/**
 * Пустой чат без вузов: проверяем описание и приглашение подключить — оно ведёт в этот же чат с заготовкой
 * в поле ввода. Роутер нужен, потому что действие — ссылка, а не кнопка-обработчик.
 */
function mount({ noSpaces, onPick = () => {} }: { noSpaces: boolean; onPick?: (text: string) => void }) {
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const chatRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/chat",
    component: () => <EmptyChat suggestions={[]} noSpaces={noSpaces} onPick={onPick} />,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([chatRoute]),
    history: createMemoryHistory({ initialEntries: ["/chat"] }),
  });
  render(<RouterProvider router={router} />);
  return router;
}

describe("EmptyChat: вуз не подключён", () => {
  it("говорит, что вуза нет и его создаст агент в этом чате; можно написать или сказать голосом", async () => {
    mount({ noSpaces: true });
    const description = await screen.findByText(/Вуза пока нет/);
    expect(description).toHaveTextContent(/создадим его в этом чате/);
    expect(description).toHaveTextContent(/название вуза и пришлите ссылку на личный кабинет/);
    expect(description).toHaveTextContent(/скажите голосом/);
    expect(description).toHaveTextContent(/агент создаст вуз и подключит источники/);
  });

  it("«Подключить вуз» кладёт заготовку в поле этого же чата, а не открывает форму", async () => {
    const user = userEvent.setup();
    const router = mount({ noSpaces: true });
    const link = await screen.findByRole("link", { name: "Подключить вуз" });
    const href = hrefOf(link);
    expect(href.pathname).toBe("/chat");
    expect(href.draft).toBe(CONNECT_DRAFT);

    await user.click(link);
    await waitFor(() => expect(router.state.location.search).toMatchObject({ draft: CONNECT_DRAFT }));
    // Ни отдельного экрана подключения, ни формы: маршрут остаётся чатом.
    expect(router.state.location.pathname).toBe("/chat");
    expect(screen.queryByRole("button", { name: /Создать вуз/ })).toBeNull();
    expect(document.querySelector("form")).toBeNull();
  });

  it("когда вуз есть, новый вуз всё равно можно подключить заготовкой", async () => {
    mount({ noSpaces: false });
    expect(await screen.findByText(/Агент отвечает по вашим данным/)).toBeInTheDocument();
    expect(hrefOf(screen.getByRole("link", { name: "Подключить новый вуз" })).draft).toBe(CONNECT_DRAFT);
  });
});
