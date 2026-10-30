import { formatDayWithWeekday, ScheduleDay, Skeleton } from "@botai/design";
import type { ScheduleEvent } from "@/api/types";
import { startOfDay } from "@/features/data/dates";
import type { SpaceInfo } from "@/features/data/use-space-directory";
import { startMs, toScheduleItem } from "@/features/schedule/events";

/**
 * Ближайший день с парами: полночь этого дня и все его пары по времени. Дни без пар пропускаются — сегодня и
 * завтра может не быть занятий, а послезавтра они есть.
 */
export function upcomingDay(
  events: readonly ScheduleEvent[],
  timeZone: string,
): { day: number; events: ScheduleEvent[] } | null {
  const first = [...events].sort((a, b) => startMs(a) - startMs(b))[0];
  if (!first) return null;
  const day = startOfDay(startMs(first), timeZone);
  return { day, events: events.filter((event) => startOfDay(startMs(event), timeZone) === day) };
}

/**
 * «Ближайшие пары» экрана «Сегодня» на день, когда сегодня занятий нет: день недели и дата и компактные карточки
 * пар того же вида, что в ленте дня (тип занятия, аудитория, преподаватель — пресет `eventDetails`).
 */
export function UpcomingClasses({
  loading,
  day,
  events,
  spaces,
  nowMs,
  timeZone,
  onOpen,
}: {
  loading: boolean;
  day: number | null;
  events: readonly ScheduleEvent[];
  spaces: ReadonlyMap<string, SpaceInfo>;
  nowMs: number;
  timeZone: string;
  onOpen: (eventId: string) => void;
}) {
  return (
    <section aria-labelledby="today-upcoming" className="flex flex-col gap-3">
      <h2 id="today-upcoming" className="px-1 type-heading text-fg">
        Ближайшие пары
      </h2>
      {loading || day === null ? (
        <div role="status" aria-busy="true" className="flex flex-col gap-2">
          <span className="sr-only">Загрузка ближайших пар</span>
          {[0, 1].map((row) => (
            <Skeleton key={row} className="h-16 rounded-lg" />
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <h3 className="px-1 type-subheading text-fg first-letter:uppercase">
            {formatDayWithWeekday(day, nowMs, timeZone)}
          </h3>
          <ScheduleDay
            compact
            date={day}
            nowMs={nowMs}
            timeZone={timeZone}
            state="ok"
            showSpace
            items={events.map((event) => toScheduleItem(event, spaces))}
            onOpenItem={(item) => onOpen(item.id)}
          />
        </div>
      )}
    </section>
  );
}
