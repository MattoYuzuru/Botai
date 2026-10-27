import { cva } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";
import { type ButtonVariant, buttonVariants } from "../button/button";
import { Icon, type IconName } from "../icon";
import { Spinner } from "../spinner/spinner";
import { Tooltip } from "../tooltip/tooltip";

const squareSize = cva("shrink-0 px-0", {
  variants: {
    size: {
      md: "size-11",
      sm: "size-9 before:absolute before:-inset-[5px]",
    },
    shape: { square: "", round: "rounded-full" },
  },
  defaultVariants: { size: "md", shape: "square" },
});

export interface IconButtonProps extends Omit<ComponentProps<"button">, "children" | "color"> {
  icon: IconName;
  /** Название действия: становится `aria-label`. Обязательно — кнопка без подписи недоступна. */
  label: string;
  variant?: ButtonVariant;
  tone?: "default" | "danger";
  /** `md` — 44 px, `sm` — 36 px с зоной нажатия 44 px. */
  size?: "md" | "sm";
  shape?: "square" | "round";
  loading?: boolean;
  /** Подсказка при наведении мыши; на касание не показывается. */
  tooltip?: boolean;
}

/** Квадратная или круглая кнопка с иконкой. По умолчанию ghost. */
export function IconButton({
  icon,
  label,
  variant = "ghost",
  tone,
  size = "md",
  shape = "square",
  loading = false,
  tooltip = false,
  disabled,
  className,
  type = "button",
  ...props
}: IconButtonProps) {
  const button = (
    <button
      type={type}
      aria-label={label}
      aria-busy={loading || undefined}
      disabled={disabled || loading}
      className={cn(buttonVariants({ variant, tone, size: "md" }), squareSize({ size, shape }), className)}
      {...props}
    >
      {loading ? <Spinner /> : <Icon name={icon} size={20} />}
    </button>
  );
  return tooltip ? <Tooltip content={label}>{button}</Tooltip> : button;
}
