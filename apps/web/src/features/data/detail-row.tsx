import { cn, Icon, type IconName } from "@botai/design";
import type { ReactNode } from "react";

/** Строка свойства в шите: иконка, подпись и значение. Пустое значение не рисуется. */
export function DetailRow({
  icon,
  label,
  children,
  className,
}: {
  icon: IconName;
  label: string;
  children: ReactNode;
  className?: string;
}) {
  if (children === null || children === undefined || children === "" || children === false) return null;
  return (
    <div className={cn("flex items-start gap-3 py-2", className)}>
      <Icon name={icon} size={20} className="shrink-0 text-fg-subtle" />
      <div className="flex min-w-0 flex-1 flex-col">
        <dt className="type-caption text-fg-muted">{label}</dt>
        <dd className="type-ui text-fg">{children}</dd>
      </div>
    </div>
  );
}

/** Группа строк свойств: список определений. */
export function DetailList({ children, label }: { children: ReactNode; label: string }) {
  return (
    <dl aria-label={label} className="flex flex-col divide-y divide-border">
      {children}
    </dl>
  );
}
