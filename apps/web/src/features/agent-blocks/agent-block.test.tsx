import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { groupCitations } from "@/features/chat/citations";
import { parseBlock } from "@/features/jobs/blocks";
import type { AnswerState } from "@/features/jobs/job-state";
import { AgentBlockView } from "./agent-block";
import type { BlockContext } from "./context";
import { validateInput } from "./input-card";

const AT = "2026-10-27T10:00:00Z";
const ID = (n: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(n).padStart(2, "0")}`;
const NOW = Date.parse("2026-10-27T11:05:00Z");

function setup(raw: unknown, answers: Record<string, AnswerState> = {}, extra: Partial<BlockContext> = {}) {
  const ctx: BlockContext = {
    jobId: "J",
    answers,
    nowMs: NOW,
    timeZone: "Europe/Moscow",
    spaceTitle: "ВШЭ",
    onAnswer: vi.fn(),
    onApprove: vi.fn(),
    onReject: vi.fn(),
    ...extra,
  };
  const block = parseBlock(raw);
  if (!block) throw new Error("не блок");
  render(<AgentBlockView block={block} ctx={ctx} />);
  return ctx;
}

const choice = (extra: object = {}) => ({
  type: "choice",
  id: ID(1),
  at: AT,
  question: "Это ваши предметы?",
  mode: "single",
  required: true,
  options: [
    { value: "a", label: "Алгоритмы", hint: "Лекции" },
    { value: "b", label: "Базы данных" },
  ],
  ...extra,
});

describe("choice", () => {
  it("single: RadioGroup, «Ответить» неактивна до выбора и отправляет значение и подпись", async () => {
    const ctx = setup(choice());
    expect(screen.getByText("Жду ответа")).toBeInTheDocument();
    expect(screen.getByText("Без ответа работа не продолжится.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ответить" })).toBeDisabled();
    await userEvent.click(screen.getByText("Базы данных"));
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(1), "b", "Базы данных");
    expect(screen.queryByRole("button", { name: "Не знаю" })).toBeNull();
  });

  it("multi: Checkbox, значения списком, подписи через запятую", async () => {
    const ctx = setup(choice({ mode: "multi" }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Алгоритмы/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /Базы данных/ }));
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(1), ["a", "b"], "Алгоритмы, Базы данных");
  });

  it("allowOther: «Другое» требует текста и отправляет его", async () => {
    const ctx = setup(choice({ allowOther: true }));
    await userEvent.click(screen.getByText("Другое"));
    expect(screen.getByRole("button", { name: "Ответить" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Своё значение"), "Физика");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(1), "Физика", "Физика");
  });

  it("необязательный вопрос: есть «Не знаю», ответ — пустой список (так принимает сервер)", async () => {
    const ctx = setup(choice({ required: false }));
    expect(screen.queryByText("Без ответа работа не продолжится.")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Не знаю" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(1), [], "Не знаю");
  });

  it("отправленный ответ остаётся в истории как сообщение человека", () => {
    setup(choice(), { [ID(1)]: { status: "answered", shown: "Базы данных", error: null } });
    expect(screen.getByText("Это ваши предметы?")).toBeInTheDocument();
    expect(screen.getByText("Базы данных")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ответить" })).toBeNull();
  });

  it("ошибка отправки показана под вопросом, ответить можно снова", () => {
    setup(choice(), { [ID(1)]: { status: "failed", shown: null, error: "Нет связи с BotAI." } });
    expect(screen.getByRole("alert")).toHaveTextContent("Нет связи с BotAI.");
    expect(screen.getByRole("button", { name: "Ответить" })).toBeInTheDocument();
  });
});

const input = (extra: object = {}) => ({
  type: "input",
  id: ID(2),
  at: AT,
  question: "Вход в lk.fakeuni.test. Логин",
  inputKind: "text",
  required: true,
  ...extra,
});

describe("input", () => {
  it.each([
    { autocomplete: "username" },
    { autocomplete: "current-password" },
    { autocomplete: "one-time-code" },
    { inputKind: "password" },
    { inputKind: "code" },
  ])("ручной браузер доступен для auth marker %j с trusted connection context", async (marker) => {
    const onOpen = vi.fn();
    const ctx = setup(
      input(marker),
      {},
      {
        manualLogin: { connectionId: ID(9), onOpen, busy: false, error: null },
      },
    );
    expect(screen.getByText("Вход через поля BotAI")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Открыть браузер" });
    expect(button.closest("form")).toBeNull();
    await userEvent.click(button);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(ID(2));
    expect(ctx.onAnswer).not.toHaveBeenCalled();
  });

  it.each([
    { question: "Какой сайт подключить?", inputKind: "url" },
    { question: "Вход. Логин" },
    { question: "Поиск курсов" },
    { inputKind: "url", autocomplete: "username" },
  ])("browser option не угадывается по тексту и не появляется у generic поля %j", (extra) => {
    setup(
      input(extra),
      {},
      {
        manualLogin: { connectionId: ID(9), onOpen: vi.fn(), busy: false, error: null },
      },
    );
    expect(screen.queryByRole("button", { name: "Открыть браузер" })).toBeNull();
  });

  it("без connection callback auth question остаётся обычным полем", () => {
    setup(input({ autocomplete: "username" }));
    expect(screen.queryByRole("button", { name: "Открыть браузер" })).toBeNull();
  });

  it("переход в браузер не отправляет введённый пароль как ответ", async () => {
    const onOpen = vi.fn();
    const ctx = setup(
      input({ inputKind: "password", secret: true }),
      {},
      {
        manualLogin: { connectionId: ID(9), onOpen, busy: false, error: null },
      },
    );
    await userEvent.type(screen.getByLabelText("Ответ"), "manual-only-secret");
    await userEvent.click(screen.getByRole("button", { name: "Открыть браузер" }));
    expect(onOpen).toHaveBeenCalledExactlyOnceWith(ID(2));
    expect(ctx.onAnswer).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain("manual-only-secret");
  });

  it.each(["answer", "browser"])("busy %s блокирует ответ и переход в браузер", (source) => {
    setup(
      input({ inputKind: "password", secret: true }),
      source === "answer" ? { [ID(2)]: { status: "sending", shown: null, error: null } } : {},
      {
        manualLogin: { connectionId: ID(9), onOpen: vi.fn(), busy: source === "browser", error: null },
      },
    );
    expect(screen.getByLabelText("Ответ")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ответить" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Открыть браузер" })).toBeDisabled();
  });

  it("обычное поле: подпись, 16 px, autocomplete из блока, пустое значение не уходит", async () => {
    const ctx = setup(input({ autocomplete: "username", placeholder: "student@fakeuni.test" }));
    const field = screen.getByLabelText("Ответ");
    expect(field).toHaveAttribute("autocomplete", "username");
    expect(field).toHaveClass("type-body");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(screen.getByText("Нужно заполнить это поле")).toBeInTheDocument();
    expect(ctx.onAnswer).not.toHaveBeenCalled();
    await userEvent.type(field, "  anna  ");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(2), "anna", "anna");
  });

  it("url: проверка ссылки; number: запятая становится точкой", async () => {
    expect(validateInput("url", "javascript:alert(1)", true)).toMatch(/ссылка/);
    expect(validateInput("url", "lk.example.ru", true)).toMatch(/ссылка/);
    expect(validateInput("url", "https://lk.example.ru", true)).toBeNull();
    expect(validateInput("number", "3,5", true)).toBeNull();
    expect(validateInput("number", "три", true)).toBe("Нужно число");
    expect(validateInput("email", "a@b", true)).toMatch(/почта/i);
    expect(validateInput("text", "", false)).toBeNull();
  });

  it("необязательное поле можно пропустить", async () => {
    const ctx = setup(input({ required: false }));
    await userEvent.click(screen.getByRole("button", { name: "Пропустить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(2), "", "Пропущено");
  });

  it("секретное поле: SecretField, пароль уходит без подписи, поле очищается, в истории нет значения", async () => {
    const ctx = setup(
      input({
        question: "Вход. Пароль",
        inputKind: "password",
        secret: true,
        autocomplete: "current-password",
        maxLength: 200,
      }),
    );
    const field = screen.getByLabelText("Ответ") as HTMLInputElement;
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "current-password");
    expect(field).toHaveAttribute("maxlength", "200");
    expect(screen.getByText(/Сохраним зашифрованным/)).toBeInTheDocument();
    await userEvent.type(field, "s3kret!");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(2), "s3kret!", null);
    expect(field.value).toBe("");
    expect(document.body.innerHTML).not.toContain("s3kret!");
  });

  it("любое поле «пароль» секретное, даже если сервер забыл поставить secret", () => {
    setup(input({ question: "Пароль", inputKind: "password" }));
    expect(screen.getByLabelText("Ответ")).toHaveAttribute("type", "password");
  });

  it("код из SMS: цифровая клавиатура, one-time-code, без подписи про шифрование", () => {
    setup(
      input({
        question: "Код из SMS",
        inputKind: "code",
        secret: true,
        autocomplete: "one-time-code",
        maxLength: 6,
      }),
    );
    const field = screen.getByLabelText("Ответ");
    expect(field).toHaveAttribute("inputmode", "numeric");
    expect(field).toHaveAttribute("autocomplete", "one-time-code");
    expect(screen.queryByText(/Сохраним зашифрованным/)).toBeNull();
  });

  it("секретная ссылка (календарь): скрытое поле без автозаполнения паролем, ссылка уходит без подписи", async () => {
    const ctx = setup(
      input({
        question: "Ссылка на экспорт календаря (iCal)",
        inputKind: "url",
        secret: true,
        autocomplete: "url",
        placeholder: "https://… или webcal://…",
        maxLength: 2000,
      }),
    );
    const field = screen.getByLabelText("Ответ") as HTMLInputElement;
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field).toHaveAttribute("maxlength", "2000");
    expect(field).toHaveAttribute("placeholder", "https://… или webcal://…");
    expect(screen.queryByText(/Сохраним зашифрованным/)).toBeNull();
    expect(screen.getByText(/Ссылка хранится зашифрованной/)).toBeInTheDocument();
    await userEvent.type(field, "https://calendar.example.org/export/ics.xml?private_token=abc123");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(
      ID(2),
      "https://calendar.example.org/export/ics.xml?private_token=abc123",
      null,
    );
    expect(field.value).toBe("");
    expect(document.body.innerHTML).not.toContain("private_token=abc123");
  });

  it("отвеченный секрет в истории — «Введено», без значения", () => {
    setup(input({ inputKind: "password", secret: true }), {
      [ID(2)]: { status: "answered", shown: null, error: null },
    });
    expect(screen.getByText("Введено")).toBeInTheDocument();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
  });
});

describe("confirm", () => {
  it("подписи кнопок — из блока, без переименования; ответ true/false", async () => {
    const ctx = setup({
      type: "confirm",
      id: ID(3),
      at: AT,
      question: "Подключить календарь?",
      confirmLabel: "Подключить",
      cancelLabel: "Не нужно",
    });
    await userEvent.click(screen.getByRole("button", { name: "Не нужно" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(3), false, "Не нужно");
    await userEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(ctx.onAnswer).toHaveBeenCalledWith(ID(3), true, "Подключить");
  });
});

describe("summary, text, wait, handoff, unknown", () => {
  it("summary: группы, предположение помечено, источник — ссылкой", () => {
    setup({
      type: "summary",
      id: ID(4),
      at: AT,
      title: "Что собрано",
      groups: [
        {
          title: "Профиль",
          items: [{ label: "Программа", value: "Прикладная математика", confidence: "confirmed" }],
        },
        {
          title: "Сроки",
          items: [
            {
              label: "Задания",
              value: "7 заданий",
              confidence: "guess",
              sourceUrl: "https://fakeuni.test/tasks",
            },
          ],
        },
        { title: "Не подключено", items: [] },
      ],
    });
    expect(screen.getByRole("heading", { name: "Что собрано" })).toBeInTheDocument();
    expect(screen.getByText("Прикладная математика")).toBeInTheDocument();
    expect(screen.getByText("Предположение")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Источник/ })).toHaveAttribute(
      "href",
      "https://fakeuni.test/tasks",
    );
    expect(screen.getByText("Ничего не нашлось.")).toBeInTheDocument();
  });

  it("text: безопасный markdown, таблица, курсив и ссылки; источники сгруппированы по источнику", () => {
    setup(
      {
        type: "text",
        id: ID(5),
        at: AT,
        markdown:
          "Следующая пара — **Алгоритмы** в 11:10, *аудитория 305*. <script>alert(1)</script>\n\n| Время | Предмет |\n|---|---|\n| 11:10 | **Алгоритмы** |\n\nСм. [расписание](https://fakeuni.test/timetable).",
        sources: [
          { title: "Расписание на сегодня", url: "https://fakeuni.test/timetable", fetchedAt: AT },
          { title: "Расписание на неделю", url: "https://fakeuni.test/timetable/week", fetchedAt: AT },
          { title: "Расписание на месяц", url: "https://fakeuni.test/timetable/month", fetchedAt: AT },
        ],
      },
      {},
      {
        groupSources: (sources) =>
          groupCitations(
            sources,
            [{ id: "C1", title: "Личный кабинет", origin: "https://fakeuni.test", spaceTitle: "ВШЭ" }],
            () => null,
          ),
      },
    );
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getAllByText("Алгоритмы").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(screen.getByText("аудитория 305").tagName).toBe("EM");
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "расписание" })).toHaveAttribute(
      "href",
      "https://fakeuni.test/timetable",
    );
    // Три страницы одного подключения — одна запись, подписанная названием источника, а не адресом сайта.
    const sources = screen.getByRole("list", { name: "Источники" });
    expect(within(sources).getAllByRole("listitem")).toHaveLength(1);
    expect(within(sources).getByText("Личный кабинет")).toBeInTheDocument();
    expect(within(sources).getByText("ВШЭ")).toBeInTheDocument();
    expect(within(sources).queryByText("fakeuni.test")).toBeNull();
  });

  it("text: внутренняя ссылка botai://threads/… открывает тред в приложении", async () => {
    const open = vi.fn();
    setup(
      { type: "text", id: ID(5), at: AT, markdown: "Продолжим [в чате подключения](botai://threads/T1)." },
      {},
      {
        resolveLink: (url) =>
          url === "botai://threads/T1" ? { href: "/chat?thread=T1", onOpen: open } : null,
      },
    );
    const link = screen.getByRole("link", { name: "в чате подключения" });
    expect(link).toHaveAttribute("href", "/chat?thread=T1");
    await userEvent.click(link);
    expect(open).toHaveBeenCalledOnce();
  });

  it("text: быстрые ответы — поле блока, а не часть текста", () => {
    setup({ type: "text", id: ID(5), at: AT, markdown: "Готово.", quickReplies: ["Да", "Нет"] });
    expect(screen.getByText("Готово.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Да" })).toBeNull();
  });

  it("plan: шаги со своими состояниями словами", () => {
    setup({
      type: "plan",
      id: ID(5),
      at: AT,
      title: "Как отвечу",
      steps: [
        { id: "a", title: "Собрать пары", state: "done" },
        { id: "b", title: "Сравнить нагрузку", state: "active" },
        { id: "c", title: "Ответить", state: "pending" },
      ],
    });
    const list = screen.getByRole("list", { name: /План|Шаги/ });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText(/выполнено 1 из 3/)).toBeInTheDocument();
  });

  it("attachment: файл открывается во вьюере по нажатию", async () => {
    const ctx = setup(
      {
        type: "attachment",
        id: ID(5),
        at: AT,
        attachmentId: "A1",
        name: "график.pdf",
        mime: "application/pdf",
        size: 2048,
        kind: "document",
      },
      {},
      { openAttachment: vi.fn() },
    );
    await userEvent.click(screen.getByRole("button", { name: /Открыть график\.pdf/ }));
    expect(ctx.openAttachment).toHaveBeenCalledWith("A1");
  });

  it("task: карточка задачи этого чата показывает состояние без ссылки «в другой чат»", () => {
    setup(
      {
        type: "task",
        id: ID(5),
        at: AT,
        jobId: ID(9),
        kind: "connect",
        title: "Подключение ЦУ",
        state: "running",
        threadId: "T1",
      },
      {},
      { threadId: "T1", taskState: () => "waiting_user" },
    );
    expect(screen.getByText("Подключение ЦУ")).toBeInTheDocument();
    expect(screen.getByText("Ждёт вас")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Открыть чат" })).toBeNull();
  });

  it("wait: без кнопок, срок текстом", () => {
    setup({
      type: "wait",
      id: ID(6),
      at: AT,
      message: "Подтвердите вход в приложении на телефоне",
      expiresAt: "2026-10-27T11:07:00Z",
    });
    expect(screen.getByText("Подтвердите вход в приложении на телефоне")).toBeInTheDocument();
    expect(screen.getByText(/Осталось 2/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("handoff: безопасная ссылка в новой вкладке и текст для копирования", () => {
    setup({
      type: "handoff",
      id: ID(7),
      at: AT,
      title: "Заявка",
      url: "https://fakeuni.test/requests/new",
      text: "Прошу выдать справку",
    });
    expect(screen.getByText("Прошу выдать справку")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть сайт вуза/ })).toHaveAttribute("target", "_blank");
  });

  it("неизвестный тип: пометка и текст, без разметки и без исполнения", () => {
    setup({ type: "carousel", id: ID(8), at: AT, markdown: "Смотри <img src=x onerror=alert(1)>" });
    expect(screen.getByText(/Блок неизвестного типа «carousel»/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/Смотри/)).toBeInTheDocument();
    expect(screen.getByText(/Показан как текст/)).toBeInTheDocument();
  });

  it("thinking сам не рисуется: шаги собирает ThinkingStream", () => {
    setup({ type: "thinking", id: ID(9), at: AT, stage: "Читаю" });
    expect(screen.queryByText("Читаю")).toBeNull();
  });
});

describe("approval", () => {
  const raw = {
    type: "approval",
    id: ID(10),
    at: AT,
    proposal: {
      id: ID(11),
      connectionId: ID(12),
      intent: "Записаться на консультацию",
      reversible: true,
      expiresAt: "2026-10-27T11:15:00Z",
      request: {
        method: "POST",
        url: "https://lk.fakeuni.test/consultations",
        bodyKind: "form",
        fields: [
          { name: "course", label: "Предмет", value: "Базы данных", editable: false },
          { name: "comment", label: "Комментарий", value: "", editable: true },
        ],
      },
    },
  };

  it("показывает метод, хост, вуз и поля; подтверждение отдаёт итоговые значения", async () => {
    const ctx = setup(raw);
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(screen.getByText(/lk\.fakeuni\.test/)).toBeInTheDocument();
    expect(screen.getByText("ВШЭ")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Комментарий"), "Нужна очная консультация");
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(ctx.onApprove).toHaveBeenCalledWith(ID(10), ID(11), {
      course: "Базы данных",
      comment: "Нужна очная консультация",
    });
  });

  it("«Не делать» отклоняет предложение", async () => {
    const ctx = setup(raw);
    await userEvent.click(screen.getByRole("button", { name: "Не делать" }));
    expect(ctx.onReject).toHaveBeenCalledWith(ID(10), ID(11));
  });

  it.each([
    ["sending", undefined, "aria-busy"],
    ["answered", "approved", /Подтверждено/],
    ["answered", "rejected", /Не делаю/],
    ["expired", undefined, /Время подтверждения истекло/],
  ] as const)("состояние %s / %s", (status, outcome, expected) => {
    setup(raw, { [ID(10)]: { status, shown: null, error: null, outcome } });
    if (expected === "aria-busy")
      expect(screen.getByRole("button", { name: "Подтвердить" })).toHaveAttribute("aria-busy", "true");
    else expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("ошибка от сервера (например, поля изменились) показана под карточкой", () => {
    setup(raw, {
      [ID(10)]: { status: "failed", shown: null, error: "Поля изменились. Подтвердите заново." },
    });
    expect(
      within(screen.getByRole("alert")).getByText("Поля изменились. Подтвердите заново."),
    ).toBeInTheDocument();
  });
});
