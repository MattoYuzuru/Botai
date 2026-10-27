import { cn } from "../../lib/cn";
import { type IconName, iconRegistry } from "./registry";

/** Разрешённые размеры (styleguide §6): 16 — плотные строки и бейджи, 20 — по умолчанию, 24 — вкладки и пустые состояния. */
export type IconSize = 16 | 20 | 24;

export interface IconProps {
  name: IconName;
  size?: IconSize;
  /** Подпись для иконки, которая несёт смысл сама по себе. Без неё иконка скрыта от скринридера. */
  label?: string;
  className?: string;
}

/** Иконка Tabler с обводкой 1.5. Единственный способ показать иконку в интерфейсе. */
export function Icon({ name, size = 20, label, className }: IconProps) {
  const Component = iconRegistry[name];
  return (
    <Component
      size={size}
      stroke={1.5}
      className={cn("shrink-0", className)}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      focusable="false"
      data-icon={name}
    />
  );
}
