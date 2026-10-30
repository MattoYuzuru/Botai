import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Choice, Confirm, Input } from "@/api/agent-ui.gen";
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

const questionTurn: Turn = { kind: "block", key: `block:J:${choice.id}`, jobId: "J", block: choice, at: NOW };
const reply = (blockId: string): Turn => ({
  kind: "user",
  key: "server-ack",
  text: "Алгоритмы",
  attachments: [],
  quote: { text: choice.question, blockId },
  at: NOW + 1,
  status: "sent",
});

function view(turns: Turn[], jobs: ThreadJob[]) {
  const timeline: Timeline = { turns, quickReplies: null };
  return render(
    <TimelineView
      timeline={timeline}
      jobs={jobs}
      runtime={undefined}
      threadId="T"
      nowMs={NOW}
      timeZone="Europe/Moscow"
      links={links}
      onQuickReply={() => {}}
      quickRepliesDisabled={false}
      onRestart={() => {}}
      restarting={false}
      onChanged={() => {}}
    />,
  );
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
