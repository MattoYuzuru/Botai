import { ALL_SPACES_ID } from "@botai/design";
import { queryOptions } from "@tanstack/react-query";
import { api } from "@/api/client";
import { queryKeys } from "@/api/queries";
import { callMeta } from "./fetch";

/**
 * Запросы экранов данных. Обновление идёт в фоне: раз в минуту, а при сбое или при показе снимка из
 * офлайн-кэша — чаще. Кнопок «Обновить» нет; человек видит `SyncIndicator` и подпись свежести.
 */
const POLL_MS = 60_000;
const RECOVER_MS = 20_000;

/** Интервал фонового обновления: после сбоя и пока показан снимок из кэша — чаще. */
export function backgroundInterval(query: {
  state: { status: string; fetchFailureCount: number; data: { snapshotAt: number | null } | undefined };
}): number {
  const failing = query.state.status === "error" || query.state.fetchFailureCount > 0;
  return failing || query.state.data?.snapshotAt != null ? RECOVER_MS : POLL_MS;
}

/** `all` в запросе — без фильтра по пространству. */
export const spaceParam = (spaceId: string | undefined): string | undefined =>
  spaceId && spaceId !== ALL_SPACES_ID ? spaceId : undefined;

export const dataKeys = {
  today: queryKeys.today,
  schedule: (spaceId: string | undefined, start: number, end: number) =>
    ["schedule", spaceId ?? ALL_SPACES_ID, start, end] as const,
  deadlines: (spaceId: string | undefined, includeDone: boolean) =>
    ["deadlines", spaceId ?? ALL_SPACES_ID, includeDone] as const,
  courses: (spaceId: string | undefined) => ["courses", spaceId ?? ALL_SPACES_ID] as const,
  grades: (spaceId: string | undefined) => ["grades", spaceId ?? ALL_SPACES_ID] as const,
  people: (spaceId: string | undefined) => ["people", spaceId ?? ALL_SPACES_ID] as const,
  news: (spaceId: string | undefined) => ["news", spaceId ?? ALL_SPACES_ID] as const,
  documents: (spaceId: string) => ["spaces", spaceId, "documents"] as const,
  job: (jobId: string) => ["jobs", jobId] as const,
};

/** Снимок из офлайн-кэша устаревает сразу: вернулась сеть — данные запрашиваются заново, а не ждут таймера. */
function staleAfter(query: { state: { data: { snapshotAt: number | null } | undefined } }): number {
  return query.state.data?.snapshotAt != null ? 0 : 30_000;
}

const live = { staleTime: staleAfter, refetchInterval: backgroundInterval } as const;

export const todayQuery = () =>
  queryOptions({
    queryKey: dataKeys.today,
    queryFn: () => callMeta(api.GET("/api/v1/today")),
    ...live,
  });

/** Занятия за период [start, end) из всех пространств или одного; конфликты между вузами размечает сервер. */
export const scheduleQuery = (spaceId: string | undefined, start: number, end: number) =>
  queryOptions({
    queryKey: dataKeys.schedule(spaceId, start, end),
    queryFn: () =>
      callMeta(
        api.GET("/api/v1/schedule", {
          params: {
            query: {
              start: new Date(start).toISOString(),
              end: new Date(end).toISOString(),
              space_id: spaceParam(spaceId),
            },
          },
        }),
      ),
    ...live,
  });

export const deadlinesQuery = (spaceId: string | undefined, includeDone = true) =>
  queryOptions({
    queryKey: dataKeys.deadlines(spaceId, includeDone),
    queryFn: () =>
      callMeta(
        api.GET("/api/v1/deadlines", {
          params: { query: { space_id: spaceParam(spaceId), include_done: includeDone } },
        }),
      ),
    ...live,
  });

export const coursesQuery = (spaceId: string | undefined) =>
  queryOptions({
    queryKey: dataKeys.courses(spaceId),
    queryFn: () =>
      callMeta(api.GET("/api/v1/courses", { params: { query: { space_id: spaceParam(spaceId) } } })),
    ...live,
  });

export const gradesQuery = (spaceId: string | undefined) =>
  queryOptions({
    queryKey: dataKeys.grades(spaceId),
    queryFn: () =>
      callMeta(api.GET("/api/v1/grades", { params: { query: { space_id: spaceParam(spaceId) } } })),
    ...live,
  });

export const peopleQuery = (spaceId: string | undefined) =>
  queryOptions({
    queryKey: dataKeys.people(spaceId),
    queryFn: () =>
      callMeta(api.GET("/api/v1/people", { params: { query: { space_id: spaceParam(spaceId) } } })),
    ...live,
  });

export const newsQuery = (spaceId: string | undefined) =>
  queryOptions({
    queryKey: dataKeys.news(spaceId),
    queryFn: () =>
      callMeta(api.GET("/api/v1/news", { params: { query: { space_id: spaceParam(spaceId) } } })),
    ...live,
  });

export const documentsQuery = (spaceId: string) =>
  queryOptions({
    queryKey: dataKeys.documents(spaceId),
    queryFn: () =>
      callMeta(api.GET("/api/v1/spaces/{space_id}/documents", { params: { path: { space_id: spaceId } } })),
    ...live,
  });
