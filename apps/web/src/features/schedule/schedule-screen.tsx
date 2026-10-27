import {
  Banner,
  Button,
  EmptyState,
  IconButton,
  ScheduleDay,
  ScheduleGrid,
  ScheduleWeek,
  Skeleton,
  useMediaQuery,
  useNow,
  useOnline,
} from "@botai/design";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Screen } from "@/app/screen";
import { ConnectedSpaceSwitcher } from "@/app/space-switcher";
import { resolveDataState, scheduleListState, toScheduleState } from "@/features/data/data-state";
import { addDays, isSameDay, startOfDay } from "@/features/data/dates";
import { scheduleQuery } from "@/features/data/queries";
import { FreshnessFooter, ScreenSyncIndicator, SourceNotices } from "@/features/data/source-notices";
import { useScope, useSpaceDirectory } from "@/features/data/use-space-directory";
import { MascotPresence } from "@/features/mascot/mascot-presence";
import { ConnectPrompt } from "@/features/today/connect-prompt";
import { EventSheet } from "./event-sheet";
import { sortEvents, toScheduleItem } from "./events";
import {
  conflictingCount,
  conflictSummary,
  eventsOnDay,
  stripDays,
  weekCountLabel,
  weekOf,
  weekRangeLabel,
} from "./week";

/**
 * «Расписание» (`/schedule`): лента дней недели и пары выбранного дня; на ноутбуке — неделя сеткой в семь
 * колонок (styleguide §7). Пересечения пар разных вузов видны в ленте, в списке и в карточке пары. Вуз —
 * переключателем в шапке. Недели листаются кнопками, соседние недели подгружаются заранее.
 */
export function ScheduleScreen() {
  const directory = useSpaceDirectory();
  const scope = useScope(directory);
  const online = useOnline();
  const nowMs = useNow(30_000);
  const wide = useMediaQuery("(min-width: 64rem)");
  const queryClient = useQueryClient();
  const timeZone = scope.timeZone;
  // `null` — «сегодня»: выбор следует за часами, пока человек не выбрал другой день.
  const [picked, setPicked] = useState<number | null>(null);
  const [eventId, setEventId] = useState<string | null>(null);

  const today = startOfDay(nowMs, timeZone);
  const day = picked ?? today;
  const range = useMemo(() => weekOf(day, timeZone), [day, timeZone]);
  const ready = directory.loaded && !directory.none;

  const week = useQuery({ ...scheduleQuery(scope.spaceId, range.start, range.end), enabled: ready });
  const fetched = week.data;
  const events = useMemo(() => sortEvents(fetched?.data ?? []), [fetched]);

  // Соседние недели подгружаются заранее: листание не показывает скелетон.
  // biome-ignore lint/correctness/useExhaustiveDependencies: зависим от границ недели и вуза, а не от клиента
  useEffect(() => {
    if (!ready) return;
    for (const shift of [-7, 7]) {
      const start = addDays(range.start, shift, timeZone);
      void queryClient.prefetchQuery(scheduleQuery(scope.spaceId, start, addDays(start, 7, timeZone)));
    }
  }, [ready, range.start, scope.spaceId, timeZone]);

  const selectedEvent = events.find((row) => row.id === eventId) ?? null;
  // Конфликтующая пара другого вуза в отфильтрованной неделе не приходит: для карточки берём сводную неделю.
  const needsPartners = !scope.isAll && (selectedEvent?.conflictsWith.length ?? 0) > 0;
  const allWeek = useQuery({
    ...scheduleQuery(undefined, range.start, range.end),
    enabled: ready && needsPartners,
  });
  const partnerPool = scope.isAll ? events : (allWeek.data?.data ?? events);

  if (!directory.loaded) {
    return (
      <Screen title="Расписание" trailing={<ConnectedSpaceSwitcher />}>
        <div data-screen="schedule" data-state="loading" className="flex flex-col gap-4">
          <Skeleton className="h-16 rounded-lg" />
          <ScheduleDay date={nowMs} nowMs={nowMs} state="loading" items={[]} />
        </div>
      </Screen>
    );
  }
  if (directory.none) {
    return (
      <Screen title="Расписание">
        <ConnectPrompt />
      </Screen>
    );
  }

  const state = resolveDataState({
    hasData: fetched !== undefined,
    isEmpty: events.length === 0,
    online,
    snapshot: fetched?.snapshotAt != null,
    failed: week.isError,
    sources: scope.spaces.map((space) => space.health.state).filter((value) => value !== "none"),
    staleItems: events.some((event) => event.provenance.stale),
  });
  const dayEvents = eventsOnDay(events, day, timeZone);
  const showSpace = directory.spaces.length > 1 && scope.isAll;
  const conflicts = conflictingCount(events);
  const weekHasPairs = events.length > 0;
  const isCurrentWeek = range.start === weekOf(today, timeZone).start;
  const dayState = scheduleListState(state, dayEvents.length > 0);

  return (
    <Screen
      title="Расписание"
      subtitle={scope.isAll ? "Все вузы" : scope.spaces[0]?.title}
      trailing={
        <>
          <ScreenSyncIndicator />
          <ConnectedSpaceSwitcher />
        </>
      }
    >
      <div data-screen="schedule" data-state={state} className="flex flex-col gap-4">
        <SourceNotices spaces={scope.spaces} nowMs={nowMs} />
        <MascotPresence screen="schedule" />

        <div className="flex items-center gap-2">
          <IconButton
            icon="chevron-left"
            label="Предыдущая неделя"
            onClick={() => setPicked(addDays(day, -7, timeZone))}
          />
          <IconButton
            icon="chevron-right"
            label="Следующая неделя"
            onClick={() => setPicked(addDays(day, 7, timeZone))}
          />
          <div className="flex min-w-0 flex-1 flex-col px-1">
            <p className="truncate type-label text-fg" aria-live="polite">
              {weekRangeLabel(range, nowMs, timeZone)}
            </p>
            {state !== "loading" && weekHasPairs && (
              <p className="type-caption text-fg-muted">{weekCountLabel(events.length)}</p>
            )}
          </div>
          {(!isCurrentWeek || !isSameDay(day, today, timeZone)) && (
            <Button variant="ghost" onClick={() => setPicked(null)}>
              Сегодня
            </Button>
          )}
        </div>

        {conflicts > 0 && (
          <Banner tone="warning" icon="warning">
            {conflictSummary(conflicts)}
          </Banner>
        )}

        {wide ? (
          state !== "loading" && !weekHasPairs && (state === "ok" || state === "empty") ? (
            <EmptyState
              icon="schedule"
              title="На этой неделе пар нет"
              description="Расписание получено, пар в нём на эти дни нет."
            />
          ) : (
            <ScheduleGrid
              days={range.days.map((date) => ({
                date,
                items: eventsOnDay(events, date, timeZone).map((event) =>
                  toScheduleItem(event, directory.byId),
                ),
              }))}
              state={toScheduleState(state)}
              nowMs={nowMs}
              timeZone={timeZone}
              showSpace={showSpace}
              onOpenItem={(item) => setEventId(item.id)}
            />
          )
        ) : (
          <>
            <ScheduleWeek
              days={stripDays(range.days, events, timeZone)}
              selected={day}
              onSelect={(date) => setPicked(startOfDay(date, timeZone))}
              nowMs={nowMs}
              timeZone={timeZone}
            />
            {dayState === null ? (
              <EmptyState
                icon="schedule"
                size="sm"
                title="Пар в этих данных нет"
                description="Данные могут быть неполными: проверьте, когда они обновлялись."
              />
            ) : (
              <ScheduleDay
                date={day}
                nowMs={nowMs}
                timeZone={timeZone}
                state={dayState}
                showSpace={showSpace}
                items={dayEvents.map((event) => toScheduleItem(event, directory.byId))}
                onOpenItem={(item) => setEventId(item.id)}
              />
            )}
          </>
        )}

        <FreshnessFooter spaces={scope.spaces} nowMs={nowMs} />
      </div>

      <EventSheet
        event={selectedEvent}
        events={partnerPool}
        spaces={directory.byId}
        nowMs={nowMs}
        onClose={() => setEventId(null)}
      />
    </Screen>
  );
}
