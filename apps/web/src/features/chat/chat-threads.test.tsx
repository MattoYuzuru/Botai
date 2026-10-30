import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import type { ThreadOut } from "@/api/v3";
import { chatHarness, composer, wait } from "@/test/chat-harness";
import { renderApp } from "@/test/render-app";
import { chatKeys } from "./queries";
import { outbox } from "./runtime";
import { plainPreview } from "./threads-panel";
import { attentionCount, attentionToast } from "./use-thread-watcher";

const harness = chatHarness();

const AT = "2026-10-27T11:00:00.000Z";
const thread = (partial: Partial<ThreadOut> & Pick<ThreadOut, "id" | "title">): ThreadOut => ({
  spaceId: null,
  createdAt: AT,
  lastMessageAt: AT,
  unread: false,
  attention: null,
  activeTask: null,
  preview: null,
  ...partial,
});

const task = (kind: "connect" | "sync" | "ingest") => ({
  jobId: "J1",
  kind,
  connectionId: null,
  state: "waiting_user",
  title: "Подключение",
});

describe("список чатов", () => {
  it("непрочитанное — кружок, «Ждёт вас», «Работает», «Не удалось», превью простым текстом", async () => {
    const rows = [
      thread({
        id: "T1",
        title: "Подключение ЦУ",
        unread: true,
        attention: "waiting_user",
        activeTask: task("connect"),
        preview: "**Нужен** ваш вход — [откройте чат](botai://threads/T1)",
      }),
      thread({ id: "T2", title: "Индексация документа", attention: "running" }),
      thread({ id: "T3", title: "Вход в ЛК", attention: "failed" }),
      thread({ id: "T4", title: "Просто разговор" }),
    ];
    harness.server.use(http.get("*/api/v1/chat/threads", () => HttpResponse.json(rows)));
    const user = userEvent.setup();
    renderApp("/chat?thread=new");
    await user.click(await screen.findByRole("button", { name: "Чаты" }, wait));
    const sheet = await screen.findByRole("dialog", { name: "Чаты" });
    const list = await within(sheet).findByRole("list", { name: "Чаты" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    const [waiting, running, failed, plain] = items as HTMLElement[];
    expect(within(waiting as HTMLElement).getByText("Ждёт вас")).toBeInTheDocument();
    expect(
      within(waiting as HTMLElement).getByRole("img", { name: "Есть непрочитанное" }),
    ).toBeInTheDocument();
    expect(within(waiting as HTMLElement).getByText("Нужен ваш вход — откройте чат")).toBeInTheDocument();
    expect(within(running as HTMLElement).getByText("Работает")).toBeInTheDocument();
    expect(within(failed as HTMLElement).getByText("Не удалось")).toBeInTheDocument();
    expect(within(plain as HTMLElement).queryByText("Ждёт вас")).toBeNull();
    expect(within(plain as HTMLElement).queryByRole("img", { name: "Есть непрочитанное" })).toBeNull();
    // Ссылка на тред — обычная ссылка: «назад» и открытие в новой вкладке работают.
    expect(within(waiting as HTMLElement).getByRole("link", { name: /Подключение ЦУ/ })).toHaveAttribute(
      "href",
      "/chat?thread=T1",
    );
  });

  it("значок на вкладке «Чат» считает чаты, где агент ждёт или есть непрочитанное, кроме открытого", async () => {
    harness.server.use(
      http.get("*/api/v1/chat/threads", () =>
        HttpResponse.json([
          thread({ id: "T1", title: "Один", unread: true }),
          thread({ id: "T2", title: "Два", attention: "waiting_user" }),
          thread({ id: "T3", title: "Три" }),
        ]),
      ),
    );
    renderApp("/chat?thread=T3");
    const tab = await screen.findByRole("link", { name: /^2\s*Чат|Чат/ }, wait);
    await waitFor(() => expect(within(tab).getByText("2")).toBeInTheDocument(), wait);
    expect(attentionCount([thread({ id: "a", title: "a", unread: true })], "a")).toBe(0);
    expect(attentionCount(undefined)).toBe(0);
  });

  it("превью без разметки: жирный, ссылки, таблицы и код в списке одной строкой", () => {
    expect(plainPreview("**Сегодня** три [пары](https://x.test)\n\n| A | B |\n|---|---|\n| 1 | 2 |")).toBe(
      "Сегодня три пары A B 1 2",
    );
    expect(plainPreview("```chart\n{}\n```\nГрафик готов")).toBe("График готов");
  });

  it("переименование на месте: Enter сохраняет название, оно видно и в списке, и в заголовке", async () => {
    const user = userEvent.setup();
    renderApp("/chat");
    await user.click(await screen.findByRole("button", { name: "Чаты" }, wait));
    await user.click(
      await screen.findByRole("button", { name: /Переименовать «Что сегодня по расписанию\?»/ }, wait),
    );
    const field = screen.getByRole("textbox", { name: "Название чата" });
    await user.clear(field);
    await user.type(field, "Расписание на сегодня{Enter}");
    const sheet = screen.getByRole("dialog", { name: "Чаты" });
    expect(
      await within(sheet).findByRole("link", { name: /Расписание на сегодня/ }, wait),
    ).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: "Закрыть" }));
    await waitFor(
      () => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Расписание на сегодня"),
      wait,
    );
  });

  it("удаление спрашивает подтверждение; открытый чат после удаления уступает место новому", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/chat");
    await user.click(await screen.findByRole("button", { name: "Чаты" }, wait));
    await user.click(
      await screen.findByRole("button", { name: /Удалить «Что сегодня по расписанию\?»/ }, wait),
    );
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/задачи остановятся/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(screen.getByRole("link", { name: /Что сегодня по расписанию\?/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Удалить «Что сегодня по расписанию\?»/ }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Удалить чат" }),
    );
    await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: "new" }), wait);
    expect(await screen.findByText("Чат удалён")).toBeInTheDocument();
    await waitFor(
      () => expect(screen.queryByRole("link", { name: /Что сегодня по расписанию\?/ })).toBeNull(),
      wait,
    );
  });
});

describe("прочтение и уведомления", () => {
  it("открытый тред отмечается прочитанным (POST /read), кружок гаснет сразу", async () => {
    const reads: string[] = [];
    let unread = true;
    const real = await fetch(new URL("/api/v1/chat/threads", window.location.origin)).then(
      (response) => response.json() as Promise<ThreadOut[]>,
    );
    const seed = real[0] as ThreadOut;
    harness.server.use(
      http.get("*/api/v1/chat/threads", () => HttpResponse.json([{ ...seed, unread }])),
      http.post("*/api/v1/chat/threads/:threadId/read", ({ params }) => {
        reads.push(String(params.threadId));
        unread = false;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { queryClient } = renderApp(`/chat?thread=${seed.id}`);
    await screen.findByText(/Сегодня три пары/, undefined, wait);
    await waitFor(() => expect(reads).toEqual([seed.id]), wait);
    expect(queryClient.getQueryData<ThreadOut[]>(chatKeys.threads)?.[0]?.unread).toBe(false);
  });

  it("прочитанным тред не отмечается, пока самое новое его сообщение не показано на экране", async () => {
    const reads: string[] = [];
    const real = await fetch(new URL("/api/v1/chat/threads", window.location.origin)).then(
      (response) => response.json() as Promise<ThreadOut[]>,
    );
    const seed = real[0] as ThreadOut;
    // Список знает о более новом сообщении, чем загружено в ленту: прочитать его ещё нельзя.
    harness.server.use(
      http.get("*/api/v1/chat/threads", () =>
        HttpResponse.json([{ ...seed, unread: true, lastMessageAt: "2099-01-01T00:00:00.000Z" }]),
      ),
      http.post("*/api/v1/chat/threads/:threadId/read", ({ params }) => {
        reads.push(String(params.threadId));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp(`/chat?thread=${seed.id}`);
    await screen.findByText(/Сегодня три пары/, undefined, wait);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(reads).toEqual([]);
  });

  it("тред ушёл в «ждёт вас» вне открытого: тост «Ботай просит войти: …» ведёт прямо в этот тред", async () => {
    let rows = [thread({ id: "T1", title: "Старый" }), thread({ id: "T2", title: "Подключение ЦУ" })];
    harness.server.use(http.get("*/api/v1/chat/threads", () => HttpResponse.json(rows)));
    const user = userEvent.setup();
    const { router, queryClient } = renderApp("/chat?thread=T1");
    await screen.findByRole("heading", { level: 1, name: "Старый" }, wait);
    // Первый ответ списка только запоминается.
    expect(screen.queryByText(/Ботай просит войти/)).toBeNull();

    rows = [
      thread({ id: "T1", title: "Старый" }),
      thread({
        id: "T2",
        title: "Подключение ЦУ",
        unread: true,
        attention: "waiting_user",
        activeTask: task("connect"),
      }),
    ];
    await queryClient.invalidateQueries({ queryKey: chatKeys.threads });
    expect(
      await screen.findByText("Ботай просит войти: Подключение ЦУ", undefined, wait),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Открыть" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ thread: "T2" }), wait);
  });

  it("для открытого треда уведомления нет; не вход — «Ботай ждёт вас»", async () => {
    let rows = [thread({ id: "T1", title: "Открытый" })];
    harness.server.use(http.get("*/api/v1/chat/threads", () => HttpResponse.json(rows)));
    const { queryClient } = renderApp("/chat?thread=T1");
    await screen.findByRole("heading", { level: 1, name: "Открытый" }, wait);
    rows = [thread({ id: "T1", title: "Открытый", attention: "waiting_user", activeTask: task("connect") })];
    await queryClient.invalidateQueries({ queryKey: chatKeys.threads });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.queryByText(/Ботай просит войти/)).toBeNull();
    expect(attentionToast(thread({ id: "x", title: "Документ", activeTask: task("ingest") })).title).toBe(
      "Ботай ждёт вас: Документ",
    );
    expect(attentionToast(thread({ id: "x", title: "ЦУ", activeTask: task("sync") })).title).toBe(
      "Ботай просит войти: ЦУ",
    );
    expect(composer()).toBeTruthy();
  });
});

describe("«Источники»: вузы и их источники со статусами", () => {
  it("панель показывает вузы и источники с состоянием; «Добавить источник» кладёт заготовку в поле нового чата", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/chat");
    await user.click(await screen.findByRole("button", { name: "Источники" }, wait));
    const sheet = await screen.findByRole("dialog", { name: "Источники" });
    expect(
      await within(sheet).findByRole("heading", { name: "Высшая школа экономики" }, wait),
    ).toBeInTheDocument();
    expect(within(sheet).getByRole("heading", { name: "Центральный университет" })).toBeInTheDocument();
    expect(within(sheet).getAllByText("Работает").length).toBeGreaterThanOrEqual(3);
    expect(within(sheet).getByText("Личный кабинет", { selector: "span" })).toBeInTheDocument();
    expect(within(sheet).getByText(/Обновлено 5 минут назад/)).toBeInTheDocument();

    await user.click(within(sheet).getByRole("link", { name: "Добавить источник для ВШЭ" }));
    await waitFor(() => expect(composer()).toHaveValue("Добавь источник для ВШЭ: "), wait);
    expect(router.state.location.search).toEqual({ thread: "new" });
    expect(outboxEmpty()).toBe(true);
  });
});

const outboxEmpty = (): boolean => outbox.snapshot.length === 0;
