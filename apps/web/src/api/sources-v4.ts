import { queryOptions } from "@tanstack/react-query";
import { NetworkError, parseProblem } from "./errors";

/**
 * Локальные типы v4-источников до `make gen` (05-implementation-plan.md §2, 03-sources-and-ux.md §6.3).
 *
 * Бэкенд уже отдаёт `/api/v1/sources` (`backend/src/botai/sources/v4/router.py`), но `backend/openapi.json`
 * ещё не пересобран: маршрута и схем `SourceOut` в сгенерированных типах нет. Поэтому здесь только форма
 * ответа `SourceOut` (`sources/v4/schemas.py`) и один запрос; после появления OpenAPI этот файл заменяется
 * сгенерированным клиентом (`make gen`), а UI-компоненты остаются.
 */

/** Состояния источника — словарь v4 (`db.models.SOURCE_STATUSES`), UI показывает их словами и цветом. */
export type SourceStatusV4 =
  | "draft"
  | "probing"
  | "learning"
  | "waiting_user"
  | "active"
  | "syncing"
  | "stale"
  | "needs_reauth"
  | "needs_link"
  | "error"
  | "disabled"
  | "archived";

/** Природа источника: чем является точка доступа. */
export type SourceNatureKindV4 =
  | "site"
  | "portal"
  | "lms"
  | "api"
  | "calendar"
  | "sheet"
  | "doc"
  | "file"
  | "mail"
  | "feed"
  | "other";

/** Виды данных, которые источник отдаёт; у одного источника их несколько (FR-057). */
export type SourceDataKindV4 =
  | "schedule"
  | "deadlines"
  | "assignments"
  | "courses"
  | "grades"
  | "people"
  | "locations"
  | "news"
  | "documents"
  | "messages";

export type SourceKindV4 = SourceNatureKindV4 | SourceDataKindV4;

/** Ошибка последнего прогона: код и человеческое описание; секретов не содержит. */
export interface SourceErrorV4 {
  code?: string;
  detail?: string;
}

/**
 * Источник v4 (`SourceOut`): без секретов, только `authRef`-хендл Vault и `identityKey`-дайджест.
 * Поля camelCase — так их отдаёт `ApiModel` бэкенда, как и остальные ответы API.
 */
export interface SourceV4 {
  id: string;
  spaceId: string;
  universityId: string | null;
  title: string;
  entryUrl: string | null;
  kinds: SourceKindV4[];
  status: SourceStatusV4;
  playbookId: string | null;
  config: Record<string, unknown>;
  authRef: string | null;
  priority: number;
  trust: "high" | "normal" | "low";
  lastSyncAt: string | null;
  lastError: SourceErrorV4 | null;
  disabledFrom: string | null;
  createdAt: string;
  updatedAt: string;
}

export const sourcesV4Keys = {
  all: ["sources-v4"] as const,
  list: (spaceId?: string) => ["sources-v4", "list", spaceId ?? "all"] as const,
};

const SOURCES_PATH = "/api/v1/sources";

/** `GET /api/v1/sources?space_id=` без сгенерированных типов; ошибки — те же `ApiError`/`NetworkError`. */
async function fetchSources(spaceId?: string): Promise<SourceV4[]> {
  const query = spaceId ? `?space_id=${encodeURIComponent(spaceId)}` : "";
  let response: Response;
  try {
    response = await globalThis.fetch(`${SOURCES_PATH}${query}`, {
      credentials: "include",
      headers: { accept: "application/json" },
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null; // пустое тело ошибки: parseProblem даст нейтральный текст
  }
  if (!response.ok) throw parseProblem(response.status, body);
  if (!Array.isArray(body)) {
    // Не наш ответ (dev-сервер отдал index.html, прокси — чужую страницу): это повод откатиться на
    // старые подключения, а не показывать пустой список источников.
    throw new NetworkError(new TypeError("sources: unexpected response body"));
  }
  return body as SourceV4[];
}

/**
 * Источники v4: по вузу (`spaceId`) или все. 404 `not_found` (движок v3 / источник отключён флагом)
 * — повод откатиться на старые подключения, а не показывать ошибку: см. `use-study-sources.ts`.
 */
export const sourcesV4Query = (spaceId?: string) =>
  queryOptions({
    queryKey: sourcesV4Keys.list(spaceId),
    queryFn: () => fetchSources(spaceId),
    staleTime: 30_000,
  });

/** API v4 выключен на этом бэкенде (движок v3): не сбой, а повод к fallback. */
export function isSourcesV4Unavailable(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error.status === 404 || error.status === 501)
  );
}
