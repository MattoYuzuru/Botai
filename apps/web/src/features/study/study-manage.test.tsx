import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useActiveSpaceStore } from "@/app/active-space";
import type { Scenario } from "@/mocks/db";
import { addConnection, hrefOf } from "@/test/data-fixtures";
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

  describe("отключение, включение и основной источник расписания (FR-054)", () => {
    const portal = () =>
      current.db.connections.find((c) => c.spaceId === hseId() && c.kind === "portal") as {
        id: string;
        status: string;
        scheduleRole?: string | null;
      };

    it("«Отключить источник» просит подтверждения, говорит, что ничего не удаляется, и отключает", async () => {
      const sheet = await openPortal();
      await userEvent.click(within(sheet).getByRole("button", { name: "Отключить источник" }));
      const dialog = await screen.findByRole("alertdialog", { name: "Отключить «Личный кабинет»?" });
      expect(dialog).toHaveTextContent("Ничего не удаляется");
      // что именно скрывается: не только расписание
      expect(dialog).toHaveTextContent(/документы/);
      expect(dialog).toHaveTextContent(/остановит его задачи/);
      expect(portal().status).toBe("active"); // пока не подтвердили — ничего не изменилось
      await userEvent.click(within(dialog).getByRole("button", { name: "Отключить" }));
      await waitFor(() => expect(portal().status).toBe("disabled"));
      expect(await screen.findByText("Источник отключён")).toBeInTheDocument();
    });

    it("отключённый источник включается обратно кнопкой в той же карточке", async () => {
      portal().status = "disabled";
      const sheet = await openPortal();
      await userEvent.click(within(sheet).getByRole("button", { name: "Включить источник" }));
      await waitFor(() => expect(portal().status).toBe("active"));
      expect(await screen.findByText("Источник включён")).toBeInTheDocument();
    });

    it("дополнительный источник расписания можно сделать основным; у основного кнопки нет", async () => {
      addConnection(current.db, hseId(), {
        title: "Календарь",
        kind: "calendar",
        adapter: "ical",
        origin: "https://calendar.fakeuni.test",
        entryUrl: null,
        hasPassword: false,
        scheduleRole: "secondary",
      });
      portal().scheduleRole = "primary";
      const requests: unknown[] = [];
      current.server.use(
        http.put("*/api/v1/spaces/:id/primary-source", async ({ request }) => {
          requests.push(await request.json());
          return new HttpResponse(null, { status: 204 });
        }),
      );
      renderApp(`/study/connections?space=${hseId()}`);
      // в списке у обоих подписана роль: источников с расписанием несколько
      expect(await screen.findByText("Основное расписание")).toBeInTheDocument();
      expect(screen.getByText("Дополнительное расписание")).toBeInTheDocument();
      await userEvent.click(await screen.findByRole("button", { name: /^Календарь/ }));
      const calendar = await screen.findByRole("dialog", { name: "Календарь" });
      expect(within(calendar).getByText(/Дополнительный источник расписания/)).toBeInTheDocument();
      await userEvent.click(within(calendar).getByRole("button", { name: "Сделать основным" }));
      await waitFor(() => expect(requests).toHaveLength(1));
      expect(requests[0]).toEqual({ connectionId: expect.stringMatching(/^[0-9A-Z]{26}$/) });
    });

    it("у единственного источника расписания роль в списке не подписана", async () => {
      portal().scheduleRole = "primary";
      renderApp(`/study/connections?space=${hseId()}`);
      await screen.findByRole("button", { name: /Личный кабинет/ });
      expect(screen.queryByText("Основное расписание")).toBeNull();
    });
  });

  it("в разделе «Источники» у вуза есть «Добавить источник» и его вариант «Календарь (iCal)» — оба в чат", async () => {
    renderApp(`/study/connections?space=${hseId()}`);
    const add = await screen.findByRole("link", { name: "Добавить источник" });
    const calendar = await screen.findByRole("link", { name: "Календарь (iCal)" });
    for (const link of [add, calendar]) expect(hrefOf(link).pathname).toBe("/chat");
    expect(hrefOf(calendar).draft).toContain("Высшая школа экономики");
    expect(hrefOf(calendar).draft).toMatch(/календар/i);
  });

  describe("календарь по ссылке (adapter ical)", () => {
    async function openCalendar(patch: Parameters<typeof addConnection>[2] = {}) {
      addConnection(current.db, hseId(), {
        title: "Календарь",
        kind: "calendar",
        adapter: "ical",
        origin: "https://calendar.fakeuni.test",
        entryUrl: null,
        hasPassword: false,
        ...patch,
      });
      renderApp(`/study/connections?space=${hseId()}`);
      await userEvent.click(await screen.findByRole("button", { name: /^Календарь/ }));
      return screen.findByRole("dialog", { name: "Календарь" });
    }

    it("новую ссылку принимает защищённое поле чата, а не форма в карточке", async () => {
      const sheet = await openCalendar();
      expect(within(sheet).queryByRole("button", { name: /Добавить ключ доступа/ })).toBeNull();
      expect(within(sheet).queryByLabelText("Ссылка на календарь")).toBeNull();
      const replace = within(sheet).getByRole("link", { name: "Ввести новую ссылку" });
      expect(hrefOf(replace).pathname).toBe("/chat");
      expect(hrefOf(replace).draft).toContain("«Календарь»");
      expect(hrefOf(replace).draft).toContain("Высшая школа экономики");
      expect(hrefOf(replace).draft).not.toMatch(/https?:|webcal:|\.ics/);
    });

    it("ссылка умерла: «Нужна новая ссылка» и кнопка «Новая ссылка» вместо «Войти снова»", async () => {
      const sheet = await openCalendar({ status: "needs_reauth" });
      expect(within(sheet).getByText("Нужна новая ссылка")).toBeInTheDocument();
      expect(within(sheet).getByText(/Ссылка на календарь .* перестала работать/)).toBeInTheDocument();
      expect(within(sheet).queryByRole("link", { name: "Войти снова" })).toBeNull();
      const fresh = within(sheet).getByRole("link", { name: "Новая ссылка" });
      expect(hrefOf(fresh).draft).toContain("новой ссылки календаря");
    });
  });

  it("ключ доступа: уходит один раз с нужным видом, значение нигде не показывается после сохранения", async () => {
    const bodies: unknown[] = [];
    current.server.use(
      http.put("*/api/v1/connections/:id/secrets", async ({ request }) => {
        bodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const sheet = await openPortal();
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить ключ доступа" }));
    const field = await within(sheet).findByLabelText("Ключ доступа");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    await userEvent.type(field, "very-secret-key-123");
    await userEvent.click(within(sheet).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ kind: "token", value: "very-secret-key-123" });
    // Подтверждение — тост без значения; формы со значением больше нет.
    expect(await screen.findByText("Ключ доступа сохранён")).toBeInTheDocument();
    await waitFor(() => expect(within(sheet).queryByLabelText("Ключ доступа")).toBeNull());
    expect(document.body.textContent).not.toContain("very-secret-key-123");
    expect(
      [...document.querySelectorAll("input")].every((input) => !input.value.includes("very-secret")),
    ).toBe(true);
  });

  it("в форме ключей ссылки календаря нет: её принимает защищённое поле чата", async () => {
    const sheet = await openPortal();
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить ключ доступа" }));
    expect(within(sheet).queryByRole("radio", { name: /Ссылка на календарь/ })).toBeNull();
    expect(
      within(sheet)
        .getAllByRole("radio")
        .map((radio) => radio.getAttribute("value")),
    ).toEqual(["token", "api_key"]);
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
    await userEvent.click(within(sheet).getByRole("button", { name: "Добавить ключ доступа" }));
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
    expect(router.state.location.search.draft).toContain("LMS");
    expect(router.state.location.search.draft).toContain("Центральный университет");
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
