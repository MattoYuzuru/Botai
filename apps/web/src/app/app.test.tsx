import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { DEMO, type Scenario } from "@/mocks/db";
import { hrefOf } from "@/test/data-fixtures";
import { renderApp } from "@/test/render-app";
import { createTestServer } from "@/test/server";

let current = createTestServer();

function useScenario(scenario: Scenario) {
  current.server.close();
  current = createTestServer(scenario);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => current.server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => current.server.close());
beforeEach(() => useScenario("fresh"));

describe("вход и регистрация", () => {
  it("без сессии адрес приложения ведёт на экран входа", async () => {
    const { router } = renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Вход" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("неверный пароль: ошибка у поля пароля, без технических слов и без кнопки повтора", async () => {
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Почта"), DEMO.email);
    await userEvent.type(screen.getByLabelText("Пароль"), "не-тот-пароль");
    await userEvent.click(screen.getByRole("button", { name: "Войти" }));
    const field = await screen.findByLabelText("Пароль");
    await waitFor(() => expect(field).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByText("Неверная почта или пароль")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Повторить|Обновить/ })).toBeNull();
  });

  it("верный пароль открывает «Сегодня» с пятью вкладками; без вуза — приглашение подключить", async () => {
    const { router } = renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Почта"), DEMO.email);
    await userEvent.type(screen.getByLabelText("Пароль"), DEMO.password);
    await userEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Сегодня" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
    const nav = screen.getByRole("navigation", { name: "Основная навигация" });
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
    const link = hrefOf(await screen.findByRole("link", { name: "Подключить вуз" }));
    expect(link.pathname).toBe("/chat");
    expect(link.draft).toBeTruthy();
    expect(screen.getByText("Вузов пока нет")).toBeInTheDocument();
  });

  it("вошедшего пользователя экран входа сразу отправляет в приложение", async () => {
    useScenario("empty");
    const { router } = renderApp("/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Сегодня" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("регистрация: проверка полей до запроса", async () => {
    renderApp("/register");
    await userEvent.click(await screen.findByRole("button", { name: "Создать аккаунт" }));
    expect(await screen.findByText("Код приглашения — не меньше 6 знаков")).toBeInTheDocument();
    expect(screen.getByText("Как к вам обращаться?")).toBeInTheDocument();
    expect(screen.getByText(/Введите почту целиком/)).toBeInTheDocument();
    expect(screen.getByText("Пароль — не меньше 8 знаков")).toBeInTheDocument();
  });

  it("регистрация: чужой код приглашения отклоняется у поля кода", async () => {
    renderApp("/register");
    await userEvent.type(await screen.findByLabelText("Код приглашения"), "WRONG-CODE");
    await userEvent.type(screen.getByLabelText("Как к вам обращаться"), "Борис");
    await userEvent.type(screen.getByLabelText("Почта"), "boris@example.test");
    await userEvent.type(screen.getByLabelText("Пароль"), "long-enough-password");
    await userEvent.click(screen.getByRole("button", { name: "Создать аккаунт" }));
    expect(
      await screen.findByText("Приглашение недействительно, истекло или уже использовано"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Код приглашения")).toHaveAttribute("aria-invalid", "true");
  });

  it("регистрация по приглашению ведёт в онбординг", async () => {
    const { router } = renderApp("/register?invite=BOTAI-DEMO");
    // Код подставлен из адреса и убран из истории.
    expect(await screen.findByLabelText("Код приглашения")).toHaveValue("BOTAI-DEMO");
    await waitFor(() => expect(router.state.location.search).toEqual({}));
    await userEvent.type(screen.getByLabelText("Как к вам обращаться"), "Борис");
    await userEvent.type(screen.getByLabelText("Почта"), "boris@example.test");
    await userEvent.type(screen.getByLabelText("Пароль"), "long-enough-password");
    await userEvent.click(screen.getByRole("button", { name: "Создать аккаунт" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: /Расписание и сроки из всех ваших вузов/ }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/welcome");
  });

  it("поле пароля имеет нативную семантику менеджера паролей и кегль 16 px", async () => {
    renderApp("/login");
    const password = await screen.findByLabelText("Пароль");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveClass("type-body");
    expect(screen.getByLabelText("Почта")).toHaveAttribute("autocomplete", "username");
  });
});

describe("онбординг", () => {
  it("объясняет, что такое BotAI, что с паролем и как подключается вуз; первый шаг — «Подключить вуз»", async () => {
    useScenario("empty");
    renderApp("/welcome");
    expect(await screen.findByRole("heading", { level: 2, name: "Что такое BotAI" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Что будет с паролем" })).toBeInTheDocument();
    expect(screen.getByText(/в зашифрованном виде/)).toBeInTheDocument();
    expect(screen.getByText(/проходит через сервер BotAI/)).toBeInTheDocument();
    expect(screen.getByText(/обходить такие защиты мы не будем/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Подключить вуз" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/chat\?draft=/),
    );
    expect(screen.getByRole("link", { name: "Позже" })).toHaveAttribute("href", "/");
  });

  it("без сессии онбординг недоступен", async () => {
    const { router } = renderApp("/welcome");
    expect(await screen.findByRole("heading", { level: 1, name: "Вход" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });
});

describe("каркас приложения", () => {
  it("пять вкладок в порядке §7.1, активная помечена", async () => {
    useScenario("connected");
    renderApp("/schedule");
    const nav = await screen.findByRole("navigation", { name: "Основная навигация" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "Сегодня",
      "Расписание",
      "Задания",
      "Чат",
      "Учёба",
    ]);
    expect(within(nav).getByRole("link", { name: "Расписание" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Сегодня" })).not.toHaveAttribute("aria-current");
  });

  it("вкладки ведут на свои экраны", async () => {
    useScenario("connected");
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Основная навигация" });
    for (const [name, title] of [
      ["Расписание", "Расписание"],
      ["Задания", "Задания"],
      // У чата заголовок — название открытого треда, а «Чат» стоит подписью под ним.
      ["Чат", "Что сегодня по расписанию?"],
      ["Учёба", "Учёба"],
      ["Сегодня", "Сегодня"],
    ] as const) {
      await userEvent.click(within(nav).getByRole("link", { name }));
      // Экран чата загружается отдельным чанком: холодный импорт в CI может занять больше 1 с.
      expect(
        await screen.findByRole("heading", { level: 1, name: title }, { timeout: 5_000 }),
      ).toBeInTheDocument();
    }
  });

  it("переключатель вуза показывает состояние источника каждого вуза", async () => {
    useScenario("reauth");
    renderApp("/schedule");
    await userEvent.click(await screen.findByRole("button", { name: /Все вузы/ }));
    const sheet = await screen.findByRole("dialog", { name: "Вуз" });
    expect(await within(sheet).findByText("Нужно снова войти в ЛК ЦУ")).toBeInTheDocument();
    expect(within(sheet).getByText(/Обновлено/)).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Подключить ещё вуз" })).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: /Повторить|Обновить/ })).toBeNull();
  });

  it("выбор вуза меняет акцент пространства на тон вуза", async () => {
    useScenario("connected");
    const { container } = renderApp("/schedule");
    await userEvent.click(await screen.findByRole("button", { name: /Все вузы/ }));
    const sheet = await screen.findByRole("dialog", { name: "Вуз" });
    await userEvent.click(await within(sheet).findByRole("button", { name: /Центральный университет/ }));
    await waitFor(() => expect(container.querySelector("[data-space]")).toHaveAttribute("data-space", "cu"));
  });

  it("«Учёба»: профиль, оформление и выход", async () => {
    useScenario("connected");
    const { router } = renderApp("/study");
    expect(await screen.findByRole("heading", { level: 2, name: "Профиль и настройки" })).toBeInTheDocument();
    expect(await screen.findByText(DEMO.email)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Тёмная" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Спокойное движение/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Выйти из BotAI" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Вход" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
  });

  it("несуществующий адрес — человеческая страница, а не белый экран", async () => {
    useScenario("connected");
    renderApp("/nowhere");
    expect(await screen.findByText("Такой страницы нет")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "На главную" })).toBeInTheDocument();
  });
});
