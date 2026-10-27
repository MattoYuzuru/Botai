import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { formatDuration } from "../../lib/format";
import { Icon } from "../icon";

export interface WaitCardProps {
  /** Что происходит и что сделать человеку: «Подтвердите вход в приложении на телефоне». */
  message: string;
  /** Когда ожидание истечёт, мс. */
  expiresAt?: number;
  nowMs: number;
  /** Варя в позе ожидания (слот; `aria-hidden`). */
  mascot?: ReactNode;
  className?: string;
}

/**
 * Ожидание действия человека на другом устройстве. Без кнопок: делать нужно не здесь.
 * Срок виден текстом («Осталось 2 мин»), обратный отсчёт скринридером не объявляется.
 */
export function WaitCard({ message, expiresAt, nowMs, mascot, className }: WaitCardProps) {
  const left = expiresAt === undefined ? null : expiresAt - nowMs;
  return (
    <div
      data-wait=""
      className={cn("flex items-center gap-4 rounded-lg border border-border bg-surface p-4", className)}
    >
      {mascot ? (
        <div aria-hidden="true" className="shrink-0">
          {mascot}
        </div>
      ) : (
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-warning-subtle text-warning-fg">
          <Icon name="phone" size={24} />
        </span>
      )}
      <div className="flex min-w-0 flex-col gap-1">
        <p className="type-subheading text-fg">{message}</p>
        {left !== null && (
          <p className="type-caption text-fg-muted" data-numeric="">
            {left > 0 ? `Осталось ${formatDuration(left)}` : "Время ожидания истекло"}
          </p>
        )}
        <p className="type-caption text-fg-muted">Жду подтверждения, дальше продолжу сам.</p>
      </div>
    </div>
  );
}
