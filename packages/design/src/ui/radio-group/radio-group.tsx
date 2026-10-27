import { RadioGroup as RadioGroupPrimitive } from "radix-ui";
import { type ComponentProps, type ReactNode, useId } from "react";
import { cn } from "../../lib/cn";
import { choiceRow } from "../styles";

export interface RadioItem {
  value: string;
  label: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
}

export interface RadioGroupProps extends Omit<ComponentProps<typeof RadioGroupPrimitive.Root>, "children"> {
  /** Варианты списком; либо передайте `RadioGroupItem` как дочерние элементы. */
  items?: readonly RadioItem[];
  invalid?: boolean;
  children?: ReactNode;
}

/** Группа взаимоисключающих вариантов. Стрелки переключают выбор, Tab уходит из группы. */
export function RadioGroup({ items, invalid, className, children, ...props }: RadioGroupProps) {
  return (
    <RadioGroupPrimitive.Root
      aria-invalid={invalid || undefined}
      data-invalid={invalid || undefined}
      className={cn("flex flex-col gap-0.5", className)}
      {...props}
    >
      {items?.map((item) => (
        <RadioGroupItem
          key={item.value}
          value={item.value}
          label={item.label}
          description={item.description}
          disabled={item.disabled}
        />
      ))}
      {children}
    </RadioGroupPrimitive.Root>
  );
}

export interface RadioGroupItemProps
  extends Omit<ComponentProps<typeof RadioGroupPrimitive.Item>, "children"> {
  label: ReactNode;
  description?: ReactNode;
}

/** Радио: вся строка — зона нажатия не меньше 44 px; рамка — `control`, выбранный — заливка `accent`. */
export function RadioGroupItem({
  label,
  description,
  id,
  className,
  disabled,
  ...props
}: RadioGroupItemProps) {
  const autoId = useId();
  const controlId = id ?? autoId;
  const descriptionId = description ? `${controlId}-description` : undefined;
  return (
    <label htmlFor={controlId} data-disabled={disabled || undefined} className={cn(choiceRow, className)}>
      <RadioGroupPrimitive.Item
        id={controlId}
        disabled={disabled}
        aria-describedby={descriptionId}
        className={cn(
          "group/radio mt-0.5 grid size-5 shrink-0 cursor-pointer place-items-center rounded-full border border-control bg-surface",
          "transition-[background-color,border-color] duration-fast ease-soft",
          "data-[state=checked]:border-accent data-[state=checked]:bg-accent",
          "group-hover/row:border-fg-muted group-data-[invalid]/row:border-danger-fg",
          "disabled:cursor-not-allowed",
        )}
        {...props}
      >
        <RadioGroupPrimitive.Indicator className="size-2 rounded-full bg-fg-on-accent" />
      </RadioGroupPrimitive.Item>
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="type-ui text-fg">{label}</span>
        {description && (
          <span id={descriptionId} className="type-caption text-fg-muted">
            {description}
          </span>
        )}
      </span>
    </label>
  );
}
