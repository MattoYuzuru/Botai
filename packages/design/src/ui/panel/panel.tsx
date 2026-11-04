import { createElement, type ReactNode, useId, useState } from "react";
import { cn } from "../../lib/cn";
import { EmptyState } from "../empty-state/empty-state";
import { Icon, type IconName } from "../icon";
import { MonogramBadge, type MonogramBadgeProps } from "../monogram-badge/monogram-badge";
import { SkeletonText } from "../skeleton/skeleton";

export type PanelState = "ready" | "loading" | "empty";

export interface PanelProps {
  /** Название панели: «Успеваемость», «Рейтинг потока». Заголовок раздела на странице. */
  title: string;
  /** Иконка из реестра (`ICON_NAMES`). Декоративна: название и так в заголовке. */
  icon?: IconName;
  /** Строка под названием: свежесть данных, период. */
  subtitle?: ReactNode;
  /** Вуз панели. Знак вуза справа от названия; в режиме одного вуза не нужен. */
  university?: Pick<MonogramBadgeProps, "title" | "shortTitle" | "hue">;
  /** Действия справа: меню видимости, «Открыть раздел». Каждое — зона нажатия не меньше 44 px (`IconButton`). */
  actions?: ReactNode;
  /** Уровень заголовка в структуре страницы. */
  headingLevel?: 2 | 3 | 4;
  /** Название — кнопка, сворачивающая тело. Состояние хранит хозяин (`onCollapsedChange`). */
  collapsible?: boolean;
  defaultCollapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  /** `loading` — форма будущего содержимого, `empty` — пустое состояние вместо тела. */
  state?: PanelState;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  children?: ReactNode;
  className?: string;
}

/**
 * Рамка панели «Учёбы»: карточка с заголовком (иконка, название, знак вуза, действия) и телом. Рамка у панели одна:
 * внутри неё лежат `StatGrid`, `ChartBlock bare`, `RankList`, `DataTable`, `ProfileCard`, без своих рамок. Заголовок —
 * настоящий `h2`–`h4`; сворачиваемое название — кнопка внутри него с `aria-expanded`, высота зоны 44 px.
 */
export function Panel({
  title,
  icon,
  subtitle,
  university,
  actions,
  headingLevel = 3,
  collapsible = false,
  defaultCollapsed = false,
  onCollapsedChange,
  state = "ready",
  emptyTitle = "Данных пока нет",
  emptyDescription,
  children,
  className,
}: PanelProps) {
  const titleId = useId();
  const bodyId = useId();
  const [collapsedState, setCollapsedState] = useState(defaultCollapsed);
  const collapsed = collapsible && collapsedState;
  const toggle = () => {
    const next = !collapsedState;
    setCollapsedState(next);
    onCollapsedChange?.(next);
  };
  const heading = collapsible ? (
    <button
      type="button"
      aria-expanded={!collapsed}
      aria-controls={bodyId}
      onClick={toggle}
      className="-mx-2 flex min-h-11 min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 text-left transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed"
    >
      {icon && <Icon name={icon} size={20} className="text-fg-subtle" />}
      <span id={titleId} className="min-w-0 break-words">
        {title}
      </span>
      <Icon name={collapsed ? "chevron-down" : "chevron-up"} size={16} className="ml-auto text-fg-subtle" />
    </button>
  ) : (
    <span className="flex min-h-11 min-w-0 items-center gap-2">
      {icon && <Icon name={icon} size={20} className="text-fg-subtle" />}
      <span id={titleId} className="min-w-0 break-words">
        {title}
      </span>
    </span>
  );
  return (
    <section
      aria-labelledby={titleId}
      data-panel=""
      data-collapsed={collapsed ? "" : undefined}
      className={cn("flex min-w-0 flex-col rounded-lg border border-border bg-surface text-fg", className)}
    >
      <header className="flex items-start gap-2 px-4 pt-2">
        <div className="flex min-w-0 flex-1 flex-col">
          {createElement(`h${headingLevel}`, { className: "type-heading text-fg" }, heading)}
          {subtitle && <p className="pb-1 type-caption text-fg-muted">{subtitle}</p>}
        </div>
        {(university || actions) && (
          <div className="flex shrink-0 items-center gap-2 pt-1.5">
            {university && <MonogramBadge size="sm" {...university} />}
            {actions}
          </div>
        )}
      </header>
      <div id={bodyId} hidden={collapsed} className="min-w-0 px-4 pt-2 pb-4">
        {state === "loading" ? (
          <SkeletonText lines={3} label={`Загрузка: ${title}`} />
        ) : state === "empty" ? (
          <EmptyState
            as="p"
            layout="inline"
            icon={icon ?? "info"}
            title={emptyTitle}
            description={emptyDescription}
          />
        ) : (
          children
        )}
      </div>
    </section>
  );
}
