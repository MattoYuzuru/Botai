import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/api/errors";
import { parseBlock } from "@/features/jobs/blocks";
import { initialJob, type JobSnapshot } from "@/features/jobs/job-state";
import { MANUAL_LOGIN_DEADLINE_MS, useManualLogin } from "./use-manual-login";

const ID = (n: number) => `01JAAAAAAAAAAAAAAAAAAAAA${String(n).padStart(2, "0")}`;
const AT = "2026-10-28T10:00:00Z";
const auth = parseBlock({
  type: "input",
  id: ID(1),
  at: AT,
  question: "Пароль",
  inputKind: "password",
  secret: true,
  required: true,
});
if (!auth) throw new Error("invalid fixture");

const waiting = (): JobSnapshot => ({ ...initialJob("J"), state: "waiting_user", blocks: [auth] });
const screen = (id: number, connectionId = ID(9)) => {
  const block = parseBlock({
    type: "login",
    id: ID(id),
    at: AT,
    connectionId,
    ticket: "one-time-ticket",
    wsUrl: "/api/v1/login-sessions/L/ws",
    expiresAt: AT,
  });
  if (!block) throw new Error("invalid fixture");
  return block;
};

afterEach(() => vi.useRealTimers());

describe("переход с native auth question в браузер", () => {
  it("HTTP success ещё не открывает экран; deadline освобождает обе кнопки и допускает новую попытку", async () => {
    vi.useFakeTimers();
    const open = vi.fn(async (_signal: AbortSignal) => {});
    const { result } = renderHook(() => useManualLogin("J", waiting(), ID(9), open));
    await act(async () => result.current.onOpen(ID(1)));
    expect(result.current.busy).toBe(true);
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0]).toHaveLength(1);
    act(() => result.current.onOpen(ID(1)));
    expect(open).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(MANUAL_LOGIN_DEADLINE_MS));
    expect(result.current.busy).toBe(false);
    expect(result.current.error).toMatch(/Браузер пока не открылся/);
    expect(open.mock.calls[0]?.[0].aborted).toBe(true);
    await act(async () => result.current.onOpen(ID(1)));
    expect(open).toHaveBeenCalledTimes(2);
  });

  it("handoff принимает только новый matching SSE экран после вопроса, включая SSE до HTTP response", async () => {
    const open = vi.fn((_signal: AbortSignal) => new Promise<void>(() => {}));
    const old = screen(2);
    const job = { ...waiting(), blocks: [old, auth] };
    const { result, rerender } = renderHook(({ current }) => useManualLogin("J", current, ID(9), open), {
      initialProps: { current: job },
    });
    act(() => result.current.onOpen(ID(1)));
    rerender({ current: { ...job, blocks: [...job.blocks, screen(3, ID(8))] } });
    expect(result.current.busy).toBe(true);
    rerender({ current: { ...job, blocks: [...job.blocks, old] } });
    expect(result.current.busy).toBe(true);
    rerender({ current: { ...job, blocks: [...job.blocks, screen(4)] } });
    expect(result.current.busy).toBe(false);
    expect(result.current.error).toBeNull();
    expect(open.mock.calls[0]?.[0].aborted).toBe(true);
  });

  it("404 виден человеку, позволяет открыть снова и не хранит API response", async () => {
    const open = vi.fn(async (_signal: AbortSignal) => {
      throw new ApiError(404, "not_found", "");
    });
    const { result } = renderHook(() => useManualLogin("J", waiting(), ID(9), open));
    await act(async () => result.current.onOpen(ID(1)));
    expect(result.current.busy).toBe(false);
    expect(result.current.error).toMatch(/Браузер пока недоступен/);
    await act(async () => result.current.onOpen(ID(1)));
    expect(open).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(result.current)).not.toContain("ticket");
  });

  it("отмена при уходе abort-ит HTTP и deadline; generic и sending вопросы не вызывают API", () => {
    vi.useFakeTimers();
    const open = vi.fn((_signal: AbortSignal) => new Promise<void>(() => {}));
    const job = waiting();
    job.answers[ID(1)] = { status: "sending", shown: null, error: null };
    const { result, rerender, unmount } = renderHook(
      ({ current }) => useManualLogin("J", current, ID(9), open),
      {
        initialProps: { current: job },
      },
    );
    act(() => result.current.onOpen(ID(1)));
    expect(open).not.toHaveBeenCalled();
    rerender({ current: waiting() });
    act(() => result.current.onOpen(ID(1)));
    const signal = open.mock.calls[0]?.[0];
    unmount();
    expect(signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
