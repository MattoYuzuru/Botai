import {
  EmptyState,
  formatDay,
  formatDayWithWeekday,
  formatTime,
  ScheduleDay,
  Skeleton,
} from "@botai/design";
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

/** Как поиск ближайших пар: идёт, дал ответ, нет сети или не удался (прежних данных нет). */
export type UpcomingStatus = "loading" | "ready" | "offline" | "failed";

/**
 * Подпись свежести ближайших пар, как у остальных экранов данных: снимок из офлайн-кэша, неудавшееся
 * обновление при прежних данных, пары, которые сервер пометил устаревшими. Свежие данные — без подписи.
 */
export function upcomingFreshness(input: {
  events: readonly ScheduleEvent[];
  snapshotAt: number | null;
  refreshFailedAt: number | null;
  nowMs: number;
  timeZone: string;
}): string | null {
  const { events, snapshotAt, refreshFailedAt, nowMs, timeZone } = input;
  const at = (ms: number) => `${formatDay(ms, nowMs, timeZone)}, ${formatTime(ms, timeZone)}`;
  if (snapshotAt !== null) return `Нет сети. Показан снимок от ${at(snapshotAt)}`;
  if (refreshFailedAt !== null)
    return `Не удалось обновить, обновление идёт само. Показаны данные от ${at(refreshFailedAt)}`;
  const stale = events
    .filter((event) => event.provenance.stale)
    .map((event) => Date.parse(event.provenance.fetchedAt))
    .filter(Number.isFinite);
  return stale.length > 0 ? `Данные от ${formatDay(Math.min(...stale), nowMs, timeZone)}` : null;
}

/**
 * «Ближайшие пары» экрана «Сегодня» на день, когда сегодня занятий нет: день недели и дата и компактные карточки
 * пар того же вида, что в ленте дня (тип занятия, аудитория, преподаватель — пресет `eventDetails`). Поиск, который
 * идёт, — скелетон; нет сети или он не удался — словами, без вечного скелетона и без кнопки «Повторить»: обновление
 * идёт само. Устаревшие данные подписаны.
 */
export function UpcomingClasses({
  status,
  day,
  events,
  freshness,
  spaces,
  nowMs,
  timeZone,
  onOpen,
}: {
  status: UpcomingStatus;
  day: number | null;
  events: readonly ScheduleEvent[];
  freshness?: string | null;
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
      {status === "loading" ? (
        <div role="status" aria-busy="true" className="flex flex-col gap-2">
          <span className="sr-only">Загрузка ближайших пар</span>
          {[0, 1].map((row) => (
            <Skeleton key={row} className="h-16 rounded-lg" />
          ))}
        </div>
      ) : status === "offline" || status === "failed" || day === null ? (
        <EmptyState
          icon="schedule"
          size="sm"
          layout="inline"
          as="h3"
          title={status === "offline" ? "Нет сети" : "Ближайшие пары не загрузились"}
          description={
            status === "offline"
              ? "Ближайшие пары появятся, когда вернётся сеть."
              : "Обновление идёт само, как только сайт ответит."
          }
        />
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
            state={freshness ? "stale" : "ok"}
            showSpace
            freshness={freshness}
            items={events.map((event) => toScheduleItem(event, spaces))}
            onOpenItem={(item) => onOpen(item.id)}
          />
        </div>
      )}
    </section>
  );
}
