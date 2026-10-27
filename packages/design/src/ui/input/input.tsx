import { type ComponentProps, type PointerEvent, type ReactNode, useRef } from "react";
import { cn } from "../../lib/cn";
import { mergeRefs } from "../../lib/merge-refs";
import { useFieldControl } from "../field/field";
import { Icon, type IconName } from "../icon";
import { controlFrame } from "../styles";

export interface InputProps extends Omit<ComponentProps<"input">, "size"> {
  /** Иконка внутри поля слева. */
  leadingIcon?: IconName;
  /** Элемент справа: `IconButton` показа пароля, единица измерения. */
  trailing?: ReactNode;
  invalid?: boolean;
  /** Классы рамки (ширина). `className` применяется к самому `<input>`. */
  wrapperClassName?: string;
}

/**
 * Однострочное поле, 44 px, кегль 16 px (`type-body`) — iOS не приближает страницу при фокусе.
 * Внутри `Field` само получает подпись, подсказку и ошибку.
 */
export function Input({
  leadingIcon,
  trailing,
  invalid,
  disabled,
  id,
  className,
  wrapperClassName,
  ref,
  readOnly,
  "aria-describedby": describedBy,
  ...props
}: InputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const field = useFieldControl({ id, invalid, disabled, describedBy });
  // Нажатие на рамку или иконку ставит курсор в поле, как в нативном контроле.
  const focusInput = (event: PointerEvent<HTMLDivElement>) => {
    if (event.target !== inputRef.current && !(event.target as HTMLElement).closest("button")) {
      event.preventDefault();
      inputRef.current?.focus();
    }
  };
  return (
    <div
      data-invalid={field.invalid || undefined}
      data-disabled={field.disabled || undefined}
      data-readonly={readOnly || undefined}
      onPointerDown={focusInput}
      className={cn(controlFrame(), "cursor-text", readOnly && "bg-transparent", wrapperClassName)}
    >
      {leadingIcon && <Icon name={leadingIcon} size={20} className="text-fg-subtle" />}
      <input
        ref={mergeRefs(inputRef, ref)}
        id={field.id}
        disabled={field.disabled}
        readOnly={readOnly}
        aria-invalid={field["aria-invalid"]}
        aria-describedby={field["aria-describedby"]}
        className={cn(
          "h-full min-w-0 flex-1 bg-transparent type-body text-fg outline-none placeholder:text-fg-subtle",
          "disabled:cursor-not-allowed [&::-webkit-search-cancel-button]:hidden",
          className,
        )}
        {...props}
      />
      {trailing && <div className="-mr-2 flex shrink-0 items-center gap-1">{trailing}</div>}
    </div>
  );
}
