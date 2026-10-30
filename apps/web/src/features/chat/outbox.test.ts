import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, NetworkError } from "@/api/errors";
import type { Message, Thread } from "@/api/types";
import type { MessageAccepted } from "@/api/v3";
import {
  conflictKind,
  isCalendarLink,
  isSecretInChat,
  Outbox,
  type OutboxDeps,
  type OutboxEvent,
  type OutboxPost,
  retryDelay,
} from "./outbox";

const NOW = Date.UTC(2026, 9, 27, 11, 5);

const thread = (id = "t1"): Thread => ({
  id,
  spaceId: null,
  title: "Новый чат",
  createdAt: new Date(NOW).toISOString(),
  lastMessageAt: new Date(NOW).toISOString(),
  unread: false,
  attention: null,
  activeTask: null,
  preview: null,
});

const userMessage = (text: string, at = NOW): Message => ({
  id: `m-${text}`,
  role: "user",
  blocks: [{ type: "text", id: "b1", at: new Date(at).toISOString(), markdown: text }],
  jobId: null,
  createdAt: new Date(at).toISOString(),
});

const accepted = (text: string): MessageAccepted => ({ message: userMessage(text), jobId: "job-1" });

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
  };
}

function setup(overrides: Partial<OutboxDeps> = {}) {
  let clock = NOW;
  const storage = memoryStorage();
  let online = true;
  let counter = 0;
  const deps: OutboxDeps = {
    now: () => clock,
    online: () => online,
    currentUserId: async () => "u1",
    createThread: vi.fn(async () => thread()),
    postMessage: vi.fn(async (_threadId: string, post: OutboxPost) => accepted(post.text)),
    listMessages: vi.fn(async () => [] as Message[]),
    storage,
    setTimer: (run, ms) => setTimeout(run, ms),
    clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    newId: () => `local-${++counter}`,
    ...overrides,
  };
  const outbox = new Outbox(deps);
  const events: OutboxEvent[] = [];
  outbox.on((event) => events.push(event));
  return {
    outbox,
    deps,
    events,
    storage,
    setOnline: (value: boolean) => {
      online = value;
    },
    advance: async (ms: number) => {
      clock += ms;
      await vi.advanceTimersByTimeAsync(ms);
    },
  };
}

const target = { ownerId: "u1", threadId: "t1", spaceId: null };

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("очередь исходящих сообщений", () => {
  it("сброс во время создания чата не возобновляет отправку после ответа сервера", async () => {
    let resolveThread: (value: Thread) => void = () => undefined;
    const { outbox, deps, events } = setup({
      createThread: () =>
        new Promise((resolve) => {
          resolveThread = resolve;
        }),
    });
    outbox.send("Старая попытка подключения", { ...target, threadId: null });
    await vi.advanceTimersByTimeAsync(0);
    outbox.discardBefore("u1", NOW);
    resolveThread(thread());
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).not.toHaveBeenCalled();
    expect(events).toEqual([]);
    expect(outbox.snapshot).toEqual([]);
  });

  it("сброс во время проверки сессии не отправляет уже удалённое сообщение и не создаёт чат", async () => {
    let resolveUser: (id: string) => void = () => undefined;
    const { outbox, deps } = setup({
      currentUserId: () =>
        new Promise((resolve) => {
          resolveUser = resolve;
        }),
    });
    outbox.send("Старая попытка подключения", { ...target, threadId: null });
    outbox.discardBefore("u1", NOW);
    resolveUser("u1");
    await vi.advanceTimersByTimeAsync(0);
    expect(outbox.snapshot).toEqual([]);
    expect(deps.createThread).not.toHaveBeenCalled();
    expect(deps.postMessage).not.toHaveBeenCalled();
  });

  it("с сетью сообщение уходит сразу и пропадает из очереди; событие несёт задание ответа", async () => {
    const { outbox, deps, events } = setup();
    outbox.send("Какая следующая пара?", target);
    expect(outbox.snapshot[0]?.status).toBe("sending");
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).toHaveBeenCalledWith("t1", {
      text: "Какая следующая пара?",
      attachmentIds: [],
      clientMessageId: "local-1",
      targetJobId: undefined,
    });
    expect(outbox.snapshot).toEqual([]);
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "sent", threadId: "t1", jobId: "job-1" });
  });

  it("первое сообщение нового чата создаёт чат, потом отправляется в него", async () => {
    const { outbox, deps, events } = setup();
    outbox.send("Привет", { ownerId: "u1", threadId: null, spaceId: "space-1" });
    await vi.advanceTimersByTimeAsync(0);
    // Идентификатор очереди уходит как clientRequestId: повторный запрос не создаст второй чат.
    expect(deps.createThread).toHaveBeenCalledWith("space-1", "local-1");
    expect(events.map((event) => event.type)).toEqual(["thread", "sent"]);
    expect(deps.postMessage).toHaveBeenCalledWith("t1", expect.objectContaining({ text: "Привет" }));
  });

  it("без сети сообщение ждёт с пометкой и уходит само, когда сеть вернулась", async () => {
    const { outbox, deps, events, setOnline } = setup();
    setOnline(false);
    outbox.send("Когда экзамен?", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(outbox.snapshot[0]?.status).toBe("queued");
    expect(deps.postMessage).not.toHaveBeenCalled();
    setOnline(true);
    outbox.kick();
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).toHaveBeenCalledTimes(1);
    expect(outbox.snapshot).toEqual([]);
    expect(events.at(-1)).toMatchObject({ type: "sent" });
  });

  it("сбой сети при отправке: пометка «ждёт сети», повтор с нарастающей паузой, сообщение одно", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new NetworkError())
      .mockRejectedValueOnce(new NetworkError())
      .mockImplementation(async (_threadId: string, post: OutboxPost) => accepted(post.text));
    const { outbox, advance, events } = setup({ postMessage: post });
    outbox.send("Что сдавать?", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(outbox.snapshot[0]).toMatchObject({ status: "queued", attempts: 1 });
    expect(post).toHaveBeenCalledTimes(1);
    await advance(retryDelay(1) - 1);
    expect(post).toHaveBeenCalledTimes(1);
    await advance(2);
    expect(post).toHaveBeenCalledTimes(2);
    expect(outbox.snapshot[0]?.attempts).toBe(2);
    await advance(retryDelay(2) + 1);
    expect(post).toHaveBeenCalledTimes(3);
    expect(outbox.snapshot).toEqual([]);
    expect(events.filter((event) => event.type === "sent")).toHaveLength(1);
  });

  it("пауза не длиннее 30 секунд", () => {
    expect(retryDelay(1)).toBe(2_000);
    expect(retryDelay(2)).toBe(4_000);
    expect(retryDelay(20)).toBe(30_000);
  });

  it("ответ потерян по дороге, а сообщение дошло: по clientMessageId его находят и не дублируют", async () => {
    const post = vi.fn().mockRejectedValueOnce(new NetworkError());
    const list = vi.fn(async () => [
      { ...userMessage("Что сдавать?", NOW + 500), clientMessageId: "local-1" },
    ]);
    const { outbox, advance, events } = setup({ postMessage: post, listMessages: list });
    outbox.send("Что сдавать?", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(post).toHaveBeenCalledTimes(1);
    await advance(retryDelay(1) + 1);
    expect(list).toHaveBeenCalledWith("t1");
    expect(post).toHaveBeenCalledTimes(1);
    expect(outbox.snapshot).toEqual([]);
    expect(events.at(-1)).toMatchObject({ type: "sent", jobId: null });
  });

  it("такой же текст от другого сообщения дубликатом не считается: сопоставление — по clientMessageId, не по тексту", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new NetworkError())
      .mockImplementation(async (_threadId: string, body: OutboxPost) => accepted(body.text));
    // В чате уже есть сообщение с тем же текстом, но другое: свежее и без нашего идентификатора.
    const list = vi.fn(async () => [
      { ...userMessage("Что сдавать?", NOW + 100), clientMessageId: "someone-else" },
      userMessage("Что сдавать?", NOW + 200),
    ]);
    const { outbox, advance } = setup({ postMessage: post, listMessages: list });
    outbox.send("Что сдавать?", target);
    await vi.advanceTimersByTimeAsync(0);
    await advance(retryDelay(1) + 1);
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenLastCalledWith("t1", expect.objectContaining({ clientMessageId: "local-1" }));
    expect(outbox.snapshot).toEqual([]);
  });

  it("ход агента ещё идёт (409 agent_busy): ждём с нарастающей паузой и отправляем, подписи «нет сети» нет", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(409, "agent_busy", "Ассистент ещё отвечает на прошлое сообщение"))
      .mockImplementation(async (_threadId: string, body: OutboxPost) => accepted(body.text));
    const { outbox, advance } = setup({ postMessage: post });
    outbox.send("И ещё вопрос", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(outbox.snapshot[0]?.status).toBe("sending");
    await advance(3_100);
    expect(post).toHaveBeenCalledTimes(2);
    expect(outbox.snapshot).toEqual([]);
  });

  it("агент не освобождается: после пяти попыток сообщение снимается и возвращается человеку, а не лежит вечно", async () => {
    const post = vi.fn().mockRejectedValue(new ApiError(409, "agent_busy", ""));
    const { outbox, advance, events, storage } = setup({ postMessage: post });
    outbox.send("Терпеливое сообщение", target);
    await vi.advanceTimersByTimeAsync(0);
    // Паузы 3, 6, 12, 24 с: пять попыток в сумме.
    for (const pause of [3_000, 6_000, 12_000, 24_000]) await advance(pause + 100);
    expect(post).toHaveBeenCalledTimes(5);
    expect(events.at(-1)).toMatchObject({ type: "rejected" });
    expect(outbox.snapshot).toEqual([]);
    expect(storage.data.has("botai.chat.outbox")).toBe(false);
  });

  it("запасное распознавание busy по тексту, пока бэкенд не отдаёт код", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(409, "conflict", "Ассистент ещё отвечает на прошлое сообщение"))
      .mockImplementation(async (_threadId: string, body: OutboxPost) => accepted(body.text));
    const { outbox, advance } = setup({ postMessage: post });
    outbox.send("И ещё", target);
    await vi.advanceTimersByTimeAsync(0);
    await advance(3_100);
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("пароль в обычном поле (409 secret_in_chat): сообщение сразу стирается из очереди и хранилища, повтора нет", async () => {
    const post = vi
      .fn()
      .mockRejectedValue(
        new ApiError(409, "secret_in_chat", "Пароль и коды вводите только в защищённое поле"),
      );
    const { outbox, advance, events, storage } = setup({ postMessage: post });
    outbox.send("correct-horse-battery", { ...target, targetJobId: "job-9" });
    await vi.advanceTimersByTimeAsync(0);
    expect(post).toHaveBeenCalledTimes(1);
    expect(events.at(-1)).toMatchObject({ type: "rejected" });
    expect(outbox.snapshot).toEqual([]);
    // Ни очереди, ни пароля в localStorage: ни сейчас, ни после «перезагрузки» и долгого ожидания.
    expect(storage.data.has("botai.chat.outbox")).toBe(false);
    expect([...storage.data.values()].join("")).not.toContain("correct-horse-battery");
    await advance(60_000);
    expect(post).toHaveBeenCalledTimes(1);
    expect(setup({ storage }).outbox.snapshot).toEqual([]);
  });

  it("секрет узнаётся и по тексту отказа, пока код не пришёл; isSecretInChat различает его", async () => {
    const post = vi.fn().mockRejectedValue(new ApiError(409, "conflict", "Ответьте в защищённом блоке"));
    const { outbox, events, storage } = setup({ postMessage: post });
    outbox.send("123456", target);
    await vi.advanceTimersByTimeAsync(0);
    const rejected = events.at(-1);
    expect(rejected?.type === "rejected" && isSecretInChat(rejected.error)).toBe(true);
    expect(storage.data.has("botai.chat.outbox")).toBe(false);
    expect(isSecretInChat(new ApiError(409, "agent_busy", ""))).toBe(false);
    expect(isSecretInChat(new ApiError(422, "validation", ""))).toBe(false);
    expect(isSecretInChat(new NetworkError())).toBe(false);
  });

  it("личная ссылка календаря (409 secret_link_calendar): стёрта из очереди и хранилища, повтора нет, это не секрет «пароля»", async () => {
    const link = "https://calendar.example.org/export/ics.xml?private_token=k9Xq2LmZp7Vb";
    const post = vi
      .fn()
      .mockRejectedValue(
        new ApiError(
          409,
          "secret_link_calendar",
          "Это личная ссылка календаря — подключить календарь можно через защищённое поле: я предложил это ниже",
        ),
      );
    const { outbox, advance, events, storage } = setup({ postMessage: post });
    outbox.send(`вот ссылка ${link}`, target);
    await vi.advanceTimersByTimeAsync(0);
    expect(post).toHaveBeenCalledTimes(1);
    const rejected = events.at(-1);
    expect(rejected).toMatchObject({ type: "rejected" });
    expect(rejected?.type === "rejected" && isCalendarLink(rejected.error)).toBe(true);
    // Не «пароль в чате»: у экрана для них разные подсказки.
    expect(rejected?.type === "rejected" && isSecretInChat(rejected.error)).toBe(false);
    expect(outbox.snapshot).toEqual([]);
    expect(storage.data.has("botai.chat.outbox")).toBe(false);
    expect([...storage.data.values()].join("")).not.toContain("private_token");
    await advance(60_000);
    expect(post).toHaveBeenCalledTimes(1);
    expect(setup({ storage }).outbox.snapshot).toEqual([]);
  });

  it("conflictKind различает коды 409; текст отказа календаря не принимается за пароль", () => {
    const detail =
      "Это личная ссылка календаря — подключить календарь можно через защищённое поле: я предложил это ниже";
    expect(conflictKind(new ApiError(409, "secret_link_calendar", detail))).toBe("calendar_link");
    expect(conflictKind(new ApiError(409, "secret_in_chat", detail))).toBe("secret_in_chat");
    expect(conflictKind(new ApiError(409, "agent_busy", ""))).toBe("agent_busy");
    expect(conflictKind(new ApiError(422, "secret_link_calendar", detail))).toBe("other");
    expect(isCalendarLink(new ApiError(409, "agent_busy", ""))).toBe(false);
    expect(isCalendarLink(new NetworkError())).toBe(false);
  });

  it("любой другой 409 — отказ по существу: сообщение снимается, текст возвращается человеку", async () => {
    const post = vi.fn().mockRejectedValue(new ApiError(409, "conflict", "Этот вопрос уже закрыт"));
    const { outbox, events } = setup({ postMessage: post });
    outbox.send("Ответ на закрытый вопрос", { ...target, targetJobId: "job-9" });
    await vi.advanceTimersByTimeAsync(0);
    expect(post).toHaveBeenCalledTimes(1);
    expect(events.at(-1)).toMatchObject({ type: "rejected", item: { text: "Ответ на закрытый вопрос" } });
    expect(outbox.snapshot).toEqual([]);
  });

  it("отказ сервера по существу не повторяется: сообщение снимается, остальные идут дальше", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(422, "validation", ""))
      .mockImplementation(async (_threadId: string, post: OutboxPost) => accepted(post.text));
    const { outbox, events } = setup({ postMessage: post });
    outbox.send("a".repeat(10), target);
    outbox.send("Второе", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(events.map((event) => event.type)).toEqual(["rejected", "sent"]);
    expect(outbox.snapshot).toEqual([]);
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("порядок сохраняется: пока первое не ушло, второе не отправляется", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce(new NetworkError())
      .mockImplementation(async (_threadId: string, body: OutboxPost) => accepted(body.text));
    const { outbox, advance } = setup({ postMessage: post });
    outbox.send("Первое", target);
    outbox.send("Второе", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(post).toHaveBeenCalledTimes(1);
    expect(outbox.snapshot.map((item) => item.text)).toEqual(["Первое", "Второе"]);
    await advance(retryDelay(1) + 1);
    expect(post.mock.calls.map((call) => (call[1] as OutboxPost).text)).toEqual([
      "Первое",
      "Первое",
      "Второе",
    ]);
  });

  it("очередь переживает перезагрузку, а чужие сообщения после смены пользователя стираются", async () => {
    const first = setup();
    first.setOnline(false);
    first.outbox.send("Мой вопрос", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(first.storage.data.get("botai.chat.outbox")).toContain("Мой вопрос");

    // «Перезагрузка»: новая очередь читает то же хранилище; сообщения снова ждут, а не «отправляются».
    const second = setup({ storage: first.storage, currentUserId: async () => "u2" });
    expect(second.outbox.snapshot).toHaveLength(1);
    expect(second.outbox.snapshot[0]?.status).toBe("queued");
    second.outbox.kick();
    await vi.advanceTimersByTimeAsync(0);
    // Вошёл другой человек: чужое сообщение не отправлено и стёрто.
    expect(second.deps.postMessage).not.toHaveBeenCalled();
    expect(second.outbox.snapshot).toEqual([]);
    expect(second.storage.data.has("botai.chat.outbox")).toBe(false);
  });

  it("сессии нет (401): сообщения ждут входа и не пропадают", async () => {
    const { outbox, deps } = setup({ currentUserId: async () => null });
    outbox.send("Вопрос", target);
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).not.toHaveBeenCalled();
    expect(outbox.snapshot).toHaveLength(1);
    expect(outbox.snapshot[0]?.status).toBe("queued");
  });

  it("вложения уходят идентификаторами; сообщение без текста допустимо, если вложение загружено", async () => {
    const { outbox, deps, events } = setup();
    const photo = { id: "A1", name: "фото.png", mime: "image/png", size: 12, kind: "image" as const };
    outbox.send("", { ...target, attachments: [photo] });
    expect(outbox.snapshot[0]?.attachments).toEqual([photo]);
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).toHaveBeenCalledWith("t1", {
      text: "",
      attachmentIds: ["A1"],
      clientMessageId: "local-1",
      targetJobId: undefined,
    });
    expect(events[0]).toMatchObject({ type: "sent" });
  });

  it("вложения переживают перезагрузку страницы вместе с сообщением", async () => {
    const first = setup();
    first.setOnline(false);
    const voice = { id: "A2", name: "голос.webm", mime: "audio/webm", size: 5, kind: "audio" as const };
    first.outbox.send("Послушай", { ...target, attachments: [voice] });
    await vi.advanceTimersByTimeAsync(0);
    const second = setup({ storage: first.storage });
    expect(second.outbox.snapshot[0]?.attachments).toEqual([voice]);
  });

  it("ответ на вопрос задачи уходит с targetJobId", async () => {
    const { outbox, deps } = setup();
    outbox.send("https://lk.fakeuni.test", { ...target, targetJobId: "job-9" });
    await vi.advanceTimersByTimeAsync(0);
    expect(deps.postMessage).toHaveBeenCalledWith("t1", expect.objectContaining({ targetJobId: "job-9" }));
  });

  it("clear() стирает очередь и хранилище", async () => {
    const { outbox, storage, setOnline } = setup();
    setOnline(false);
    outbox.send("Вопрос", target);
    await vi.advanceTimersByTimeAsync(0);
    outbox.clear();
    expect(outbox.snapshot).toEqual([]);
    expect(storage.data.size).toBe(0);
  });
});
