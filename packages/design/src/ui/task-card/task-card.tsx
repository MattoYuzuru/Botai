import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Badge, type BadgeProps } from "../badge/badge";
import { Card } from "../card/card";
import { Icon, type IconName } from "../icon";

export type TaskKind = "connect" | "repair" | "sync" | "ingest";
export type TaskState = "waiting_user" | "running" | "done" | "failed" | "queued" | "cancelled";

export interface TaskCardProps {
  title: string;
  kind: TaskKind;
  state: TaskState;
  /** Задача идёт в этом чате: подписи «в другом чате» и действия нет. */
  here?: boolean;
  /** Слот приложения: кнопка-ссылка «Открыть чат» (роутер знает приложение, не дизайн-система). */
  action?: ReactNode;
  className?: string;
}

const KINDS: Record<TaskKind, { icon: IconName; label: string }> = {
  connect: { icon: "connection", label: "Подключение" },
  repair: { icon: "repairing", label: "Починка" },
  sync: { icon: "sync", label: "Обновление" },
  ingest: { icon: "document", label: "Индексация" },
};

const STATES: Record<TaskState, { tone: NonNullable<BadgeProps["tone"]>; icon: IconName; label: string }> = {
  waiting_user: { tone: "warning", icon: "waiting", label: "Ждёт вас" },
  running: { tone: "info", icon: "sync", label: "Работает" },
  done: { tone: "success", icon: "ok", label: "Готово" },
  failed: { tone: "danger", icon: "error", label: "Не удалось" },
  queued: { tone: "neutral", icon: "pending", label: "В очереди" },
  cancelled: { tone: "neutral", icon: "close", label: "Отменено" },
};

/**
 * Карточка фоновой задачи в ленте чата: подключение, починка, обновление, индексация. Если задача идёт
 * в другом чате, карточка говорит об этом и ведёт туда через слот `action`; у задачи этого чата в слоте —
 * её действие («Отменить подключение»).
 */
export function TaskCard({ title, kind, state, here = true, action, className }: TaskCardProps) {
  const meta = KINDS[kind];
  const status = STATES[state];
  return (
    <Card
      data-task-card=""
      data-kind={kind}
      data-state={state}
      padding="md"
      className={cn("flex flex-col gap-3", className)}
    >
      <div className="flex items-start gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-md bg-pressed text-fg-muted">
          <Icon name={meta.icon} size={20} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="type-caption text-fg-muted">{meta.label}</span>
          <span className="type-subheading text-fg [overflow-wrap:anywhere]">{title}</span>
        </div>
        <Badge tone={status.tone} icon={status.icon}>
          {status.label}
        </Badge>
      </div>
      {(!here || action) && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          {!here && <span className="type-caption text-fg-muted">Идёт в другом чате</span>}
          {action && <span className="ml-auto">{action}</span>}
        </div>
      )}
    </Card>
  );
}
