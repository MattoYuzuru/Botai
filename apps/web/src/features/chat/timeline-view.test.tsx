import type { CitationGroup } from "@botai/design";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Choice, Confirm, Input, Text } from "@/api/agent-ui.gen";
import { initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { answerMark } from "./answer-mark";
import type { ChatLinks } from "./chat-links";
import type { ThreadJob, Timeline, Turn } from "./timeline";
import { TimelineView } from "./timeline-view";

const NOW = Date.UTC(2026, 9, 27, 11, 5);
const AT = new Date(NOW).toISOString();
const links: ChatLinks = {
  resolveInternal: () => null,
  resolveLink: () => null,
  groupSources: () => [],
  openAttachment: () => {},
};

const choice: Choice = {
  type: "choice",
  id: "01JMKCDB000000000000000001",
  at: AT,
  question: "Это ваши предметы?",
  mode: "single",
  required: true,
  options: [
    { value: "a", label: "Алгоритмы" },
    { value: "b", label: "Базы данных" },
  ],
};

function job(answers: JobSnapshot["answers"]): ThreadJob {
  return {
    id: "J",
    kind: "connect",
    connectionId: null,
    snapshot: { ...initialJob("J"), state: "running", blocks: [choice], answers },
  };
}

const questionTurn: Turn = {
  kind: "block",
  key: `block:J:${choice.id}`,
  jobId: "J",
  block: choice,
  at: NOW,
  live: false,
};
const reply = (blockId: string): Turn => ({
  kind: "user",
  key: "server-ack",
  text: "Алгоритмы",
  attachments: [],
  quote: { text: choice.question, blockId },
  at: NOW + 1,
  status: "sent",
});

function timelineTree(
  turns: Turn[],
  jobs: ThreadJob[] = [],
  overrides: Partial<ChatLinks> = {},
  quickReplies: Timeline["quickReplies"] = null,
) {
  const timeline: Timeline = { turns, quickReplies };
  return (
    <TimelineView
      timeline={timeline}
      jobs={jobs}
      runtime={undefined}
      threadId="T"
      nowMs={NOW}
      timeZone="Europe/Moscow"
      links={{ ...links, ...overrides }}
      onQuickReply={() => {}}
      quickRepliesDisabled={false}
      onRestart={() => {}}
      restarting={false}
      onChanged={() => {}}
    />
  );
}

function view(turns: Turn[], jobs: ThreadJob[]) {
  return render(timelineTree(turns, jobs));
}

const userBubbles = (root: HTMLElement) => [...root.querySelectorAll("[data-role='user']")];

describe("ответ на вопрос задачи в ленте", () => {
  it("пока сервер не прислал своё сообщение, под вопросом стоит отметка ответа с цитатой — провала нет", () => {
    const { container } = view(
      [questionTurn],
      [job({ [choice.id]: { status: "answered", shown: "Алгоритмы", error: null } })],
    );
    const [mark, ...rest] = userBubbles(container);
    expect(rest).toHaveLength(0);
    expect(mark).toHaveTextContent("Алгоритмы");
    expect(mark?.querySelector("[data-message-quote]")).toHaveTextContent(choice.question);
  });

  it("пришло сообщение сервера с цитатой этого вопроса — отметка уходит, ответ показан один раз", () => {
    const { container } = view(
      [questionTurn, reply(choice.id)],
      [job({ [choice.id]: { status: "answered", shown: "Алгоритмы", error: null } })],
    );
    const bubbles = userBubbles(container);
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]?.querySelector("[data-message-quote]")).toHaveTextContent(choice.question);
    expect(screen.getAllByText("Алгоритмы", { selector: "p" })).toHaveLength(1);
  });

  it("вопрос без ответа отметки не имеет", () => {
    const { container } = view([questionTurn], [job({})]);
    expect(userBubbles(container)).toHaveLength(0);
  });
});

describe("отметка ответа: то же, что запишет сервер", () => {
  const input = (patch: Partial<Input>): Input => ({
    type: "input",
    id: "01JMKCDB000000000000000002",
    at: AT,
    question: "Поле",
    inputKind: "text",
    required: true,
    ...patch,
  });
  const confirm: Confirm = {
    type: "confirm",
    id: "01JMKCDB000000000000000003",
    at: AT,
    question: "Всё верно?",
    confirmLabel: "Да, всё верно",
    cancelLabel: "Нет, не то",
  };

  it("выбор и согласие — слова варианта", () => {
    expect(answerMark(choice, "Алгоритмы, Базы данных")).toBe("Алгоритмы, Базы данных");
    expect(answerMark(confirm, "Да, всё верно")).toBe("Да, всё верно");
    expect(answerMark(choice, null)).toBe("Ответ передан");
  });

  it("пароль, код, логин и любое поле входа — без значения", () => {
    expect(answerMark(input({ inputKind: "password", secret: true }), null)).toBe("Пароль передан");
    expect(answerMark(input({ inputKind: "code", secret: true }), null)).toBe("Код передан");
    expect(answerMark(input({ autocomplete: "username" }), "anna")).toBe("Логин передан");
    // поле входа с любым видом и без особого autocomplete: номер студенческого, дата рождения
    expect(answerMark(input({ login: true, autocomplete: "off" }), "СТ-2026-0042")).toBe("Ответ передан");
    expect(answerMark(input({ login: true, inputKind: "number" }), "42")).toBe("Ответ передан");
  });

  it("обычное несекретное поле показывает введённое", () => {
    expect(answerMark(input({}), "https://example.test")).toBe("https://example.test");
  });
});

/** Кадры rAF под ручным управлением: анимация не зависит от реального времени и `waitFor`. */
function installFrames() {
  const original = { raf: window.requestAnimationFrame, caf: window.cancelAnimationFrame };
  const queue = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  let now = 0;
  const raf = vi.fn((callback: FrameRequestCallback) => {
    const id = nextId++;
    queue.set(id, callback);
    return id;
  });
  const caf = vi.fn((id: number) => void queue.delete(id));
  window.requestAnimationFrame = raf as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = caf as typeof window.cancelAnimationFrame;
  return {
    raf,
    caf,
    /** Выполнить накопленные кадры, сдвинув часы анимации на `ms`. */
    step(ms: number) {
      now += ms;
      const callbacks = [...queue.values()];
      queue.clear();
      for (const callback of callbacks) callback(now);
    },
    pending: () => queue.size,
    restore() {
      window.requestAnimationFrame = original.raf;
      window.cancelAnimationFrame = original.caf;
    },
  };
}

/** Движение включено: jsdom-заглушка `matchMedia` отвечает «нет», но тест не должен зависеть от окружения. */
function enableMotion() {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

const restores: (() => void)[] = [];
afterEach(() => {
  for (const restore of restores.splice(0)) restore();
  window.localStorage.clear();
});

function textTurn(markdown: string, live: boolean, extra: Partial<Text> = {}): Turn {
  const block: Text = { type: "text", id: "01JMKCDB000000000000000009", at: AT, markdown, ...extra };
  return { kind: "block", key: `block:J:${block.id}`, jobId: "J", block, at: NOW, live };
}

const LIVE_TEXT = "Сегодня три пары: алгоритмы, базы данных и физика. Первая начинается в 9:30.";

const agentOf = (root: HTMLElement) => root.querySelector<HTMLElement>("[data-role='agent']") as HTMLElement;
const shownText = (root: HTMLElement) => root.querySelector("[data-safe-markdown]")?.textContent ?? "";

describe("печать ответа агента", () => {
  it("живой блок печатается прогрессивно: streaming с курсором, в конце — полный текст и sent", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const markdown = `${LIVE_TEXT}\n\nЕщё быстрые ответы.`;
    const turn = textTurn(markdown, true, { quickReplies: ["Что дальше?"] });
    const { container } = render(
      timelineTree([turn], [], {}, { key: turn.key, options: ["Что дальше?"], quote: null }),
    );

    // Ответ пришёл, но текста ещё нет: сообщение сразу помечено streaming и aria-busy (тело — как только
    // появятся первые символы; пустой markdown дизайн-система не рисует).
    expect(shownText(container)).toBe("");
    expect(agentOf(container)).toHaveAttribute("data-status", "streaming");
    expect(agentOf(container)).toHaveAttribute("aria-busy", "true");
    // Быстрые ответы — поле блока, а не текст: печать их не задерживает.
    expect(screen.getByRole("group", { name: "Быстрые ответы" })).toBeInTheDocument();

    act(() => frames.step(16));
    act(() => frames.step(120));
    const partial = shownText(container);
    expect(partial.length).toBeGreaterThan(0);
    expect(partial.length).toBeLessThan(markdown.length);
    expect(markdown.startsWith(partial)).toBe(true);
    expect(agentOf(container).querySelector("[data-caret]")).not.toBeNull();
    expect(agentOf(container)).toHaveAttribute("data-status", "streaming");

    act(() => frames.step(6000));
    // textContent не содержит переводов строк: абзацы — отдельные элементы.
    expect(shownText(container)).toBe(markdown.replaceAll("\n", ""));
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(agentOf(container)).toHaveAttribute("data-status", "sent");
    expect(agentOf(container)).not.toHaveAttribute("aria-busy");
  });

  it("историю при открытии чата не печатаем: сразу весь текст, без курсора и без кадров", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const { container } = view([textTurn(LIVE_TEXT, false)], []);
    expect(shownText(container)).toBe(LIVE_TEXT);
    expect(agentOf(container)).toHaveAttribute("data-status", "sent");
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(frames.raf).not.toHaveBeenCalled();
  });

  it("спокойный режим из настроек: сразу весь текст, без кадров, даже если система движение разрешает", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    window.localStorage.setItem("botai.motion", "calm");
    const { container } = view([textTurn(LIVE_TEXT, true)], []);
    expect(shownText(container)).toBe(LIVE_TEXT);
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(frames.raf).not.toHaveBeenCalled();
  });

  it("клик по сообщению во время печати показывает всё сразу", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const { container } = view([textTurn(LIVE_TEXT, true)], []);
    act(() => frames.step(16));
    act(() => frames.step(120));
    expect(shownText(container).length).toBeGreaterThan(0);
    expect(shownText(container).length).toBeLessThan(LIVE_TEXT.length);

    fireEvent.click(agentOf(container));
    expect(shownText(container)).toBe(LIVE_TEXT);
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(agentOf(container)).toHaveAttribute("data-status", "sent");
    expect(frames.pending()).toBe(0);
  });

  it("тот же ключ из истории не перезапускает печать: текст продолжает расти, а не прыгает", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const rendered = render(timelineTree([textTurn(LIVE_TEXT, true)]));
    const { container } = rendered;
    act(() => frames.step(16));
    act(() => frames.step(120));
    const partial = shownText(container);
    expect(partial.length).toBeGreaterThan(0);
    expect(partial.length).toBeLessThan(LIVE_TEXT.length);

    // Ответ сохранили: тот же блок с тем же ключом приходит из истории, признак «живой» снят.
    rendered.rerender(timelineTree([textTurn(LIVE_TEXT, false)]));
    expect(shownText(container)).toBe(partial);
    expect(agentOf(container).querySelector("[data-caret]")).not.toBeNull();

    act(() => frames.step(6000));
    expect(shownText(container)).toBe(LIVE_TEXT);
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(agentOf(container)).toHaveAttribute("data-status", "sent");
  });

  it("размонтирование отменяет печать: кадры не остаются", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const { unmount } = view([textTurn(LIVE_TEXT, true)], []);
    act(() => frames.step(16));
    expect(frames.pending()).toBe(1);

    unmount();
    expect(frames.caf).toHaveBeenCalled();
    expect(frames.pending()).toBe(0);
    // После размонтирования исполнять нечего: ни кадров, ни setState.
    act(() => frames.step(1000));
  });

  it("недописанный markdown не роняет рендер: **, незакрытая таблица и ```chart по пути печати", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const markdown = [
      "**Незакрытый жирный",
      "",
      "| Время | Предмет |",
      "| --- | --- |",
      "| 9:30 | Алгоритмы |",
      "",
      "```chart",
      '{"type":"bar","labels":["Пн","Вт"],"series":[{"name":"Пары","values":[3,2]}]}',
      "```",
    ].join("\n");
    const { container } = view([textTurn(markdown, true)], []);
    // Идём по префиксам текста, включая недописанные жирный, таблицу и fenced-блок: ни один префикс не падает.
    for (let frame = 0; frame < 120; frame++) act(() => frames.step(40));
    // Конец печати: курсор ушёл, недописанный до конца «**» остался текстом, таблица и график разобрались.
    expect(agentOf(container).querySelector("[data-caret]")).toBeNull();
    expect(agentOf(container)).toHaveAttribute("data-status", "sent");
    expect(shownText(container)).toContain("**Незакрытый жирный");
    expect(container.querySelector("[data-md-table]")).not.toBeNull();
    expect(screen.getByRole("figure")).toBeInTheDocument();
  });

  it("уверенность считается от полного текста: «Не уверен» виден с первого кадра, а не после печати", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const markdown = "Не знаю: в расписании пока нет данных.";
    const { container } = view([textTurn(markdown, true)], []);
    expect(screen.getByText("Не уверен")).toBeInTheDocument();
    expect(shownText(container)).toBe("");

    act(() => frames.step(16));
    act(() => frames.step(60));
    expect(shownText(container)).not.toBe("");
    expect(screen.getByText("Не уверен")).toBeInTheDocument();

    act(() => frames.step(6000));
    expect(shownText(container)).toBe(markdown);
  });

  it("цитаты рисуются по полному блоку, пока текст ещё печатается", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const groups: CitationGroup[] = [
      {
        id: "C",
        label: "Личный кабинет",
        fetchedAt: NOW - 60_000,
        items: [{ id: "I", title: "Расписание", url: "https://fakeuni.test/schedule" }],
      },
    ];
    const markdown = "Ответ с источником, который ещё печатается.";
    const sources = [{ title: "Расписание", url: "https://fakeuni.test/schedule", fetchedAt: AT }];
    const { container } = render(
      timelineTree([textTurn(markdown, true, { sources })], [], { groupSources: () => groups }),
    );
    expect(screen.getByRole("list", { name: "Источники" })).toBeInTheDocument();

    act(() => frames.step(16));
    act(() => frames.step(60));
    expect(shownText(container).length).toBeLessThan(markdown.length);
    expect(screen.getByRole("list", { name: "Источники" })).toBeInTheDocument();
  });

  it("state обновляется не чаще 30 раз в секунду: длинный ответ не перерисовывается на каждый кадр", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const markdown = "Длинный ответ. ".repeat(40);
    const { container } = view([textTurn(markdown, true)], []);
    const lengths = new Set<number>();
    // 60 кадров по 8 мс — 480 мс; при 30 fps это не больше ~15 обновлений, а не 60.
    for (let frame = 0; frame < 60; frame++) {
      act(() => frames.step(8));
      lengths.add(shownText(container).length);
    }
    expect(lengths.size).toBeGreaterThan(2);
    expect(lengths.size).toBeLessThanOrEqual(16);
    expect(shownText(container).length).toBeGreaterThan(0);
    expect(shownText(container).length).toBeLessThan(markdown.length);
  });

  it("печатает по code points: эмодзи не разрываются на половинки", () => {
    const frames = installFrames();
    restores.push(frames.restore, enableMotion());
    const markdown = "Итог 🙂🙂🙂 и всё";
    const { container } = view([textTurn(markdown, true)], []);
    const parts: string[] = [];
    for (let frame = 0; frame < 40; frame++) {
      act(() => frames.step(16));
      parts.push(shownText(container));
    }
    for (const part of parts) {
      expect(markdown.startsWith(part)).toBe(true);
      // Ни одиночного старшего суррогата, ни одиночного младшего: половинки эмодзи не печатаются.
      expect(part).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
      expect(part).not.toMatch(/(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/);
    }
    expect(shownText(container)).toBe(markdown);
  });
});
