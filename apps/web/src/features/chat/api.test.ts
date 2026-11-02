import { afterEach, expect, it, vi } from "vitest";
import { chatApi } from "./api";

afterEach(() => vi.unstubAllGlobals());

it("подсказка привязана к последнему ответу, запрос можно отменить", async () => {
  const fetch = vi.fn(async () => Response.json({ text: "Расскажи подробнее", afterMessageId: "M1" }));
  vi.stubGlobal("fetch", fetch);
  const controller = new AbortController();
  expect(await chatApi.suggestion("T/1", "M1", controller.signal)).toEqual({
    text: "Расскажи подробнее",
    afterMessageId: "M1",
  });
  const [url, init] = (fetch.mock.calls as unknown as [string, RequestInit][])[0] ?? ["", {}];
  expect(String(url)).toContain("/api/v1/chat/threads/T%2F1/suggestion");
  expect(init.method).toBe("POST");
  expect(JSON.parse(String(init.body))).toEqual({ afterMessageId: "M1" });
  expect(init.signal).toBe(controller.signal);
});

it("отметка прочтения называет последнее показанное сообщение, без него — просто «всё прочитано»", async () => {
  const fetch = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetch);
  await chatApi.markRead("T1", "2026-10-28T10:00:00.123456Z");
  await chatApi.markRead("T1");
  const calls = fetch.mock.calls as unknown as [string, RequestInit][];
  expect(calls).toHaveLength(2);
  const [url, first] = calls[0] ?? ["", {}];
  const second = calls[1]?.[1] ?? {};
  expect(String(url)).toContain("/api/v1/chat/threads/T1/read");
  expect(first.method).toBe("POST");
  expect(JSON.parse(String(first.body))).toEqual({ upTo: "2026-10-28T10:00:00.123456Z" });
  expect(second.body).toBeUndefined();
});
