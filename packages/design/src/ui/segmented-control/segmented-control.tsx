import { ToggleGroup } from "radix-ui";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: IconName;
}

export interface SegmentedControlProps<T extends string> {
  options: readonly SegmentedOption<T>[];
  value: T;
  onValueChange: (value: T) => void;
  /** Подпись группы для скринридера. */
  label: string;
  className?: string;
}

/** Выбор одного из нескольких режимов. Выбранный сегмент — заливка `accent` и белый текст. */
export function SegmentedControl<T extends string>({
  options,
  value,
  onValueChange,
  label,
  className,
}: SegmentedControlProps<T>) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      // Radix отдаёт пустую строку при повторном нажатии на выбранный сегмент: выбор не снимаем.
      onValueChange={(next) => next && onValueChange(next as T)}
      aria-label={label}
      className={cn("inline-flex w-full gap-1 rounded-lg bg-bg-subtle p-1", className)}
    >
      {options.map((option) => (
        <ToggleGroup.Item
          key={option.value}
          value={option.value}
          className={cn(
            "inline-flex h-9 min-w-0 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-md px-3 type-ui font-medium text-fg-muted",
            "transition-[color,background-color] duration-fast ease-soft hover:text-fg",
            "data-[state=on]:bg-accent data-[state=on]:text-fg-on-accent data-[state=on]:shadow-xs",
            "before:absolute before:inset-x-0 before:-inset-y-1 relative",
          )}
        >
          {option.icon && <Icon name={option.icon} size={16} />}
          <span className="truncate">{option.label}</span>
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
