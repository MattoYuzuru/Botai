import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { ask, chatHarness, composer, openNewChat, wait } from "@/test/chat-harness";
import { renderApp } from "@/test/render-app";

const harness = chatHarness();

const pendingQuestion = () => document.querySelector<HTMLElement>("[data-pending-questions]");

describe("один тред: подключение, вход, вопросы агента и ответы в одной ленте", () => {
  it("«Подключи ЦУ» запускает задачу в этом же треде: карточка, вопрос входа у поля ввода, итог — без отдельного экрана", async () => {
    const { user, router } = await openNewChat();
    await ask(user, "Подключи ЦУ https://new.fakeuni.test");

    // Ход агента ответил и запустил задачу: карточка задачи — в ленте этого треда.
    expect(await screen.findByText(/Запустил подключение/, undefined, wait)).toBeInTheDocument();
    const card = await screen.findByText("Подключение ЦУ", { selector: "p, span" }, wait);
    expect(card.closest("[data-task-card]")).not.toBeNull();
    expect(within(card.closest("[data-task-card]") as HTMLElement).getByText("Ждёт вас")).toBeInTheDocument();
    // Адрес остался чатом: ни /connect, ни второго таймлайна.
    expect(router.state.location.pathname).toBe("/chat");
    expect(document.querySelectorAll("[data-timeline]")).toHaveLength(1);

    // Актуальный вопрос задачи стоит у поля ввода; писать в поле нельзя, и это сказано словами.
    const login = await screen.findByText("Вход в lk.fakeuni.test. Логин", undefined, wait);
    expect(pendingQuestion()?.contains(login)).toBe(true);
    expect(composer()).toBeDisabled();
    expect(composer()).toHaveAttribute("placeholder", "Ответьте на вопрос выше");

    await user.type(within(pendingQuestion() as HTMLElement).getByLabelText("Ответ"), "student");
    await user.click(within(pendingQuestion() as HTMLElement).getByRole("button", { name: "Ответить" }));

    // Пароль — секрет: поле скрыто, значение уходит через jobs/answers, в ленте остаётся «Введено».
    const password = await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, wait);
    expect(pendingQuestion()?.contains(password)).toBe(true);
    await user.type(
      within(pendingQuestion() as HTMLElement).getByLabelText("Ответ"),
      "correct-horse-battery",
    );
    await user.click(within(pendingQuestion() as HTMLElement).getByRole("button", { name: "Ответить" }));

    const subjects = await screen.findByText("Это ваши предметы в этом семестре?", undefined, wait);
    expect(pendingQuestion()?.contains(subjects)).toBe(true);
    await user.click(screen.getByRole("checkbox", { name: /Алгоритмы и структуры данных/ }));
    await user.click(within(pendingQuestion() as HTMLElement).getByRole("button", { name: "Ответить" }));

    // Итог подключения — в той же ленте; вопросов больше нет, поле свободно.
    expect(await screen.findByText("Что подключено", undefined, wait)).toBeInTheDocument();
    await waitFor(() => expect(pendingQuestion()).toBeNull(), wait);
    await waitFor(() => expect(composer()).toBeEnabled(), wait);
    // Ответы человека остались в истории; пароль в тексте страницы нет.
    expect(screen.getAllByText("Введено").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("correct-horse-battery");
    expect(router.state.location.pathname).toBe("/chat");
  });

  it("пока задача ждёт вход, можно спросить агента: ответ приходит, вопрос задачи остаётся", async () => {
    const { user } = await openNewChat();
    await ask(user, "Подключи ЦУ https://new.fakeuni.test");
    await screen.findByText("Вход в lk.fakeuni.test. Логин", undefined, wait);
    // Пока задача ждёт защищённого ответа, поле занято — вопрос задачи важнее.
    expect(composer()).toBeDisabled();
  });

  it("повторная просьба подключить уже подключённое: «уже подключено» с быстрыми ответами, задача не запускается", async () => {
    const { user } = await openNewChat();
    await ask(user, "Подключи ЦУ https://my.fakeuni.test");
    expect(await screen.findByText(/уже подключён/, undefined, wait)).toBeInTheDocument();
    const replies = await screen.findByRole("group", { name: "Быстрые ответы" }, wait);
    expect(
      within(replies)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Перепроверить", "Войти заново"]);
    expect(document.querySelector("[data-task-card]")).toBeNull();
  });

  it("задача из другого треда: карточка ведёт в тот тред ссылкой", async () => {
    const AT = "2026-10-27T11:00:00.000Z";
    harness.server.use(
      http.get("*/api/v1/chat/threads/:threadId/messages", () =>
        HttpResponse.json([
          {
            id: "01JMKCDB000000000000000001",
            role: "agent",
            jobId: null,
            createdAt: AT,
            blocks: [
              {
                type: "task",
                id: "01JMKCDB000000000000000002",
                at: AT,
                jobId: "01JMKCDB000000000000000003",
                kind: "connect",
                title: "Подключение ВШЭ",
                state: "running",
                threadId: "OTHER",
              },
            ],
          },
        ]),
      ),
    );
    const user = userEvent.setup();
    const { router } = renderApp("/chat");
    const link = await screen.findByRole("link", { name: "Открыть чат" }, wait);
    expect(screen.getByText("Идёт в другом чате")).toBeInTheDocument();
    await user.click(link);
    await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: "OTHER" }), wait);
  });
});
