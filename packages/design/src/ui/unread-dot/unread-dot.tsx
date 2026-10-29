import { cn } from "../../lib/cn";

export interface UnreadDotProps {
  /** Подпись для скринридера. */
  label?: string;
  className?: string;
}

/**
 * Точка «есть непрочитанное» в списке чатов, как в Codex App: заливка `accent` 8 px. Смысл несёт подпись
 * и порядок списка, цвет сам по себе его не передаёт.
 */
export function UnreadDot({ label = "Есть непрочитанное", className }: UnreadDotProps) {
  return (
    <span
      role="img"
      aria-label={label}
      data-unread-dot=""
      className={cn("inline-block size-2 shrink-0 rounded-full bg-accent", className)}
    />
  );
}
