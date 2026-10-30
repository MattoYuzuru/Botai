import { afterEach, expect, it, vi } from "vitest";
import { chatApi } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("отметка прочтения называет последнее показанное сообщение, без него — просто «всё прочитано»", async () => {
  const fetch = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  await chatApi.markRead("T1", "2026-10-28T10:00:00.123456Z");
  await chatApi.markRead("T1");
  const [[firstUrl, first], [, second]] = fetch.mock.calls as unknown as [string, RequestInit][];
  expect(String(firstUrl)).toContain("/api/v1/chat/threads/T1/read");
  expect(first.method).toBe("POST");
  expect(JSON.parse(String(first.body))).toEqual({ upTo: "2026-10-28T10:00:00.123456Z" });
  expect(second.body).toBeUndefined();
});
