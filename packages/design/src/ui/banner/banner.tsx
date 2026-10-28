import { cva, type VariantProps } from "class-variance-authority";
import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

const bannerVariants = cva(
  "grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-2 rounded-lg px-4 py-3 type-ui md:flex md:items-center",
  {
    variants: {
      tone: {
        info: "bg-info-subtle text-info-fg",
        warning: "bg-warning-subtle text-warning-fg",
        danger: "bg-danger-subtle text-danger-fg",
        success: "bg-success-subtle text-success-fg",
        neutral: "bg-bg-subtle text-fg-muted",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BannerProps extends VariantProps<typeof bannerVariants> {
  icon: IconName;
  children: ReactNode;
  /** Одно бизнес-действие: «Войти снова», «Спросить в чате». Кнопки «Повторить» у полосы нет. */
  action?: ReactNode;
  className?: string;
  /** `status` объявляется скринридером вежливо — только для итогов, не для обратных отсчётов. */
  live?: boolean;
}

/** Полоса состояния: нет сети, нужен повторный вход, вуз что-то поменял. Цвет всегда дублируется иконкой и текстом. */
export function Banner({ tone, icon, children, action, className, live = false }: BannerProps) {
  return (
    <div
      role={live ? "status" : undefined}
      data-tone={tone ?? "neutral"}
      className={cn(bannerVariants({ tone }), className)}
    >
      <Icon name={icon} size={20} className="mt-0" />
      <div className="min-w-0 flex-1">{children}</div>
      {action && <div className="col-start-2 flex shrink-0 md:ml-auto">{action}</div>}
    </div>
  );
}
