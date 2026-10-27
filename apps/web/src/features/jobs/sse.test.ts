import { describe, expect, it, vi } from "vitest";
import { createSseParser, readSse, reconnectDelay, type SseHttpError, type SseMessage } from "./sse";

function parse(chunks: string[]): SseMessage[] {
  const out: SseMessage[] = [];
  const parser = createSseParser((message) => out.push(message));
  for (const chunk of chunks) parser.push(chunk);
  return out;
}

describe("разбор SSE", () => {
  it("событие с id, типом и данными", () => {
    expect(parse(['id: 7\nevent: status\ndata: {"state":"running"}\n\n'])).toEqual([
      { id: "7", event: "status", data: '{"state":"running"}' },
    ]);
  });

  it("кусок может обрываться в любом месте, в том числе посреди \\r\\n", () => {
    const text = 'id: 1\r\nevent: block\r\ndata: {"a":1}\r\n\r\nid: 2\r\nevent: done\r\ndata: {}\r\n\r\n';
    for (let split = 1; split < text.length; split++) {
      const events = parse([text.slice(0, split), text.slice(split)]);
      expect(
        events.map((event) => event.id),
        `разрез ${split}`,
      ).toEqual(["1", "2"]);
      expect(events[0]?.data).toBe('{"a":1}');
    }
  });

  it("комментарии и heartbeat не создают событий, но считаются активностью", () => {
    const out: SseMessage[] = [];
    const activity = vi.fn();
    const parser = createSseParser((message) => out.push(message), activity);
    parser.push(":keepalive\n\n");
    parser.push(":keepalive\n\n");
    expect(out).toEqual([]);
    expect(activity).toHaveBeenCalledTimes(2);
  });

  it("несколько строк data склеиваются переводом строки; без event — message", () => {
    expect(parse(["data: первая\ndata: вторая\n\n"])).toEqual([
      { id: undefined, event: "message", data: "первая\nвторая" },
    ]);
  });

  it("id действует до следующего id (как в спецификации), пробел после двоеточия необязателен", () => {
    const events = parse(["id:5\nevent:block\ndata:x\n\nevent:block\ndata:y\n\n"]);
    expect(events.map((event) => [event.id, event.data])).toEqual([
      ["5", "x"],
      ["5", "y"],
    ]);
  });

  it("поле id с нулевым символом игнорируется", () => {
    expect(parse(["id: a\u0000b\ndata: z\n\n"])[0]?.id).toBeUndefined();
  });
});

function streamResponse(parts: string[], status = 200): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(new TextEncoder().encode(part));
      controller.close();
    },
  });
  return new Response(stream, { status, headers: { "content-type": "text/event-stream" } });
}

describe("readSse", () => {
  it("читает поток до конца, отправляет Last-Event-ID и вызывает onOpen", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
      streamResponse(["id: 3\nevent: block\ndata: {}\n\n", ":keepalive\n\nid: 4\nevent: done\ndata: {}\n\n"]),
    );
    const messages: SseMessage[] = [];
    const onOpen = vi.fn();
    await readSse({
      url: "http://localhost/api/v1/jobs/J/events",
      signal: new AbortController().signal,
      lastEventId: "2",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: (message) => messages.push(message),
      onOpen,
    });
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(messages.map((message) => message.id)).toEqual(["3", "4"]);
    const init = fetchImpl.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)["last-event-id"]).toBe("2");
    expect((init.headers as Record<string, string>).accept).toBe("text/event-stream");
    expect(init.credentials).toBe("include");
  });

  it("ответ не 200 — SseHttpError с телом problem+json", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ code: "not_found" }), { status: 404 }));
    await expect(
      readSse({
        url: "http://localhost/x",
        signal: new AbortController().signal,
        fetchImpl: fetchImpl as unknown as typeof fetch,
        onMessage: () => {},
      }),
    ).rejects.toMatchObject({ status: 404, body: { code: "not_found" } } satisfies Partial<SseHttpError>);
  });
});

describe("пауза переподключения", () => {
  it("1, 2, 4, 8 с и не больше 15 с", () => {
    expect([0, 1, 2, 3, 4, 10].map(reconnectDelay)).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
  });
});
