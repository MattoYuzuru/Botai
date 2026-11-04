import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

export interface ProfileField {
  /** Название поля, как у источника: «Группа», «Программа», «Курс», «Статус». */
  label: string;
  /** Значение строкой, как дал источник. Пустое значение поле скрывает (прочерков нет). */
  value: string | null | undefined;
}

export interface ProfileCardProps {
  /** Имя студента или заголовок профиля. */
  name?: string | null;
  /** Строка под именем: «Бакалавриат, очная форма». */
  subtitle?: string | null;
  /** Знак слева от имени: обычно `MonogramBadge`. */
  monogram?: ReactNode;
  fields: readonly ProfileField[];
  /** Обвести рамкой. Внутри `Panel` не нужна: карточки не вкладываем. */
  framed?: boolean;
  className?: string;
}

/**
 * Профиль студента: имя со знаком вуза и список «поле — значение» (`dl`). На телефоне поле стоит над значением, от
 * 640 px — подпись слева, значение справа. Поля без значения не показываются, значения не нормализуются.
 */
export function ProfileCard({
  name,
  subtitle,
  monogram,
  fields,
  framed = false,
  className,
}: ProfileCardProps) {
  const visible = fields.filter((field) => (field.value ?? "").trim() !== "");
  const hasHeader = Boolean(name || subtitle || monogram);
  return (
    <div
      data-profile-card=""
      className={cn(
        "flex min-w-0 flex-col gap-3",
        framed && "rounded-lg border border-border bg-surface p-4",
        className,
      )}
    >
      {hasHeader && (
        <div className="flex min-w-0 items-center gap-3">
          {monogram}
          <div className="flex min-w-0 flex-col gap-0.5">
            {name && <p className="break-words type-heading text-fg">{name}</p>}
            {subtitle && <p className="break-words type-caption text-fg-muted">{subtitle}</p>}
          </div>
        </div>
      )}
      {visible.length > 0 && (
        <dl className="flex min-w-0 flex-col">
          {visible.map((field) => (
            <div
              key={field.label}
              className="flex min-w-0 flex-col gap-0.5 border-t border-border py-2 first:border-t-0 sm:flex-row sm:items-baseline sm:gap-4"
            >
              <dt className="type-caption text-fg-muted sm:w-2/5 sm:shrink-0">{field.label}</dt>
              <dd className="min-w-0 break-words type-ui text-fg sm:flex-1">{field.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
