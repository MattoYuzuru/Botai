/** Имя кэша снимков: то же, что в `sw/core.ts` (API_CACHE). */
export const SNAPSHOT_CACHE = "botai-api-v1";
const OWNER_KEY = "botai.snapshot-owner";

/** Снимок данных — личные данные человека на этом устройстве: на выходе и при смене человека они стираются. */
export async function clearSnapshots(
  cacheStorage: Pick<CacheStorage, "delete"> | undefined = globalThis.caches,
): Promise<void> {
  try {
    await cacheStorage?.delete(SNAPSHOT_CACHE);
  } catch {
    /* кэш недоступен — стирать нечего */
  }
}

/**
 * Снимки принадлежат тому, кто вошёл. Если на устройстве теперь другой человек (прежний не вышел, а сессия
 * закончилась), снимки прежнего стираются до того, как они могли бы показаться.
 */
export async function keepSnapshotsForUser(
  userId: string,
  storage: Pick<Storage, "getItem" | "setItem"> = window.localStorage,
  cacheStorage: Pick<CacheStorage, "delete"> | undefined = globalThis.caches,
): Promise<boolean> {
  let owner: string | null = null;
  try {
    owner = storage.getItem(OWNER_KEY);
  } catch {
    /* хранилище недоступно */
  }
  if (owner === userId) return false;
  if (owner !== null) await clearSnapshots(cacheStorage);
  try {
    storage.setItem(OWNER_KEY, userId);
  } catch {
    /* хранилище недоступно: владелец не запомнится, снимки сотрутся на выходе */
  }
  return owner !== null;
}
