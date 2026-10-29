import { type SwitcherSpace, UniversitySpaceSwitcher, useNow, useOnline } from "@botai/design";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { connectionsQuery, spacesQuery, universitiesQuery } from "@/api/queries";
import { CONNECT_DRAFT } from "@/features/chat/drafts";
import { useActiveSpaceId, useActiveSpaceStore } from "./active-space";
import { summarizeConnections } from "./space-status";

/** Вузы пользователя со сводным состоянием источников: по одному запросу подключений на вуз. */
export function useSwitcherSpaces(): { spaces: SwitcherSpace[]; ready: boolean } {
  const { data: spaces } = useQuery(spacesQuery);
  const { data: universities } = useQuery(universitiesQuery(""));
  const online = useOnline();
  const results = useQueries({ queries: (spaces ?? []).map((space) => connectionsQuery(space.id)) });
  const ready = Boolean(spaces) && results.every((result) => !result.isPending);
  const switcher = (spaces ?? []).map((space, index): SwitcherSpace => {
    const health = summarizeConnections(results[index]?.data ?? [], online);
    return {
      id: space.id,
      title: space.title,
      shortTitle: universities?.find((university) => university.id === space.universityId)?.shortTitle,
      hue: space.accentHue,
      status: health.state,
      updatedAt: health.updatedAt,
    };
  });
  return { spaces: switcher, ready };
}

/** Кнопка пространства для шапок «Расписания», «Заданий» и «Учёбы». Пока вузов нет — скрыта. */
export function ConnectedSpaceSwitcher() {
  const { spaces } = useSwitcherSpaces();
  const { data } = useQuery(spacesQuery);
  const activeId = useActiveSpaceId(data);
  const select = useActiveSpaceStore((state) => state.select);
  const nowMs = useNow(60_000);
  const navigate = useNavigate();
  if (spaces.length === 0) return null;
  return (
    <UniversitySpaceSwitcher
      spaces={spaces}
      activeId={activeId}
      onChange={select}
      allowAll={spaces.length > 1}
      onAdd={() => navigate({ to: "/chat", search: { draft: CONNECT_DRAFT } })}
      nowMs={nowMs}
    />
  );
}
