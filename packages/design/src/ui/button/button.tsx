import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";
import { Spinner } from "../spinner/spinner";

/**
 * Три уровня важности: primary — одно главное действие на экране (заливка `accent`, белый текст),
 * secondary — обычные, ghost — в строках и панелях. Тон `danger` — только необратимое.
 * Высота 44 px (`md`); компактная 36 px (`sm`) расширяет зону нажатия до 44 px через ::before.
 */
export const buttonVariants = cva(
  [
    "group/button relative inline-flex shrink-0 cursor-pointer select-none items-center justify-center whitespace-nowrap rounded-md type-ui font-medium",
    "transition-[color,background-color,border-color] duration-fast ease-soft",
    "disabled:pointer-events-none disabled:opacity-45 aria-busy:pointer-events-none",
  ],
  {
    variants: {
      variant: {
        primary: "bg-accent text-fg-on-accent shadow-xs hover:bg-accent-hover active:bg-accent-hover",
        secondary:
          "border border-border-strong bg-surface text-fg shadow-xs hover:bg-hover active:bg-pressed",
        ghost: "text-fg-muted hover:bg-hover hover:text-fg active:bg-pressed",
      },
      tone: { default: "", danger: "" },
      size: {
        md: "h-11 gap-2 px-4",
        sm: "h-9 gap-1.5 px-3 before:absolute before:-inset-x-px before:-inset-y-[5px]",
      },
      fullWidth: { true: "w-full", false: "" },
    },
    compoundVariants: [
      {
        variant: "primary",
        tone: "danger",
        className: "bg-danger hover:bg-danger-hover active:bg-danger-hover",
      },
      { variant: "secondary", tone: "danger", className: "text-danger-fg" },
      { variant: "ghost", tone: "danger", className: "text-danger-fg hover:text-danger-fg" },
    ],
    defaultVariants: { variant: "secondary", tone: "default", size: "md", fullWidth: false },
  },
);

export type ButtonVariant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>;
export type ButtonSize = NonNullable<VariantProps<typeof buttonVariants>["size"]>;

export interface ButtonProps
  extends Omit<ComponentProps<"button">, "color">,
    VariantProps<typeof buttonVariants> {
  /** Иконка слева от подписи. */
  leadingIcon?: IconName;
  /** Иконка справа от подписи. */
  trailingIcon?: IconName;
  /**
   * Идёт действие: подпись скрыта, в центре спиннер, ширина не меняется,
   * кнопка `disabled` и `aria-busy`, повторное нажатие невозможно.
   */
  loading?: boolean;
  /** Отрисовать дочерний элемент (ссылку) со стилями кнопки. */
  asChild?: boolean;
}

export function Button({
  variant,
  tone,
  size,
  fullWidth,
  leadingIcon,
  trailingIcon,
  loading = false,
  asChild = false,
  disabled,
  className,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  const classes = cn(buttonVariants({ variant, tone, size, fullWidth }), className);
  if (asChild) {
    return (
      <Slot.Root className={classes} {...props}>
        {children}
      </Slot.Root>
    );
  }
  return (
    <button
      type={type}
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      data-loading={loading || undefined}
      {...props}
    >
      <span className={cn("inline-flex items-center justify-center gap-[inherit]", loading && "invisible")}>
        {leadingIcon && <Icon name={leadingIcon} size={20} />}
        {children}
        {trailingIcon && <Icon name={trailingIcon} size={20} />}
      </span>
      {loading && (
        <span className="absolute inset-0 grid place-items-center">
          <Spinner />
        </span>
      )}
    </button>
  );
}
