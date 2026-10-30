/**
 * Очередь исходящих сообщений чата. Сообщение, которое не ушло из-за сети, не теряется и не требует кнопки
 * «Повторить»: оно остаётся в чате с подписью «Отправится, когда появится сеть» и уходит само — по событию
 * `online` и по нарастающей паузе. Порядок сообщений сохраняется: пока первое не ушло, второе ждёт.
 *
 * Очередь живёт вне компонентов (как `JobManager`) и хранится в `localStorage`, чтобы сообщение пережило
 * перезагрузку страницы. Каждое сообщение помечено владельцем: после выхода и входа другим человеком чужие
 * сообщения не отправляются, а стираются.
 */
import { type ApiError, isApiError, isNetworkError } from "@/api/errors";
import type { Message, Thread } from "@/api/types";
import type { AttachmentKind, MessageAccepted } from "@/api/v3";

/** Загруженное вложение, которое уйдёт с сообщением: имя и тип нужны, чтобы показать его в ленте до ответа сервера. */
export interface OutboxAttachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: AttachmentKind;
}

/** Что уходит на сервер вместе с сообщением. `clientMessageId` — идентификатор элемента очереди (идемпотентность). */
export interface OutboxPost {
  text: string;
  attachmentIds: string[];
  clientMessageId: string;
  targetJobId?: string;
}

export interface OutboxItem {
  /** Локальный идентификатор (до ответа сервера у сообщения своего id нет). */
  id: string;
  ownerId: string;
  /** `null` — чат ещё не создан, его создаст первая успешная попытка. */
  threadId: string | null;
  spaceId: string | null;
  text: string;
  /** Вложения уже загружены (`POST /attachments`), в сообщении уходят их идентификаторы. */
  attachments: OutboxAttachment[];
  /** Ответ на вопрос задачи, которую человек видел: сервер вернёт 409, если вопрос уже закрыт. */
  targetJobId?: string;
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
  createThread: (spaceId: string | null, clientRequestId: string) => Promise<Thread>;
  postMessage: (threadId: string, post: OutboxPost) => Promise<MessageAccepted>;
  listMessages: (threadId: string) => Promise<Message[]>;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  setTimer: (run: () => void, ms: number) => unknown;
  clearTimer: (handle: unknown) => void;
  newId: () => string;
}

export const OUTBOX_KEY = "botai.chat.outbox";
/** Пауза между попытками: 2, 4, 8 … 30 секунд. */
export const retryDelay = (attempts: number) => Math.min(2_000 * 2 ** Math.max(0, attempts - 1), 30_000);
/** Ход агента ещё идёт: следующая попытка через 3, 6, 12, 24 с, и не больше пяти попыток. */
const BUSY_DELAY_MS = 3_000;
export const BUSY_MAX_ATTEMPTS = 5;

export type ConflictKind = "agent_busy" | "secret_in_chat" | "other";

/**
 * Чем кончился 409. Бэкенд различает коды: `agent_busy` — ход агента ещё идёт, `secret_in_chat` — человек написал
 * пароль или код в обычное поле. Пока код не пришёл, узнаём по тексту отказа; всё, что похоже на «защищённое поле»,
 * считается секретом: такой текст нельзя ни повторять, ни возвращать в поле.
 */
export function conflictKind(error: ApiError): ConflictKind {
  if (error.status !== 409) return "other";
  if (error.code === "secret_in_chat") return "secret_in_chat";
  if (error.code === "agent_busy") return "agent_busy";
  if (/защищённ|парол|секрет/i.test(error.detail)) return "secret_in_chat";
  if (/ещё отвечает|уже идёт/i.test(error.detail)) return "agent_busy";
  return "other";
}

/** Сообщение с секретом сервер не принял: оно удалено из очереди и из хранилища, в поле не возвращается. */
export const isSecretInChat = (error: unknown): boolean =>
  isApiError(error) && conflictKind(error) === "secret_in_chat";
type Listener = () => void;

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
        .map((item) => ({
          ...item,
          attachments: Array.isArray(item.attachments) ? item.attachments : [],
          status: "queued" as const,
          retryAt: null,
        }));
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
    target: {
      ownerId: string;
      threadId: string | null;
      spaceId: string | null;
      attachments?: readonly OutboxAttachment[];
      targetJobId?: string;
    },
  ): OutboxItem {
    const item: OutboxItem = {
      id: this.deps.newId(),
      ownerId: target.ownerId,
      threadId: target.threadId,
      spaceId: target.spaceId,
      text,
      attachments: [...(target.attachments ?? [])],
      targetJobId: target.targetJobId,
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
        const thread = await this.deps.createThread(item.spaceId, item.id);
        if (!this.items.some((candidate) => candidate.id === item.id)) return "rejected";
        threadId = thread.id;
        this.patch(item.id, { threadId });
        this.emit({ type: "thread", item: { ...item, threadId }, thread });
      } else if (item.maybeSent) {
        // Прошлая отправка оборвалась на ответе: возможно, сообщение уже дошло. Дубль не нужен.
        const found = await this.findSent(threadId, item);
        if (!this.items.some((candidate) => candidate.id === item.id)) return "rejected";
        if (found) {
          this.remove(item.id);
          this.emit({ type: "sent", item, threadId, message: found, jobId: null });
          return "sent";
        }
      }
      this.patch(item.id, { maybeSent: true });
      const accepted = await this.deps.postMessage(threadId, {
        text: item.text,
        attachmentIds: item.attachments.map((attachment) => attachment.id),
        clientMessageId: item.id,
        targetJobId: item.targetJobId,
      });
      if (!this.items.some((candidate) => candidate.id === item.id)) return "rejected";
      this.remove(item.id);
      this.emit({ type: "sent", item, threadId, message: accepted.message, jobId: accepted.jobId });
      return "sent";
    } catch (error) {
      if (!this.items.some((candidate) => candidate.id === item.id)) return "rejected";
      if (isNetworkError(error)) {
        this.retry(this.current(item), "queued");
        return "retry";
      }
      if (isApiError(error)) {
        this.patch(item.id, { maybeSent: false });
        // Ход агента ещё идёт: подождать и отправить, без шума, но не бесконечно. Всё остальное (в том числе пароль
        // в обычном поле) сервер отверг по существу: повторять нельзя, текст не должен дожидаться в хранилище.
        const current = this.current(item);
        if (conflictKind(error) === "agent_busy" && current.attempts + 1 < BUSY_MAX_ATTEMPTS) {
          this.retry(current, "sending", Math.min(BUSY_DELAY_MS * 2 ** current.attempts, 30_000));
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
      // Сервер отказал по существу (чата нет, текст не принят): повторять бессмысленно. Чат мог быть создан этой
      // же попыткой: экран узнаёт, к какому чату относится отказ, и возвращает текст в его поле.
      this.remove(item.id);
      this.emit({ type: "rejected", item: { ...item, threadId }, error });
      return "rejected";
    }
  }

  private current(item: OutboxItem): OutboxItem {
    return this.items.find((candidate) => candidate.id === item.id) ?? item;
  }

  /** Сервер хранит `clientMessageId` сообщения: дошло ли оборванное сообщение, видно по нему, а не по тексту. */
  private async findSent(threadId: string, item: OutboxItem): Promise<Message | null> {
    const messages = await this.deps.listMessages(threadId);
    return messages.find((message) => message.role === "user" && message.clientMessageId === item.id) ?? null;
  }
}
