import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jobManager } from "@/features/jobs/job-manager";
import { ask, chatHarness, openNewChat, wait } from "@/test/chat-harness";

const harness = chatHarness();

/** Тела `POST .../messages` и адреса `POST .../cancel`, которые видел стенд. */
function watch() {
  const messages: Record<string, unknown>[] = [];
  const cancels: string[] = [];
  harness.server.events.on("request:start", async ({ request }) => {
    const url = new URL(request.url);
    if (request.method !== "POST") return;
    if (/\/api\/v1\/chat\/threads\/[^/]+\/messages$/.test(url.pathname))
      messages.push((await request.clone().json()) as Record<string, unknown>);
    if (/\/api\/v1\/jobs\/[^/]+\/cancel$/.test(url.pathname)) cancels.push(url.pathname);
  });
  return { messages, cancels };
}

describe("остановленный ход агента продолжается кнопкой", () => {
  it("«Продолжить» шлёт сообщение с чекпоинтом хода, агент продолжает и кнопка уходит", async () => {
    const seen = watch();
    const { user } = await openNewChat();
    await ask(user, "Собери всё и остановись по лимиту");

    const button = await screen.findByRole("button", { name: "Продолжить" }, wait);
    expect(screen.getByText(/Остановился: потрачено \$2\.00 из \$2\.00/)).toBeInTheDocument();
    expect(screen.getByText(/Работа остановлена, прогресс сохранён/)).toBeInTheDocument();

    await user.click(button);
    await waitFor(() => expect(seen.messages).toHaveLength(2), wait);
    const body = seen.messages[1];
    expect(body?.text).toBe("Продолжить");
    expect(typeof body?.resumeRunId).toBe("string");
    expect(String(body?.resumeRunId)).toHaveLength(26);

    expect(
      await screen.findByText(/Продолжил с места остановки и закончил/, undefined, wait),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Продолжить" })).toBeNull(), wait);
    expect(seen.cancels).toEqual([]);
  });

  it("обычный вопрос без остановки не показывает «Продолжить»", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какая следующая пара?");
    expect(await screen.findByRole("group", { name: "Быстрые ответы" }, wait)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Продолжить" })).toBeNull();
  });
});

describe("закрытая вкладка и оборванный поток не отменяют ход", () => {
  it("размонтирование экрана чата и закрытие потоков не шлют отмену", async () => {
    const seen = watch();
    const { user, unmount } = await openNewChat();
    await ask(user, "Мне нужна справка");

    // Ход припаркован на вопросе: поток задания открыт, «Остановить агента» доступна.
    expect(await screen.findByRole("button", { name: "Остановить агента" }, wait)).toBeInTheDocument();
    await waitFor(() => expect(seen.messages).toHaveLength(1), wait);

    unmount(); // вкладка закрыта
    jobManager.stopAll(); // потоки закрыты вместе с ней
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(seen.cancels).toEqual([]);
  });

  it("явное «Остановить агента» отменяет ход (контроль: тест выше не вырожден)", async () => {
    const seen = watch();
    const { user } = await openNewChat();
    await ask(user, "Мне нужна справка");
    await user.click(await screen.findByRole("button", { name: "Остановить агента" }, wait));
    await waitFor(() => expect(seen.cancels).toHaveLength(1), wait);
  });
});
