import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { isSourcesApiUnavailable, type SourceOut, sourceErrorOf, sourcesQuery } from "@/api/sources";
import type { Connection } from "@/api/types";
import type { SpaceInfo } from "@/features/data/use-space-directory";

/**
 * Источник на экране «Учёбы»: общая форма для нового API `/api/v1/sources` (v4) и прежней модели
 * подключений `GET /spaces/{id}/connections`. Компоненты экрана не знают, откуда пришли поля.
 *
 * Прежняя модель — только честный fallback, когда v4-API на сервере выключен (404/501). Настоящая
 * ошибка (сеть, 5xx) не прячется за старыми данными: экран показывает её и продолжает фоновое
 * обновление, а не выдаёт устаревшую картину за правду.
 */
export interface StudySource {
  id: string;
  title: string;
  kinds: string[];
  status: string;
  lastSyncAt: string | null;
  /** Человеческое описание ошибки, если источник сломан; технические коды не показываются. */
  errorDetail: string | null;
  /** Код ошибки сервера — только для выбора человеческого действия в карточке. */
  errorCode: string | null;
  /** Страница интерфейса источника для «Открыть». */
  entryUrl: string | null;
  origin: string | null;
  /** Тред последней задачи источника (в прежней модели известен; в v4 появится вместе с API задач). */
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
  /** Настоящая ошибка загрузки (сеть/сервер), не отсутствие v4-API. */
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

/** Старые подключения в форме источника: fallback, пока `/api/v1/sources` на сервере выключен. */
export function connectionToStudySource(connection: Connection): StudySource {
  return {
    id: connection.id,
    title: connection.title,
    kinds: kindsOfConnection(connection),
    status: V3_STATUS[connection.status] ?? connection.status,
    lastSyncAt: connection.lastSyncAt,
    errorDetail: connection.errorDetail ?? null,
    errorCode: connection.errorCode ?? null,
    entryUrl: connection.entryUrl,
    origin: connection.origin,
    threadId: connection.threadId,
    playbookId: null,
    trust: null,
    priority: connection.scheduleRole === "primary" ? 0 : null,
    mode: "connections",
  };
}

/** Ответ v4 в форме экрана: ошибка нормализуется, поля остаются как у сервера. */
export function sourceFromApi(source: SourceOut): StudySource {
  const error = sourceErrorOf(source);
  return {
    id: source.id,
    title: source.title,
    kinds: source.kinds,
    status: source.status,
    lastSyncAt: source.lastSyncAt,
    errorDetail: error.detail,
    errorCode: error.code,
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
 * Источники одного вуза: v4-API с откатом на прежние подключения только при отсутствии v4-API.
 *
 * Откат — не ошибка экрана: на движке v3 `/api/v1/sources` отвечает 404 (`require_sources_v4`), а
 * прежняя модель подключений уже загружена `useSpaceDirectory`. Пока нет ни ответа, ни ошибки, экран
 * показывает скелетон, чтобы не выдавать «источников нет» за факт.
 */
export function useStudySources(space: SpaceInfo): StudySourcesResult {
  const enabled = Boolean(space.universityId);
  const query = useQuery({ ...sourcesQuery(space.id), enabled });

  const fallback = useMemo(() => space.connections.map(connectionToStudySource), [space.connections]);

  if (!enabled) {
    return { sources: fallback, mode: "connections", loading: false, error: null };
  }
  if (query.data) {
    return { sources: query.data.map(sourceFromApi), mode: "v4", loading: false, error: null };
  }
  if (query.isError) {
    if (isSourcesApiUnavailable(query.error)) {
      // v4-API выключен на сервере: прежние подключения — честный эквивалент, а не «пусто».
      return { sources: fallback, mode: "connections", loading: false, error: null };
    }
    return { sources: [], mode: "v4", loading: false, error: query.error };
  }
  return { sources: [], mode: "v4", loading: query.isPending, error: null };
}
