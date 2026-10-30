import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

export interface EmptyStateProps {
  icon: IconName;
  /** Что здесь будет или почему пусто: «Дедлайнов пока нет». Без восклицаний и без «всё сдано». */
  title: string;
  description?: ReactNode;
  /** Одно действие: «Подключить вуз». */
  action?: ReactNode;
  size?: "sm" | "md";
  /**
   * `stacked` — по центру, значок над текстом (пустой экран). `inline` — компактная строка у левого края: значок
   * слева, текст справа; для пустой части экрана с другим содержимым рядом, чтобы не оставлять пустую зону.
   */
  layout?: "stacked" | "inline";
  className?: string;
  /** Уровень заголовка в структуре страницы. */
  as?: "h2" | "h3" | "p";
}

/** Пустая область. Отсутствие данных никогда не выглядит как «всё сдано» (styleguide §9). */
export function EmptyState({
  icon,
  title,
  description,
  action,
  size = "md",
  layout = "stacked",
  className,
  as: Title = "h2",
}: EmptyStateProps) {
  const inline = layout === "inline";
  return (
    <div
      data-layout={layout}
      className={cn(
        inline
          ? "flex items-start gap-3 px-1 py-2"
          : "mx-auto flex max-w-reading flex-col items-center text-center",
        !inline && (size === "md" ? "gap-3 px-4 py-10" : "gap-2 px-4 py-6"),
        className,
      )}
    >
      <span
        className={cn(
          "grid shrink-0 place-items-center rounded-lg bg-bg-subtle text-fg-muted",
          inline ? "size-10" : "size-12",
        )}
      >
        <Icon name={icon} size={24} />
      </span>
      <div className={cn("flex min-w-0 flex-col gap-1", inline && "pt-1.5")}>
        <Title className={inline ? "type-subheading text-fg" : "type-heading text-fg"}>{title}</Title>
        {description && <p className="type-ui text-fg-muted">{description}</p>}
        {inline && action && <div className="pt-1">{action}</div>}
      </div>
      {!inline && action && <div className="pt-2">{action}</div>}
    </div>
  );
}
