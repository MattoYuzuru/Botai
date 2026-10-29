import { queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import { isClientError } from "./errors";
import type { Connection, Space } from "./types";
import { asV3 } from "./v3-data";

/**
 * Ключи и запросы. Повторы идут в фоне с нарастающей паузой (QueryClient в main.tsx); ответ сервера
 * 4xx — не сбой связи, его не повторяем. Кнопок «Повторить» и «Обновить» в интерфейсе нет.
 */
export const queryKeys = {
  me: ["me"] as const,
  spaces: ["spaces"] as const,
  connections: (spaceId: string) => ["spaces", spaceId, "connections"] as const,
  sections: (spaceId: string) => ["spaces", spaceId, "sections"] as const,
  universities: (q: string) => ["universities", q] as const,
  today: ["today"] as const,
};

export const retryPolicy = (failureCount: number, error: unknown) =>
  !isClientError(error) && failureCount < 3;

export const meQuery = queryOptions({
  queryKey: queryKeys.me,
  queryFn: () => call(api.GET("/api/v1/me")),
  staleTime: 5 * 60_000,
});

export const spacesQuery = queryOptions({
  queryKey: queryKeys.spaces,
  queryFn: () => call(asV3<Space[]>(api.GET("/api/v1/spaces"))),
  staleTime: 60_000,
});

export const connectionsQuery = (spaceId: string) =>
  queryOptions({
    queryKey: queryKeys.connections(spaceId),
    queryFn: () =>
      call(
        asV3<Connection[]>(
          api.GET("/api/v1/spaces/{space_id}/connections", { params: { path: { space_id: spaceId } } }),
        ),
      ),
    staleTime: 30_000,
  });

export const sectionsQuery = (spaceId: string) =>
  queryOptions({
    queryKey: queryKeys.sections(spaceId),
    queryFn: () =>
      call(api.GET("/api/v1/spaces/{space_id}/sections", { params: { path: { space_id: spaceId } } })),
    staleTime: 60_000,
  });

export const universitiesQuery = (q: string) =>
  queryOptions({
    queryKey: queryKeys.universities(q),
    queryFn: () => call(api.GET("/api/v1/universities", { params: { query: { q } } })),
    staleTime: 5 * 60_000,
  });
