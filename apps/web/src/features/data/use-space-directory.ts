import { ALL_SPACES_ID, DEFAULT_TIME_ZONE, useOnline } from "@botai/design";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { connectionsQuery, spacesQuery, universitiesQuery } from "@/api/queries";
import type { Connection, Space, University } from "@/api/types";
import { useActiveSpaceStore } from "@/app/active-space";
import { type SpaceHealth, summarizeConnections } from "@/app/space-status";

export interface SpaceInfo {
  id: string;
  /** «НИУ ВШЭ»: название вуза, по которому названо пространство. */
  title: string;
  /** «ВШЭ»: короткое имя пространства, иначе из справочника вузов, иначе его название. */
  shortTitle: string;
  timeZone: string;
  hue: number;
  universityId: string | null;
  connections: readonly Connection[];
  health: SpaceHealth;
}

export interface SpaceDirectory {
  spaces: readonly SpaceInfo[];
  byId: ReadonlyMap<string, SpaceInfo>;
  /** Список пространств получен. */
  loaded: boolean;
  /** Подключения всех пространств получены хотя бы раз. */
  connectionsLoaded: boolean;
  /** Пространств нет вовсе: экран показывает «Подключить вуз». */
  none: boolean;
  /** Пояс для «сегодня» на сводном экране — пояс самого старого пространства, как у сервера. */
  homeZone: string;
}

const CONNECTIONS_REFRESH_MS = 30_000;

/** Короткое имя вуза для чипов и подписей: `shortTitle` пространства, справочник, название. Адрес сайта — не имя. */
export function spaceShortTitle(space: Space, universities: readonly University[] | undefined): string {
  return (
    space.shortTitle ||
    universities?.find((university) => university.id === space.universityId)?.shortTitle ||
    space.title
  );
}

/**
 * Только пространства-вузы: «Мои материалы» больше нет как единицы интерфейса (D-013,
 * 03-sources-and-ux.md §1.6). Сервер до миграции может их знать — интерфейс их не показывает.
 */
export const universitySpaces = (spaces: readonly Space[] | undefined): Space[] | undefined =>
  spaces?.filter((space) => space.universityId !== null);

const combineConnections = (results: readonly { data: Connection[] | undefined; isPending: boolean }[]) => ({
  lists: results.map((result) => result.data),
  pending: results.some((result) => result.isPending),
});

/**
 * Вузы пользователя вместе с подключениями и сводным состоянием источников. Подключения обновляются
 * в фоне: статус «обновляется» сменяется на «работает» без действий человека.
 *
 * Пространства без вуза («Мои материалы») в интерфейс не попадают: в v4 вуз — единственная единица
 * экрана, а файл и документ — источник вуза (D-013, 03-sources-and-ux.md §1.6). Прежняя модель ещё
 * может их знать до миграции — фильтр снимается вместе с ней.
 */
export function useSpaceDirectory(): SpaceDirectory {
  const spacesResult = useQuery(spacesQuery);
  const universities = useQuery(universitiesQuery(""));
  const online = useOnline();
  const spaces = useMemo(() => universitySpaces(spacesResult.data), [spacesResult.data]);
  const connections = useQueries({
    queries: (spaces ?? []).map((space) => ({
      ...connectionsQuery(space.id),
      refetchInterval: CONNECTIONS_REFRESH_MS,
    })),
    combine: combineConnections,
  });

  return useMemo(() => {
    const infos = (spaces ?? []).map((space, index): SpaceInfo => {
      const list = connections.lists[index] ?? [];
      return {
        id: space.id,
        title: space.title,
        shortTitle: spaceShortTitle(space, universities.data),
        timeZone: space.timeZone || DEFAULT_TIME_ZONE,
        hue: space.accentHue,
        universityId: space.universityId,
        connections: list,
        health: summarizeConnections(list, online),
      };
    });
    return {
      spaces: infos,
      byId: new Map(infos.map((info) => [info.id, info])),
      loaded: spacesResult.data !== undefined,
      connectionsLoaded: spacesResult.data !== undefined && !connections.pending,
      none: spacesResult.data !== undefined && infos.length === 0,
      homeZone: infos[0]?.timeZone ?? DEFAULT_TIME_ZONE,
    };
  }, [spaces, spacesResult.data, universities.data, connections, online]);
}

export interface Scope {
  /** Выбор в переключателе: `all` или id пространства. */
  activeId: string;
  isAll: boolean;
  /** Пространства, чьи данные показываются на экране. */
  spaces: readonly SpaceInfo[];
  /** Параметр `space_id` для запросов; у сводного экрана его нет. */
  spaceId: string | undefined;
  /** Пояс для календарных вычислений экрана. */
  timeZone: string;
}

/** Область экрана: выбранный в переключателе вуз либо все вузы. Один вуз — это и есть «все». */
export function useScope(directory: SpaceDirectory): Scope {
  const selected = useActiveSpaceStore((state) => state.id);
  return useMemo(() => {
    // Выбранного вуза больше нет — сводный экран.
    const resolved = directory.byId.has(selected) ? selected : ALL_SPACES_ID;
    const isAll = resolved === ALL_SPACES_ID;
    const spaces = isAll ? directory.spaces : directory.spaces.filter((space) => space.id === resolved);
    return {
      activeId: resolved,
      isAll,
      spaces,
      spaceId: isAll ? undefined : resolved,
      timeZone: isAll ? directory.homeZone : (spaces[0]?.timeZone ?? directory.homeZone),
    };
  }, [directory, selected]);
}
