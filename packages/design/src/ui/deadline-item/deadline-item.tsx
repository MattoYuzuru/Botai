import { cn } from "../../lib/cn";
import { DEFAULT_TIME_ZONE } from "../../lib/format";
import { type Urgency, urgencyLabel } from "../../lib/urgency";
import { Badge } from "../badge/badge";
import { Icon } from "../icon";

export interface DeadlineItemProps {
  title: string;
  /** Срок в мс или null, если его нет. */
  dueAt: number | null;
  urgency: Urgency;
  spaceId: string;
  /** Название вуза: метка источника у каждой учебной строки. */
  spaceTitle?: string;
  courseTitle?: string | null;
  done?: boolean;
  nowMs: number;
  timeZone?: string;
  /** Показывать метку вуза (на сводном экране «Все вузы» — да). */
  showSpace?: boolean;
  onOpen?: () => void;
  className?: string;
}

/**
 * Строка работы со сроком, не ниже 44 px. Срочность — текстом и меткой: `calm` нейтрально, `soon`
 * «Скоро · 2 д», `urgent` «Осталось 5 ч» заливкой, `overdue` «Просрочено» без укоров, `unknown`
 * «Срок не указан» без придуманного отсчёта. Сданное — зачёркнуто и приглушено.
 */
export function DeadlineItem({
  title,
  dueAt,
  urgency,
  spaceId,
  spaceTitle,
  courseTitle,
  done = false,
  nowMs,
  timeZone = DEFAULT_TIME_ZONE,
  showSpace = false,
  onOpen,
  className,
}: DeadlineItemProps) {
  const label = urgencyLabel(urgency, dueAt, nowMs, timeZone);
  const interactive = Boolean(onOpen);
  const Root = interactive ? "button" : "div";
  return (
    <Root
      {...(interactive ? { type: "button" as const, onClick: onOpen } : {})}
      data-urgency={done ? "done" : urgency}
      data-space-id={spaceId}
      data-done={done || undefined}
      className={cn(
        "flex min-h-14 w-full items-start gap-3 rounded-md px-3 py-3 text-left",
        interactive &&
          "cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed",
        className,
      )}
    >
      <span className="mt-0.5 shrink-0" aria-hidden="true">
        {done ? (
          <Icon name="ok" size={20} className="text-success" />
        ) : urgency === "overdue" ? (
          <Icon name="warning" size={20} className="text-danger-fg" />
        ) : urgency === "urgent" || urgency === "soon" ? (
          <Icon name="time" size={20} className="text-warning" />
        ) : (
          <Icon name="deadline" size={20} className="text-control" />
        )}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className={cn("type-subheading", done ? "text-fg-subtle line-through" : "text-fg")}>
          {title}
        </span>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
          {courseTitle && <span className="min-w-0 truncate">{courseTitle}</span>}
          <span>{done ? "Сдано" : label.due}</span>
          {showSpace && spaceTitle && <Badge tone="neutral">{spaceTitle}</Badge>}
        </span>
      </span>
      {!done && label.badge && (
        <span className="shrink-0">
          {urgency === "urgent" && (
            <Badge tone="warning" variant="solid">
              {label.badge}
            </Badge>
          )}
          {urgency === "soon" && <Badge tone="warning">{label.badge}</Badge>}
          {urgency === "overdue" && <Badge tone="danger">{label.badge}</Badge>}
        </span>
      )}
    </Root>
  );
}
