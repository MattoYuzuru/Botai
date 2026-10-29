import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Message } from "@/api/types";
import { observeDataReset } from "@/features/auth/data-reset";
import { useConnectionEchoes } from "./use-connection-echoes";

const canonical: Message = {
  id: "canonical-message",
  role: "user",
  jobId: null,
  createdAt: "2026-10-29T10:00:00Z",
  blocks: [{ type: "text", markdown: "canonical text" }],
};

describe("эфемерный CONNECT echo", () => {
  it("GET до ACK не удваивает echo; только canonical ID завершает передачу", () => {
    const { result } = renderHook(() => useConnectionEchoes("owner", "thread"));
    act(() => {
      expect(result.current.start("client", "only-in-source-echo", ["question"], [])).toBe(true);
    });
    expect(result.current.messages([canonical])).toEqual([]);
    act(() => {
      expect(result.current.accept("client", canonical)).toBe(true);
    });
    expect(result.current.messages([canonical])).toEqual([canonical]);
    expect(result.current.echoes[0]?.message?.id).toBe(canonical.id);
    expect(JSON.stringify(localStorage)).not.toContain("only-in-source-echo");
    expect(JSON.stringify(sessionStorage)).not.toContain("only-in-source-echo");
  });

  it("отказ удаляет echo, а одинаковые повторные сообщения остаются разными попытками", () => {
    const { result } = renderHook(() => useConnectionEchoes("owner", "thread"));
    act(() => {
      result.current.start("first", "same text", [], []);
      result.current.reject("first");
    });
    expect(result.current.echoes).toEqual([]);
    act(() => {
      result.current.start("second", "same text", [], []);
      result.current.accept("second", canonical);
      result.current.start("third", "same text", ["message:second"], []);
    });
    expect(result.current.echoes.map((echo) => echo.id)).toEqual(["second", "third"]);
  });

  it("смена thread/owner и unmount отвергают поздний ACK старого запроса", () => {
    const { result, rerender, unmount } = renderHook(
      ({ owner, thread }) => useConnectionEchoes(owner, thread),
      { initialProps: { owner: "first-owner", thread: "first-thread" } },
    );
    act(() => {
      result.current.start("first", "private first text", [], []);
    });
    const oldAccept = result.current.accept;
    rerender({ owner: "first-owner", thread: "second-thread" });
    expect(result.current.echoes).toEqual([]);
    expect(oldAccept("first", canonical)).toBe(false);
    act(() => {
      result.current.start("second", "private second text", [], []);
    });
    rerender({ owner: "second-owner", thread: "second-thread" });
    expect(result.current.echoes).toEqual([]);
    act(() => {
      result.current.start("third", "private third text", [], []);
    });
    const late = result.current.accept;
    unmount();
    expect(late("third", canonical)).toBe(false);
  });

  it("account reset убирает старый echo и блокирует поздний ACK, новый ввод разрешён", async () => {
    const owner = "connection-echo-reset-owner";
    const { result } = renderHook(() => useConnectionEchoes(owner, "thread"));
    act(() => {
      result.current.start("before-reset", "old text", [], []);
    });
    await act(async () => {
      await observeDataReset(
        new Response(JSON.stringify({ user: { id: owner } }), {
          headers: { "X-Botai-Data-Reset": "1000000000000", "Content-Type": "application/json" },
        }),
      );
    });
    expect(result.current.echoes).toEqual([]);
    expect(result.current.accept("before-reset", canonical)).toBe(false);
    expect(result.current.generation).toBe(1);
    act(() => {
      expect(result.current.start("after-reset", "new text", [], [])).toBe(true);
    });
    await act(async () => {
      await observeDataReset(
        new Response(JSON.stringify({ user: { id: owner } }), {
          headers: { "X-Botai-Data-Reset": "1000000000000", "Content-Type": "application/json" },
        }),
      );
    });
    expect(result.current.echoes.map((echo) => echo.id)).toEqual(["after-reset"]);
    expect(result.current.generation).toBe(1);
  });
});
