import { type FormEvent, type KeyboardEvent, type ReactNode, useRef } from "react";
import { cn } from "../../lib/cn";
import { useMediaQuery } from "../../lib/hooks";
import { useAutoGrow } from "../../lib/use-auto-grow";
import { IconButton } from "../icon-button/icon-button";

export interface ChatComposerProps {
  value: string;
  onValueChange: (value: string) => void;
  /** Отправка непустого текста. Поле очищает вызывающий код. */
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Насест Вари: стоит на верхней кромке поля справа, лапы на линии рамки (mascot.md §4.1). */
  mascotSlot?: ReactNode;
  /** Enter отправляет (по умолчанию — на устройствах с мышью); на телефоне Enter — перенос строки. */
  enterToSend?: boolean;
  /** Подпись поля для скринридера. */
  label?: string;
  className?: string;
}

/**
 * Поле ввода чата. `Textarea` 16 px с авто-ростом до 6 строк, «Отправить» — круглая `IconButton primary`.
 * Варя сидит на верхней кромке справа (`mascotSlot`) и не перекрывает текст, плейсхолдер и кнопку:
 * ей отведён отступ 16 px от правого края и место над рамкой.
 */
export function ChatComposer({
  value,
  onValueChange,
  onSend,
  disabled = false,
  placeholder = "Спросите про пары и сроки",
  mascotSlot,
  enterToSend,
  label = "Сообщение агенту",
  className,
}: ChatComposerProps) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const finePointer = useMediaQuery("(pointer: fine)");
  const sendOnEnter = enterToSend ?? finePointer;
  const resize = useAutoGrow(ref, value, 6);
  const text = value.trim();
  const canSend = !disabled && text.length > 0;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (canSend) onSend(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || !sendOnEnter) return;
    event.preventDefault();
    submit();
  };

  return (
    <form onSubmit={submit} data-composer="" className={cn("relative z-dock", className)}>
      {mascotSlot && (
        <div
          data-perch=""
          className="pointer-events-none absolute right-4 bottom-full z-dock translate-y-px *:pointer-events-auto"
        >
          {mascotSlot}
        </div>
      )}
      <div
        className={cn(
          "flex items-end gap-2 rounded-xl border border-border-strong bg-surface p-1.5 shadow-sm",
          "transition-colors duration-base ease-soft hover:border-control",
          "focus-within:border-focus-ring focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus-ring",
        )}
      >
        <textarea
          ref={ref}
          value={value}
          rows={1}
          disabled={disabled}
          aria-label={label}
          placeholder={placeholder}
          enterKeyHint={sendOnEnter ? "send" : "enter"}
          onChange={(event) => onValueChange(event.target.value)}
          onInput={resize}
          onKeyDown={onKeyDown}
          className="min-h-11 min-w-0 flex-1 resize-none bg-transparent px-3 py-2.5 type-body text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed"
        />
        <IconButton
          type="submit"
          icon="send"
          label="Отправить"
          variant="primary"
          shape="round"
          disabled={!canSend}
        />
      </div>
    </form>
  );
}
