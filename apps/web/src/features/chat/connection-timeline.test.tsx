import { ThinkingStream } from "@botai/design";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Message } from "@/api/types";
import { AgentBlockView } from "@/features/agent-blocks/agent-block";
import type { BlockContext } from "@/features/agent-blocks/context";
import { type ParsedBlock, parseBlock } from "@/features/jobs/blocks";
import { initialJob } from "@/features/jobs/job-state";
import { RemoteScreen } from "@/features/remote-screen/remote-screen";
import type { ThreadRuntime } from "./connection-api";
import { connectionSnapshot, connectionTurns } from "./connection-timeline";

vi.mock("@/features/remote-screen/remote-screen", () => ({
  RemoteScreen: vi.fn(() => <div>socket opened</div>),
}));
const ID = (number: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(number).padStart(2, "0")}`;
const AT = "2026-10-28T10:00:00Z";
const block = (raw: Record<string, unknown>): ParsedBlock => {
  const parsed = parseBlock({ at: AT, ...raw });
  if (!parsed || parsed.type === "unknown") throw new Error("invalid block fixture");
  return parsed;
};
const question = block({
  type: "input",
  id: ID(2),
  question: "Какие данные нужны?",
  inputKind: "text",
  required: true,
});
const job = { ...initialJob(ID(1)), state: "waiting_user" as const, blocks: [question], lastEventId: "2" };
const runtime: ThreadRuntime = {
  purpose: "connect",
  activeJob: {
    id: ID(1),
    kind: "connect",
    connectionId: null,
    state: "waiting_user",
    stage: "Жду ответ",
    createdAt: AT,
    startedAt: AT,
    finishedAt: null,
  },
  connection: null,
  pending: [{ jobId: ID(1), blockId: ID(2), channel: "dialogue", secret: false }],
  receipts: [],
};
const row = (id: number, value: ParsedBlock, role: "user" | "agent" = "agent"): Message => ({
  id: ID(id),
  role,
  jobId: role === "agent" ? ID(1) : null,
  createdAt: AT,
  blocks: [value as never],
});

describe("плоская source timeline", () => {
  it("исторические шаги ограничены последним durable turn, открытие позже не наращивает elapsed", () => {
    const started = block({ type: "thinking", id: ID(10), stage: "Читаю источник", state: "ok" });
    const end = "2026-10-28T10:00:20Z";
    const reply = block({ type: "text", id: ID(11), at: end, markdown: "Источник прочитан" });
    const turns = connectionTurns([row(12, started), { ...row(13, reply), createdAt: end }], null, {
      ...runtime,
      activeJob: null,
      pending: [],
    });
    const thought = turns.find((turn) => turn.kind === "thinking");
    if (thought?.kind !== "thinking") throw new Error("missing historical thinking");
    expect(thought.finishedAt).toBe(Date.parse(end));
    const view = render(
      <ThinkingStream
        steps={thought.steps}
        finishedAt={thought.finishedAt}
        nowMs={Date.parse(end) + 60_000}
        collapsed
      />,
    );
    const before = screen.getByRole("button").textContent;
    view.rerender(
      <ThinkingStream
        steps={thought.steps}
        finishedAt={thought.finishedAt}
        nowMs={Date.parse(end) + 86_400_000}
        collapsed
      />,
    );
    expect(screen.getByRole("button").textContent).toBe(before);
  });

  it("SSE question сохраняет key после durable upsert и остаётся перед user turn", () => {
    const live = connectionTurns([], job, runtime);
    const reply = block({ type: "text", id: ID(4), markdown: "Нужны оценки, не дедлайны" });
    const persisted = connectionTurns([row(3, question), row(5, reply, "user")], job, runtime);
    const before = live.find((turn) => turn.kind === "block");
    const after = persisted.find((turn) => turn.kind === "block");
    expect(after?.key).toBe(before?.key);
    expect(persisted.filter((turn) => turn.kind === "block")).toHaveLength(1);
    expect(persisted.findIndex((turn) => turn.kind === "block")).toBeLessThan(
      persisted.findIndex((turn) => turn.kind === "user"),
    );
  });

  it("только pending matching Login может заменить durable placeholder, ticket не возрождается в history", () => {
    const id = ID(6);
    const placeholder = block({ type: "text", id, markdown: "Вход через браузер" });
    const login = block({
      type: "login",
      id,
      connectionId: ID(9),
      ticket: "ephemeral-ticket",
      wsUrl: "/api/v1/login-sessions/L/ws",
      expiresAt: AT,
    });
    const current = { ...job, blocks: [login] };
    const pending = {
      ...runtime,
      pending: [{ jobId: job.jobId, blockId: id, channel: "manual" as const, secret: false }],
    };
    const active = connectionTurns([row(7, placeholder)], current, pending).find(
      (turn) => turn.kind === "block",
    );
    expect(active?.kind === "block" && active.block.type).toBe("login");
    const historical = connectionTurns([row(7, placeholder)], current, { ...pending, pending: [] }).find(
      (turn) => turn.kind === "block",
    );
    expect(historical?.kind === "block" && historical.block.type).toBe("text");
    expect(historical?.key).toBe(active?.key);
  });

  it("terminal runtime побеждает устаревший partial SSE, не оставляя диалог на stale CONNECT target", () => {
    const saved = {
      ...runtime,
      pending: [],
      activeJob: runtime.activeJob
        ? { ...runtime.activeJob, state: "done" as const, result: { status: "ok" }, finishedAt: AT }
        : null,
    };
    expect(connectionSnapshot(saved, job)).toMatchObject({ state: "done", result: { status: "ok" } });
  });

  it("исторические auth/choice/approval/login controls readonly и не открывают websocket", () => {
    vi.mocked(RemoteScreen).mockClear();
    const ctx: BlockContext = {
      jobId: job.jobId,
      answers: {},
      nowMs: Date.parse(AT),
      timeZone: "Europe/Moscow",
      readOnly: true,
      onAnswer: vi.fn(),
      onApprove: vi.fn(),
      onReject: vi.fn(),
    };
    const login = block({
      type: "login",
      id: ID(6),
      connectionId: ID(9),
      ticket: "expired-ticket",
      wsUrl: "/api/v1/login-sessions/L/ws",
      expiresAt: AT,
    });
    render(
      <>
        <AgentBlockView block={question} ctx={ctx} />
        <AgentBlockView block={login} ctx={ctx} />
      </>,
    );
    expect(screen.getByText("Какие данные нужны?")).toBeInTheDocument();
    expect(screen.getByText("Вход через браузер")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(RemoteScreen).not.toHaveBeenCalled();
  });
});
