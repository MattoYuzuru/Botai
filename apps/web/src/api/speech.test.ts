import { afterEach, describe, expect, it, vi } from "vitest";
import { transcribeSpeech } from "./speech";

/** `XMLHttpRequest`, которым управляет тест: что ушло на сервер и что он ответил. */
class FakeRequest {
  static last: FakeRequest | undefined;
  withCredentials = false;
  responseType = "";
  status = 0;
  responseText = "";
  upload = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: FormData | undefined;
  aborted = false;
  constructor() {
    FakeRequest.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: FormData) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  answer(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
  }
}

afterEach(() => vi.unstubAllGlobals());

const clip = () => new File(["voice"], "dictation.webm", { type: "audio/webm" });
const options = () => ({ signal: new AbortController().signal, durationMs: 7_432.6 });

describe("transcribeSpeech", () => {
  it("отправляет запись и длительность формой на /api/v1/speech/transcribe и возвращает текст", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeRequest);
    const result = transcribeSpeech(clip(), options());
    const request = FakeRequest.last as FakeRequest;
    expect(request.method).toBe("POST");
    expect(request.url).toContain("/api/v1/speech/transcribe");
    expect(request.withCredentials).toBe(true);
    const audio = request.body?.get("audio") as File;
    expect(audio.name).toBe("dictation.webm");
    expect(audio.type).toBe("audio/webm");
    expect(request.body?.get("durationMs")).toBe("7433");
    request.answer(200, { text: "Какие у меня пары завтра" });
    await expect(result).resolves.toBe("Какие у меня пары завтра");
  });

  it("ошибка сервера превращается в Error с понятным текстом: сообщение сервера или фраза по коду", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeRequest);
    const first = transcribeSpeech(clip(), options());
    FakeRequest.last?.answer(429, {
      code: "quota_exceeded",
      detail: "Слишком много записей за час. Попробуйте через 12 мин или напишите текстом.",
    });
    await expect(first).rejects.toThrow("Попробуйте через 12 мин");

    const second = transcribeSpeech(clip(), options());
    FakeRequest.last?.answer(503, { code: "stt_unavailable" });
    await expect(second).rejects.toThrow("Распознавание речи сейчас недоступно. Напишите текстом.");

    const third = transcribeSpeech(clip(), options());
    FakeRequest.last?.answer(502, "<html>bad gateway</html>");
    await expect(third).rejects.toThrow("У BotAI сейчас неполадка");
  });

  it("обрыв связи — понятная фраза про сеть", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeRequest);
    const result = transcribeSpeech(clip(), options());
    FakeRequest.last?.onerror?.();
    await expect(result).rejects.toThrow("Нет связи с BotAI");
  });

  it("отмена обрывает запрос и остаётся отменой, а не ошибкой для человека", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeRequest);
    const controller = new AbortController();
    const result = transcribeSpeech(clip(), { signal: controller.signal, durationMs: 1000 });
    controller.abort();
    expect(FakeRequest.last?.aborted).toBe(true);
    await expect(result).rejects.toMatchObject({ name: "AbortError" });
  });
});
