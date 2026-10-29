import type { QueryClient } from "@tanstack/react-query";
import { api, call } from "@/api/client";
import { queryKeys, spacesQuery } from "@/api/queries";
import type { Space, University } from "@/api/types";
import { asV3 } from "@/api/v3-data";
import { useActiveJob } from "@/features/jobs/active-job";
import { jobManager } from "@/features/jobs/job-manager";

export interface ConnectChoice {
  /** Вуз из учебного реестра пула или `null` — студент назвал его сам. */
  university: University | null;
  /** Название вуза, как его пишет студент (для своего вуза). */
  title: string;
  /** Адрес личного кабинета — необязательный, если студент знает, откуда обычно заходит. */
  url?: string;
}

export interface StartedConnection {
  jobId: string;
  connectionId: string;
  space: Space;
}

const normalize = (value: string) => value.trim().toLowerCase().replace(/\s+/g, " ");

/**
 * Начало подключения: находим пространство этого вуза (или создаём) и заводим подключение — сервер ставит
 * в очередь задание «вход + изучение». Одно пространство на вуз: второй источник того же вуза добавляется
 * в то же пространство, а не плодит новое.
 */
export async function startConnection(
  queryClient: QueryClient,
  choice: ConnectChoice,
): Promise<StartedConnection> {
  const title = choice.university?.title ?? choice.title.trim();
  const spaces = await queryClient.fetchQuery({ ...spacesQuery, staleTime: 0 });
  const existing = spaces.find(
    (space) =>
      (choice.university && space.universityId === choice.university.id) ||
      normalize(space.title) === normalize(title),
  );
  const space =
    existing ??
    (await call(
      asV3<Space>(
        api.POST("/api/v1/spaces", {
          body: { title, universityId: choice.university?.id ?? null },
        }),
      ),
    ));

  const started = await call(
    api.POST("/api/v1/spaces/{space_id}/connections", {
      params: { path: { space_id: space.id } },
      body: { title: "Личный кабинет", kind: "portal", url: choice.url?.trim() || null },
    }),
  );

  const shortTitle = choice.university?.shortTitle ?? title;
  useActiveJob.getState().set({
    jobId: started.jobId,
    title: `Подключаю ${shortTitle}`,
    spaceTitle: shortTitle,
    connectionId: started.connection.id,
  });
  jobManager.start(started.jobId);
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.spaces }),
    queryClient.invalidateQueries({ queryKey: queryKeys.connections(space.id) }),
  ]);
  return { jobId: started.jobId, connectionId: started.connection.id, space };
}
