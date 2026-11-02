import { cn } from "../../lib/cn";
import { countRu, formatTime } from "../../lib/format";
import { Badge } from "../badge/badge";
import type { ScheduleItem } from "./schedule-day";
import { KIND_BADGE, ScheduleFacts, ScheduleProvenance } from "./schedule-facts";

/** One button for simultaneous events; each event and its provenance remain distinct. */
export function ScheduleCard({
  items,
  nowMs,
  timeZone,
  showSpace,
  compact = false,
  onOpenItem,
  onOpenGroup,
}: {
  items: readonly ScheduleItem[];
  nowMs: number;
  timeZone: string;
  showSpace: boolean;
  compact?: boolean;
  onOpenItem?: (item: ScheduleItem) => void;
  onOpenGroup?: (items: readonly ScheduleItem[]) => void;
}) {
  const first = items[0];
  if (!first) return null;
  const grouped = items.length > 1;
  const interactive = !!(onOpenItem || onOpenGroup);
  const Row = interactive ? "button" : "div";
  const conflict = items.some((item) => item.conflict);
  const live = items.some((item) => !item.allDay && item.start <= nowMs && nowMs < item.end);
  return (
    <Row
      {...(interactive
        ? {
            type: "button" as const,
            onClick: () =>
              onOpenGroup && (grouped || !onOpenItem) ? onOpenGroup(items) : onOpenItem?.(first),
          }
        : {})}
      data-space-id={first.spaceId}
      data-conflict={conflict || undefined}
      data-live={live || undefined}
      data-event-count={items.length}
      data-schedule-card={compact ? "compact" : "expanded"}
      className={cn(
        "schedule-card flex min-h-16 w-full min-w-0 flex-col rounded-lg border border-border bg-surface text-left",
        compact ? "p-2" : "px-3 py-3",
        interactive &&
          "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
        conflict && "border-warning",
        live && "bg-accent-subtle",
      )}
    >
      {grouped && (
        <span className="mb-2 type-caption text-fg-muted" data-part="group-label">
          {countRu(items.length, ["событие", "события", "событий"])} · в одно время
        </span>
      )}
      <span className="flex w-full min-w-0 flex-col gap-2">
        {items.map((item) => {
          const current = !item.allDay && item.start <= nowMs && nowMs < item.end;
          return (
            <span
              key={item.id}
              data-event-id={item.id}
              className={cn(
                "flex min-w-0 gap-3",
                compact && "flex-col gap-1",
                grouped && "border-t border-border pt-2 first:border-0 first:pt-0",
              )}
            >
              <span
                className={cn(
                  "shrink-0 text-fg-muted",
                  compact ? "flex flex-wrap gap-x-1 type-caption" : "flex w-12 flex-col type-mono",
                )}
                data-numeric=""
              >
                {item.allDay ? (
                  <span className="type-caption">Весь день</span>
                ) : (
                  <>
                    <time dateTime={new Date(item.start).toISOString()}>
                      {formatTime(item.start, timeZone)}
                    </time>
                    {compact && <span aria-hidden="true">–</span>}
                    <time dateTime={new Date(item.end).toISOString()} className="text-fg-subtle">
                      {formatTime(item.end, timeZone)}
                    </time>
                  </>
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-1">
                <span
                  className={cn(
                    "schedule-card-title break-words text-fg",
                    compact ? "type-label" : "type-subheading",
                  )}
                >
                  {item.title}
                </span>
                <span data-part="event-preview" className="schedule-card-details grid">
                  <span className="min-h-0 overflow-hidden">
                    <span className="flex flex-col gap-1">
                      {item.badge && (
                        <Badge tone="info" className={cn(KIND_BADGE, "self-start")}>
                          {item.badge}
                        </Badge>
                      )}
                      <ScheduleFacts facts={item.facts} layout={compact ? "stacked" : "inline"} />
                      {grouped && item.sourceTitle && !item.sources?.length && (
                        <span className="type-caption text-fg-muted">{item.sourceTitle}</span>
                      )}
                      <ScheduleProvenance sources={item.sources} note={item.note} />
                    </span>
                  </span>
                </span>
                <span className="flex flex-wrap gap-1 empty:hidden">
                  {current && <Badge tone="accent">Идёт сейчас</Badge>}
                  {item.conflict && (
                    <Badge tone="warning" icon="warning">
                      {compact ? "Пересечение" : "Пересекается по времени"}
                    </Badge>
                  )}
                  {item.changed && (
                    <Badge tone="warning" icon="edit">
                      Изменено
                    </Badge>
                  )}
                  {showSpace && item.spaceTitle && <Badge tone="neutral">{item.spaceTitle}</Badge>}
                </span>
              </span>
            </span>
          );
        })}
      </span>
    </Row>
  );
}
