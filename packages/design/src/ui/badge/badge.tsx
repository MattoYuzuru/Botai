import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

const badgeVariants = cva(
  "inline-flex h-6 shrink-0 items-center gap-1 whitespace-nowrap rounded-sm px-2 type-caption font-medium",
  {
    variants: {
      tone: {
        neutral: "",
        accent: "",
        info: "",
        success: "",
        warning: "",
        danger: "",
      },
      variant: { subtle: "", outline: "border bg-transparent", solid: "" },
    },
    compoundVariants: [
      { tone: "neutral", variant: "subtle", className: "bg-pressed text-fg-muted" },
      { tone: "accent", variant: "subtle", className: "bg-accent-subtle text-accent-fg" },
      { tone: "info", variant: "subtle", className: "bg-info-subtle text-info-fg" },
      { tone: "success", variant: "subtle", className: "bg-success-subtle text-success-fg" },
      { tone: "warning", variant: "subtle", className: "bg-warning-subtle text-warning-fg" },
      { tone: "danger", variant: "subtle", className: "bg-danger-subtle text-danger-fg" },
      { tone: "neutral", variant: "outline", className: "border-border-strong text-fg-muted" },
      { tone: "accent", variant: "outline", className: "border-accent-fg text-accent-fg" },
      { tone: "info", variant: "outline", className: "border-info-fg text-info-fg" },
      { tone: "success", variant: "outline", className: "border-success-fg text-success-fg" },
      { tone: "warning", variant: "outline", className: "border-warning-fg text-warning-fg" },
      { tone: "danger", variant: "outline", className: "border-danger-fg text-danger-fg" },
      // Заливка: метка срочной работы и счётчик на вкладке. Текст на ней — всегда fg-on-accent или тёмный warning-fg.
      { tone: "accent", variant: "solid", className: "bg-accent text-fg-on-accent" },
      { tone: "danger", variant: "solid", className: "bg-danger text-fg-on-accent" },
      { tone: "warning", variant: "solid", className: "bg-warning text-bg-subtle" },
    ],
    defaultVariants: { tone: "neutral", variant: "subtle" },
  },
);

const dotColors = {
  neutral: "bg-control",
  accent: "bg-accent-fg",
  info: "bg-info",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger-fg",
} as const;

export interface BadgeProps
  extends Omit<ComponentProps<"span">, "color">,
    VariantProps<typeof badgeVariants> {
  icon?: IconName;
  /** Точка статуса слева вместо иконки. */
  dot?: boolean;
}

/** Неинтерактивная метка статуса или количества. Цвет всегда сопровождается текстом или иконкой. */
export function Badge({ tone = "neutral", variant, icon, dot, className, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone, variant }), className)} {...props}>
      {dot && (
        <span aria-hidden="true" className={cn("size-1.5 rounded-full", dotColors[tone ?? "neutral"])} />
      )}
      {icon && <Icon name={icon} size={16} />}
      {children}
    </span>
  );
}
