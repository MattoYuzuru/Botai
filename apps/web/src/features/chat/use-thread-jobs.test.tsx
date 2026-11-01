import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ask, chatHarness, openNewChat, wait } from "@/test/chat-harness";

const harness = chatHarness();

/**
 * Список вузов после хода агента. Вуз создаёт агент в чате (`space_create`), поэтому клиент обязан
 * перезапросить `GET /api/v1/spaces`, иначе новый вуз не появится в шапке, переключателе и «Учёбе»
 * до перезагрузки. Считаем сами запросы стенда: так видно и нужное обновление, и лишние перезапросы.
 */
describe("завершение хода чата обновляет вузы", () => {
  it("один повторный запрос списка вузов после ответа агента, без перезагрузки и без перезапросов на шаг", async () => {
    let spacesRequests = 0;
    harness.server.events.on("request:start", ({ request }) => {
      if (request.method === "GET" && new URL(request.url).pathname === "/api/v1/spaces") spacesRequests += 1;
    });

    const { user } = await openNewChat();
    // Экран загрузил вузы один раз.
    await waitFor(() => expect(spacesRequests).toBe(1), wait);

    await ask(user, "Какая следующая пара?");
    // Ответ пришёл целиком: ход агента завершён.
    expect(await screen.findByRole("group", { name: "Быстрые ответы" }, wait)).toBeInTheDocument();

    // Агент мог создать вуз — список обновляется сам, без действий человека.
    await waitFor(() => expect(spacesRequests).toBe(2), wait);

    // Лишних перезапросов нет: обновление одно на завершение хода, а не на каждый его шаг.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 150));
    });
    expect(spacesRequests).toBe(2);
  });
});
