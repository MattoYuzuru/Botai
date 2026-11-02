import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Message } from "@/api/types";
import { initialJob } from "@/features/jobs/job-state";
import { type ChatSuggestion, chatApi } from "./api";
import type { ThreadJob } from "./timeline";
import { suggestionAnchor, useChatSuggestion } from "./use-chat-suggestion";

afterEach(() => vi.restoreAllMocks());
const assistant: Message = {
  id: "M1",
  role: "agent",
  jobId: "J1",
  createdAt: "2026-11-01T10:00:00Z",
  blocks: [{ type: "text", markdown: "Ответ готов" }],
};
const done: ThreadJob = {
  id: "J1",
  kind: "chat",
  connectionId: null,
  snapshot: { ...initialJob("J1"), state: "done" },
};
const ready = { ownerId: "U1", threadId: "T1", anchor: "M1", enabled: true };
function mount(props = ready) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { ...renderHook(useChatSuggestion, { initialProps: props, wrapper }), queryClient };
}

describe("продолжение: якорь ответа", () => {
  it("нужен последний текст агента, а не вопрос человека или промежуточный блок", () => {
    expect(suggestionAnchor([assistant], [done])).toBe("M1");
    expect(suggestionAnchor([assistant, { ...assistant, id: "M2", role: "user" }], [done])).toBeNull();
    expect(suggestionAnchor([{ ...assistant, blocks: [{ type: "thinking" }] }], [done])).toBeNull();
    expect(suggestionAnchor([], [])).toBeNull();
  });
  it("остановка, ошибка, лимит и незавершённое задание не дают якорь", () => {
    for (const state of ["cancelled", "failed", "running"] as const)
      expect(suggestionAnchor([assistant], [{ ...done, snapshot: { ...done.snapshot, state } }])).toBeNull();
    expect(
      suggestionAnchor(
        [assistant],
        [{ ...done, snapshot: { ...done.snapshot, result: { status: "token_limit" } } }],
      ),
    ).toBeNull();
  });
});

describe("продолжение: кэш и поздние ответы", () => {
  it("нет запросов в неподходящем состоянии; ввод и возврат не вызывают модель снова", async () => {
    const api = vi
      .spyOn(chatApi, "suggestion")
      .mockResolvedValue({ text: "Подробнее", afterMessageId: "M1" });
    const app = mount({ ...ready, enabled: false });
    expect(api).not.toHaveBeenCalled();
    app.rerender(ready);
    await waitFor(() => expect(app.result.current).toBe("Подробнее"));
    app.rerender({ ...ready, enabled: false });
    expect(app.result.current).toBeNull();
    app.rerender(ready);
    await waitFor(() => expect(app.result.current).toBe("Подробнее"));
    await app.queryClient.invalidateQueries({ queryKey: ["unrelated"] });
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("поздний ответ не попадает в другой чат или аккаунт и не заменяет ввод", async () => {
    const pending: ((data: ChatSuggestion) => void)[] = [];
    vi.spyOn(chatApi, "suggestion").mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
    const app = mount();
    await waitFor(() => expect(pending).toHaveLength(1));
    app.rerender({ ...ready, threadId: "T2" });
    await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[0]?.({ text: "Из первого чата", afterMessageId: "M1" }));
    expect(app.result.current).toBeNull();
    app.rerender({ ...ready, threadId: "T2", ownerId: "U2" });
    await waitFor(() => expect(pending).toHaveLength(3));
    await act(async () => pending[1]?.({ text: "Первому владельцу", afterMessageId: "M1" }));
    expect(app.result.current).toBeNull();
    app.rerender({ ...ready, threadId: "T2", ownerId: "U2", enabled: false });
    await act(async () => pending[2]?.({ text: "Не заменять ввод", afterMessageId: "M1" }));
    expect(app.result.current).toBeNull();
  });

  it("ошибка сервиса, пустой ответ и чужой якорь не мешают чату и не повторяются", async () => {
    const api = vi.spyOn(chatApi, "suggestion").mockRejectedValue(new Error("offline"));
    const app = mount();
    await waitFor(() => expect(app.queryClient.getQueryCache().findAll()[0]?.state.status).toBe("success"));
    expect(app.result.current).toBeNull();
    app.rerender({ ...ready, enabled: false });
    app.rerender(ready);
    expect(api).toHaveBeenCalledTimes(1);
    api.mockResolvedValueOnce({ text: null, afterMessageId: "M2" });
    app.rerender({ ...ready, anchor: "M2" });
    await waitFor(() => expect(api).toHaveBeenCalledTimes(2));
    expect(app.result.current).toBeNull();
    api.mockResolvedValueOnce({ text: "Чужой ответ", afterMessageId: "wrong" });
    app.rerender({ ...ready, anchor: "M3" });
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3));
    expect(app.result.current).toBeNull();
  });
});
