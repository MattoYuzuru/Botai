/**
 * Протокол запасного экрана входа (contracts/events.md §2). Кадры удалённого браузера и ввод человека идут
 * по WebSocket; кадры не пишутся ни на диск, ни в логи, ни в состояние приложения: каждый кадр рисуется
 * на canvas и забывается.
 */

export type NeedsKind = "mfa" | "captcha" | "choice" | "input";

export type ServerMessage =
  | { t: "frame"; seq: number; mime: string; w: number; h: number; data: string }
  | { t: "state"; url: string; title: string; loading: boolean }
  | { t: "needs"; what: NeedsKind; hint: string }
  | { t: "success" }
  | { t: "expired"; reason: string }
  | { t: "error"; code: string; detail: string };

export type ClientMessage =
  | {
      t: "mouse";
      kind: "move" | "down" | "up" | "click";
      x: number;
      y: number;
      button: "left" | "right" | "middle";
    }
  | { t: "key"; kind: "down" | "up" | "char"; code?: string; text?: string }
  | { t: "scroll"; x: number; y: number; dx: number; dy: number }
  | { t: "resize"; w: number; h: number }
  | { t: "ack"; seq: number }
  | { t: "done" };

/** Лимиты контракта: сообщение клиента ≤ 8 КБ, не больше 120 сообщений в секунду. */
export const MAX_CLIENT_MESSAGE_BYTES = 8 * 1024;
export const MAX_CLIENT_RATE_PER_S = 120;
/** Кадр больше 6 МБ в base64 не принимаем: это не кадр экрана. */
const MAX_FRAME_CHARS = 6 * 1024 * 1024;
const FRAME_MIME = new Set(["image/jpeg", "image/png", "image/webp"]);
const NEEDS: readonly string[] = ["mfa", "captcha", "choice", "input"];

type Rec = Record<string, unknown>;
const rec = (value: unknown): value is Rec =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** Сообщение сервера или `null` — всё неожиданное отбрасывается. */
export function parseServerMessage(raw: string): ServerMessage | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!rec(value)) return null;
  switch (value.t) {
    case "frame":
      if (
        finite(value.seq) &&
        finite(value.w) &&
        finite(value.h) &&
        value.w > 0 &&
        value.h > 0 &&
        value.w <= 4096 &&
        value.h <= 4096 &&
        typeof value.mime === "string" &&
        FRAME_MIME.has(value.mime) &&
        typeof value.data === "string" &&
        value.data.length <= MAX_FRAME_CHARS &&
        /^[A-Za-z0-9+/=]*$/.test(value.data)
      ) {
        return { t: "frame", seq: value.seq, mime: value.mime, w: value.w, h: value.h, data: value.data };
      }
      return null;
    case "state":
      return typeof value.url === "string"
        ? {
            t: "state",
            url: value.url,
            title: typeof value.title === "string" ? value.title : "",
            loading: value.loading === true,
          }
        : null;
    case "needs":
      return typeof value.what === "string" && NEEDS.includes(value.what)
        ? {
            t: "needs",
            what: value.what as NeedsKind,
            hint: typeof value.hint === "string" ? value.hint : "",
          }
        : null;
    case "success":
      return { t: "success" };
    case "expired":
      return { t: "expired", reason: typeof value.reason === "string" ? value.reason : "" };
    case "error":
      return {
        t: "error",
        code: typeof value.code === "string" ? value.code : "unknown",
        detail: typeof value.detail === "string" ? value.detail : "",
      };
    default:
      return null;
  }
}

export function encode(message: ClientMessage): string | null {
  const text = JSON.stringify(message);
  return new TextEncoder().encode(text).length <= MAX_CLIENT_MESSAGE_BYTES ? text : null;
}

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Точка на экране → координаты удалённой страницы. Масштаб считается по размеру кадра, а не окна. */
export function toRemotePoint(
  client: { x: number; y: number },
  rect: Rect,
  frame: { w: number; h: number },
): { x: number; y: number } {
  const x = ((client.x - rect.left) / Math.max(1, rect.width)) * frame.w;
  const y = ((client.y - rect.top) / Math.max(1, rect.height)) * frame.h;
  return {
    x: Math.round(Math.min(frame.w - 1, Math.max(0, x))),
    y: Math.round(Math.min(frame.h - 1, Math.max(0, y))),
  };
}

/**
 * Адрес сокета с одноразовым тикетом. Только тот же origin (ws/wss): блок пришёл от API, но тикет
 * не должен уйти на чужой хост, даже если блок подделан. Возвращает `null`, если адрес недопустим.
 */
export function resolveSocketUrl(wsUrl: string, ticket: string, origin: string): string | null {
  try {
    const base = new URL(origin);
    const url = new URL(wsUrl, base);
    if (url.protocol === "http:") url.protocol = "ws:";
    else if (url.protocol === "https:") url.protocol = "wss:";
    if (url.protocol !== "ws:" && url.protocol !== "wss:") return null;
    if (url.host !== base.host) return null;
    if (!url.searchParams.has("ticket")) url.searchParams.set("ticket", ticket);
    return url.toString();
  } catch {
    return null;
  }
}

/** Печатаемый символ или управляющая клавиша — как отправить на сервер. */
export function keyMessage(event: {
  key: string;
  code: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
}): ClientMessage | null {
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
    return { t: "key", kind: "char", text: event.key };
  }
  const named = new Set([
    "Backspace",
    "Enter",
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Delete",
    "Home",
    "End",
  ]);
  return named.has(event.key) ? { t: "key", kind: "down", code: event.code || event.key } : null;
}
