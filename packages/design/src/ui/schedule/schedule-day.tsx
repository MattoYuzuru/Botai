import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { countRu, DEFAULT_TIME_ZONE, formatDayWithWeekday } from "../../lib/format";
import { EmptyState } from "../empty-state/empty-state";
import { Skeleton } from "../skeleton/skeleton";
import { ScheduleCard } from "./schedule-card";
import type { ScheduleFact } from "./schedule-facts";
import { groupScheduleItems } from "./schedule-groups";

export interface ScheduleItem {
  id: string;
  /** Начало и конец занятия, мс. */
  start: number;
  end: number;
  title: string;
  /** Calendar date event; does not imply that the person is busy. */
  allDay?: boolean;
  /** Source of this distinct event, especially useful inside an overlap group. */
  sourceTitle?: string;
  /** Тип занятия бейджем: «Лекция» или слова источника («Текущий рубежный контроль»). */
  badge?: string | null;
  /** Детали в порядке показа (аудитория, преподаватель, адрес, группа…): их готовит пресет `eventDetails`. */
  facts?: readonly ScheduleFact[];
  spaceId: string;
  spaceTitle?: string;
  /** Занятие изменилось по сравнению с прошлым снимком. */
  changed?: boolean;
  /** Пересекается по времени с другим занятием. */
  conflict?: boolean;
  /** Источники, из которых склеена пара, основной первым; строка видна, когда их больше одного (FR-054). */
  sources?: readonly string[];
  /** Источники расходятся или пары нет в основном источнике: готовая спокойная строка под деталями. */
  note?: string | null;
}

export type { ScheduleFact };

export type ScheduleDayState = "loading" | "empty" | "ok" | "stale";

export interface ScheduleDayProps {
  /** Любой момент нужного дня, мс. */
  date: number;
  items: readonly ScheduleItem[];
  state: ScheduleDayState;
  nowMs: number;
  timeZone?: string;
  /** Метка вуза у каждой строки (сводный экран). */
  showSpace?: boolean;
  /** Компактный вид без заголовка дня — колонка недели на ноутбуке. */
  compact?: boolean;
  onOpenItem?: (item: ScheduleItem) => void;
  /** Enables shared cards for overlapping events, opening every member together. */
  onOpenGroup?: (items: readonly ScheduleItem[], date: number) => void;
  /** Подпись свежести данных под списком. */
  freshness?: ReactNode;
  className?: string;
}

const PAIRS = ["пара", "пары", "пар"] as const;

/**
 * Занятия одного дня. Состояния: loading — скелетон, empty — «Пар нет», ok, stale. Изменение — метка
 * «Изменено», пересечение по времени — метка и полоса `warning`, идущее сейчас занятие — «Идёт сейчас».
 */
export function ScheduleDay({
  date,
  items,
  state,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  showSpace = false,
  compact = false,
  onOpenItem,
  onOpenGroup,
  freshness,
  className,
}: ScheduleDayProps) {
  const sorted = [...items].sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
  return (
    <section
      aria-label={formatDayWithWeekday(date, nowMs, timeZone)}
      aria-busy={state === "loading" || undefined}
      data-state={state}
      className={cn("flex flex-col gap-3", className)}
    >
      {!compact && (
        <header className="flex items-baseline justify-between gap-3 px-1">
          <h2 className="type-heading text-fg">{formatDayWithWeekday(date, nowMs, timeZone)}</h2>
          {state !== "loading" && sorted.length > 0 && (
            <span className="type-caption text-fg-muted">{countRu(sorted.length, PAIRS)}</span>
          )}
        </header>
      )}

      {state === "loading" && (
        <div role="status" className="flex flex-col gap-2">
          <span className="sr-only">Загрузка расписания</span>
          {[0, 1, 2].map((row) => (
            <Skeleton key={row} className="h-16 rounded-lg" />
          ))}
        </div>
      )}

      {state === "empty" && <EmptyState icon="schedule" title="Пар нет" size="sm" as="h3" />}

      {(state === "ok" || state === "stale") && (
        <ol className="flex flex-col gap-2">
          {(onOpenGroup ? groupScheduleItems(sorted) : sorted.map((item) => [item])).map((group) => (
            <li key={group[0]?.id}>
              <ScheduleCard
                items={group}
                nowMs={nowMs}
                timeZone={timeZone}
                showSpace={showSpace}
                onOpenItem={onOpenItem}
                onOpenGroup={onOpenGroup ? (items) => onOpenGroup(items, date) : undefined}
              />
            </li>
          ))}
        </ol>
      )}

      {freshness && <p className="px-1 type-caption text-fg-subtle">{freshness}</p>}
    </section>
  );
}
