/**
 * Очередь исходящих сообщений чата. Сообщение, которое не ушло из-за сети, не теряется и не требует кнопки
 * «Повторить»: оно остаётся в чате с подписью «Отправится, когда появится сеть» и уходит само — по событию
 * `online` и по нарастающей паузе. Порядок сообщений сохраняется: пока первое не ушло, второе ждёт.
 *
 * Очередь живёт вне компонентов (как `JobManager`) и хранится в `localStorage`, чтобы сообщение пережило
 * перезагрузку страницы. Каждое сообщение помечено владельцем: после выхода и входа другим человеком чужие
 * сообщения не отправляются, а стираются.
 */
import { isApiError, isNetworkError } from "@/api/errors";
import type { Message, Thread } from "@/api/types";
import type { MessageAccepted } from "./queries";

export interface OutboxItem {
  /** Локальный идентификатор (до ответа сервера у сообщения своего id нет). */
  id: string;
  ownerId: string;
  /** `null` — чат ещё не создан, его создаст первая успешная попытка. */
  threadId: string | null;
  spaceId: string | null;
  text: string;
  createdAt: number;
  /** `queued` — нет сети, ждёт её; `sending` — идёт отправка или короткая пауза перед следующей попыткой. */
  status: "queued" | "sending";
  attempts: number;
  /** Не раньше этого момента пробуем снова. */
  retryAt: number | null;
  /** Запрос на отправку уже уходил: ответ мог потеряться, перед повтором проверяем, дошло ли сообщение. */
  maybeSent: boolean;
}

export type OutboxEvent =
  | { type: "thread"; item: OutboxItem; thread: Thread }
  | { type: "sent"; item: OutboxItem; threadId: string; message: Message | null; jobId: string | null }
  | { type: "rejected"; item: OutboxItem; error: unknown };

export interface OutboxDeps {
  now: () => number;
  online: () => boolean;
  /** Кто сейчас вошёл. `null` — сессии нет (очередь ждёт); сбой сети — исключение. */
  currentUserId: () => Promise<string | null>;
  createThread: (spaceId: string | null) => Promise<Thread>;
  postMessage: (threadId: string, text: string) => Promise<MessageAccepted>;
  listMessages: (threadId: string) => Promise<Message[]>;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  setTimer: (run: () => void, ms: number) => unknown;
  clearTimer: (handle: unknown) => void;
  newId: () => string;
}

export const OUTBOX_KEY = "botai.chat.outbox";
/** Пауза между попытками: 2, 4, 8 … 30 секунд. */
export const retryDelay = (attempts: number) => Math.min(2_000 * 2 ** Math.max(0, attempts - 1), 30_000);
/** Ассистент ещё отвечает на прошлое сообщение: следующая попытка скоро, без шума. */
const BUSY_DELAY_MS = 3_000;
/** Допуск расхождения часов клиента и сервера при проверке «дошло ли сообщение». */
const SKEW_MS = 2_000;

type Listener = () => void;

const textOf = (message: Message): string =>
  message.blocks
    .map((block) => (block.type === "text" && typeof block.markdown === "string" ? block.markdown : ""))
    .join("\n")
    .trim();

export class Outbox {
  private items: OutboxItem[] = [];
  private readonly listeners = new Set<Listener>();
  private readonly eventListeners = new Set<(event: OutboxEvent) => void>();
  private version = 0;
  private running = false;
  private rerun = false;
  private timer: unknown = null;

  constructor(private readonly deps: OutboxDeps) {
    this.items = this.load();
  }

  /** Снимок очереди: новый массив при каждом изменении. */
  get snapshot(): readonly OutboxItem[] {
    return this.items;
  }

  subscribe = (listener: Listener) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getVersion = () => this.version;

  on(listener: (event: OutboxEvent) => void): () => void {
    this.eventListeners.add(listener);
    return () => {
      this.eventListeners.delete(listener);
    };
  }

  private emit(event: OutboxEvent) {
    for (const listener of this.eventListeners) listener(event);
  }

  private commit(items: OutboxItem[]) {
    this.items = items;
    this.version += 1;
    this.save();
    for (const listener of this.listeners) listener();
  }

  private patch(id: string, change: Partial<OutboxItem>) {
    this.commit(this.items.map((item) => (item.id === id ? { ...item, ...change } : item)));
  }

  private remove(id: string) {
    this.commit(this.items.filter((item) => item.id !== id));
  }

  private load(): OutboxItem[] {
    try {
      const raw = this.deps.storage?.getItem(OUTBOX_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter(
          (item): item is OutboxItem =>
            typeof item === "object" &&
            item !== null &&
            typeof (item as OutboxItem).id === "string" &&
            typeof (item as OutboxItem).text === "string" &&
            typeof (item as OutboxItem).ownerId === "string",
        )
        .map((item) => ({ ...item, status: "queued" as const, retryAt: null }));
    } catch {
      return [];
    }
  }

  private save() {
    try {
      if (this.items.length === 0) this.deps.storage?.removeItem(OUTBOX_KEY);
      else this.deps.storage?.setItem(OUTBOX_KEY, JSON.stringify(this.items));
    } catch {
      /* хранилище недоступно: очередь живёт до перезагрузки страницы */
    }
  }

  /** Кладёт сообщение в очередь и пытается отправить сразу, если сеть есть. */
  send(
    text: string,
    target: { ownerId: string; threadId: string | null; spaceId: string | null },
  ): OutboxItem {
    const item: OutboxItem = {
      id: this.deps.newId(),
      ownerId: target.ownerId,
      threadId: target.threadId,
      spaceId: target.spaceId,
      text,
      createdAt: this.deps.now(),
      status: this.deps.online() ? "sending" : "queued",
      attempts: 0,
      retryAt: null,
      maybeSent: false,
    };
    this.commit([...this.items, item]);
    this.kick();
    return item;
  }

  /** Вернулась сеть, сменился чат или освободился ассистент: пробуем отправить очередь. */
  kick(): void {
    if (this.timer !== null) {
      this.deps.clearTimer(this.timer);
      this.timer = null;
    }
    // Явный запуск снимает паузу: человек не должен ждать остаток 30 секунд, когда сеть уже вернулась.
    if (this.items.some((item) => item.retryAt !== null)) {
      this.commit(this.items.map((item) => ({ ...item, retryAt: null })));
    }
    void this.process();
  }

  /** Стирает всё: выход из аккаунта. */
  clear(): void {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    this.commit([]);
  }

  discardBefore(ownerId: string, resetAt: number): void {
    this.commit(this.items.filter((item) => item.ownerId !== ownerId || item.createdAt > resetAt));
  }

  private async process(): Promise<void> {
    if (this.running) {
      this.rerun = true;
      return;
    }
    this.running = true;
    try {
      do {
        this.rerun = false;
        await this.drain();
      } while (this.rerun);
    } finally {
      this.running = false;
    }
  }

  private schedule(ms: number) {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      this.kick();
    }, ms);
  }

  private async drain(): Promise<void> {
    while (this.items.length > 0) {
      const first = this.items[0] as OutboxItem;
      const now = this.deps.now();
      if (first.retryAt !== null && first.retryAt > now) {
        this.schedule(first.retryAt - now);
        return;
      }
      if (!this.deps.online()) {
        this.markAll("queued");
        return;
      }
      let me: string | null;
      try {
        me = await this.deps.currentUserId();
      } catch {
        this.retry(first, "queued");
        return;
      }
      if (me === null) {
        // Сессии нет: сообщения ждут входа того же человека, чужие не отправляем никогда.
        this.markAll("queued");
        return;
      }
      // Проверка /me могла очистить очередь после операторского сброса данных.
      if (!this.items.some((item) => item.id === first.id)) continue;
      if (this.items.some((item) => item.ownerId !== me)) {
        this.commit(this.items.filter((item) => item.ownerId === me));
        continue;
      }
      this.patch(first.id, { status: "sending" });
      const outcome = await this.attempt(this.items.find((item) => item.id === first.id) ?? first);
      if (outcome === "retry") return;
    }
  }

  private markAll(status: OutboxItem["status"]) {
    if (this.items.every((item) => item.status === status)) return;
    this.commit(this.items.map((item) => ({ ...item, status })));
  }

  private retry(item: OutboxItem, status: OutboxItem["status"], delay = retryDelay(item.attempts + 1)) {
    this.patch(item.id, { status, attempts: item.attempts + 1, retryAt: this.deps.now() + delay });
    this.schedule(delay);
  }

  private async attempt(item: OutboxItem): Promise<"sent" | "retry" | "rejected"> {
    let threadId = item.threadId;
    try {
      if (!threadId) {
        const thread = await this.deps.createThread(item.spaceId);
        threadId = thread.id;
        this.patch(item.id, { threadId });
        this.emit({ type: "thread", item: { ...item, threadId }, thread });
      } else if (item.maybeSent) {
        // Прошлая отправка оборвалась на ответе: возможно, сообщение уже дошло. Дубль не нужен.
        const found = await this.findSent(threadId, item);
        if (found) {
          this.remove(item.id);
          this.emit({ type: "sent", item, threadId, message: found, jobId: null });
          return "sent";
        }
      }
      this.patch(item.id, { maybeSent: true });
      const accepted = await this.deps.postMessage(threadId, item.text);
      this.remove(item.id);
      this.emit({ type: "sent", item, threadId, message: accepted.message, jobId: accepted.jobId });
      return "sent";
    } catch (error) {
      if (isNetworkError(error)) {
        this.retry(this.current(item), "queued");
        return "retry";
      }
      if (isApiError(error)) {
        this.patch(item.id, { maybeSent: false });
        if (error.status === 409) {
          // Ассистент ещё отвечает на прошлое сообщение: подождать и отправить, без шума.
          this.retry(this.current(item), "sending", BUSY_DELAY_MS);
          return "retry";
        }
        if (error.status === 401) {
          this.markAll("queued");
          return "retry";
        }
        if (error.status === 429 || error.status >= 500) {
          this.retry(this.current(item), "sending");
          return "retry";
        }
      }
      // Сервер отказал по существу (чата нет, текст не принят): повторять бессмысленно.
      this.remove(item.id);
      this.emit({ type: "rejected", item, error });
      return "rejected";
    }
  }

  private current(item: OutboxItem): OutboxItem {
    return this.items.find((candidate) => candidate.id === item.id) ?? item;
  }

  private async findSent(threadId: string, item: OutboxItem): Promise<Message | null> {
    const messages = await this.deps.listMessages(threadId);
    return (
      messages.find(
        (message) =>
          message.role === "user" &&
          textOf(message) === item.text.trim() &&
          Date.parse(message.createdAt) >= item.createdAt - SKEW_MS,
      ) ?? null
    );
  }
}
