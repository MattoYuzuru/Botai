import { clearSnapshots } from "@/features/pwa/snapshots";

export interface DataReset {
  userId: string;
  resetAt: number;
}

const seen = new Map<string, number>();
const listeners = new Set<(reset: DataReset) => void>();
const queueListeners = new Set<(reset: DataReset) => void>();
const pending = new Map<string, Promise<void>>();

export function onDataReset(listener: (reset: DataReset) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Очередь очищается и в уже открытых соседних вкладках этого origin. */
export function onQueueReset(listener: (reset: DataReset) => void): () => void {
  queueListeners.add(listener);
  return () => queueListeners.delete(listener);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    const prefix = "botai.data-reset.";
    if (!event.key?.startsWith(prefix) || !event.newValue || !/^\d{13}$/.test(event.newValue)) return;
    const reset = { userId: event.key.slice(prefix.length), resetAt: Number(event.newValue) };
    for (const listener of queueListeners) listener(reset);
  });
}

/** Проверяется до возобновления очереди. Cookie входа и настройки оформления сохраняются. */
export async function observeDataReset(response: Response): Promise<void> {
  const header = response.headers.get("X-Botai-Data-Reset");
  if (!response.ok || !header || !/^\d{13}$/.test(header)) return;
  const resetAt = Number(header);
  const body: unknown = await response.clone().json();
  if (typeof body !== "object" || body === null || !("user" in body)) return;
  const user = body.user;
  if (typeof user !== "object" || user === null || !("id" in user) || typeof user.id !== "string") return;
  const userId = user.id;
  // Вкладка могла пропустить storage event или уже видеть отметку другой вкладки.
  for (const listener of queueListeners) listener({ userId, resetAt });
  const key = `botai.data-reset.${userId}`;
  if ((seen.get(userId) ?? 0) >= resetAt) return;
  try {
    if (Number(window.localStorage.getItem(key)) >= resetAt) {
      seen.set(userId, resetAt);
      return;
    }
  } catch {
    // Запрет storage не мешает очистить память и доступные снимки.
  }
  const existing = pending.get(key);
  if (existing) {
    await existing;
    return observeDataReset(response);
  }
  const clear = async () => {
    try {
      const storage = window.localStorage;
      const raw: unknown = JSON.parse(storage.getItem("botai.chat.outbox") ?? "[]");
      if (Array.isArray(raw)) {
        const kept = raw.filter(
          (item: { ownerId?: string; createdAt?: number }) =>
            item?.ownerId !== userId || (typeof item.createdAt === "number" && item.createdAt > resetAt),
        );
        if (kept.length) storage.setItem("botai.chat.outbox", JSON.stringify(kept));
        else storage.removeItem("botai.chat.outbox");
      }
    } catch {
      // Некорректная очередь не гидратируется; runtime ниже очищает свою копию.
    }
    for (const listener of listeners) listener({ userId, resetAt });
    try {
      window.sessionStorage.removeItem("botai.chat.jobs");
      window.sessionStorage.removeItem("botai.activeJob");
      window.localStorage.removeItem("botai.space");
      const owner = window.localStorage.getItem("botai.snapshot-owner");
      if (owner === null || owner === userId) await clearSnapshots();
      window.localStorage.setItem(key, String(resetAt));
    } catch {
      await clearSnapshots();
    }
    seen.set(userId, resetAt);
  };
  const operation = clear();
  pending.set(key, operation);
  try {
    await operation;
  } finally {
    pending.delete(key);
  }
}
