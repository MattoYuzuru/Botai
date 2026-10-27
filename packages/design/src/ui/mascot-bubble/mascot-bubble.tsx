import { cn } from "../../lib/cn";

/** Пузырь не длиннее одной строки: 48 знаков (mascot.md §4.3). */
export const BUBBLE_MAX_CHARS = 48;
/** Пузырь сам исчезает через 6 секунд. */
export const BUBBLE_MS = 6_000;

export interface MascotBubbleProps {
  text: string;
  /** Нажатие закрывает пузырь. */
  onClick?: () => void;
  /** Где хвостик: у правого края (над насестом) или по центру (над сценой). */
  tail?: "right" | "center";
  className?: string;
}

/**
 * Реплика Вари над насестом или сценой (mascot.md §4.3): до 48 знаков, `type-caption`, `surface-raised` и
 * `shadow-md`, хвостик к клюву. Это декор: `aria-hidden`, всё, что в нём сказано, уже есть текстом на экране
 * (стадия в `ThinkingStream`, пары на «Сегодня»), и это не ответ агента.
 */
export function MascotBubble({ text, onClick, tail = "right", className }: MascotBubbleProps) {
  const chars = [...text];
  const shown =
    chars.length > BUBBLE_MAX_CHARS
      ? `${chars
          .slice(0, BUBBLE_MAX_CHARS - 1)
          .join("")
          .trimEnd()}…`
      : text;
  return (
    <div
      aria-hidden="true"
      data-mascot-bubble=""
      onClick={onClick}
      className={cn(
        "relative w-max max-w-full animate-message-in rounded-lg border border-border bg-surface-raised px-3 py-2",
        "type-caption text-fg shadow-md",
        onClick && "cursor-pointer",
        className,
      )}
    >
      {shown}
      {/* Хвостик: повёрнутый квадрат с рамкой справа и снизу, смотрит вниз, на голову Вари. */}
      <span
        aria-hidden="true"
        className={cn(
          "absolute -bottom-1.5 size-3 rotate-45 border-r border-b border-border bg-surface-raised",
          tail === "center" ? "left-1/2 -ml-1.5" : "right-5",
        )}
      />
    </div>
  );
}
