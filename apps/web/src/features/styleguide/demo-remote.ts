import { resolveColorToken } from "@botai/design";
import type { SocketLike } from "@/features/remote-screen/remote-session";

type Rec = Record<string, unknown>;

/**
 * Сервер запасного экрана для витрины и e2e: рисует «страницу входа вуза» в кадры JPEG и отвечает на ввод
 * так, как отвечает настоящий: клик ставит фокус в поле, символы дописываются, «Я вошёл» заканчивается `success`.
 * Сети нет, значения никуда не уходят; рисуется цветами токенов текущей темы.
 */
export class DemoRemoteSocket implements SocketLike {
  static instances: DemoRemoteSocket[] = [];
  readyState = 0;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  /** Принятые сообщения клиента — для проверок в e2e. */
  readonly received: Rec[] = [];
  private w = 390;
  private h = 560;
  private seq = 0;
  private focus: 0 | 1 | 2 = 0;
  private readonly fields = ["", ""];
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(readonly url: string) {
    DemoRemoteSocket.instances.push(this);
    this.timer = setTimeout(() => {
      this.readyState = 1;
      this.onopen?.({});
      this.say({ t: "state", url: "https://lk.fakeuni.test/login", title: "Вход", loading: false });
      this.say({ t: "needs", what: "captcha", hint: "" });
      this.frame();
    }, 30);
  }

  send(data: string): void {
    const message = JSON.parse(data) as Rec;
    this.received.push(message);
    switch (message.t) {
      case "resize":
        this.w = Math.max(240, Math.min(1280, Number(message.w) || this.w));
        this.h = Math.max(320, Math.min(1000, Number(message.h) || this.h));
        this.frame();
        break;
      case "mouse":
        if (message.kind === "click" || message.kind === "down") {
          const y = Number(message.y);
          this.focus =
            y > this.h * 0.2 && y < this.h * 0.4 ? 1 : y >= this.h * 0.4 && y < this.h * 0.6 ? 2 : 0;
          this.frame();
        }
        break;
      case "key":
        if (this.focus > 0) {
          const index = this.focus - 1;
          if (message.kind === "char" && typeof message.text === "string") this.fields[index] += message.text;
          if (message.kind === "down" && message.code === "Backspace")
            this.fields[index] = (this.fields[index] ?? "").slice(0, -1);
          this.frame();
        }
        break;
      case "done":
        setTimeout(() => this.say({ t: "success" }), 20);
        break;
    }
  }

  close(): void {
    clearTimeout(this.timer);
    this.readyState = 3;
  }

  private say(message: Rec) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  private frame() {
    const canvas = document.createElement("canvas");
    canvas.width = this.w;
    canvas.height = this.h;
    const context = canvas.getContext?.("2d");
    if (!context) return;
    const color = (token: string) => resolveColorToken(token);
    context.fillStyle = color("--color-surface");
    context.fillRect(0, 0, this.w, this.h);
    context.fillStyle = color("--color-fg");
    const scale = Math.min(this.w, 480) / 390;
    context.font = `600 ${Math.round(22 * scale)}px sans-serif`;
    context.fillText("Личный кабинет", 24, this.h * 0.12);
    ["Логин", "Пароль"].forEach((label, index) => {
      const top = this.h * (0.2 + index * 0.2);
      context.fillStyle = color("--color-fg-muted");
      context.font = `${Math.round(15 * scale)}px sans-serif`;
      context.fillText(label, 24, top);
      context.strokeStyle =
        this.focus === index + 1 ? color("--color-focus-ring") : color("--color-border-strong");
      context.lineWidth = this.focus === index + 1 ? 3 : 1;
      context.strokeRect(24, top + 10, this.w - 48, this.h * 0.09);
      context.fillStyle = color("--color-fg");
      // design-lint-ignore: символ пароля на нарисованной странице вуза, это не иконка интерфейса
      const text = index === 1 ? "•".repeat((this.fields[1] ?? "").length) : (this.fields[0] ?? "");
      context.fillText(text, 36, top + 10 + this.h * 0.06);
    });
    context.fillStyle = color("--color-accent");
    context.fillRect(24, this.h * 0.64, this.w - 48, this.h * 0.09);
    const data = canvas.toDataURL("image/jpeg", 0.7).split(",")[1];
    if (!data) return;
    this.say({ t: "frame", seq: ++this.seq, mime: "image/jpeg", w: this.w, h: this.h, data });
  }
}

export const createDemoSocket = (url: string): SocketLike => new DemoRemoteSocket(url);
