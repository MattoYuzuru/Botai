import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import type { ThreadOut } from "@/api/v3";
import { ask, chatHarness, composer, openNewChat, wait } from "@/test/chat-harness";
import { renderApp } from "@/test/render-app";
import { outbox } from "./runtime";

const harness = chatHarness();

const ULID = (n: number) => `01JMKCDB00000000000000${String(n).padStart(4, "0")}`;
const AT = "2026-10-27T11:00:00.000Z";
async function seededThreadId(): Promise<string> {
  const response = await fetch(new URL("/api/v1/chat/threads", window.location.origin));
  const rows = (await response.json()) as ThreadOut[];
  const id = rows[0]?.id;
  if (!id) throw new Error("В стенде нет треда");
  return id;
}

describe("чат на весь экран: шапка, история, источники ответа", () => {
  it("название треда — заголовок, «Чат» — подпись; справа «Источники», «Чаты», «Новый чат»", async () => {
    renderApp("/chat");
    const title = await screen.findByRole("heading", { level: 1, name: "Что сегодня по расписанию?" }, wait);
    const bar = title.closest("[data-chat-topbar]") as HTMLElement;
    expect(within(bar).getByText("Чат")).toBeInTheDocument();
    for (const name of ["Источники", "Чаты", "Новый чат"]) {
      expect(within(bar).getByRole("button", { name })).toBeInTheDocument();
    }
  });

  it("цитаты — одна запись на источник с названием источника; внутренняя ссылка ведёт в «Учёбу»", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/chat");
    const sources = await screen.findByRole("list", { name: "Источники" }, wait);
    expect(within(sources).getAllByRole("listitem")).toHaveLength(1);
    expect(within(sources).getByText("ВШЭ")).toBeInTheDocument();
    // Название источника, а не адрес сайта.
    expect(within(sources).queryByText("lk.fakeuni.test")).toBeNull();
    const row = within(sources).getByRole("button", { name: /Личный кабинет/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    await user.click(row);
    const internal = within(sources).getByRole("link", { name: /Открыть подключение в «Учёбе»/ });
    expect(internal).toHaveAttribute("href", "/study");
    expect(internal).not.toHaveAttribute("target");
    // Внешняя страница — в новой вкладке.
    expect(within(sources).getByRole("link", { name: /Расписание на сайте вуза/ })).toHaveAttribute(
      "target",
      "_blank",
    );
    await user.click(internal);
    await waitFor(() => expect(router.state.location.pathname).toBe("/study"));
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
    expect(within(sheet).queryByText("Свободно")).toBeNull();
  });
});

describe("переходы в чат: /chat?thread=, /chat?draft=", () => {
  it("/chat?thread=<id> открывает тред", async () => {
    const id = await seededThreadId();
    const { router } = renderApp(`/chat?thread=${id}`);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Что сегодня по расписанию?" }, wait),
    ).toBeTruthy();
    expect(router.state.location.search).toMatchObject({ thread: id });
  });

  it("/chat?draft=… — новый тред с заполненным полем, ничего не отправлено, заготовка ушла из адреса", async () => {
    const { router } = renderApp("/chat?draft=Хочу%20подключить%20вуз");
    expect(
      await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait),
    ).toBeTruthy();
    await waitFor(() => expect(composer()).toHaveValue("Хочу подключить вуз"), wait);
    await waitFor(() => expect(router.state.location.search).toEqual({ thread: "new" }));
    expect(outbox.snapshot).toEqual([]);
    expect(document.querySelector("[data-timeline]")).toBeNull();
  });

  it("/chat?thread=<id>&draft=… — то же в существующем треде: история на месте, поле заполнено", async () => {
    const id = await seededThreadId();
    const { router } = renderApp(`/chat?thread=${id}&draft=Уточни%20время`);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Что сегодня по расписанию?" }, wait),
    ).toBeTruthy();
    await waitFor(() => expect(composer()).toHaveValue("Уточни время"), wait);
    await waitFor(() => expect(router.state.location.search).toEqual({ thread: id }));
    expect(outbox.snapshot).toEqual([]);
    expect(await screen.findByText(/Сегодня три пары/, undefined, wait)).toBeInTheDocument();
  });

  it("у каждого чата свой черновик: начатое не теряется при переключении", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    await screen.findByText(/Сегодня три пары/, undefined, wait);
    await user.type(composer(), "Недописанное");
    await user.click(screen.getByRole("button", { name: "Новый чат" }));
    await screen.findByRole("heading", { level: 2, name: "Спросите про пары и сроки" }, wait);
    expect(composer()).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Чаты" }));
    await user.click(await screen.findByRole("link", { name: /Что сегодня по расписанию\?/ }, wait));
    await screen.findByText(/Сегодня три пары/, undefined, wait);
    expect(composer()).toHaveValue("Недописанное");
  });

  it("botai://threads/<id> в ответе — ссылка, она открывает тред в приложении, а не сайт", async () => {
    const id = await seededThreadId();
    const other = ULID(77);
    harness.server.use(
      http.get("*/api/v1/chat/threads/:threadId/messages", ({ params }) =>
        HttpResponse.json(
          params.threadId === id
            ? [
                {
                  id: ULID(1),
                  role: "agent",
                  jobId: null,
                  createdAt: AT,
                  blocks: [
                    {
                      type: "text",
                      id: ULID(2),
                      at: AT,
                      markdown: `Подключение идёт [в этом чате](botai://threads/${other}).`,
                    },
                  ],
                },
              ]
            : [],
        ),
      ),
    );
    const user = userEvent.setup();
    const { router } = renderApp(`/chat?thread=${id}`);
    const link = await screen.findByRole("link", { name: "в этом чате" }, wait);
    expect(link).toHaveAttribute("href", `/chat?thread=${other}`);
    expect(link).not.toHaveAttribute("target");
    await user.click(link);
    await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: other }));
  });
});

describe("пустой чат и подсказки из настоящих данных", () => {
  it("до трёх подсказок, построенных из данных; нажатие отправляет вопрос", async () => {
    const { user } = await openNewChat();
    const list = await screen.findByRole("list", { name: "Подсказки" }, wait);
    const items = within(list).getAllByRole("button");
    expect(items.length).toBeGreaterThan(0);
    expect(items.length).toBeLessThanOrEqual(3);
    for (const item of items) expect(item.textContent).toMatch(/\?$/);
    await user.click(items[0] as HTMLElement);
    expect(await screen.findByText(items[0]?.textContent ?? "", { selector: "p" }, wait)).toBeInTheDocument();
  });

  it("вуз не подключён: приглашение «Подключить вуз» открывает чат с заготовкой, без выдуманных подсказок", async () => {
    harness.setScenario("empty");
    renderApp("/chat?thread=new");
    const link = await screen.findByRole("link", { name: "Подключить вуз" }, wait);
    expect(link).toHaveAttribute("href", expect.stringMatching(/^\/chat\?draft=/));
    expect(screen.queryByRole("list", { name: "Подсказки" })).toBeNull();
  });
});

describe("ответ агента: таблицы, курсив, ссылки, график, план, быстрые ответы", () => {
  it("вопрос → шаги → таблица с выделением, курсив, ссылка, цитаты одной записью; поле занято и потом свободно", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какая следующая пара?");
    expect(await screen.findByText("Какая следующая пара?", { selector: "p" }, wait)).toBeInTheDocument();
    const table = await screen.findByRole("table", undefined, wait);
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Время", "Предмет", "Аудитория"]);
    const italic = screen.getByText(/Расписание обновлено 12 минут назад/);
    expect(italic.tagName).toBe("EM");
    expect(screen.getByRole("link", { name: "в кабинете" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^https:/),
    );
    const sources = await screen.findByRole("list", { name: "Источники" });
    expect(within(sources).getAllByRole("listitem")).toHaveLength(1);
    expect(within(sources).getByText(/3 страницы/)).toBeInTheDocument();
    await waitFor(() => expect(composer()).toBeEnabled(), wait);
    expect(document.querySelector("[data-live-reply]")).toBeNull();
    // Название чата подставилось из вопроса.
    await waitFor(
      () => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Какая следующая пара?"),
      wait,
    );
  });

  it("быстрые ответы под последним ответом: нажатие отправляет текст как сообщение; потом кнопки уходят", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какая следующая пара?");
    const group = await screen.findByRole("group", { name: "Быстрые ответы" }, wait);
    await user.click(within(group).getByRole("button", { name: "Что сдавать на неделе?" }));
    expect(await screen.findByText(/Ближайшие работы/, undefined, wait)).toBeInTheDocument();
    expect(screen.getAllByText("Что сдавать на неделе?", { selector: "p" })).toHaveLength(1);
    // Кнопки были под прежним ответом; теперь последний ответ — про работы, без быстрых ответов.
    await waitFor(() => expect(screen.queryByRole("group", { name: "Быстрые ответы" })).toBeNull(), wait);
  });

  it("пока агент думает, «Думает» последний в ленте; ответил — свёрнутый «Думал · N» остаётся над ответом", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какая следующая пара?");
    const turns = () =>
      [...document.querySelectorAll<HTMLElement>("[data-timeline] > li")].map((li) => li.dataset.turn);
    // Ответ пришёл целиком: перед ним — «Думал», а не пустота и не блок выше сообщений.
    await screen.findByRole("group", { name: "Быстрые ответы" }, wait);
    await waitFor(() => expect(turns()).toEqual(["user", "thinking", "block"]), wait);
    const thought = document.querySelector<HTMLElement>("[data-turn='thinking']");
    expect(thought).toHaveTextContent(/^Думал · /);
    // Блок свёрнут: шаги по клику.
    expect(within(thought as HTMLElement).getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("кнопка быстрого ответа уходит с цитатой: в ленте видно, на какой вопрос отвечено", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какая следующая пара?");
    const group = await screen.findByRole("group", { name: "Быстрые ответы" }, wait);
    await user.click(within(group).getByRole("button", { name: "Что сдавать на неделе?" }));
    await screen.findByText(/Ближайшие работы/, undefined, wait);
    const mine = [...document.querySelectorAll<HTMLElement>("[data-role='user']")].find((reply) =>
      reply.textContent?.includes("Что сдавать на неделе?"),
    );
    // Цитата — последняя фраза прежнего ответа, простым текстом; сама кнопка в ней не повторяется.
    expect(mine?.querySelector("[data-message-quote]")).toHaveTextContent("Подробнее — в кабинете.");
    expect(mine?.querySelector("[data-message-quote]")?.textContent).not.toContain("**");
    // История хранит её на сервере: после перечитывания она на месте.
    const rows = (await (
      await fetch(new URL(`/api/v1/chat/threads/${await seededThreadId()}/messages`, window.location.origin))
    ).json()) as { role: string; blocks: { quote?: { text: string } }[] }[];
    expect(rows.some((row) => row.role === "user" && row.blocks[0]?.quote?.text)).toBe(true);
  });

  it("план ответа: шаги обновляются тем же блоком и доходят до «выполнено 3 из 3»; в ленте один план", async () => {
    const { user } = await openNewChat();
    await ask(user, "Сравни нагрузку и составь план");
    expect(await screen.findByText(/выполнено 3 из 3/, undefined, wait)).toBeInTheDocument();
    expect(screen.getAllByText(/^План ·/)).toHaveLength(1);
    const table = await screen.findByRole("table", undefined, wait);
    // Выделение жирным в ячейках таблицы сохраняется.
    expect(
      within(table)
        .getAllByText("2")
        .some((cell) => cell.tagName === "STRONG"),
    ).toBe(true);
  });

  it("график ```chart: доступная картинка и таблица-альтернатива", async () => {
    const { user } = await openNewChat();
    await ask(user, "Покажи график");
    const figure = await screen.findByRole("figure", undefined, wait);
    expect(within(figure).getByRole("img")).toHaveAccessibleName(/Пар по дням/);
    expect(within(figure).getByRole("button", { name: "Показать таблицу" })).toBeInTheDocument();
  });

  it("ответ из документов: источник-документ — внутренняя ссылка «Открыть документ в «Учёбе»»", async () => {
    const { user } = await openNewChat();
    await ask(user, "Какие правила сдачи курсовых?");
    const link = await screen.findByRole("link", { name: /Открыть документ/ }, wait);
    expect(link).toHaveAttribute("href", "/study");
  });

  it("«Не знаю: …» помечено «Не уверен», источника нет и он не имитируется", async () => {
    const { user } = await openNewChat();
    await ask(user, "Расскажи анекдот");
    expect(await screen.findByText("Не уверен", undefined, wait)).toBeInTheDocument();
    expect(await screen.findByText(/^Не знаю:/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Источники" })).toBeNull();
  });

  it("сбой задания: человеческий текст без кода и без кнопки «Повторить»", async () => {
    const { user } = await openNewChat();
    await ask(user, "сбой");
    expect(await screen.findByText(/лимит ответов исчерпан/, undefined, wait)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/quota_exceeded/);
    for (const button of screen.getAllByRole("button")) {
      expect(button.textContent?.trim()).not.toMatch(/^(Повторить|Обновить|Загрузить|Попробовать снова)$/);
    }
    await waitFor(() => expect(composer()).toBeEnabled(), wait);
  });

  it("запрос на запись: вопрос у поля ввода, ApprovalCard с методом, хостом и полями, одобрение по POST /actions", async () => {
    const { user } = await openNewChat();
    await ask(user, "Мне нужна справка");
    expect(await screen.findByText("Какая справка нужна?", undefined, wait)).toBeInTheDocument();
    // Пока агент ждёт человека, писать в поле нельзя, и это сказано словами. Состояние поля
    // приходит отдельным запросом: на загруженном раннере оно может отстать от текста вопроса.
    await waitFor(() => {
      expect(composer()).toBeDisabled();
      expect(composer()).toHaveAttribute("placeholder", "Ответьте на вопрос выше");
    }, wait);
    // Вопрос с вводом стоит у поля ввода, а не в середине ленты.
    expect(document.querySelector("[data-pending-questions] [data-question='pending']")).not.toBeNull();
    await user.click(await screen.findByRole("radio", { name: "О стипендии" }, wait));
    await user.click(screen.getByRole("button", { name: "Ответить" }));
    await screen.findByText("Подать заявку на справку", undefined, wait);
    expect(screen.getByText("lk.fakeuni.test")).toBeInTheDocument();
    expect(screen.getByText("POST")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Подтвердить" }));
    await waitFor(() => {
      expect(screen.getByText(/Заявка отправлена/)).toBeInTheDocument();
      expect(composer()).toBeEnabled();
    }, wait);
    expect(document.querySelector("[data-pending-questions]")).toBeNull();
  });

  it("отказ от записи: «Не делать» — ничего не отправлено", async () => {
    const { user } = await openNewChat();
    await ask(user, "Хочу записаться на консультацию");
    await screen.findByText("Какая справка нужна?", undefined, wait);
    await user.click(await screen.findByRole("radio", { name: "Об обучении" }, wait));
    await user.click(screen.getByRole("button", { name: "Ответить" }));
    await screen.findByText("Подать заявку на справку", undefined, wait);
    await user.click(screen.getByRole("button", { name: "Не делать" }));
    expect(
      await screen.findByText(/Если передумаете, напишите ещё раз/, undefined, wait),
    ).toBeInTheDocument();
  });

  it("агент не может отправить форму сам: готовый текст с «Скопировать» остаётся в ленте после ответа", async () => {
    const { user } = await openNewChat();
    await ask(user, "Подай заявку вручную");
    expect(await screen.findByRole("button", { name: "Скопировать" }, wait)).toBeInTheDocument();
    const open = screen.getByRole("link", { name: /Открыть сайт вуза/ });
    expect(open).toHaveAttribute("href", "https://lk.fakeuni.test/requests/new");
    expect(open).toHaveAttribute("target", "_blank");
    await waitFor(() => expect(composer()).toBeEnabled(), wait);
    expect(screen.getByRole("button", { name: "Скопировать" })).toBeInTheDocument();
  });
});

describe("нет сети", () => {
  it("сообщение ждёт с подписью «Отправится, когда появится сеть» и уходит само, без кнопки", async () => {
    const { user } = await openNewChat();
    const { vi } = await import("vitest");
    const online = vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    await ask(user, "Что сдавать на неделе?");
    expect(await screen.findByText("Отправится, когда появится сеть")).toBeInTheDocument();
    expect(screen.getByText("Что сдавать на неделе?", { selector: "p" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Повторить|Отправить ещё раз/ })).toBeNull();
    expect(outbox.snapshot).toHaveLength(1);

    online.mockReturnValue(true);
    window.dispatchEvent(new Event("online"));
    expect(await screen.findByText(/Ближайшие работы/, undefined, wait)).toBeInTheDocument();
    expect(screen.queryByText("Отправится, когда появится сеть")).toBeNull();
    expect(outbox.snapshot).toHaveLength(0);
    expect(screen.getAllByText("Что сдавать на неделе?", { selector: "p" })).toHaveLength(1);
  });
});
