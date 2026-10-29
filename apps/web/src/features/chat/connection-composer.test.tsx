import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { initialJob } from "@/features/jobs/job-state";
import { sendConnectText, type ThreadRuntime } from "./connection-api";
import { ConnectionComposer } from "./connection-composer";

vi.mock("./connection-api", async (original) => ({
  ...(await original<typeof import("./connection-api")>()),
  sendConnectText: vi.fn(),
}));

it("SSE native до failed ACK сохраняет свободный текст в editor и повторяет тот же client ID", async () => {
  const text = "https://lk.fakeuni.test/";
  const user = userEvent.setup();
  const job = { ...initialJob("source-job"), state: "waiting_user" as const };
  const runtime: ThreadRuntime = {
    purpose: "connect",
    connection: null,
    receipts: [],
    activeJob: {
      id: job.jobId,
      kind: "connect",
      connectionId: null,
      state: "waiting_user",
      stage: "Жду ссылку",
      createdAt: "2026-10-29T10:00:00Z",
      startedAt: null,
      finishedAt: null,
    },
    pending: [{ jobId: job.jobId, blockId: "url-question", channel: "dialogue", secret: false }],
  };
  let reject = (_error: Error) => {};
  vi.mocked(sendConnectText).mockReturnValueOnce(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  const callbacks = {
    onTyping: vi.fn(),
    onSending: vi.fn(() => true),
    onAccepted: vi.fn(() => true),
    onRejected: vi.fn(),
  };
  const client = new QueryClient();
  const view = (state: ThreadRuntime) => (
    <QueryClientProvider client={client}>
      <ConnectionComposer threadId="thread" runtime={state} job={job} {...callbacks} />
    </QueryClientProvider>
  );
  const { rerender } = render(view(runtime));
  const editor = screen.getByRole("textbox", { name: "Сообщение агенту" });
  await user.type(editor, text);
  await user.click(screen.getByRole("button", { name: /^Отправить$/ }));
  const first = vi.mocked(sendConnectText).mock.calls[0];
  expect(first).toBeDefined();
  const protectedState: ThreadRuntime = {
    ...runtime,
    pending: [{ jobId: job.jobId, blockId: "login-question", channel: "native", secret: false }],
  };
  rerender(view(protectedState));
  await act(async () => {
    reject(new Error("Synthetic lost acknowledgement"));
  });
  expect(editor).toBeDisabled();
  expect(editor).toHaveValue(text);
  expect(screen.getByRole("alert")).toBeVisible();
  expect(callbacks.onRejected).toHaveBeenCalledWith(first?.[2]);
  expect(callbacks.onAccepted).not.toHaveBeenCalled();
  // A subsequent runtime refetch must not erase the failed draft either.
  rerender(view({ ...protectedState, pending: protectedState.pending.map((item) => ({ ...item })) }));
  expect(editor).toHaveValue(text);
  rerender(view(runtime));
  expect(editor).toBeEnabled();
  vi.mocked(sendConnectText).mockResolvedValueOnce({
    jobId: job.jobId,
    message: {
      id: "canonical",
      role: "user",
      jobId: null,
      createdAt: "2026-10-29T10:00:00Z",
      blocks: [{ type: "text", markdown: text }],
    },
  });
  await user.click(screen.getByRole("button", { name: /^Отправить$/ }));
  expect(vi.mocked(sendConnectText).mock.calls[1]?.[2]).toBe(first?.[2]);
  expect(editor).toHaveValue("");
});
