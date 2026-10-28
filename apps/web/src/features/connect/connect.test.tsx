import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { outbox } from "@/features/chat/runtime";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";
import type { JobVariant } from "@/mocks/connect-script";
import type { Scenario } from "@/mocks/db";
import { renderApp } from "@/test/render-app";
import { createTestServer } from "@/test/server";

let current = createTestServer("empty");
function switchScenario(scenario: Scenario, variant: JobVariant = "basic", jobPace = 0) {
  current.server.close();
  current = createTestServer(scenario, { jobPace });
  current.db.jobVariant = variant;
  current.server.listen({ onUnhandledRequest: "error" });
}
beforeAll(() => current.server.listen({ onUnhandledRequest: "error" }));
afterAll(() => current.server.close());
beforeEach(() => {
  jobManager.stopAll();
  useActiveJob.setState({ job: null });
  window.sessionStorage.clear();
  window.localStorage.clear();
  outbox.clear();
  switchScenario("empty");
});
afterEach(() => {
  current.server.resetHandlers();
  act(() => jobManager.stopAll());
});
const WAIT = { timeout: 5000 };
const URL_QUESTION = "Пришлите ссылку на источник, который хотите подключить.";

async function send(text: string) {
  const composer = await screen.findByRole("textbox", { name: "Сообщение агенту" });
  await waitFor(() => expect(composer).toBeEnabled(), WAIT);
  await userEvent.type(composer, text);
  await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
}
async function start(variant: JobVariant = "basic") {
  current.db.jobVariant = variant;
  const app = renderApp("/connect?university=u-hse");
  await screen.findByText(URL_QUESTION, undefined, WAIT);
  await send("https://lk.fakeuni.test/");
  await screen.findByText("Вход в lk.fakeuni.test. Логин", undefined, WAIT);
  return app;
}
async function answer(text: string) {
  await waitFor(() => expect(screen.getByLabelText("Ответ")).toBeEnabled(), WAIT);
  await userEvent.type(screen.getByLabelText("Ответ"), text);
  await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
}
async function courses(variant: JobVariant = "basic") {
  const app = await start(variant);
  await answer("anna");
  await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, WAIT);
  await answer("fixture-password");
  await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT);
  return app;
}
async function finishCourses() {
  await userEvent.click(await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT));
  await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
}

describe("подключение в обычном чате", () => {
  it("сразу открывает canonical chat с одним вопросом о ссылке, без отдельного конфигуратора", async () => {
    const { router } = renderApp("/connect");
    expect(await screen.findByText(URL_QUESTION, undefined, WAIT)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/chat");
    expect(router.state.location.search.thread).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "Чат" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Начать подключение" })).toBeNull();
    expect(current.db.spaces).toHaveLength(0);
    expect(outbox.snapshot).toHaveLength(0);
  });

  it("вопрос остаётся тем же DOM turn до и после принятого ответа, хронология идёт вниз", async () => {
    renderApp("/connect");
    const question = await screen.findByText(URL_QUESTION, undefined, WAIT);
    const turn = question.closest("[data-conversation-turn]");
    await send("https://lk.fakeuni.test/");
    await screen.findByText("Вход в lk.fakeuni.test. Логин", undefined, WAIT);
    expect(screen.getByText(URL_QUESTION).closest("[data-conversation-turn]")).toBe(turn);
    const history = screen.getByRole("list", { name: "Сообщения" }).textContent ?? "";
    expect(history.indexOf(URL_QUESTION)).toBeLessThan(history.indexOf("https://lk.fakeuni.test/"));
    expect(history.indexOf("https://lk.fakeuni.test/")).toBeLessThan(
      history.indexOf("Вход в lk.fakeuni.test. Логин"),
    );
    expect(outbox.snapshot).toHaveLength(0);
  });

  it("native username/secret идут защищёнными блоками, composer блокируется; секрет не попадает в историю/хранилища", async () => {
    await start();
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toBeDisabled();
    expect(screen.getByText("Вход через поля BotAI")).toBeInTheDocument();
    await answer("anna");
    await waitFor(() => expect(screen.getByLabelText("Ответ")).toHaveAttribute("type", "password"), WAIT);
    const password = screen.getByLabelText("Ответ");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByRole("button", { name: "Открыть браузер" }).closest("form")).toBeNull();
    await answer("never-persist-this-secret");
    await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT);
    expect(document.body.textContent).not.toContain("never-persist-this-secret");
    for (const storage of [window.localStorage, window.sessionStorage])
      for (const key of Object.keys(storage))
        expect(storage.getItem(key)).not.toContain("never-persist-this-secret");
    expect(outbox.snapshot).toHaveLength(0);
    expect(screen.getAllByText("Ответ отправлен").length).toBeGreaterThan(0);
  });

  it("после завершения источника обычный вопрос идёт в том же чате без CONNECT target", async () => {
    const { router } = await courses();
    const thread = router.state.location.search.thread;
    await finishCourses();
    await screen.findByRole("heading", { level: 3, name: "Что подключено" }, WAIT);
    await send("Что сегодня по расписанию?");
    await screen.findByText(/Следующая пара|На сегодня пары закончились/, undefined, WAIT);
    expect(router.state.location.search.thread).toBe(thread);
    expect(outbox.snapshot).toHaveLength(0);
    expect(current.db.connections).toHaveLength(1);
  });

  it("вуз не принял пароль: новый вопрос остаётся в хронологии, прежнее поле не активно", async () => {
    await start("wrong-password");
    await answer("anna");
    await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, WAIT);
    await answer("wrong-fixture");
    expect(await screen.findByText(/попытка 2 из 3/, undefined, WAIT)).toBeInTheDocument();
    expect(screen.getAllByLabelText("Ответ")).toHaveLength(1);
    expect(screen.getByLabelText("Ответ")).toHaveValue("");
  });

  it("push ждёт человека без подставной кнопки ответа, затем продолжает", async () => {
    switchScenario("empty", "push", 0.1);
    await start("push");
    await answer("anna");
    await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, WAIT);
    await answer("fixture-password");
    expect(await screen.findByText(/Подтвердите вход в приложении/, undefined, WAIT)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ответить" })).toBeNull();
    await screen.findByRole("checkbox", { name: /Алгоритмы/ }, WAIT);
  });

  it("сбой источника показан словами и предлагает бизнес-действие нового входа", async () => {
    await start("failure");
    await answer("anna");
    await screen.findByText("Вход в lk.fakeuni.test. Пароль", undefined, WAIT);
    await answer("fixture-password");
    expect(await screen.findByText(/Сайт вуза не ответил/, undefined, WAIT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Войти заново" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
    expect(document.body.textContent).not.toContain("source_unavailable");
  });

  it("работа остаётся в фоне, Открыть возвращает canonical thread", async () => {
    const { router } = await start();
    const thread = router.state.location.search.thread;
    await act(() => router.navigate({ to: "/" }));
    expect(await screen.findByText("Нужен ответ", undefined, WAIT)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Открыть" }));
    await waitFor(() => expect(router.state.location.search.thread).toBe(thread), WAIT);
    expect(await screen.findByRole("button", { name: "Открыть браузер" }, WAIT)).toBeInTheDocument();
  });

  it("write approval сохраняет метод/поля/одноразовое подтверждение внутри чата", async () => {
    await courses("approval");
    await finishCourses();
    await screen.findByText("Записаться на консультацию по базам данных", undefined, WAIT);
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(screen.getByText("lk.fakeuni.test")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    await screen.findByRole("heading", { level: 3, name: "Что подключено" }, WAIT);
    expect(screen.queryByRole("button", { name: "Подтвердить" })).toBeNull();
  });

  it("отклонение write approval продолжает безопасное чтение", async () => {
    await courses("approval");
    await finishCourses();
    await userEvent.click(await screen.findByRole("button", { name: "Не делать" }, WAIT));
    await screen.findByRole("heading", { level: 3, name: "Что подключено" }, WAIT);
  });

  it("отмена требует подтверждения, безопасный Продолжить получает фокус", async () => {
    await start();
    await userEvent.click(screen.getByRole("button", { name: "Источник и подключения" }));
    await userEvent.click(screen.getByRole("button", { name: "Отменить подключение" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Отменить подключение?" });
    expect(within(dialog).getByRole("button", { name: "Продолжить" })).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: "Отменить подключение" }));
    expect(await screen.findByText(/Подключение отменено/, undefined, WAIT)).toBeInTheDocument();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
  });
});

describe("необязательный каталог в контексте чата", () => {
  it("поиск/выбор с клавиатуры сохраняются, предупреждение каталога видно до уточнения", async () => {
    renderApp("/connect");
    await screen.findByText(URL_QUESTION, undefined, WAIT);
    await userEvent.click(screen.getByRole("button", { name: "Источник и подключения" }));
    const input = await screen.findByRole("combobox", { name: "Вуз" });
    await userEvent.type(input, "ЦУ");
    await screen.findByRole("option", { name: /Центральный университет/ }, WAIT);
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(input).toHaveValue("ЦУ");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(/Правила вуза ограничивают сторонний доступ/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Указать вуз в чате" })).toBeEnabled();
  });

  it("существующий источник открывает прежний разговор, Новый источник — отдельное подключение", async () => {
    switchScenario("connected");
    const count = current.db.connections.length;
    renderApp("/connect");
    await screen.findByText(URL_QUESTION, undefined, WAIT);
    await userEvent.click(screen.getByRole("button", { name: "Источник и подключения" }));
    const links = await screen.findAllByRole("button", { name: "Открыть разговор" });
    await userEvent.click(links[0] as HTMLElement);
    await waitFor(
      () => expect(screen.queryByRole("dialog", { name: "Источник и подключения" })).toBeNull(),
      WAIT,
    );
    expect(current.db.connections).toHaveLength(count);
    expect(screen.queryByRole("button", { name: "Начать подключение" })).toBeNull();
  });

  it("известное пространство передаётся при создании нового источника", async () => {
    switchScenario("connected");
    const space = current.db.spaces[0];
    if (!space) throw new Error("missing fixture space");
    renderApp(`/connect?space=${space.id}`);
    await screen.findByText(URL_QUESTION, undefined, WAIT);
    await send("https://lk.fakeuni.test/second");
    await screen.findByText("Вход в lk.fakeuni.test. Логин", undefined, WAIT);
    expect(current.db.connections.at(-1)?.spaceId).toBe(space.id);
  });
});
