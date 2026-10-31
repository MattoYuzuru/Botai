import {
  type ClientMessage,
  encode,
  parseServerMessage,
  resolveSocketUrl,
  type ServerMessage,
} from "./protocol";

export type SessionStatus = "connecting" | "live" | "reconnecting" | "ended";

export interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
}

export interface Ticket {
  wsUrl: string;
  ticket: string;
}

export interface RemoteSessionOptions {
  origin: string;
  /** Билет из блока `login` — для первого подключения. */
  first: Ticket;
  /** Новый билет для переподключения (`POST /connections/{id}/login-session`). */
  renew: () => Promise<Ticket>;
  onMessage: (message: ServerMessage) => void;
  onStatus: (status: SessionStatus) => void;
  createSocket?: (url: string) => SocketLike;
  /** Паузы между попытками переподключения, мс: суммарно меньше 3 с по контракту (§3 ADR-0001). */
  retryDelays?: readonly number[];
  now?: () => number;
}

const OPEN = 1;

/**
 * Коды закрытия сервера (`botai/browser/screen.py`), после которых переподключаться нельзя: экран занят
 * другим окном, адрес не наш, вход уже не ждёт человека, слишком частый ввод.
 */
const FINAL_CLOSE: Record<number, string> = {
  4001: "Этот экран открыт в другом окне или на другом устройстве.",
  4403: "Запасной экран открыт с недопустимого адреса. Откройте вход заново.",
  4404: "Вход сейчас не ждёт вас на экране. Если подключение продолжается, дождитесь его в основном окне.",
  1008: "Экран закрыт из-за слишком частого ввода. Откройте вход заново.",
};

/**
 * Соединение с запасным экраном входа. Обрыв — автоматическое переподключение с новым одноразовым билетом
 * за ≤ 3 с; сессия браузера на сервере при этом не теряется. Ввод ограничен по размеру и частоте (лимиты контракта).
 */
export class RemoteSession {
  private socket: SocketLike | null = null;
  private status: SessionStatus = "connecting";
  private ended = false;
  private attempt = 0;
  private sent: number[] = [];
  private timer: ReturnType<typeof setTimeout> | undefined;
  private readonly retryDelays: readonly number[];
  private readonly now: () => number;

  constructor(private readonly options: RemoteSessionOptions) {
    this.retryDelays = options.retryDelays ?? [0, 400, 800, 1500];
    this.now = options.now ?? (() => Date.now());
  }

  start(): void {
    this.connect(this.options.first);
  }

  private setStatus(status: SessionStatus) {
    if (this.status === status) return;
    this.status = status;
    this.options.onStatus(status);
  }

  private connect(ticket: Ticket) {
    const url = resolveSocketUrl(ticket.wsUrl, ticket.ticket, this.options.origin);
    if (!url) {
      this.finish({ t: "error", code: "bad_screen_url", detail: "Адрес запасного экрана недопустим." });
      return;
    }
    const socket = (
      this.options.createSocket ?? ((target) => new WebSocket(target) as unknown as SocketLike)
    )(url);
    this.socket = socket;
    socket.onopen = () => {
      this.attempt = 0;
      this.setStatus("live");
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      const message = parseServerMessage(event.data);
      if (!message) return;
      if (message.t === "success" || message.t === "expired" || message.t === "error") {
        this.ended = true;
        this.options.onMessage(message);
        this.setStatus("ended");
        socket.close(1000, "done");
        return;
      }
      this.options.onMessage(message);
    };
    socket.onerror = () => {};
    socket.onclose = (event) => {
      if (this.ended || this.socket !== socket) return;
      const final = FINAL_CLOSE[event.code];
      if (final) {
        this.finish({ t: "error", code: `closed_${event.code}`, detail: final });
        return;
      }
      void this.reconnect();
    };
  }

  private async reconnect() {
    this.setStatus("reconnecting");
    const delay = this.retryDelays[Math.min(this.attempt, this.retryDelays.length - 1)] ?? 0;
    if (this.attempt >= this.retryDelays.length) {
      this.finish({
        t: "error",
        code: "connection_lost",
        detail: "Связь с запасным экраном потеряна. Откройте вход заново.",
      });
      return;
    }
    this.attempt += 1;
    await new Promise<void>((resolve) => {
      this.timer = setTimeout(resolve, delay);
    });
    if (this.ended) return;
    await this.renewAndConnect();
  }

  private async renewAndConnect() {
    try {
      const ticket = await this.options.renew();
      if (this.ended) return;
      this.connect(ticket);
    } catch {
      if (!this.ended) void this.reconnect();
    }
  }

  /**
   * Start over now: a fresh ticket and a new socket, without spending the automatic-retry
   * budget. Used when the connection is up but the screen is silent (a slow VPN, a lost frame).
   */
  restart(): void {
    if (this.ended) return;
    clearTimeout(this.timer);
    this.attempt = 0;
    const previous = this.socket;
    this.socket = null; // the old socket's close must not start a reconnect of its own
    previous?.close(1000, "restart");
    this.setStatus("reconnecting");
    void this.renewAndConnect();
  }

  private finish(message: ServerMessage) {
    this.ended = true;
    this.options.onMessage(message);
    this.setStatus("ended");
  }

  /** Отправка сообщения; лишнее по размеру или частоте отбрасывается молча. Возвращает, ушло ли оно. */
  send(message: ClientMessage): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN || this.ended) return false;
    const text = encode(message);
    if (!text) return false;
    const now = this.now();
    this.sent = this.sent.filter((time) => now - time < 1000);
    if (message.t !== "ack" && this.sent.length >= 100) return false;
    this.sent.push(now);
    socket.send(text);
    return true;
  }

  close(): void {
    this.ended = true;
    clearTimeout(this.timer);
    this.socket?.close(1000, "closed");
    this.setStatus("ended");
  }
}
