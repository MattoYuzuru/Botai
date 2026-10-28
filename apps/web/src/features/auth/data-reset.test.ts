import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { observeDataReset, onDataReset, onQueueReset } from "./data-reset";

const RESET_AT = 1_793_225_000_000;
const response = (id: string, resetAt = RESET_AT) =>
  new Response(JSON.stringify({ user: { id } }), {
    headers: { "X-Botai-Data-Reset": String(resetAt) },
  });

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("сброс данных аккаунта", () => {
  it("общая отметка другой вкладки не сохраняет старое активное подключение текущей вкладки", async () => {
    localStorage.setItem("botai.data-reset.session-owner", String(RESET_AT));
    sessionStorage.setItem("botai.activeJob", "stale-active-job");
    sessionStorage.setItem("botai.chat.jobs", "stale-job-refs");
    const listener = vi.fn();
    const remove = onDataReset(listener);
    try {
      await observeDataReset(response("session-owner"));
      expect(listener).toHaveBeenCalledOnce();
      expect(sessionStorage.getItem("botai.activeJob")).toBeNull();
      expect(sessionStorage.getItem("botai.chat.jobs")).toBeNull();
      expect(sessionStorage.getItem("botai.data-reset.session-owner")).toBe(String(RESET_AT));
      sessionStorage.setItem("botai.activeJob", "new-job-after-reset");
      await observeDataReset(response("session-owner"));
      expect(sessionStorage.getItem("botai.activeJob")).toBe("new-job-after-reset");
    } finally {
      remove();
    }
  });
  it("уведомляет очередь соседней вкладки и повторно проверяет её при уже сохранённой отметке", async () => {
    const listener = vi.fn();
    const remove = onQueueReset(listener);
    try {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "botai.data-reset.other-tab-owner",
          newValue: String(RESET_AT),
        }),
      );
      expect(listener).toHaveBeenCalledWith({ userId: "other-tab-owner", resetAt: RESET_AT });
      localStorage.setItem("botai.data-reset.other-tab-owner", String(RESET_AT));
      await observeDataReset(response("other-tab-owner"));
      expect(listener).toHaveBeenCalledTimes(2);
    } finally {
      remove();
    }
  });
  it("удаляет старую очередь и снимки владельца; сохраняет новые сообщения, чужую очередь и настройки", async () => {
    localStorage.setItem("botai.snapshot-owner", "reset-owner");
    localStorage.setItem("botai.theme", "dark");
    localStorage.setItem("botai.space", "old-space");
    localStorage.setItem(
      "botai.chat.outbox",
      JSON.stringify([
        { ownerId: "reset-owner", createdAt: RESET_AT - 1 },
        { ownerId: "reset-owner", createdAt: RESET_AT + 1 },
        { ownerId: "other", createdAt: RESET_AT - 1 },
      ]),
    );
    sessionStorage.setItem("botai.chat.jobs", "old-job");
    sessionStorage.setItem("botai.activeJob", "old-active");
    const del = vi.fn(async () => true);
    vi.stubGlobal("caches", { delete: del });
    const listener = vi.fn();
    const remove = onDataReset(listener);
    try {
      await observeDataReset(response("reset-owner"));
      expect(JSON.parse(localStorage.getItem("botai.chat.outbox") ?? "null")).toEqual([
        { ownerId: "reset-owner", createdAt: RESET_AT + 1 },
        { ownerId: "other", createdAt: RESET_AT - 1 },
      ]);
      expect(del).toHaveBeenCalledExactlyOnceWith("botai-api-v1");
      expect(listener).toHaveBeenCalledExactlyOnceWith({ userId: "reset-owner", resetAt: RESET_AT });
      expect(localStorage.getItem("botai.theme")).toBe("dark");
      expect(localStorage.getItem("botai.space")).toBeNull();
      expect(sessionStorage.getItem("botai.chat.jobs")).toBeNull();
      expect(sessionStorage.getItem("botai.activeJob")).toBeNull();
      await observeDataReset(response("reset-owner"));
      expect(del).toHaveBeenCalledTimes(1);
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      remove();
    }
  });

  it("не удаляет снимки другого владельца и не реагирует на неуспешный/непомеченный ответ", async () => {
    localStorage.setItem("botai.snapshot-owner", "someone-else");
    const del = vi.fn(async () => true);
    vi.stubGlobal("caches", { delete: del });
    await observeDataReset(response("isolated-reset-owner"));
    await observeDataReset(new Response("{}"));
    await observeDataReset(
      new Response("{}", { status: 401, headers: { "X-Botai-Data-Reset": String(RESET_AT) } }),
    );
    expect(del).not.toHaveBeenCalled();
  });

  it("параллельные ответы сбрасывают состояние один раз", async () => {
    const del = vi.fn(async () => true);
    vi.stubGlobal("caches", { delete: del });
    await Promise.all([
      observeDataReset(response("parallel-owner")),
      observeDataReset(response("parallel-owner")),
    ]);
    expect(del).toHaveBeenCalledTimes(1);
  });
});
