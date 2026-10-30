import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { NBSP } from "../../lib/format";
import { ApprovalCard, type ApprovalProposal } from "../approval-card/approval-card";
import { ChatComposer } from "../chat-composer/chat-composer";
import { ChatMessage } from "../chat-message/chat-message";
import { HandoffCard } from "../handoff-card/handoff-card";
import { parseMarkdown } from "../markdown/parse";
import { SafeMarkdown } from "../markdown/safe-markdown";
import { ProgressRail } from "../progress-rail/progress-rail";
import { SecretField } from "../secret-field/secret-field";
import { ThinkingStream } from "../thinking-stream/thinking-stream";
import { WaitCard } from "../wait-card/wait-card";

const MSK = "Europe/Moscow";
const NOW = Date.UTC(2026, 9, 27, 11, 5);
const plain = (text: string) => text.replaceAll(NBSP, " ");

describe("SafeMarkdown: без HTML и без ссылок-действий", () => {
  it("теги остаются текстом, а не разметкой", () => {
    const { container } = render(
      <SafeMarkdown source={"<script>alert(1)</script> <img src=x onerror=alert(1)> **жирный**"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("<script>alert(1)</script>");
    expect(container.querySelector("strong")).toHaveTextContent("жирный");
  });

  it.each([
    "[нажми](javascript:alert(1))",
    "[нажми](data:text/html;base64,AAAA)",
    "[нажми](/подтвердить)",
    "[нажми](mailto:a@b.test)",
    "[нажми](action:approve)",
  ])("ссылка %s не становится ссылкой", (source) => {
    const { container } = render(<SafeMarkdown source={source} />);
    expect(container.querySelector("a")).toBeNull();
    expect(container).toHaveTextContent("нажми");
  });

  it("http(s)-ссылка открывается в новой вкладке, хост виден рядом, если подпись его скрывает", () => {
    render(<SafeMarkdown source={"[Личный кабинет](https://lk.fakeuni.test/login?x=1)"} />);
    const link = screen.getByRole("link", { name: "Личный кабинет" });
    expect(link).toHaveAttribute("href", "https://lk.fakeuni.test/login?x=1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.getByText(/\(lk\.fakeuni\.test\)/)).toBeInTheDocument();
  });

  it("голый адрес становится ссылкой, адрес в подписи — без дубля хоста", () => {
    render(<SafeMarkdown source={"Смотри https://fakeuni.test/api/v2/timetable. Готово."} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "https://fakeuni.test/api/v2/timetable");
    expect(screen.queryByText(/\(fakeuni\.test\)/)).toBeNull();
  });

  it("картинки не загружаются: остаётся подпись", () => {
    const { container } = render(<SafeMarkdown source="![схема](https://evil.test/pixel.png)" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("схема");
  });

  it("списки, заголовки, код и цитаты", () => {
    const blocks = parseMarkdown(
      "# Итог\n\n- первое\n- второе\n\n1. раз\n2. два\n\n> цитата\n\n```\nкод **не жирный**\n```\n\nОбычный `код` и текст.",
    );
    expect(blocks.map((block) => block.type)).toEqual([
      "heading",
      "list",
      "list",
      "quote",
      "code",
      "paragraph",
    ]);
    const { container } = render(<SafeMarkdown source={"- первое\n- второе\n\n1. раз\n2. два"} />);
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("курсив: *слово* и _слово_ становятся <em>", () => {
    const { container } = render(<SafeMarkdown source="Это *важно* и _тоже_" />);
    expect([...container.querySelectorAll("em")].map((node) => node.textContent)).toEqual(["важно", "тоже"]);
    expect(container).toHaveTextContent("Это важно и тоже");
  });

  it("незакрытая разметка не ломает текст", () => {
    const { container } = render(<SafeMarkdown source="**не закрыто и `тоже" />);
    expect(container).toHaveTextContent("**не закрыто и `тоже");
  });
});

describe("ChatMessage", () => {
  it("«Не уверен»: бейдж перед текстом", () => {
    render(
      <ChatMessage
        role="agent"
        confidence="unsure"
        markdown="Не нашёл расписание на пятницу. Подключите ещё источник."
      />,
    );
    expect(screen.getByText("Не уверен")).toBeInTheDocument();
    const article = screen.getByRole("article");
    expect(article.textContent?.indexOf("Не уверен")).toBeLessThan(
      article.textContent?.indexOf("Не нашёл") ?? 0,
    );
  });

  it("ответ без источников не имитирует источник", () => {
    render(<ChatMessage role="agent" markdown="Ответ без источника" />);
    expect(screen.queryByRole("list", { name: "Источники" })).toBeNull();
  });

  it("источники сгруппированы по источнику, недоступный — с подписью «Источник недоступен»", () => {
    render(
      <ChatMessage
        role="agent"
        markdown="Следующая пара — **Алгоритмы** в 11:10."
        nowMs={NOW}
        timeZone={MSK}
        citationGroups={[
          {
            id: "1",
            label: "Расписание — личный кабинет",
            spaceTitle: "ВШЭ",
            fetchedAt: NOW - 3600_000,
            items: [{ id: "1a", title: "Расписание", url: "https://fakeuni.test/api/v2/timetable" }],
          },
          {
            id: "2",
            label: "Старый портал",
            fetchedAt: NOW - 86_400_000 * 3,
            items: [{ id: "2a", title: "Старый портал", url: null }],
          },
        ]}
      />,
    );
    const list = screen.getByRole("list", { name: "Источники" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Источник недоступен")).toBeInTheDocument();
    expect(within(list).getAllByRole("link")).toHaveLength(1);
  });

  it("queued: «Отправится, когда появится сеть»", () => {
    render(<ChatMessage role="user" markdown="Когда экзамен?" status="queued" />);
    expect(screen.getByText("Отправится, когда появится сеть")).toBeInTheDocument();
  });

  it("streaming: курсор в конце и aria-busy", () => {
    const { container } = render(<ChatMessage role="agent" markdown="Ищу" status="streaming" />);
    expect(container.querySelector("[data-caret]")).not.toBeNull();
    expect(screen.getByRole("article")).toHaveAttribute("aria-busy", "true");
  });

  it("время сообщения в часовом поясе пространства", () => {
    render(<ChatMessage role="user" markdown="Привет" at={NOW} timeZone={MSK} />);
    expect(screen.getByText("14:05")).toBeInTheDocument();
  });

  it("вопрос с вариантами живёт внутри сообщения: Checkbox и Button работают в ChatMessage", async () => {
    const onAnswer = vi.fn();
    render(
      <ChatMessage
        role="agent"
        actions={
          <button type="button" onClick={onAnswer}>
            Ответить
          </button>
        }
      >
        <p>Это твои предметы?</p>
      </ChatMessage>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(onAnswer).toHaveBeenCalled();
  });
});

describe("ChatComposer", () => {
  it("кнопка отправки неактивна, пока поле пусто; отправляет обрезанный текст", async () => {
    const onSend = vi.fn();
    const { rerender } = render(<ChatComposer value="" onValueChange={() => {}} onSend={onSend} />);
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    rerender(<ChatComposer value="  Когда экзамен?  " onValueChange={() => {}} onSend={onSend} />);
    await userEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(onSend).toHaveBeenCalledWith("Когда экзамен?");
  });

  it("поле ввода — 16 px (type-body)", () => {
    render(<ChatComposer value="" onValueChange={() => {}} onSend={() => {}} />);
    expect(screen.getByRole("textbox", { name: "Сообщение агенту" })).toHaveClass("type-body");
  });

  it("Enter отправляет на устройстве с мышью, Shift+Enter переносит строку", async () => {
    const onSend = vi.fn();
    render(<ChatComposer value="Привет" onValueChange={() => {}} onSend={onSend} enterToSend />);
    const field = screen.getByRole("textbox");
    await userEvent.type(field, "{Shift>}{Enter}{/Shift}");
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.type(field, "{Enter}");
    expect(onSend).toHaveBeenCalledWith("Привет");
  });

  it("на телефоне Enter — перенос строки, отправка только кнопкой", async () => {
    const onSend = vi.fn();
    render(<ChatComposer value="Привет" onValueChange={() => {}} onSend={onSend} enterToSend={false} />);
    await userEvent.type(screen.getByRole("textbox"), "{Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("насест Вари стоит на верхней кромке справа и не перехватывает ввод", () => {
    render(
      <ChatComposer
        value=""
        onValueChange={() => {}}
        onSend={() => {}}
        mascotSlot={<span data-testid="varya" />}
      />,
    );
    const perch = screen.getByTestId("varya").closest("[data-perch]");
    expect(perch).toHaveClass("bottom-full", "right-4", "pointer-events-none");
  });

  it("disabled блокирует и поле, и отправку", () => {
    render(<ChatComposer value="x" onValueChange={() => {}} onSend={() => {}} disabled />);
    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  });
});

describe("ThinkingStream", () => {
  const steps = [
    { id: "1", label: "Ищу личный кабинет", state: "done" as const, at: NOW - 12_000 },
    { id: "2", label: "Читаю список курсов", state: "running" as const, at: NOW - 6_000 },
    {
      id: "3",
      label: "Открываю расписание",
      state: "failed" as const,
      detail: "Страница не ответила",
      at: NOW - 4_000,
    },
    { id: "4", label: "Нужен ваш ответ", state: "waiting" as const, at: NOW - 1_000 },
  ];

  it("свёрнут до «Думает · 12 с»", () => {
    render(<ThinkingStream steps={steps} collapsed onToggle={() => {}} nowMs={NOW} />);
    expect(screen.getByRole("button", { name: /^Думает · 12\sс$/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("Ищу личный кабинет")).toBeNull();
  });

  it("развёрнут: упавший шаг — текст danger-fg и причина, ждущий — «Жду ответа» словами", () => {
    render(<ThinkingStream steps={steps} nowMs={NOW} />);
    expect(screen.getByText("Открываю расписание", { exact: false })).toHaveClass("text-danger-fg");
    expect(screen.getByText("Страница не ответила")).toBeInTheDocument();
    expect(screen.getByText("Жду ответа")).toBeInTheDocument();
    expect(
      within(screen.getAllByRole("listitem")[0] as HTMLElement).getByText("Ищу личный кабинет", {
        exact: false,
      }),
    ).toBeInTheDocument();
  });

  it("после завершения заголовок — «Думал», отсчёт останавливается", () => {
    render(
      <ThinkingStream
        steps={steps.map((step) => ({ ...step, state: "done" as const }))}
        nowMs={NOW + 60_000}
        finishedAt={NOW}
        onToggle={() => {}}
        collapsed
      />,
    );
    expect(screen.getByRole("button", { name: /^Думал · 12\sс$/ })).toBeInTheDocument();
  });

  it("переключение вызывает onToggle", async () => {
    const onToggle = vi.fn();
    render(<ThinkingStream steps={steps} nowMs={NOW} onToggle={onToggle} />);
    await userEvent.click(screen.getByRole("button", { expanded: true }));
    expect(onToggle).toHaveBeenCalledWith(true);
  });
});

describe("ProgressRail: стадии, а не проценты", () => {
  it("с известным числом стадий: засечки, номер стадии вместо процента", () => {
    const { container } = render(
      <ProgressRail stage={2} stageCount={5} elapsedMs={72_000} etaKnown={false} />,
    );
    expect(screen.getByRole("img", { name: "Стадия 3 из 5" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/%/);
    expect(screen.getByText(plain(`Идёт 1${NBSP}мин 12${NBSP}с`))).toBeInTheDocument();
    expect(screen.getByText("Сколько осталось — пока неизвестно")).toBeInTheDocument();
  });

  it("без числа стадий: честная неизвестность, засечек нет", () => {
    const { container } = render(
      <ProgressRail stage={0} stageCount={null} elapsedMs={5_000} etaKnown={false} />,
    );
    expect(
      screen.getByRole("img", { name: "Работа идёт, число стадий пока неизвестно" }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("[data-current]")).toHaveLength(0);
    expect(container.textContent).not.toMatch(/%|осталось примерно/i);
  });

  it("оценка остатка показывается, только если её дал сервер", () => {
    render(<ProgressRail stage={1} stageCount={4} elapsedMs={10_000} etaKnown etaMs={120_000} />);
    expect(screen.getByText(plain(`Осталось примерно 2${NBSP}мин`))).toBeInTheDocument();
  });

  it("Варя-бегун ставится на позицию завершённой стадии", () => {
    const { container } = render(
      <ProgressRail
        stage={2}
        stageCount={4}
        elapsedMs={1_000}
        etaKnown={false}
        runner={<span data-testid="varya" />}
      />,
    );
    const runner = container.querySelector<HTMLElement>("[data-rail-runner]");
    expect(runner?.style.left).toBe("50%");
  });
});

describe("SecretField", () => {
  it("значение уходит в onSubmit один раз, поле очищается и снова скрыто", async () => {
    const onSubmit = vi.fn();
    render(<SecretField label="Пароль от личного кабинета" onSubmit={onSubmit} />);
    const input = screen.getByLabelText("Пароль от личного кабинета") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByRole("button", { name: "Ответить" })).toBeDisabled();
    await userEvent.type(input, "секрет123");
    await userEvent.click(screen.getByRole("button", { name: "Показать пароль" }));
    expect(input).toHaveAttribute("type", "text");
    await userEvent.click(screen.getByRole("button", { name: "Ответить" }));
    expect(onSubmit).toHaveBeenCalledWith("секрет123");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(input.value).toBe("");
    expect(input).toHaveAttribute("type", "password");
    expect(document.body.innerHTML).not.toContain("секрет123");
  });

  it("подпись «Сохраним зашифрованным» у пароля, у кода её нет", () => {
    const { rerender } = render(<SecretField label="Пароль" onSubmit={() => {}} />);
    expect(screen.getByText(/Сохраним зашифрованным/)).toBeInTheDocument();
    rerender(<SecretField label="Код из SMS" kind="code" onSubmit={() => {}} />);
    expect(screen.queryByText(/Сохраним зашифрованным/)).toBeNull();
  });

  it("код: one-time-code, цифровая клавиатура, кегль 16 px", () => {
    render(<SecretField label="Код из SMS" kind="code" onSubmit={() => {}} />);
    const input = screen.getByLabelText("Код из SMS");
    expect(input).toHaveAttribute("autocomplete", "one-time-code");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveClass("type-body");
  });

  it("autoComplete берётся из блока", () => {
    render(<SecretField label="Пароль" autoComplete="off" onSubmit={() => {}} />);
    expect(screen.getByLabelText("Пароль")).toHaveAttribute("autocomplete", "off");
  });

  it("ошибка сервера показана под полем", () => {
    render(<SecretField label="Пароль" error="Неверный пароль. Проверьте раскладку." onSubmit={() => {}} />);
    expect(screen.getByLabelText("Пароль")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Неверный пароль. Проверьте раскладку.")).toBeInTheDocument();
  });
});

describe("WaitCard, HandoffCard", () => {
  it("WaitCard: срок виден текстом, кнопок нет", () => {
    render(
      <WaitCard message="Подтвердите вход в приложении на телефоне" expiresAt={NOW + 120_000} nowMs={NOW} />,
    );
    expect(screen.getByText("Подтвердите вход в приложении на телефоне")).toBeInTheDocument();
    expect(screen.getByText(plain(`Осталось 2${NBSP}мин`))).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("WaitCard: истёкшее ожидание названо словами", () => {
    render(<WaitCard message="Подтвердите вход" expiresAt={NOW - 1} nowMs={NOW} />);
    expect(screen.getByText("Время ожидания истекло")).toBeInTheDocument();
  });

  it("HandoffCard: ссылка в новой вкладке с noopener, копирование", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(
      <HandoffCard
        title="Заявка на справку"
        url="https://fakeuni.test/requests/new"
        text="Прошу выдать справку об обучении"
      />,
    );
    const link = screen.getByRole("link", { name: /Открыть сайт вуза/ });
    expect(link).toHaveAttribute("href", "https://fakeuni.test/requests/new");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    await userEvent.click(screen.getByRole("button", { name: "Скопировать" }));
    expect(writeText).toHaveBeenCalledWith("Прошу выдать справку об обучении");
    expect(await screen.findByRole("button", { name: "Скопировано" })).toBeInTheDocument();
  });

  it("HandoffCard: небезопасный адрес не становится ссылкой", () => {
    render(<HandoffCard title="Заявка" url="javascript:alert(1)" text="текст" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Адрес сайта вуза недоступен.")).toBeInTheDocument();
  });
});

describe("ApprovalCard: единственный способ получить согласие на запись", () => {
  const proposal: ApprovalProposal = {
    id: "01JAAAAAAAAAAAAAAAAAAAAAB1",
    intent: "Подать заявку на справку об обучении",
    method: "POST",
    host: "fakeuni.test",
    reversible: false,
    expiresAt: NOW + 10 * 60_000,
    spaceTitle: "ВШЭ",
    fields: [
      { name: "type", label: "Тип справки", value: "Об обучении", editable: false },
      { name: "copies", label: "Количество копий", value: "1", editable: true },
    ],
  };

  it("показывает метод, хост, поля, вуз, срок и необратимость", () => {
    render(
      <ApprovalCard
        proposal={proposal}
        nowMs={NOW}
        timeZone={MSK}
        onApprove={() => {}}
        onReject={() => {}}
      />,
    );
    expect(screen.getByText("Подать заявку на справку об обучении")).toBeInTheDocument();
    expect(screen.getByText("POST")).toBeInTheDocument();
    expect(screen.getByText(/fakeuni\.test/)).toBeInTheDocument();
    expect(screen.getByText("ВШЭ")).toBeInTheDocument();
    expect(screen.getByText("Об обучении")).toBeInTheDocument();
    expect(screen.getByLabelText("Количество копий")).toHaveValue("1");
    expect(screen.getByText("Нельзя отменить")).toBeInTheDocument();
    expect(screen.getByText(/до 27 октября, 14:15/)).toBeInTheDocument();
  });

  it("подтверждение отдаёт итоговые значения редактируемых полей; автоподтверждения нет", async () => {
    const onApprove = vi.fn();
    render(
      <ApprovalCard
        proposal={proposal}
        nowMs={NOW}
        timeZone={MSK}
        onApprove={onApprove}
        onReject={() => {}}
      />,
    );
    expect(onApprove).not.toHaveBeenCalled();
    const copies = screen.getByLabelText("Количество копий");
    await userEvent.clear(copies);
    await userEvent.type(copies, "2");
    await userEvent.click(screen.getByRole("button", { name: "Подтвердить" }));
    expect(onApprove).toHaveBeenCalledWith({ type: "Об обучении", copies: "2" });
  });

  it("«Не делать» отклоняет", async () => {
    const onReject = vi.fn();
    render(<ApprovalCard proposal={proposal} nowMs={NOW} onApprove={() => {}} onReject={onReject} />);
    await userEvent.click(screen.getByRole("button", { name: "Не делать" }));
    expect(onReject).toHaveBeenCalled();
  });

  it("истёкшее предложение теряет кнопки и просит новое", () => {
    render(
      <ApprovalCard proposal={proposal} nowMs={NOW + 11 * 60_000} onApprove={() => {}} onReject={() => {}} />,
    );
    expect(screen.getByText(/Время подтверждения истекло/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подтвердить" })).toBeNull();
  });

  it.each([
    ["approved", /Подтверждено/],
    ["rejected", /Не делаю/],
    ["expired", /Время подтверждения истекло/],
  ] as const)("состояние %s", (state, text) => {
    render(
      <ApprovalCard proposal={proposal} state={state} nowMs={NOW} onApprove={() => {}} onReject={() => {}} />,
    );
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подтвердить" })).toBeNull();
  });

  it("pending: кнопка подтверждения в загрузке, отклонить нельзя", () => {
    render(
      <ApprovalCard
        proposal={proposal}
        state="pending"
        nowMs={NOW}
        onApprove={() => {}}
        onReject={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Подтвердить" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Не делать" })).toBeDisabled();
  });
});
