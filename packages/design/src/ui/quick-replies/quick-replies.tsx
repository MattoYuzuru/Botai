import { cn } from "../../lib/cn";
import { Button } from "../button/button";

export const MAX_QUICK_REPLIES = 4;

export interface QuickRepliesProps {
  options: readonly string[];
  /** Нажатие отправляет текст как обычное сообщение пользователя. */
  onPick: (text: string) => void;
  disabled?: boolean;
  className?: string;
}

/** Быстрые ответы под последним ответом агента: до четырёх кнопок-пилюль, длинный текст переносится. */
export function QuickReplies({ options, onPick, disabled = false, className }: QuickRepliesProps) {
  // Одинаковые подписи схлопываются: ключ кнопки — её текст, и две одинаковые кнопки ничего не добавляют.
  const shown = [...new Set(options)].slice(0, MAX_QUICK_REPLIES);
  if (shown.length === 0) return null;
  return (
    <fieldset
      aria-label="Быстрые ответы"
      data-quick-replies=""
      className={cn("m-0 flex min-w-0 flex-wrap gap-2 border-0 p-0", className)}
    >
      {shown.map((text) => (
        <Button
          key={text}
          variant="secondary"
          disabled={disabled}
          onClick={() => onPick(text)}
          className="h-auto min-h-11 max-w-full rounded-full py-2 text-left whitespace-normal [overflow-wrap:anywhere]"
        >
          {text}
        </Button>
      ))}
    </fieldset>
  );
}
