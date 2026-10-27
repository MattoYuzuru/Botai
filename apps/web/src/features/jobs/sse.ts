/**
 * Клиент SSE на `fetch`: поток задания `GET /jobs/{id}/events` (contracts/events.md §1).
 * `EventSource` не подходит: ему нельзя задать свою политику переподключения и проверить статус ответа,
 * а повторы должны идти в фоне и быть видны как «связь восстанавливается», а не как ошибка.
 */

export interface SseMessage {
  /** Поле `id:` — для `Last-Event-ID` при переподключении. */
  id?: string;
  event: string;
  data: string;
}

/**
 * Разбор текстового потока по правилам SSE: события разделяет пустая строка, строки `:` — комментарии
 * (heartbeat `:keepalive`), несколько `data:` склеиваются через перевод строки, `\r\n` и `\r` допустимы.
 * Данные приходят кусками произвольной длины.
 */
export function createSseParser(onMessage: (message: SseMessage) => void, onActivity?: () => void) {
  let buffer = "";
  let id: string | undefined;
  let event = "";
  let data: string[] = [];

  const dispatch = () => {
    if (data.length > 0 || event) onMessage({ id, event: event || "message", data: data.join("\n") });
    event = "";
    data = [];
  };

  const line = (raw: string) => {
    if (raw === "") return dispatch();
    if (raw.startsWith(":")) return; // комментарий, например :keepalive
    const colon = raw.indexOf(":");
    const field = colon < 0 ? raw : raw.slice(0, colon);
    let value = colon < 0 ? "" : raw.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id" && !value.includes("\0")) id = value;
  };

  return {
    push(chunk: string) {
      onActivity?.();
      buffer += chunk;
      let start = 0;
      for (let index = 0; index < buffer.length; index++) {
        const char = buffer[index];
        if (char !== "\n" && char !== "\r") continue;
        // «\r\n» — один перевод строки; «\r» в конце куска может оказаться началом «\r\n».
        if (char === "\r" && index === buffer.length - 1) break;
        line(buffer.slice(start, index));
        if (char === "\r" && buffer[index + 1] === "\n") index++;
        start = index + 1;
      }
      buffer = buffer.slice(start);
    },
  };
}

export interface ReadSseOptions {
  url: string;
  signal: AbortSignal;
  lastEventId?: string;
  onMessage: (message: SseMessage) => void;
  /** Любой байт из потока, включая heartbeat: сторож зависания сбрасывается. */
  onActivity?: () => void;
  onOpen?: () => void;
  fetchImpl?: typeof fetch;
}

export class SseHttpError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`SSE ${status}`);
    this.name = "SseHttpError";
  }
}

/** Одно соединение. Возвращается, когда сервер закрыл поток; бросает при сетевой ошибке и не-200. */
export async function readSse({
  url,
  signal,
  lastEventId,
  onMessage,
  onActivity,
  onOpen,
  fetchImpl = (input, init) => globalThis.fetch(input, init),
}: ReadSseOptions): Promise<void> {
  const headers: Record<string, string> = { accept: "text/event-stream" };
  if (lastEventId) headers["last-event-id"] = lastEventId;
  const response = await fetchImpl(url, { headers, signal, credentials: "include", cache: "no-store" });
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* тело не JSON */
    }
    throw new SseHttpError(response.status, body);
  }
  if (!response.body) throw new Error("SSE: пустое тело ответа");
  onOpen?.();
  const parser = createSseParser(onMessage, onActivity);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      parser.push(value);
    }
  } finally {
    reader.releaseLock();
  }
}

/** Пауза перед повторным подключением: 1, 2, 4, 8 с и не больше 15 с. */
export function reconnectDelay(attempt: number): number {
  return Math.min(1000 * 2 ** Math.max(0, attempt), 15_000);
}
