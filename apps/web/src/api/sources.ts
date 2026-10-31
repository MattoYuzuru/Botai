import { queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

/**
 * Источники v4 (`03-sources-and-ux.md` §6.3): типы и запросы из сгенерированного OpenAPI
 * (`schema.gen.ts`, `make gen`), без ручного fetch. UI-словарь статусов и видов — в
 * `features/study/v4/status.ts`; здесь только транспорт.
 */
type Schemas = components["schemas"];

export type SourceOut = Schemas["SourceOut"];
export type RunReportOut = Schemas["RunReportOut"];
export type PlaybookOut = Schemas["PlaybookOut"];
export type PlaybookVersionOut = Schemas["PlaybookVersionOut"];
export type ArtifactOut = Schemas["ArtifactOut"];
export type PriorityOut = Schemas["PriorityOut"];

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

/** Код и человеческое описание последней ошибки источника; секретов не содержит. */
export function sourceErrorOf(source: Pick<SourceOut, "lastError">): {
  code: string | null;
  detail: string | null;
} {
  const raw = source.lastError;
  if (!raw) return { code: null, detail: null };
  const code = typeof raw.code === "string" && raw.code ? raw.code : null;
  const detail = typeof raw.detail === "string" && raw.detail.trim() ? raw.detail.trim() : null;
  return { code, detail };
}

const path = (sourceId: string) => ({ path: { source_id: sourceId } });

export const sourceKeys = {
  all: ["sources"] as const,
  list: (spaceId?: string) => ["sources", "list", spaceId ?? "all"] as const,
  playbook: (sourceId: string) => ["sources", sourceId, "playbook"] as const,
  versions: (sourceId: string) => ["sources", sourceId, "versions"] as const,
  artifacts: (sourceId: string) => ["sources", sourceId, "artifacts"] as const,
  priority: (spaceId: string, kind: string) => ["sources", "priority", spaceId, kind] as const,
};

const SOURCES_REFRESH_MS = 30_000;

/**
 * Источники: по вузу (`spaceId`) или все. Обновляются в фоне: статус источника меняется без действий
 * человека, а кнопок «Обновить» в интерфейсе нет (styleguide §9, NFR-017).
 */
export const sourcesQuery = (spaceId?: string) =>
  queryOptions({
    queryKey: sourceKeys.list(spaceId),
    queryFn: () =>
      call(
        api.GET("/api/v1/sources", {
          params: { query: spaceId ? { space_id: spaceId } : {} },
        }),
      ),
    staleTime: 15_000,
    refetchInterval: SOURCES_REFRESH_MS,
  });

/** Playbook источника; `null` — playbook ещё не создан (404 — не ошибка экрана). */
export const sourcePlaybookQuery = (sourceId: string) =>
  queryOptions({
    queryKey: sourceKeys.playbook(sourceId),
    queryFn: async (): Promise<PlaybookOut | null> => {
      try {
        return await call(api.GET("/api/v1/sources/{source_id}/playbook", { params: path(sourceId) }));
      } catch (error) {
        if (isApiError(error) && error.status === 404) return null;
        throw error;
      }
    },
    staleTime: 60_000,
  });

/** История ревизий playbook: состояние, канареечный прогон, дата. */
export const sourceVersionsQuery = (sourceId: string) =>
  queryOptions({
    queryKey: sourceKeys.versions(sourceId),
    queryFn: () => call(api.GET("/api/v1/sources/{source_id}/playbook/versions", { params: path(sourceId) })),
    staleTime: 60_000,
  });

/** Артефакты источника — прочитанные страницы, файлы и документы с временем получения. */
export const sourceArtifactsQuery = (sourceId: string) =>
  queryOptions({
    queryKey: sourceKeys.artifacts(sourceId),
    queryFn: () =>
      call(
        api.GET("/api/v1/sources/{source_id}/artifacts", {
          params: { ...path(sourceId), query: { limit: 20 } },
        }),
      ),
    staleTime: 60_000,
  });

/** Порядок источников одного вида данных: первый живой источник решает конфликты (FR-062, D-013). */
export const priorityQuery = (spaceId: string, kind: string) =>
  queryOptions({
    queryKey: sourceKeys.priority(spaceId, kind),
    queryFn: () =>
      call(
        api.GET("/api/v1/spaces/{space_id}/priorities/{kind}", {
          params: { path: { space_id: spaceId, kind } },
        }),
      ),
    staleTime: 60_000,
  });

/** Перечитать источник: прогон активной ревизии playbook, отчёт возвращается сервером. */
export function syncSource(sourceId: string): Promise<RunReportOut> {
  return call(api.POST("/api/v1/sources/{source_id}/sync", { params: path(sourceId), body: {} }));
}

/** Починить источник: прогон с артефактами упавшего чтения и отчёт о результате. */
export function repairSource(sourceId: string): Promise<RunReportOut> {
  return call(
    api.POST("/api/v1/sources/{source_id}/repair", {
      params: path(sourceId),
      body: { language: "python", activate: false },
    }),
  );
}

/** Выключить источник: обратимо, данные скрываются, задачи останавливаются. */
export function disableSource(sourceId: string): Promise<SourceOut> {
  return call(api.POST("/api/v1/sources/{source_id}/disable", { params: path(sourceId) }));
}

/** Включить выключенный источник обратно. */
export function enableSource(sourceId: string): Promise<SourceOut> {
  return call(api.POST("/api/v1/sources/{source_id}/enable", { params: path(sourceId) }));
}

/** Сделать источник первым для вида данных (основной источник склейки). */
export function setPrimarySource(spaceId: string, kind: string, sourceIds: string[]): Promise<PriorityOut> {
  return call(
    api.PUT("/api/v1/spaces/{space_id}/priorities/{kind}", {
      params: { path: { space_id: spaceId, kind } },
      body: { sourceIds },
    }),
  );
}

/** v4-API источников выключен на этом сервере (движок v3): это не сбой, а повод к fallback. */
export function isSourcesApiUnavailable(error: unknown): boolean {
  return isApiError(error) && (error.status === 404 || error.status === 501);
}
