import { NetworkError, parseProblem } from "@/api/errors";

/**
 * Заголовок, которым service worker помечает ответ из кэша, когда сети нет: значение — время, когда
 * снимок был получен. У живого ответа заголовка нет (features/pwa/sw.ts).
 */
export const SNAPSHOT_HEADER = "x-botai-snapshot-at";

export interface Fetched<T> {
  data: T;
  /** Время снимка, мс, если ответ взят из офлайн-кэша; у живого ответа — null. */
  snapshotAt: number | null;
}

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Как `call` из api/client, но сохраняет признак «это снимок из кэша»: экран данных обязан отличать
 * свежий ответ от последнего снимка (styleguide §9, состояния `stale` и `offline`).
 */
export async function callMeta<T>(request: Promise<Result<T>>): Promise<Fetched<T>> {
  let result: Result<T>;
  try {
    result = await request;
  } catch (cause) {
    throw new NetworkError(cause);
  }
  if (!result.response.ok) throw parseProblem(result.response.status, result.error);
  const raw = result.response.headers.get(SNAPSHOT_HEADER);
  const at = raw ? Date.parse(raw) : Number.NaN;
  return { data: result.data as T, snapshotAt: Number.isFinite(at) ? at : null };
}
