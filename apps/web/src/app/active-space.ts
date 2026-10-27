import { ALL_SPACES_ID } from "@botai/design";
import { create } from "zustand";
import type { Space } from "@/api/types";

const STORAGE_KEY = "botai.space";

function read(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || ALL_SPACES_ID;
  } catch {
    return ALL_SPACES_ID;
  }
}

interface ActiveSpaceState {
  /** Идентификатор пространства или `all` — сводный экран. */
  id: string;
  select: (id: string) => void;
}

export const useActiveSpaceStore = create<ActiveSpaceState>((set) => ({
  id: read(),
  select: (id) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      /* хранилище недоступно: выбор живёт до перезагрузки */
    }
    set({ id });
  },
}));

/**
 * Активное пространство с поправкой на данные: выбранного вуза больше нет — сводный экран; вуз один —
 * он и активен. При смене пространства экран обязан сбросить прежние данные и показать `loading` (styleguide §9).
 */
export function resolveActiveSpace(spaces: readonly Space[] | undefined, selected: string): string {
  if (!spaces || spaces.length === 0) return ALL_SPACES_ID;
  if (selected !== ALL_SPACES_ID && spaces.some((space) => space.id === selected)) return selected;
  return ALL_SPACES_ID;
}

export function useActiveSpaceId(spaces: readonly Space[] | undefined): string {
  const selected = useActiveSpaceStore((state) => state.id);
  return resolveActiveSpace(spaces, selected);
}
