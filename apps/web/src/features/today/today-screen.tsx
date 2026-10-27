import {
  Button,
  Card,
  countRu,
  DeadlineItem,
  EmptyState,
  formatDayWithWeekday,
  Icon,
  ScheduleDay,
  Skeleton,
  useNow,
  useOnline,
} from "@botai/design";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { Screen } from "@/app/screen";
import { resolveDataState, scheduleListState } from "@/features/data/data-state";
import { todayQuery } from "@/features/data/queries";
import { FreshnessFooter, ScreenSyncIndicator, SourceNotices } from "@/features/data/source-notices";
import { useSpaceDirectory } from "@/features/data/use-space-directory";
import { EventSheet } from "@/features/schedule/event-sheet";
import { sortEvents, toScheduleItem } from "@/features/schedule/events";
import { DeadlineSheet } from "@/features/tasks/deadline-sheet";
import { type DeadlineView, sortByDue, toDeadlineView } from "@/features/tasks/deadlines";
import { ConnectPrompt } from "./connect-prompt";

const NEAREST = 3;
const PAIRS = ["пара", "пары", "пар"] as const;

/**
 * «Сегодня» (`/`): пары сегодня из всех вузов одной лентой, три ближайших срока и полоса состояния подключений —
 * только когда что-то не в порядке (`/today.attention`). До первого вуза — приглашение подключить.
 * На ноутбуке две колонки: лента дня и сроки (styleguide §7).
 */
export function TodayScreen() {
  const directory = useSpaceDirectory();
  const online = useOnline();
  const nowMs = useNow(30_000);
  const today = useQuery({ ...todayQuery(), enabled: directory.loaded && !directory.none });
  const [eventId, setEventId] = useState<string | null>(null);
  const [deadlineId, setDeadlineId] = useState<string | null>(null);

  const fetched = today.data;
  const events = useMemo(() => sortEvents(fetched?.data.events ?? []), [fetched]);
  const deadlines = useMemo(
    () =>
      sortByDue(
        (fetched?.data.deadlines ?? []).map((row) => toDeadlineView(row, nowMs)).filter((v) => !v.done),
      ).slice(0, NEAREST),
    [fetched, nowMs],
  );

  if (!directory.loaded) {
    return (
      <Screen title="Сегодня" subtitle={formatDayWithWeekday(nowMs, nowMs, directory.homeZone)}>
        <TodaySkeleton nowMs={nowMs} />
      </Screen>
    );
  }
  if (directory.none) {
    return (
      <Screen title="Сегодня">
        <ConnectPrompt />
      </Screen>
    );
  }

  const state = resolveDataState({
    hasData: fetched !== undefined,
    isEmpty: events.length === 0 && deadlines.length === 0,
    online,
    snapshot: fetched?.snapshotAt != null,
    failed: today.isError,
    sources: directory.spaces.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: [...events, ...deadlines.map((view) => view.deadline)].some((row) => row.provenance.stale),
  });
  const timeZone = directory.homeZone;
  const scheduleState = scheduleListState(state, events.length > 0);
  const selectedEvent = events.find((row) => row.id === eventId) ?? null;
  const selectedDeadline: DeadlineView | null =
    deadlines.find((view) => view.deadline.id === deadlineId) ?? null;

  return (
    <Screen
      title="Сегодня"
      subtitle={formatDayWithWeekday(nowMs, nowMs, timeZone)}
      trailing={<ScreenSyncIndicator />}
    >
      <div data-screen="today" data-state={state} className="flex flex-col gap-6">
        <SourceNotices spaces={directory.spaces} attentionIds={fetched?.data.attention} nowMs={nowMs} />

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:items-start">
          <section aria-labelledby="today-classes" className="flex flex-col gap-3">
            <div className="flex items-baseline justify-between gap-3 px-1">
              <h2 id="today-classes" className="type-heading text-fg">
                Пары
              </h2>
              {state !== "loading" && events.length > 0 && (
                <span className="type-caption text-fg-muted">{countRu(events.length, PAIRS)}</span>
              )}
            </div>
            {scheduleState === null ? (
              <EmptyState
                icon="schedule"
                size="sm"
                as="h3"
                title="Пар в этих данных нет"
                description="Данные могут быть неполными: проверьте, когда они обновлялись."
              />
            ) : (
              <ScheduleDay
                compact
                date={nowMs}
                nowMs={nowMs}
                timeZone={timeZone}
                state={scheduleState}
                showSpace
                items={events.map((event) => toScheduleItem(event, directory.byId))}
                onOpenItem={(item) => setEventId(item.id)}
              />
            )}
          </section>

          <section aria-labelledby="today-deadlines" className="flex flex-col gap-3">
            <h2 id="today-deadlines" className="px-1 type-heading text-fg">
              Ближайшие сроки
            </h2>
            {state === "loading" ? (
              <DeadlinesSkeleton />
            ) : deadlines.length === 0 ? (
              <EmptyState
                icon="deadline"
                size="sm"
                as="h3"
                title="Ближайших сроков нет"
                description={
                  state === "ok" || state === "empty"
                    ? "Когда в источниках появятся задания со сроком, три ближайших будут здесь."
                    : "В этих данных сроков нет. Они могут быть неполными."
                }
              />
            ) : (
              <Card padding="sm" className="flex flex-col">
                <ul className="flex flex-col">
                  {deadlines.map((view) => (
                    <li key={view.deadline.id}>
                      <DeadlineItem
                        title={view.deadline.title}
                        courseTitle={view.deadline.courseTitle}
                        dueAt={view.dueMs}
                        urgency={view.urgency}
                        done={view.done}
                        spaceId={view.deadline.provenance.spaceId}
                        spaceTitle={directory.byId.get(view.deadline.provenance.spaceId)?.shortTitle}
                        showSpace
                        nowMs={nowMs}
                        timeZone={timeZone}
                        onOpen={() => setDeadlineId(view.deadline.id)}
                      />
                    </li>
                  ))}
                </ul>
                <Button variant="ghost" asChild className="self-start">
                  <Link to="/tasks">
                    Все задания
                    <Icon name="chevron-right" size={20} />
                  </Link>
                </Button>
              </Card>
            )}
          </section>
        </div>

        <FreshnessFooter spaces={directory.spaces} nowMs={nowMs} />
      </div>

      <EventSheet
        event={selectedEvent}
        events={events}
        spaces={directory.byId}
        nowMs={nowMs}
        onClose={() => setEventId(null)}
      />
      <DeadlineSheet
        view={selectedDeadline}
        spaces={directory.byId}
        nowMs={nowMs}
        onClose={() => setDeadlineId(null)}
      />
    </Screen>
  );
}

function DeadlinesSkeleton() {
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-2">
      <span className="sr-only">Загрузка сроков</span>
      {[0, 1, 2].map((row) => (
        <Skeleton key={row} className="h-14 rounded-lg" />
      ))}
    </div>
  );
}

function TodaySkeleton({ nowMs }: { nowMs: number }) {
  return (
    <div
      data-screen="today"
      data-state="loading"
      className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"
    >
      <section aria-label="Загрузка пар" className="flex flex-col gap-3">
        <Skeleton width="30%" className="h-6" />
        <ScheduleDay compact date={nowMs} nowMs={nowMs} state="loading" items={[]} />
      </section>
      <section aria-label="Загрузка сроков" className="flex flex-col gap-3">
        <Skeleton width="50%" className="h-6" />
        <DeadlinesSkeleton />
      </section>
    </div>
  );
}
