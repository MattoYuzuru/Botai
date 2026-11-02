import {
  Button,
  Card,
  countRu,
  DeadlineItem,
  EmptyState,
  Icon,
  Skeleton,
  SourceStatusBar,
  useNow,
  useOnline,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { meQuery } from "@/api/queries";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { resolveDataState } from "@/features/data/data-state";
import { deadlinesQuery } from "@/features/data/queries";
import { ScreenSyncIndicator, SourceNotices, sourceStatusItems } from "@/features/data/source-notices";
import { useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { MascotPresence } from "@/features/mascot/mascot-presence";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { DeadlineSheet } from "./deadline-sheet";
import { toDeadlineView } from "./deadlines";
import { GROUP_NOTE, groupDeadlines } from "./groups";

const PAGE_SIZE = 10;
const TASKS = ["задание", "задания", "заданий"] as const;

/**
 * «Задания» (`/tasks`): дедлайны и домашки по группам «Просрочено / Сегодня / На неделе / Позже / Без срока /
 * Сдано». Срочность считает `urgency.ts`; у задания без срока честное «Срок не указан» и никакого отсчёта.
 * Вуз выбирается переключателем в шапке, у каждой строки — метка вуза, у списка — подпись свежести.
 */
export function TasksScreen() {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const online = useOnline();
  const me = useQuery(meQuery);
  const listKey = `${me.data?.user.id ?? ""}:${scope.activeId}`;
  const nowMs = useNow(30_000);
  const ready = directory.loaded && !directory.none;
  const query = useQuery({ ...deadlinesQuery(scope.spaceId, true), enabled: ready });
  const [openId, setOpenId] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [revealed, setRevealed] = useState<{ key: string; counts: Record<string, number> }>({
    key: listKey,
    counts: {},
  });
  const counts = revealed.key === listKey ? revealed.counts : {};

  useEffect(() => {
    setOpenId(null);
    setShowDone(false);
    setRevealed({ key: listKey, counts: {} });
  }, [listKey]);

  const fetched = query.data;
  const timeZone = scope.timeZone;
  const views = useMemo(
    () => (fetched?.data ?? []).map((row) => toDeadlineView(row, nowMs)),
    [fetched, nowMs],
  );
  const groups = useMemo(() => groupDeadlines(views, nowMs, timeZone), [views, nowMs, timeZone]);

  const trailing = (
    <>
      <ScreenSyncIndicator />
      <ConnectedSpaceSwitcher />
    </>
  );

  if (!directory.loaded) {
    return (
      <Screen title="Задания" trailing={trailing}>
        <div data-screen="tasks" data-state="loading">
          <TasksSkeleton />
        </div>
      </Screen>
    );
  }
  if (directory.none) {
    return (
      <Screen title="Задания">
        <ConnectPrompt />
      </Screen>
    );
  }

  const state = resolveDataState({
    hasData: fetched !== undefined,
    isEmpty: views.length === 0,
    online,
    snapshot: fetched?.snapshotAt != null,
    failed: query.isError,
    sources: scope.spaces.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: views.some((view) => view.deadline.provenance.stale),
  });
  const showSpace = scope.isAll && directory.spaces.length > 1;
  const open = views.filter((view) => !view.done).length;
  const selected = views.find((view) => view.deadline.id === openId) ?? null;

  return (
    <Screen
      title="Задания"
      subtitle={scope.isAll ? "Все вузы" : scope.spaces[0]?.title}
      meta={<SourceStatusBar items={sourceStatusItems(scope.spaces)} nowMs={nowMs} />}
      trailing={trailing}
    >
      <div data-screen="tasks" data-state={state} className="flex flex-col gap-6">
        <SourceNotices spaces={scope.spaces} nowMs={nowMs} />
        <MascotPresence screen="deadlines" />

        {state === "loading" ? (
          <TasksSkeleton />
        ) : views.length === 0 ? (
          <EmptyState
            icon="deadline"
            title="Заданий пока нет"
            description={
              state === "ok" || state === "empty"
                ? "Когда в источниках появятся задания, они соберутся здесь по срокам."
                : "В этих данных заданий нет. Они могут быть неполными: проверьте, когда они обновлялись."
            }
          />
        ) : (
          <>
            {open === 0 && (
              <p className="px-1 type-ui text-fg-muted">
                Заданий, которые нужно сдать, в источниках нет. Сданные — ниже.
              </p>
            )}
            {groups.map((group) => {
              const done = group.group === "done";
              const collapsed = done && !showDone;
              const visibleCount = counts[group.group] ?? PAGE_SIZE;
              const listId = `tasks-${group.group}-list`;
              return (
                <section
                  key={group.group}
                  aria-labelledby={`tasks-${group.group}`}
                  data-group={group.group}
                  className="flex flex-col gap-3"
                >
                  <div className="flex items-center justify-between gap-3 px-1">
                    <h2 id={`tasks-${group.group}`} className="type-heading text-fg">
                      {group.title}
                      <span className="ml-2 type-caption text-fg-muted">{group.views.length}</span>
                    </h2>
                    {done && (
                      <Button
                        variant="ghost"
                        aria-expanded={showDone}
                        aria-controls="tasks-done-list"
                        onClick={() => setShowDone((value) => !value)}
                      >
                        {showDone ? "Скрыть" : `Показать ${countRu(group.views.length, TASKS)}`}
                        <Icon name={showDone ? "chevron-up" : "chevron-down"} size={20} />
                      </Button>
                    )}
                  </div>
                  {GROUP_NOTE[group.group] && (
                    <p className="px-1 type-caption text-fg-muted">{GROUP_NOTE[group.group]}</p>
                  )}
                  {!collapsed && (
                    <Card padding="sm" id={listId}>
                      <ul className="flex flex-col">
                        {group.views.slice(0, visibleCount).map((view) => (
                          <li key={view.deadline.id}>
                            <DeadlineItem
                              title={view.deadline.title}
                              courseTitle={view.deadline.courseTitle}
                              dueAt={view.dueMs}
                              urgency={view.urgency}
                              done={view.done}
                              spaceId={view.deadline.provenance.spaceId}
                              spaceTitle={directory.byId.get(view.deadline.provenance.spaceId)?.shortTitle}
                              showSpace={showSpace}
                              nowMs={nowMs}
                              timeZone={
                                directory.byId.get(view.deadline.provenance.spaceId)?.timeZone ?? timeZone
                              }
                              onOpen={() => setOpenId(view.deadline.id)}
                            />
                          </li>
                        ))}
                      </ul>
                      {visibleCount < group.views.length && (
                        <Button
                          variant="ghost"
                          className="mt-2 w-full"
                          aria-controls={listId}
                          onClick={() =>
                            setRevealed({
                              key: listKey,
                              counts: { ...counts, [group.group]: visibleCount + PAGE_SIZE },
                            })
                          }
                        >
                          Загрузить ещё
                        </Button>
                      )}
                    </Card>
                  )}
                </section>
              );
            })}
          </>
        )}
      </div>

      <DeadlineSheet view={selected} spaces={directory.byId} nowMs={nowMs} onClose={() => setOpenId(null)} />
    </Screen>
  );
}

function TasksSkeleton() {
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-3">
      <span className="sr-only">Загрузка заданий</span>
      <Skeleton width="30%" className="h-6" />
      {[0, 1, 2, 3].map((row) => (
        <Skeleton key={row} className="h-14 rounded-lg" />
      ))}
    </div>
  );
}
