import { useOnline } from "@botai/design";
import { useMemo } from "react";
import { type DataState, resolveDataState } from "@/features/data/data-state";
import {
  type Scope,
  type SpaceDirectory,
  type SpaceInfo,
  useScope,
  useSpaceDirectory,
} from "@/features/data/use-space-directory";

/**
 * Область раздела: вуз из адреса (`?space=`) — тогда экран показывает только его, — иначе выбор в переключателе
 * (все вузы либо один). Выбор в переключателе от адреса не меняется.
 */
export function useSectionScope(spaceParam: string | undefined): { directory: SpaceDirectory; scope: Scope } {
  const directory = useSpaceDirectory();
  const active = useScope(directory);
  const scope = useMemo((): Scope => {
    const space = spaceParam ? directory.byId.get(spaceParam) : undefined;
    if (!space) return active;
    return { activeId: space.id, isAll: false, spaces: [space], spaceId: space.id, timeZone: space.timeZone };
  }, [directory.byId, spaceParam, active]);
  return { directory, scope };
}

/** Состояние экрана-списка по результату запросов и пространствам области. */
export function useSectionState(input: {
  hasData: boolean;
  isEmpty: boolean;
  snapshot: boolean;
  failed: boolean;
  staleItems: boolean;
  spaces: readonly SpaceInfo[];
}): DataState {
  const online = useOnline();
  return resolveDataState({
    hasData: input.hasData,
    isEmpty: input.isEmpty,
    online,
    snapshot: input.snapshot,
    failed: input.failed,
    sources: input.spaces.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: input.staleItems,
  });
}

/** Свежие ли данные настолько, чтобы пустой список был правдой, а не пробелом. */
export const isTrustworthy = (state: DataState): boolean => state === "ok" || state === "empty";

/** Состояние списка записей с происхождением (`provenance`): предметы, оценки, люди, новости. */
export function useRecordsState(
  result: {
    data?: { data: readonly { provenance: { stale: boolean } }[]; snapshotAt: number | null };
    isError: boolean;
  },
  spaces: readonly SpaceInfo[],
): DataState {
  const items = result.data?.data ?? [];
  return useSectionState({
    hasData: result.data !== undefined,
    isEmpty: items.length === 0,
    snapshot: result.data?.snapshotAt != null,
    failed: result.isError,
    staleItems: items.some((item) => item.provenance.stale),
    spaces,
  });
}
