import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Icon, type IconName } from "../icon";

/** Классы строки списка: не ниже 56 px, вся строка — зона нажатия; для ссылок (`<Link className={listRowClass}>`). */
export const listRowClass =
  "flex min-h-14 w-full items-center gap-3 rounded-md px-3 py-3 text-left cursor-pointer transition-colors duration-fast ease-soft hover:bg-hover active:bg-pressed";

export interface ListRowBodyProps {
  icon?: IconName;
  title: ReactNode;
  /** Вторая строка: вуз, дата, код. */
  meta?: ReactNode;
  /** Значение справа: число, оценка. */
  trailing?: ReactNode;
  /** Стрелка справа: строка ведёт дальше. */
  chevron?: boolean;
}

/** Содержимое строки: иконка, название, подпись и значение справа. Не интерактивно само по себе. */
export function ListRowBody({ icon, title, meta, trailing, chevron = false }: ListRowBodyProps) {
  return (
    <>
      {icon && <Icon name={icon} size={24} className="shrink-0 text-fg-subtle" />}
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="type-subheading text-fg">{title}</span>
        {meta && (
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 type-caption text-fg-muted">
            {meta}
          </span>
        )}
      </span>
      {trailing && (
        <span className="shrink-0 type-ui text-fg-muted" data-numeric="">
          {trailing}
        </span>
      )}
      {chevron && <Icon name="chevron-right" size={20} className="shrink-0 text-fg-subtle" />}
    </>
  );
}

export interface ListRowProps extends ListRowBodyProps, Omit<ComponentProps<"button">, "title" | "children"> {
  /** Нажатие открывает карточку. Без обработчика строка — просто блок. */
  onOpen?: () => void;
}

/**
 * Строка списка записей: предмет, оценка, преподаватель, документ. С `onOpen` — кнопка, без него — блок.
 * Строка-ссылка собирается из `listRowClass` и `ListRowBody`.
 */
export function ListRow({ icon, title, meta, trailing, chevron, onOpen, className, ...props }: ListRowProps) {
  const body = (
    <ListRowBody
      icon={icon}
      title={title}
      meta={meta}
      trailing={trailing}
      chevron={chevron ?? Boolean(onOpen)}
    />
  );
  if (!onOpen) {
    return (
      <div
        className={cn(listRowClass, "cursor-default hover:bg-transparent active:bg-transparent", className)}
      >
        {body}
      </div>
    );
  }
  return (
    <button type="button" onClick={onOpen} className={cn(listRowClass, className)} {...props}>
      {body}
    </button>
  );
}
