import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Login } from "@/features/agent-blocks/types";
import { encode, keyMessage, parseServerMessage, resolveSocketUrl, toRemotePoint } from "./protocol";
import { RemoteScreen } from "./remote-screen";
import { RemoteSession, type SocketLike } from "./remote-session";

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  receive(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  messages() {
    return this.sent.map((text) => JSON.parse(text) as Record<string, unknown>);
  }
}

const ORIGIN = "http://localhost:3000";
const block: Login = {
  type: "login",
  id: "01JAAAAAAAAAAAAAAAAAAAAA01",
  at: "2026-10-27T10:00:00Z",
  connectionId: "01JAAAAAAAAAAAAAAAAAAAAA02",
  ticket: "t-1",
  wsUrl: "/api/v1/login-sessions/S1/ws",
  expiresAt: "2026-10-27T10:10:00Z",
  hint: "Вуз показал проверку «я не робот».",
};

describe("протокол запасного экрана", () => {
  it("кадр разбирается, чужое и опасное отбрасывается", () => {
    const frame = { t: "frame", seq: 1, mime: "image/jpeg", w: 390, h: 640, data: "AAAA" };
    expect(parseServerMessage(JSON.stringify(frame))).toEqual(frame);
    expect(parseServerMessage(JSON.stringify({ ...frame, mime: "text/html" }))).toBeNull();
    expect(parseServerMessage(JSON.stringify({ ...frame, data: "<script>" }))).toBeNull();
    expect(parseServerMessage(JSON.stringify({ ...frame, w: 99999 }))).toBeNull();
    expect(parseServerMessage("не json")).toBeNull();
    expect(parseServerMessage(JSON.stringify({ t: "needs", what: "teleport" }))).toBeNull();
    expect(parseServerMessage(JSON.stringify({ t: "needs", what: "captcha", hint: "x" }))).toEqual({
      t: "needs",
      what: "captcha",
      hint: "x",
    });
  });

  it("координаты: масштаб по размеру кадра, а не окна, с обрезкой по краям", () => {
    const rect = { left: 10, top: 20, width: 195, height: 320 };
    expect(toRemotePoint({ x: 10 + 97.5, y: 20 + 160 }, rect, { w: 390, h: 640 })).toEqual({
      x: 195,
      y: 320,
    });
    expect(toRemotePoint({ x: -50, y: 9999 }, rect, { w: 390, h: 640 })).toEqual({ x: 0, y: 639 });
  });

  it("адрес сокета: тот же origin, тикет добавляется, чужой хост и не ws — отказ", () => {
    expect(resolveSocketUrl("/api/v1/login-sessions/S1/ws", "t-1", ORIGIN)).toBe(
      "ws://localhost:3000/api/v1/login-sessions/S1/ws?ticket=t-1",
    );
    expect(resolveSocketUrl("wss://localhost:3000/ws?ticket=zz", "t-1", ORIGIN)).toBe(
      "wss://localhost:3000/ws?ticket=zz",
    );
    expect(resolveSocketUrl("wss://evil.test/ws", "t-1", ORIGIN)).toBeNull();
    expect(resolveSocketUrl("javascript:alert(1)", "t-1", ORIGIN)).toBeNull();
    expect(resolveSocketUrl("ftp://localhost:3000/ws", "t-1", ORIGIN)).toBeNull();
  });

  it("сообщения клиента не больше 8 КБ; клавиши: символ или управляющая", () => {
    expect(encode({ t: "key", kind: "char", text: "а".repeat(5000) })).toBeNull();
    expect(encode({ t: "ack", seq: 3 })).toBe('{"t":"ack","seq":3}');
    expect(keyMessage({ key: "ф", code: "KeyA" })).toEqual({ t: "key", kind: "char", text: "ф" });
    expect(keyMessage({ key: "Enter", code: "Enter" })).toEqual({ t: "key", kind: "down", code: "Enter" });
    expect(keyMessage({ key: "a", code: "KeyA", ctrlKey: true })).toBeNull();
    expect(keyMessage({ key: "Shift", code: "ShiftLeft" })).toBeNull();
  });
});

describe("сессия запасного экрана", () => {
  const make = (renew = vi.fn(async () => ({ wsUrl: "/api/v1/login-sessions/S2/ws", ticket: "t-2" }))) => {
    FakeSocket.all = [];
    const onMessage = vi.fn();
    const onStatus = vi.fn();
    const session = new RemoteSession({
      origin: ORIGIN,
      first: { wsUrl: block.wsUrl, ticket: block.ticket },
      renew,
      onMessage,
      onStatus,
      createSocket: (url) => new FakeSocket(url),
      retryDelays: [0, 0, 0],
    });
    return { session, renew, onMessage, onStatus };
  };

  it("первое подключение по билету из блока, статус live после открытия", () => {
    const { session, onStatus } = make();
    session.start();
    expect(FakeSocket.all[0]?.url).toContain("ticket=t-1");
    FakeSocket.all[0]?.open();
    expect(onStatus).toHaveBeenLastCalledWith("live");
  });

  it("обрыв: новый билет и переподключение без вмешательства человека", async () => {
    const { session, renew, onStatus } = make();
    session.start();
    FakeSocket.all[0]?.open();
    FakeSocket.all[0]?.drop();
    await waitFor(() => expect(FakeSocket.all).toHaveLength(2));
    expect(renew).toHaveBeenCalledTimes(1);
    expect(FakeSocket.all[1]?.url).toContain("ticket=t-2");
    expect(onStatus).toHaveBeenCalledWith("reconnecting");
    FakeSocket.all[1]?.open();
    expect(onStatus).toHaveBeenLastCalledWith("live");
  });

  it("success закрывает сессию и больше не переподключается", async () => {
    const { session, onMessage, onStatus } = make();
    session.start();
    FakeSocket.all[0]?.open();
    FakeSocket.all[0]?.receive({ t: "success" });
    FakeSocket.all[0]?.drop();
    expect(onMessage).toHaveBeenCalledWith({ t: "success" });
    expect(onStatus).toHaveBeenLastCalledWith("ended");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(FakeSocket.all).toHaveLength(1);
  });

  it("экран занят другим окном или вход уже не ждёт человека: переподключения нет, текст человеческий", async () => {
    for (const code of [4001, 4404]) {
      FakeSocket.all = [];
      const { session, renew, onMessage } = make();
      session.start();
      FakeSocket.all[0]?.open();
      FakeSocket.all[0]?.drop(code);
      expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ t: "error", code: `closed_${code}` }));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(renew).not.toHaveBeenCalled();
      expect(FakeSocket.all).toHaveLength(1);
    }
  });

  it("не удаётся переподключиться — человеческий текст, без бесконечных попыток", async () => {
    const renew = vi.fn(async () => {
      throw new Error("нет сети");
    });
    const { session, onMessage } = make(renew);
    session.start();
    FakeSocket.all[0]?.open();
    FakeSocket.all[0]?.drop();
    await waitFor(() =>
      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({ t: "error", code: "connection_lost" }),
      ),
    );
    expect(renew.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it("лимит частоты: больше 100 сообщений в секунду отбрасываются, подтверждения кадров — нет", () => {
    FakeSocket.all = [];
    let time = 1000;
    const session = new RemoteSession({
      origin: ORIGIN,
      first: { wsUrl: block.wsUrl, ticket: "t" },
      renew: async () => ({ wsUrl: block.wsUrl, ticket: "t" }),
      onMessage: () => {},
      onStatus: () => {},
      createSocket: (url) => new FakeSocket(url),
      now: () => time,
    });
    session.start();
    FakeSocket.all[0]?.open();
    let accepted = 0;
    for (let i = 0; i < 150; i++)
      if (session.send({ t: "mouse", kind: "move", x: i, y: 0, button: "left" })) accepted++;
    expect(accepted).toBe(100);
    expect(session.send({ t: "ack", seq: 1 })).toBe(true);
    time += 1100;
    expect(session.send({ t: "mouse", kind: "move", x: 1, y: 1, button: "left" })).toBe(true);
  });

  it("недопустимый адрес сокета — ошибка без подключения", () => {
    FakeSocket.all = [];
    const onMessage = vi.fn();
    const session = new RemoteSession({
      origin: ORIGIN,
      first: { wsUrl: "wss://evil.test/ws", ticket: "t" },
      renew: async () => ({ wsUrl: "", ticket: "" }),
      onMessage,
      onStatus: () => {},
      createSocket: (url) => new FakeSocket(url),
    });
    session.start();
    expect(FakeSocket.all).toHaveLength(0);
    expect(onMessage).toHaveBeenCalledWith(expect.objectContaining({ t: "error", code: "bad_screen_url" }));
  });
});

describe("RemoteScreen", () => {
  const renderScreen = () => {
    FakeSocket.all = [];
    render(
      <RemoteScreen
        block={block}
        nowMs={Date.parse("2026-10-27T10:00:00Z")}
        createSocket={(url) => new FakeSocket(url)}
        retryDelays={[0, 0, 0]}
      />,
    );
    return FakeSocket.all[0] as FakeSocket;
  };

  it("предупреждает, что экран идёт через сервер; показывает статус и оставшееся время словами", async () => {
    const socket = renderScreen();
    expect(screen.getByText(/проходят через сервер BotAI/)).toBeInTheDocument();
    expect(screen.getByText(/Экран закроется через 10 мин/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Я вошёл" })).toBeDisabled();
    act(() => socket.open());
    expect(await screen.findByText(/Нажмите на экран, чтобы ввести данные/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Я вошёл" })).toBeEnabled();
    // При открытии шлётся размер под ширину экрана.
    expect(socket.messages().some((message) => message.t === "resize")).toBe(true);
  });

  it("капча: сказано словами, что проверку проходит человек; обходить мы не будем", () => {
    const socket = renderScreen();
    act(() => socket.open());
    act(() => socket.receive({ t: "needs", what: "captcha", hint: "" }));
    expect(screen.getByText(/Пройдите её сами: обходить такие проверки мы не будем/)).toBeInTheDocument();
  });

  it("кадр подтверждается, касание — клик в координатах кадра, свайп — прокрутка", () => {
    const socket = renderScreen();
    act(() => socket.open());
    act(() => socket.receive({ t: "frame", seq: 5, mime: "image/jpeg", w: 390, h: 640, data: "AAAA" }));
    expect(socket.messages()).toContainEqual({ t: "ack", seq: 5 });

    const canvas = screen.getByTestId("remote-canvas");
    canvas.getBoundingClientRect = () => ({
      left: 0,
      top: 0,
      width: 195,
      height: 320,
      right: 195,
      bottom: 320,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const fire = (type: string, props: Record<string, unknown>) =>
      canvas.dispatchEvent(
        new PointerEvent(type, { pointerId: 1, pointerType: "touch", bubbles: true, ...props }),
      );

    fire("pointerdown", { clientX: 100, clientY: 160 });
    fire("pointerup", { clientX: 100, clientY: 160 });
    expect(socket.messages()).toContainEqual({ t: "mouse", kind: "click", x: 200, y: 320, button: "left" });

    fire("pointerdown", { clientX: 100, clientY: 200 });
    fire("pointermove", { clientX: 100, clientY: 150 });
    fire("pointerup", { clientX: 100, clientY: 150 });
    const scroll = socket.messages().find((message) => message.t === "scroll");
    expect(scroll).toMatchObject({ t: "scroll", dx: 0, dy: 50 });
    // Свайп не превращается в клик.
    expect(socket.messages().filter((message) => message.t === "mouse")).toHaveLength(1);
  });

  it("нажатие на экран ставит фокус на скрытое поле: так открывается экранная клавиатура", async () => {
    const socket = renderScreen();
    act(() => socket.open());
    await userEvent.click(screen.getByTestId("remote-canvas"));
    expect(screen.getByLabelText("Ввод с клавиатуры на странице вуза")).toHaveFocus();
  });

  it("«Я вошёл» отправляет done; success показывает итог и убирает управление", async () => {
    const socket = renderScreen();
    act(() => socket.open());
    await userEvent.click(screen.getByRole("button", { name: "Я вошёл" }));
    expect(socket.messages()).toContainEqual({ t: "done" });
    act(() => socket.receive({ t: "success" }));
    expect(screen.getByText(/Вход выполнен/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Я вошёл" })).toBeNull();
  });

  it("обрыв связи — полоса «Связь прервалась… вход не потерян» и переподключение", async () => {
    const socket = renderScreen();
    act(() => socket.open());
    act(() => socket.drop());
    expect(await screen.findByText(/Ваш вход на сайте вуза не потерян/)).toBeInTheDocument();
  });

  it("истёкшая сессия и ошибка — человеческий текст", () => {
    const socket = renderScreen();
    act(() => socket.open());
    act(() => socket.receive({ t: "expired", reason: "ttl" }));
    expect(screen.getByText(/Время входа истекло/)).toBeInTheDocument();
  });
});
