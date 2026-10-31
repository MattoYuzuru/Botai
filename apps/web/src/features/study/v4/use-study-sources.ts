import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { type SourceV4, sourcesV4Query } from "@/api/sources-v4";
import type { Connection } from "@/api/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { useStudyUiMode } from "./flag";

/**
 * Источник на экране «Учёбы»: общая форма для нового API `/api/v1/sources` (v4) и старых
 * `GET /spaces/{id}/connections`, пока v4-эндпоинт недоступен (движок v3, OpenAPI без v4,
 * локальный стенд). Компоненты экрана не знают, откуда пришли поля.
 */
export interface StudySource {
  id: string;
  title: string;
  kinds: string[];
  status: string;
  lastSyncAt: string | null;
  /** Человеческое описание ошибки, если источник сломан; технические коды не показываются. */
  errorDetail: string | null;
  /** Страница интерфейса источника для «Открыть». */
  entryUrl: string | null;
  origin: string | null;
  /** Тред последней задачи источника (в v3 известен, в v4 — появится вместе с API задач). */
  threadId: string | null;
  playbookId: string | null;
  trust: string | null;
  priority: number | null;
  /** Откуда данные: новый API или fallback на подключения. */
  mode: "v4" | "connections";
}

export interface StudySourcesResult {
  sources: StudySource[];
  mode: "v4" | "connections";
  /** Ждём первый ответ v4-API: показываем скелетон, а не «источников нет». */
  loading: boolean;
  error: unknown;
}

const V3_STATUS: Record<string, string> = {
  draft: "draft",
  learning: "learning",
  waiting_user: "waiting_user",
  active: "active",
  syncing: "syncing",
  stale: "stale",
  needs_reauth: "needs_reauth",
  error: "error",
  disabled: "disabled",
  repairing: "probing", // «чинится» = агент работает с источником; отдельного статуса в v4-словаре UI нет
};

const V3_KINDS: Record<string, string[]> = {
  schedule: ["schedule"],
  document: ["file", "documents"],
};

function kindsOfConnection(connection: Connection): string[] {
  return V3_KINDS[connection.kind] ?? [connection.kind];
}

/** Старые подключения в форме источника: fallback, пока `/api/v1/sources` не отвечает (FR-068, миграция). */
export function connectionToStudySource(connection: Connection): StudySource {
  return {
    id: connection.id,
    title: connection.title,
    kinds: kindsOfConnection(connection),
    status: V3_STATUS[connection.status] ?? connection.status,
    lastSyncAt: connection.lastSyncAt,
    errorDetail: connection.errorDetail ?? null,
    entryUrl: connection.entryUrl,
    origin: connection.origin,
    threadId: connection.threadId,
    playbookId: null,
    trust: null,
    priority: connection.scheduleRole === "primary" ? 0 : null,
    mode: "connections",
  };
}

function sourceFromV4(source: SourceV4): StudySource {
  const error =
    typeof source.lastError?.detail === "string" && source.lastError.detail.trim()
      ? source.lastError.detail
      : null;
  return {
    id: source.id,
    title: source.title,
    kinds: source.kinds,
    status: source.status,
    lastSyncAt: source.lastSyncAt,
    errorDetail: error,
    entryUrl: source.entryUrl,
    origin: null,
    threadId: null,
    playbookId: source.playbookId,
    trust: source.trust,
    priority: source.priority,
    mode: "v4",
  };
}

/**
 * Источники одного вуза: v4-API с откатом на старые подключения.
 *
 * Откат — не ошибка экрана: на движке v3 `/api/v1/sources` отвечает 404 (`require_sources_v4`), а старая
 * модель подключений уже загружена `useSpaceDirectory`. Пока нет ни ответа, ни ошибки, экран показывает
 * скелетон, чтобы не выдавать «источников нет» за факт.
 */
export function useStudySources(space: SpaceInfo): StudySourcesResult {
  const v4 = useStudyUiMode() === "v4";
  const enabled = v4 && Boolean(space.universityId);
  const query = useQuery({ ...sourcesV4Query(space.id), enabled });

  const fallback = useMemo(() => space.connections.map(connectionToStudySource), [space.connections]);

  if (!enabled) {
    return { sources: fallback, mode: "connections", loading: false, error: null };
  }
  if (query.data) {
    return { sources: query.data.map(sourceFromV4), mode: "v4", loading: false, error: null };
  }
  if (query.isError) {
    return { sources: fallback, mode: "connections", loading: false, error: query.error };
  }
  return { sources: [], mode: "v4", loading: query.isPending, error: null };
}
