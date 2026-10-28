import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { parseBlock } from "@/features/jobs/blocks";
import { JobManager } from "@/features/jobs/job-manager";
import { initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { ProgressWall } from "./progress-wall";

vi.mock("@/features/remote-screen/remote-screen", () => ({
  RemoteScreen: () => <section aria-label="Вход через браузер">Вход на сайте вуза</section>,
}));

const ID = (n: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(n).padStart(2, "0")}`;
const AT = "2026-10-28T10:00:00Z";
const block = (raw: unknown) => {
  const parsed = parseBlock(raw);
  if (!parsed) throw new Error("invalid fixture");
  return parsed;
};
const auth = block({
  type: "input",
  id: ID(1),
  at: AT,
  question: "Пароль от кабинета",
  inputKind: "password",
  secret: true,
  required: true,
});

describe("ProgressWall: ручной вход и результат", () => {
  it("только реальный matching LoginBlock убирает старое поле и его секрет из UI без выдуманного ответа", async () => {
    const manager = new JobManager();
    const job: JobSnapshot = { ...initialJob("J"), state: "waiting_user", blocks: [auth] };
    manager.seed(job);
    const open = vi.fn(async (_signal: AbortSignal) => {});
    render(
      <ProgressWall
        jobId="J"
        title="Подключение"
        connectionId={ID(9)}
        onOpenBrowser={open}
        manager={manager}
        onFinish={vi.fn()}
        onMinimize={vi.fn()}
      />,
    );
    await userEvent.type(screen.getByLabelText("Ответ"), "never-send-this");
    await userEvent.click(screen.getByRole("button", { name: "Открыть браузер" }));
    expect(screen.getByRole("button", { name: "Ответить" })).toBeDisabled();
    expect(screen.getByText("Открываю браузер…")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Вход через браузер" })).toBeNull();
    expect(manager.get("J")?.answers).toEqual({});
    act(() =>
      manager.seed({
        ...job,
        blocks: [
          auth,
          block({
            type: "login",
            id: ID(2),
            at: AT,
            connectionId: ID(9),
            ticket: "sse-ticket",
            wsUrl: "/api/v1/login-sessions/L/ws",
            expiresAt: AT,
          }),
        ],
      }),
    );
    expect(screen.getByRole("region", { name: "Вход через браузер" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
    expect(screen.queryByText("Пароль от кабинета")).toBeNull();
    expect(document.body.innerHTML).not.toContain("never-send-this");
    expect(manager.get("J")?.answers).toEqual({});
    act(() => manager.stopAll());
  });

  it("legacy done+error не объявляет успех, не показывает summary и предлагает новый вход", async () => {
    const manager = new JobManager();
    manager.seed({
      ...initialJob("J"),
      state: "done",
      result: { status: "error", detail: "login_failed" },
      blocks: [
        auth,
        block({
          type: "summary",
          id: ID(3),
          at: AT,
          title: "Успешный итог",
          groups: [],
        }),
      ],
    });
    const restart = vi.fn();
    render(
      <ProgressWall
        jobId="J"
        title="Подключение"
        connectionId={ID(9)}
        onLoginAgain={restart}
        onAskInChat={vi.fn()}
        manager={manager}
        onFinish={vi.fn()}
        onMinimize={vi.fn()}
        renderMascot={(state) => <span data-testid="mascot">{state}</span>}
      />,
    );
    expect(screen.getByTestId("mascot")).toHaveTextContent("failed");
    expect(screen.getByText("Подключение не завершено", { exact: true })).toHaveAttribute("role", "status");
    expect(screen.getByText("Подключение не завершено.")).toBeInTheDocument();
    expect(screen.queryByText("Успешный итог")).toBeNull();
    expect(screen.queryByRole("button", { name: "Ответить" })).toBeNull();
    expect(screen.queryByLabelText("Ответ")).toBeNull();
    expect(screen.queryByRole("button", { name: "Спросить в чате" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Войти заново" }));
    expect(restart).toHaveBeenCalledTimes(1);
    act(() => manager.stopAll());
  });
});
