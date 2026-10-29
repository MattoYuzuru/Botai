import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { Scenario } from "@/mocks/db";
import { createDataServer, NOW } from "@/test/data-server";
import { renderApp } from "@/test/render-app";

let current = createDataServer();

function useScenario(scenario: Scenario) {
  current.server.close();
  current = createDataServer(scenario);
  current.server.listen({ onUnhandledRequest: "error" });
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
beforeEach(() => useScenario("connected"));
afterEach(() => {
  current.server.resetHandlers();
  window.localStorage.clear();
  useActiveSpaceStore.setState({ id: "all" });
  vi.restoreAllMocks();
});
afterAll(() => {
  current.server.close();
  vi.useRealTimers();
});

const hseId = () => (current.db.spaces[0] as { id: string }).id;
const SECRET = "https://my.fakeuni.test/calendar/export.ics?key=very-secret-key-123";

async function openPortal() {
  renderApp(`/study/connections?space=${hseId()}`);
  await userEvent.click(await screen.findByRole("button", { name: /Личный кабинет/ }));
  return screen.findByRole("dialog", { name: "Личный кабинет" });
}

describe("«Источники»: управление", () => {
  it("карточка подключения: статус, когда обновлялось, пароль хранится зашифрованным", async () => {
    const sheet = await openPortal();
    expect(within(sheet).getByText("Работает")).toBeInTheDocument();
    expect(within(sheet).getByText(/Обновлено 5\sминут назад/)).toBeInTheDocument();
    expect(within(sheet).getByText(/сегодня в 17:35/)).toBeInTheDocument();
    expect(within(sheet).getByText(/Сохранён зашифрованным/)).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: /Повторить|Обновить/ })).toBeNull();
  });

  it("ссылка календаря: уходит один раз с нужным видом, значение нигде не показывается после сохранения", async () => {
    const bodies: unknown[] = [];
    current.server.use(
      http.put("*/api/v1/connections/:id/secrets", async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const sheet = await openPortal();
    await userEvent.click(
      within(sheet).getByRole("button", { name: /Добавить ссылку на календарь или ключ/ }),
    );
    const field = await within(sheet).findByLabelText("Ссылка на календарь");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    await userEvent.type(field, SECRET);
    await userEvent.click(within(sheet).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ kind: "ical_url", value: SECRET });
    // Подтверждение — тост без значения; формы со значением больше нет.
    expect(await screen.findByText("Ссылка на календарь сохранена")).toBeInTheDocument();
    await waitFor(() => expect(within(sheet).queryByLabelText("Ссылка на календарь")).toBeNull());
    expect(document.body.textContent).not.toContain("very-secret-key-123");
    expect(
      [...document.querySelectorAll("input")].every((input) => !input.value.includes("very-secret")),
    ).toBe(true);
  });

  it("ссылка календаря без https:// отклоняется у поля словами, на сервер не уходит", async () => {
    const calls: string[] = [];
    current.server.use(
      http.put("*/api/v1/connections/:id/secrets", () => {
        calls.push("put");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const sheet = await openPortal();
    await userEvent.click(
      within(sheet).getByRole("button", { name: /Добавить ссылку на календарь или ключ/ }),
    );
    await userEvent.type(
      await within(sheet).findByLabelText("Ссылка на календарь"),
      "calendar.example.test/x.ics",
    );
    await userEvent.click(within(sheet).getByRole("button", { name: "Сохранить" }));
    expect(
      await within(sheet).findByText(/Нужна ссылка на календарь: https:\/\/ или webcal:\/\//),
    ).toBeInTheDocument();
    expect(calls).toEqual([]);
  });

  it("ключ доступа: выбор вида меняет подпись поля, ошибка сервера показана у поля человеческим текстом", async () => {
    current.server.use(
      http.put(
        "*/api/v1/connections/:id/secrets",
        () =>
          new HttpResponse(
            JSON.stringify({
              code: "validation",
              status: 422,
              detail: "Ключ не подошёл. Проверьте, что он скопирован целиком",
            }),
            { status: 422, headers: { "content-type": "application/problem+json" } },
          ),
      ),
    );
    const sheet = await openPortal();
    await userEvent.click(
      within(sheet).getByRole("button", { name: /Добавить ссылку на календарь или ключ/ }),
    );
    await userEvent.click(within(sheet).getByRole("radio", { name: /Ключ доступа/ }));
    await userEvent.type(await within(sheet).findByLabelText("Ключ доступа"), "abc123");
    await userEvent.click(within(sheet).getByRole("button", { name: "Сохранить" }));
    expect(await within(sheet).findByText(/Ключ не подошёл/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("abc123");
  });

  it("«Забыть пароль»: подтверждение с последствиями, после — пароль не сохранён", async () => {
    const sheet = await openPortal();
    await userEvent.click(within(sheet).getByRole("button", { name: "Забыть пароль" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Забыть пароль?" });
    expect(within(dialog).getByText(/Автоматический вход перестанет работать/)).toBeInTheDocument();
    // Фокус на безопасной «Отмене».
    expect(within(dialog).getByRole("button", { name: "Отмена" })).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: "Забыть пароль" }));
    await waitFor(() =>
      expect(current.db.connections.find((c) => c.title === "Личный кабинет")?.hasPassword).toBe(false),
    );
    expect(await screen.findByText("Пароль удалён")).toBeInTheDocument();
    await waitFor(() => expect(within(sheet).getByText(/Не сохранён/)).toBeInTheDocument());
    expect(within(sheet).queryByRole("button", { name: "Забыть пароль" })).toBeNull();
  });

  it("«Отмена» в подтверждении ничего не удаляет", async () => {
    const sheet = await openPortal();
    await userEvent.click(within(sheet).getByRole("button", { name: "Забыть пароль" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(current.db.connections.find((c) => c.title === "Личный кабинет")?.hasPassword).toBe(true);
  });

  it("удаление подключения: подтверждение, потом подключение исчезает из списка и из базы", async () => {
    const sheet = await openPortal();
    await userEvent.click(within(sheet).getByRole("button", { name: "Удалить подключение" }));
    const dialog = await screen.findByRole("alertdialog", { name: /Удалить «Личный кабинет»\?/ });
    expect(within(dialog).getByText(/Данные этого источника исчезнут/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Удалить подключение" }));
    expect(await screen.findByText("Подключение удалено")).toBeInTheDocument();
    await waitFor(() => expect(current.db.connections.some((c) => c.title === "Личный кабинет")).toBe(false));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Личный кабинет/ })).toBeNull());
  });

  it("нужен повторный вход: в карточке «Войти снова» ведёт в чат, а не ставит вход в очередь", async () => {
    useScenario("reauth");
    const cu = (current.db.spaces[1] as { id: string }).id;
    const { router } = renderApp(`/study/connections?space=${cu}`);
    await userEvent.click(await screen.findByRole("button", { name: /LMS/ }));
    const sheet = await screen.findByRole("dialog", { name: "LMS" });
    await userEvent.click(within(sheet).getByRole("link", { name: "Войти снова" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(router.state.location.search.draft).toBe("Войди снова в «LMS» (Центральный университет)");
  });
});

describe("«Добавить документ»", () => {
  it("ссылка без https:// отклоняется у поля, без запроса", async () => {
    renderApp(`/study/documents?space=${hseId()}`);
    await userEvent.click(await screen.findByRole("button", { name: "Добавить документ" }));
    const sheet = await screen.findByRole("dialog", { name: "Добавить документ" });
    await userEvent.type(within(sheet).getByLabelText("Ссылка"), "ftp://x.test/file");
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить" }));
    expect(await within(sheet).findByText("Нужна ссылка вида https://…")).toBeInTheDocument();
    expect(within(sheet).getByLabelText("Ссылка")).toHaveAttribute("aria-invalid", "true");
  });

  it("ссылка: задание идёт стадиями словами сервера, без процентов, документ появляется в списке", async () => {
    renderApp(`/study/documents?space=${hseId()}`);
    await screen.findByRole("button", { name: /Рейтинг группы/ });
    await userEvent.click(screen.getByRole("button", { name: "Добавить документ" }));
    const sheet = await screen.findByRole("dialog", { name: "Добавить документ" });
    await userEvent.type(within(sheet).getByLabelText("Ссылка"), "https://docs.fakeuni.test/sheets/exams");
    await userEvent.type(within(sheet).getByLabelText(/^Название/), "Расписание экзаменов");
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить" }));
    expect(await within(sheet).findByText("Расписание экзаменов")).toBeInTheDocument();
    await within(sheet).findByText("Читаю документ", {}, { timeout: 6000 });
    expect(document.body.textContent).not.toMatch(/\d\s*%/);
    expect(within(sheet).queryByRole("button", { name: /Повторить|Обновить/ })).toBeNull();
    expect(await within(sheet).findByText(/Документ добавлен/, {}, { timeout: 10_000 })).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Готово" }));
    expect(await screen.findByRole("button", { name: /Расписание экзаменов/ })).toBeInTheDocument();
  }, 30_000);

  it("файл неподходящего типа или больше 8 МБ отклоняется до отправки", async () => {
    renderApp(`/study/documents?space=${hseId()}`);
    await userEvent.click(await screen.findByRole("button", { name: "Добавить документ" }));
    const sheet = await screen.findByRole("dialog", { name: "Добавить документ" });
    await userEvent.click(within(sheet).getByRole("radio", { name: "Файл" }));
    const input = sheet.querySelector("input[type=file]") as HTMLInputElement;
    await userEvent.upload(input, new File(["x"], "virus.exe"), { applyAccept: false });
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить" }));
    expect(await within(sheet).findByText(/Этот тип файла не поддерживается/)).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: "Убрать файл" }));
    await userEvent.upload(input, new File([new ArrayBuffer(9 * 1024 * 1024)], "big.pdf"));
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить" }));
    expect(await within(sheet).findByText(/больше 8\sМБ/)).toBeInTheDocument();
  });

  it("сервер отклонил ссылку — его слова у поля, шит остаётся открытым", async () => {
    current.server.use(
      http.post(
        "*/api/v1/spaces/:id/documents/url",
        () =>
          new HttpResponse(
            JSON.stringify({
              code: "egress_blocked",
              status: 422,
              detail: "Этот сайт нельзя читать: он не входит в разрешённые",
            }),
            { status: 422, headers: { "content-type": "application/problem+json" } },
          ),
      ),
    );
    renderApp(`/study/documents?space=${hseId()}`);
    await userEvent.click(await screen.findByRole("button", { name: "Добавить документ" }));
    const sheet = await screen.findByRole("dialog", { name: "Добавить документ" });
    await userEvent.type(within(sheet).getByLabelText("Ссылка"), "https://blocked.example.test/a");
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить" }));
    expect(await within(sheet).findByText(/Этот сайт нельзя читать/)).toBeInTheDocument();
  });

  it("удаление документа: подтверждение, потом документ исчезает", async () => {
    renderApp(`/study/documents?space=${hseId()}`);
    await userEvent.click(await screen.findByRole("button", { name: /Правила сдачи курсовых работ/ }));
    const sheet = await screen.findByRole("dialog", { name: "Правила сдачи курсовых работ" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Удалить документ" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Удалить документ?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Удалить документ" }));
    expect(await screen.findByText("Документ удалён")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Правила сдачи курсовых работ/ })).toBeNull(),
    );
  });
});
