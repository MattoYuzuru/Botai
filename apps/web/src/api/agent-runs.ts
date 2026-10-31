import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { api, call } from "./client";
import { isApiError } from "./errors";
import type { components } from "./schema.gen";

/**
 * Ходы агента v4 и их трассы: `GET /api/v1/agent/runs/{id}` и `/events?afterSeq&limit`
 * (docs/v4/02-architecture.md §4.8, D-014). Типы — из сгенерированного OpenAPI, не ручные.
 *
 * Эндпоинты включены только на движке v4: на v3 они отвечают 404. Это не сбой, а отсутствие
 * возможности — экран показывает честный текст, а не скелетон навсегда.
 */
type Schemas = components["schemas"];

export type AgentRun = Schemas["AgentRunOut"];
export type AgentRunEvent = Schemas["AgentRunEventOut"];
export type AgentRunEventsPage = Schemas["AgentRunEventsOut"];

/** Сколько шагов трассы просить за одну страницу (сервер разрешает до 500). */
export const EVENTS_PAGE_SIZE = 100;

const path = (runId: string) => ({ path: { run_id: runId } });

export const agentRunKeys = {
  all: ["agent-runs"] as const,
  run: (runId: string) => ["agent-runs", runId] as const,
  events: (runId: string) => ["agent-runs", runId, "events"] as const,
};

export const isRunning = (run: Pick<AgentRun, "status"> | undefined): boolean => run?.status === "running";

/**
 * Сводка хода. Пока ход выполняется, обновляется в фоне: шаги и счётчики растут без кнопки
 * «Обновить» (NFR-017). Завершённый ход больше не опрашивается.
 */
export const agentRunQuery = (runId: string) =>
  queryOptions({
    queryKey: agentRunKeys.run(runId),
    queryFn: () => call(api.GET("/api/v1/agent/runs/{run_id}", { params: path(runId) })),
    staleTime: 5_000,
    refetchInterval: (query) => (isRunning(query.state.data) ? 2_000 : false),
  });

/**
 * Шаги трассы страницами. `afterSeq` сервер понимает включительно (`seq >= afterSeq`), поэтому
 * следующая страница начинается с `последний seq + 1` и шаги не повторяются.
 */
export const agentRunEventsQuery = (runId: string, running: boolean) =>
  infiniteQueryOptions({
    queryKey: agentRunKeys.events(runId),
    queryFn: ({ pageParam }) =>
      call(
        api.GET("/api/v1/agent/runs/{run_id}/events", {
          params: {
            ...path(runId),
            query: { afterSeq: pageParam, limit: EVENTS_PAGE_SIZE },
          },
        }),
      ),
    initialPageParam: 0,
    getNextPageParam: (last: AgentRunEventsPage) => {
      if (!last.hasMore) return undefined;
      const lastSeq = last.events.at(-1)?.seq;
      return lastSeq === undefined ? 0 : lastSeq + 1;
    },
    staleTime: 5_000,
    // Новые шаги идут по ходу хода; после завершения страница больше не дёргается.
    refetchInterval: running ? 2_000 : false,
  });

/**
 * `run_id` из результата задания v4 (`run_result_dict` в `agent/v4/job_runtime.py`): по нему
 * карточка задачи и плашка работы ведут в трассу. У прежних заданий поля нет — ссылки не будет.
 */
export function runIdFromResult(result: unknown): string | null {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const raw = (result as Record<string, unknown>).run_id;
  return typeof raw === "string" && raw.trim() ? raw.trim() : null;
}

/** Трассы нет на этом сервере (движок v3) или она чужая — 404. Это не сетевая ошибка. */
export const isAgentRunsUnavailable = (error: unknown): boolean =>
  isApiError(error) && (error.status === 404 || error.status === 501);

/** Адрес экрана трассы: ссылка из карточки задания и плашки работы. */
export const runTraceHref = (runId: string): string => `/agent/runs/${encodeURIComponent(runId.trim())}`;
