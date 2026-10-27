import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
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
  switchScenario("empty");
});
afterEach(() => current.server.resetHandlers());

const choose = async (name: RegExp | string) => {
  await userEvent.click(await screen.findByRole("radio", { name }));
};

describe("выбор вуза", () => {
  it("список из учебного реестра пула и «Другой вуз»; без выбора начать нельзя", async () => {
    renderApp("/connect");
    expect(await screen.findByRole("radio", { name: /Высшая школа экономики/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Центральный университет/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Другой вуз/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Начать подключение" }));
    expect(await screen.findByText("Выберите вуз из списка или укажите свой")).toBeInTheDocument();
  });

  it("поиск сужает список по названию и сокращению", async () => {
    renderApp("/connect");
    await userEvent.type(await screen.findByLabelText("Вуз"), "ЦУ");
    await waitFor(() => expect(screen.queryByRole("radio", { name: /Высшая школа экономики/ })).toBeNull());
    expect(await screen.findByRole("radio", { name: /Центральный университет/ })).toBeInTheDocument();
  });

  it("предупреждение вуза о стороннем доступе показано до начала (Q12)", async () => {
    renderApp("/connect");
    await choose(/Центральный университет/);
    expect(await screen.findByText(/ограничивают сторонний доступ/)).toBeInTheDocument();
  });

  it("адрес кабинета проверяется; свой вуз требует названия", async () => {
    renderApp("/connect");
    await choose(/Другой вуз/);
    await userEvent.type(screen.getByLabelText(/Адрес личного кабинета/), "javascript:alert(1)");
    await userEvent.click(screen.getByRole("button", { name: "Начать подключение" }));
    expect(await screen.findByText("Нужна ссылка вида https://lk.example.ru")).toBeInTheDocument();
    expect(screen.getByText("Как называется вуз?")).toBeInTheDocument();
  });

  it("предупреждает, что ввод проходит через сервер и пароль хранится зашифрованным", async () => {
    renderApp("/connect");
    expect(await screen.findByText(/пароль хранится зашифрованным/)).toBeInTheDocument();
    expect(screen.getByText(/Ввод при входе проходит через сервер BotAI/)).toBeInTheDocument();
  });
});

async function startHse() {
  const utils = renderApp("/connect");
  await choose(/Высшая школа экономики/);
  await userEvent.click(screen.getByRole("button", { name: "Начать подключение" }));
  return utils;
}

describe("ProgressWall: подключение вуза", () => {
  it("весь путь: логин, пароль, предметы, итог, «Готово»", async () => {
    const { router } = await startHse();
    expect(await screen.findByRole("heading", { level: 1, name: "Подключаю ВШЭ" })).toBeInTheDocument();
    expect(router.state.location.pathname).toMatch(/^\/connect\/01J/);
    // Рельс без процентов; остаток неизвестен — и это сказано.
    expect(await screen.findByText("Сколько осталось — пока неизвестно")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\d\s*%/);

    // Логин — обычное поле с подсказкой для менеджера паролей.
    const login = await screen.findByText("Вход в lk.fakeuni.test. Логин");
    expect(login).toBeInTheDocument();
    expect(screen.getByText("Жду ответа")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Ответ"), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));

    // Пароль — секретное поле: значение не остаётся ни в поле, ни в истории.
    await screen.findByText("Вход в lk.fakeuni.test. Пароль");
    const password = await screen.findByLabelText("Ответ");
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    await userEvent.type(password, "correct-horse");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await waitFor(() => expect(document.body.textContent).not.toContain("correct-horse"));
    expect(await screen.findByText("Введено")).toBeInTheDocument();
    expect(screen.getByText("anna")).toBeInTheDocument();

    // Выбор предметов.
    await userEvent.click(await screen.findByRole("checkbox", { name: /Алгоритмы/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Базы данных/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));

    // Итог: что подключено, что нет, «Готово» — экран не исчезает сам.
    expect(await screen.findByRole("heading", { level: 3, name: "Что подключено" })).toBeInTheDocument();
    expect(screen.getByText("Предположение")).toBeInTheDocument();
    expect(screen.getByText("Оценки")).toBeInTheDocument();
    expect(screen.getByRole("status", { hidden: true })).toBeInTheDocument();
    const stream = screen.getByRole("button", { name: /^Думал/ });
    expect(stream).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Готово" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    // Вуз появился: на «Сегодня» уже не онбординг.
    expect(await screen.findByRole("region", { name: "Пары" })).toBeInTheDocument();
    expect(screen.queryByText("Вузов пока нет")).toBeNull();
    expect(useActiveJob.getState().job).toBeNull();
    expect(current.db.connections.at(-1)).toMatchObject({ status: "active", hasPassword: true });
  });

  it("вуз не принял пароль: вопрос повторяется с понятным текстом и счётчиком попыток", async () => {
    switchScenario("empty", "wrong-password");
    await startHse();
    await userEvent.type(await screen.findByLabelText("Ответ"), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await userEvent.type(await screen.findByLabelText("Ответ"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(
      await screen.findByText(
        /Вуз не принял данные\. Проверьте и введите ещё раз: Пароль \(попытка 2 из 3\)/,
      ),
    ).toBeInTheDocument();
    // Упавший шаг — текстом danger-fg с причиной.
    expect(screen.getByText("Проверьте раскладку и язык ввода")).toBeInTheDocument();
  });

  it("вход по push: карточка ожидания без кнопок, работа продолжается сама", async () => {
    // Ожидание на телефоне — пауза сценария: ей нужно время, иначе карточка исчезнет раньше, чем её найдёт тест.
    switchScenario("empty", "push", 0.5);
    await startHse();
    await userEvent.type(await screen.findByLabelText("Ответ"), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await userEvent.type(await screen.findByLabelText("Ответ"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(
      await screen.findByText(/Подтвердите вход на телефоне|Подтвердите вход в приложении/),
    ).toBeInTheDocument();
    // Подтверждение прошло, работа пошла дальше: карточка ожидания не остаётся рядом с новым вопросом.
    expect(
      await screen.findByText("Это ваши предметы в этом семестре?", {}, { timeout: 4000 }),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/Подтвердите вход в приложении/)).toBeNull());
  });

  it("сбой источника: человеческий текст и «Спросить в чате», кнопки повтора нет", async () => {
    switchScenario("empty", "failure");
    const { router } = await startHse();
    await userEvent.type(await screen.findByLabelText("Ответ"), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await userEvent.type(await screen.findByLabelText("Ответ"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(await screen.findByText("Подключение не завершено.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Спросить в чате" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(screen.queryByRole("button", { name: /Повторить|Обновить|Загрузить/ })).toBeNull();
    // В тексте нет технических кодов.
    expect(document.body.textContent).not.toMatch(/source_unavailable|502|traceId/);
  });

  it("свёрнутая работа остаётся в шапке: «Нужен ответ», «Открыть» возвращает на экран", async () => {
    const { router } = await startHse();
    await screen.findByText("Вход в lk.fakeuni.test. Логин");
    await userEvent.click(screen.getByRole("button", { name: "Свернуть" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    const bar = await screen.findByText("Подключаю ВШЭ");
    expect(bar).toBeInTheDocument();
    expect(await screen.findAllByText("Нужен ответ")).not.toHaveLength(0);
    await userEvent.click(screen.getByRole("link", { name: "Открыть" }));
    expect(await screen.findByText("Вход в lk.fakeuni.test. Логин")).toBeInTheDocument();
  });

  async function reachApproval() {
    switchScenario("empty", "approval");
    await startHse();
    await userEvent.type(await screen.findByLabelText("Ответ"), "anna");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await userEvent.type(await screen.findByLabelText("Ответ"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /Алгоритмы/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    return screen.findByLabelText("Комментарий");
  }

  it("запись от имени студента: правка поля, хеш показанного, работа идёт дальше", async () => {
    await userEvent.type(await reachApproval(), "Очно");
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(await screen.findByRole("heading", { level: 3, name: "Что подключено" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("«Не делать» отклоняет предложение, работа идёт дальше", async () => {
    await reachApproval();
    await userEvent.click(screen.getByRole("button", { name: "Не делать" }));
    expect(await screen.findByRole("heading", { level: 3, name: "Что подключено" })).toBeInTheDocument();
  });

  it("отмена: подтверждение в Dialog, затем «Подключение отменено»", async () => {
    await startHse();
    await screen.findByText("Вход в lk.fakeuni.test. Логин");
    await userEvent.click(screen.getByRole("button", { name: "Отменить подключение" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Отменить подключение?" });
    expect(within(dialog).getByRole("button", { name: "Продолжить" })).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: "Отменить подключение" }));
    expect(await screen.findByText(/Подключение отменено/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Готово" })).toBeInTheDocument();
  });

  it("второй источник того же вуза идёт в то же пространство", async () => {
    switchScenario("connected");
    renderApp("/connect");
    await choose(/Высшая школа экономики/);
    await userEvent.click(screen.getByRole("button", { name: "Начать подключение" }));
    await screen.findByRole("heading", { level: 1, name: "Подключаю ВШЭ" });
    expect(current.db.spaces.filter((space) => space.universityId === "u-hse")).toHaveLength(1);
  });

  it("свой вуз: пространство создаётся с названием студента", async () => {
    renderApp("/connect");
    await choose(/Другой вуз/);
    await userEvent.type(screen.getByLabelText("Название вуза"), "Политех");
    await userEvent.click(screen.getByRole("button", { name: "Начать подключение" }));
    await screen.findByRole("heading", { level: 1, name: "Подключаю Политех" });
    expect(current.db.spaces.map((space) => space.title)).toContain("Политех");
  });
});
