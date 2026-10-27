import { type ComponentProps, useRef } from "react";
import { cn } from "../../lib/cn";
import { mergeRefs } from "../../lib/merge-refs";
import { useAutoGrow } from "../../lib/use-auto-grow";
import { useFieldControl } from "../field/field";
import { controlFrame } from "../styles";

export interface TextareaProps extends ComponentProps<"textarea"> {
  invalid?: boolean;
  /** Высота растёт вместе с текстом между `minRows` и `maxRows`, дальше — прокрутка внутри. */
  minRows?: number;
  maxRows?: number;
  wrapperClassName?: string;
}

/** Многострочное поле, кегль 16 px. Авто-рост без прыжков раскладки. */
export function Textarea({
  invalid,
  disabled,
  id,
  minRows = 1,
  maxRows = 6,
  className,
  wrapperClassName,
  ref,
  value,
  onInput,
  "aria-describedby": describedBy,
  ...props
}: TextareaProps) {
  const innerRef = useRef<HTMLTextAreaElement>(null);
  const field = useFieldControl({ id, invalid, disabled, describedBy });
  const resize = useAutoGrow(innerRef, value, maxRows);

  return (
    <div
      data-invalid={field.invalid || undefined}
      data-disabled={field.disabled || undefined}
      className={cn(controlFrame({ multiline: true }), "cursor-text", wrapperClassName)}
    >
      <textarea
        ref={mergeRefs(innerRef, ref)}
        id={field.id}
        rows={minRows}
        value={value}
        disabled={field.disabled}
        aria-invalid={field["aria-invalid"]}
        aria-describedby={field["aria-describedby"]}
        onInput={(event) => {
          resize();
          onInput?.(event);
        }}
        className={cn(
          "min-w-0 flex-1 resize-none bg-transparent type-body text-fg outline-none placeholder:text-fg-subtle",
          "disabled:cursor-not-allowed",
          className,
        )}
        {...props}
      />
    </div>
  );
}
