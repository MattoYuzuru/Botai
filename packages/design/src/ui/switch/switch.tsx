import { Switch as SwitchPrimitive } from "radix-ui";
import { type ComponentProps, type ReactNode, useId } from "react";
import { cn } from "../../lib/cn";
import { choiceRow } from "../styles";

export interface SwitchProps extends Omit<ComponentProps<typeof SwitchPrimitive.Root>, "children"> {
  label: ReactNode;
  description?: ReactNode;
}

/**
 * Переключатель: применяется сразу, без кнопки «Сохранить». Выключенный — рамка и бегунок `control`
 * (≥ 3:1), включённый — заливка `accent`. Вся строка — зона нажатия.
 */
export function Switch({ label, description, id, className, disabled, ...props }: SwitchProps) {
  const autoId = useId();
  const controlId = id ?? autoId;
  const descriptionId = description ? `${controlId}-description` : undefined;
  return (
    <label
      htmlFor={controlId}
      data-disabled={disabled || undefined}
      className={cn(choiceRow, "items-center justify-between", className)}
    >
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="type-ui text-fg">{label}</span>
        {description && (
          <span id={descriptionId} className="type-caption text-fg-muted">
            {description}
          </span>
        )}
      </span>
      <SwitchPrimitive.Root
        id={controlId}
        disabled={disabled}
        aria-describedby={descriptionId}
        className={cn(
          "group/switch relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border border-control bg-bg-subtle px-0.5",
          "transition-colors duration-fast ease-soft",
          "data-[state=checked]:border-accent data-[state=checked]:bg-accent",
          "disabled:cursor-not-allowed",
        )}
        {...props}
      >
        <SwitchPrimitive.Thumb className="block size-5 rounded-full bg-control shadow-xs transition-transform duration-base ease-soft data-[state=checked]:translate-x-5 data-[state=checked]:bg-fg-on-accent" />
      </SwitchPrimitive.Root>
    </label>
  );
}
