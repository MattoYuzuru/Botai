import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createChatDeps, resetChatRuntime } from "./runtime";

beforeEach(() => {
  resetChatRuntime();
  window.localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

it("перед каждой отправкой проверяет живой аккаунт и маркер сброса, включая смену сессии в другой вкладке", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ user: { id: "first-owner" } })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ user: { id: "second-owner" } })));
  vi.stubGlobal("fetch", fetch);
  const deps = createChatDeps();
  expect(await deps.currentUserId()).toBe("first-owner");
  expect(await deps.currentUserId()).toBe("second-owner");
  expect(fetch).toHaveBeenCalledTimes(2);
  for (const [request] of fetch.mock.calls) expect((request as Request).cache).toBe("no-store");
});
