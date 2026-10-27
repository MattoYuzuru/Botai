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
  className,
  as: Title = "h2",
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "mx-auto flex max-w-prose flex-col items-center text-center",
        size === "md" ? "gap-3 px-4 py-10" : "gap-2 px-4 py-6",
        className,
      )}
    >
      <span className="grid size-12 place-items-center rounded-lg bg-bg-subtle text-fg-muted">
        <Icon name={icon} size={24} />
      </span>
      <div className="flex flex-col gap-1">
        <Title className="type-heading text-fg">{title}</Title>
        {description && <p className="type-ui text-fg-muted">{description}</p>}
      </div>
      {action && <div className="pt-2">{action}</div>}
    </div>
  );
}
