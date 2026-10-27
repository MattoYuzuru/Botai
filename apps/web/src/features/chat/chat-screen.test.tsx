import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { jobManager } from "@/features/jobs/job-manager";
import { resetChatState } from "@/mocks/chat-handlers";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";
import { useChatJobs } from "./chat-jobs";
import { outbox } from "./runtime";

let current = createDataServer();

function useScenario(scenario: Scenario) {
  current.server.close();
  current = createDataServer(scenario);
  resetChatState(current.db);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  // Часы подменяем только у Date: таймеры настоящие, `waitFor` и поток ответа работают как обычно.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  outbox.clear();
  useChatJobs.getState().clearAll();
  jobManager.stopAll();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
  window.dispatchEvent(new Event("online"));
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const wait = { timeout: 8000 };

async function openNewChat(user: ReturnType<typeof userEvent.setup>) {
  renderApp("/chat?thread=new");
  await screen.findByRole("heading", { level: 1, name: "Чат" });
  await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" });
  return user;
}

async function ask(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(screen.getByRole("textbox", { name: "Сообщение агенту" }), text);
  await user.click(screen.getByRole("button", { name: "Отправить" }));
}

describe("чат: история, источники и шкала контекста", () => {
  it("открывает последний чат: вопрос, ответ с источниками, внутренняя ссылка ведёт в «Учёбу»", async () => {
    const { router } = renderApp("/chat");
    expect(
      await screen.findByText("Что сегодня по расписанию?", { selector: "p, span, div" }, wait),
    ).toBeTruthy();
    const link = await screen.findByRole("link", { name: /Открыть подключение в «Учёбе»/ }, wait);
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/study\?connection=/));
    // Внешний источник — в новой вкладке, внутренний — в той же.
    const external = screen.getByRole("link", { name: /lk\.fakeuni\.test/ });
    expect(external).toHaveAttribute("target", "_blank");
    expect(link).not.toHaveAttribute("target");
    await userEvent.setup().click(link);
    await waitFor(() => expect(router.state.location.pathname).toBe("/study"));
    expect(router.state.location.search).toMatchObject({ connection: expect.any(String) });
  });

  it("шкала контекста в шапке: число из ответа сервера, шит с сегментами и источниками", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    const button = await screen.findByRole("button", { name: /Контекст агента: занято/ }, wait);
    expect(button.getAttribute("aria-label")).toMatch(/из 1\sмлн токенов/);
    await user.click(button);
    const sheet = await screen.findByRole("dialog", { name: "Контекст агента" });
    expect(await within(sheet).findByRole("list", { name: "Из чего состоит контекст" })).toBeInTheDocument();
    expect(within(sheet).getByText("Правила и навыки")).toBeInTheDocument();
    expect(within(sheet).getByText("Переписка")).toBeInTheDocument();
    expect(within(sheet).getByRole("heading", { name: "Источники ответа" })).toBeInTheDocument();
    expect(within(sheet).getByText("Расписание — личный кабинет")).toBeInTheDocument();
    // Свободного места нет в легенде: это остаток трека, а не слой контекста.
    expect(within(sheet).queryByText("Свободно")).toBeNull();
  });

  it("история чатов в шите: выбор чата и «Новый чат»", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/chat");
    await screen.findByRole("button", { name: "История чатов" });
    await user.click(screen.getByRole("button", { name: "История чатов" }));
    const sheet = await screen.findByRole("dialog", { name: "Чаты" });
    expect(
      await within(sheet).findByRole("link", { name: /Что сегодня по расписанию\?/ }),
    ).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Новый чат" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: "new" }));
    expect(
      await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }),
    ).toBeInTheDocument();
  });
});

describe("чат: пустой чат и подсказки из настоящих данных", () => {
  it("до трёх подсказок, построенных из данных; нажатие отправляет вопрос", async () => {
    const user = await openNewChat(userEvent.setup());
    const list = await screen.findByRole("list", { name: "Подсказки" }, wait);
    const items = within(list).getAllByRole("button");
    expect(items.length).toBeGreaterThan(0);
    expect(items.length).toBeLessThanOrEqual(3);
    for (const item of items) expect(item.textContent).toMatch(/\?$/);
    await user.click(items[0] as HTMLElement);
    expect(await screen.findByText(items[0]?.textContent ?? "", { selector: "p" }, wait)).toBeInTheDocument();
  });

  it("вуз не подключён: приглашение «Подключить вуз» вместо выдуманных подсказок", async () => {
    useScenario("empty");
    renderApp("/chat?thread=new");
    expect(await screen.findByRole("link", { name: "Подключить вуз" }, wait)).toHaveAttribute(
      "href",
      "/connect",
    );
    expect(screen.queryByRole("list", { name: "Подсказки" })).toBeNull();
  });
});

describe("чат: ответ агента", () => {
  it("вопрос → шаги → текст с источниками; поле ввода на время ответа занято и потом свободно", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Что сдавать на неделе?");
    expect(await screen.findByText("Что сдавать на неделе?", { selector: "p" }, wait)).toBeInTheDocument();
    expect(await screen.findByText(/Ближайшие работы/, undefined, wait)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Открыть подключение в «Учёбе»/ })).toBeInTheDocument();
    // Ответ в истории, живая лента ушла, поле снова свободно.
    await waitFor(
      () => expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeEnabled(),
      wait,
    );
    expect(document.querySelector("[data-live-reply]")).toBeNull();
    // Название чата подставилось из вопроса.
    await waitFor(
      () => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Что сдавать на неделе?"),
      wait,
    );
  });

  it("ответ из документов: источник-документ — внутренняя ссылка «Открыть документ в «Учёбе»»", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Какие правила сдачи курсовых?");
    const link = await screen.findByRole("link", { name: /Открыть документ в «Учёбе»/ }, wait);
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/study\?document=/));
  });

  it("«Не знаю: …» помечено «Не уверен», источника нет и он не имитируется", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Расскажи анекдот");
    expect(await screen.findByText("Не уверен", undefined, wait)).toBeInTheDocument();
    expect(await screen.findByText(/^Не знаю:/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Источники" })).toBeNull();
  });

  it("сбой задания: человеческий текст без кода и без кнопки «Повторить»", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "сбой");
    expect(await screen.findByText(/лимит ответов исчерпан/, undefined, wait)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/quota_exceeded/);
    for (const button of screen.getAllByRole("button")) {
      expect(button.textContent?.trim()).not.toMatch(/^(Повторить|Обновить|Загрузить|Попробовать снова)$/);
    }
    // После сбоя спросить можно снова.
    await waitFor(
      () => expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeEnabled(),
      wait,
    );
  });

  it("запрос на запись: вопрос агента, ApprovalCard с методом, хостом и полями, одобрение по POST /actions", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Мне нужна справка");
    expect(await screen.findByText("Какая справка нужна?", undefined, wait)).toBeInTheDocument();
    // Пока агент ждёт человека, новое сообщение отправить нельзя, и это сказано словами.
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toHaveAttribute(
      "placeholder",
      "Ответьте на вопрос выше",
    );
    await user.click(screen.getByRole("radio", { name: "О стипендии" }));
    await user.click(screen.getByRole("button", { name: "Ответить" }));
    const card = await screen.findByText("Подать заявку на справку", undefined, wait);
    const approval = card.closest("[data-approval], article, section, div") as HTMLElement;
    expect(approval).toBeTruthy();
    expect(screen.getByText("lk.fakeuni.test")).toBeInTheDocument();
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(screen.getAllByText("О стипендии").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(await screen.findByText(/Заявка отправлена/, undefined, wait)).toBeInTheDocument();
  });

  it("отказ от записи: «Не делать» — ничего не отправлено", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Хочу записаться на консультацию");
    await screen.findByText("Какая справка нужна?", undefined, wait);
    await user.click(screen.getByRole("radio", { name: "Об обучении" }));
    await user.click(screen.getByRole("button", { name: "Ответить" }));
    await screen.findByText("Подать заявку на справку", undefined, wait);
    await user.click(screen.getByRole("button", { name: "Не делать" }));
    // Итог виден дважды: в карточке запроса («Ничего не отправлено») и в ответе агента.
    expect((await screen.findAllByText(/Ничего не отправлено/, undefined, wait)).length).toBeGreaterThan(0);
    expect(
      await screen.findByText(/Если передумаете, напишите ещё раз/, undefined, wait),
    ).toBeInTheDocument();
  });

  it("агент не может отправить форму сам: готовый текст с «Скопировать» и ссылка на сайт вуза", async () => {
    const user = await openNewChat(userEvent.setup());
    await ask(user, "Подай заявку вручную");
    expect(await screen.findByRole("button", { name: "Скопировать" }, wait)).toBeInTheDocument();
    const open = screen.getByRole("link", { name: /Открыть сайт вуза/ });
    expect(open).toHaveAttribute("href", "https://lk.fakeuni.test/requests/new");
    expect(open).toHaveAttribute("target", "_blank");
  });
});

describe("чат: нет сети", () => {
  it("сообщение ждёт с подписью «Отправится, когда появится сеть» и уходит само, без кнопки", async () => {
    const user = await openNewChat(userEvent.setup());
    const online = vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    await ask(user, "Что сдавать на неделе?");
    expect(await screen.findByText("Отправится, когда появится сеть")).toBeInTheDocument();
    expect(screen.getByText("Что сдавать на неделе?", { selector: "p" })).toBeInTheDocument();
    // Кнопки «Повторить» нет, повтор идёт сам.
    expect(screen.queryByRole("button", { name: /Повторить|Отправить ещё раз/ })).toBeNull();
    expect(outbox.snapshot).toHaveLength(1);

    online.mockReturnValue(true);
    window.dispatchEvent(new Event("online"));
    expect(await screen.findByText(/Ближайшие работы/, undefined, wait)).toBeInTheDocument();
    expect(screen.queryByText("Отправится, когда появится сеть")).toBeNull();
    expect(outbox.snapshot).toHaveLength(0);
    // Сообщение одно: повтор не продублировал его.
    expect(screen.getAllByText("Что сдавать на неделе?", { selector: "p" })).toHaveLength(1);
  });
});
